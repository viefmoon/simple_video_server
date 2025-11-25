/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IMX662 Register Definitions
 * Ported from Linux V4L2 driver by OCTOPUS CINEMA
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/* Chip ID */
#define IMX662_REG_CHIP_ID              0x30DC
#define IMX662_CHIP_ID                  0x32

/* Standby or streaming mode */
#define IMX662_REG_MODE_SELECT          0x3000
#define IMX662_MODE_STANDBY             0x01
#define IMX662_MODE_STREAMING           0x00

/* Register hold */
#define IMX662_REG_REGHOLD              0x3001

/* Master mode (XMASTER) - starts/stops master clock generation */
#define IMX662_REG_MASTER_MODE          0x3002
#define IMX662_REG_XMASTER              0x3002

/* Input clock selection */
#define IMX662_REG_INCK_SEL             0x3014

/* Data rate selection */
#define IMX662_REG_DATARATE_SEL         0x3015

/* Window mode */
#define IMX662_REG_WINMODE              0x3018

/* WD mode (HDR) */
#define IMX662_REG_WDMODE               0x301A

/* Binning mode */
#define IMX662_REG_ADDMODE              0x301B

/* Vertical thinning */
#define IMX662_REG_THIN_V_EN            0x301C

/* VC mode */
#define IMX662_REG_VCMODE               0x301E

/* Flip controls */
#define IMX662_REG_HREVERSE             0x3020
#define IMX662_REG_VREVERSE             0x3021

/* AD bit depth */
#define IMX662_REG_ADBIT                0x3022

/* Output bit depth */
#define IMX662_REG_MDBIT                0x3023

/* VMAX (frame length) */
#define IMX662_REG_VMAX_L               0x3028
#define IMX662_REG_VMAX_M               0x3029
#define IMX662_REG_VMAX_H               0x302A

/* HMAX (line length) */
#define IMX662_REG_HMAX_L               0x302C
#define IMX662_REG_HMAX_H               0x302D

/* Gain selection */
#define IMX662_REG_FDG_SEL0             0x3030
#define IMX662_REG_FDG_SEL1             0x3031
#define IMX662_REG_FDG_SEL2             0x3032

/* Pixel start/width horizontal */
#define IMX662_REG_PIX_HST_L            0x303C
#define IMX662_REG_PIX_HST_H            0x303D
#define IMX662_REG_PIX_HWIDTH_L         0x303E
#define IMX662_REG_PIX_HWIDTH_H         0x303F

/* Lane mode */
#define IMX662_REG_LANEMODE             0x3040

/* Pixel start/width vertical */
#define IMX662_REG_PIX_VST_L            0x3044
#define IMX662_REG_PIX_VST_H            0x3045
#define IMX662_REG_PIX_VWIDTH_L         0x3046
#define IMX662_REG_PIX_VWIDTH_H         0x3047

/* Shutter (exposure) */
#define IMX662_REG_SHR0_L               0x3050
#define IMX662_REG_SHR0_M               0x3051
#define IMX662_REG_SHR0_H               0x3052

/* Analog gain */
#define IMX662_REG_GAIN_L               0x3070
#define IMX662_REG_GAIN_H               0x3071

/* Black level */
#define IMX662_REG_BLKLEVEL_L           0x30DC
#define IMX662_REG_BLKLEVEL_H           0x30DD

/* Special registers */
#define IMX662_REG_DELAY                0xEEEE
#define IMX662_REG_END                  0xFFFF

#ifdef __cplusplus
}
#endif
