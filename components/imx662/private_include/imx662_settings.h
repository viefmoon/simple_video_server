/*
 * SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IMX662 Register Settings
 * Based on Linux V4L2 driver by OCTOPUS CINEMA
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "imx662_regs.h"
#include "imx662_types.h"

/* MIPI Link frequencies (per lane) - from Linux driver */
#define IMX662_LINK_FREQ_297MHZ     297000000   /* 594 Mbps/lane, reg=0x07 */
#define IMX662_LINK_FREQ_445MHZ     445500000   /* 891 Mbps/lane, reg=0x05 */
#define IMX662_LINK_FREQ_594MHZ     594000000   /* 1188 Mbps/lane, reg=0x04 */
#define IMX662_LINK_FREQ_720MHZ     720000000   /* 1440 Mbps/lane, reg=0x03 */
#define IMX662_LINK_FREQ_891MHZ     891000000   /* 1782 Mbps/lane, reg=0x02 */

/* DATARATE_SEL register values - maps to MIPI data rate per lane
 * Link frequency = data_rate / 2 (MIPI uses DDR)
 * Based on Sony IMX662 datasheet and working Linux driver
 */
#define IMX662_DATARATE_2376MBPS    0x00  /* 2376 Mbps/lane, link_freq=1188 MHz */
#define IMX662_DATARATE_2079MBPS    0x01  /* 2079 Mbps/lane, link_freq=1039.5 MHz */
#define IMX662_DATARATE_1782MBPS    0x02  /* 1782 Mbps/lane, link_freq=891 MHz */
#define IMX662_DATARATE_1440MBPS    0x03  /* 1440 Mbps/lane, link_freq=720 MHz */
#define IMX662_DATARATE_1188MBPS    0x04  /* 1188 Mbps/lane, link_freq=594 MHz - USE THIS FOR 2-LANE 30fps */
#define IMX662_DATARATE_891MBPS     0x05  /* 891 Mbps/lane, link_freq=445.5 MHz */
#define IMX662_DATARATE_720MBPS     0x06  /* 720 Mbps/lane, link_freq=360 MHz */
#define IMX662_DATARATE_594MBPS     0x07  /* 594 Mbps/lane, link_freq=297 MHz */

/* INCK_SEL register values
 * Supported frequencies per Sony datasheet: 24 / 27 / 37.125 / 72 / 74.25 MHz
 */
#define IMX662_INCK_74_25MHZ        0x00  /* 74.25 MHz - try this first! */
#define IMX662_INCK_37_125MHZ       0x01  /* 37.125 MHz */
#define IMX662_INCK_72MHZ           0x02  /* 72 MHz (guess) */
#define IMX662_INCK_27MHZ           0x03  /* 27 MHz (guess) */
#define IMX662_INCK_24MHZ           0x04  /* 24 MHz */

/* Pixel clock */
#define IMX662_PIXEL_RATE           74250000ULL

/* Native dimensions */
#define IMX662_NATIVE_WIDTH         1936U
#define IMX662_NATIVE_HEIGHT        1100U
#define IMX662_PIXEL_ARRAY_WIDTH    1920U
#define IMX662_PIXEL_ARRAY_HEIGHT   1080U

/*
 * Common initialization sequence for IMX662
 * Based on Linux driver mode_common_regs
 */
static const imx662_reginfo_t imx662_common_init_regs[] = {
    /* Standby and control */
    {0x3000, 0x01},  /* STANDBY */
    {0x3001, 0x00},  /* REGHOLD */
    {0x3002, 0x00},  /* XMASTER - will be set to 1 when streaming starts */

    /* Clock configuration - 74.25MHz input
     * Your module has a 74.25MHz oscillator (marked "sjk 74.250")
     * Using 720Mbps data rate - this works with ESP32-P4 CSI receiver
     */
    {0x3014, IMX662_INCK_74_25MHZ},     /* INCK_SEL = 74.25MHz */
    {0x3015, IMX662_DATARATE_720MBPS},  /* DATARATE_SEL = 720Mbps - works with ESP32 */

    /* Window mode */
    {0x3018, 0x00},  /* WINMODE = All-pixel */
    {0x301A, 0x00},  /* WDMODE = Normal mode */
    {0x301B, 0x00},  /* ADDMODE = Non-binning */
    {0x301C, 0x00},  /* THIN_V_EN */
    {0x301E, 0x01},  /* VCMODE */

    /* Flip controls */
    {0x3020, 0x00},  /* HREVERSE */
    {0x3021, 0x00},  /* VREVERSE */

    /* Bit depth - 10bit (changed from 12bit for testing) */
    {0x3022, 0x00},  /* ADBIT = 10bit (was 0x01 for 12bit) */
    {0x3023, 0x00},  /* MDBIT = 10bit (was 0x01 for 12bit) */

    /* Gain selection */
    {0x3030, 0x00},  /* FDG_SEL0 */
    {0x3031, 0x00},  /* FDG_SEL1 */
    {0x3032, 0x00},  /* FDG_SEL2 */

    /* Pixel window - horizontal (full sensor width like RPi driver) */
    {0x303C, 0x00},  /* PIX_HST = 0 (no offset - RPi driver uses this) */
    {0x303D, 0x00},
    {0x303E, 0x90},  /* PIX_HWIDTH = 1936 (0x0790) - full sensor width */
    {0x303F, 0x07},

    /* Lane mode - 2 lanes */
    {0x3040, 0x01},  /* LANEMODE = 2 lanes */

    /* Pixel window - vertical (full sensor height like RPi driver) */
    {0x3044, 0x00},  /* PIX_VST = 0 (no offset - RPi driver uses this) */
    {0x3045, 0x00},
    {0x3046, 0x4C},  /* PIX_VWIDTH = 1100 (0x044C) - full sensor height */
    {0x3047, 0x04},

    /* Exposure (SHR0) - main exposure control
     * SHR0 must be between 4 and (VMAX-1) = 4 to 1249
     * Integration time = (VMAX - SHR0) lines
     * Lower SHR0 = longer exposure, Higher SHR0 = shorter exposure
     * SHR0 = 500 gives medium exposure (~750 lines = 60% of max)
     */
    {0x3050, 0xF4},  /* SHR0_L = 500 (0x01F4) - medium exposure */
    {0x3051, 0x01},  /* SHR0_M */
    {0x3052, 0x00},  /* SHR0_H */

    /* SHR1/SHR2 for HDR modes (not used in normal mode) */
    {0x3054, 0x0E},  /* SHR1 */
    {0x3055, 0x00},
    {0x3056, 0x00},
    {0x3058, 0x8A},  /* SHR2 */
    {0x3059, 0x01},
    {0x305A, 0x00},

    /* RHS (readout timing) */
    {0x3060, 0x16},  /* RHS1 */
    {0x3061, 0x01},
    {0x3062, 0x00},
    {0x3064, 0xC4},  /* RHS2 */
    {0x3065, 0x0C},
    {0x3066, 0x00},

    /* HDR and gain */
    {0x3069, 0x00},  /* CHDR_GAIN_EN */
    {0x306B, 0x00},  /* Sensor register */
    /* GAIN: 0-240 (0.3dB per step). 30=9dB, 60=18dB, 100=30dB
     * For bright indoor: 20-40, Normal indoor: 40-80, Low light: 80-150
     */
    {0x3070, 0x1E},  /* GAIN = 30 (9dB) - for normal indoor lighting */
    {0x3071, 0x00},
    {0x3072, 0x00},  /* GAIN_1 */
    {0x3073, 0x00},
    {0x3074, 0x00},  /* GAIN_2 */
    {0x3075, 0x00},
    {0x3081, 0x00},  /* EXP_GAIN */

    /* Clear HDR digital gain */
    {0x308C, 0x00},  /* CHDR_DGAIN0_HG */
    {0x308D, 0x01},
    {0x3094, 0x00},  /* CHDR_AGAIN0_LG */
    {0x3095, 0x00},
    {0x3096, 0x00},  /* CHDR_AGAIN1_LG */
    {0x3097, 0x00},
    {0x309C, 0x00},  /* CHDR_AGAIN0_HG */
    {0x309D, 0x00},

    /* XVS output */
    {0x30A4, 0xAA},  /* XVSOUTSEL */
    {0x30A6, 0x0F},  /* XVS_DRV = HiZ */
    {0x30CC, 0x00},  /* XVSLNG */
    {0x30CD, 0x00},  /* XHSLNG */

    /* Black level - Sony recommends 0x032 (50 LSB for 10bit, 200 LSB for 12bit) */
    {0x30DC, 0x32},  /* BLKLEVEL[7:0] = 0x32 */
    {0x30DD, 0x00},  /* BLKLEVEL[11:8] = 0x00 → BLKLEVEL = 0x032 = 50 */

    /* Gain PGC */
    {0x3400, 0x01},  /* GAIN_PGC_FIDMD */

    /* Sensor specific registers */
    {0x3444, 0xAC},
    {0x3460, 0x21},
    {0x3492, 0x08},

    /* Reserved registers from Linux driver - CRITICAL for proper operation */
    {0x3B00, 0x39},
    {0x3B23, 0x2D},
    {0x3B45, 0x04},
    {0x3C0A, 0x1F},
    {0x3C0B, 0x1E},
    {0x3C38, 0x21},
    {0x3C40, 0x06},  /* Normal mode */
    {0x3C44, 0x00},
    {0x3CB6, 0xD8},
    {0x3CC4, 0xDA},
    {0x3E24, 0x79},
    {0x3E2C, 0x15},
    {0x3EDC, 0x2D},

    /* More reserved registers from Linux driver */
    {0x4498, 0x05},
    {0x449C, 0x19},
    {0x449D, 0x00},
    {0x449E, 0x32},
    {0x449F, 0x01},
    {0x44A0, 0x92},
    {0x44A2, 0x91},
    {0x44A4, 0x8C},
    {0x44A6, 0x87},
    {0x44A8, 0x82},
    {0x44AA, 0x78},
    {0x44AC, 0x6E},
    {0x44AE, 0x69},
    {0x44B0, 0x92},
    {0x44B2, 0x91},
    {0x44B4, 0x8C},
    {0x44B6, 0x87},
    {0x44B8, 0x82},
    {0x44BA, 0x78},
    {0x44BC, 0x6E},
    {0x44BE, 0x69},
    {0x44C1, 0x01},
    {0x44C2, 0x7F},
    {0x44C3, 0x01},
    {0x44C4, 0x7A},
    {0x44C5, 0x01},
    {0x44C6, 0x7A},
    {0x44C7, 0x01},
    {0x44C8, 0x70},
    {0x44C9, 0x01},
    {0x44CA, 0x6B},
    {0x44CB, 0x01},
    {0x44CC, 0x6B},
    {0x44CD, 0x01},
    {0x44CE, 0x5C},
    {0x44CF, 0x01},
    {0x44D0, 0x7F},
    {0x44D1, 0x01},
    {0x44D2, 0x7F},
    {0x44D3, 0x01},
    {0x44D4, 0x7A},
    {0x44D5, 0x01},
    {0x44D6, 0x7A},
    {0x44D7, 0x01},
    {0x44D8, 0x70},
    {0x44D9, 0x01},
    {0x44DA, 0x6B},
    {0x44DB, 0x01},
    {0x44DC, 0x6B},
    {0x44DD, 0x01},
    {0x44DE, 0x5C},
    {0x44DF, 0x01},
    {0x4534, 0x1C},
    {0x4535, 0x03},
    {0x4538, 0x1C},
    {0x4539, 0x1C},
    {0x453A, 0x1C},
    {0x453B, 0x1C},
    {0x453C, 0x1C},
    {0x453D, 0x1C},
    {0x453E, 0x1C},
    {0x453F, 0x1C},
    {0x4540, 0x1C},
    {0x4541, 0x03},
    {0x4542, 0x03},
    {0x4543, 0x03},
    {0x4544, 0x03},
    {0x4545, 0x03},
    {0x4546, 0x03},
    {0x4547, 0x03},
    {0x4548, 0x03},
    {0x4549, 0x03},

    /* End of init */
    {IMX662_REG_END, 0x00},
};

/*
 * 1920x1080 @ 30fps RAW12 - 2 lanes MIPI
 * Configuration that works with ESP32-P4:
 * - DATARATE_SEL = 0x06 (720 Mbps/lane)
 * - HMAX = 1980 (1H period), VMAX = 1250
 */
static const imx662_reginfo_t imx662_1920x1080_30fps_2lane_raw12[] = {
    /* Mode specific settings */
    {0x3015, IMX662_DATARATE_720MBPS},  /* 720 Mbps/lane - works with ESP32 */
    {0x301A, 0x00},  /* WDMODE = Normal (HDR mode select) */
    {0x301B, 0x00},  /* ADDMODE = Non-binning (Normal/binning) */
    {0x3022, 0x00},  /* ADBIT = 10bit (testing RAW10) */
    {0x3023, 0x00},  /* MDBIT = 10bit (testing RAW10) */

    /* HMAX = 1980 (0x07BC) - from Sony manual: 1H period for 30fps */
    {0x302C, 0xBC},  /* HMAX_L = 1980 & 0xFF */
    {0x302D, 0x07},  /* HMAX_H = 1980 >> 8 */

    /* VMAX = 1250 (0x04E2) */
    {0x3028, 0xE2},  /* VMAX_L */
    {0x3029, 0x04},  /* VMAX_M */
    {0x302A, 0x00},  /* VMAX_H */

    /* Lane mode = 2 */
    {0x3040, 0x01},

    /* AD conversion for 10-bit mode (from binning mode - 10bit compatible)
     * These values MUST match ADBIT setting!
     * For 12-bit: 0x3A50=0xFF, 0x3A51=0x03, 0x3A52=0x00
     * For 10-bit: 0x3A50=0x62, 0x3A51=0x01, 0x3A52=0x19
     */
    {0x3A50, 0x62},  /* 10-bit AD conversion */
    {0x3A51, 0x01},  /* 10-bit AD conversion */
    {0x3A52, 0x19},  /* 10-bit AD conversion */

    {IMX662_REG_END, 0x00},
};

/*
 * 1920x1080 @ 60fps RAW12 - 2 lanes MIPI
 * Same data rate but faster VMAX
 */
static const imx662_reginfo_t imx662_1920x1080_60fps_2lane_raw12[] = {
    /* Mode specific settings */
    {0x3015, IMX662_DATARATE_1782MBPS},  /* 1782 Mbps/lane */
    {0x301A, 0x00},  /* WDMODE = Normal */
    {0x301B, 0x00},  /* ADDMODE = Non-binning */
    {0x3022, 0x01},  /* ADBIT = 12bit */
    {0x3023, 0x01},  /* MDBIT = 12bit */

    /* HMAX = 990 */
    {0x302C, 0xDE},  /* HMAX_L */
    {0x302D, 0x03},  /* HMAX_H */

    /* VMAX = 625 for 60fps (half of 1250) */
    {0x3028, 0x71},  /* VMAX_L = 625 & 0xFF */
    {0x3029, 0x02},  /* VMAX_M = 625 >> 8 */
    {0x302A, 0x00},  /* VMAX_H */

    /* Lane mode = 2 */
    {0x3040, 0x01},

    /* AD conversion */
    {0x3A50, 0x62},
    {0x3A51, 0x01},
    {0x3A52, 0x19},

    {IMX662_REG_END, 0x00},
};

/*
 * 960x540 @ 90fps RAW12 - 2x2 Binning
 * From Linux driver mode_540_regs
 */
static const imx662_reginfo_t imx662_960x540_90fps_2lane_raw12[] = {
    /* Mode specific settings */
    {0x3015, IMX662_DATARATE_1782MBPS},  /* 1782 Mbps/lane */
    {0x301A, 0x00},  /* WDMODE = Normal */
    {0x301B, 0x01},  /* ADDMODE = 2x2 Binning */
    {0x3022, 0x00},  /* ADBIT = 10bit (for binning) */
    {0x3023, 0x01},  /* MDBIT = 12bit */

    /* HMAX = 990, VMAX = 1250 (same as full res) */
    {0x302C, 0xDE},  /* HMAX_L */
    {0x302D, 0x03},  /* HMAX_H */
    {0x3028, 0xE2},  /* VMAX_L */
    {0x3029, 0x04},  /* VMAX_M */
    {0x302A, 0x00},  /* VMAX_H */

    /* Lane mode = 2 */
    {0x3040, 0x01},

    /* AD conversion (from Linux driver mode_540_regs) */
    {0x3A50, 0x62},
    {0x3A51, 0x01},
    {0x3A52, 0x19},

    {IMX662_REG_END, 0x00},
};

#ifdef __cplusplus
}
#endif
