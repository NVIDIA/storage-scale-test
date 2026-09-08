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

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/hmac.h>
#include <openssl/evp.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>
#include <unistd.h>
#include <ctype.h>

#define BUFFER_SIZE 8192
#define RESPONSE_SIZE 262144  // 256 KiB
#define EMPTY_PAYLOAD_HASH "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// Credential structure to track ownership
typedef struct {
    char *access_key;
    char *secret_key;
    int access_key_owned;  // 1 if we allocated it, 0 if from getenv
    int secret_key_owned;
} S3Credentials;

// S3 client configuration
typedef struct {
    const char *bucket;
    const char *region;
    const char *host;
    int port;
    S3Credentials creds;
    SSL_CTX *ctx;
} S3Config;

static void print_debug_info(void) {
    fprintf(stderr, "Environment variables:\n");
    fprintf(stderr, "  OBJ_BUCKET=%s\n", getenv("OBJ_BUCKET") ? getenv("OBJ_BUCKET") : "");
    fprintf(stderr, "  OBJ_REGION=%s\n", getenv("OBJ_REGION") ? getenv("OBJ_REGION") : "");
    fprintf(stderr, "  OBJ_HOST=%s\n", getenv("OBJ_HOST") ? getenv("OBJ_HOST") : "");
    fprintf(stderr, "  OBJ_HOST_PORT=%s\n", getenv("OBJ_HOST_PORT") ? getenv("OBJ_HOST_PORT") : "");
    fprintf(stderr, "  WARP_ACCESS_KEY=%s\n", getenv("WARP_ACCESS_KEY") ? getenv("WARP_ACCESS_KEY") : "");
    fprintf(stderr, "  WARP_SECRET_KEY=*****\n");
}

static void print_usage(void) {
    fprintf(stderr, "Required environment variables:\n"
            "  OBJ_BUCKET      - bucket name\n"
            "  OBJ_REGION      - region\n"
            "  OBJ_HOST        - endpoint hostname\n"
            "  OBJ_HOST_PORT   - port number (usually 443)\n"
            "  WARP_ACCESS_KEY - access key\n"
            "  WARP_SECRET_KEY - secret key\n");
    exit(1);
}

static void free_credentials(S3Credentials *creds) {
    if (creds->access_key_owned && creds->access_key) {
        free(creds->access_key);
    }
    if (creds->secret_key_owned && creds->secret_key) {
        free(creds->secret_key);
    }
    creds->access_key = NULL;
    creds->secret_key = NULL;
}

static void cleanup_config(S3Config *config) {
    if (config->ctx) {
        SSL_CTX_free(config->ctx);
        config->ctx = NULL;
    }
    free_credentials(&config->creds);
}

static int connect_socket(const char *host, int port) {
    struct hostent hostbuf;
    struct hostent *hp = NULL;
    char tmpbuf[1024];
    int herr = 0;

    if (gethostbyname_r(host, &hostbuf, tmpbuf, sizeof(tmpbuf), &hp, &herr) != 0
        || hp == NULL) {
        fprintf(stderr, "Error: DNS resolution failed for host '%s'\n", host);
        fprintf(stderr, "  h_errno: %d\n", herr);
        return -1;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    memcpy(&addr.sin_addr, hp->h_addr, hp->h_length);

    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        fprintf(stderr, "Error: Failed to create socket\n");
        perror("  socket");
        return -1;
    }

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        fprintf(stderr, "Error: Failed to connect to %s:%d\n", host, port);
        perror("  connect");
        close(sock);
        return -1;
    }

    return sock;
}

static void sha256_hex(const char *str, char out[65]) {
    unsigned char hash[32];
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (!ctx) {
        fprintf(stderr, "Error: Failed to create EVP_MD_CTX for SHA256\n");
        exit(1);
    }
    EVP_DigestInit_ex(ctx, EVP_sha256(), NULL);
    EVP_DigestUpdate(ctx, str, strlen(str));
    unsigned int len;
    EVP_DigestFinal_ex(ctx, hash, &len);
    EVP_MD_CTX_free(ctx);

    for (int i = 0; i < 32; i++)
        sprintf(out + (i * 2), "%02x", hash[i]);
    out[64] = 0;
}

static void hmac_sha256(const unsigned char *key, int key_len,
                       const unsigned char *data, int data_len,
                       unsigned char *out) {
    unsigned int len;
    HMAC(EVP_sha256(), key, key_len, data, data_len, out, &len);
}

static char *get_signing_key(const char *secret, const char *date,
                           const char *region, const char *service) {
    char k_date[32], k_region[32], k_service[32], signing_key[32];
    char key[256];
    sprintf(key, "AWS4%s", secret);

    hmac_sha256((unsigned char *)key, strlen(key),
                (unsigned char *)date, strlen(date),
                (unsigned char *)k_date);

    hmac_sha256((unsigned char *)k_date, 32,
                (unsigned char *)region, strlen(region),
                (unsigned char *)k_region);

    hmac_sha256((unsigned char *)k_region, 32,
                (unsigned char *)service, strlen(service),
                (unsigned char *)k_service);

    hmac_sha256((unsigned char *)k_service, 32,
                (unsigned char *)"aws4_request", 12,
                (unsigned char *)signing_key);

    char *result = malloc(32);
    if (!result) {
        fprintf(stderr, "Error: Memory allocation failed for signing key\n");
        exit(1);
    }
    memcpy(result, signing_key, 32);
    return result;
}

static char *extract_value(const char *line, const char *key) {
    char *start = strstr(line, key);
    if (!start) return NULL;

    start += strlen(key);
    while (*start == ' ' || *start == '=') start++;

    if (*start == '"' || *start == '\'') {
        char quote = *start;
        start++;
        char *end = strchr(start, quote);
        if (end) {
            return strndup(start, end - start);
        }
    } else {
        char *end = strpbrk(start, " \n\r");
        if (end) {
            return strndup(start, end - start);
        }
        return strdup(start);
    }
    return NULL;
}

static void update_credential(char **key, int *owned, char *new_value) {
    if (!new_value) return;
    if (*owned && *key) {
        free(*key);
    }
    *key = new_value;
    *owned = 1;
}

static void load_credentials_from_file(S3Credentials *creds) {
    const char *auth_file = getenv("OBJ_AUTH_FILE");
    if (!auth_file) return;

    FILE *f = fopen(auth_file, "r");
    if (!f) {
        fprintf(stderr, "Error: Failed to open OBJ_AUTH_FILE '%s'\n", auth_file);
        perror("  fopen");
        return;
    }

    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        if (strstr(line, "export WARP_ACCESS_KEY")) {
            char *val = extract_value(line, "WARP_ACCESS_KEY");
            update_credential(&creds->access_key, &creds->access_key_owned, val);
        }
        if (strstr(line, "export WARP_SECRET_KEY")) {
            char *val = extract_value(line, "WARP_SECRET_KEY");
            update_credential(&creds->secret_key, &creds->secret_key_owned, val);
        }
    }
    fclose(f);
}

static int init_credentials(S3Credentials *creds) {
    creds->access_key = getenv("WARP_ACCESS_KEY");
    creds->secret_key = getenv("WARP_SECRET_KEY");
    creds->access_key_owned = 0;
    creds->secret_key_owned = 0;

    // Try loading from auth file if missing
    if (!creds->access_key || !creds->secret_key) {
        load_credentials_from_file(creds);
    }

    return (creds->access_key && creds->secret_key) ? 0 : -1;
}

static SSL_CTX *create_ssl_context(void) {
    SSL_library_init();
    SSL_load_error_strings();
    OpenSSL_add_all_algorithms();

    SSL_CTX *ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) {
        fprintf(stderr, "Error: Failed to create SSL context\n");
        fprintf(stderr, "SSL_CTX_new: %s\n", ERR_error_string(ERR_get_error(), NULL));
        return NULL;
    }

    // Enforce TLS 1.2+ and enable certificate/hostname verification
    SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, NULL);

    // Load system CA certificates for peer verification
    if (!SSL_CTX_set_default_verify_paths(ctx)) {
        fprintf(stderr, "Warning: Failed to load default CA certificates\n");
        fprintf(stderr, "  Set SSL_CERT_FILE or SSL_CERT_DIR to override\n");
    }

    return ctx;
}

static SSL *create_ssl_connection(SSL_CTX *ctx, const char *host, int port, int *sock_out) {
    int sock = connect_socket(host, port);
    if (sock < 0) {
        return NULL;
    }

    SSL *ssl = SSL_new(ctx);
    if (!ssl) {
        fprintf(stderr, "Error: Failed to create SSL object\n");
        fprintf(stderr, "SSL_new: %s\n", ERR_error_string(ERR_get_error(), NULL));
        close(sock);
        return NULL;
    }

    SSL_set_fd(ssl, sock);
    SSL_set_tlsext_host_name(ssl, host);
    SSL_set1_host(ssl, host);

    if (SSL_connect(ssl) != 1) {
        fprintf(stderr, "Error: SSL handshake failed with %s:%d\n", host, port);
        fprintf(stderr, "SSL_connect: %s\n", ERR_error_string(ERR_get_error(), NULL));
        SSL_free(ssl);
        close(sock);
        return NULL;
    }

    *sock_out = sock;
    return ssl;
}

static void close_ssl_connection(SSL *ssl, int sock) {
    SSL_shutdown(ssl);
    SSL_free(ssl);
    close(sock);
}

static void build_canonical_request(char *out, size_t size, const char *bucket,
                                    const char *host, const char *amz_date,
                                    const char *continuation_token) {
    snprintf(out, size,
            "GET\n"
            "/%s\n"
            "%s%s%slist-type=2\n"
            "host:%s\n"
            "x-amz-content-sha256:%s\n"
            "x-amz-date:%s\n"
            "\n"
            "host;x-amz-content-sha256;x-amz-date\n"
            "%s",
            bucket,
            continuation_token ? "continuation-token=" : "",
            continuation_token ? continuation_token : "",
            continuation_token ? "&" : "",
            host, EMPTY_PAYLOAD_HASH, amz_date, EMPTY_PAYLOAD_HASH);
}

static void compute_signature(char signature[65], const char *secret_key,
                             const char *canonical_request, const char *amz_date,
                             const char *date_only, const char *region) {
    char cr_hash[65];
    sha256_hex(canonical_request, cr_hash);

    char string_to_sign[512];
    snprintf(string_to_sign, sizeof(string_to_sign),
             "AWS4-HMAC-SHA256\n%s\n%s/%s/s3/aws4_request\n%s",
             amz_date, date_only, region, cr_hash);

    char *signing_key = get_signing_key(secret_key, date_only, region, "s3");
    unsigned char signature_bin[32];
    hmac_sha256((unsigned char *)signing_key, 32,
                (unsigned char *)string_to_sign, strlen(string_to_sign),
                signature_bin);
    free(signing_key);

    for (int i = 0; i < 32; i++)
        sprintf(signature + (i * 2), "%02x", signature_bin[i]);
    signature[64] = 0;
}

static void build_http_request(char *out, size_t size, const S3Config *config,
                              const char *amz_date, const char *date_only,
                              const char *signature, const char *continuation_token) {
    snprintf(out, size,
            "GET /%s?%s%s%slist-type=2 HTTP/1.1\r\n"
            "Host: %s\r\n"
            "x-amz-content-sha256: %s\r\n"
            "x-amz-date: %s\r\n"
            "Authorization: AWS4-HMAC-SHA256 Credential=%s/%s/%s/s3/aws4_request,"
            "SignedHeaders=host;x-amz-content-sha256;x-amz-date,Signature=%s\r\n"
            "Connection: close\r\n\r\n",
            config->bucket,
            continuation_token ? "continuation-token=" : "",
            continuation_token ? continuation_token : "",
            continuation_token ? "&" : "",
            config->host, EMPTY_PAYLOAD_HASH, amz_date,
            config->creds.access_key, date_only, config->region, signature);
}

static int send_request(SSL *ssl, const char *request) {
    int result = SSL_write(ssl, request, strlen(request));
    if (result <= 0) {
        int ssl_error_code = SSL_get_error(ssl, result);
        fprintf(stderr, "Error: Failed to send HTTP request\n");
        fprintf(stderr, "  SSL_write returned %d, SSL error: %d\n", result, ssl_error_code);
        return -1;
    }
    return 0;
}

static int read_response(SSL *ssl, char *response, size_t response_size, int *status_code) {
    char buffer[BUFFER_SIZE];
    int total_len = 0;
    *status_code = 0;

    int bytes;
    while ((bytes = SSL_read(ssl, buffer, sizeof(buffer) - 1)) > 0) {
        buffer[bytes] = 0;
        if (!*status_code && strncmp(buffer, "HTTP/1.1 ", 9) == 0) {
            *status_code = atoi(buffer + 9);
        }
        size_t remaining = response_size - total_len;
        if (remaining > 0) {
            size_t copy_len = ((size_t)bytes < remaining) ? (size_t)bytes : remaining;
            memcpy(response + total_len, buffer, copy_len);
            total_len += copy_len;
        }
    }

    if (bytes < 0) {
        int ssl_error_code = SSL_get_error(ssl, bytes);
        fprintf(stderr, "Error: Failed to read HTTP response\n");
        fprintf(stderr, "  SSL_read returned %d, SSL error: %d\n", bytes, ssl_error_code);
        return -1;
    }

    return total_len;
}

static int extract_key_count(const char *xml) {
    const char *keycount_start = strstr(xml, "<KeyCount>");
    if (keycount_start) {
        return atoi(keycount_start + 10);
    }
    return 0;
}

static char *url_encode(const char *str, int len) {
    char *encoded = malloc(len * 3 + 1);
    if (!encoded) return NULL;

    char *p = encoded;
    for (int i = 0; i < len; i++) {
        unsigned char c = str[i];
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            *p++ = c;
        } else {
            sprintf(p, "%%%02X", c);
            p += 3;
        }
    }
    *p = 0;
    return encoded;
}

static char *extract_continuation_token(const char *xml) {
    if (!strstr(xml, "<IsTruncated>true</IsTruncated>")) {
        return NULL;
    }

    const char *token_start = strstr(xml, "<NextContinuationToken>");
    if (!token_start) return NULL;

    const char *token_end = strstr(token_start, "</NextContinuationToken>");
    if (!token_end) return NULL;

    int len = token_end - (token_start + 23);
    if (len <= 0 || len > 1023) return NULL;

    char raw_token[1024];
    memcpy(raw_token, token_start + 23, len);
    raw_token[len] = 0;

    return url_encode(raw_token, len);
}

static int perform_list_request(S3Config *config, const char *continuation_token,
                               int *key_count, char **next_token) {
    time_t now;
    struct tm tm_now;
    char amz_date[17];
    char date_only[9];

    time(&now);
    gmtime_r(&now, &tm_now);
    strftime(amz_date, sizeof(amz_date), "%Y%m%dT%H%M%SZ", &tm_now);
    strftime(date_only, sizeof(date_only), "%Y%m%d", &tm_now);

    // Build and sign request
    char canonical_request[1024];
    build_canonical_request(canonical_request, sizeof(canonical_request),
                           config->bucket, config->host, amz_date, continuation_token);

    char signature[65];
    compute_signature(signature, config->creds.secret_key, canonical_request,
                     amz_date, date_only, config->region);

    char request[2048];
    build_http_request(request, sizeof(request), config, amz_date, date_only,
                      signature, continuation_token);

    // Create connection and send request
    int sock;
    SSL *ssl = create_ssl_connection(config->ctx, config->host, config->port, &sock);
    if (!ssl) {
        print_debug_info();
        return -1;
    }

    if (send_request(ssl, request) < 0) {
        close_ssl_connection(ssl, sock);
        print_debug_info();
        return -1;
    }

    // Read response
    char *response = calloc(1, RESPONSE_SIZE);
    if (!response) {
        fprintf(stderr, "Error: Memory allocation failed for response buffer\n");
        close_ssl_connection(ssl, sock);
        return -1;
    }

    int status_code;
    int total_len = read_response(ssl, response, RESPONSE_SIZE, &status_code);
    close_ssl_connection(ssl, sock);

    if (total_len < 0) {
        free(response);
        print_debug_info();
        return -1;
    }

    if (status_code != 200) {
        fprintf(stderr, "Error: HTTP request failed with status code %d\n", status_code);
        fprintf(stderr, "Response headers and body:\n");
        printf("%s", response);
        free(response);
        print_debug_info();
        return -1;
    }

    // Parse response
    const char *xml_start = strstr(response, "<?xml");
    if (!xml_start) {
        fprintf(stderr, "Error: No XML response found in server response\n");
        fprintf(stderr, "Response length: %d bytes\n", total_len);
        fprintf(stderr, "First 500 bytes of response:\n");
        int preview_len = total_len < 500 ? total_len : 500;
        fwrite(response, 1, preview_len, stderr);
        fprintf(stderr, "\n");
        free(response);
        print_debug_info();
        return -1;
    }

    *key_count = extract_key_count(xml_start);
    *next_token = extract_continuation_token(xml_start);

    free(response);
    return 0;
}

static int count_bucket_objects(S3Config *config) {
    int total_objects = 0;
    char *continuation_token = NULL;

    do {
        int key_count = 0;
        char *next_token = NULL;

        if (perform_list_request(config, continuation_token, &key_count, &next_token) < 0) {
            free(continuation_token);
            return -1;
        }

        total_objects += key_count;
        free(continuation_token);
        continuation_token = next_token;

    } while (continuation_token);

    return total_objects;
}

int main(int argc, char *argv[]) {
    if (argc > 1 && (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0)) {
        print_usage();
    }

    // Initialize configuration
    S3Config config = {0};
    config.bucket = getenv("OBJ_BUCKET");
    config.region = getenv("OBJ_REGION");
    config.host = getenv("OBJ_HOST");

    const char *port_str = getenv("OBJ_HOST_PORT");
    if (port_str) {
        config.port = atoi(port_str);
    }

    // Load credentials
    if (init_credentials(&config.creds) < 0) {
        fprintf(stderr, "Missing required environment variables\n\n");
        print_debug_info();
        free_credentials(&config.creds);
        print_usage();
    }

    // Validate configuration
    if (!config.bucket || !config.region || !config.host || !port_str) {
        fprintf(stderr, "Missing required environment variables\n\n");
        print_debug_info();
        free_credentials(&config.creds);
        print_usage();
    }

    if (config.port <= 0 || config.port > 65535) {
        fprintf(stderr, "Error: Invalid port number '%s' (must be 1-65535)\n", port_str);
        print_debug_info();
        free_credentials(&config.creds);
        return 1;
    }

    // Create SSL context
    config.ctx = create_ssl_context();
    if (!config.ctx) {
        print_debug_info();
        free_credentials(&config.creds);
        return 1;
    }

    // Count objects in bucket
    int total = count_bucket_objects(&config);
    if (total < 0) {
        cleanup_config(&config);
        return 1;
    }

    printf("%d\n", total);
    cleanup_config(&config);
    return 0;
}
