/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: ESPRESSIF MIT
 *
 * RAW10 Camera Capture to SD Card
 * Captures RAW10 Bayer RGGB frames from IMX662 and saves to SD card
 */

#include <string.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/errno.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_err.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "driver/sdmmc_host.h"
#include "sd_pwr_ctrl_by_on_chip_ldo.h"
#include "example_video_common.h"

/* Configuration */
#define VIDEO_BUFFER_COUNT      2
#define MOUNT_POINT             "/sdcard"
#define FRAMES_TO_CAPTURE       3       /* Number of frames to save */
#define FRAME_INTERVAL_MS       2000    /* Interval between saves (ms) */

/* SD Card Pin Configuration for ESP32-P4 */
#define SD_PIN_CLK              43
#define SD_PIN_CMD              44
#define SD_PIN_D0               39
#define SD_PIN_D1               40
#define SD_PIN_D2               41
#define SD_PIN_D3               42
#define SD_LDO_CHANNEL_ID       4

static const char *TAG = "raw_capture";

/* Camera state */
typedef struct {
    int fd;
    uint8_t *buffer[VIDEO_BUFFER_COUNT];
    uint32_t buffer_size;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format;
    uint32_t bytesperline;  /* Stride - bytes per line including padding */
    uint8_t *save_buffer;  /* Buffer for saving to SD (to avoid DMA corruption) */
} camera_t;

/* SD Card state */
typedef struct {
    sdmmc_card_t *card;
    bool mounted;
} sdcard_t;

static camera_t s_camera = {.fd = -1};
static sdcard_t s_sdcard = {0};

/*
 * Initialize SD Card with LDO power control (ESP32-P4 specific)
 */
static esp_err_t init_sdcard(void)
{
    esp_err_t ret;

    ESP_LOGI(TAG, "Initializing SD card...");

    esp_vfs_fat_sdmmc_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 5,
        .allocation_unit_size = 16 * 1024
    };

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();

    /* Configure internal LDO for SD card power (ESP32-P4 specific) */
    sd_pwr_ctrl_ldo_config_t ldo_config = {
        .ldo_chan_id = SD_LDO_CHANNEL_ID,
    };
    sd_pwr_ctrl_handle_t pwr_ctrl_handle = NULL;

    ret = sd_pwr_ctrl_new_on_chip_ldo(&ldo_config, &pwr_ctrl_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize SD card LDO power control");
        return ret;
    }
    host.pwr_ctrl_handle = pwr_ctrl_handle;

    /* Configure SDMMC slot with explicit pins */
    sdmmc_slot_config_t slot_config = SDMMC_SLOT_CONFIG_DEFAULT();
    slot_config.width = 4;
    slot_config.clk = SD_PIN_CLK;
    slot_config.cmd = SD_PIN_CMD;
    slot_config.d0 = SD_PIN_D0;
    slot_config.d1 = SD_PIN_D1;
    slot_config.d2 = SD_PIN_D2;
    slot_config.d3 = SD_PIN_D3;
    slot_config.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;

    ret = esp_vfs_fat_sdmmc_mount(MOUNT_POINT, &host, &slot_config, &mount_config, &s_sdcard.card);
    if (ret != ESP_OK) {
        if (ret == ESP_FAIL) {
            ESP_LOGE(TAG, "Failed to mount filesystem");
        } else {
            ESP_LOGE(TAG, "Failed to initialize SD card: %s", esp_err_to_name(ret));
        }
        return ret;
    }

    s_sdcard.mounted = true;
    ESP_LOGI(TAG, "SD card mounted at %s", MOUNT_POINT);
    sdmmc_card_print_info(stdout, s_sdcard.card);

    /* Verify we can write to the SD card */
    FILE *test = fopen(MOUNT_POINT "/test.txt", "w");
    if (test) {
        fprintf(test, "test");
        fclose(test);
        remove(MOUNT_POINT "/test.txt");
        ESP_LOGI(TAG, "SD card write test passed");
    } else {
        ESP_LOGE(TAG, "SD card write test FAILED: %s", strerror(errno));
    }

    return ESP_OK;
}

/*
 * Deinitialize SD Card
 */
static void deinit_sdcard(void)
{
    if (s_sdcard.mounted) {
        esp_vfs_fat_sdcard_unmount(MOUNT_POINT, s_sdcard.card);
        s_sdcard.mounted = false;
        ESP_LOGI(TAG, "SD card unmounted");
    }
}

/*
 * Initialize Camera
 */
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

    /* First get current format to know the resolution */
    memset(&format, 0, sizeof(format));
    format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(fd, VIDIOC_G_FMT, &format) != 0) {
        ESP_LOGE(TAG, "Failed to get format");
        close(fd);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Initial format: %"PRIu32"x%"PRIu32", pixfmt=0x%08lx, bytesperline=%"PRIu32,
             format.fmt.pix.width, format.fmt.pix.height,
             (unsigned long)format.fmt.pix.pixelformat, format.fmt.pix.bytesperline);

    /* Set format explicitly - RAW10 RGGB (SRGGB10) for IMX662 */
    /* V4L2_PIX_FMT_SRGGB10 = 0x30303152 = "RG10" */
    format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    format.fmt.pix.width = 1936;   /* IMX662 full sensor width (matches RPi driver) */
    format.fmt.pix.height = 1100;  /* IMX662 full sensor height (matches RPi driver) */
    format.fmt.pix.pixelformat = V4L2_PIX_FMT_SRGGB10;  /* RAW10 Bayer RGGB (IMX662 native) */
    format.fmt.pix.field = V4L2_FIELD_NONE;

    if (ioctl(fd, VIDIOC_S_FMT, &format) != 0) {
        ESP_LOGW(TAG, "Failed to set format, using default");
        /* Re-read the format */
        memset(&format, 0, sizeof(format));
        format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        ioctl(fd, VIDIOC_G_FMT, &format);
    }

    s_camera.fd = fd;
    s_camera.width = format.fmt.pix.width;
    s_camera.height = format.fmt.pix.height;
    s_camera.pixel_format = format.fmt.pix.pixelformat;
    s_camera.bytesperline = format.fmt.pix.bytesperline;

    /* Calculate expected bytesperline for RAW10: (width * 10 bits) / 8 = width * 1.25 */
    /* RAW10 packed: 5 bytes per 4 pixels */
    uint32_t expected_bpl = (s_camera.width * 5) / 4;
    ESP_LOGI(TAG, "Camera: %"PRIu32"x%"PRIu32", format=0x%08lx",
             s_camera.width, s_camera.height, (unsigned long)s_camera.pixel_format);
    ESP_LOGI(TAG, "Bytesperline: %"PRIu32" (expected for RAW10: %"PRIu32")",
             s_camera.bytesperline, expected_bpl);

    if (s_camera.bytesperline != expected_bpl && s_camera.bytesperline > 0) {
        ESP_LOGW(TAG, "Bytesperline mismatch! Image may have padding.");
    }

    /* Request buffers */
    memset(&req, 0, sizeof(req));
    req.count = VIDEO_BUFFER_COUNT;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_REQBUFS, &req) != 0) {
        ESP_LOGE(TAG, "Failed to request buffers");
        close(fd);
        return ESP_FAIL;
    }

    /* Map buffers */
    for (int i = 0; i < VIDEO_BUFFER_COUNT; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        if (ioctl(fd, VIDIOC_QUERYBUF, &buf) != 0) {
            ESP_LOGE(TAG, "Failed to query buffer %d", i);
            close(fd);
            return ESP_FAIL;
        }

        s_camera.buffer[i] = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buf.m.offset);
        if (s_camera.buffer[i] == MAP_FAILED) {
            ESP_LOGE(TAG, "Failed to mmap buffer %d", i);
            close(fd);
            return ESP_FAIL;
        }
        s_camera.buffer_size = buf.length;

        /* Queue buffer */
        if (ioctl(fd, VIDIOC_QBUF, &buf) != 0) {
            ESP_LOGE(TAG, "Failed to queue buffer %d", i);
            close(fd);
            return ESP_FAIL;
        }
    }

    /* Allocate save buffer in PSRAM for SD card writing */
    s_camera.save_buffer = heap_caps_malloc(s_camera.buffer_size, MALLOC_CAP_SPIRAM);
    if (s_camera.save_buffer == NULL) {
        ESP_LOGE(TAG, "Failed to allocate save buffer");
        close(fd);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "Camera initialized, buffer_size=%"PRIu32" bytes", s_camera.buffer_size);
    return ESP_OK;
}

/*
 * Start camera streaming
 */
static esp_err_t start_camera_stream(void)
{
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(s_camera.fd, VIDIOC_STREAMON, &type) != 0) {
        ESP_LOGE(TAG, "Failed to start stream");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Camera stream started");
    return ESP_OK;
}

/*
 * Stop camera streaming
 */
static esp_err_t stop_camera_stream(void)
{
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(s_camera.fd, VIDIOC_STREAMOFF, &type) != 0) {
        ESP_LOGE(TAG, "Failed to stop stream");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Camera stream stopped");
    return ESP_OK;
}

/*
 * Save RAW12 frame to SD card
 */
static esp_err_t save_raw_frame(uint8_t *data, size_t size, uint32_t frame_num)
{
    char filename[32];
    FILE *f;

    /* Simple filename: /sdcard/imgXXXX.raw */
    snprintf(filename, sizeof(filename), MOUNT_POINT"/img%04"PRIu32".raw", frame_num);

    ESP_LOGI(TAG, "Saving %s (%zu bytes)...", filename, size);

    f = fopen(filename, "wb");
    if (f == NULL) {
        ESP_LOGE(TAG, "Failed to open file for writing: %s (errno=%d)", strerror(errno), errno);
        return ESP_FAIL;
    }

    size_t written = fwrite(data, 1, size, f);
    fclose(f);

    if (written != size) {
        ESP_LOGE(TAG, "Write error: wrote %zu of %zu bytes", written, size);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Saved successfully");
    return ESP_OK;
}

/*
 * Capture and save frames task
 */
static void capture_task(void *arg)
{
    struct v4l2_buffer buf;
    uint32_t frame_count = 0;
    uint32_t saved_count = 0;
    int64_t start_time = esp_timer_get_time();
    int64_t last_save_time = start_time - (FRAME_INTERVAL_MS * 1000);  /* Force first save immediately */

    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "╔════════════════════════════════════════════════════╗");
    ESP_LOGI(TAG, "║       RAW10 CAPTURE TO SD CARD STARTING            ║");
    ESP_LOGI(TAG, "╚════════════════════════════════════════════════════╝");
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "Resolution: %"PRIu32"x%"PRIu32, s_camera.width, s_camera.height);
    ESP_LOGI(TAG, "Format: RAW10 Bayer RGGB (SRGGB10), pixfmt=0x%08lx", (unsigned long)s_camera.pixel_format);
    ESP_LOGI(TAG, "Bytesperline (stride): %"PRIu32, s_camera.bytesperline);
    ESP_LOGI(TAG, "Frame size: %"PRIu32" bytes (RAW10 expected: %d)", s_camera.buffer_size, (1936*1100*10)/8);
    ESP_LOGI(TAG, "Frames to capture: %d", FRAMES_TO_CAPTURE);
    ESP_LOGI(TAG, "Interval: %d ms", FRAME_INTERVAL_MS);
    ESP_LOGI(TAG, "");

    while (saved_count < FRAMES_TO_CAPTURE) {
        /* Dequeue buffer */
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;

        if (ioctl(s_camera.fd, VIDIOC_DQBUF, &buf) != 0) {
            ESP_LOGE(TAG, "VIDIOC_DQBUF failed: %s", strerror(errno));
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        frame_count++;
        int64_t now = esp_timer_get_time();

        /* Log frame reception */
        float elapsed = (now - start_time) / 1000000.0f;
        float fps = frame_count / elapsed;
        ESP_LOGI(TAG, "Frame %"PRIu32": %"PRIu32" bytes @ %.1f fps",
                 frame_count, buf.bytesused, fps);

        /* Debug: Show first bytes of first few lines to check alignment */
        if (frame_count <= 2) {
            uint8_t *frame_data = s_camera.buffer[buf.index];
            /* RAW10: 5 bytes per 4 pixels, RAW12: 3 bytes per 2 pixels */
            uint32_t bpl = s_camera.bytesperline > 0 ? s_camera.bytesperline : (s_camera.width * 5) / 4;
            ESP_LOGI(TAG, "DEBUG: First 12 bytes of lines 0-3 (bytesperline=%"PRIu32"):", bpl);
            for (int line = 0; line < 4; line++) {
                uint8_t *line_ptr = frame_data + (line * bpl);
                ESP_LOGI(TAG, "  Line %d: %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X",
                         line,
                         line_ptr[0], line_ptr[1], line_ptr[2], line_ptr[3],
                         line_ptr[4], line_ptr[5], line_ptr[6], line_ptr[7],
                         line_ptr[8], line_ptr[9], line_ptr[10], line_ptr[11]);
            }
        }

        /* Check if it's time to save */
        if ((now - last_save_time) >= (FRAME_INTERVAL_MS * 1000)) {
            /* Copy data to save buffer */
            memcpy(s_camera.save_buffer, s_camera.buffer[buf.index], buf.bytesused);
            size_t bytes_to_save = buf.bytesused;

            /* Stop streaming while writing to SD to avoid DMA overflow */
            int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            ioctl(s_camera.fd, VIDIOC_STREAMOFF, &type);

            /* Write to SD card (slow operation) */
            esp_err_t ret = save_raw_frame(s_camera.save_buffer, bytes_to_save, saved_count + 1);
            if (ret == ESP_OK) {
                saved_count++;
                last_save_time = esp_timer_get_time();  /* Update after save completes */
            }

            /* Re-queue all buffers and restart streaming */
            for (int i = 0; i < VIDEO_BUFFER_COUNT; i++) {
                struct v4l2_buffer qbuf;
                memset(&qbuf, 0, sizeof(qbuf));
                qbuf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
                qbuf.memory = V4L2_MEMORY_MMAP;
                qbuf.index = i;
                ioctl(s_camera.fd, VIDIOC_QBUF, &qbuf);
            }
            ioctl(s_camera.fd, VIDIOC_STREAMON, &type);

            /* Reset timing for accurate FPS after pause */
            start_time = esp_timer_get_time();
            frame_count = 0;
        } else {
            /* Re-queue buffer */
            ioctl(s_camera.fd, VIDIOC_QBUF, &buf);
        }
    }

    /* Final report */
    int64_t total_time = esp_timer_get_time() - start_time;
    float total_sec = total_time / 1000000.0f;

    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "╔════════════════════════════════════════════════════╗");
    ESP_LOGI(TAG, "║           CAPTURE COMPLETE                         ║");
    ESP_LOGI(TAG, "╠════════════════════════════════════════════════════╣");
    ESP_LOGI(TAG, "║  Frames received:  %4"PRIu32"                           ║", frame_count);
    ESP_LOGI(TAG, "║  Frames saved:     %4"PRIu32"                           ║", saved_count);
    ESP_LOGI(TAG, "║  Average FPS:      %5.1f                          ║", frame_count / total_sec);
    ESP_LOGI(TAG, "║  Total time:       %5.1f sec                      ║", total_sec);
    ESP_LOGI(TAG, "╚════════════════════════════════════════════════════╝");
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "Files saved to SD card:");
    for (uint32_t i = 1; i <= saved_count; i++) {
        ESP_LOGI(TAG, "  - img%04"PRIu32".raw", i);
    }
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "To decode RAW10 RGGB, use Python script or dcraw");

    /* Stop streaming and cleanup */
    stop_camera_stream();
    deinit_sdcard();

    ESP_LOGI(TAG, "Capture task finished. Safe to remove SD card.");

    /* Keep task alive */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "===========================================");
    ESP_LOGI(TAG, "   IMX662 RAW10 Capture to SD Card");
    ESP_LOGI(TAG, "===========================================");
    ESP_LOGI(TAG, "");

    /* Initialize NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    /* Initialize SD Card first */
    ret = init_sdcard();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SD card initialization failed!");
        ESP_LOGE(TAG, "Please insert SD card and restart.");
        return;
    }

    /* Test write a simple file before camera init */
    ESP_LOGI(TAG, ">>> Testing SD card write with simple file...");
    FILE *testfile = fopen("/sdcard/test_raw.bin", "wb");
    if (testfile) {
        uint8_t testdata[1024];
        memset(testdata, 0xAA, sizeof(testdata));
        size_t written = fwrite(testdata, 1, sizeof(testdata), testfile);
        fclose(testfile);
        ESP_LOGI(TAG, ">>> Test file written: %zu bytes", written);
    } else {
        ESP_LOGE(TAG, ">>> FAILED to open test file: %s (errno=%d)", strerror(errno), errno);
    }

    /* Initialize Camera */
    ret = init_camera();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Camera initialization failed!");
        deinit_sdcard();
        return;
    }

    /* Start streaming */
    ret = start_camera_stream();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start camera stream!");
        deinit_sdcard();
        return;
    }

    /* Start capture task */
    xTaskCreate(capture_task, "capture", 8192, NULL, 5, NULL);

    ESP_LOGI(TAG, "Capture task started");
}
