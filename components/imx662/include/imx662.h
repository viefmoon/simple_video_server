/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IMX662 Camera Sensor Driver for ESP-IDF
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include "esp_cam_sensor.h"

/**
 * @brief IMX662 I2C Address (7-bit)
 */
#define IMX662_SCCB_ADDR    0x1A

/**
 * @brief IMX662 Product ID
 */
#define IMX662_PID          0x32

/**
 * @brief IMX662 Sensor Name
 */
#define IMX662_SENSOR_NAME  "IMX662"

/**
 * @brief Detect and initialize IMX662 sensor
 *
 * @param config Sensor configuration
 * @return Pointer to sensor device on success, NULL on failure
 */
esp_cam_sensor_device_t *imx662_detect(esp_cam_sensor_config_t *config);

#ifdef __cplusplus
}
#endif
