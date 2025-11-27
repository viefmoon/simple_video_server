#!/usr/bin/env python3
"""
RGB565 Stream Viewer for IMX662
Receives RGB565 stream from ESP32-P4 (ISP processed) and displays in real-time

Usage:
    python rgb565_stream_viewer.py --host 192.168.1.100 --port 80
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

# RGB565: 2 bytes per pixel
BYTES_PER_PIXEL = 2


class RGB565Decoder:
    """Decode RGB565 data from ESP32-P4 ISP"""

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.width = width
        self.height = height
        self.frame_size = width * height * BYTES_PER_PIXEL
        print(f"Decoder initialized: {width}x{height}")
        print(f"  RGB565 frame size: {self.frame_size} bytes")

    def decode_rgb565(self, raw_data):
        """Decode RGB565 data to BGR for OpenCV"""
        if len(raw_data) < self.frame_size:
            print(f"Warning: Expected {self.frame_size} bytes, got {len(raw_data)}")
            return None

        # Take exact frame size
        raw_data = raw_data[:self.frame_size]

        # Convert to numpy array (16-bit little-endian)
        rgb565 = np.frombuffer(raw_data, dtype=np.uint16).reshape((self.height, self.width))

        # Extract RGB components
        # RGB565: RRRRRGGGGGGBBBBB (5-6-5 bits)
        r = ((rgb565 >> 11) & 0x1F).astype(np.uint8)  # 5 bits
        g = ((rgb565 >> 5) & 0x3F).astype(np.uint8)   # 6 bits
        b = (rgb565 & 0x1F).astype(np.uint8)          # 5 bits

        # Scale to 8-bit
        r = (r << 3) | (r >> 2)  # 5-bit to 8-bit
        g = (g << 2) | (g >> 4)  # 6-bit to 8-bit
        b = (b << 3) | (b >> 2)  # 5-bit to 8-bit

        # Create BGR image for OpenCV
        bgr = np.stack([b, g, r], axis=-1)

        return bgr

    def process_frame(self, raw_data, rotate=0):
        """Process frame with optional rotation"""
        bgr = self.decode_rgb565(raw_data)
        if bgr is None:
            return None

        # Apply rotation
        if rotate == 90:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
        elif rotate == 180:
            bgr = cv2.rotate(bgr, cv2.ROTATE_180)
        elif rotate == 270:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)

        return bgr


class RGB565StreamViewer:
    """Real-time RGB565 stream viewer"""

    def __init__(self, host, port, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.host = host
        self.port = port
        self.decoder = RGB565Decoder(width, height)
        self.frame_queue = Queue(maxsize=8)
        self.running = False
        self.fps = 0
        self.last_frame = None

    def receiver_thread(self):
        """Background thread using continuous streaming"""
        last_time = time.time()
        frame_count = 0

        url = f"http://{self.host}:{self.port}/stream"
        boundary = b'--raw_frame_boundary'
        min_size = self.decoder.frame_size

        print(f"Expected frame size: {min_size} bytes")

        while self.running:
            try:
                print(f"Connecting to stream: {url}")
                response = requests.get(url, stream=True, timeout=10)
                buffer = b''

                for chunk in response.iter_content(chunk_size=65536):
                    if not self.running:
                        break

                    buffer += chunk

                    # Look for frame boundaries
                    while boundary in buffer:
                        parts = buffer.split(boundary, 1)
                        frame_data = parts[0]
                        buffer = parts[1] if len(parts) > 1 else b''

                        # Extract binary data (skip headers)
                        if b'\r\n\r\n' in frame_data:
                            _, data = frame_data.split(b'\r\n\r\n', 1)

                            if len(data) >= min_size:
                                # Drop oldest frame if queue full
                                if self.frame_queue.full():
                                    try:
                                        self.frame_queue.get_nowait()
                                    except:
                                        pass
                                self.frame_queue.put(data[:min_size])

                                frame_count += 1
                                current_time = time.time()
                                if current_time - last_time >= 1.0:
                                    self.fps = frame_count / (current_time - last_time)
                                    print(f"FPS: {self.fps:.1f}")
                                    frame_count = 0
                                    last_time = current_time

            except Exception as e:
                print(f"Stream error: {e}, reconnecting...")
                time.sleep(0.5)

    def run(self):
        """Main display loop"""
        self.running = True

        rotations = [0, 90, 180, 270]
        rotation_idx = 2  # Start with 180

        # Start receiver thread
        receiver = threading.Thread(target=self.receiver_thread, daemon=True)
        receiver.start()

        print(f"Connecting to {self.host}:{self.port}...")
        print("Controls:")
        print("  q - Quit")
        print("  s - Save frame")
        print("  r - Rotate image")

        cv2.namedWindow('IMX662 RGB565 Stream', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('IMX662 RGB565 Stream', 960, 550)

        frame_num = 0
        last_raw_data = None
        last_displayed_frame = None

        while self.running:
            # Try to get new frame
            try:
                raw_data = self.frame_queue.get(timeout=0.05)
                last_raw_data = raw_data
            except:
                raw_data = None

            current_rotation = rotations[rotation_idx]

            # Process new frame if available
            if raw_data is not None:
                frame = self.decoder.process_frame(raw_data, rotate=current_rotation)
                if frame is not None:
                    last_displayed_frame = frame.copy()
                    frame_num += 1

            # Display frame
            if last_displayed_frame is not None:
                display_frame = last_displayed_frame.copy()
                info = f"FPS:{self.fps:.1f} Rot:{current_rotation} Frame:{frame_num}"
                cv2.putText(display_frame, info, (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow('IMX662 RGB565 Stream', display_frame)
            else:
                # Show connecting message at startup
                connecting_img = np.zeros((550, 960, 3), dtype=np.uint8)
                cv2.putText(connecting_img, "Connecting...", (380, 275),
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                cv2.imshow('IMX662 RGB565 Stream', connecting_img)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('s') and last_displayed_frame is not None:
                filename = f"frame_{frame_num:04d}.jpg"
                cv2.imwrite(filename, last_displayed_frame)
                print(f"Saved: {filename}")
            elif key == ord('r'):
                rotation_idx = (rotation_idx + 1) % len(rotations)
                print(f"Rotation: {rotations[rotation_idx]}")

        self.running = False
        cv2.destroyAllWindows()
        print("Viewer closed.")


def main():
    parser = argparse.ArgumentParser(description='RGB565 Stream Viewer for IMX662')
    parser.add_argument('--host', default='192.168.1.100', help='ESP32 IP address')
    parser.add_argument('--port', type=int, default=80, help='HTTP port (default: 80)')
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH, help='Frame width')
    parser.add_argument('--height', type=int, default=DEFAULT_HEIGHT, help='Frame height')

    args = parser.parse_args()

    viewer = RGB565StreamViewer(args.host, args.port, args.width, args.height)
    viewer.run()


if __name__ == '__main__':
    main()
