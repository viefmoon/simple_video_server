#!/usr/bin/env python3
"""
RAW Bayer Stream Viewer for IMX662
Receives RAW10 stream from ESP32-P4 and displays in real-time

Usage:
    python raw_stream_viewer.py --host 192.168.1.100 --port 81

Or for testing with saved files:
    python raw_stream_viewer.py --test-file IMG0001.RAW

RAW10 Format (MIPI packed):
    - 4 pixels are stored in 5 bytes
    - Bytes 0-3: Upper 8 bits of each pixel
    - Byte 4: Lower 2 bits of all 4 pixels packed together
    - Pattern: P0[9:2], P1[9:2], P2[9:2], P3[9:2], P3[1:0]P2[1:0]P1[1:0]P0[1:0]
"""

import numpy as np
import cv2
import argparse
import requests
import time
import threading
from queue import Queue
import struct

# IMX662 sensor configuration
DEFAULT_WIDTH = 1936
DEFAULT_HEIGHT = 1100

# RAW10 packed format: 5 bytes per 4 pixels
# Frame size = width * height * 10 bits / 8 bits = width * height * 1.25
BYTES_PER_4_PIXELS = 5  # RAW10 MIPI packed format

class RawBayerDecoder:
    """Decode RAW Bayer data from IMX662"""

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.width = width
        self.height = height

        # RAW10 packed: 5 bytes per 4 pixels
        # Width must be multiple of 4 for RAW10 packed
        self.row_bytes = (width * 5) // 4  # bytes per row (stride)
        self.frame_size_packed = self.row_bytes * height  # RAW10 packed size
        self.frame_size_unpacked = width * height * 2  # RAW10 in 16-bit container

        print(f"Decoder initialized: {width}x{height}")
        print(f"  RAW10 packed frame size: {self.frame_size_packed} bytes")
        print(f"  RAW10 unpacked (16-bit) frame size: {self.frame_size_unpacked} bytes")

        # CLAHE for auto-enhancement
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

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

    def auto_decode(self, raw_data):
        """Auto-detect format based on data size and decode"""
        data_len = len(raw_data)

        # Check which format matches
        if abs(data_len - self.frame_size_packed) < 1000:
            print(f"Detected RAW10 packed format ({data_len} bytes)")
            return self.decode_raw10_packed(raw_data)
        elif abs(data_len - self.frame_size_unpacked) < 1000:
            print(f"Detected RAW10 in 16-bit container ({data_len} bytes)")
            return self.decode_raw16(raw_data)
        else:
            print(f"Unknown format: {data_len} bytes")
            print(f"  Expected packed: {self.frame_size_packed}")
            print(f"  Expected 16-bit: {self.frame_size_unpacked}")
            # Try packed format as fallback
            return self.decode_raw10_packed(raw_data)

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

    def process_frame(self, raw_data, enhance=True, pattern='RG', rotate=0):
        """Full processing pipeline: RAW -> RGB

        Args:
            raw_data: Raw bytes from sensor
            enhance: Apply CLAHE enhancement
            pattern: Bayer pattern ('RG', 'BG', 'GR', 'GB')
            rotate: Rotation (0, 90, 180, 270)
        """
        # Auto-detect and decode RAW format
        bayer = self.auto_decode(raw_data)
        if bayer is None:
            return None

        # Debug: print min/max pixel values for first frame
        if not hasattr(self, '_debug_printed'):
            self._debug_printed = True
            print(f"Bayer stats: min={bayer.min()}, max={bayer.max()}, mean={bayer.mean():.1f}")
            if bayer.max() > 900:
                print("WARNING: Image may be overexposed (max > 900 on 10-bit scale)")

        rgb = self.debayer_rggb(bayer, pattern)

        if enhance:
            rgb = self.enhance_image(rgb)

        # Apply rotation
        if rotate == 90:
            rgb = cv2.rotate(rgb, cv2.ROTATE_90_CLOCKWISE)
        elif rotate == 180:
            rgb = cv2.rotate(rgb, cv2.ROTATE_180)
        elif rotate == 270:
            rgb = cv2.rotate(rgb, cv2.ROTATE_90_COUNTERCLOCKWISE)

        return rgb


class RawStreamViewer:
    """Real-time RAW stream viewer"""

    def __init__(self, host, port, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.host = host
        self.port = port
        self.decoder = RawBayerDecoder(width, height)
        self.frame_queue = Queue(maxsize=2)
        self.running = False
        self.fps = 0
        self.frame_count = 0

    def fetch_frame_http(self):
        """Fetch single frame via HTTP binary endpoint"""
        try:
            url = f"http://{self.host}:{self.port}/api/capture_binary?source=0"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                return response.content
        except Exception as e:
            print(f"HTTP error: {e}")
        return None

    def fetch_stream_http(self):
        """Fetch continuous stream via HTTP (multipart)"""
        url = f"http://{self.host}:{self.port}/stream"
        try:
            response = requests.get(url, stream=True, timeout=10)
            boundary = b'--' + b'123456789000000000000987654321'  # Default boundary

            buffer = b''
            for chunk in response.iter_content(chunk_size=8192):
                buffer += chunk

                # Look for frame boundaries
                while boundary in buffer:
                    parts = buffer.split(boundary, 1)
                    frame_data = parts[0]
                    buffer = parts[1] if len(parts) > 1 else b''

                    # Extract binary data (skip headers)
                    if b'\r\n\r\n' in frame_data:
                        _, data = frame_data.split(b'\r\n\r\n', 1)
                        # Accept both packed and unpacked sizes
                        min_size = self.decoder.frame_size_packed
                        if len(data) >= min_size:
                            yield data

        except Exception as e:
            print(f"Stream error: {e}")

    def receiver_thread(self):
        """Background thread to receive frames"""
        last_time = time.time()
        frame_count = 0

        while self.running:
            raw_data = self.fetch_frame_http()
            if raw_data:
                # Don't block if queue is full (drop frames)
                if not self.frame_queue.full():
                    self.frame_queue.put(raw_data)

                frame_count += 1
                current_time = time.time()
                if current_time - last_time >= 1.0:
                    self.fps = frame_count / (current_time - last_time)
                    frame_count = 0
                    last_time = current_time
            else:
                time.sleep(0.1)  # Wait before retry

    def run(self, enhance=True, save_frames=False):
        """Main display loop"""
        self.running = True

        # Settings
        patterns = ['RG', 'BG', 'GR', 'GB']
        pattern_idx = 0
        rotations = [0, 90, 180, 270]
        rotation_idx = 2  # Start with 180 (image is upside down)

        # Start receiver thread
        receiver = threading.Thread(target=self.receiver_thread, daemon=True)
        receiver.start()

        print(f"Connecting to {self.host}:{self.port}...")
        print("Controls:")
        print("  q - Quit")
        print("  s - Save frame")
        print("  e - Toggle enhancement")
        print("  p - Cycle Bayer pattern (RG/BG/GR/GB)")
        print("  r - Rotate image")

        cv2.namedWindow('IMX662 RAW Stream', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('IMX662 RAW Stream', 960, 550)

        frame_num = 0
        while self.running:
            try:
                raw_data = self.frame_queue.get(timeout=1.0)
            except:
                # Show "waiting" message
                waiting_img = np.zeros((550, 960, 3), dtype=np.uint8)
                cv2.putText(waiting_img, "Waiting for frames...", (300, 275),
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                cv2.imshow('IMX662 RAW Stream', waiting_img)
                key = cv2.waitKey(100) & 0xFF
                if key == ord('q'):
                    break
                continue

            # Decode and display
            current_pattern = patterns[pattern_idx]
            current_rotation = rotations[rotation_idx]
            frame = self.decoder.process_frame(raw_data, enhance=enhance,
                                                pattern=current_pattern,
                                                rotate=current_rotation)
            if frame is not None:
                # Add info overlay
                info = f"FPS:{self.fps:.1f} Pattern:{current_pattern} Rot:{current_rotation}"
                cv2.putText(frame, info, (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                cv2.imshow('IMX662 RAW Stream', frame)
                frame_num += 1

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('s'):
                # Save current frame
                filename = f"frame_{frame_num:04d}.jpg"
                cv2.imwrite(filename, frame)
                print(f"Saved: {filename}")
                # Also save RAW
                raw_filename = f"frame_{frame_num:04d}.raw"
                with open(raw_filename, 'wb') as f:
                    f.write(raw_data)
                print(f"Saved: {raw_filename}")
            elif key == ord('e'):
                enhance = not enhance
                print(f"Enhancement: {'ON' if enhance else 'OFF'}")
            elif key == ord('p'):
                # Cycle through Bayer patterns
                pattern_idx = (pattern_idx + 1) % len(patterns)
                print(f"Bayer pattern: {patterns[pattern_idx]}")
            elif key == ord('r'):
                # Cycle through rotations
                rotation_idx = (rotation_idx + 1) % len(rotations)
                print(f"Rotation: {rotations[rotation_idx]}°")

        self.running = False
        cv2.destroyAllWindows()
        print("Viewer closed.")


def test_with_file(filename, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
    """Test decoder with a saved RAW file"""
    decoder = RawBayerDecoder(width, height)

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
    parser.add_argument('--port', type=int, default=81, help='HTTP port (default: 81)')
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH, help='Frame width')
    parser.add_argument('--height', type=int, default=DEFAULT_HEIGHT, help='Frame height')
    parser.add_argument('--no-enhance', action='store_true', help='Disable image enhancement')
    parser.add_argument('--test-file', help='Test with a saved RAW file instead of streaming')

    args = parser.parse_args()

    if args.test_file:
        test_with_file(args.test_file, args.width, args.height)
    else:
        viewer = RawStreamViewer(args.host, args.port, args.width, args.height)
        viewer.run(enhance=not args.no_enhance)


if __name__ == '__main__':
    main()
