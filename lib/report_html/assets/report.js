/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* Storage report behaviour: table sorting and column toggles, contents
   filter, run expansion, light/dark theme, and charts. Tables work without
   this script. Charts are built from the JSON blob only when they scroll into
   view (or their run is opened, or the page is printed), with Chart.js for
   line and bar charts and a plain canvas for heatmaps. */
(function () {
  "use strict";

  var root = document.documentElement;
  var blob = document.getElementById("report-charts");
  var specs = blob ? JSON.parse(blob.textContent) : {};
  var haveChartJs = typeof window.Chart === "function";
  var built = {};
  var palette = readPalette();

  // ----- small utilities -------------------------------------------------

  function each(selector, fn, scope) {
    Array.prototype.forEach.call((scope || document).querySelectorAll(selector), fn);
  }

  function cssVar(name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }

  function isDark() {
    if (root.classList.contains("dark")) return true;
    if (root.classList.contains("light")) return false;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function readPalette() {
    return {
      dark: isDark(),
      ink: cssVar("--ink"),
      soft: cssVar("--ink-soft"),
      grid: cssVar("--gridline"),
      rule: cssVar("--rule"),
      tipBg: cssVar("--tip-bg"),
      tipInk: cssVar("--tip-ink")
    };
  }

  function hexToRgb(hex) {
    var n = parseInt(hex.replace("#", ""), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function blend(a, b, t) {
    var x = hexToRgb(a), y = hexToRgb(b);
    return "rgb(" + [0, 1, 2].map(function (i) {
      return Math.round(x[i] + (y[i] - x[i]) * t);
    }).join(",") + ")";
  }

  function withAlpha(hex, alpha) {
    var c = hexToRgb(hex);
    return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + alpha + ")";
  }

  // Series colours suit a white page; lighten them on the dark theme.
  function seriesColor(hex) {
    return palette.dark ? blend(hex, "#ffffff", 0.3) : hex;
  }

  // ----- number formatting -----------------------------------------------

  function round(v, places) {
    return String(Number(v.toFixed(places)));
  }

  function compact(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return round(v / 1e9, 2) + "G";
    if (a >= 1e6) return round(v / 1e6, 2) + "M";
    if (a >= 1e4) return round(v / 1e3, 1) + "k";
    return round(v, a < 10 ? 3 : a < 100 ? 2 : 0);
  }

  function adaptive(v) {
    var a = Math.abs(v);
    return round(v, a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3);
  }

  function formatValue(v, ax) {
    if (v === null || v === undefined || isNaN(v)) return "n/a";
    if (ax.fmt === "gbs") return adaptive(v) + " GB/s (" + adaptive(v * 1e9 / 1073741824) + " GiB/s)";
    if (ax.fmt === "ms") return adaptive(v) + " ms";
    if (ax.fmt === "pct") return Math.round(v) + "%";
    if (ax.fmt === "ratio") return v.toFixed(3);
    return compact(v) + (ax.label && ax.label.indexOf("/") > 0 ? " " + ax.label : "");
  }

  // Log axes label fewer mantissas per decade the more decades they span.
  function logTick(value, span) {
    if (!(value > 0)) return "";
    var mantissa = Math.round(value / Math.pow(10, Math.floor(Math.log10(value) + 1e-9)) * 1000) / 1000;
    var wanted = span > 2.5 ? [1] : span > 1.2 ? [1, 2, 5] : span > 0.5 ? [1, 2, 3, 5] : [1, 2, 3, 4, 5, 6, 7, 8, 9];
    return wanted.indexOf(mantissa) >= 0 ? compact(value) : "";
  }

  // ----- Chart.js configuration ------------------------------------------

  function values(spec, axisId) {
    var out = [];
    spec.series.forEach(function (s) {
      if ((s.axis || "y") !== axisId) return;
      (s.points || []).forEach(function (p) { out.push(p[1]); });
      (s.values || []).forEach(function (v) { out.push(v); });
    });
    if (spec.band && axisId === "y") {
      spec.band.lower.concat(spec.band.upper).forEach(function (p) { out.push(p[1]); });
    }
    return out.filter(function (v) { return v !== null && v > 0; });
  }

  function scale(ax, position, data, small) {
    var s = {
      type: ax.type === "log" ? "logarithmic" : ax.type === "category" ? "category" : "linear",
      position: position,
      title: { display: !!ax.label, text: ax.label, color: palette.ink },
      grid: { color: palette.grid, drawOnChartArea: position !== "right" },
      border: { display: false },
      ticks: { color: palette.ink, maxRotation: 0, autoSkip: true, maxTicksLimit: small ? 7 : 10 }
    };
    if (ax.integer) s.ticks.precision = 0;
    if (ax.min !== undefined) s.min = ax.min;
    if (ax.max !== undefined) s.max = ax.max;
    if (s.type === "logarithmic" && data.length) {
      var span = Math.log10(Math.max.apply(null, data) / Math.min.apply(null, data));
      s.ticks.autoSkip = false;
      // Flat data get an axis of several decades; label by the wider span.
      s.ticks.callback = function (v, index, ticks) {
        var shown = Math.log10(ticks[ticks.length - 1].value / ticks[0].value) || 0;
        return logTick(v, Math.max(span, shown));
      };
    } else if (s.type === "linear" && position !== "bottom") {
      s.ticks.callback = function (v) { return ax.fmt === "pct" ? v + "%" : compact(v); };
    }
    return s;
  }

  function lineData(points, categorical) {
    if (!categorical) return points.map(function (p) { return { x: p[0], y: p[1] }; });
    var row = [];
    points.forEach(function (p) { row[p[0]] = p[1]; });
    return row;
  }

  // Series dash: true or "dash", "dot", "dashdot"; anything else is solid.
  function dashPattern(dash) {
    if (dash === "dot") return [2, 3];
    if (dash === "dashdot") return [8, 3, 2, 3];
    return dash ? [6, 4] : [];
  }

  function lineDatasets(spec, categorical) {
    var sets = [];
    if (spec.band) {
      var fill = withAlpha(spec.band.color, palette.dark ? 0.3 : 0.18);
      sets.push({ label: "p10", data: lineData(spec.band.lower, categorical), borderWidth: 0,
        pointRadius: 0, fill: false, backgroundColor: fill, hideInLegend: true });
      sets.push({ label: spec.band.name, data: lineData(spec.band.upper, categorical), borderWidth: 0,
        pointRadius: 0, fill: "-1", backgroundColor: fill });
    }
    // One marker choice per chart, so its lines look alike.
    var dense = spec.markers === false ||
      spec.series.some(function (s) { return s.points.length > 30; });
    spec.series.forEach(function (s) {
      var color = seriesColor(s.color);
      sets.push({
        label: s.name, data: lineData(s.points, categorical), yAxisID: s.axis || "y",
        borderColor: color, backgroundColor: color, borderDash: dashPattern(s.dash),
        borderWidth: dense ? 1.6 : 2.2, pointRadius: dense ? 0 : 3, pointHoverRadius: 4,
        spanGaps: true, tension: 0, hidden: !!s.hidden
      });
    });
    return sets;
  }

  function barDatasets(spec) {
    return spec.series.map(function (s) {
      var color = seriesColor(s.color);
      return { label: s.name, data: s.values, backgroundColor: color, borderColor: color,
        borderRadius: 3, maxBarThickness: 48 };
    });
  }

  // Line legends show a short stroke in the series' own dash pattern. With
  // point-style legends Chart.js takes the item style from the first point,
  // which has no dash, so copy the dataset's dash onto the item.
  function legendItems(chart) {
    var items = Chart.defaults.plugins.legend.labels.generateLabels(chart);
    if (chart.config.type !== "line") return items;
    items.forEach(function (item) {
      var dataset = chart.data.datasets[item.datasetIndex] || {};
      item.pointStyle = "line";
      item.lineWidth = 2.5;
      item.lineDash = dataset.borderDash || [];
    });
    return items;
  }

  // Shades the steady window behind the data.
  var steadyShade = {
    id: "steadyShade",
    beforeDatasetsDraw: function (chart, args, options) {
      if (!options || !options.range) return;
      var area = chart.chartArea, x = chart.scales.x, ctx = chart.ctx;
      var left = Math.max(area.left, x.getPixelForValue(options.range[0]));
      var right = Math.min(area.right, x.getPixelForValue(options.range[1]));
      if (right <= left) return;
      ctx.save();
      ctx.fillStyle = palette.dark ? "rgba(255,255,255,0.06)" : "rgba(30,50,80,0.06)";
      ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
      ctx.restore();
    }
  };

  function chartConfig(spec, small) {
    var categorical = spec.x.type === "category";
    var bar = spec.kind === "bar";
    var axes = { y: spec.y, y2: spec.y2 || spec.y };
    var scales = {
      x: scale(spec.x, "bottom", [], small),
      y: scale(spec.y, "left", values(spec, "y"), small)
    };
    if (categorical) scales.x.labels = spec.x.categories;
    if (!categorical && spec.x.type === "log") {
      var xs = [];
      spec.series.forEach(function (s) { s.points.forEach(function (p) { xs.push(p[0]); }); });
      scales.x = scale(spec.x, "bottom", xs.filter(function (v) { return v > 0; }), small);
    }
    if (spec.y2) scales.y2 = scale(spec.y2, "right", values(spec, "y2"), small);
    return {
      type: bar ? "bar" : "line",
      data: { datasets: bar ? barDatasets(spec) : lineDatasets(spec, categorical) },
      plugins: [steadyShade],
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, normalized: true,
        interaction: { mode: categorical ? "index" : "nearest", axis: "x", intersect: false },
        scales: scales,
        plugins: {
          steadyShade: { range: spec.shade || null },
          legend: { labels: { color: palette.ink, boxWidth: small ? 22 : 30, usePointStyle: !bar,
            pointStyleWidth: small ? 22 : 30,
            generateLabels: legendItems,
            filter: function (item, data) { return !data.datasets[item.datasetIndex].hideInLegend; } } },
          tooltip: {
            backgroundColor: palette.tipBg, titleColor: palette.tipInk, bodyColor: palette.tipInk,
            filter: function (item) { return item.raw !== null && item.raw !== undefined; },
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                if (categorical) return spec.x.categories[items[0].dataIndex];
                return spec.x.label + ": " + compact(items[0].parsed.x);
              },
              label: function (item) {
                var ax = axes[item.dataset.yAxisID === "y2" ? "y2" : "y"];
                return " " + item.dataset.label + ": " + formatValue(item.parsed.y, ax);
              }
            }
          }
        }
      }
    };
  }

  // ----- heatmap (no Chart.js needed) ------------------------------------

  function heatColors() {
    var low = palette.dark ? "#5b8fd9" : "#2f63b0";
    var mid = palette.dark ? "#2a3039" : "#f1f1ee";
    var high = palette.dark ? "#e0705f" : "#bb3a2f";
    var table = [];
    for (var i = 0; i <= 254; i++) {
      var t = i / 254;
      table.push(t < 0.5 ? blend(low, mid, t * 2) : blend(mid, high, (t - 0.5) * 2));
    }
    table.push(palette.dark ? "#4a515c" : "#c3c8cf");
    return table;
  }

  function Heatmap(box, spec) {
    this.box = box;
    this.h = spec.heatmap;
    this.label = spec.x.label;
    this.canvas = box.querySelector("canvas");
    this.tip = box.querySelector(".heat-tip");
    var raw = atob(this.h.data);
    this.cells = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) this.cells[i] = raw.charCodeAt(i);
    var self = this;
    this.canvas.addEventListener("mousemove", function (e) { self.hover(e); });
    this.canvas.addEventListener("mouseleave", function () { self.tip.hidden = true; });
    if (window.ResizeObserver) new ResizeObserver(function () { self.draw(); }).observe(box);
    this.draw();
  }

  Heatmap.prototype.draw = function () {
    var w = this.box.clientWidth, h = this.box.clientHeight, h0 = this.h;
    if (!w || !h) return;
    var ratio = window.devicePixelRatio || 1, ctx = this.canvas.getContext("2d");
    this.canvas.width = Math.round(w * ratio);
    this.canvas.height = Math.round(h * ratio);
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = "11px system-ui, sans-serif";
    var widest = 0;
    h0.rows.forEach(function (name) { widest = Math.max(widest, ctx.measureText(name).width); });
    var box = { left: Math.min(w * 0.35, widest + 14), top: 30, right: w - 8, bottom: h - 34 };
    this.area = box;
    var colors = heatColors(), rows = h0.rows.length, cols = h0.cols;
    var cw = (box.right - box.left) / cols, rh = (box.bottom - box.top) / rows;
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        ctx.fillStyle = colors[this.cells[r * cols + c]];
        ctx.fillRect(box.left + c * cw, box.top + r * rh, Math.ceil(cw + 0.5), Math.ceil(rh + 0.5));
      }
    }
    ctx.fillStyle = palette.ink;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    var every = Math.max(1, Math.ceil(12 / rh));
    for (r = 0; r < rows; r += every) ctx.fillText(h0.rows[r], box.left - 6, box.top + (r + 0.5) * rh, box.left - 8);
    this.drawAxis(ctx, box);
    this.drawLegend(ctx, box, colors);
  };

  Heatmap.prototype.drawAxis = function (ctx, box) {
    var start = this.h.t0, end = this.h.t0 + this.h.cols * this.h.dt;
    var steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
    var room = Math.max(2, Math.floor((box.right - box.left) / 80));
    var step = steps.filter(function (s) { return (end - start) / s <= room; })[0] || 7200;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (var t = Math.ceil(start / step) * step; t <= end; t += step) {
      var x = box.left + (t - start) / (end - start) * (box.right - box.left);
      ctx.fillText(String(t), x, box.bottom + 4);
    }
    ctx.fillText(this.label, (box.left + box.right) / 2, box.bottom + 18);
  };

  Heatmap.prototype.drawLegend = function (ctx, box, colors) {
    var width = Math.min(160, (box.right - box.left) / 2), x0 = box.right - width;
    for (var i = 0; i < width; i++) {
      ctx.fillStyle = colors[Math.round(i / (width - 1) * 254)];
      ctx.fillRect(x0 + i, 6, 1.5, 8);
    }
    ctx.fillStyle = colors[255];
    ctx.fillRect(x0 - 90, 6, 10, 8);
    ctx.fillStyle = palette.soft;
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText("missing", x0 - 76, 4);
    ctx.fillText(this.h.lo + "×", x0, 16);
    ctx.textAlign = "right";
    ctx.fillText(this.h.hi + "× median", box.right, 16);
  };

  Heatmap.prototype.hover = function (e) {
    var box = this.area, h0 = this.h;
    var rect = this.canvas.getBoundingClientRect();
    var x = e.clientX - rect.left, y = e.clientY - rect.top;
    var c = Math.floor((x - box.left) / (box.right - box.left) * h0.cols);
    var r = Math.floor((y - box.top) / (box.bottom - box.top) * h0.rows.length);
    if (c < 0 || r < 0 || c >= h0.cols || r >= h0.rows.length) { this.tip.hidden = true; return; }
    var cell = this.cells[r * h0.cols + c], t = h0.t0 + c * h0.dt;
    var share = cell === h0.missing ? "no sample"
      : (h0.lo + cell / 254 * (h0.hi - h0.lo)).toFixed(2) + "× fleet median";
    this.tip.textContent = h0.rows[r] + "\n" + round(t, 1) + "–" + round(t + h0.dt, 1) + " s\n" + share;
    this.tip.hidden = false;
    var left = x + 14 + this.tip.offsetWidth > this.box.clientWidth ? x - this.tip.offsetWidth - 10 : x + 14;
    this.tip.style.left = left + "px";
    this.tip.style.top = Math.max(0, y - this.tip.offsetHeight - 8) + "px";
  };

  // ----- chart lifecycle -------------------------------------------------

  function build(plot) {
    var canvas = plot.querySelector("canvas[data-chart]");
    var id = canvas && canvas.getAttribute("data-chart");
    var spec = id && specs[id];
    if (!spec || built[id]) return;
    if (spec.kind === "heatmap") {
      built[id] = { heat: new Heatmap(plot, spec) };
    } else if (!haveChartJs) {
      plot.classList.add("offline");
      plot.textContent = "Chart unavailable: Chart.js could not be loaded (needs network access).";
    } else {
      var small = !!plot.closest(".grid");
      built[id] = { chart: new Chart(canvas, chartConfig(spec, small)), small: small, spec: spec };
    }
  }

  function rebuildAll() {
    palette = readPalette();
    Object.keys(built).forEach(function (id) {
      var item = built[id];
      if (item.heat) { item.heat.draw(); return; }
      var canvas = item.chart.canvas;
      item.chart.destroy();
      item.chart = new Chart(canvas, chartConfig(item.spec, item.small));
    });
  }

  if (haveChartJs) {
    Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
  }
  var plots = document.querySelectorAll(".plot");
  if ("IntersectionObserver" in window) {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { watcher.unobserve(entry.target); build(entry.target); }
      });
    }, { rootMargin: "300px 0px" });
    Array.prototype.forEach.call(plots, function (p) { watcher.observe(p); });
  } else {
    Array.prototype.forEach.call(plots, build);
  }

  // Printing needs every chart: build those not yet scrolled to, and refit
  // them all to the page. Charts in collapsed runs stay collapsed (unprinted).
  function printable(el) { return !el.closest("details:not([open])"); }

  function refit() {
    Object.keys(built).forEach(function (id) {
      var item = built[id];
      if (item.heat) { if (printable(item.heat.canvas)) item.heat.draw(); return; }
      if (printable(item.chart.canvas)) item.chart.resize();
    });
  }

  function prepareForPrint() {
    Array.prototype.forEach.call(plots, function (p) { if (printable(p)) build(p); });
    refit();
  }

  window.addEventListener("beforeprint", prepareForPrint);
  window.addEventListener("afterprint", refit);
  if (window.matchMedia) {
    var printMedia = matchMedia("print");
    var onPrintMedia = function (e) { if (e.matches) prepareForPrint(); else refit(); };
    if (printMedia.addEventListener) printMedia.addEventListener("change", onPrintMedia);
    else if (printMedia.addListener) printMedia.addListener(onPrintMedia);
  }

  // ----- tables ----------------------------------------------------------

  function sortKey(cell) {
    var v = cell.getAttribute("data-v");
    return v === null ? cell.textContent.trim().toLowerCase() : parseFloat(v);
  }

  function sortBy(th) {
    var table = th.closest("table"), body = table.tBodies[0];
    var index = Array.prototype.indexOf.call(th.parentNode.children, th);
    var ascending = th.getAttribute("aria-sort") !== "ascending";
    each("th[aria-sort]", function (h) { h.removeAttribute("aria-sort"); }, table);
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = sortKey(a.cells[index]), y = sortKey(b.cells[index]);
      var order = typeof x === "number" && typeof y === "number" ? x - y
        : typeof x === "number" ? -1 : typeof y === "number" ? 1 : String(x).localeCompare(String(y));
      return ascending ? order : -order;
    });
    rows.forEach(function (row) { body.appendChild(row); });
  }

  // ----- page controls ---------------------------------------------------

  function setRuns(button) {
    var scope = button.closest(".runs") || document;
    var open = button.getAttribute("data-runs") === "open";
    each("details.run", function (d) { d.open = open; }, scope);
  }

  function toggleTheme() {
    var dark = isDark();
    root.classList.remove("dark", "light");
    root.classList.add(dark ? "light" : "dark");
    rebuildAll();
  }

  document.addEventListener("click", function (e) {
    var target = e.target;
    var th = target.closest("table.data th");
    if (th) { sortBy(th); return; }
    if (target.closest("button.cols")) {
      var table = target.closest(".tbl").querySelector("table");
      table.classList.toggle("lean");
      target.textContent = table.classList.contains("lean") ? "Show all columns" : "Show fewer columns";
    } else if (target.closest("button[data-runs]")) {
      setRuns(target.closest("button[data-runs]"));
    } else if (target.closest("button.theme")) {
      toggleTheme();
    }
  });

  var filter = document.querySelector(".toc-filter");
  if (filter) {
    filter.addEventListener("input", function () {
      var words = filter.value.toLowerCase().trim();
      each(".toc a[data-filter]", function (a) {
        a.classList.toggle("hidden", !!words && a.getAttribute("data-filter").indexOf(words) < 0);
      });
    });
  }

  // Opening a link into collapsed run detail opens the detail first.
  function reveal() {
    var id = location.hash.slice(1);
    try { id = decodeURIComponent(id); } catch (err) { /* malformed: use as is */ }
    var el = id && document.getElementById(id);
    if (!el) return;
    for (var d = el.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    el.scrollIntoView();
  }
  window.addEventListener("hashchange", reveal);
  if (location.hash) reveal();

  if (window.matchMedia) {
    var scheme = matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) scheme.addEventListener("change", rebuildAll);
  }
})();
