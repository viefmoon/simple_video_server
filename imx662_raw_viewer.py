"""
IMX662 RAW Image Viewer
-----------------------
Visualizador de imágenes RAW 12-bit del sensor Sony IMX662.

Especificaciones del sensor:
- Resolución: 1920x1080 (modo all-pixel) o 960x540 (modo binning)
- Patrón Bayer: GBRG
- Profundidad de bits: 12-bit RAW

Autor: Generado con Claude Code
"""

import numpy as np
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from PIL import Image, ImageTk
import os

# Constantes del sensor IMX662
RESOLUTIONS = {
    "1920x1080 (All-pixel)": (1920, 1080),
    "960x540 (Binning 2x2)": (960, 540),
    "Custom": None
}

BAYER_PATTERNS = {
    "GBRG": "gbrg",  # IMX662 default
    "RGGB": "rggb",
    "BGGR": "bggr",
    "GRBG": "grbg"
}


def unpack_raw12_v4l2_sbggr12(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta V4L2_PIX_FMT_SBGGR12 (BG12) - Formato MIPI CSI-2 RAW12
    Byte 0: P0[7:0]            (8 bits LSB del pixel 0)
    Byte 1: P1[7:0]            (8 bits LSB del pixel 1)
    Byte 2: P1[11:8] | P0[11:8] (4 MSB de P1 en nibble alto, 4 MSB de P0 en nibble bajo)
    """
    expected_size_packed = (width * height * 3) // 2

    if len(data) >= expected_size_packed:
        raw_data = np.frombuffer(data[:expected_size_packed], dtype=np.uint8)
        raw_data = raw_data.reshape(-1, 3)

        byte0 = raw_data[:, 0].astype(np.uint16)  # P0 LSB
        byte1 = raw_data[:, 1].astype(np.uint16)  # P1 LSB
        byte2 = raw_data[:, 2].astype(np.uint16)  # MSBs combinados

        # pixel0 = P0[11:8] << 8 | P0[7:0]
        pixel0 = ((byte2 & 0x0F) << 8) | byte0

        # pixel1 = P1[11:8] << 8 | P1[7:0]
        pixel1 = ((byte2 >> 4) << 8) | byte1

        result = np.empty(width * height, dtype=np.uint16)
        result[0::2] = pixel0
        result[1::2] = pixel1

        return result.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size_packed} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_packed_v1(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta RAW12 packed - Formato ESP32-P4 / MSB-first

    Este es el formato correcto para imágenes capturadas con ESP32-P4 + IMX662.

    Formato de bytes:
    - Byte 0: P0[11:4] (8 bits MSB del pixel 0)
    - Byte 1: P1[11:4] (8 bits MSB del pixel 1)
    - Byte 2: [P1[3:0] | P0[3:0]] (4 bits LSB de cada pixel)

    Reconstrucción:
    - pixel0 = (byte0 << 4) | (byte2 & 0x0F)
    - pixel1 = (byte1 << 4) | ((byte2 >> 4) & 0x0F)
    """
    expected_size_packed = (width * height * 3) // 2

    if len(data) >= expected_size_packed:
        raw_data = np.frombuffer(data[:expected_size_packed], dtype=np.uint8)
        raw_data = raw_data.reshape(-1, 3)

        pixel0 = (raw_data[:, 0].astype(np.uint16) << 4) | (raw_data[:, 2] & 0x0F)
        pixel1 = (raw_data[:, 1].astype(np.uint16) << 4) | ((raw_data[:, 2] >> 4) & 0x0F)

        result = np.empty(width * height, dtype=np.uint16)
        result[0::2] = pixel0
        result[1::2] = pixel1

        return result.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size_packed} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_packed_v2(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta RAW12 packed - Variante 2
    Byte 0: P0[11:4], Byte 1: P1[11:4], Byte 2: [P0[3:0] | P1[3:0]]
    """
    expected_size_packed = (width * height * 3) // 2

    if len(data) >= expected_size_packed:
        raw_data = np.frombuffer(data[:expected_size_packed], dtype=np.uint8)
        raw_data = raw_data.reshape(-1, 3)

        pixel0 = (raw_data[:, 0].astype(np.uint16) << 4) | ((raw_data[:, 2] >> 4) & 0x0F)
        pixel1 = (raw_data[:, 1].astype(np.uint16) << 4) | (raw_data[:, 2] & 0x0F)

        result = np.empty(width * height, dtype=np.uint16)
        result[0::2] = pixel0
        result[1::2] = pixel1

        return result.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size_packed} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_packed_v3(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta RAW12 packed - Variante 3 (por línea, con stride)
    Procesa línea por línea para manejar posible padding
    """
    bytes_per_line = (width * 3) // 2
    expected_size = bytes_per_line * height

    if len(data) >= expected_size:
        result = np.zeros((height, width), dtype=np.uint16)

        for y in range(height):
            line_start = y * bytes_per_line
            line_data = np.frombuffer(data[line_start:line_start + bytes_per_line], dtype=np.uint8)
            line_data = line_data.reshape(-1, 3)

            pixel0 = (line_data[:, 0].astype(np.uint16) << 4) | (line_data[:, 2] & 0x0F)
            pixel1 = (line_data[:, 1].astype(np.uint16) << 4) | ((line_data[:, 2] >> 4) & 0x0F)

            result[y, 0::2] = pixel0
            result[y, 1::2] = pixel1

        return result
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_packed_v4(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta RAW12 packed - Variante 4 (little-endian, 16-bit words)
    Lee como palabras de 16 bits y extrae los 12 bits
    """
    expected_size = width * height * 2

    # Si el archivo es más pequeño, usar formato packed real
    if len(data) < expected_size:
        return unpack_raw12_packed_v1(data, width, height)

    raw_data = np.frombuffer(data[:expected_size], dtype=np.uint16)
    # Extraer 12 bits (asumiendo little-endian)
    result = raw_data & 0x0FFF
    return result.reshape(height, width)


def unpack_raw10_packed(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Desempaqueta RAW10 packed (5 bytes por cada 4 píxeles) - por si es RAW10
    """
    expected_size = (width * height * 5) // 4

    if len(data) >= expected_size:
        raw_data = np.frombuffer(data[:expected_size], dtype=np.uint8)
        raw_data = raw_data.reshape(-1, 5)

        # 4 píxeles de cada 5 bytes
        p0 = (raw_data[:, 0].astype(np.uint16) << 2) | (raw_data[:, 4] & 0x03)
        p1 = (raw_data[:, 1].astype(np.uint16) << 2) | ((raw_data[:, 4] >> 2) & 0x03)
        p2 = (raw_data[:, 2].astype(np.uint16) << 2) | ((raw_data[:, 4] >> 4) & 0x03)
        p3 = (raw_data[:, 3].astype(np.uint16) << 2) | ((raw_data[:, 4] >> 6) & 0x03)

        result = np.empty(width * height, dtype=np.uint16)
        result[0::4] = p0
        result[1::4] = p1
        result[2::4] = p2
        result[3::4] = p3

        # Escalar de 10 a 12 bits
        result = result << 2

        return result.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes para RAW10. Esperado: {expected_size} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_unpacked(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Lee datos RAW12 en formato unpacked (2 bytes por pixel, little-endian).
    Los 12 bits están en los bits menos significativos.
    """
    expected_size = width * height * 2

    if len(data) >= expected_size:
        raw_data = np.frombuffer(data[:expected_size], dtype=np.uint16)
        # Máscara para obtener solo los 12 bits
        raw_data = raw_data & 0x0FFF
        return raw_data.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size} bytes, Recibido: {len(data)} bytes")


def unpack_raw12_msb(data: bytes, width: int, height: int) -> np.ndarray:
    """
    Lee datos RAW12 en formato unpacked MSB-aligned (2 bytes por pixel).
    Los 12 bits están en los bits más significativos.
    """
    expected_size = width * height * 2

    if len(data) >= expected_size:
        raw_data = np.frombuffer(data[:expected_size], dtype=np.uint16)
        # Shift right para obtener los 12 bits más significativos
        raw_data = raw_data >> 4
        return raw_data.reshape(height, width)
    else:
        raise ValueError(f"Datos insuficientes. Esperado: {expected_size} bytes, Recibido: {len(data)} bytes")


def demosaic_gbrg(bayer: np.ndarray) -> np.ndarray:
    """
    Demosaico de patrón Bayer GBRG a RGB usando interpolación bilineal.

    Patrón GBRG:
    G B G B ...
    R G R G ...
    G B G B ...
    R G R G ...
    """
    height, width = bayer.shape

    # Crear imagen RGB
    rgb = np.zeros((height, width, 3), dtype=np.float32)

    # Normalizar a float para procesamiento
    bayer_float = bayer.astype(np.float32)

    # Canal Rojo (R está en posiciones [1,0], [1,2], [3,0], etc.)
    # R en posiciones (fila impar, columna par)
    rgb[1::2, 0::2, 0] = bayer_float[1::2, 0::2]

    # Interpolar R en posiciones (fila impar, columna impar) - horizontal
    rgb[1::2, 1::2, 0] = (bayer_float[1::2, 0:-1:2] + bayer_float[1::2, 2::2]) / 2

    # Interpolar R en posiciones (fila par, columna par) - vertical
    rgb[0:-1:2, 0::2, 0] = (bayer_float[1::2, 0::2][:-1] + bayer_float[1::2, 0::2][1:]) / 2
    rgb[0, 0::2, 0] = bayer_float[1, 0::2]  # Primera fila

    # Interpolar R en posiciones (fila par, columna impar) - diagonal
    rgb[0:-1:2, 1::2, 0] = (
        bayer_float[1::2, 0:-1:2][:-1] + bayer_float[1::2, 2::2][:-1] +
        bayer_float[1::2, 0:-1:2][1:] + bayer_float[1::2, 2::2][1:]
    ) / 4
    rgb[0, 1::2, 0] = (bayer_float[1, 0:-1:2] + bayer_float[1, 2::2]) / 2

    # Canal Azul (B está en posiciones [0,1], [0,3], [2,1], etc.)
    # B en posiciones (fila par, columna impar)
    rgb[0::2, 1::2, 2] = bayer_float[0::2, 1::2]

    # Interpolar B en posiciones (fila par, columna par) - horizontal
    rgb[0::2, 0:-1:2, 2] = (bayer_float[0::2, 1::2][:, :-1] + bayer_float[0::2, 1::2][:, 1:]) / 2
    rgb[0::2, 0, 2] = bayer_float[0::2, 1]  # Primera columna

    # Interpolar B en posiciones (fila impar, columna impar) - vertical
    rgb[1::2, 1::2, 2] = (bayer_float[0:-1:2, 1::2] + bayer_float[2::2, 1::2]) / 2

    # Interpolar B en posiciones (fila impar, columna par) - diagonal
    rgb[1::2, 0:-1:2, 2] = (
        bayer_float[0:-1:2, 1::2][:, :-1] + bayer_float[0:-1:2, 1::2][:, 1:] +
        bayer_float[2::2, 1::2][:, :-1] + bayer_float[2::2, 1::2][:, 1:]
    ) / 4
    rgb[1::2, 0, 2] = (bayer_float[0:-1:2, 1] + bayer_float[2::2, 1]) / 2

    # Canal Verde
    # G está en [0,0], [0,2], [1,1], [1,3], etc.
    # Gb (fila par, columna par)
    rgb[0::2, 0::2, 1] = bayer_float[0::2, 0::2]
    # Gr (fila impar, columna impar)
    rgb[1::2, 1::2, 1] = bayer_float[1::2, 1::2]

    # Interpolar G en posiciones R (fila impar, columna par)
    rgb[1::2, 0:-1:2, 1] = (
        bayer_float[0:-1:2, 0:-1:2] + bayer_float[2::2, 0:-1:2] +
        bayer_float[1::2, 1::2][:, :-1] + bayer_float[1::2, 1::2][:, 1:]
    ) / 4
    rgb[1::2, -1, 1] = (bayer_float[0:-1:2, -1] + bayer_float[2::2, -1] + bayer_float[1::2, -2]) / 3

    # Interpolar G en posiciones B (fila par, columna impar)
    rgb[0:-1:2, 1::2, 1] = (
        bayer_float[0:-1:2, 0:-1:2][:, 1:] + bayer_float[0:-1:2, 2::2] +
        bayer_float[1::2, 1::2][:-1] + bayer_float[1::2, 1::2][1:]
    ) / 4
    rgb[-1, 1::2, 1] = (bayer_float[-1, 0:-1:2][1:] + bayer_float[-1, 2::2] + bayer_float[-2, 1::2]) / 3

    return rgb


def demosaic_simple(bayer: np.ndarray, pattern: str = "gbrg") -> np.ndarray:
    """
    Demosaico simple usando OpenCV si está disponible, o interpolación básica.
    """
    try:
        import cv2

        # Convertir a 16 bits para OpenCV
        bayer_16 = bayer.astype(np.uint16)

        pattern_codes = {
            "gbrg": cv2.COLOR_BAYER_GB2RGB,
            "rggb": cv2.COLOR_BAYER_RG2RGB,
            "bggr": cv2.COLOR_BAYER_BG2RGB,
            "grbg": cv2.COLOR_BAYER_GR2RGB
        }

        code = pattern_codes.get(pattern.lower(), cv2.COLOR_BAYER_GB2RGB)
        rgb = cv2.cvtColor(bayer_16, code)
        return rgb.astype(np.float32)

    except ImportError:
        # Usar implementación propia si OpenCV no está disponible
        if pattern.lower() == "gbrg":
            return demosaic_gbrg(bayer)
        else:
            # Fallback básico para otros patrones
            return demosaic_gbrg(bayer)


def apply_white_balance(rgb: np.ndarray, r_gain: float = 1.0, g_gain: float = 1.0, b_gain: float = 1.0) -> np.ndarray:
    """Aplica balance de blancos."""
    result = rgb.copy()
    result[:, :, 0] *= r_gain
    result[:, :, 1] *= g_gain
    result[:, :, 2] *= b_gain
    return result


def apply_gamma(rgb: np.ndarray, gamma: float = 2.2) -> np.ndarray:
    """Aplica corrección gamma."""
    # Normalizar a [0, 1]
    max_val = rgb.max() if rgb.max() > 0 else 1
    normalized = rgb / max_val
    # Aplicar gamma
    corrected = np.power(normalized, 1.0 / gamma)
    return corrected * 255


class IMX662Viewer:
    def __init__(self, root):
        self.root = root
        self.root.title("IMX662 RAW Image Viewer")
        self.root.geometry("1200x800")

        self.current_image = None
        self.current_raw = None
        self.current_file = None  # Guardar path del archivo actual
        self.current_data = None  # Guardar datos raw del archivo
        self.photo = None

        self.setup_ui()

    def setup_ui(self):
        # Frame principal
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")

        # Configurar expansión
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(1, weight=1)

        # Panel de controles (izquierda)
        control_frame = ttk.LabelFrame(main_frame, text="Controles", padding="10")
        control_frame.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 10))

        # Botón para abrir archivo
        ttk.Button(control_frame, text="Abrir imagen RAW", command=self.open_file).grid(
            row=0, column=0, columnspan=2, pady=5, sticky="ew"
        )

        # Resolución
        ttk.Label(control_frame, text="Resolución:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.resolution_var = tk.StringVar(value="1920x1080 (All-pixel)")
        resolution_combo = ttk.Combobox(control_frame, textvariable=self.resolution_var,
                                        values=list(RESOLUTIONS.keys()), state="readonly", width=20)
        resolution_combo.grid(row=2, column=0, columnspan=2, pady=2, sticky="ew")
        resolution_combo.bind("<<ComboboxSelected>>", self.on_resolution_change)

        # Resolución personalizada
        self.custom_frame = ttk.Frame(control_frame)
        self.custom_frame.grid(row=3, column=0, columnspan=2, pady=5)

        ttk.Label(self.custom_frame, text="Ancho:").grid(row=0, column=0, padx=2)
        self.width_var = tk.StringVar(value="1920")
        ttk.Entry(self.custom_frame, textvariable=self.width_var, width=8).grid(row=0, column=1, padx=2)

        ttk.Label(self.custom_frame, text="Alto:").grid(row=0, column=2, padx=2)
        self.height_var = tk.StringVar(value="1080")
        ttk.Entry(self.custom_frame, textvariable=self.height_var, width=8).grid(row=0, column=3, padx=2)

        # Formato de datos
        ttk.Label(control_frame, text="Formato RAW:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        # ESP32-P4 RAW12 es el formato correcto para IMX662 + ESP32-P4
        self.format_var = tk.StringVar(value="ESP32-P4 RAW12 (MSB first)")
        format_combo = ttk.Combobox(control_frame, textvariable=self.format_var,
                                    values=["ESP32-P4 RAW12 (MSB first)",
                                           "V4L2 SBGGR12 (BG12)",
                                           "Packed V2 (LSB invertido)",
                                           "Packed V3 (por línea)",
                                           "RAW10 Packed (5B/4px)",
                                           "Unpacked LSB (2 bytes/pixel)",
                                           "Unpacked MSB (2 bytes/pixel)"],
                                    state="readonly", width=25)
        format_combo.grid(row=5, column=0, columnspan=2, pady=2, sticky="ew")
        format_combo.bind("<<ComboboxSelected>>", self.on_format_change)

        # Patrón Bayer
        ttk.Label(control_frame, text="Patrón Bayer:").grid(row=6, column=0, sticky="w", pady=(10, 0))
        self.bayer_var = tk.StringVar(value="BGGR")
        bayer_combo = ttk.Combobox(control_frame, textvariable=self.bayer_var,
                                   values=list(BAYER_PATTERNS.keys()), state="readonly", width=20)
        bayer_combo.grid(row=7, column=0, columnspan=2, pady=2, sticky="ew")
        bayer_combo.bind("<<ComboboxSelected>>", lambda e: self.process_and_display())

        # Opción de escala de grises (sin demosaico)
        self.grayscale_var = tk.BooleanVar(value=False)
        grayscale_check = ttk.Checkbutton(control_frame, text="Solo escala de grises (debug)",
                        variable=self.grayscale_var, command=self.process_and_display)
        grayscale_check.grid(row=8, column=0, columnspan=2, pady=5, sticky="w")

        # Separador
        ttk.Separator(control_frame, orient="horizontal").grid(row=9, column=0, columnspan=2,
                                                                sticky="ew", pady=15)

        # Ajustes de imagen
        ttk.Label(control_frame, text="Ajustes de imagen", font=("", 10, "bold")).grid(
            row=10, column=0, columnspan=2, sticky="w"
        )

        # Gamma
        ttk.Label(control_frame, text="Gamma:").grid(row=11, column=0, sticky="w", pady=(5, 0))
        self.gamma_var = tk.DoubleVar(value=2.2)
        gamma_scale = ttk.Scale(control_frame, from_=0.5, to=4.0, variable=self.gamma_var,
                                orient="horizontal", length=150, command=self.on_slider_change)
        gamma_scale.grid(row=12, column=0, columnspan=2, sticky="ew")
        self.gamma_label = ttk.Label(control_frame, text="2.2")
        self.gamma_label.grid(row=12, column=1, sticky="e")

        # Balance de blancos
        ttk.Label(control_frame, text="Balance de Blancos:").grid(row=13, column=0, sticky="w", pady=(10, 0))

        # R gain
        ttk.Label(control_frame, text="R:").grid(row=14, column=0, sticky="w")
        self.r_gain_var = tk.DoubleVar(value=1.0)
        r_scale = ttk.Scale(control_frame, from_=0.5, to=2.0, variable=self.r_gain_var,
                  orient="horizontal", length=150, command=self.on_slider_change)
        r_scale.grid(row=14, column=1, sticky="ew")

        # G gain
        ttk.Label(control_frame, text="G:").grid(row=15, column=0, sticky="w")
        self.g_gain_var = tk.DoubleVar(value=1.0)
        g_scale = ttk.Scale(control_frame, from_=0.5, to=2.0, variable=self.g_gain_var,
                  orient="horizontal", length=150, command=self.on_slider_change)
        g_scale.grid(row=15, column=1, sticky="ew")

        # B gain
        ttk.Label(control_frame, text="B:").grid(row=16, column=0, sticky="w")
        self.b_gain_var = tk.DoubleVar(value=1.0)
        b_scale = ttk.Scale(control_frame, from_=0.5, to=2.0, variable=self.b_gain_var,
                  orient="horizontal", length=150, command=self.on_slider_change)
        b_scale.grid(row=16, column=1, sticky="ew")

        # Botón de reprocesar
        ttk.Button(control_frame, text="Aplicar ajustes", command=self.reprocess_image).grid(
            row=17, column=0, columnspan=2, pady=15, sticky="ew"
        )

        # Botón guardar
        ttk.Button(control_frame, text="Guardar como PNG", command=self.save_image).grid(
            row=18, column=0, columnspan=2, pady=5, sticky="ew"
        )

        # Información del archivo
        self.info_label = ttk.Label(control_frame, text="No hay imagen cargada", wraplength=200)
        self.info_label.grid(row=19, column=0, columnspan=2, pady=10)

        # Panel de imagen (derecha)
        image_frame = ttk.LabelFrame(main_frame, text="Vista previa", padding="5")
        image_frame.grid(row=0, column=1, rowspan=2, sticky="nsew")
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)

        # Canvas con scrollbars
        canvas_frame = ttk.Frame(image_frame)
        canvas_frame.grid(row=0, column=0, sticky="nsew")
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(canvas_frame, bg="gray20")
        self.canvas.grid(row=0, column=0, sticky="nsew")

        # Scrollbars
        v_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        h_scroll.grid(row=1, column=0, sticky="ew")

        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        # Zoom
        zoom_frame = ttk.Frame(image_frame)
        zoom_frame.grid(row=1, column=0, pady=5)

        ttk.Button(zoom_frame, text="Zoom -", command=lambda: self.zoom(0.8)).grid(row=0, column=0, padx=5)
        ttk.Button(zoom_frame, text="Zoom 100%", command=lambda: self.zoom(1.0, absolute=True)).grid(row=0, column=1, padx=5)
        ttk.Button(zoom_frame, text="Zoom +", command=lambda: self.zoom(1.25)).grid(row=0, column=2, padx=5)
        ttk.Button(zoom_frame, text="Ajustar", command=self.fit_to_window).grid(row=0, column=3, padx=5)

        self.zoom_level = 1.0
        self.zoom_label = ttk.Label(zoom_frame, text="100%")
        self.zoom_label.grid(row=0, column=4, padx=10)

    def on_resolution_change(self, event=None):
        res = self.resolution_var.get()
        if res == "Custom":
            for widget in self.custom_frame.winfo_children():
                widget.configure(state="normal")
        else:
            if res in RESOLUTIONS and RESOLUTIONS[res]:
                w, h = RESOLUTIONS[res]
                self.width_var.set(str(w))
                self.height_var.set(str(h))

    def open_file(self):
        filetypes = [
            ("RAW files", "*.raw"),
            ("All files", "*.*")
        ]

        filename = filedialog.askopenfilename(
            title="Seleccionar imagen RAW",
            filetypes=filetypes
        )

        if filename:
            self.load_raw_file(filename)

    def load_raw_file(self, filename):
        try:
            # Leer archivo
            with open(filename, 'rb') as f:
                self.current_data = f.read()

            self.current_file = filename

            # Desempaquetar y procesar
            self.unpack_and_process()

            # Actualizar info
            file_size = os.path.getsize(filename)
            width = int(self.width_var.get())
            height = int(self.height_var.get())
            self.info_label.config(
                text=f"Archivo: {os.path.basename(filename)}\n"
                     f"Tamaño: {file_size:,} bytes\n"
                     f"Resolución: {width}x{height}\n"
                     f"RAW max: {self.current_raw.max()}\n"
                     f"RAW min: {self.current_raw.min()}"
            )

        except Exception as e:
            messagebox.showerror("Error", f"Error al cargar la imagen:\n{str(e)}")

    def unpack_and_process(self):
        """Desempaqueta los datos RAW y procesa la imagen"""
        if self.current_data is None:
            return

        try:
            # Obtener dimensiones
            width = int(self.width_var.get())
            height = int(self.height_var.get())

            # Desempaquetar según el formato
            format_type = self.format_var.get()

            if "ESP32-P4" in format_type or "MSB first" in format_type:
                # Formato correcto para ESP32-P4 + IMX662
                self.current_raw = unpack_raw12_packed_v1(self.current_data, width, height)
            elif "V4L2" in format_type or "BG12" in format_type:
                self.current_raw = unpack_raw12_v4l2_sbggr12(self.current_data, width, height)
            elif "Packed V2" in format_type:
                self.current_raw = unpack_raw12_packed_v2(self.current_data, width, height)
            elif "Packed V3" in format_type:
                self.current_raw = unpack_raw12_packed_v3(self.current_data, width, height)
            elif "RAW10" in format_type:
                self.current_raw = unpack_raw10_packed(self.current_data, width, height)
            elif "LSB" in format_type:
                self.current_raw = unpack_raw12_unpacked(self.current_data, width, height)
            else:  # MSB
                self.current_raw = unpack_raw12_msb(self.current_data, width, height)

            # Procesar imagen
            self.process_and_display()

        except Exception as e:
            messagebox.showerror("Error", f"Error al desempaquetar:\n{str(e)}")

    def on_format_change(self, event=None):
        """Llamado cuando cambia el formato RAW"""
        if self.current_data is not None:
            self.unpack_and_process()

    def on_slider_change(self, value=None):
        """Llamado cuando cambia un slider"""
        self.gamma_label.config(text=f"{self.gamma_var.get():.1f}")
        self.process_and_display()

    def process_and_display(self):
        if self.current_raw is None:
            return

        try:
            if self.grayscale_var.get():
                # Modo escala de grises - sin demosaico
                # Normalizar a 8 bits
                raw_normalized = self.current_raw.astype(np.float32)
                max_val = raw_normalized.max() if raw_normalized.max() > 0 else 1
                raw_normalized = raw_normalized / max_val

                # Aplicar gamma
                raw_normalized = np.power(raw_normalized, 1.0 / self.gamma_var.get())
                gray = (raw_normalized * 255).astype(np.uint8)

                # Convertir a imagen
                self.current_image = Image.fromarray(gray, mode='L')
            else:
                # Demosaico normal
                pattern = BAYER_PATTERNS.get(self.bayer_var.get(), "gbrg")
                rgb = demosaic_simple(self.current_raw, pattern)

                # Balance de blancos
                rgb = apply_white_balance(rgb,
                                         self.r_gain_var.get(),
                                         self.g_gain_var.get(),
                                         self.b_gain_var.get())

                # Corrección gamma
                rgb = apply_gamma(rgb, self.gamma_var.get())

                # Convertir a 8 bits
                rgb = np.clip(rgb, 0, 255).astype(np.uint8)

                # Guardar imagen procesada
                self.current_image = Image.fromarray(rgb)

            # Mostrar
            self.display_image()

        except Exception as e:
            messagebox.showerror("Error", f"Error al procesar la imagen:\n{str(e)}")

    def display_image(self):
        if self.current_image is None:
            return

        # Calcular tamaño con zoom
        width = int(self.current_image.width * self.zoom_level)
        height = int(self.current_image.height * self.zoom_level)

        # Redimensionar para mostrar
        display_image = self.current_image.resize((width, height), Image.Resampling.LANCZOS)

        # Convertir a PhotoImage
        self.photo = ImageTk.PhotoImage(display_image)

        # Actualizar canvas
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, width, height))

        # Actualizar label de zoom
        self.zoom_label.config(text=f"{int(self.zoom_level * 100)}%")

    def zoom(self, factor, absolute=False):
        if self.current_image is None:
            return

        if absolute:
            self.zoom_level = factor
        else:
            self.zoom_level *= factor

        self.zoom_level = max(0.1, min(5.0, self.zoom_level))
        self.display_image()

    def fit_to_window(self):
        if self.current_image is None:
            return

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()

        width_ratio = canvas_width / self.current_image.width
        height_ratio = canvas_height / self.current_image.height

        self.zoom_level = min(width_ratio, height_ratio) * 0.95
        self.display_image()

    def reprocess_image(self):
        self.process_and_display()

    def save_image(self):
        if self.current_image is None:
            messagebox.showwarning("Advertencia", "No hay imagen para guardar")
            return

        filename = filedialog.asksaveasfilename(
            title="Guardar imagen",
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("JPEG files", "*.jpg"), ("All files", "*.*")]
        )

        if filename:
            try:
                self.current_image.save(filename)
                messagebox.showinfo("Guardado", f"Imagen guardada en:\n{filename}")
            except Exception as e:
                messagebox.showerror("Error", f"Error al guardar:\n{str(e)}")


def main():
    root = tk.Tk()
    app = IMX662Viewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
