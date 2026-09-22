# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        file \
        findutils \
        gawk \
        gzip \
        iproute2 \
        openssh-client \
        openssh-server \
        procps \
        psmisc \
        tar \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 2000 storage-test \
    && useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash tester \
    && install -d -m 0755 /run/sshd \
    && printf '%s\n' \
        'PasswordAuthentication no' \
        'PermitRootLogin no' \
        'PubkeyAuthentication yes' \
        'AllowUsers tester' \
        >> /etc/ssh/sshd_config \
    && rm -f /etc/ssh/ssh_host_*

EXPOSE 22
CMD ["bash", "-c", "ssh-keygen -A && exec /usr/sbin/sshd -D -e"]
