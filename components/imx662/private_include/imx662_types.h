/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IMX662 Type Definitions
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/**
 * @brief IMX662 register information structure
 */
typedef struct {
    uint16_t reg;   /*!< Register address */
    uint8_t val;    /*!< Register value */
} imx662_reginfo_t;

#ifdef __cplusplus
}
#endif
