/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IMX662 Camera Sensor Driver for ESP-IDF
 * Ported from Linux V4L2 driver by OCTOPUS CINEMA
 */

#include <string.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

#include "esp_cam_sensor.h"
#include "esp_cam_sensor_detect.h"
#include "imx662_settings.h"
#include "imx662.h"

#ifndef portTICK_RATE_MS
#define portTICK_RATE_MS portTICK_PERIOD_MS
#endif
#define delay_ms(ms)  vTaskDelay((ms > portTICK_PERIOD_MS ? ms / portTICK_PERIOD_MS : 1))

static const char *TAG = "imx662";

/*
 * Format definitions - RAW10 output only (no ISP)
 * IMX662 Bayer pattern: RGGB
 * Post-processing (demosaicing) will be done in software
 *
 * MIPI configuration:
 * - DATARATE_SEL=0x06: 720 Mbps per lane
 * - 2 lanes × 720 Mbps = 1440 Mbps total bandwidth
 */

/*
 * ISP info para RAW8 output - REQUERIDO para que el ISP funcione correctamente.
 *
 * IMPORTANTE PARA NDVI:
 * - RAW8 conserva el patrón Bayer (RGGB) sin demosaicing
 * - Cada pixel sigue siendo R, G, o B puro (sin interpolación)
 * - Pierdes 2 bits de resolución (10→8 bits) pero conservas separación espectral
 * - Esto es CRÍTICO para calcular NDVI = (NIR - Red) / (NIR + Red)
 *
 * El sensor envía RAW10, el ISP lo trunca/convierte a RAW8.
 */
static const esp_cam_sensor_isp_info_t imx662_isp_info = {
    .isp_v1_info = {
        .version = SENSOR_ISP_INFO_VERSION_DEFAULT,
        .pclk = 74250000,
        .vts = 1250,   /* VMAX for 1936x1100 @ 30fps */
        .hts = 1980,   /* HMAX for 1936x1100 @ 30fps */
        .bayer_type = ESP_CAM_SENSOR_BAYER_RGGB,  /* IMX662 Bayer pattern */
    },
};

static const esp_cam_sensor_format_t imx662_format_info[] = {
    /*
     * FORMAT 0: RAW10 entrada → RAW8 salida via ISP
     *
     * Esta configuración:
     *   1. El sensor envía RAW10 por MIPI-CSI
     *   2. El ISP convierte a RAW8 (trunca 2 bits LSB)
     *   3. El patrón Bayer RGGB se conserva intacto
     *   4. Las interrupciones de frame funcionan correctamente
     *
     * Para NDVI: Los datos RAW8 mantienen la separación espectral
     * necesaria para calcular (NIR - Red) / (NIR + Red)
     */
    {
        .name = "MIPI_2lane_RAW10in_RAW8out_1936x1100_30fps",
        .format = ESP_CAM_SENSOR_PIXFORMAT_RAW10,  /* Entrada del sensor */
        .port = ESP_CAM_SENSOR_MIPI_CSI,
        .xclk = 74250000,
        .width = 1936,
        .height = 1100,
        .regs = imx662_1920x1080_30fps_2lane_raw12,
        .regs_size = sizeof(imx662_1920x1080_30fps_2lane_raw12) / sizeof(imx662_reginfo_t),
        .fps = 30,
        .isp_info = &imx662_isp_info,  /* ISP activo - salida será RAW8 */
        .mipi_info = {
            .mipi_clk = 720000000,
            .lane_num = 2,
            .line_sync_en = false,
        },
        .reserved = NULL,
    },
};

#define IMX662_FORMAT_COUNT (sizeof(imx662_format_info) / sizeof(esp_cam_sensor_format_t))

/* Read register (16-bit address, 8-bit value) */
static esp_err_t imx662_read(esp_sccb_io_handle_t sccb_handle, uint16_t reg, uint8_t *read_buf)
{
    return esp_sccb_transmit_receive_reg_a16v8(sccb_handle, reg, read_buf);
}

/* Write register (16-bit address, 8-bit value) */
static esp_err_t imx662_write(esp_sccb_io_handle_t sccb_handle, uint16_t reg, uint8_t data)
{
    return esp_sccb_transmit_reg_a16v8(sccb_handle, reg, data);
}

/* Write array of registers */
static esp_err_t imx662_write_array(esp_sccb_io_handle_t sccb_handle, const imx662_reginfo_t *regarray)
{
    int i = 0;
    esp_err_t ret = ESP_OK;

    while ((ret == ESP_OK) && regarray[i].reg != IMX662_REG_END) {
        if (regarray[i].reg != IMX662_REG_DELAY) {
            ret = imx662_write(sccb_handle, regarray[i].reg, regarray[i].val);
        } else {
            delay_ms(regarray[i].val);
        }
        i++;
    }
    ESP_LOGD(TAG, "Wrote %d registers", i);
    return ret;
}

/* Hardware reset */
static esp_err_t imx662_hw_reset(esp_cam_sensor_device_t *dev)
{
    if (dev->reset_pin >= 0) {
        gpio_set_level(dev->reset_pin, 0);
        delay_ms(10);
        gpio_set_level(dev->reset_pin, 1);
        delay_ms(10);
    }
    return ESP_OK;
}

/* Get sensor ID - IMX662 doesn't have a traditional chip ID register,
 * so we just verify I2C communication works (like Linux driver does) */
static esp_err_t imx662_get_sensor_id(esp_cam_sensor_device_t *dev, esp_cam_sensor_id_t *id)
{
    uint8_t val = 0;
    esp_err_t ret;

    ESP_LOGI(TAG, "Attempting to detect IMX662 at I2C addr 0x%02x", IMX662_SCCB_ADDR);

    /* Try to read a known register to verify I2C communication */
    ret = imx662_read(dev->sccb_handle, IMX662_REG_CHIP_ID, &val);

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read from IMX662 (I2C error: 0x%x)", ret);
        return ret;
    }

    /* IMX662 doesn't have a standard chip ID - Linux driver just checks
     * if I2C communication succeeds. We do the same but log the value. */
    ESP_LOGI(TAG, "IMX662 I2C communication OK, reg 0x%04x = 0x%02x",
             IMX662_REG_CHIP_ID, val);

    /* Set PID for the framework */
    id->pid = IMX662_PID;
    ESP_LOGI(TAG, "Detected Camera sensor PID=0x%x (%s)", id->pid, IMX662_SENSOR_NAME);

    return ESP_OK;
}

/* Set horizontal mirror */
static esp_err_t imx662_set_mirror(esp_cam_sensor_device_t *dev, int enable)
{
    return imx662_write(dev->sccb_handle, IMX662_REG_HREVERSE, enable ? 0x01 : 0x00);
}

/* Set vertical flip */
static esp_err_t imx662_set_vflip(esp_cam_sensor_device_t *dev, int enable)
{
    return imx662_write(dev->sccb_handle, IMX662_REG_VREVERSE, enable ? 0x01 : 0x00);
}

/* Set analog gain (0-240, 0.3dB steps) */
static esp_err_t imx662_set_gain(esp_cam_sensor_device_t *dev, uint32_t gain)
{
    if (gain > 240) gain = 240;

    esp_err_t ret = imx662_write(dev->sccb_handle, IMX662_REG_GAIN_L, gain & 0xFF);
    if (ret == ESP_OK) {
        ret = imx662_write(dev->sccb_handle, IMX662_REG_GAIN_H, 0x00);
    }

    return ret;
}

/* Set exposure (in lines) */
static esp_err_t imx662_set_exposure(esp_cam_sensor_device_t *dev, uint32_t exposure)
{
    esp_err_t ret;

    /* Read VMAX */
    uint8_t vmax_l, vmax_m, vmax_h;
    ret = imx662_read(dev->sccb_handle, IMX662_REG_VMAX_L, &vmax_l);
    ret |= imx662_read(dev->sccb_handle, IMX662_REG_VMAX_M, &vmax_m);
    ret |= imx662_read(dev->sccb_handle, IMX662_REG_VMAX_H, &vmax_h);

    if (ret != ESP_OK) return ret;

    uint32_t vmax = vmax_l | (vmax_m << 8) | (vmax_h << 16);

    /* SHR0 = VMAX - exposure */
    uint32_t shr0 = vmax - exposure;
    if (shr0 < 11) shr0 = 11;  /* Minimum SHR */

    ret = imx662_write(dev->sccb_handle, IMX662_REG_SHR0_L, shr0 & 0xFF);
    ret |= imx662_write(dev->sccb_handle, IMX662_REG_SHR0_M, (shr0 >> 8) & 0xFF);
    ret |= imx662_write(dev->sccb_handle, IMX662_REG_SHR0_H, (shr0 >> 16) & 0x0F);

    return ret;
}

/* Query supported formats */
static int imx662_query_support_formats(esp_cam_sensor_device_t *dev, esp_cam_sensor_format_array_t *formats)
{
    formats->count = IMX662_FORMAT_COUNT;
    formats->format_array = imx662_format_info;
    return 0;
}

/* Query capability */
static int imx662_query_support_capability(esp_cam_sensor_device_t *dev, esp_cam_sensor_capability_t *cap)
{
    cap->fmt_raw = 1;
    return 0;
}

/* Query parameter descriptor */
static int imx662_query_para_desc(esp_cam_sensor_device_t *dev, esp_cam_sensor_param_desc_t *qdesc)
{
    switch (qdesc->id) {
    case ESP_CAM_SENSOR_EXPOSURE_VAL:
        qdesc->type = ESP_CAM_SENSOR_PARAM_TYPE_NUMBER;
        qdesc->number.minimum = 52;
        qdesc->number.maximum = 49865;
        qdesc->number.step = 1;
        qdesc->default_value = 1000;
        break;
    case ESP_CAM_SENSOR_GAIN:
        qdesc->type = ESP_CAM_SENSOR_PARAM_TYPE_NUMBER;
        qdesc->number.minimum = 0;
        qdesc->number.maximum = 240;
        qdesc->number.step = 1;
        qdesc->default_value = 0;
        break;
    default:
        return ESP_ERR_INVALID_ARG;
    }
    return 0;
}

/* Get parameter */
static int imx662_get_para_value(esp_cam_sensor_device_t *dev, uint32_t id, void *arg, size_t size)
{
    /*
     * NOTE: ESP_CAM_SENSOR_DATA_SEQ_SHORT_SWAPPED causes crashes with RAW10
     * because RAW10 is packed (5 bytes per 4 pixels) and doesn't align to 16-bit.
     *
     * For now, return NOT_SUPPORTED and handle byte order in post-processing.
     */
    return ESP_ERR_NOT_SUPPORTED;
}

/* Set parameter */
static int imx662_set_para_value(esp_cam_sensor_device_t *dev, uint32_t id, const void *arg, size_t size)
{
    esp_err_t ret = ESP_OK;

    switch (id) {
    case ESP_CAM_SENSOR_EXPOSURE_VAL:
        ret = imx662_set_exposure(dev, *(uint32_t *)arg);
        break;
    case ESP_CAM_SENSOR_GAIN:
        ret = imx662_set_gain(dev, *(uint32_t *)arg);
        break;
    case ESP_CAM_SENSOR_HMIRROR:
        ret = imx662_set_mirror(dev, *(int *)arg);
        break;
    case ESP_CAM_SENSOR_VFLIP:
        ret = imx662_set_vflip(dev, *(int *)arg);
        break;
    default:
        return ESP_ERR_INVALID_ARG;
    }

    return ret;
}

/* Set format */
static int imx662_set_format(esp_cam_sensor_device_t *dev, const esp_cam_sensor_format_t *format)
{
    esp_err_t ret = ESP_OK;

    /* Validate parameters */
    if (!dev) {
        ESP_LOGE(TAG, "set_format: dev is NULL");
        return ESP_ERR_INVALID_ARG;
    }

    if (!dev->sccb_handle) {
        ESP_LOGE(TAG, "set_format: sccb_handle is NULL");
        return ESP_ERR_INVALID_STATE;
    }

    /* If format is NULL, use the current format or default */
    if (!format) {
        if (dev->cur_format) {
            ESP_LOGI(TAG, "set_format: using current format");
            format = dev->cur_format;
        } else {
            ESP_LOGI(TAG, "set_format: using default format");
            format = &imx662_format_info[0];
        }
    }

    ESP_LOGI(TAG, "set_format called: %s", format->name ? format->name : "unknown");

    /* Stop streaming first */
    ret = imx662_write(dev->sccb_handle, IMX662_REG_MODE_SELECT, IMX662_MODE_STANDBY);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set standby mode");
        return ret;
    }

    delay_ms(10);

    /* Write common init registers */
    ret = imx662_write_array(dev->sccb_handle, imx662_common_init_regs);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write common init registers");
        return ret;
    }

    /* Write format-specific registers */
    if (format->regs) {
        ret = imx662_write_array(dev->sccb_handle, (const imx662_reginfo_t *)format->regs);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to write format registers");
            return ret;
        }
    }

    dev->cur_format = format;
    ESP_LOGI(TAG, "Set format: %s", format->name ? format->name : "unknown");

    return 0;
}

/* Get current format */
static int imx662_get_format(esp_cam_sensor_device_t *dev, esp_cam_sensor_format_t *format)
{
    if (dev->cur_format) {
        memcpy(format, dev->cur_format, sizeof(esp_cam_sensor_format_t));
        return 0;
    }
    return ESP_ERR_INVALID_STATE;
}

/* Private ioctl - handles streaming control */
static int imx662_priv_ioctl(esp_cam_sensor_device_t *dev, uint32_t cmd, void *arg)
{
    esp_err_t ret = ESP_OK;
    uint8_t reg_val = 0;

    if (!dev || !dev->sccb_handle) {
        ESP_LOGE(TAG, "priv_ioctl: invalid device");
        return ESP_ERR_INVALID_ARG;
    }

    switch (ESP_CAM_SENSOR_IOC_GET_ID(cmd)) {
    case ESP_CAM_SENSOR_IOC_GET_ID(ESP_CAM_SENSOR_IOC_S_STREAM): {
        int enable = arg ? *(int *)arg : 0;
        ESP_LOGI(TAG, "Stream control: %s", enable ? "START" : "STOP");

        if (enable) {
            /* === DIAGNOSTIC: Read key registers before streaming === */
            ESP_LOGI(TAG, "=== IMX662 Pre-Stream Diagnostics ===");
            ESP_LOGI(TAG, "MIPI Config: bit_rate=%lu Mbps/lane, lanes=%d",
                     (unsigned long)(dev->cur_format->mipi_info.mipi_clk / 1000000),
                     dev->cur_format->mipi_info.lane_num);

            imx662_read(dev->sccb_handle, 0x3000, &reg_val);
            ESP_LOGI(TAG, "STANDBY (0x3000) = 0x%02X (expect 0x01)", reg_val);

            imx662_read(dev->sccb_handle, 0x3014, &reg_val);
            ESP_LOGI(TAG, "INCK_SEL (0x3014) = 0x%02X (expect 0x00 for 74.25MHz)", reg_val);

            imx662_read(dev->sccb_handle, 0x3015, &reg_val);
            ESP_LOGI(TAG, "DATARATE_SEL (0x3015) = 0x%02X (expect 0x06 for 720Mbps)", reg_val);

            imx662_read(dev->sccb_handle, 0x3040, &reg_val);
            ESP_LOGI(TAG, "LANEMODE (0x3040) = 0x%02X (expect 0x01 for 2-lane)", reg_val);

            imx662_read(dev->sccb_handle, 0x3022, &reg_val);
            ESP_LOGI(TAG, "ADBIT (0x3022) = 0x%02X (expect 0x00 for 10bit)", reg_val);

            /* Verify AD conversion registers match ADBIT setting */
            imx662_read(dev->sccb_handle, 0x3A50, &reg_val);
            ESP_LOGI(TAG, "AD_CONV0 (0x3A50) = 0x%02X (expect 0x62 for 10bit, 0xFF for 12bit)", reg_val);
            imx662_read(dev->sccb_handle, 0x3A51, &reg_val);
            ESP_LOGI(TAG, "AD_CONV1 (0x3A51) = 0x%02X (expect 0x01 for 10bit, 0x03 for 12bit)", reg_val);
            imx662_read(dev->sccb_handle, 0x3A52, &reg_val);
            ESP_LOGI(TAG, "AD_CONV2 (0x3A52) = 0x%02X (expect 0x19 for 10bit, 0x00 for 12bit)", reg_val);

            /* Read HMAX */
            uint8_t hmax_l, hmax_h;
            imx662_read(dev->sccb_handle, 0x302C, &hmax_l);
            imx662_read(dev->sccb_handle, 0x302D, &hmax_h);
            ESP_LOGI(TAG, "HMAX = %d (expect 1980)", hmax_l | (hmax_h << 8));

            /* Read VMAX */
            uint8_t vmax_l, vmax_m;
            imx662_read(dev->sccb_handle, 0x3028, &vmax_l);
            imx662_read(dev->sccb_handle, 0x3029, &vmax_m);
            ESP_LOGI(TAG, "VMAX = %d (expect 1250)", vmax_l | (vmax_m << 8));

            /* Read SHR0 (exposure) */
            uint8_t shr0_l, shr0_m;
            imx662_read(dev->sccb_handle, 0x3050, &shr0_l);
            imx662_read(dev->sccb_handle, 0x3051, &shr0_m);
            uint16_t shr0 = shr0_l | (shr0_m << 8);
            uint16_t vmax = vmax_l | (vmax_m << 8);
            ESP_LOGI(TAG, "SHR0 = %d (integration = %d lines)", shr0, vmax - shr0);

            ESP_LOGI(TAG, "=== Starting Stream Sequence ===");

            /* IMX662 Start sequence (from working RPi driver):
             * 1. Release REGHOLD
             * 2. Exit STANDBY (write 0x00)
             * 3. Wait 30ms for PLL lock
             * 4. Start master mode (XMSTA = 0x00)
             *
             * XMSTA register (0x3002):
             *   0x00 = Master mode operation START
             *   0x01 = Master mode operation STOP
             */
            ret = imx662_write(dev->sccb_handle, IMX662_REG_REGHOLD, 0x00);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "Failed to release REGHOLD");
                break;
            }
            ESP_LOGI(TAG, "REGHOLD released");

            /* Exit standby mode */
            ret = imx662_write(dev->sccb_handle, IMX662_REG_MODE_SELECT, IMX662_MODE_STREAMING);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "Failed to exit standby");
                break;
            }
            ESP_LOGI(TAG, "STANDBY released (streaming mode)");

            /* Wait for PLL to stabilize - RPi uses 30ms */
            delay_ms(30);

            /* Start master mode - XMSTA = 0x00 means START */
            ret = imx662_write(dev->sccb_handle, IMX662_REG_XMASTER, 0x00);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "Failed to start XMASTER");
                break;
            }
            ESP_LOGI(TAG, "XMASTER started (0x00 = master mode ON)");

            /* Verify streaming started */
            imx662_read(dev->sccb_handle, 0x3000, &reg_val);
            ESP_LOGI(TAG, "STANDBY after start = 0x%02X (expect 0x00)", reg_val);

            imx662_read(dev->sccb_handle, 0x3002, &reg_val);
            ESP_LOGI(TAG, "XMASTER after start = 0x%02X (expect 0x00)", reg_val);

            ESP_LOGI(TAG, "IMX662 streaming started - sensor should now output MIPI data");
        } else {
            /* Stop streaming (from RPi driver):
             * 1. Enter standby mode (STANDBY = 0x01)
             * 2. Wait
             * 3. Stop master mode (XMSTA = 0x01)
             */
            ret = imx662_write(dev->sccb_handle, IMX662_REG_MODE_SELECT, IMX662_MODE_STANDBY);
            delay_ms(30);
            ret |= imx662_write(dev->sccb_handle, IMX662_REG_XMASTER, 0x01);  /* 0x01 = STOP */
            ESP_LOGI(TAG, "IMX662 streaming stopped");
        }
        break;
    }
    case ESP_CAM_SENSOR_IOC_GET_ID(ESP_CAM_SENSOR_IOC_HW_RESET):
        ESP_LOGI(TAG, "Hardware reset requested");
        ret = imx662_hw_reset(dev);
        break;
    case ESP_CAM_SENSOR_IOC_GET_ID(ESP_CAM_SENSOR_IOC_SW_RESET):
        ESP_LOGI(TAG, "Software reset requested");
        ret = imx662_write(dev->sccb_handle, IMX662_REG_MODE_SELECT, IMX662_MODE_STANDBY);
        delay_ms(10);
        break;
    default:
        ESP_LOGW(TAG, "Unknown ioctl cmd: 0x%lx", (unsigned long)cmd);
        ret = ESP_ERR_NOT_SUPPORTED;
        break;
    }

    return ret;
}

/* Delete device */
static int imx662_del(esp_cam_sensor_device_t *dev)
{
    ESP_LOGD(TAG, "Deleting IMX662 device");
    free(dev);
    return 0;
}

/* Sensor operations */
static const esp_cam_sensor_ops_t imx662_ops = {
    .query_para_desc = imx662_query_para_desc,
    .get_para_value = imx662_get_para_value,
    .set_para_value = imx662_set_para_value,
    .query_support_formats = imx662_query_support_formats,
    .query_support_capability = imx662_query_support_capability,
    .set_format = imx662_set_format,
    .get_format = imx662_get_format,
    .priv_ioctl = imx662_priv_ioctl,
    .del = imx662_del,
};

/* Detect and initialize IMX662 - called by auto-detect system */
esp_cam_sensor_device_t *imx662_detect(esp_cam_sensor_config_t *config)
{
    esp_cam_sensor_device_t *dev = NULL;

    ESP_LOGI(TAG, "IMX662 detect called");

    if (!config) {
        ESP_LOGE(TAG, "Config is NULL");
        return NULL;
    }

    ESP_LOGI(TAG, "Config: sccb_handle=%p, reset_pin=%d, sensor_port=%d",
             config->sccb_handle, config->reset_pin, config->sensor_port);

    dev = (esp_cam_sensor_device_t *)heap_caps_calloc(1, sizeof(esp_cam_sensor_device_t), MALLOC_CAP_DEFAULT);
    if (!dev) {
        ESP_LOGE(TAG, "Failed to allocate memory for device");
        return NULL;
    }

    dev->name = (char *)IMX662_SENSOR_NAME;
    dev->sccb_handle = config->sccb_handle;
    dev->reset_pin = config->reset_pin;
    dev->pwdn_pin = config->pwdn_pin;
    dev->xclk_pin = config->xclk_pin;
    dev->sensor_port = config->sensor_port;

    /* Hardware reset if pin configured */
    ESP_LOGI(TAG, "Performing hardware reset (pin=%d)", dev->reset_pin);
    imx662_hw_reset(dev);

    /* Verify sensor ID */
    esp_cam_sensor_id_t sensor_id = {0};
    if (imx662_get_sensor_id(dev, &sensor_id) != ESP_OK) {
        ESP_LOGE(TAG, "IMX662 not detected - I2C communication failed");
        free(dev);
        return NULL;
    }

    /* Setup device */
    dev->id = sensor_id;
    dev->ops = &imx662_ops;
    dev->cur_format = &imx662_format_info[0];

    /* Initialize with default format */
    ESP_LOGI(TAG, "Setting default format...");
    if (imx662_set_format(dev, &imx662_format_info[0]) != 0) {
        ESP_LOGE(TAG, "Failed to set default format");
        free(dev);
        return NULL;
    }

    ESP_LOGI(TAG, "IMX662 initialized successfully");
    return dev;
}

/* Auto-detect registration */
#if CONFIG_CAMERA_IMX662_AUTO_DETECT
ESP_CAM_SENSOR_DETECT_FN(imx662_detect, ESP_CAM_SENSOR_MIPI_CSI, IMX662_SCCB_ADDR)
{
    ((esp_cam_sensor_config_t *)config)->sensor_port = ESP_CAM_SENSOR_MIPI_CSI;
    return imx662_detect((esp_cam_sensor_config_t *)config);
}
#endif
