#!/usr/bin/env python3
"""
Image Stream Viewer for IMX662 on ESP32-P4
Receives video stream from ESP32-P4 ISP and displays in real-time

Usage:
    python raw_stream_viewer.py --host 192.168.1.100 --port 80

Or for testing with saved files:
    python raw_stream_viewer.py --test-file frame.raw

Supports:
    - RGB888: 3 bytes per pixel (ISP processed - full color correction)
    - RGB565: 2 bytes per pixel (ISP processed)
    - RAW8: 1 byte per pixel, Bayer RGGB pattern
    - RAW10 packed: 5 bytes per 4 pixels (legacy)
"""

import numpy as np
import cv2
import argparse
import requests
import time
import threading
from queue import Queue

# IMX662 sensor configuration
DEFAULT_WIDTH = 1936
DEFAULT_HEIGHT = 1100

class ImageDecoder:
    """Decode image data from IMX662 (supports RGB888 and RAW formats)"""

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.width = width
        self.height = height

        # RGB888: 3 bytes per pixel (ISP processed)
        self.frame_size_rgb888 = width * height * 3

        # RGB565: 2 bytes per pixel
        self.frame_size_rgb565 = width * height * 2

        # RAW8: 1 byte per pixel
        self.frame_size_raw8 = width * height

        # RAW10 packed: 5 bytes per 4 pixels (legacy support)
        self.row_bytes = (width * 5) // 4
        self.frame_size_packed = self.row_bytes * height

        # RAW10 in 16-bit container
        self.frame_size_unpacked = width * height * 2

        print(f"Decoder initialized: {width}x{height}")
        print(f"  RGB888 frame size: {self.frame_size_rgb888} bytes")
        print(f"  RGB565 frame size: {self.frame_size_rgb565} bytes")
        print(f"  RAW8 frame size: {self.frame_size_raw8} bytes")

        # CLAHE for auto-enhancement
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def decode_rgb888(self, data):
        """Decode RGB888 data (3 bytes per pixel, ISP processed)

        This is the output from the ISP with full processing:
        - Demosaicing applied
        - Color correction applied
        - Gamma correction applied

        Note: ESP32 ISP outputs BGR order (same as OpenCV), no conversion needed
        """
        expected_size = self.frame_size_rgb888
        if len(data) < expected_size:
            print(f"Warning: Expected {expected_size} bytes for RGB888, got {len(data)}")
            return None

        # Convert to numpy array and reshape to (height, width, 3)
        bgr_bytes = np.frombuffer(data[:expected_size], dtype=np.uint8)
        bgr_img = bgr_bytes.reshape((self.height, self.width, 3))

        # ESP32 ISP outputs BGR - same as OpenCV, no conversion needed
        return bgr_img

    def decode_rgb565(self, data):
        """Decode RGB565 data (2 bytes per pixel)"""
        expected_size = self.frame_size_rgb565
        if len(data) < expected_size:
            print(f"Warning: Expected {expected_size} bytes for RGB565, got {len(data)}")
            return None

        # Convert to numpy array as uint16
        rgb565 = np.frombuffer(data[:expected_size], dtype=np.uint16)
        rgb565 = rgb565.reshape((self.height, self.width))

        # Extract R, G, B components
        r = ((rgb565 >> 11) & 0x1F) << 3  # 5 bits -> 8 bits
        g = ((rgb565 >> 5) & 0x3F) << 2   # 6 bits -> 8 bits
        b = (rgb565 & 0x1F) << 3          # 5 bits -> 8 bits

        # Stack into BGR image for OpenCV
        bgr_img = np.stack([b, g, r], axis=-1).astype(np.uint8)

        return bgr_img

    def decode_raw8(self, raw_data):
        """Decode RAW8 Bayer data (1 byte per pixel)

        This is the simplest format - each byte is one pixel value (0-255)
        in Bayer RGGB pattern.
        """
        expected_size = self.frame_size_raw8
        if len(raw_data) < expected_size:
            print(f"Warning: Expected {expected_size} bytes for RAW8, got {len(raw_data)}")
            return None

        # Convert to numpy array and reshape to image
        raw_bytes = np.frombuffer(raw_data[:expected_size], dtype=np.uint8)
        bayer_img = raw_bytes.reshape((self.height, self.width))

        # Convert to 16-bit for consistent processing (shift left 2 bits)
        # This makes it compatible with the 10-bit processing pipeline
        bayer_16bit = bayer_img.astype(np.uint16) << 2

        return bayer_16bit

    def decode_raw10_packed(self, raw_data):
        """Decode MIPI RAW10 packed data (5 bytes per 4 pixels)

        Format:
            Byte 0: P0[9:2] - upper 8 bits of pixel 0
            Byte 1: P1[9:2] - upper 8 bits of pixel 1
            Byte 2: P2[9:2] - upper 8 bits of pixel 2
            Byte 3: P3[9:2] - upper 8 bits of pixel 3
            Byte 4: P3[1:0]P2[1:0]P1[1:0]P0[1:0] - lower 2 bits packed
        """
        expected_size = self.frame_size_packed
        if len(raw_data) < expected_size:
            print(f"Warning: Expected {expected_size} bytes, got {len(raw_data)}")
            return None

        # Only use expected bytes
        raw_data = raw_data[:expected_size]

        # Convert to numpy array
        raw_bytes = np.frombuffer(raw_data, dtype=np.uint8)

        # Reshape to groups of 5 bytes (4 pixels per group)
        # Total groups = total_pixels / 4
        total_pixels = self.width * self.height
        num_groups = total_pixels // 4

        # Reshape: each row has (width/4) groups of 5 bytes
        groups_per_row = self.width // 4
        raw_reshaped = raw_bytes.reshape((self.height, groups_per_row, 5))

        # Extract the 4 pixels from each 5-byte group
        # Upper 8 bits from bytes 0-3, lower 2 bits from byte 4
        p0_high = raw_reshaped[:, :, 0].astype(np.uint16)
        p1_high = raw_reshaped[:, :, 1].astype(np.uint16)
        p2_high = raw_reshaped[:, :, 2].astype(np.uint16)
        p3_high = raw_reshaped[:, :, 3].astype(np.uint16)
        low_bits = raw_reshaped[:, :, 4].astype(np.uint16)

        # Extract lower 2 bits for each pixel from byte 4
        p0_low = (low_bits >> 0) & 0x03
        p1_low = (low_bits >> 2) & 0x03
        p2_low = (low_bits >> 4) & 0x03
        p3_low = (low_bits >> 6) & 0x03

        # Combine: pixel = (high << 2) | low
        pixel0 = (p0_high << 2) | p0_low
        pixel1 = (p1_high << 2) | p1_low
        pixel2 = (p2_high << 2) | p2_low
        pixel3 = (p3_high << 2) | p3_low

        # Interleave pixels back into image
        # Output shape: (height, width)
        bayer_img = np.zeros((self.height, self.width), dtype=np.uint16)
        bayer_img[:, 0::4] = pixel0
        bayer_img[:, 1::4] = pixel1
        bayer_img[:, 2::4] = pixel2
        bayer_img[:, 3::4] = pixel3

        return bayer_img

    def decode_raw16(self, raw_data):
        """Decode 16-bit RAW data (10-bit in 16-bit container)"""
        expected_size = self.frame_size_unpacked
        if len(raw_data) != expected_size:
            print(f"Warning: Expected {expected_size} bytes for RAW16, got {len(raw_data)}")
            return None

        # Read as 16-bit little-endian
        raw_16bit = np.frombuffer(raw_data, dtype=np.uint16)

        # Extract 10-bit data (mask lower 10 bits)
        raw_10bit = (raw_16bit & 0x3FF).astype(np.uint16)

        # Reshape to image
        bayer_img = raw_10bit.reshape((self.height, self.width))

        return bayer_img

    def auto_decode(self, data):
        """Auto-detect format based on data size and decode (cached after first detection)

        Returns:
            For RGB888/RGB565: BGR image ready for display
            For RAW formats: Bayer image (needs further processing)
        """
        data_len = len(data)

        # Use cached format if available
        if hasattr(self, '_cached_format'):
            if self._cached_format == 'rgb888':
                return self.decode_rgb888(data), 'rgb'
            elif self._cached_format == 'rgb565':
                return self.decode_rgb565(data), 'rgb'
            elif self._cached_format == 'raw8':
                return self.decode_raw8(data), 'raw'
            elif self._cached_format == 'packed':
                return self.decode_raw10_packed(data), 'raw'
            elif self._cached_format == 'raw16':
                return self.decode_raw16(data), 'raw'

        # Check which format matches (first frame only)
        # Check RGB888 first (most common now with ISP)
        if abs(data_len - self.frame_size_rgb888) < 1000:
            print(f"Detected RGB888 format ({data_len} bytes) - ISP processed!")
            self._cached_format = 'rgb888'
            return self.decode_rgb888(data), 'rgb'
        elif abs(data_len - self.frame_size_rgb565) < 1000:
            print(f"Detected RGB565 format ({data_len} bytes)")
            self._cached_format = 'rgb565'
            return self.decode_rgb565(data), 'rgb'
        elif abs(data_len - self.frame_size_raw8) < 1000:
            print(f"Detected RAW8 format ({data_len} bytes)")
            self._cached_format = 'raw8'
            return self.decode_raw8(data), 'raw'
        elif abs(data_len - self.frame_size_packed) < 1000:
            print(f"Detected RAW10 packed format ({data_len} bytes)")
            self._cached_format = 'packed'
            return self.decode_raw10_packed(data), 'raw'
        elif abs(data_len - self.frame_size_unpacked) < 1000:
            print(f"Detected RAW10 in 16-bit container ({data_len} bytes)")
            self._cached_format = 'raw16'
            return self.decode_raw16(data), 'raw'
        else:
            print(f"Unknown format: {data_len} bytes")
            print(f"  Expected RGB888: {self.frame_size_rgb888}")
            print(f"  Expected RGB565: {self.frame_size_rgb565}")
            print(f"  Expected RAW8: {self.frame_size_raw8}")
            # Try RGB888 as fallback (most likely with ISP)
            self._cached_format = 'rgb888'
            return self.decode_rgb888(data), 'rgb'

    def debayer_rggb(self, bayer_img, pattern='RG'):
        """Apply debayering with selectable Bayer pattern

        Patterns: 'RG' (RGGB), 'BG' (BGGR), 'GR' (GRBG), 'GB' (GBRG)
        """
        # Convert 10-bit to 8-bit for OpenCV
        bayer_8bit = (bayer_img >> 2).astype(np.uint8)

        # Select debayering pattern
        patterns = {
            'RG': cv2.COLOR_BAYER_RG2BGR,  # RGGB
            'BG': cv2.COLOR_BAYER_BG2BGR,  # BGGR
            'GR': cv2.COLOR_BAYER_GR2BGR,  # GRBG
            'GB': cv2.COLOR_BAYER_GB2BGR,  # GBRG
        }

        cv_pattern = patterns.get(pattern, cv2.COLOR_BAYER_RG2BGR)
        rgb_img = cv2.cvtColor(bayer_8bit, cv_pattern)

        return rgb_img

    def enhance_image(self, img):
        """Apply CLAHE enhancement for better visibility"""
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self.clahe.apply(l)
        enhanced = cv2.merge([l, a, b])
        enhanced = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
        return enhanced

    def apply_color_correction(self, img, r_gain=1.0, g_gain=1.0, b_gain=1.0,
                                gamma=1.0, saturation=1.0, brightness=0):
        """Apply color correction to BGR image

        Args:
            img: BGR image (uint8)
            r_gain: Red channel multiplier (0.5-2.0)
            g_gain: Green channel multiplier (0.5-2.0)
            b_gain: Blue channel multiplier (0.5-2.0)
            gamma: Gamma correction (0.5-2.0, 1.0=no change)
            saturation: Color saturation (0.0-2.0, 1.0=no change)
            brightness: Brightness adjustment (-50 to +50)
        """
        # Convert to float for processing
        img_float = img.astype(np.float32)

        # Apply white balance (BGR order in OpenCV)
        img_float[:, :, 0] *= b_gain  # Blue
        img_float[:, :, 1] *= g_gain  # Green
        img_float[:, :, 2] *= r_gain  # Red

        # Clip and convert back
        img_float = np.clip(img_float, 0, 255)

        # Apply gamma correction
        if gamma != 1.0:
            img_float = 255.0 * np.power(img_float / 255.0, 1.0 / gamma)

        # Apply brightness
        if brightness != 0:
            img_float = img_float + brightness
            img_float = np.clip(img_float, 0, 255)

        img_corrected = img_float.astype(np.uint8)

        # Apply saturation in HSV space
        if saturation != 1.0:
            hsv = cv2.cvtColor(img_corrected, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] *= saturation  # Saturation channel
            hsv[:, :, 1] = np.clip(hsv[:, :, 1], 0, 255)
            img_corrected = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

        return img_corrected

    def process_frame(self, raw_data, enhance=True, pattern='RG', rotate=0,
                      color_correction=None):
        """Full processing pipeline: RAW -> RGB or pass-through for ISP-processed RGB

        Args:
            raw_data: Raw bytes from sensor (RAW or RGB888 from ISP)
            enhance: Apply CLAHE enhancement (only for RAW formats)
            pattern: Bayer pattern ('RG', 'BG', 'GR', 'GB') - only for RAW
            rotate: Rotation (0, 90, 180, 270)
            color_correction: Dict with r_gain, g_gain, b_gain, gamma, saturation, brightness
                              (only applied to RAW formats, RGB888 already has ISP corrections)
        """
        # Auto-detect and decode format
        result = self.auto_decode(raw_data)
        if result is None or result[0] is None:
            return None

        image, format_type = result

        if format_type == 'rgb':
            # RGB888/RGB565 from ISP - demosaic done, but CCM/AWB may not be configured
            rgb = image

            # Debug: print image info for first frame only
            if not hasattr(self, '_debug_printed'):
                self._debug_printed = True
                print(f"RGB image from ISP: {rgb.shape}, dtype={rgb.dtype}")
                print(f"  Stats: min={rgb.min()}, max={rgb.max()}, mean={rgb.mean():.1f}")
                print("  NOTE: ISP CCM/AWB not configured - applying Python color correction")

            # Apply color correction (ISP CCM not configured, so we do it here)
            if color_correction:
                rgb = self.apply_color_correction(
                    rgb,
                    r_gain=color_correction.get('r_gain', 1.0),
                    g_gain=color_correction.get('g_gain', 1.0),
                    b_gain=color_correction.get('b_gain', 1.0),
                    gamma=color_correction.get('gamma', 1.0),
                    saturation=color_correction.get('saturation', 1.0),
                    brightness=color_correction.get('brightness', 0)
                )

        else:
            # RAW format - needs debayering and color correction
            bayer = image

            # Debug: print min/max pixel values for first frame only
            if not hasattr(self, '_debug_printed'):
                self._debug_printed = True
                bayer_min, bayer_max, bayer_mean = bayer.min(), bayer.max(), bayer.mean()
                print(f"Bayer stats: min={bayer_min}, max={bayer_max}, mean={bayer_mean:.1f}")
                if bayer_max > 900:
                    print("WARNING: Image may be overexposed (max > 900 on 10-bit scale)")

            rgb = self.debayer_rggb(bayer, pattern)

            # Apply color correction before CLAHE (if specified) - only for RAW
            if color_correction:
                rgb = self.apply_color_correction(
                    rgb,
                    r_gain=color_correction.get('r_gain', 1.0),
                    g_gain=color_correction.get('g_gain', 1.0),
                    b_gain=color_correction.get('b_gain', 1.0),
                    gamma=color_correction.get('gamma', 1.0),
                    saturation=color_correction.get('saturation', 1.0),
                    brightness=color_correction.get('brightness', 0)
                )

            if enhance:
                rgb = self.enhance_image(rgb)

        # Apply rotation (for all formats)
        if rotate == 90:
            rgb = cv2.rotate(rgb, cv2.ROTATE_90_CLOCKWISE)
        elif rotate == 180:
            rgb = cv2.rotate(rgb, cv2.ROTATE_180)
        elif rotate == 270:
            rgb = cv2.rotate(rgb, cv2.ROTATE_90_COUNTERCLOCKWISE)

        return rgb


class RawStreamViewer:
    """Real-time stream viewer for IMX662 (supports RGB888/RGB565 from ISP and RAW)"""

    def __init__(self, host, port, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.host = host
        self.port = port
        self.decoder = ImageDecoder(width, height)
        self.frame_queue = Queue(maxsize=2)  # Small queue for low latency
        self.running = False
        self.fps = 0
        self.frame_count = 0
        self.last_frame = None  # Keep last frame to avoid "waiting" screen
        self.dropped_frames = 0  # Count dropped frames for stats

    def receiver_thread(self):
        """Background thread using continuous streaming"""
        last_time = time.time()
        frame_count = 0
        debug_count = 0

        url = f"http://{self.host}:{self.port}/stream"
        boundary = b'--raw_frame_boundary'
        # Use RGB888 size as primary (ISP output), can auto-detect others
        min_size = self.decoder.frame_size_rgb888

        print(f"Expected RGB888 frame size: {min_size} bytes (ISP processed)")
        print(f"  (RGB565: {self.decoder.frame_size_rgb565} bytes)")
        print(f"  (RAW8: {self.decoder.frame_size_raw8} bytes)")

        # Max buffer size: 2 frames worth to prevent accumulation
        max_buffer_size = self.decoder.frame_size_rgb888 * 2

        while self.running:
            try:
                print(f"Connecting to stream: {url}")
                print(f"LOW LATENCY MODE: Dropping old frames, max buffer {max_buffer_size} bytes")
                response = requests.get(url, stream=True, timeout=10)
                buffer = b''
                total_received = 0

                for chunk in response.iter_content(chunk_size=131072):  # Larger chunks for speed
                    if not self.running:
                        break

                    buffer += chunk
                    total_received += len(chunk)

                    # LOW LATENCY: If buffer gets too large, keep only the end
                    if len(buffer) > max_buffer_size:
                        # Find last boundary and keep from there
                        last_boundary = buffer.rfind(boundary)
                        if last_boundary > 0:
                            buffer = buffer[last_boundary:]
                            print(f"Buffer overflow - trimmed to {len(buffer)} bytes")

                    # Look for frame boundaries
                    while boundary in buffer:
                        parts = buffer.split(boundary, 1)
                        frame_data = parts[0]
                        buffer = parts[1] if len(parts) > 1 else b''

                        # Extract binary data (skip headers)
                        if b'\r\n\r\n' in frame_data:
                            _, data = frame_data.split(b'\r\n\r\n', 1)
                            # Don't strip - raw data might end with \r\n

                            data_len = len(data)
                            # Accept any valid frame size (RGB888, RGB565, RAW8)
                            valid_sizes = [
                                self.decoder.frame_size_rgb888,
                                self.decoder.frame_size_rgb565,
                                self.decoder.frame_size_raw8,
                            ]

                            # Find matching size (allow small tolerance)
                            matched_size = None
                            for vs in valid_sizes:
                                if abs(data_len - vs) < 1000:
                                    matched_size = vs
                                    break

                            if frame_count == 0:  # Only print for first frame
                                print(f"Frame data size: {data_len} bytes")
                                if matched_size:
                                    print(f"  Matched format size: {matched_size} bytes")

                            if matched_size and data_len >= matched_size:
                                # LOW LATENCY: Clear ALL old frames, keep only newest
                                dropped = 0
                                while not self.frame_queue.empty():
                                    try:
                                        self.frame_queue.get_nowait()
                                        dropped += 1
                                    except:
                                        break
                                self.dropped_frames += dropped

                                # Put the newest frame
                                self.frame_queue.put(data[:matched_size])

                                frame_count += 1
                                current_time = time.time()
                                if current_time - last_time >= 1.0:
                                    self.fps = frame_count / (current_time - last_time)
                                    print(f"FPS: {self.fps:.1f} (dropped {self.dropped_frames} for low latency)")
                                    frame_count = 0
                                    self.dropped_frames = 0
                                    last_time = current_time
                            elif data_len > 1000:  # Ignore tiny fragments
                                print(f"Unknown frame size: {data_len} bytes")

            except Exception as e:
                print(f"Stream error: {e}, reconnecting...")
                time.sleep(0.5)

    def run(self, enhance=True, save_frames=False):
        """Main display loop with interactive color correction controls"""
        self.running = True

        # Settings
        patterns = ['RG', 'BG', 'GR', 'GB']
        pattern_idx = 0
        rotations = [0, 90, 180, 270]
        rotation_idx = 2  # Start with 180 (image is upside down)

        # Color correction defaults (trackbar values: 0-200 mapped to 0.0-2.0)
        # For gains: value/100 = multiplier (100 = 1.0x)
        # For gamma: value/100 = gamma (100 = 1.0)
        # For saturation: value/100 = saturation (100 = 1.0)
        # For brightness: value-100 = offset (-100 to +100)
        color_params = {
            'r_gain': 100,      # Red gain (100 = 1.0x)
            'g_gain': 100,      # Green gain (100 = 1.0x) - reduce to fix green tint
            'b_gain': 100,      # Blue gain (100 = 1.0x)
            'gamma': 100,       # Gamma (100 = 1.0)
            'saturation': 100,  # Saturation (100 = 1.0)
            'brightness': 100,  # Brightness offset (100 = 0)
        }

        # Trackbar callback (does nothing, we read values in loop)
        def nothing(x):
            pass

        # Start receiver thread
        receiver = threading.Thread(target=self.receiver_thread, daemon=True)
        receiver.start()

        print(f"Connecting to {self.host}:{self.port}...")
        print("\n" + "="*60)
        print("CONTROLS:")
        print("="*60)
        print("  Keyboard:")
        print("    q - Quit")
        print("    s - Save frame (JPG + RAW)")
        print("    e - Toggle CLAHE enhancement")
        print("    p - Cycle Bayer pattern (RG/BG/GR/GB)")
        print("    r - Rotate image (0/90/180/270)")
        print("    c - Toggle color correction panel")
        print("    0 - Reset color correction to defaults")
        print("    1 - Apply IMX662 preset (fix green tint)")
        print("    2 - Apply neutral daylight preset")
        print("")
        print("  Color Correction Sliders:")
        print("    R Gain  - Red channel (reduce green tint: increase R)")
        print("    G Gain  - Green channel (reduce green tint: decrease G)")
        print("    B Gain  - Blue channel")
        print("    Gamma   - Gamma correction (>1.0 = brighter shadows)")
        print("    Satur.  - Color saturation")
        print("    Bright. - Brightness offset")
        print("="*60 + "\n")

        # Create windows
        cv2.namedWindow('IMX662 RAW Stream', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('IMX662 RAW Stream', 960, 550)

        # Create color correction control window
        show_controls = True
        cv2.namedWindow('Color Correction', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Color Correction', 400, 300)

        # Create trackbars (0-200 range for 0.0-2.0 multipliers)
        cv2.createTrackbar('R Gain', 'Color Correction', color_params['r_gain'], 200, nothing)
        cv2.createTrackbar('G Gain', 'Color Correction', color_params['g_gain'], 200, nothing)
        cv2.createTrackbar('B Gain', 'Color Correction', color_params['b_gain'], 200, nothing)
        cv2.createTrackbar('Gamma', 'Color Correction', color_params['gamma'], 200, nothing)
        cv2.createTrackbar('Saturation', 'Color Correction', color_params['saturation'], 200, nothing)
        cv2.createTrackbar('Brightness', 'Color Correction', color_params['brightness'], 200, nothing)

        # Preset: fix green tint for IMX662 (sensor has strong green response)
        # These values compensate for missing ISP CCM configuration
        cv2.setTrackbarPos('R Gain', 'Color Correction', 140)  # 1.4x - boost red significantly
        cv2.setTrackbarPos('G Gain', 'Color Correction', 65)   # 0.65x - reduce green strongly
        cv2.setTrackbarPos('B Gain', 'Color Correction', 125)  # 1.25x - boost blue
        cv2.setTrackbarPos('Gamma', 'Color Correction', 110)   # 1.1 - slight gamma boost
        cv2.setTrackbarPos('Saturation', 'Color Correction', 110)  # 1.1x - slight saturation boost

        frame_num = 0
        last_raw_data = None
        last_displayed_frame = None

        while self.running:
            # Read trackbar values
            r_gain = cv2.getTrackbarPos('R Gain', 'Color Correction') / 100.0
            g_gain = cv2.getTrackbarPos('G Gain', 'Color Correction') / 100.0
            b_gain = cv2.getTrackbarPos('B Gain', 'Color Correction') / 100.0
            gamma = max(0.1, cv2.getTrackbarPos('Gamma', 'Color Correction') / 100.0)
            saturation = cv2.getTrackbarPos('Saturation', 'Color Correction') / 100.0
            brightness = cv2.getTrackbarPos('Brightness', 'Color Correction') - 100  # -100 to +100

            color_correction = {
                'r_gain': r_gain,
                'g_gain': g_gain,
                'b_gain': b_gain,
                'gamma': gamma,
                'saturation': saturation,
                'brightness': brightness
            }

            # LOW LATENCY: Get the NEWEST frame, discard any older ones
            raw_data = None
            frames_in_queue = 0
            while True:
                try:
                    raw_data = self.frame_queue.get_nowait()
                    frames_in_queue += 1
                except:
                    break  # Queue empty
            if raw_data is not None:
                last_raw_data = raw_data

            current_pattern = patterns[pattern_idx]
            current_rotation = rotations[rotation_idx]

            # Process new frame if available
            if raw_data is not None:
                frame = self.decoder.process_frame(
                    raw_data,
                    enhance=enhance,
                    pattern=current_pattern,
                    rotate=current_rotation,
                    color_correction=color_correction
                )
                if frame is not None:
                    last_displayed_frame = frame.copy()
                    frame_num += 1

            # Display frame (new or last)
            if last_displayed_frame is not None:
                display_frame = last_displayed_frame.copy()
                # Show info overlay
                info1 = f"FPS:{self.fps:.1f} Pattern:{current_pattern} Rot:{current_rotation}"
                info2 = f"R:{r_gain:.2f} G:{g_gain:.2f} B:{b_gain:.2f} Gam:{gamma:.2f}"
                cv2.putText(display_frame, info1, (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.putText(display_frame, info2, (10, 60),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow('IMX662 RAW Stream', display_frame)
            else:
                # Only show connecting message at startup
                connecting_img = np.zeros((550, 960, 3), dtype=np.uint8)
                cv2.putText(connecting_img, "Connecting...", (380, 275),
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                cv2.imshow('IMX662 RAW Stream', connecting_img)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('s') and last_displayed_frame is not None:
                # Save current frame with color correction values in filename
                filename = f"frame_{frame_num:04d}_R{int(r_gain*100)}_G{int(g_gain*100)}_B{int(b_gain*100)}.jpg"
                cv2.imwrite(filename, last_displayed_frame)
                print(f"Saved: {filename}")
                if last_raw_data is not None:
                    raw_filename = f"frame_{frame_num:04d}.raw"
                    with open(raw_filename, 'wb') as f:
                        f.write(last_raw_data)
                    print(f"Saved: {raw_filename}")
                # Also save color correction settings
                settings_file = f"frame_{frame_num:04d}_settings.txt"
                with open(settings_file, 'w') as f:
                    f.write(f"R_Gain: {r_gain:.3f}\n")
                    f.write(f"G_Gain: {g_gain:.3f}\n")
                    f.write(f"B_Gain: {b_gain:.3f}\n")
                    f.write(f"Gamma: {gamma:.3f}\n")
                    f.write(f"Saturation: {saturation:.3f}\n")
                    f.write(f"Brightness: {brightness}\n")
                    f.write(f"Pattern: {current_pattern}\n")
                    f.write(f"Rotation: {current_rotation}\n")
                    f.write(f"CLAHE: {'ON' if enhance else 'OFF'}\n")
                print(f"Saved settings: {settings_file}")
            elif key == ord('e'):
                enhance = not enhance
                print(f"Enhancement: {'ON' if enhance else 'OFF'}")
            elif key == ord('p'):
                pattern_idx = (pattern_idx + 1) % len(patterns)
                print(f"Bayer pattern: {patterns[pattern_idx]}")
            elif key == ord('r'):
                rotation_idx = (rotation_idx + 1) % len(rotations)
                print(f"Rotation: {rotations[rotation_idx]}°")
            elif key == ord('c'):
                # Toggle color correction window
                show_controls = not show_controls
                if show_controls:
                    cv2.namedWindow('Color Correction', cv2.WINDOW_NORMAL)
                else:
                    cv2.destroyWindow('Color Correction')
            elif key == ord('0'):
                # Reset to defaults
                cv2.setTrackbarPos('R Gain', 'Color Correction', 100)
                cv2.setTrackbarPos('G Gain', 'Color Correction', 100)
                cv2.setTrackbarPos('B Gain', 'Color Correction', 100)
                cv2.setTrackbarPos('Gamma', 'Color Correction', 100)
                cv2.setTrackbarPos('Saturation', 'Color Correction', 100)
                cv2.setTrackbarPos('Brightness', 'Color Correction', 100)
                print("Color correction reset to defaults")
            elif key == ord('1'):
                # IMX662 preset - fix strong green tint
                cv2.setTrackbarPos('R Gain', 'Color Correction', 140)
                cv2.setTrackbarPos('G Gain', 'Color Correction', 65)
                cv2.setTrackbarPos('B Gain', 'Color Correction', 125)
                cv2.setTrackbarPos('Gamma', 'Color Correction', 110)
                cv2.setTrackbarPos('Saturation', 'Color Correction', 110)
                cv2.setTrackbarPos('Brightness', 'Color Correction', 100)
                print("Applied IMX662 preset (R:1.4 G:0.65 B:1.25)")
            elif key == ord('2'):
                # Neutral daylight preset
                cv2.setTrackbarPos('R Gain', 'Color Correction', 120)
                cv2.setTrackbarPos('G Gain', 'Color Correction', 85)
                cv2.setTrackbarPos('B Gain', 'Color Correction', 115)
                cv2.setTrackbarPos('Gamma', 'Color Correction', 100)
                cv2.setTrackbarPos('Saturation', 'Color Correction', 100)
                cv2.setTrackbarPos('Brightness', 'Color Correction', 100)
                print("Applied neutral daylight preset (R:1.2 G:0.85 B:1.15)")

        self.running = False
        cv2.destroyAllWindows()
        print("Viewer closed.")


def test_with_file(filename, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
    """Test decoder with a saved RAW or RGB file"""
    decoder = ImageDecoder(width, height)

    print(f"\nLoading: {filename}")
    with open(filename, 'rb') as f:
        raw_data = f.read()

    print(f"File size: {len(raw_data)} bytes")
    print(f"Expected RAW10 packed: {decoder.frame_size_packed} bytes")
    print(f"Expected RAW10 16-bit: {decoder.frame_size_unpacked} bytes")

    # Test all Bayer patterns to find the correct one
    patterns = ['RG', 'BG', 'GR', 'GB']
    print("\nTesting all Bayer patterns...")
    print("Controls: p=next pattern, r=rotate, e=enhance, s=save, q=quit\n")

    pattern_idx = 0
    rotation_idx = 2  # Start at 180
    rotations = [0, 90, 180, 270]
    enhance = True

    cv2.namedWindow('Test Frame', cv2.WINDOW_NORMAL)
    cv2.resizeWindow('Test Frame', 960, 550)

    while True:
        frame = decoder.process_frame(raw_data, enhance=enhance,
                                       pattern=patterns[pattern_idx],
                                       rotate=rotations[rotation_idx])
        if frame is not None:
            # Add info overlay
            info = f"Pattern:{patterns[pattern_idx]} Rot:{rotations[rotation_idx]} Enh:{'ON' if enhance else 'OFF'}"
            cv2.putText(frame, info, (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imshow('Test Frame', frame)
        else:
            print("Failed to decode frame")
            break

        key = cv2.waitKey(0) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('p'):
            pattern_idx = (pattern_idx + 1) % len(patterns)
            print(f"Pattern: {patterns[pattern_idx]}")
            decoder._debug_printed = False  # Reset debug flag
        elif key == ord('r'):
            rotation_idx = (rotation_idx + 1) % len(rotations)
            print(f"Rotation: {rotations[rotation_idx]}")
        elif key == ord('e'):
            enhance = not enhance
            print(f"Enhancement: {'ON' if enhance else 'OFF'}")
        elif key == ord('s'):
            output_file = filename.rsplit('.', 1)[0] + f'_{patterns[pattern_idx]}_decoded.jpg'
            cv2.imwrite(output_file, frame)
            print(f"Saved: {output_file}")

    cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description='RAW Bayer Stream Viewer for IMX662')
    parser.add_argument('--host', default='192.168.1.100', help='ESP32 IP address')
    parser.add_argument('--port', type=int, default=80, help='HTTP port (default: 80)')
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH, help='Frame width')
    parser.add_argument('--height', type=int, default=DEFAULT_HEIGHT, help='Frame height')
    parser.add_argument('--no-enhance', action='store_true', help='Disable CLAHE enhancement (faster)')
    parser.add_argument('--test-file', help='Test with a saved RAW file instead of streaming')

    args = parser.parse_args()

    if args.test_file:
        test_with_file(args.test_file, args.width, args.height)
    else:
        viewer = RawStreamViewer(args.host, args.port, args.width, args.height)
        viewer.run(enhance=not args.no_enhance)


if __name__ == '__main__':
    main()
