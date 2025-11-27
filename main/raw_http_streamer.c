/*
 * RAW10 HTTP Streaming Server for IMX662
 * Based on simple_video_server example
 * Streams RAW Bayer frames for Python viewer to decode
 */

#include <string.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/param.h>
#include <sys/errno.h>
#include <errno.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_err.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "esp_http_server.h"
#include "protocol_examples_common.h"
#include "example_video_common.h"

/* Configuration */
#define VIDEO_BUFFER_COUNT      4  /* Increased from 2 for smoother streaming */
#define FRAME_WIDTH             1936
#define FRAME_HEIGHT            1100

/* Stream boundary for multipart */
#define STREAM_BOUNDARY         "raw_frame_boundary"
static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" STREAM_BOUNDARY;
static const char *STREAM_PART = "\r\n--" STREAM_BOUNDARY "\r\nContent-Type: application/octet-stream\r\nContent-Length: %u\r\n\r\n";

static const char *TAG = "raw_streamer";

/* Camera state */
typedef struct {
    int fd;
    uint8_t *buffer[VIDEO_BUFFER_COUNT];
    uint32_t buffer_size;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format;
    SemaphoreHandle_t sem;
} camera_t;

static camera_t s_camera = {.fd = -1};

/* ========== Camera Functions ========== */
static esp_err_t init_camera(void)
{
    int fd;
    struct v4l2_format format;
    struct v4l2_requestbuffers req;

    ESP_LOGI(TAG, "Initializing camera...");

    /* Initialize video subsystem */
    ESP_RETURN_ON_ERROR(example_video_init(), TAG, "Failed to init video");

    /* Open video device */
    fd = open(ESP_VIDEO_MIPI_CSI_DEVICE_NAME, O_RDWR);
    if (fd < 0) {
        ESP_LOGE(TAG, "Failed to open video device");
        return ESP_ERR_NOT_FOUND;
    }

    /* Set format - RGB565 for IMX662
     * The ISP will convert RAW Bayer to RGB565 automatically.
     * This avoids the buggy ISP bypass mode that freezes after first frame.
     * Supported ISP output formats: RGB565, RGB24, YUV420, YUV422P
     */
    memset(&format, 0, sizeof(format));
    format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    format.fmt.pix.width = FRAME_WIDTH;
    format.fmt.pix.height = FRAME_HEIGHT;
    format.fmt.pix.pixelformat = V4L2_PIX_FMT_RGB565;  /* ISP converts RAW to RGB565 */
    format.fmt.pix.field = V4L2_FIELD_NONE;

    if (ioctl(fd, VIDIOC_S_FMT, &format) != 0) {
        ESP_LOGW(TAG, "Failed to set RGB565 format, reading current...");
        memset(&format, 0, sizeof(format));
        format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        ioctl(fd, VIDIOC_G_FMT, &format);
    } else {
        ESP_LOGI(TAG, "RGB565 format set successfully!");
    }

    s_camera.fd = fd;
    s_camera.width = format.fmt.pix.width;
    s_camera.height = format.fmt.pix.height;
    s_camera.pixel_format = format.fmt.pix.pixelformat;

    /* Log format as 4-char code */
    char fmt_str[5] = {0};
    fmt_str[0] = (s_camera.pixel_format >> 0) & 0xFF;
    fmt_str[1] = (s_camera.pixel_format >> 8) & 0xFF;
    fmt_str[2] = (s_camera.pixel_format >> 16) & 0xFF;
    fmt_str[3] = (s_camera.pixel_format >> 24) & 0xFF;

    ESP_LOGI(TAG, "Camera: %"PRIu32"x%"PRIu32", format=%s (0x%08lx)",
             s_camera.width, s_camera.height, fmt_str, (unsigned long)s_camera.pixel_format);

    /* Request buffers */
    memset(&req, 0, sizeof(req));
    req.count = VIDEO_BUFFER_COUNT;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    ESP_RETURN_ON_FALSE(ioctl(fd, VIDIOC_REQBUFS, &req) == 0, ESP_FAIL, TAG, "REQBUFS failed");

    /* Map buffers */
    for (int i = 0; i < VIDEO_BUFFER_COUNT; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        ESP_RETURN_ON_FALSE(ioctl(fd, VIDIOC_QUERYBUF, &buf) == 0, ESP_FAIL, TAG, "QUERYBUF failed");

        s_camera.buffer[i] = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buf.m.offset);
        ESP_RETURN_ON_FALSE(s_camera.buffer[i] != MAP_FAILED, ESP_ERR_NO_MEM, TAG, "mmap failed");
        s_camera.buffer_size = buf.length;

        ESP_RETURN_ON_FALSE(ioctl(fd, VIDIOC_QBUF, &buf) == 0, ESP_FAIL, TAG, "QBUF failed");
    }

    s_camera.sem = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_camera.sem, ESP_ERR_NO_MEM, TAG, "Failed to create semaphore");
    xSemaphoreGive(s_camera.sem);

    /* Start streaming */
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ESP_RETURN_ON_FALSE(ioctl(fd, VIDIOC_STREAMON, &type) == 0, ESP_FAIL, TAG, "STREAMON failed");

    ESP_LOGI(TAG, "Camera initialized, buffer_size=%"PRIu32, s_camera.buffer_size);
    return ESP_OK;
}

/* ========== HTTP Handlers ========== */

/* Single frame capture - for Python viewer polling */
static esp_err_t capture_handler(httpd_req_t *req)
{
    struct v4l2_buffer buf;

    if (xSemaphoreTake(s_camera.sem, pdMS_TO_TICKS(1000)) != pdTRUE) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Camera busy");
        return ESP_FAIL;
    }

    /* Dequeue buffer */
    memset(&buf, 0, sizeof(buf));
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;

    if (ioctl(s_camera.fd, VIDIOC_DQBUF, &buf) != 0) {
        xSemaphoreGive(s_camera.sem);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Frame capture failed");
        return ESP_FAIL;
    }

    /* Send frame */
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "X-Frame-Width", "1936");
    httpd_resp_set_hdr(req, "X-Frame-Height", "1100");
    httpd_resp_set_hdr(req, "X-Frame-Format", "RAW10_RGGB");

    esp_err_t ret = httpd_resp_send(req, (char *)s_camera.buffer[buf.index], buf.bytesused);

    /* Re-queue buffer */
    ioctl(s_camera.fd, VIDIOC_QBUF, &buf);
    xSemaphoreGive(s_camera.sem);

    return ret;
}

/* Continuous stream handler - multipart RAW stream */
static esp_err_t stream_handler(httpd_req_t *req)
{
    struct v4l2_buffer buf;
    char part_header[128];
    esp_err_t ret = ESP_OK;
    int frame_count = 0;
    int dqbuf_errors = 0;

    ESP_LOGI(TAG, "Stream client connected");

    httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "X-Frame-Width", "1936");
    httpd_resp_set_hdr(req, "X-Frame-Height", "1100");

    while (true) {
        if (xSemaphoreTake(s_camera.sem, pdMS_TO_TICKS(1000)) != pdTRUE) {
            ESP_LOGW(TAG, "Semaphore timeout");
            continue;
        }

        /* Dequeue buffer */
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;

        ESP_LOGD(TAG, "Waiting for frame (DQBUF)...");
        if (ioctl(s_camera.fd, VIDIOC_DQBUF, &buf) != 0) {
            dqbuf_errors++;
            if (dqbuf_errors <= 5 || dqbuf_errors % 100 == 0) {
                ESP_LOGE(TAG, "DQBUF failed (errno=%d), errors=%d", errno, dqbuf_errors);
            }
            xSemaphoreGive(s_camera.sem);
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        frame_count++;
        if (frame_count <= 3 || frame_count % 30 == 0) {
            ESP_LOGI(TAG, "Frame %d: size=%"PRIu32" bytes", frame_count, buf.bytesused);
        }

        /* Send part header */
        int hlen = snprintf(part_header, sizeof(part_header), STREAM_PART, buf.bytesused);
        ret = httpd_resp_send_chunk(req, part_header, hlen);
        if (ret != ESP_OK) {
            ioctl(s_camera.fd, VIDIOC_QBUF, &buf);
            xSemaphoreGive(s_camera.sem);
            break;
        }

        /* Send frame data */
        ret = httpd_resp_send_chunk(req, (char *)s_camera.buffer[buf.index], buf.bytesused);

        /* Re-queue buffer */
        ioctl(s_camera.fd, VIDIOC_QBUF, &buf);
        xSemaphoreGive(s_camera.sem);

        if (ret != ESP_OK) {
            break;
        }
    }

    ESP_LOGI(TAG, "Stream client disconnected");
    return ret;
}

/* Status endpoint */
static esp_err_t status_handler(httpd_req_t *req)
{
    char json[256];
    snprintf(json, sizeof(json),
             "{\"width\":%"PRIu32",\"height\":%"PRIu32",\"format\":\"RAW10_RGGB\",\"buffer_size\":%"PRIu32"}",
             s_camera.width, s_camera.height, s_camera.buffer_size);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_sendstr(req, json);
}

/* Index page */
static esp_err_t index_handler(httpd_req_t *req)
{
    const char *html =
        "<!DOCTYPE html><html><head><title>IMX662 RAW Streamer</title></head>"
        "<body style='font-family:monospace;padding:20px'>"
        "<h1>IMX662 RAW10 Streaming Server</h1>"
        "<p>Resolution: 1936x1100, Format: RAW10 Bayer RGGB</p>"
        "<h2>Endpoints:</h2>"
        "<ul>"
        "<li><a href='/capture'>/capture</a> - Single RAW frame (for Python viewer)</li>"
        "<li><a href='/stream'>/stream</a> - Continuous RAW stream</li>"
        "<li><a href='/status'>/status</a> - Camera status (JSON)</li>"
        "</ul>"
        "<h2>Python Viewer:</h2>"
        "<pre>python raw_stream_viewer.py --host [THIS_IP] --port 80</pre>"
        "</body></html>";

    httpd_resp_set_type(req, "text/html");
    return httpd_resp_sendstr(req, html);
}

/* API compatibility endpoint */
static esp_err_t api_capture_handler(httpd_req_t *req)
{
    return capture_handler(req);
}

static esp_err_t init_http_server(void)
{
    httpd_handle_t server = NULL;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 8192;
    config.max_uri_handlers = 8;

    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), TAG, "Failed to start HTTP server");

    /* Register handlers */
    httpd_uri_t index_uri = { .uri = "/", .method = HTTP_GET, .handler = index_handler };
    httpd_uri_t capture_uri = { .uri = "/capture", .method = HTTP_GET, .handler = capture_handler };
    httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler };
    httpd_uri_t status_uri = { .uri = "/status", .method = HTTP_GET, .handler = status_handler };
    httpd_uri_t api_uri = { .uri = "/api/capture_binary", .method = HTTP_GET, .handler = api_capture_handler };

    httpd_register_uri_handler(server, &index_uri);
    httpd_register_uri_handler(server, &capture_uri);
    httpd_register_uri_handler(server, &stream_uri);
    httpd_register_uri_handler(server, &status_uri);
    httpd_register_uri_handler(server, &api_uri);

    ESP_LOGI(TAG, "HTTP server started on port %d", config.server_port);
    return ESP_OK;
}

/* ========== Main ========== */
void app_main(void)
{
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "╔════════════════════════════════════════════════════╗");
    ESP_LOGI(TAG, "║     IMX662 RAW10 HTTP Streaming Server             ║");
    ESP_LOGI(TAG, "╚════════════════════════════════════════════════════╝");
    ESP_LOGI(TAG, "");

    /* Initialize NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    /* Initialize network (WiFi via protocol_examples_common) */
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect());

    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "WiFi Connected! Starting camera...");
    ESP_LOGI(TAG, "");

    /* Initialize Camera */
    ESP_ERROR_CHECK(init_camera());

    /* Start HTTP Server */
    ESP_ERROR_CHECK(init_http_server());

    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "╔════════════════════════════════════════════════════╗");
    ESP_LOGI(TAG, "║           Server Ready!                            ║");
    ESP_LOGI(TAG, "╠════════════════════════════════════════════════════╣");
    ESP_LOGI(TAG, "║  Endpoints:                                        ║");
    ESP_LOGI(TAG, "║    /         - Info page                           ║");
    ESP_LOGI(TAG, "║    /capture  - Single RAW frame                    ║");
    ESP_LOGI(TAG, "║    /stream   - Continuous stream                   ║");
    ESP_LOGI(TAG, "║    /status   - JSON status                         ║");
    ESP_LOGI(TAG, "╚════════════════════════════════════════════════════╝");
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "Use Python viewer:");
    ESP_LOGI(TAG, "  python raw_stream_viewer.py --host <IP> --port 80");
    ESP_LOGI(TAG, "");
}
