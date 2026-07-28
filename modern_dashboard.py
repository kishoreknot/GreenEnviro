#!/usr/bin/env python3
"""
H2 / Multi-Gas Detector Dashboard
RS485 Modbus-RTU - Dynamic device scanning - Glassmorphism UI

Scan command  : [addr, 0x03, 0xA0, 0x29, 0x00, 0x11, CRC_LO, CRC_HI]
Response      : [addr, 0x03, 0x22, <34 data bytes>, CRC_LO, CRC_HI]
"""

import struct
import threading
import time
import random
import datetime
import logging
import os
import bisect
import collections
import csv
import io
import weakref
import customtkinter as ctk
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates
# Suppress matplotlib.font_manager INFO/DEBUG logs
import logging
logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)

# Database layer
DB_IMPORT_ERROR = ""
try:
    from db_schema import initialise_schema
    import db_repository as _db_repo

    # Core DB API used by runtime dashboard and logs flow.
    _required_db_symbols = [
        "upsert_device",
        "get_default_plant_id",
        "insert_reading",
        "get_device_name",
        "rename_device",
        "get_all_devices",
        "get_latest_reading",
        "get_all_live_readings",
        "get_all_latest_transaction_readings",
        "get_readings_in_range",
    ]
    _missing = [name for name in _required_db_symbols if not hasattr(_db_repo, name)]
    if _missing:
        raise ImportError(f"db_repository missing required symbols: {', '.join(_missing)}")

    upsert_device = _db_repo.upsert_device
    get_default_plant_id = _db_repo.get_default_plant_id
    insert_reading = _db_repo.insert_reading
    get_device_name = _db_repo.get_device_name
    rename_device = _db_repo.rename_device
    get_all_devices = _db_repo.get_all_devices
    get_latest_reading = _db_repo.get_latest_reading
    get_all_live_readings = _db_repo.get_all_live_readings
    get_all_latest_transaction_readings = _db_repo.get_all_latest_transaction_readings
    get_readings_in_range = _db_repo.get_readings_in_range

    # Optional DB API (settings/report features) with safe fallbacks.
    get_all_live_cache_latest = getattr(_db_repo, "get_all_live_cache_latest", _db_repo.get_all_live_readings)

    _get_live_cache_recent_impl = getattr(_db_repo, "get_live_cache_recent", None)
    if _get_live_cache_recent_impl is None:
        def get_live_cache_recent(device_id: int, limit: int = 60) -> list[dict]:
            row = _db_repo.get_live_reading(device_id) if hasattr(_db_repo, "get_live_reading") else None
            return [row] if row else []
    else:
        get_live_cache_recent = _get_live_cache_recent_impl

    def _fallback_get_k_factor_rules() -> list[dict]:
        return []

    def _fallback_upsert_k_factor_rule(device_id: int, k_factor: float, is_enabled: bool = True):
        raise RuntimeError("K-factor rules API unavailable in this build")

    def _fallback_delete_k_factor_rule(rule_id: int):
        raise RuntimeError("K-factor rules API unavailable in this build")

    def _fallback_get_k_factor_for_device_address(device_address: int) -> float:
        return 1.0

    get_k_factor_rules = getattr(_db_repo, "get_k_factor_rules", _fallback_get_k_factor_rules)
    upsert_k_factor_rule = getattr(_db_repo, "upsert_k_factor_rule", _fallback_upsert_k_factor_rule)
    delete_k_factor_rule = getattr(_db_repo, "delete_k_factor_rule", _fallback_delete_k_factor_rule)
    get_k_factor_for_device_address = getattr(
        _db_repo,
        "get_k_factor_for_device_address",
        _fallback_get_k_factor_for_device_address,
    )

    from db_connection import close_connection
    DB_AVAILABLE = True
except Exception as _db_import_err:
    DB_AVAILABLE = False
    DB_IMPORT_ERROR = str(_db_import_err)
    # Fall back gracefully — dashboard still works without DB
    import logging as _log
    _log.getLogger(__name__).warning(
        "Database modules unavailable: %s", _db_import_err)

# Authentication layer
try:
    from auth import init_auth_db as _init_auth_db, User as AuthUser
    _init_auth_db()   # ensure DB + default admin exist on every launch
    AUTH_AVAILABLE = True
except Exception as _auth_import_err:
    AUTH_AVAILABLE = False
    AuthUser = None
    import logging as _log2
    _log2.getLogger(__name__).warning(
        "Auth module unavailable: %s", _auth_import_err)

# Alert manager
try:
    from alert_manager import (
        init_alert_db as _init_alert_db,
        get_alert_rules, add_alert, delete_alert_rule, delete_alert_email,
        update_rule_occurrence, get_smtp_config, save_smtp_config,
        check_and_fire, log_journal, get_journal,
        get_report_schedulers, add_report_scheduler,
        add_report_scheduler_email, delete_report_scheduler_email,
        delete_report_scheduler, send_report,
        build_device_logs_report_pdf_bytes, _valid_email,
    )
    _init_alert_db()
    ALERT_AVAILABLE = True
except Exception as _alert_import_err:
    ALERT_AVAILABLE = False
    import logging as _log3
    _log3.getLogger(__name__).warning(
        "Alert manager unavailable: %s", _alert_import_err)

logger = logging.getLogger(__name__)

# Optional real-serial import (graceful fallback)
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

# -- Font Setup (Inter via Windows ctypes if font file present; fallback Segoe UI) --
_FONT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "fonts", "Inter-Regular.ttf"
)
try:
    if os.path.isfile(_FONT_FILE):
        import ctypes
        ctypes.windll.gdi32.AddFontResourceExW(_FONT_FILE, 0x10, None)
        UI_FONT = "Inter"
    else:
        UI_FONT = "Segoe UI"
except Exception:
    UI_FONT = "Segoe UI"


# -- Icon Generator (thin-stroke PNG via Pillow, no external files needed) ----
def _make_icon(name: str, size: int = 44) -> "ctk.CTkImage | None":
    """Return a CTkImage drawn with Pillow.  Returns None on any error."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        lw  = max(2, size // 18)        # thin stroke weight
        s   = size
        ink = "#1E1B4B"                 # dark indigo — matches CLR_TITLE

        if name == "chart":
            # Three thin bars with increasing height + thin baseline
            bars = [(0.12, 0.56, 0.30, 0.87),
                    (0.38, 0.36, 0.56, 0.87),
                    (0.64, 0.16, 0.82, 0.87)]
            for x1r, y1r, x2r, y2r in bars:
                d.rectangle(
                    [int(x1r*s), int(y1r*s), int(x2r*s)-1, int(y2r*s)],
                    outline=ink, width=lw)
            d.line([(int(0.08*s), int(0.90*s)),
                    (int(0.92*s), int(0.90*s))], fill=ink, width=lw)

        elif name == "sensor":
            # Concentric radio-wave arcs  + dot at base
            cx = s // 2
            for r in [s // 5, s // 3, s // 2 - lw * 2]:
                d.arc([cx - r, s // 2 - r, cx + r, s // 2 + r],
                      start=205, end=335, fill=ink, width=lw)
            d.ellipse([cx - lw*2, s//2 + s//7,
                       cx + lw*2, s//2 + s//7 + lw*3], fill=ink)

        elif name == "expand":
            # Thin expand-arrows at all four corners
            m, e = int(s * 0.18), int(s * 0.82)
            al   = int(s * 0.22)
            segs = [
                ((m, m), (m + al, m)), ((m, m), (m, m + al)),
                ((e, m), (e - al, m)), ((e, m), (e, m + al)),
                ((m, e), (m + al, e)), ((m, e), (m, e - al)),
                ((e, e), (e - al, e)), ((e, e), (e, e - al)),
            ]
            for (ax_, ay_), (bx, by) in segs:
                d.line([(ax_, ay_), (bx, by)], fill=ink, width=lw)

        elif name == "dashboard":
            # 2×2 grid of rounded squares
            gap  = int(s * 0.10)
            cell = (s - 3 * gap) // 2
            for row in range(2):
                for col in range(2):
                    x0 = gap + col * (cell + gap)
                    y0 = gap + row * (cell + gap)
                    d.rectangle([x0, y0, x0 + cell, y0 + cell],
                                outline=ink, width=lw)

        elif name == "analytics":
            # Polyline trend + dots
            import math
            pts = [(0.08, 0.75), (0.28, 0.48), (0.50, 0.62),
                   (0.72, 0.22), (0.92, 0.38)]
            sp  = [(int(x * s), int(y * s)) for x, y in pts]
            d.line(sp, fill=ink, width=lw)
            dr = max(2, lw)
            for px, py in sp:
                d.ellipse([px - dr, py - dr, px + dr, py + dr], fill=ink)
            # Baseline
            d.line([(int(0.06*s), int(0.88*s)),
                    (int(0.94*s), int(0.88*s))], fill=ink, width=lw)

        elif name == "settings":
            # Simplified gear: outer + inner ring + 6 teeth
            import math
            cx, cy   = s // 2, s // 2
            r_outer  = int(s * 0.36)
            r_inner  = int(s * 0.20)
            r_tooth  = int(s * 0.46)
            tooth_w  = int(s * 0.08)
            d.ellipse([cx-r_outer, cy-r_outer, cx+r_outer, cy+r_outer],
                       outline=ink, width=lw)
            d.ellipse([cx-r_inner, cy-r_inner, cx+r_inner, cy+r_inner],
                       outline=ink, width=lw)
            for i in range(6):
                angle = math.radians(i * 60)
                tx = cx + int(r_tooth * math.cos(angle))
                ty = cy + int(r_tooth * math.sin(angle))
                ox = cx + int(r_outer * math.cos(angle))
                oy = cy + int(r_outer * math.sin(angle))
                d.line([(ox, oy), (tx, ty)], fill=ink, width=lw + 1)

        elif name == "journal":
            # Notebook: outer page rect + left spine line + 3 text lines
            px0, py0 = int(s * 0.15), int(s * 0.08)
            px1, py1 = int(s * 0.85), int(s * 0.92)
            d.rectangle([px0, py0, px1, py1], outline=ink, width=lw)
            # Spine / binding line
            sx = int(s * 0.30)
            d.line([(sx, py0 + lw), (sx, py1 - lw)], fill=ink, width=lw)
            # Three ruled lines (entries)
            lx0, lx1 = int(s * 0.38), int(s * 0.78)
            for yr in [0.32, 0.52, 0.70]:
                ly = int(s * yr)
                d.line([(lx0, ly), (lx1, ly)], fill=ink, width=lw)

        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


def _load_icon_from_assets(name: str, size: int = 18) -> "ctk.CTkImage | None":
    """Load sidebar icon from assets/icons folder. Returns None on any error."""
    try:
        from PIL import Image
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_map = {
            "dashboard": "Dashboard.png",
            "analytics": "analytics.png",
            "settings": "settings.png",
            "journal": "journal.png",
        }
        icon_file = file_map.get(name)
        if not icon_file:
            return None

        icon_path = os.path.join(base_dir, "assets", "icons", icon_file)
        if not os.path.isfile(icon_path):
            return None

        img = Image.open(icon_path).convert("RGBA")
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


# -- Colour Palette -----------------------------------------------------------
BG_APP          = "#C3E1F2"
BG_CARD         = "#FAFAFF"   # online device tile
BG_CARD_OFFLINE = "#DDE1E7"   # offline device tile — muted grey
BG_HEADER       = "#FAFAFF"
BG_PILL      = "#9CB1F6"
CLR_SAFE     = "#16A34A"
CLR_WARN     = "#D97706"
CLR_CRIT     = "#DC2626"
CLR_OFFLINE  = "#9CA3AF"
CLR_INVALID  = "#6B7280"
CLR_LABEL    = "#6B7280"
CLR_TITLE    = "#1E1B4B"
CLR_CARD_BDR = "#708DFF"

FONT_TITLE   = (UI_FONT, 17, "bold")   # bold + slightly smaller → cleaner header
FONT_SUB     = (UI_FONT, 10)
FONT_ZONE    = (UI_FONT, 12, "bold")
FONT_LABEL   = (UI_FONT, 10)
FONT_INPUT   = (UI_FONT, 12)
FONT_FILTER_LABEL = (UI_FONT, 10, "bold")
FONT_VALUE   = (UI_FONT, 21, "bold")
FONT_UNIT    = (UI_FONT, 12)
FONT_PILL_H  = (UI_FONT, 9)
FONT_PILL_V  = (UI_FONT, 11, "bold")
FONT_STATUS  = (UI_FONT, 10)
FONT_CLOCK   = (UI_FONT, 12, "bold")

HISTORY_LEN = 40
SCAN_TIMEOUT = 0.15   # seconds per address
MAX_COLS     = 3

# -- Gas type / unit lookup tables --------------------------------------------
GAS_TYPE_MAP = {
    0x0001: "CO",    0x0002: "H2S",  0x0003: "O2",   0x0004: "LEL",
    0x0005: "CO2",   0x0006: "NH3",  0x0007: "H2",  0x0008: "Cl2",
    0x0009: "NO2",   0x000A: "SO2",  0x000B: "NO",   0x000C: "HF",
    0x001D: "VOC",    0x002A: "CH4",  0x002B: "C3H8", 0x002C: "C4H10",
}
GAS_UNIT_MAP = {0: "ppm", 1: "%LEL", 2: "%VOL", 3: "mg/m3", 4: "%"}
ALARM_STATUS_MAP = {
    0: ("Invalid",    CLR_INVALID),
    1: ("* Normal",   CLR_SAFE),
    2: ("^ Low Alarm",  CLR_WARN),
    3: ("^ High Alarm", CLR_CRIT),
}


# =============================================================================
# CRC & Protocol Helpers
# =============================================================================
def crc16_modbus(data: bytes) -> int:
    """Return Modbus CRC-16 (little-endian, poly 0xA001)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 0x0001) else crc >> 1
    return crc


def build_scan_command(address: int) -> bytes:
    """Build the 8-byte probe command for a given device address."""
    payload = bytes([address, 0x03, 0xA0, 0x29, 0x00, 0x11])
    crc     = crc16_modbus(payload)
    return payload + struct.pack("<H", crc)   # CRC low byte first


def parse_device_response(raw: bytes):
    """
    Parse a 39-byte device response.
    Returns a dict with decoded fields, or None if invalid / CRC mismatch.
    """
    if len(raw) < 39:
        return None
    if raw[1] != 0x03 or raw[2] != 0x22:     # func=03, length=34
        return None
    crc_recv = struct.unpack_from("<H", raw, 37)[0]
    if crc_recv != crc16_modbus(raw[:37]):
        return None

    d = raw[3:37]                              # 34 payload bytes
    gas_type   = struct.unpack_from(">H", d,  0)[0]
    gas_unit   = struct.unpack_from(">H", d,  2)[0]
    decimal_pl = struct.unpack_from(">H", d,  4)[0]
    dev_range  = struct.unpack_from(">I", d,  6)[0]
    conc       = struct.unpack_from(">I", d, 10)[0]
    low_alarm  = struct.unpack_from(">I", d, 14)[0]
    high_alarm = struct.unpack_from(">I", d, 18)[0]
    stel_alarm = struct.unpack_from(">I", d, 22)[0]
    twa_alarm  = struct.unpack_from(">I", d, 26)[0]
    alarm_st   = struct.unpack_from(">H", d, 30)[0]
    adc_val    = struct.unpack_from(">H", d, 32)[0]

    div = 10 ** decimal_pl if decimal_pl <= 4 else 1
    return {
        "address":       raw[0],
        "gas_type":      gas_type,
        "gas_name":      GAS_TYPE_MAP.get(gas_type, f"Gas-{gas_type:04X}"),
        "gas_unit":      "ppm",
        "decimal_pl":    decimal_pl,
        "device_range":  round(dev_range  / div, decimal_pl),
        "concentration": round(conc       / div, decimal_pl),
        "low_alarm":     round(low_alarm  / div, decimal_pl),
        "high_alarm":    round(high_alarm / div, decimal_pl),
        "stel_alarm":    round(stel_alarm / div, decimal_pl),
        "twa_alarm":     round(twa_alarm  / div, decimal_pl),
        "alarm_status":  alarm_st,
        "adc_value":     adc_val,
        "online":        True,
        "timestamp":     time.time(),
    }


# =============================================================================
# Mock Scanner  (no hardware needed)
# =============================================================================
class MockScanner:
    """
    Simulates RS485 device detection.
    Devices are present at MOCK_ADDRESSES with randomised gas types.
    """
    MOCK_ADDRESSES = [1, 2, 3, 4, 5, 6, 7, 8]

    def __init__(self):
        self._base_conc   = {a: random.uniform(5, 80)
                             for a in self.MOCK_ADDRESSES}
        self._gas_types   = {a: 0x0029                  # H2 for all mock devices
                             for a in self.MOCK_ADDRESSES}
        self._alarm_seeds = {}
        for a in self.MOCK_ADDRESSES:
            lo = round(random.uniform(20, 40), 1)
            self._alarm_seeds[a] = (lo, round(lo * 2, 1))

    # -------------------------------------------------------------------------
    def scan(self, on_progress=None):
        """Scan addresses 1-250, return list of parsed device dicts."""
        found = []
        total = 250
        for addr in range(1, total + 1):
            if on_progress:
                on_progress(addr, total)
            time.sleep(0.005)                      # simulate bus latency
            if addr in self.MOCK_ADDRESSES:
                c_init = round(self._base_conc[addr], 1)
                lo, hi = self._alarm_seeds[addr]
                alarm_init = 3 if c_init >= hi else (2 if c_init >= lo else 1)
                raw  = self._make_fake_response(addr,
                                                concentration=c_init,
                                                alarm_status=alarm_init)
                info = parse_device_response(raw)
                if info:
                    found.append(info)
        return found

    def poll_device(self, address: int):
        """Return a fresh reading for an already-detected device."""
        if address not in self.MOCK_ADDRESSES:
            return None
        lo, hi  = self._alarm_seeds[address]
        new_val = self._base_conc[address] + random.uniform(-2.0, 2.0)
        new_val = max(0.0, min(100.0, new_val))
        self._base_conc[address] = new_val
        c = round(new_val, 1)
        alarm = 3 if c >= hi else (2 if c >= lo else 1)
        raw  = self._make_fake_response(address, concentration=c,
                                        alarm_status=alarm)
        return parse_device_response(raw)

    # -------------------------------------------------------------------------
    def _make_fake_response(self, address: int,
                            concentration=None, alarm_status: int = 1) -> bytes:
        lo, hi = self._alarm_seeds[address]
        c  = concentration if concentration is not None \
             else self._base_conc[address]
        gt = self._gas_types[address]
        dec, div = 1, 10
        payload = (
            struct.pack(">H", gt)          +   # gas type
            struct.pack(">H", 0)           +   # gas unit ppm
            struct.pack(">H", dec)         +   # decimal places
            struct.pack(">I", 100 * div)   +   # range = 100.0
            struct.pack(">I", int(c   * div))  +   # concentration
            struct.pack(">I", int(lo  * div))  +   # low alarm
            struct.pack(">I", int(hi  * div))  +   # high alarm
            struct.pack(">I", 0)           +   # stel
            struct.pack(">I", 0)           +   # twa
            struct.pack(">H", alarm_status)+   # alarm status
            struct.pack(">H", 0x0218)          # adc placeholder
        )
        header = bytes([address, 0x03, 0x22])
        full   = header + payload
        return full + struct.pack("<H", crc16_modbus(full))


# =============================================================================
# Real RS485 Scanner  (requires pyserial)
# =============================================================================
class RS485Scanner:
    """Scans a real RS485 bus via a serial port."""

    def __init__(self, port: str, baud: int = 9600):
        self._port = port
        self._baud = baud
        self._ser  = None

    def open(self):
        self._ser = serial.Serial(
            port=self._port, baudrate=self._baud,
            bytesize=8, parity="N", stopbits=1,
            timeout=SCAN_TIMEOUT,
        )

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def scan(self, on_progress=None):
        found = []
        for addr in range(1, 21):
            if on_progress:
                on_progress(addr, 20)
            cmd = build_scan_command(addr)
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                raw = self._ser.read(39)
                if len(raw) == 39 and raw[0] == addr:
                    info = parse_device_response(raw)
                    if info:
                        found.append(info)
            except Exception:
                pass
        return found

    def poll_device(self, address: int):
        cmd = build_scan_command(address)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(cmd)
            raw = self._ser.read(39)
            if len(raw) == 39 and raw[0] == address:
                return parse_device_response(raw)
        except Exception:
            pass
        return None


# =============================================================================
# Polling Engine  (background thread)
# =============================================================================
class PollingEngine:
    def __init__(self, scanner, addresses, callback, interval: float = 2.0):
        self._scanner   = scanner
        self._addresses = addresses
        self._callback  = callback
        self._interval  = interval
        self._stop      = threading.Event()
        self._thread    = threading.Thread(target=self._loop, daemon=True)

    def start(self): self._thread.start()
    def stop(self):  self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            results = []
            for addr in self._addresses:
                info = self._scanner.poll_device(addr)
                results.append(info if info else
                               {"address": addr, "online": False})
            self._callback(results)
            self._stop.wait(self._interval)


# =============================================================================
# Connection / Scan Dialog
# =============================================================================
class ConnectionDialog(ctk.CTkToplevel):
    """
    Shown on startup. User picks Mock / RS485 mode, then scans for devices.
    Calls on_complete(devices, scanner) when done.
    """

    def __init__(self, parent, on_complete):
        super().__init__(parent)
        self._on_complete = on_complete
        self._scanner     = None

        self.title("Connect to Controller")
        self.geometry("400x300")
        self.resizable(True, False)
        self.grab_set()
        self.configure(fg_color=BG_CARD)
        self.after(50, self._center)
        self._build()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        PAD = 24
        ctk.CTkLabel(self, text="H2 Gas Detector Dashboard",
                     font=FONT_TITLE, text_color=CLR_TITLE,
                     fg_color="transparent").pack(pady=(PAD, 2))
        ctk.CTkLabel(self, text="Configure connection, then scan for devices",
                     font=FONT_SUB, text_color=CLR_LABEL,
                     fg_color="transparent").pack(pady=(0, 14))

        # Mode selector
        mf = ctk.CTkFrame(self, fg_color=BG_PILL, corner_radius=12)
        mf.pack(fill="x", padx=PAD)
        ctk.CTkLabel(mf, text="Connection Mode", font=FONT_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(anchor="w", padx=14, pady=(10, 2))
        self._mode_var = ctk.StringVar(value="Mock")
        ctk.CTkSegmentedButton(mf, values=["Mock", "RS485"],
                               variable=self._mode_var,
                               command=self._on_mode_change,
                               font=FONT_LABEL).pack(
                               padx=14, pady=(0, 12), fill="x")

        # RS485 settings frame
        self._rs485_frame = ctk.CTkFrame(self, fg_color=BG_PILL, corner_radius=12)
        ports = ([p.device for p in serial.tools.list_ports.comports()]
                 if SERIAL_AVAILABLE else [])
        self._port_var = ctk.StringVar(value=ports[0] if ports else "COM1")
        self._baud_var = ctk.StringVar(value="9600")

        ctk.CTkLabel(self._rs485_frame, text="COM Port", font=FONT_LABEL,
                     text_color=CLR_LABEL, fg_color="transparent").grid(
                     row=0, column=0, sticky="w", padx=14, pady=(10, 2))
        ctk.CTkOptionMenu(self._rs485_frame, values=ports or ["COM1"],
                          variable=self._port_var,
                          font=FONT_LABEL).grid(
                          row=0, column=1, sticky="ew", padx=(0, 14), pady=(10, 2))

        ctk.CTkLabel(self._rs485_frame, text="Baud Rate", font=FONT_LABEL,
                     text_color=CLR_LABEL, fg_color="transparent").grid(
                     row=1, column=0, sticky="w", padx=14, pady=(4, 10))
        ctk.CTkOptionMenu(self._rs485_frame,
                          values=["2400", "4800", "9600", "19200", "38400"],
                          variable=self._baud_var,
                          font=FONT_LABEL).grid(
                          row=1, column=1, sticky="ew", padx=(0, 14), pady=(4, 10))
        self._rs485_frame.columnconfigure(1, weight=1)
        # hidden by default

        # Progress
        self._prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._prog_frame.pack(fill="x", padx=PAD, pady=(12, 0))
        self._prog_bar = ctk.CTkProgressBar(self._prog_frame, height=10,
                                            corner_radius=5)
        self._prog_bar.pack(fill="x")
        self._prog_bar.set(0)
        self._prog_lbl = ctk.CTkLabel(self._prog_frame,
                                      text="Ready to scan  (addresses 1 - 8)",
                                      font=FONT_LABEL, text_color=CLR_LABEL,
                                      fg_color="transparent")
        self._prog_lbl.pack(pady=(4, 0))

        # Scan button
        self._scan_btn = ctk.CTkButton(
            self, text="Scan Devices",
            font=("Segoe UI", 13, "bold"), height=44, corner_radius=12,
            fg_color=CLR_TITLE, hover_color="#312E81",
            command=self._start_scan)
        self._scan_btn.pack(fill="x", padx=PAD, pady=(18, PAD))

    def _on_mode_change(self, value):
        if value == "RS485":
            self._rs485_frame.pack(fill="x", padx=24, pady=(10, 0),
                                   before=self._prog_frame)
        else:
            self._rs485_frame.pack_forget()

    def _start_scan(self):
        self._scan_btn.configure(state="disabled", text="Scanning...")
        self._prog_bar.set(0)
        self._prog_lbl.configure(text="Initialising...", text_color=CLR_LABEL)

        mode = self._mode_var.get()
        if mode == "Mock":
            self._scanner = MockScanner()
            # return
        else:
            if not SERIAL_AVAILABLE:
                self._prog_lbl.configure(
                    text="pyserial not installed. Switch to Mock mode.",
                    text_color=CLR_CRIT)
                self._scan_btn.configure(state="normal", text="Scan Devices")
                return
            try:
                sc = RS485Scanner(self._port_var.get(),
                                  int(self._baud_var.get()))
                sc.open()
                self._scanner = sc
            except Exception as e:
                self._prog_lbl.configure(text=f"Port error: {e}",
                                         text_color=CLR_CRIT)
                self._scan_btn.configure(state="normal", text="Scan Devices")
                return

        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        def on_progress(current, total):
            self.after(0, self._prog_bar.set, current / total)
            self.after(0, self._prog_lbl.configure,
                       {"text": f"Scanning address {current} / {total}...",
                        "text_color": CLR_LABEL})

        found = self._scanner.scan(on_progress=on_progress)
        self.after(0, self._scan_done, found)

    def _scan_done(self, found):
        n = len(found)
        if n == 0:
            self._prog_lbl.configure(
                text="No devices found. Check wiring or use Mock mode.",
                text_color=CLR_CRIT)
            self._scan_btn.configure(state="normal", text="Scan Devices")
            return
        if n == 1:
            self._prog_lbl.configure(
                text="Found 1 device — opening dashboard...",
                text_color=CLR_SAFE)
        else:
            self._prog_lbl.configure(
                text=f"Found {n} devices — opening dashboard...",
                text_color=CLR_SAFE)
        self.after(600, lambda: (self.grab_release(),
                                 self.destroy(),
                                 self._on_complete(found, self._scanner)))


# =============================================================================
# Expand Modal
# =============================================================================
# class ExpandModal(ctk.CTkToplevel):
#     def __init__(self, parent, address, gas_name, history, color, tint):
#         super().__init__(parent)
#         self.title(f"Device {address:03d}  -  {gas_name} History")
#         self.geometry("860x480")
#         self.configure(fg_color=BG_CARD)
#         self.grab_set()
#         self.after(100, self.lift)

#         fig = Figure(figsize=(8, 3.2), dpi=96)
#         fig.patch.set_facecolor(tint)
#         ax  = fig.add_subplot(111)
#         ax.set_facecolor(tint)
#         xs  = list(range(len(history)))
#         ax.plot(xs, history, color=color, linewidth=2, solid_capstyle="round")
#         ax.fill_between(xs, history, alpha=0.18, color=color)
#         ax.set_xlabel("Samples (oldest -> newest)", fontsize=10, color=CLR_LABEL)
#         ax.set_ylabel("Concentration", fontsize=10, color=CLR_LABEL)
#         ax.tick_params(colors=CLR_LABEL)
#         for sp in ax.spines.values():
#             sp.set_color(CLR_CARD_BDR)
#         fig.subplots_adjust(left=0.07, right=0.97, top=0.92, bottom=0.15)
#         c = FigureCanvasTkAgg(fig, master=self)
#         c.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=16)
#         c.draw()


# =============================================================================
# Sensor Card
# =============================================================================
class SensorCard(ctk.CTkFrame):
    """Shadow-wrapper + inner card per detected device."""

    # PCB-prescribed standard / permissible limit (shared across all cards)
    _std_value: float  = 25.0    # default — overridden from Settings
    _std_unit:  str    = "ppm"
    _instances: "weakref.WeakSet" = None  # populated on first instantiation

    @classmethod
    def _ensure_registry(cls):
        if cls._instances is None:
            cls._instances = weakref.WeakSet()

    @classmethod
    def update_all_std(cls, value: float, unit: str = ""):
        """Push a new standard value to every live card."""
        cls._std_value = value
        if unit:
            cls._std_unit = unit
        if cls._instances:
            for card in list(cls._instances):
                card._refresh_std_pill()

    def __init__(self, parent, info: dict, actor: str = "system", on_rename=None):
        self._online    = info.get("online", True)
        self._on_rename = on_rename
        # Outer shadow ring — use BG_APP colour so corner bleed is invisible
        super().__init__(parent, fg_color=BG_APP, corner_radius=22,
                         border_width=0)
        # Inner card — muted grey background for offline devices
        _card_bg = BG_CARD if self._online else BG_CARD_OFFLINE
        self._card = ctk.CTkFrame(self, fg_color=_card_bg, corner_radius=20,
                                   border_width=0)
        self._prev_online_state = self._online  # Track transitions for visual updates
        self._card.pack(fill="both", expand=True, padx=3, pady=3)

        SensorCard._ensure_registry()
        SensorCard._instances.add(self)
        self._address  = info["address"]
        self._actor    = actor
        self._dev_name = info.get("device_name", f"Device {info['address']:03d}")
        self._gas_name = info["gas_name"]
        self._gas_unit = info["gas_unit"]
        self._history  = [info["concentration"]] * HISTORY_LEN
        self._build(info)

    # -- colour helpers -------------------------------------------------------
    @staticmethod
    def _alarm_color(alarm_status: int, online: bool) -> str:
        if not online:
            return CLR_OFFLINE
        return {1: CLR_SAFE, 2: CLR_WARN, 3: CLR_CRIT}.get(alarm_status,
                                                              CLR_INVALID)

    @staticmethod
    def _tint(clr: str) -> str:
        return {CLR_SAFE:    "#DCFCE7",
                CLR_WARN:    "#FEF3C7",
                CLR_CRIT:    "#FEE2E2",
                CLR_OFFLINE: "#F3F4F6",
                CLR_INVALID: "#F9FAFB"}.get(clr, "#EEF2FF")

    @staticmethod
    def _fmt_stat(value) -> str:
        if value is None:
            return "--"
        try:
            return f"{float(value):.2f}"
        except Exception:
            return "--"

    # -- build ----------------------------------------------------------------
    def _build(self, info: dict):
        PAD = 10
        # All children are placed on self._card (the white inner frame)
        self._card.grid_columnconfigure(0, weight=1)

        # Row 0: Device label | gas badge | spacer | Std pill
        top = ctk.CTkFrame(self._card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
        top.columnconfigure(2, weight=1)

        # Name wrapper: label + pencil icon, swaps to entry on edit
        name_wrap = ctk.CTkFrame(top, fg_color="transparent")
        name_wrap.grid(row=0, column=0, sticky="w")
        name_row = ctk.CTkFrame(name_wrap, fg_color="transparent")
        name_row.pack(side="top", anchor="w")
        self._name_lbl = ctk.CTkLabel(name_row, text=self._dev_name,
                                      font=FONT_ZONE, text_color=CLR_TITLE,
                                      fg_color="transparent")
        self._name_lbl.pack(side="left")
        self._edit_btn = ctk.CTkButton(
            name_row, text="\u270f", width=20, height=20, corner_radius=4,
            font=(UI_FONT, 11), fg_color="transparent", text_color="#94A3B8",
            hover_color="#E2E8F0", command=self._start_name_edit)
        self._edit_btn.pack(side="left", padx=(3, 0))
        # Edit-mode widgets (hidden initially)
        self._name_entry = ctk.CTkEntry(
            name_row, width=120, height=26, corner_radius=6,
            font=FONT_ZONE, border_color="#94A3B8")
        self._confirm_btn = ctk.CTkButton(
            name_row, text="\u2713", width=24, height=24, corner_radius=4,
            font=(UI_FONT, 10, "bold"),
            fg_color=CLR_SAFE, text_color="#FFFFFF", hover_color="#16A34A",
            command=self._confirm_name_edit)
        self._cancel_btn = ctk.CTkButton(
            name_row, text="\u00d7", width=24, height=24, corner_radius=4,
            font=(UI_FONT, 10),
            fg_color="#FEE2E2", text_color=CLR_CRIT, hover_color="#FECACA",
            command=self._cancel_name_edit)
        self._name_err_lbl = ctk.CTkLabel(
            name_wrap, text="", fg_color="transparent",
            text_color=CLR_CRIT, font=(UI_FONT, 8))

        ctk.CTkLabel(top, text=self._gas_name, font=FONT_PILL_H,
                     text_color=CLR_LABEL, fg_color=BG_PILL,
                     corner_radius=6, width=50, height=20).grid(
                     row=0, column=1, padx=(6, 0), sticky="w")

        # Std pill — value from PCB standard, updatable from Settings
        # GEnvCSTM-AMARRAJA Commented below to hide PCB standard capsule from Smart card
        # self._std_pill_lbl = ctk.CTkLabel(
        #     top,
        #     text=f"PCB Standard: {SensorCard._std_value} ", #{SensorCard._std_unit}",
        #     font=FONT_PILL_H,
        #     text_color="#1D4ED8",
        #     fg_color="#DBEAFE",
        #     corner_radius=6,
        #     width=80, height=20)
        # self._std_pill_lbl.grid(row=0, column=3, sticky="e")

        # Row 1: sub label
        # ctk.CTkLabel(self._card, text="Concentration",
        #              font=FONT_LABEL, text_color=CLR_LABEL,
        #              fg_color="transparent").grid(
        #              row=1, column=0, sticky="w", padx=PAD, pady=(4, 0))

        # Row 2: value box
        clr  = self._alarm_color(info["alarm_status"], info.get("online", True))
        tint = self._tint(clr)

        self._val_frame = ctk.CTkFrame(self._card, fg_color=tint,
                                       corner_radius=12,
                                       border_width=0)
        self._val_frame.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(4, 0))
        self._val_frame.columnconfigure(0, weight=1)

        vrow = ctk.CTkFrame(self._val_frame, fg_color="transparent")
        vrow.grid(row=0, column=0, sticky="w", padx=10, pady=6)

        val_color = CLR_OFFLINE if not info.get("online", True) else clr
        self._val_lbl = ctk.CTkLabel(vrow, text=f"{info['concentration']}",
                                     font=FONT_VALUE, text_color=val_color,
                                     fg_color="transparent")
        self._val_lbl.pack(side="left")
        self._unit_lbl = ctk.CTkLabel(vrow, text=f" {self._gas_unit}",
                                      font=FONT_UNIT, text_color=CLR_LABEL,
                                      fg_color="transparent")
        self._unit_lbl.pack(side="left", pady=(6, 0))

        # Row 3: 1-minute stats (Avg / Min / Max)
        stats_row = ctk.CTkFrame(self._card, fg_color="transparent")
        stats_row.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(6, 0))
        stats_row.columnconfigure((0, 1, 2), weight=1)

        self._avg_1m_lbl = ctk.CTkLabel(
            stats_row,
            text="Avg(1m): --",
            font=(UI_FONT, 9, "bold"),
            text_color=CLR_TITLE,
            fg_color="#E2E8F0",
            corner_radius=6,
            height=24,
        )
        self._avg_1m_lbl.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self._min_1m_lbl = ctk.CTkLabel(
            stats_row,
            text="Min(1m): --",
            font=(UI_FONT, 9, "bold"),
            text_color=CLR_TITLE,
            fg_color="#E2E8F0",
            corner_radius=6,
            height=24,
        )
        self._min_1m_lbl.grid(row=0, column=1, sticky="ew", padx=3)

        self._max_1m_lbl = ctk.CTkLabel(
            stats_row,
            text="Max(1m): --",
            font=(UI_FONT, 9, "bold"),
            text_color=CLR_TITLE,
            fg_color="#E2E8F0",
            corner_radius=6,
            height=24,
        )
        self._max_1m_lbl.grid(row=0, column=2, sticky="ew", padx=(3, 0))

        # Row 3: sparkline (mini trend graph)
        # self._spark_frame = ctk.CTkFrame(self._card, fg_color=tint,
        #                                  corner_radius=10)
        # self._spark_frame.grid(row=3, column=0, sticky="ew",
        #                         padx=PAD, pady=(4, 0))
        # self._spark_frame.columnconfigure(0, weight=1)

        # self._fig = Figure(figsize=(2.8, 0.50), dpi=72)
        # self._fig.patch.set_facecolor(tint)
        # self._ax = self._fig.add_subplot(111)
        # self._ax.set_facecolor(tint)
        # for sp in self._ax.spines.values():
        #     sp.set_visible(False)
        # self._ax.set_xticks([])
        # self._ax.set_yticks([])
        # self._line, = self._ax.plot(self._history, color=clr,
        #                             linewidth=1.8, solid_capstyle="round")
        # self._fig.subplots_adjust(left=0.01, right=0.99, top=0.92, bottom=0.08)

        # self._mpl_canvas = FigureCanvasTkAgg(self._fig,
        #                                       master=self._spark_frame)
        # tk_cvs = self._mpl_canvas.get_tk_widget()
        # tk_cvs.configure(bg=tint)
        # tk_cvs.pack(fill="x", padx=4, pady=3)
        # self._mpl_canvas.draw()

        # Row 4: Low / High alarm pills
        pills = ctk.CTkFrame(self._card, fg_color="transparent")
        pills.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(6, 0))
        pills.columnconfigure((0, 1), weight=1)

        self._lo_pill = self._make_pill(
            pills, "Low Alarm", f"{info['low_alarm']} {self._gas_unit}")
        self._hi_pill = self._make_pill(
            pills, "High Alarm", f"{info['high_alarm']} {self._gas_unit}")
        self._lo_pill.grid(row=0, column=0, padx=(0, 3), sticky="ew")
        self._hi_pill.grid(row=0, column=1, padx=(3, 0), sticky="ew")

        # Row 5: status label
        st_txt, st_clr = ALARM_STATUS_MAP.get(
            info["alarm_status"], ("Unknown", CLR_INVALID))
        if not info.get("online", True):
            st_txt, st_clr = "Offline", CLR_OFFLINE

        self._status_lbl = ctk.CTkLabel(self._card, text=st_txt,
                                        font=FONT_STATUS,
                                        text_color=st_clr,
                                        fg_color="transparent",
                                        anchor="center")
        self._status_lbl.grid(row=5, column=0, sticky="ew",
                               padx=PAD, pady=(6, 1))

        # Row 6: last-updated timestamp
        # Format last_updated for display
        last_updated = info.get('last_updated')
        logging.debug(f"Raw last_updated value for device {self._address:03d}: {last_updated}") 
        if last_updated:
            try:
                # Try parsing ISO or standard datetime string
                dt = datetime.datetime.fromisoformat(str(last_updated).replace('T', ' '))
                last_updated_str = dt.strftime('%b %d, %Y %I:%M %p')
            except Exception:
                last_updated_str = str(last_updated)
        else:
            last_updated_str = 'N/Available'
        self._ts_lbl = ctk.CTkLabel(
            self._card,
            text=f"Last updated: {last_updated_str}",
            font=(UI_FONT, 11), text_color=CLR_LABEL,
            fg_color="transparent", anchor="center")
        self._ts_lbl.grid(row=6, column=0, sticky="ew",
                          padx=PAD, pady=(0, 8))

    def _make_pill(self, parent, heading: str, value: str) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, fg_color=BG_PILL, corner_radius=8, height=28)
        f.grid_propagate(False)
        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.pack(expand=True, fill="both", padx=6)
        ctk.CTkLabel(inner, text=heading, font=FONT_PILL_H,
                     text_color=CLR_LABEL, fg_color="transparent").pack(side="left")
        lbl = ctk.CTkLabel(inner, text=f"  {value}", font=(UI_FONT, 9, "bold"),
                           text_color=CLR_TITLE, fg_color="transparent")
        lbl.pack(side="left")
        f._val_lbl = lbl
        return f

    def _refresh_std_pill(self):
        """Called by update_all_std to push new std value to this card's pill."""
        try:
            self._std_pill_lbl.configure(
                text=f"Std: {SensorCard._std_value} {SensorCard._std_unit}")
        except Exception:
            pass

    def _update_minute_stats(self, info: dict):
        avg_1m = self._fmt_stat(info.get("avg_1m"))
        min_1m = self._fmt_stat(info.get("min_1m"))
        max_1m = self._fmt_stat(info.get("max_1m"))
        self._avg_1m_lbl.configure(text=f"Avg(1m): {avg_1m}")
        self._min_1m_lbl.configure(text=f"Min(1m): {min_1m}")
        self._max_1m_lbl.configure(text=f"Max(1m): {max_1m}")

    # -- name editing ---------------------------------------------------------
    def _start_name_edit(self):
        self._name_lbl.pack_forget()
        self._edit_btn.pack_forget()
        self._name_entry.delete(0, "end")
        self._name_entry.insert(0, self._dev_name)
        self._name_entry.configure(border_color="#94A3B8")
        self._name_err_lbl.configure(text="")
        self._name_err_lbl.pack_forget()
        self._name_entry.pack(side="left")
        self._confirm_btn.pack(side="left", padx=(3, 0))
        self._cancel_btn.pack(side="left", padx=(2, 0))
        self._name_entry.focus_set()
        self._name_entry.bind("<Return>", lambda e: self._confirm_name_edit())
        self._name_entry.bind("<Escape>", lambda e: self._cancel_name_edit())

    def _confirm_name_edit(self):
        new_name = self._name_entry.get().strip()
        if not new_name:
            self._cancel_name_edit()
            return
        old_name = self._dev_name
        if DB_AVAILABLE:
            try:
                rename_device(self._address, new_name)
            except ValueError as e:
                # Duplicate name — highlight entry and show inline error
                self._name_entry.configure(border_color=CLR_CRIT)
                self._name_err_lbl.configure(text=str(e))
                self._name_err_lbl.pack(side="top", anchor="w", pady=(1, 0))
                return
            except Exception:
                pass
        self._dev_name = new_name
        if self._on_rename:
            self._on_rename(self._address, new_name)
        if ALERT_AVAILABLE:
            try:
                log_journal(
                    self._actor, "Dashboard", "UPDATE",
                    f"Device {self._address:03d} renamed: '{old_name}' → '{new_name}'"
                )
            except Exception:
                pass
        self._cancel_name_edit()

    def _cancel_name_edit(self):
        self._name_entry.pack_forget()
        self._confirm_btn.pack_forget()
        self._cancel_btn.pack_forget()
        self._name_err_lbl.configure(text="")
        self._name_err_lbl.pack_forget()
        self._name_lbl.configure(text=self._dev_name)
        self._name_lbl.pack(side="left")
        self._edit_btn.pack(side="left", padx=(3, 0))

    # -- live update ----------------------------------------------------------
    def update(self, info: dict):
        online = info.get("online", True)

        self._update_minute_stats(info)

        last_updated = info.get('last_updated')
        if last_updated:
            try:
                dt = datetime.datetime.fromisoformat(str(last_updated).replace('T', ' '))
                last_updated_str = dt.strftime('%b %d, %Y %I:%M %p')
            except Exception:
                last_updated_str = str(last_updated)
        else:
            last_updated_str = 'N/A'
        self._ts_lbl.configure(text=f"Last updated: {last_updated_str}")

        if not online:
            # Update outer card background when going offline
            if self._prev_online_state != online:
                self._card.configure(fg_color=BG_CARD_OFFLINE)
                self._prev_online_state = online
            self._val_lbl.configure(text="---", text_color=CLR_OFFLINE)
            self._val_frame.configure(fg_color=self._tint(CLR_OFFLINE))
            self._status_lbl.configure(text="Offline", text_color=CLR_OFFLINE)
            return

        # Update outer card background when coming back online
        if self._prev_online_state != online:
            self._card.configure(fg_color=BG_CARD)
            self._prev_online_state = online

        conc     = info["concentration"]
        alarm_st = info["alarm_status"]
        clr      = self._alarm_color(alarm_st, True)
        tint     = self._tint(clr)
        st_txt, st_clr = ALARM_STATUS_MAP.get(alarm_st, ("Unknown", CLR_INVALID))

        # history
        self._history.append(conc)
        if len(self._history) > HISTORY_LEN:
            self._history.pop(0)

        # value box
        self._val_lbl.configure(text=f"{conc}", text_color=clr)
        self._val_frame.configure(fg_color=tint)

        # sparkline (mini trend graph)
        # self._spark_frame.configure(fg_color=tint)
        # self._fig.patch.set_facecolor(tint)
        # self._ax.set_facecolor(tint)
        # self._line.set_ydata(self._history)
        # self._line.set_color(clr)
        # self._ax.relim()
        # self._ax.autoscale_view()
        # self._mpl_canvas.get_tk_widget().configure(bg=tint)
        # self._mpl_canvas.draw_idle()

        # pills
        self._lo_pill._val_lbl.configure(
            text=f"  {info['low_alarm']} {self._gas_unit}")
        self._hi_pill._val_lbl.configure(
            text=f"  {info['high_alarm']} {self._gas_unit}")

        # status
        self._status_lbl.configure(text=st_txt, text_color=st_clr)

        # timestamp is updated at method start so it works for both online/offline


# =============================================================================
# Sidebar navigation colour constants
# =============================================================================
NAV_ACTIVE_BG   = "#0369A1"   # kept for reference; no longer fills button bg
NAV_ACTIVE_TEXT = "#0369A1"   # active item: blue font only
NAV_IDLE_TEXT   = "#64748B"   # idle items: muted slate
NAV_HOVER_BG    = "#E0E8F4"   # hover: neutral grey-blue tint
BG_SIDEBAR      = "#F0F9FF"

# Palette for multi-device trend lines
_LINE_PALETTE = [
    "#4F46E5", "#10B981", "#F59E0B", "#EF4444",
    "#8B5CF6", "#06B6D4", "#F97316", "#EC4899",
]


# =============================================================================
# Header
# =============================================================================
class DashboardHeader(ctk.CTkFrame):
    def __init__(self, parent, device_count: int, mode: str,
                 current_user=None, on_logout=None):
        super().__init__(parent, fg_color=BG_HEADER, corner_radius=10,
                         border_width=0, height=84)
        self.pack_propagate(False)
        self._on_logout = on_logout

        # Logo
        _logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "assets", "green-logo.ico")
        _logo_image = None
        try:
            from PIL import Image
            _pil_logo = Image.open(_logo_path).convert("RGBA")
            _pil_logo = _pil_logo.resize((44, 44), Image.LANCZOS)
            _logo_image = ctk.CTkImage(light_image=_pil_logo,
                                        dark_image=_pil_logo, size=(44, 44))
        except Exception:
            pass
        if _logo_image:
            ctk.CTkLabel(self, text="", image=_logo_image,
                         fg_color="transparent").pack(side="left", padx=(14, 10))
        else:
            ctk.CTkFrame(self, fg_color="transparent",
                         width=44, height=44).pack(side="left", padx=(14, 10))

        # Title
        ctk.CTkLabel(self, text="H2 Detector Dashboard",
                     font=(UI_FONT, 15, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")

        # Subtitle separator + text
        ctk.CTkLabel(self, text="  |", font=(UI_FONT, 12),
                     text_color="#CBD5E1",
                     fg_color="transparent").pack(side="left")
        ctk.CTkLabel(self, text="Real-time Monitoring & Analytics",
                     font=FONT_SUB, text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left", padx=(2, 0))

        # Right side — user chip + clock (packed right-to-left)
        # Clock
        clock_pane = ctk.CTkFrame(self, fg_color="transparent")
        clock_pane.pack(side="right", padx=(0, 14))
        self._clock = ctk.CTkLabel(clock_pane, text="", font=(UI_FONT, 11, "bold"),
                                   text_color=CLR_TITLE,
                                   fg_color="transparent")
        self._clock.pack(anchor="e")

        # Separator
        ctk.CTkFrame(self, fg_color="#E2E8F0", width=1,
                     corner_radius=0).pack(side="right", fill="y",
                                           padx=(0, 14), pady=8)

        # User chip
        if current_user is not None:
            user_pane = ctk.CTkFrame(self, fg_color="#F0F9FF", corner_radius=8)
            user_pane.pack(side="right", padx=(0, 6), pady=(0, 4))

            top_row = ctk.CTkFrame(user_pane, fg_color="transparent")
            top_row.pack(fill="x", padx=8, pady=(4, 1))
            ctk.CTkLabel(top_row,
                         text=f"●  {current_user.display_name}",
                         font=(UI_FONT, 10, "bold"),
                         text_color=NAV_ACTIVE_BG,
                         fg_color="transparent").pack(side="left", padx=(0, 4))
            ctk.CTkLabel(top_row,
                         text=current_user.role.capitalize(),
                         font=(UI_FONT, 9), text_color=CLR_LABEL,
                         fg_color="transparent").pack(side="left", padx=(0, 4))
            if on_logout:
                ctk.CTkButton(
                    top_row, text="Sign out",
                    width=62, height=22, corner_radius=6,
                    font=(UI_FONT, 9),
                    fg_color="#E2E8F0", text_color=CLR_LABEL,
                    hover_color="#CBD5E1",
                    command=on_logout,
                ).pack(side="right", padx=(8, 0))

            ctk.CTkFrame(user_pane, fg_color="#CBD5E1", height=1,
                         corner_radius=0).pack(fill="x", padx=8, pady=(0, 1))

            company_name, location = self._resolve_company_location()
            meta_row = ctk.CTkFrame(user_pane, fg_color="transparent")
            meta_row.pack(anchor="w", padx=8, pady=(1, 4))

            company_chip = ctk.CTkFrame(
                meta_row,
                fg_color="#DBEAFE",
                corner_radius=6,
                border_width=1,
                border_color="#93C5FD",
            )
            company_chip.pack(side="left")
            ctk.CTkLabel(
                company_chip,
                text="Company:",
                font=(UI_FONT, 9, "bold"),
                text_color="#1E40AF",
                fg_color="transparent",
            ).pack(side="left", padx=(7, 3), pady=3)
            ctk.CTkLabel(
                company_chip,
                text=company_name,
                font=(UI_FONT, 10, "bold"),
                text_color="#1D4ED8",
                fg_color="transparent",
            ).pack(side="left", padx=(0, 7), pady=3)

            location_chip = ctk.CTkFrame(
                meta_row,
                fg_color="#DCFCE7",
                corner_radius=6,
                border_width=1,
                border_color="#86EFAC",
            )
            location_chip.pack(side="left", padx=(6, 0))
            ctk.CTkLabel(
                location_chip,
                text="Location:",
                font=(UI_FONT, 9, "bold"),
                text_color="#166534",
                fg_color="transparent",
            ).pack(side="left", padx=(7, 3), pady=3)
            ctk.CTkLabel(
                location_chip,
                text=location,
                font=(UI_FONT, 10, "bold"),
                text_color="#15803D",
                fg_color="transparent",
            ).pack(side="left", padx=(0, 7), pady=3)

        self.update_time()

    def update_time(self):
        self._clock.configure(
            text=datetime.datetime.now().strftime("%I:%M:%S %p"))

    def _resolve_company_location(self) -> tuple[str, str]:
        company_name = "Company"
        location = "Location"
        try:
            from db_repository import get_all_plants
            plants = get_all_plants()
            if plants:
                plant = plants[0] or {}
                company_name = str(plant.get("company_name", company_name) or company_name)
                location = str(plant.get("location", location) or location)
        except Exception:
            pass
        return company_name, location


# =============================================================================
# Sidebar
# =============================================================================
class Sidebar(ctk.CTkFrame):
    _NAV_ITEMS = [
        ("Dashboard", "dashboard"),
        ("Analytics",  "analytics"),
        ("Settings",   "settings"),
        ("Journal",    "journal"),
    ]

    def __init__(self, parent, on_navigate, current_user=None):
        super().__init__(parent, fg_color=BG_SIDEBAR, corner_radius=10,
                         border_width=0,
                         width=185)
        self.pack_propagate(False)
        self._on_navigate = on_navigate
        self._current_user = current_user
        self._collapsed = False
        self._last_online = 0
        self._last_total = 0
        self._btns:  dict[str, ctk.CTkButton] = {}
        self._icons: dict[str, ctk.CTkImage]  = {}
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 4))
        self._menu_btn = ctk.CTkButton(
            top, text="☰", width=34, height=30,
            corner_radius=8, font=(UI_FONT, 14, "bold"),
            fg_color="#E2E8F0", text_color=CLR_TITLE,
            hover_color="#CBD5E1", command=self.toggle_sidebar,
        )
        self._menu_btn.pack(side="left")

        nav_items = list(self._NAV_ITEMS)
        if self._current_user and self._current_user.role == "operator":
            # Operator: allow Dashboard, Analytics, Settings, and Journal
            nav_items = [
                item for item in nav_items
                if item[0] in ("Dashboard", "Analytics", "Settings", "Journal")
            ]

        for name, icon_name in nav_items:
            ico = _load_icon_from_assets(icon_name, 18) or _make_icon(icon_name, 18)
            if ico:
                self._icons[name] = ico
            is_first = (name == "Dashboard")
            btn = ctk.CTkButton(
                self,
                text=f"  {name}",
                image=ico,
                compound="left",
                anchor="w",
                height=42,
                corner_radius=10,
                font=(UI_FONT, 13, "bold" if is_first else "normal"),
                fg_color="transparent",
                text_color=NAV_ACTIVE_TEXT if is_first else NAV_IDLE_TEXT,
                hover_color=NAV_HOVER_BG,
                command=lambda n=name: self._nav(n),
            )
            btn.pack(fill="x", padx=12, pady=3)
            self._btns[name] = btn

        # Elastic spacer
        ctk.CTkFrame(self, fg_color="transparent").pack(expand=True, fill="y")

        # Status indicator
        sf = ctk.CTkFrame(self, fg_color="transparent")
        sf.pack(fill="x", padx=14, pady=(0, 8))
        dot_row = ctk.CTkFrame(sf, fg_color="transparent")
        dot_row.pack(anchor="w")
        self._status_dot = ctk.CTkLabel(
            # dot_row, text="●", font=(UI_FONT, 10),
            dot_row, text="", font=(UI_FONT, 10),
            text_color=CLR_SAFE, fg_color="transparent")
        self._status_dot.pack(side="left")
        self._status_text = ctk.CTkLabel(
            # dot_row, text=" System Online", font=(UI_FONT, 10),
            dot_row, text="", font=(UI_FONT, 10),
            text_color=CLR_SAFE, fg_color="transparent")
        self._status_text.pack(side="left")
        self._status_lbl = ctk.CTkLabel(sf,
                                        # text="All zones connected",
                                        text="",
                                        font=(UI_FONT, 9), text_color=CLR_LABEL,
                                        fg_color="transparent")
        self._status_lbl.pack(anchor="w")

        self._apply_sidebar_mode()

    def _nav(self, name: str):
        for n, btn in self._btns.items():
            active = (n == name)
            btn.configure(
                fg_color="transparent",
                text_color=NAV_ACTIVE_TEXT if active else NAV_IDLE_TEXT,
                font=(UI_FONT, 13, "bold" if active else "normal"),
                hover_color=NAV_HOVER_BG,
            )
        self._on_navigate(name)

    def update_status(self, n_online: int, n_total: int):
        self._last_online = n_online
        self._last_total = n_total
        # Status text logic temporarily disabled to keep sidebar simple.
        # if self._collapsed:
        #     self._status_lbl.configure(text="")
        #     return
        # if n_online >= n_total:
        #     self._status_lbl.configure(text="All Devices connected")
        # else:
        #     self._status_lbl.configure(
        #         text=f"{n_online}/{n_total} Devices online")
        self._status_lbl.configure(text="")

    def toggle_sidebar(self):
        self._collapsed = not self._collapsed
        self._apply_sidebar_mode()

    def _apply_sidebar_mode(self):
        width = 72 if self._collapsed else 185
        self.configure(width=width)

        for name, btn in self._btns.items():
            btn.configure(
                text="" if self._collapsed else f"{name}",
                anchor="center" if self._collapsed else "w",
            )
            btn.pack_configure(padx=8 if self._collapsed else 12)

        if self._collapsed:
            self._status_text.configure(text="")
            self._status_lbl.configure(text="")
        else:
            # self._status_text.configure(text=" System Online")
            # self.update_status(self._last_online, self._last_total)
            self._status_text.configure(text="")
            self._status_lbl.configure(text="")


# =============================================================================
# Dashboard View  (scrollable card grid)
# =============================================================================
class DashboardView(ctk.CTkFrame):
    def __init__(self, parent, devices: list, cols: int,
                 current_user=None, on_rename=None):
        super().__init__(parent, fg_color="transparent")
        self._actor = current_user.username if current_user else "system"
        self._on_rename = on_rename
        self._cols = max(1, int(cols))

        scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8")
        scroll.pack(fill="both", expand=True)

        self._grid = ctk.CTkFrame(scroll, fg_color="transparent")
        self._grid.pack(fill="both", expand=True)
        for c in range(self._cols):
            self._grid.columnconfigure(c, weight=1)

        self._cards: dict[int, SensorCard] = {}
        for info in devices:
            self.add_or_update_device(info)

    def add_or_update_device(self, info: dict):
        addr = info.get("address")
        if addr is None:
            return

        addr = int(addr)
        if addr in self._cards:
            self._cards[addr].update(info)
            return

        idx = len(self._cards)
        r, c = divmod(idx, self._cols)
        card = SensorCard(self._grid, info, actor=self._actor,
                          on_rename=self._on_rename)
        card.grid(row=r, column=c, padx=8, pady=8, sticky="new")
        self._cards[addr] = card

    def update(self, results: list):
        for info in results:
            addr = info.get("address")
            if addr in self._cards:
                self._cards[addr].update(info)

    @property
    def cards(self) -> dict:
        return self._cards


# =============================================================================
# Trend Graph View
# =============================================================================
class TrendGraphView(ctk.CTkFrame):
    MAX_POINTS = 300   # keep last ~10 min of 2-sec polls

    def __init__(self, parent, devices: list):
        super().__init__(parent, fg_color="transparent")
        self._color_map: dict[int, str] = {
            addr: _LINE_PALETTE[i % len(_LINE_PALETTE)]
            for i, addr in enumerate(sorted(d["address"] for d in devices))
        }
        self._devices_meta = {d["address"]: d for d in devices}
        self._series: dict[int, collections.deque] = {
            addr: collections.deque(maxlen=self.MAX_POINTS)
            for addr in self._color_map
        }
        self._hover_last_key = None
        self._hover_last_ts = 0.0
        self._build()

    def _build(self):
        # Title row
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=4, pady=(8, 2))
        ctk.CTkLabel(hdr, text="Live Concentration Trend",
                     font=(UI_FONT, 13, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")

        # Device filter — dropdown button; vars initialised here for use in popup
        self._check_vars: dict[int, ctk.BooleanVar] = {}
        self._all_var = ctk.BooleanVar(value=True)
        for addr in sorted(self._color_map.keys()):
            self._check_vars[addr] = ctk.BooleanVar(value=True)

        self._filter_btn = ctk.CTkButton(
            hdr, text="Devices  ▾", width=110, height=30,
            corner_radius=8, font=FONT_LABEL,
            fg_color=BG_PILL, text_color=CLR_TITLE,
            hover_color="#8B9FE8",
            command=self._show_device_filter)
        self._filter_btn.pack(side="right", padx=(0, 4))

        # Chart card
        card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=16,
                            border_width=0)
        card.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        self._fig = Figure(facecolor=BG_CARD, dpi=96)
        self._ax  = self._fig.add_subplot(111)
        self._ax.set_facecolor("#F4F5FF")
        self._ax.set_xlabel("Time", fontsize=10, color="#0369A1", fontweight="bold")
        self._ax.set_ylabel("Concentration", fontsize=10, color="#0F766E", fontweight="bold")
        self._ax.tick_params(colors=CLR_LABEL, labelsize=8)
        self._ax.grid(True, color=CLR_CARD_BDR, linewidth=0.5,
                      linestyle="--", alpha=0.6)
        for sp in self._ax.spines.values():
            sp.set_color(CLR_CARD_BDR)
        self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=0.14)

        self._lines: dict[int, object] = {}
        for addr, clr in self._color_map.items():
            meta      = self._devices_meta[addr]
            dev_label = meta.get("device_name", f"Device {addr:03d}")
            lbl       = f"{dev_label} ({meta['gas_name']})"
            line, = self._ax.plot([], [], color=clr, linewidth=1.8,
                                  solid_capstyle="round", label=lbl)
            self._lines[addr] = line

        self._render_legend()

        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=card)
        self._mpl_canvas.get_tk_widget().pack(fill="both", expand=True,
                              padx=8, pady=8)
        self._mpl_canvas.draw()
        self._needs_draw = False

        # Hover tooltip (single reusable annotation + marker for low overhead)
        self._hover_ann = self._ax.annotate(
            "",
            xy=(0, 0), xytext=(12, 12), textcoords="offset points",
            fontsize=8,
            color=CLR_TITLE,
            bbox=dict(boxstyle="round,pad=0.3", fc="#FFFFFF",
                      ec="#CBD5E1", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#94A3B8", lw=0.8),
        )
        self._hover_ann.set_visible(False)
        self._hover_dot, = self._ax.plot([], [], marker="o", linestyle="",
                                         markersize=5, color=CLR_TITLE,
                                         zorder=5)
        self._hover_dot.set_visible(False)

        self._mpl_canvas.mpl_connect("motion_notify_event", self._on_hover_move)
        self._mpl_canvas.mpl_connect("figure_leave_event", self._on_hover_leave)

    def _render_legend(self):
        canvas_ready = hasattr(self, "_mpl_canvas") and self._mpl_canvas is not None
        visible_lines = [ln for ln in self._lines.values() if ln.get_visible()]
        if not visible_lines:
            if self._ax.legend_ is not None:
                self._ax.legend_.remove()
            self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=0.14)
            if canvas_ready:
                self._mpl_canvas.draw_idle()
            return

        labels = [str(ln.get_label() or "") for ln in visible_lines]
        fig_w_px = max(240, int(self._fig.get_size_inches()[0] * self._fig.dpi * 0.90))

        est_label_widths = [max(96, int(len(lbl) * 6.4) + 36) for lbl in labels]
        total_w = sum(est_label_widths)

        rows = max(1, (total_w + fig_w_px - 1) // fig_w_px)
        rows = min(rows, max(1, len(labels)))
        ncol = max(1, (len(labels) + rows - 1) // rows)

        bottom = min(0.46, 0.24 + max(0, rows - 1) * 0.07)
        self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=bottom)

        self._ax.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.16),
            ncol=ncol,
            fontsize=8,
            framealpha=0.85,
            edgecolor=CLR_CARD_BDR,
            columnspacing=1.1,
            handlelength=2.0,
            handletextpad=0.5,
            borderpad=0.4,
        )
        if canvas_ready:
            self._mpl_canvas.draw_idle()

    def _hide_hover(self):
        changed = False
        if self._hover_ann.get_visible():
            self._hover_ann.set_visible(False)
            changed = True
        if self._hover_dot.get_visible():
            self._hover_dot.set_visible(False)
            changed = True
        if changed:
            self._mpl_canvas.draw_idle()

    def _on_hover_leave(self, _event):
        self._hover_last_key = None
        self._hide_hover()

    def _on_hover_move(self, event):
        # Ignore events outside axes / before chart has data
        if event.inaxes != self._ax or event.xdata is None:
            self._hover_last_key = None
            self._hide_hover()
            return

        # Throttle hover processing (~45 FPS max) to keep overhead low
        now = time.monotonic()
        if now - self._hover_last_ts < (1.0 / 45.0):
            return
        self._hover_last_ts = now

        best = None
        best_dist2 = float("inf")

        for addr, line in self._lines.items():
            if not line.get_visible():
                continue
            xs = line.get_xdata()
            ys = line.get_ydata()
            if len(xs) == 0:
                continue

            xs_list = xs.tolist() if hasattr(xs, "tolist") else list(xs)
            idx = bisect.bisect_left(xs_list, event.xdata)
            for cand in (idx - 1, idx):
                if 0 <= cand < len(xs_list):
                    px, py = self._ax.transData.transform((xs_list[cand], ys[cand]))
                    dx = px - event.x
                    dy = py - event.y
                    dist2 = dx * dx + dy * dy
                    if dist2 < best_dist2:
                        best_dist2 = dist2
                        best = (addr, cand, xs_list[cand], ys[cand], line.get_color())

        # Only show when cursor is near a point (pixel threshold)
        if best is None or best_dist2 > (14.0 * 14.0):
            self._hover_last_key = None
            self._hide_hover()
            return

        addr, idx, xval, yval, clr = best
        key = (addr, idx)
        if key == self._hover_last_key and self._hover_ann.get_visible():
            return
        self._hover_last_key = key

        meta = self._devices_meta.get(addr, {})
        dev_label = meta.get("device_name", f"Device {addr:03d}")
        unit = meta.get("gas_unit", "")
        ts_txt = mdates.num2date(xval).strftime("%H:%M:%S")
        val_txt = f"{yval:.2f} {unit}".strip()

        self._hover_ann.xy = (xval, yval)
        self._hover_ann.set_text(f"{dev_label}\n{val_txt}\n{ts_txt}")
        self._hover_ann.set_visible(True)

        self._hover_dot.set_data([xval], [yval])
        self._hover_dot.set_color(clr)
        self._hover_dot.set_visible(True)

        self._mpl_canvas.draw_idle()

    def _show_device_filter(self):
        """Open a compact dropdown popup with per-device check/uncheck."""
        import tkinter as _tk
        popup = _tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=BG_CARD)

        inner = ctk.CTkFrame(popup, fg_color=BG_CARD, corner_radius=12,
                             border_width=1, border_color="#CBD5E1")
        inner.pack(fill="both", expand=True)

        ctk.CTkLabel(inner, text="SHOW DEVICES",
                     font=(UI_FONT, 8, "bold"), text_color="#94A3B8",
                     fg_color="transparent").pack(
                     anchor="w", padx=14, pady=(10, 4))

        # All toggle
        ctk.CTkCheckBox(
            inner, text="All Devices",
            variable=self._all_var,
            font=(UI_FONT, 10, "bold"), text_color=CLR_TITLE,
            fg_color=NAV_ACTIVE_BG, hover_color="#0284C7",
            checkmark_color="#FFFFFF", border_width=2,
            command=self._toggle_all).pack(
            anchor="w", padx=14, pady=(2, 6))

        ctk.CTkFrame(inner, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=10)

        for addr in sorted(self._check_vars.keys()):
            meta      = self._devices_meta[addr]
            line_clr  = self._color_map[addr]
            dev_label = meta.get("device_name", f"Device {addr:03d}")
            ctk.CTkCheckBox(
                inner,
                text=f"{dev_label}  ({meta['gas_name']})",
                variable=self._check_vars[addr],
                font=(UI_FONT, 9), text_color=CLR_TITLE,
                fg_color=line_clr, hover_color=line_clr,
                checkmark_color="#FFFFFF", border_width=2,
                command=self._apply_filter).pack(
                anchor="w", padx=14, pady=3)

        ctk.CTkFrame(inner, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(inner, text="Click outside to close",
                     font=(UI_FONT, 8), text_color="#94A3B8",
                     fg_color="transparent").pack(
                     anchor="w", padx=14, pady=(4, 8))

        # Position below the filter button
        self._filter_btn.update_idletasks()
        popup.update_idletasks()
        popup.geometry(
            f"+{self._filter_btn.winfo_rootx()}"
            f"+{self._filter_btn.winfo_rooty() + self._filter_btn.winfo_height() + 4}")
        popup.bind("<FocusOut>",
                   lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.focus_force()

    def _toggle_all(self):
        """Set all device checkboxes to match the 'All' toggle."""
        state = self._all_var.get()
        for var in self._check_vars.values():
            var.set(state)
        for addr, line in self._lines.items():
            line.set_visible(state)
        self._render_legend()

    def _apply_filter(self):
        """Sync per-device visibility from checkboxes; keep 'All' in sync."""
        all_on = all(v.get() for v in self._check_vars.values())
        self._all_var.set(all_on)
        for addr, line in self._lines.items():
            line.set_visible(self._check_vars[addr].get())
        self._render_legend()

    def update_device_name(self, addr: int, new_name: str):
        """Refresh legend label and filter popup text for a renamed device."""
        if addr not in self._devices_meta:
            return
        meta = self._devices_meta[addr]
        meta["device_name"] = new_name
        if addr in self._lines:
            self._lines[addr].set_label(f"{new_name} ({meta['gas_name']})")
            self._render_legend()

    def add_device(self, info: dict):
        """Register a newly discovered device at runtime."""
        addr = info.get("address")
        if addr is None:
            return
        addr = int(addr)
        if addr in self._devices_meta:
            return

        self._devices_meta[addr] = info
        self._color_map[addr] = _LINE_PALETTE[len(self._color_map) % len(_LINE_PALETTE)]
        self._series[addr] = collections.deque(maxlen=self.MAX_POINTS)
        self._check_vars[addr] = ctk.BooleanVar(value=True)
        self._all_var.set(True)

        dev_label = info.get("device_name", f"Device {addr:03d}")
        lbl = f"{dev_label} ({info.get('gas_name', 'H2')})"
        line, = self._ax.plot([], [], color=self._color_map[addr], linewidth=1.8,
                              solid_capstyle="round", label=lbl)
        self._lines[addr] = line
        self._render_legend()

    def push_reading(self, addr: int, ts: datetime.datetime, value: float):
        if addr not in self._series:
            return
        self._series[addr].append((mdates.date2num(ts), value))
        series = self._series[addr]
        xs = [p[0] for p in series]
        ys = [p[1] for p in series]
        self._lines[addr].set_xdata(xs)
        self._lines[addr].set_ydata(ys)
        self._needs_draw = True

    def redraw(self):
        """Call from main thread after pushing readings."""
        if not self._needs_draw:
            return
        self._ax.relim()
        self._ax.autoscale_view()
        self._ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        self._ax.xaxis.set_major_locator(
            mdates.AutoDateLocator(minticks=3, maxticks=7))
        self._render_legend()
        self._fig.autofmt_xdate(rotation=28, ha="right")
        self._mpl_canvas.draw_idle()
        self._needs_draw = False


# =============================================================================
# Calendar Date-Picker Popup
# =============================================================================
class _CalendarPopup(ctk.CTkToplevel):
    """Lightweight month-grid datetime picker. callback('%Y-%m-%d %H:%M') on apply."""

    _WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

    def __init__(self, anchor_widget, initial_dt: datetime.datetime, callback):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=BG_CARD)
        self._callback = callback
        self._viewed = initial_dt.date().replace(day=1)
        self._selected = initial_dt.date()
        self._hour_var = ctk.StringVar(value=f"{initial_dt.hour:02d}")
        self._minute_var = ctk.StringVar(value=f"{initial_dt.minute:02d}")

        # Single card — shrink border and radius for compactness
        self._inner = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=8,
                       border_width=1, border_color="#CBD5E1")
        self._inner.pack(padx=2, pady=2)

        self._render()

        # Freeze geometry, then position below the anchor widget
        self.update_idletasks()
        w = self._inner.winfo_reqwidth()
        h = self._inner.winfo_reqheight()
        anchor_widget.update_idletasks()
        x = anchor_widget.winfo_rootx()
        y = anchor_widget.winfo_rooty() + anchor_widget.winfo_height() + 4
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.bind("<FocusOut>", lambda e: self._dismiss())
        self.focus_force()

    def _dismiss(self):
        try:
            self.destroy()
        except Exception:
            pass

    def _render(self):
        for w in self._inner.winfo_children():
            w.destroy()

        # Cell dimensions — shared by header and day buttons so columns align
        _CW, _CH = 28, 24   # cell width, cell height (smaller for compactness)

        # Navigation row
        nav = ctk.CTkFrame(self._inner, fg_color="transparent")
        nav.pack(fill="x", padx=4, pady=(4, 2))
        ctk.CTkButton(nav, text="‹", width=24, height=24, corner_radius=7,
                      fg_color=BG_PILL, text_color=CLR_TITLE,
                      hover_color="#8B9FE8", font=(UI_FONT, 12),
                      command=self._prev_month).pack(side="left")
        ctk.CTkLabel(nav, text=self._viewed.strftime("%B  %Y"),
                     font=(UI_FONT, 10, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left", expand=True)
        ctk.CTkButton(nav, text="›", width=24, height=24, corner_radius=7,
                      fg_color=BG_PILL, text_color=CLR_TITLE,
                      hover_color="#8B9FE8", font=(UI_FONT, 12),
                      command=self._next_month).pack(side="right")

        # Unified grid — row 0 = weekday header, rows 1+ = day buttons
        import calendar as _cal
        grid_f = ctk.CTkFrame(self._inner, fg_color="transparent")
        grid_f.pack(padx=2, pady=(0, 4))
        for c in range(7):
            grid_f.columnconfigure(c, minsize=_CW + 1)  # tighter columns

        # Weekday header row
        for c, day_name in enumerate(self._WEEKDAYS):
            ctk.CTkLabel(grid_f, text=day_name,
                         font=(UI_FONT, 8, "bold"),
                         text_color=CLR_LABEL,
                         fg_color="transparent",
                         width=_CW, height=16,
                         anchor="center").grid(
                         row=0, column=c, padx=0, pady=(0, 1))

        # Day buttons
        for week_r, week in enumerate(_cal.monthcalendar(
                self._viewed.year, self._viewed.month)):
            for day_c, day in enumerate(week):
                if day == 0:
                    ctk.CTkFrame(grid_f, fg_color="transparent",
                                 width=_CW, height=_CH).grid(
                                 row=week_r + 1, column=day_c, padx=0, pady=0)
                else:
                    d_date = datetime.date(
                        self._viewed.year, self._viewed.month, day)
                    is_sel = (d_date == self._selected)
                    ctk.CTkButton(
                        grid_f, text=str(day),
                        width=_CW, height=_CH,
                        corner_radius=8,
                        font=(UI_FONT, 9, "bold" if is_sel else "normal"),
                        fg_color=NAV_ACTIVE_BG if is_sel else "transparent",
                        text_color="#FFFFFF" if is_sel else CLR_TITLE,
                        hover_color="#DBEAFE",
                        border_spacing=0,
                        command=lambda dd=d_date: self._pick(dd)).grid(
                        row=week_r + 1, column=day_c, padx=0, pady=0)

        # Time row + Apply
        trow = ctk.CTkFrame(self._inner, fg_color="transparent")
        trow.pack(fill="x", padx=6, pady=(2, 6))
        ctk.CTkLabel(trow, text="Time:", font=(UI_FONT, 9, "bold"),
                     text_color=CLR_LABEL, fg_color="transparent").pack(side="left")

        ctk.CTkOptionMenu(
            trow,
            values=[f"{h:02d}" for h in range(24)],
            variable=self._hour_var,
            width=64, height=26, corner_radius=7,
            font=(UI_FONT, 9),
        ).pack(side="left", padx=(6, 2))
        ctk.CTkLabel(trow, text=":", font=(UI_FONT, 10, "bold"),
                     text_color=CLR_TITLE, fg_color="transparent").pack(side="left")
        ctk.CTkOptionMenu(
            trow,
            values=[f"{m:02d}" for m in range(0, 60)],
            variable=self._minute_var,
            width=64, height=26, corner_radius=7,
            font=(UI_FONT, 9),
        ).pack(side="left", padx=(2, 8))

        ctk.CTkButton(
            trow, text="Set", width=54, height=26,
            corner_radius=7, font=(UI_FONT, 9, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=self._apply,
        ).pack(side="right")

    def _prev_month(self):
        y, m = self._viewed.year, self._viewed.month
        self._viewed = self._viewed.replace(
            year=y - 1 if m == 1 else y,
            month=12 if m == 1 else m - 1)
        self.withdraw()
        self._render()
        self.update_idletasks()
        self.deiconify()
        self.focus_force()

    def _next_month(self):
        y, m = self._viewed.year, self._viewed.month
        self._viewed = self._viewed.replace(
            year=y + 1 if m == 12 else y,
            month=1 if m == 12 else m + 1)
        self.withdraw()
        self._render()
        self.update_idletasks()
        self.deiconify()
        self.focus_force()

    def _pick(self, d: datetime.date):
        self._selected = d
        self.withdraw()
        self._render()
        self.update_idletasks()
        self.deiconify()
        self.focus_force()

    def _apply(self):
        dt = datetime.datetime(
            self._selected.year,
            self._selected.month,
            self._selected.day,
            int(self._hour_var.get()),
            int(self._minute_var.get()),
            0,
        )
        self._callback(dt.strftime("%Y-%m-%d %H:%M"))
        self._dismiss()


# =============================================================================
# Device Logs View
# =============================================================================
class DeviceLogsView(ctk.CTkFrame):
    _TS_COL_WIDTH = 170
    _DEV_COL_WIDTH = 120
    _EXPORT_FREQ = ["Raw", "5 min", "15 min", "30 min", "60 min"]

    def __init__(self, parent, devices: list):
        super().__init__(parent, fg_color="transparent")
        self._devices        = {d["address"]: d for d in devices}
        self._selected_addrs: set[int] = set(self._devices.keys())
        self._rows_cache: list[dict] = []
        self._matrix_timestamps: list[str] = []
        self._matrix_device_cols: list[tuple[int, str]] = []
        self._device_id_by_addr: dict[int, int] = {}
        self._page_size      = 50
        self._current_page   = 0
        self._total_count    = 0
        self._query_params: dict = {}
        self._build()

    def _build(self):
        # ── Filter bar ────────────────────────────────────────────────────
        fbar = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=12,
                            border_width=0)
        fbar.pack(fill="x", padx=4, pady=(8, 6))

        inner = ctk.CTkFrame(fbar, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        # Device picker — classic look (button + popup list)
        ctk.CTkLabel(inner, text="Devices:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._dev_options = ["All Devices"] + [
            self._devices[addr].get("device_name", f"Device {addr:03d}")
            for addr in sorted(self._devices.keys())
        ]
        self._dev_name_to_addr = {
            self._devices[addr].get("device_name", f"Device {addr:03d}"): addr
            for addr in self._devices
        }
        self._dev_var = ctk.StringVar(value="All Devices")
        self._dev_btn = ctk.CTkButton(
            inner,
            textvariable=self._dev_var,
            width=130, height=30,
            corner_radius=6,
            font=FONT_FILTER_LABEL,
            fg_color="white", text_color=CLR_TITLE,
            hover_color="#F1F5F9",
            border_width=1, border_color="#CBD5E1",
            anchor="w",
            command=self._show_device_dropdown)
        self._dev_btn.pack(side="left", padx=(4, 16))

        # Date from/to
        now_dt = datetime.datetime.now().replace(second=0, microsecond=0)
        from_dt = now_dt.replace(hour=0, minute=0)
        to_dt = now_dt.replace(hour=23, minute=59)
        from_txt = from_dt.strftime("%Y-%m-%d %H:%M")
        to_txt = to_dt.strftime("%Y-%m-%d %H:%M")
        ctk.CTkLabel(inner, text="From:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._from_entry = ctk.CTkEntry(inner, width=145, font=FONT_FILTER_LABEL,
                        placeholder_text="YYYY-MM-DD HH:MM")
        self._from_entry.insert(0, from_txt)
        self._from_entry.pack(side="left", padx=(4, 2))
        self._cal_from_btn = ctk.CTkButton(
            inner, text="📅", width=28, height=28,
            corner_radius=6, font=(UI_FONT, 11),
            fg_color=BG_PILL, text_color=NAV_ACTIVE_BG,
            hover_color="#8B9FE8",
            command=self._open_from_cal)
        self._cal_from_btn.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(inner, text="To:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._to_entry = ctk.CTkEntry(inner, width=145, font=FONT_FILTER_LABEL,
                          placeholder_text="YYYY-MM-DD HH:MM")
        self._to_entry.insert(0, to_txt)
        self._to_entry.pack(side="left", padx=(4, 2))
        self._cal_to_btn = ctk.CTkButton(
            inner, text="📅", width=28, height=28,
            corner_radius=6, font=(UI_FONT, 11),
            fg_color=BG_PILL, text_color=NAV_ACTIVE_BG,
            hover_color="#8B9FE8",
            command=self._open_to_cal)
        self._cal_to_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(inner, text="Fetch", width=76, height=32,
                      corner_radius=8, font=(UI_FONT, 11, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=self._fetch).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(inner, text="Export Freq:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left", padx=(0, 4))
        self._export_freq_var = ctk.StringVar(value="Raw")
        ctk.CTkOptionMenu(
            inner,
            values=self._EXPORT_FREQ,
            variable=self._export_freq_var,
            width=95,
            height=32,
            corner_radius=8,
            font=FONT_FILTER_LABEL,
        ).pack(side="left", padx=(0, 10))

        # Single export dropdown button
        self._export_btn = ctk.CTkButton(
            inner, text="Export ▾", width=90, height=32,
            corner_radius=8, font=(UI_FONT, 10, "bold"),
            fg_color=CLR_SAFE, hover_color="#15803D",
            command=self._show_export_menu)
        self._export_btn.pack(side="left", padx=4)

        self._info_lbl = ctk.CTkLabel(inner, text="", font=(UI_FONT, 9),
                                      text_color=CLR_LABEL,
                                      fg_color="transparent")
        self._info_lbl.pack(side="right")

        # ── Table ─────────────────────────────────────────────────────────
        self._tcard = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=12,
                             border_width=0)
        self._tcard.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        tcard = self._tcard

        import tkinter as _tk

        # Horizontal scroll wrapper: moves header + body together.
        self._table_x_canvas = _tk.Canvas(
            tcard,
            highlightthickness=0,
            bg=BG_CARD,
            bd=0,
            relief="flat",
        )
        self._table_x_canvas.pack(fill="both", expand=True, padx=0, pady=0)

        self._table_x_wrap = ctk.CTkFrame(
            self._table_x_canvas,
            fg_color="transparent",
            corner_radius=0,
        )
        self._table_x_window = self._table_x_canvas.create_window(
            (0, 0),
            window=self._table_x_wrap,
            anchor="nw",
        )

        self._table_h_scroll = ctk.CTkScrollbar(
            tcard,
            orientation="horizontal",
            command=self._table_x_canvas.xview,
            button_color=BG_PILL,
            button_hover_color="#8B9FE8",
        )
        self._table_h_scroll.pack(fill="x", padx=6, pady=(0, 2))
        self._table_x_canvas.configure(xscrollcommand=self._table_h_scroll.set)

        self._table_x_wrap.bind("<Configure>", lambda _e: self._sync_table_x_layout())
        self._table_x_canvas.bind("<Configure>", lambda _e: self._sync_table_x_layout())

        # Header row
        self._hdr_row = ctk.CTkFrame(self._table_x_wrap, fg_color=BG_PILL, corner_radius=0)
        self._hdr_row.pack(fill="x", padx=0, pady=(0, 0))

        # Scrollable body
        self._table_scroll = ctk.CTkScrollableFrame(
            self._table_x_wrap, fg_color="transparent",
            scrollbar_button_color=BG_PILL)
        self._table_scroll.pack(fill="both", expand=True, padx=0, pady=0)

        self._row_widgets: list[list[ctk.CTkLabel]] = []
        self._rebuild_matrix_headers([])

        # Loading overlay (placed on top of the table card, hidden by default)
        self._loading_overlay = ctk.CTkFrame(
            tcard, fg_color="#F0F4FF", corner_radius=0)
        self._loading_lbl = ctk.CTkLabel(
            self._loading_overlay,
            text="⏳  Loading…",
            font=(UI_FONT, 13, "bold"), text_color=NAV_ACTIVE_BG,
            fg_color="transparent")
        self._loading_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # ── Pagination footer ─────────────────────────────────────────────
        footer = ctk.CTkFrame(tcard, fg_color="#E0E9F8", corner_radius=0)
        footer.pack(fill="x", padx=0, pady=0)

        # single inner row — pack centred so arrows + labels align to mid
        prow = ctk.CTkFrame(footer, fg_color="transparent")
        prow.pack(anchor="center", pady=8)

        self._prev_btn = ctk.CTkButton(
            prow, text="Prev", width=60, height=30,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color="#CBD5E1", text_color="#334155",
            hover_color="#94A3B8", state="disabled",
            command=lambda: self._go_page(-1))
        self._prev_btn.pack(side="left")

        # Centre range badge
        badge = ctk.CTkFrame(prow, fg_color="#DBEAFE", corner_radius=20)
        badge.pack(side="left")
        self._page_lbl = ctk.CTkLabel(
            badge, text="—  No data loaded  —",
            font=(UI_FONT, 10, "bold"), text_color=NAV_ACTIVE_BG,
            fg_color="transparent")
        self._page_lbl.pack(padx=16, pady=4)

        ctk.CTkLabel(prow, text="",
                     fg_color="transparent").pack(side="left", padx=(20, 5))
        self._next_btn = ctk.CTkButton(
            prow, text="Next", width=60, height=30,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color="#CBD5E1", text_color="#F3F8FF",
            hover_color="#94A3B8", state="disabled",
            command=lambda: self._go_page(1))
        self._next_btn.pack(side="left")

    # ── helpers ──────────────────────────────────────────────────────────
    def _show_loading(self):
        self._loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._loading_overlay.lift()
        self.update_idletasks()

    def _hide_loading(self):
        self._loading_overlay.place_forget()

    def _sync_table_x_layout(self):
        try:
            self._table_x_wrap.update_idletasks()
            req_w = self._table_x_wrap.winfo_reqwidth()
            req_h = self._table_x_wrap.winfo_reqheight()
            canvas_w = max(1, self._table_x_canvas.winfo_width())
            canvas_h = max(1, self._table_x_canvas.winfo_height())
            target_w = max(req_w, canvas_w)
            target_h = max(req_h, canvas_h)
            self._table_x_canvas.itemconfigure(
                self._table_x_window,
                width=target_w,
                height=target_h,
            )
            self._table_x_canvas.configure(scrollregion=self._table_x_canvas.bbox("all"))
        except Exception:
            pass

    def _refresh_device_filter_text(self):
        total = len(self._devices)
        chosen = len(self._selected_addrs)
        if chosen >= total:
            self._dev_var.set("All Devices")
        else:
            self._dev_var.set(f"Chosen ({chosen})")

    def _get_active_device_columns(self) -> list[tuple[int, str]]:
        addrs = sorted(self._selected_addrs) if self._selected_addrs else sorted(self._devices.keys())
        cols: list[tuple[int, str]] = []
        for addr in addrs:
            meta = self._devices.get(addr)
            if not meta:
                continue
            cols.append((int(addr), str(meta.get("device_name", f"Device {addr:03d}"))))
        return cols

    def _rebuild_matrix_headers(self, device_cols: list[tuple[int, str]]):
        for w in self._hdr_row.winfo_children():
            w.destroy()

        headers = [("Timestamp", self._TS_COL_WIDTH)] + [
            (name, self._DEV_COL_WIDTH) for _, name in device_cols
        ]

        for col, (text, width) in enumerate(headers):
            ctk.CTkLabel(
                self._hdr_row,
                text=text,
                font=(UI_FONT, 11, "bold"),
                text_color=CLR_TITLE,
                fg_color="transparent",
                width=width,
                anchor="w",
            ).grid(row=0, column=col,
                   padx=(8 if col == 0 else 4, 4), pady=8, sticky="w")

        for col in range(len(headers)):
            width = self._TS_COL_WIDTH if col == 0 else self._DEV_COL_WIDTH
            self._table_scroll.columnconfigure(col, minsize=width)

        self._sync_table_x_layout()

    def update_device_name(self, addr: int, new_name: str):
        """Refresh device dropdown options after a rename."""
        if addr not in self._devices:
            return
        old_name = self._devices[addr].get("device_name", f"Device {addr:03d}")
        self._devices[addr]["device_name"] = new_name
        # Rebuild options list and addr-lookup map
        self._dev_options = ["All Devices"] + [
            self._devices[a].get("device_name", f"Device {a:03d}")
            for a in sorted(self._devices.keys())
        ]
        self._dev_name_to_addr = {
            self._devices[a].get("device_name", f"Device {a:03d}"): a
            for a in self._devices
        }
        self._refresh_device_filter_text()

    def add_device(self, info: dict):
        """Register a newly discovered device for logs filters/runtime fetches."""
        addr = info.get("address")
        if addr is None:
            return
        addr = int(addr)
        if addr in self._devices:
            return

        self._devices[addr] = info
        self._selected_addrs.add(addr)
        self._dev_options = ["All Devices"] + [
            self._devices[a].get("device_name", f"Device {a:03d}")
            for a in sorted(self._devices.keys())
        ]
        self._dev_name_to_addr = {
            self._devices[a].get("device_name", f"Device {a:03d}"): a
            for a in self._devices
        }
        self._refresh_device_filter_text()

    def _show_device_dropdown(self):
        """Custom multi-select dropdown for All or Chosen devices."""
        import tkinter as _tk
        popup = _tk.Toplevel(self)
        popup.withdraw()                   # hide immediately — prevents flash at (0,0)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="white")

        frame = ctk.CTkScrollableFrame(
            popup, fg_color="white", corner_radius=8,
            border_width=1, border_color="#CBD5E1",
            height=min(260, max(6, len(self._devices) + 2) * 34))
        frame.pack(fill="both", expand=True)

        ctk.CTkLabel(
            frame, text="FILTER DEVICES", font=(UI_FONT, 9, "bold"),
            text_color="#94A3B8", fg_color="transparent", anchor="w",
        ).pack(fill="x", padx=8, pady=(6, 2))

        all_var = ctk.BooleanVar(value=(len(self._selected_addrs) == len(self._devices)))
        item_vars: dict[int, ctk.BooleanVar] = {
            addr: ctk.BooleanVar(value=(addr in self._selected_addrs))
            for addr in sorted(self._devices.keys())
        }

        def _toggle_all():
            state = all_var.get()
            for v in item_vars.values():
                v.set(state)

        ctk.CTkCheckBox(
            frame, text="All Devices", variable=all_var,
            font=(UI_FONT, 10, "bold"), text_color=CLR_TITLE,
            fg_color=NAV_ACTIVE_BG, hover_color="#0284C7",
            command=_toggle_all,
        ).pack(anchor="w", padx=8, pady=(2, 6))

        ctk.CTkFrame(frame, fg_color="#E2E8F0", height=1, corner_radius=0).pack(fill="x", padx=6, pady=(0, 4))

        for addr in sorted(self._devices.keys()):
            label = self._devices[addr].get("device_name", f"Device {addr:03d}")
            ctk.CTkCheckBox(
                frame,
                text=label,
                variable=item_vars[addr],
                font=(UI_FONT, 10),
                text_color=CLR_TITLE,
                fg_color=NAV_ACTIVE_BG,
                hover_color="#0284C7",
                command=lambda: all_var.set(all(v.get() for v in item_vars.values())),
            ).pack(anchor="w", padx=8, pady=2)

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(8, 6))

        def _apply():
            chosen = {addr for addr, var in item_vars.items() if var.get()}
            self._selected_addrs = chosen if chosen else set(self._devices.keys())
            self._refresh_device_filter_text()
            popup.destroy()

        ctk.CTkButton(
            actions, text="Apply", width=70, height=28,
            corner_radius=6, font=(UI_FONT, 10, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=_apply,
        ).pack(side="right")

        # Compute size, then position and reveal in one step
        self._dev_btn.update_idletasks()
        popup.update_idletasks()
        x = self._dev_btn.winfo_rootx()
        y = self._dev_btn.winfo_rooty() + self._dev_btn.winfo_height() + 2
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        popup.geometry(f"{w}x{h}+{x}+{y}")
        popup.bind("<FocusOut>",
                   lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.deiconify()
        popup.focus_force()

    def _open_from_cal(self):
        try:
            dt = self._parse_dt_entry(self._from_entry.get().strip(), is_end=False)
        except ValueError:
            dt = datetime.datetime.now().replace(second=0, microsecond=0)
        def _set(val):
            self._from_entry.delete(0, "end")
            self._from_entry.insert(0, val)
        _CalendarPopup(self._cal_from_btn, dt, _set)

    def _open_to_cal(self):
        try:
            dt = self._parse_dt_entry(self._to_entry.get().strip(), is_end=True)
        except ValueError:
            dt = datetime.datetime.now().replace(
                hour=23, minute=59, second=0, microsecond=0)
        def _set(val):
            self._to_entry.delete(0, "end")
            self._to_entry.insert(0, val)
        _CalendarPopup(self._cal_to_btn, dt, _set)

    def _parse_dt_entry(self, value: str, is_end: bool) -> datetime.datetime:
        """Parse date/datetime entry. Supports YYYY-MM-DD and YYYY-MM-DD HH:MM[:SS]."""
        txt = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(txt, fmt)
                if fmt == "%Y-%m-%d":
                    if is_end:
                        return dt.replace(hour=23, minute=59, second=59)
                    return dt.replace(hour=0, minute=0, second=0)
                if fmt == "%Y-%m-%d %H:%M" and is_end:
                    return dt.replace(second=59)
                return dt
            except ValueError:
                continue
        raise ValueError("Invalid date format")

    def _fetch(self):
        """Validate filters, reset to page 0, get total count, load first page."""
        if not DB_AVAILABLE:
            reason = DB_IMPORT_ERROR or "database modules failed to load"
            self._info_lbl.configure(text=f"DB unavailable: {reason}", text_color=CLR_CRIT)
            return
        try:
            start_dt = self._parse_dt_entry(self._from_entry.get().strip(), is_end=False)
            end_dt = self._parse_dt_entry(self._to_entry.get().strip(), is_end=True)
        except ValueError:
            self._info_lbl.configure(
                text="Invalid date — use YYYY-MM-DD [HH:MM[:SS]]", text_color=CLR_CRIT)
            return

        self._current_page  = 0
        selected_addrs = sorted(self._selected_addrs)
        selected_device_ids = []
        device_id_by_addr: dict[int, int] = {}
        from db_repository import get_device_by_address
        for addr in selected_addrs:
            dev = get_device_by_address(addr)
            if dev:
                selected_device_ids.append(dev["id"])
                device_id_by_addr[int(addr)] = int(dev["id"])

        self._device_id_by_addr = device_id_by_addr

        self._query_params  = {
            "start_dt": start_dt,
            "end_dt":   end_dt,
            "selected_addrs": selected_addrs,
            "device_ids": selected_device_ids,
            "all_devices": len(selected_addrs) >= len(self._devices),
        }
        self._info_lbl.configure(text="")
        self._update_total()
        self._load_page()

    def _update_total(self):
        """Query and cache the total record count for the current filters."""
        qp = self._query_params
        if not qp:
            return
        try:
            from db_repository import count_distinct_recorded_at_in_range
            if qp.get("all_devices", True):
                self._total_count = count_distinct_recorded_at_in_range(
                    qp["start_dt"], qp["end_dt"], device_ids=None)
            else:
                self._total_count = count_distinct_recorded_at_in_range(
                    qp["start_dt"], qp["end_dt"],
                    device_ids=qp.get("device_ids", []))
        except Exception:
            logger.exception("Device Logs count error")
            self._total_count = 0

    def _load_page(self):
        """Fetch the current page from the DB and refresh the table."""
        qp = self._query_params
        if not qp:
            return
        self._show_loading()
        try:
            from db_repository import get_device_logs_matrix_page
            offset = self._current_page * self._page_size
            device_ids = None if qp.get("all_devices", True) else qp.get("device_ids", [])
            timestamps, rows = get_device_logs_matrix_page(
                qp["start_dt"], qp["end_dt"], self._page_size, offset,
                device_ids=device_ids,
            )

            self._rows_cache = rows
            self._matrix_timestamps = timestamps
            self._matrix_device_cols = self._get_active_device_columns()
            self._rebuild_matrix_headers(self._matrix_device_cols)
            self._render_table(timestamps, rows, self._matrix_device_cols)

            # Pagination label
            start_row = offset + 1 if timestamps else 0
            end_row   = offset + len(timestamps)
            self._page_lbl.configure(
                text=f"{start_row} – {end_row}  of  {self._total_count} timestamp row(s)")

            # Enable / disable nav buttons with colour feedback
            n_pages  = max(0, (self._total_count - 1) // self._page_size)
            prev_ok  = self._current_page > 0
            next_ok  = self._current_page < n_pages
            self._prev_btn.configure(
                state="normal" if prev_ok else "disabled",
                fg_color=NAV_ACTIVE_BG if prev_ok else "#CBD5E1",
                text_color="#F3F8FF" if prev_ok else "#334155",
                hover_color="#075985" if prev_ok else "#94A3B8")
            self._next_btn.configure(
                state="normal" if next_ok else "disabled",
                fg_color=NAV_ACTIVE_BG if next_ok else "#CBD5E1",
                text_color="#F3F8FF" if next_ok else "#334155",
                hover_color="#075985" if next_ok else "#94A3B8")
        except Exception as e:
            logger.exception("Device Logs page load error")
            self._info_lbl.configure(text=f"Error: {e}", text_color=CLR_CRIT)
        finally:
            self._hide_loading()

    def _go_page(self, delta: int):
        self._current_page += delta
        self._load_page()

    def _render_table(self, timestamps: list[str], rows: list[dict],
                      device_cols: list[tuple[int, str]]):
        # Clear old rows
        for row_lbls in self._row_widgets:
            for lbl in row_lbls:
                lbl.destroy()
        self._row_widgets.clear()

        # Pivot: {recorded_at: {device_id: concentration}}
        pivot: dict[str, dict[int, float]] = {ts: {} for ts in timestamps}
        for row in rows:
            ts = str(row.get("recorded_at", ""))
            if ts not in pivot:
                continue
            try:
                dev_id = int(row.get("device_id", 0) or 0)
            except Exception:
                continue
            pivot[ts][dev_id] = float(row.get("concentration_value") or 0.0)

        for idx, ts in enumerate(timestamps):
            bg = "#F0F4FF" if idx % 2 == 0 else "transparent"
            row_lbls = []

            ts_lbl = ctk.CTkLabel(
                self._table_scroll,
                text=ts,
                font=(UI_FONT, 11),
                text_color=CLR_TITLE,
                fg_color=bg,
                width=self._TS_COL_WIDTH,
                anchor="w",
            )
            ts_lbl.grid(row=idx, column=0, padx=(8, 4), pady=3, sticky="w")
            row_lbls.append(ts_lbl)

            for col_idx, (addr, _name) in enumerate(device_cols, start=1):
                device_id = self._device_id_by_addr.get(int(addr), 0)
                val = pivot.get(ts, {}).get(device_id)
                text = f"{val:.2f}" if val is not None else "—"

                lbl = ctk.CTkLabel(
                    self._table_scroll,
                    text=text,
                    font=(UI_FONT, 11),
                    text_color=CLR_TITLE,
                    fg_color=bg,
                    width=self._DEV_COL_WIDTH,
                    anchor="w",
                )
                lbl.grid(row=idx, column=col_idx,
                         padx=(4, 4), pady=3, sticky="w")
                row_lbls.append(lbl)

            self._row_widgets.append(row_lbls)

    def _show_export_menu(self):
        import tkinter as _tk
        popup = _tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=BG_CARD)

        # Single bordered card — no nested frames so no double-corner artifact
        inner = ctk.CTkFrame(popup, fg_color=BG_CARD, corner_radius=12,
                             border_width=1, border_color="#CBD5E1")
        inner.pack(fill="both", expand=True)

        ctk.CTkLabel(inner, text="EXPORT AS",
                     font=(UI_FONT, 8, "bold"), text_color="#94A3B8",
                     fg_color="transparent").pack(
                     anchor="w", padx=14, pady=(10, 4))

        def _pick(cmd):
            popup.destroy()
            cmd()

        def _bind_row(frame, cmd):
            """Bind click + hover highlight to frame and every descendant."""
            def _on_enter(_):
                frame.configure(fg_color=NAV_HOVER_BG)
                for w in frame.winfo_children():
                    try:
                        w.configure(fg_color=NAV_HOVER_BG)
                    except Exception:
                        pass
            def _on_leave(_):
                frame.configure(fg_color="transparent")
                for w in frame.winfo_children():
                    try:
                        w.configure(fg_color="transparent")
                    except Exception:
                        pass
            def _on_click(_):
                _pick(cmd)
            for widget in [frame] + list(frame.winfo_children()):
                widget.bind("<Enter>",  _on_enter)
                widget.bind("<Leave>",  _on_leave)
                widget.bind("<Button-1>", _on_click)
                try:
                    widget.configure(cursor="hand2")
                except Exception:
                    pass

        for icon, title, sub, cmd in [
            ("\U0001f4c4", "Export CSV", "Comma-separated values", self._export_csv),
            ("\U0001f4c1", "Export Excel", "Workbook with summary + detail", self._export_excel),
            ("\U0001f4cb", "Export PDF", "Formatted report", self._export_pdf),
        ]:
            row = ctk.CTkFrame(inner, fg_color="transparent", corner_radius=8)
            row.pack(fill="x", padx=6, pady=2)

            ctk.CTkLabel(row, text=icon, font=(UI_FONT, 14),
                         fg_color="#EFF6FF", corner_radius=6,
                         width=28, height=28,
                         text_color=NAV_ACTIVE_BG).pack(side="left", padx=(6, 8), pady=4)
            ctk.CTkLabel(row, text=f"{title}  ",
                         font=(UI_FONT, 10, "bold"), text_color=CLR_TITLE,
                         fg_color="transparent", anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=sub,
                         font=(UI_FONT, 9), text_color=CLR_LABEL,
                         fg_color="transparent", anchor="w").pack(side="left")

            _bind_row(row, cmd)

        ctk.CTkFrame(inner, fg_color="#E2E8F0",
                     height=1, corner_radius=0).pack(
                     fill="x", padx=10, pady=(4, 0))
        ctk.CTkLabel(inner, text="Click outside to dismiss",
                     font=(UI_FONT, 8), text_color="#94A3B8",
                     fg_color="transparent").pack(
                     anchor="w", padx=14, pady=(4, 8))

        # Position below the Export button; keep popup fully inside app window.
        ref = self._export_btn
        ref.update_idletasks()
        popup.update_idletasks()
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        app = ref.winfo_toplevel()
        app.update_idletasks()
        app_x = app.winfo_rootx()
        app_y = app.winfo_rooty()
        app_w = app.winfo_width()
        app_h = app.winfo_height()
        x = ref.winfo_rootx()
        y = ref.winfo_rooty() + ref.winfo_height() + 4
        x = max(app_x + 8, min(x, app_x + app_w - w - 8))
        if y + h > app_y + app_h - 8:
            y = max(app_y + 8, ref.winfo_rooty() - h - 4)
        popup.geometry(f"{w}x{h}+{x}+{y}")

        popup.bind("<FocusOut>",
                   lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.focus_force()

    def _get_export_rows(self) -> tuple[list[dict], dict] | tuple[None, None]:
        """Return full filtered rows (not paged) and resolved query params."""
        if not DB_AVAILABLE:
            reason = DB_IMPORT_ERROR or "database modules failed to load"
            self._info_lbl.configure(text=f"DB unavailable: {reason}", text_color=CLR_CRIT)
            return None, None

        try:
            start_dt = self._parse_dt_entry(self._from_entry.get().strip(), is_end=False)
            end_dt = self._parse_dt_entry(self._to_entry.get().strip(), is_end=True)
        except ValueError:
            self._info_lbl.configure(
                text="Invalid date — use YYYY-MM-DD [HH:MM[:SS]]", text_color=CLR_CRIT)
            return None, None

        selected_addrs = sorted(self._selected_addrs)
        selected_device_ids = []
        from db_repository import get_device_by_address
        for addr in selected_addrs:
            dev = get_device_by_address(addr)
            if dev:
                selected_device_ids.append(dev["id"])

        qp = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "selected_addrs": selected_addrs,
            "device_ids": selected_device_ids,
            "all_devices": len(selected_addrs) >= len(self._devices),
        }

        try:
            from db_repository import (get_readings_in_range,
                                       get_all_readings_in_range,
                                       get_readings_for_devices_in_range)
            if qp.get("all_devices", True):
                rows = get_all_readings_in_range(start_dt, end_dt)
            else:
                device_ids = qp.get("device_ids", [])
                if not device_ids:
                    rows = []
                elif len(device_ids) == 1:
                    rows = get_readings_in_range(device_ids[0], start_dt, end_dt)
                    only_addr = qp.get("selected_addrs", [None])[0]
                    only_name = self._devices.get(only_addr, {}).get("device_name", "—")
                    for r in rows:
                        r["_dev_name"] = only_name
                else:
                    rows = get_readings_for_devices_in_range(start_dt, end_dt, device_ids)
        except Exception as e:
            logger.exception("Export rows load error")
            self._info_lbl.configure(text=f"Export error: {e}",
                                     text_color=CLR_CRIT)
            return None, None

        return rows, qp

    def _aggregate_for_export(self, rows: list[dict], start_dt: datetime.datetime) -> list[dict]:
        """Aggregate rows by configured frequency and device for export."""
        freq_label = self._export_freq_var.get()
        freq_map = {
            "Raw": 0,
            "5 min": 5,
            "15 min": 15,
            "30 min": 30,
            "60 min": 60,
        }
        mins = freq_map.get(freq_label, 0)
        if mins <= 0:
            return rows

        bucket_secs = mins * 60
        buckets: dict[tuple[str, datetime.datetime], dict] = {}

        for row in rows:
            rec = row.get("recorded_at")
            try:
                rec_dt = datetime.datetime.strptime(str(rec), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            elapsed = max(0.0, (rec_dt - start_dt).total_seconds())
            bucket_idx = int(elapsed // bucket_secs)
            bucket_start = start_dt + datetime.timedelta(seconds=bucket_idx * bucket_secs)
            bucket_end = bucket_start + datetime.timedelta(seconds=bucket_secs)
            dev_name = row.get("_dev_name", "—")
            key = (dev_name, bucket_end)

            entry = buckets.get(key)
            if entry is None:
                entry = {
                    "_dev_name": dev_name,
                    "recorded_at": bucket_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "concentration_sum": 0.0,
                    "alarm_status": 0,
                    "sample_count": 0,
                }
                buckets[key] = entry

            conc = float(row.get("concentration_value", 0.0) or 0.0)
            alarm = int(row.get("alarm_status", 0) or 0)

            entry["concentration_sum"] += conc
            entry["alarm_status"] = max(entry["alarm_status"], alarm)
            entry["sample_count"] += 1

        out: list[dict] = []
        for entry in buckets.values():
            cnt = max(1, int(entry["sample_count"]))
            out.append({
                "_dev_name": entry["_dev_name"],
                "concentration_value": entry["concentration_sum"] / cnt,
                "alarm_status": entry["alarm_status"],
                "recorded_at": entry["recorded_at"],
            })

        out.sort(key=lambda r: (r.get("recorded_at", ""), r.get("_dev_name", "")))
        return out

    def _export_step_seconds(self) -> int:
        """Return export timeline step (seconds) for the selected export frequency."""
        freq_label = self._export_freq_var.get()
        return {
            "Raw": 60,
            "5 min": 300,
            "15 min": 900,
            "30 min": 1800,
            "60 min": 3600,
        }.get(freq_label, 60)

    def _generate_export_timestamps(self, start_dt: datetime.datetime, end_dt: datetime.datetime) -> list[str]:
        """Generate full export timeline between selected From/To for report downloads."""
        if end_dt < start_dt:
            return []

        step_secs = self._export_step_seconds()
        start_base = start_dt.replace(second=0, microsecond=0)
        end_base = end_dt.replace(second=0, microsecond=0)

        if step_secs <= 60:
            cur = start_base
        else:
            # Aggregated exports are displayed at bucket end labels.
            cur = start_base + datetime.timedelta(seconds=step_secs)

        out: list[str] = []
        while cur <= end_base:
            out.append(cur.strftime("%Y-%m-%d %H:%M:%S"))
            cur += datetime.timedelta(seconds=step_secs)
        return out

    def _export_csv(self):
        rows, qp = self._get_export_rows()
        if rows is None:
            return
        rows = self._aggregate_for_export(rows, qp["start_dt"])

        forced_timestamps = self._generate_export_timestamps(qp["start_dt"], qp["end_dt"])
        device_cols, timestamps, pivot = self._build_export_matrix(
            rows,
            qp.get("selected_addrs", []),
            forced_timestamps=forced_timestamps,
        )

        if not timestamps:
            self._info_lbl.configure(text="No timestamps in selected range", text_color=CLR_WARN)
            return

        try:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
                title="Save Device Logs as CSV")
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8") as f:
                header = ["Timestamp"] + [name for _addr, name, _unit in device_cols]
                writer = csv.writer(f)
                writer.writerow(header)
                for ts in timestamps:
                    row_map = pivot.get(ts, {})
                    out_row = [ts]
                    for _addr, dev_name, _unit in device_cols:
                        val = row_map.get(dev_name)
                        out_row.append(f"{val:.2f}" if val is not None else "NA")
                    writer.writerow(out_row)
            self._info_lbl.configure(
                text=f"Saved: {os.path.basename(path)}", text_color=CLR_SAFE)
        except Exception as e:
            logger.exception("CSV export failed")
            self._info_lbl.configure(text=f"Export error: {e}",
                                     text_color=CLR_CRIT)

    def _export_excel(self):
        rows, qp = self._get_export_rows()
        if rows is None:
            return
        rows = self._aggregate_for_export(rows, qp["start_dt"])

        forced_timestamps = self._generate_export_timestamps(qp["start_dt"], qp["end_dt"])
        device_cols, timestamps, pivot = self._build_export_matrix(
            rows,
            qp.get("selected_addrs", []),
            forced_timestamps=forced_timestamps,
        )

        if not timestamps:
            self._info_lbl.configure(text="No timestamps in selected range", text_color=CLR_WARN)
            return

        try:
            import pandas as pd
            from tkinter import filedialog

            path = filedialog.asksaveasfilename(
                defaultextension=".xlsx",
                filetypes=[("Excel Workbook", "*.xlsx")],
                title="Save Device Logs as Excel")
            if not path:
                return

            detail_header = ["Timestamp"] + [name for _addr, name, _unit in device_cols]
            detail_rows = []
            for ts in timestamps:
                row_map = pivot.get(ts, {})
                row = [ts]
                for _addr, dev_name, _unit in device_cols:
                    val = row_map.get(dev_name)
                    row.append(f"{val:.2f}" if val is not None else "NA")
                detail_rows.append(row)

            summary_rows = self._build_export_summary(device_cols, rows)
            meta_rows = [
                ["Frequency", self._export_freq_var.get()],
                ["From", qp["start_dt"].strftime("%Y-%m-%d %H:%M:%S")],
                ["To", qp["end_dt"].strftime("%Y-%m-%d %H:%M:%S")],
                ["Devices", ", ".join([name for _addr, name, _unit in device_cols]) or "All"],
            ]

            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                pd.DataFrame(meta_rows, columns=["Field", "Value"]).to_excel(
                    writer, index=False, sheet_name="Report Info")
                pd.DataFrame(summary_rows, columns=["Device", "Units", "Min", "Max", "Average"]).to_excel(
                    writer, index=False, sheet_name="Summary")
                pd.DataFrame(detail_rows, columns=detail_header).to_excel(
                    writer, index=False, sheet_name="Detail")

            self._info_lbl.configure(
                text=f"Saved: {os.path.basename(path)}", text_color=CLR_SAFE)
        except Exception as e:
            logger.exception("Excel export failed")
            self._info_lbl.configure(text=f"Export error: {e}", text_color=CLR_CRIT)

    def _build_export_matrix(
        self,
        rows: list[dict],
        selected_addrs: list[int],
        forced_timestamps: list[str] | None = None,
    ) -> tuple[list[tuple[int, str, str]], list[str], dict[str, dict[str, float]]]:
        """Return (device_columns, timestamps, pivot) for export/report rendering."""
        device_cols: list[tuple[int, str, str]] = []
        for addr in selected_addrs:
            meta = self._devices.get(addr)
            if not meta:
                continue
            device_cols.append((
                int(addr),
                str(meta.get("device_name", f"Device {addr:03d}")),
                str(meta.get("gas_unit", "ppm")),
            ))

        if forced_timestamps is not None:
            timestamps = list(forced_timestamps)
        else:
            timestamps = sorted({str(r.get("recorded_at", "")) for r in rows if r.get("recorded_at")})
        pivot: dict[str, dict[str, float]] = {ts: {} for ts in timestamps}
        name_by_addr = {int(addr): name for addr, name, _ in device_cols}

        for row in rows:
            ts = str(row.get("recorded_at", ""))
            if ts not in pivot:
                continue
            dev_name = str(row.get("_dev_name", "") or "")
            if not dev_name:
                continue
            try:
                pivot[ts][dev_name] = float(row.get("concentration_value") or 0.0)
            except Exception:
                continue

        # Keep only requested device order if names were injected from DB rows.
        ordered_pivot: dict[str, dict[str, float]] = {}
        for ts in timestamps:
            ordered_pivot[ts] = {}
            for _addr, dev_name, _unit in device_cols:
                if dev_name in pivot[ts]:
                    ordered_pivot[ts][dev_name] = pivot[ts][dev_name]

        return device_cols, timestamps, ordered_pivot

    def _build_export_summary(self, device_cols: list[tuple[int, str, str]], rows: list[dict]) -> list[list[str]]:
        """Build summary rows: Device, Units, Min, Max, Average."""
        values_by_name: dict[str, list[float]] = {name: [] for _addr, name, _unit in device_cols}
        unit_by_name: dict[str, str] = {name: unit for _addr, name, unit in device_cols}

        for row in rows:
            name = str(row.get("_dev_name", "") or "")
            if name not in values_by_name:
                continue
            try:
                values_by_name[name].append(float(row.get("concentration_value") or 0.0))
            except Exception:
                continue

        summary_rows: list[list[str]] = []
        for _addr, name, unit in device_cols:
            vals = values_by_name.get(name, [])
            if vals:
                mn = min(vals)
                mx = max(vals)
                avg = sum(vals) / len(vals)
                summary_rows.append([
                    name,
                    unit_by_name.get(name, unit),
                    f"{mn:.2f}",
                    f"{mx:.2f}",
                    f"{avg:.2f}",
                ])
            else:
                summary_rows.append([name, unit_by_name.get(name, unit), "—", "—", "—"])

        return summary_rows

    def _export_pdf(self):
        try:
            from tkinter import filedialog

            raw_rows, qp = self._get_export_rows()
            if raw_rows is None:
                return

            freq_label = self._export_freq_var.get()
            freq_str = "Raw" if freq_label == "Raw" else freq_label
            forced_timestamps = self._generate_export_timestamps(qp["start_dt"], qp["end_dt"])

            device_cols, _ts, _pivot = self._build_export_matrix(
                raw_rows,
                qp.get("selected_addrs", []),
                forced_timestamps=forced_timestamps,
            )

            path = filedialog.asksaveasfilename(
                defaultextension=".pdf",
                filetypes=[("PDF files", "*.pdf")],
                title="Save Device Logs as PDF")
            if not path:
                return

            pdf_bytes = build_device_logs_report_pdf_bytes(
                raw_rows,
                frequency=freq_str,
                report_range=(
                    qp["start_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                    qp["end_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                ),
                forced_timestamps=forced_timestamps,
                forced_device_names=[name for _addr, name, _unit in device_cols],
            )
            with open(path, "wb") as f:
                f.write(pdf_bytes)
            self._info_lbl.configure(
                text=f"PDF saved: {os.path.basename(path)}",
                text_color=CLR_SAFE)
        except ImportError:
            self._info_lbl.configure(
                text="PDF requires: pip install reportlab",
                text_color=CLR_WARN)
        except Exception as e:
            logger.exception("PDF export failed")
            self._info_lbl.configure(text=f"Export error: {e}",
                                     text_color=CLR_CRIT)


# =============================================================================
# Historic Trend View
# =============================================================================
class HistoricTrendView(ctk.CTkFrame):
    def __init__(self, parent, devices: list):
        super().__init__(parent, fg_color="transparent")
        self._devices = {d["address"]: d for d in devices}
        self._selected_addrs: set[int] = set(self._devices.keys())
        self._color_map: dict[int, str] = {
            addr: _LINE_PALETTE[i % len(_LINE_PALETTE)]
            for i, addr in enumerate(sorted(self._devices.keys()))
        }
        self._lines: dict[int, object] = {}
        self._hover_last_key = None
        self._hover_last_ts = 0.0
        self._build()

    def _build(self):
        # Filter bar (same interaction style as Device Logs)
        fbar = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=12,
                            border_width=0)
        fbar.pack(fill="x", padx=4, pady=(8, 6))

        inner = ctk.CTkFrame(fbar, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(inner, text="Devices:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._dev_var = ctk.StringVar(value="All Devices")
        self._dev_btn = ctk.CTkButton(
            inner,
            textvariable=self._dev_var,
            width=130, height=30,
            corner_radius=6,
            font=FONT_FILTER_LABEL,
            fg_color="white", text_color=CLR_TITLE,
            hover_color="#F1F5F9",
            border_width=1, border_color="#CBD5E1",
            anchor="w",
            command=self._show_device_dropdown)
        self._dev_btn.pack(side="left", padx=(4, 16))

        now_dt = datetime.datetime.now().replace(second=0, microsecond=0)
        from_dt = now_dt.replace(hour=0, minute=0)
        to_dt = now_dt.replace(hour=23, minute=59)

        ctk.CTkLabel(inner, text="From:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._from_entry = ctk.CTkEntry(
            inner, width=145, font=FONT_FILTER_LABEL,
            placeholder_text="YYYY-MM-DD HH:MM")
        self._from_entry.insert(0, from_dt.strftime("%Y-%m-%d %H:%M"))
        self._from_entry.pack(side="left", padx=(4, 2))

        self._cal_from_btn = ctk.CTkButton(
            inner, text="📅", width=28, height=28,
            corner_radius=6, font=(UI_FONT, 11),
            fg_color=BG_PILL, text_color=NAV_ACTIVE_BG,
            hover_color="#8B9FE8",
            command=self._open_from_cal)
        self._cal_from_btn.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(inner, text="To:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left")
        self._to_entry = ctk.CTkEntry(
            inner, width=145, font=FONT_FILTER_LABEL,
            placeholder_text="YYYY-MM-DD HH:MM")
        self._to_entry.insert(0, to_dt.strftime("%Y-%m-%d %H:%M"))
        self._to_entry.pack(side="left", padx=(4, 2))

        self._cal_to_btn = ctk.CTkButton(
            inner, text="📅", width=28, height=28,
            corner_radius=6, font=(UI_FONT, 11),
            fg_color=BG_PILL, text_color=NAV_ACTIVE_BG,
            hover_color="#8B9FE8",
            command=self._open_to_cal)
        self._cal_to_btn.pack(side="left", padx=(0, 10))

        ctk.CTkButton(inner, text="Fetch", width=76, height=32,
                      corner_radius=8, font=(UI_FONT, 11, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=self._fetch).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(inner, text="Avg by:", font=FONT_FILTER_LABEL,
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left", padx=(0, 4))
        self._agg_var = ctk.StringVar(value="None")
        ctk.CTkOptionMenu(
            inner,
            values=["None", "1 min", "5 min", "15 min", "30 min", "1 hour"],
            variable=self._agg_var,
            width=88,
            height=30,
            corner_radius=8,
            font=FONT_FILTER_LABEL,
        ).pack(side="left", padx=(0, 12))

        self._export_btn = ctk.CTkButton(
            inner, text="Export ▾", width=92, height=32,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color=CLR_SAFE, hover_color="#15803D",
            command=self._show_hist_export_menu,
        )
        self._export_btn.pack(side="right", padx=(0, 10))

        self._info_lbl = ctk.CTkLabel(inner, text="", font=(UI_FONT, 9),
                                      text_color=CLR_LABEL,
                                      fg_color="transparent")
        self._info_lbl.pack(side="right")

        self._loaded_lbl = ctk.CTkLabel(
            fbar,
            text="",
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_LABEL,
            fg_color="transparent",
        )
        self._loaded_lbl.pack(fill="x", padx=14, pady=(0, 8), anchor="w")

        # Chart card
        card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=16,
                            border_width=0)
        card.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        self._fig = Figure(facecolor=BG_CARD, dpi=96)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_facecolor("#F4F5FF")
        self._ax.set_xlabel("Time", fontsize=10, color="#0369A1", fontweight="bold")
        self._ax.set_ylabel("Concentration", fontsize=10, color="#0F766E", fontweight="bold")
        self._ax.tick_params(colors=CLR_LABEL, labelsize=8)
        self._ax.grid(True, color=CLR_CARD_BDR, linewidth=0.5,
                      linestyle="--", alpha=0.6)
        for sp in self._ax.spines.values():
            sp.set_color(CLR_CARD_BDR)
        self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=0.14)

        for addr, clr in self._color_map.items():
            meta = self._devices[addr]
            dev_label = meta.get("device_name", f"Device {addr:03d}")
            lbl = f"{dev_label} ({meta['gas_name']})"
            line, = self._ax.plot([], [], color=clr, linewidth=1.8,
                                  solid_capstyle="round", label=lbl)
            self._lines[addr] = line

        self._render_legend()

        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=card)
        self._mpl_canvas.get_tk_widget().pack(fill="both", expand=True,
                                              padx=8, pady=8)
        self._mpl_canvas.draw()

        # Hover tooltip (same interaction style as Live Trend)
        self._hover_ann = self._ax.annotate(
            "",
            xy=(0, 0), xytext=(12, 12), textcoords="offset points",
            fontsize=8,
            color=CLR_TITLE,
            bbox=dict(boxstyle="round,pad=0.3", fc="#FFFFFF",
                      ec="#CBD5E1", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#94A3B8", lw=0.8),
        )
        self._hover_ann.set_visible(False)
        self._hover_dot, = self._ax.plot([], [], marker="o", linestyle="",
                                         markersize=5, color=CLR_TITLE,
                                         zorder=5)
        self._hover_dot.set_visible(False)

        self._mpl_canvas.mpl_connect("motion_notify_event", self._on_hover_move)
        self._mpl_canvas.mpl_connect("figure_leave_event", self._on_hover_leave)

        self._refresh_device_filter_text()

    def _render_legend(self):
        canvas_ready = hasattr(self, "_mpl_canvas") and self._mpl_canvas is not None
        visible_lines = [ln for ln in self._lines.values() if ln.get_visible()]
        if not visible_lines:
            if self._ax.legend_ is not None:
                self._ax.legend_.remove()
            self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=0.14)
            if canvas_ready:
                self._mpl_canvas.draw_idle()
            return

        labels = [str(ln.get_label() or "") for ln in visible_lines]
        fig_w_px = max(240, int(self._fig.get_size_inches()[0] * self._fig.dpi * 0.90))

        est_label_widths = [max(96, int(len(lbl) * 6.4) + 36) for lbl in labels]
        total_w = sum(est_label_widths)

        rows = max(1, (total_w + fig_w_px - 1) // fig_w_px)
        rows = min(rows, max(1, len(labels)))
        ncol = max(1, (len(labels) + rows - 1) // rows)

        bottom = min(0.46, 0.24 + max(0, rows - 1) * 0.07)
        self._fig.subplots_adjust(left=0.07, right=0.97, top=0.95, bottom=bottom)

        self._ax.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.16),
            ncol=ncol,
            fontsize=8,
            framealpha=0.85,
            edgecolor=CLR_CARD_BDR,
            columnspacing=1.1,
            handlelength=2.0,
            handletextpad=0.5,
            borderpad=0.4,
        )
        if canvas_ready:
            self._mpl_canvas.draw_idle()

    def _hide_hover(self):
        changed = False
        if self._hover_ann.get_visible():
            self._hover_ann.set_visible(False)
            changed = True
        if self._hover_dot.get_visible():
            self._hover_dot.set_visible(False)
            changed = True
        if changed:
            self._mpl_canvas.draw_idle()

    def _on_hover_leave(self, _event):
        self._hover_last_key = None
        self._hide_hover()

    def _on_hover_move(self, event):
        if event.inaxes != self._ax or event.xdata is None:
            self._hover_last_key = None
            self._hide_hover()
            return

        now = time.monotonic()
        if now - self._hover_last_ts < (1.0 / 45.0):
            return
        self._hover_last_ts = now

        best = None
        best_dist2 = float("inf")

        for addr, line in self._lines.items():
            if not line.get_visible():
                continue
            xs = line.get_xdata()
            ys = line.get_ydata()
            if len(xs) == 0:
                continue

            xs_list = xs.tolist() if hasattr(xs, "tolist") else list(xs)
            idx = bisect.bisect_left(xs_list, event.xdata)
            for cand in (idx - 1, idx):
                if 0 <= cand < len(xs_list):
                    px, py = self._ax.transData.transform((xs_list[cand], ys[cand]))
                    dx = px - event.x
                    dy = py - event.y
                    dist2 = dx * dx + dy * dy
                    if dist2 < best_dist2:
                        best_dist2 = dist2
                        best = (addr, cand, xs_list[cand], ys[cand], line.get_color())

        if best is None or best_dist2 > (14.0 * 14.0):
            self._hover_last_key = None
            self._hide_hover()
            return

        addr, idx, xval, yval, clr = best
        key = (addr, idx)
        if key == self._hover_last_key and self._hover_ann.get_visible():
            return
        self._hover_last_key = key

        meta = self._devices.get(addr, {})
        dev_label = meta.get("device_name", f"Device {addr:03d}")
        unit = meta.get("gas_unit", "")
        ts_txt = mdates.num2date(xval).strftime("%Y-%m-%d %H:%M:%S")
        val_txt = f"{yval:.2f} {unit}".strip()

        self._hover_ann.xy = (xval, yval)
        self._hover_ann.set_text(f"{dev_label}\n{val_txt}\n{ts_txt}")
        self._hover_ann.set_visible(True)

        self._hover_dot.set_data([xval], [yval])
        self._hover_dot.set_color(clr)
        self._hover_dot.set_visible(True)

        self._mpl_canvas.draw_idle()

    def _refresh_device_filter_text(self):
        total = len(self._devices)
        chosen = len(self._selected_addrs)
        if chosen >= total:
            self._dev_var.set("All Devices")
        else:
            self._dev_var.set(f"Chosen ({chosen})")

    def _show_device_dropdown(self):
        import tkinter as _tk
        popup = _tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="white")

        frame = ctk.CTkScrollableFrame(
            popup, fg_color="white", corner_radius=8,
            border_width=1, border_color="#CBD5E1",
            height=min(260, max(6, len(self._devices) + 2) * 34))
        frame.pack(fill="both", expand=True)

        ctk.CTkLabel(
            frame, text="SHOW DEVICES", font=(UI_FONT, 8, "bold"),
            text_color="#94A3B8", fg_color="transparent", anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 4))

        all_var = ctk.BooleanVar(value=(len(self._selected_addrs) == len(self._devices)))
        item_vars: dict[int, ctk.BooleanVar] = {
            addr: ctk.BooleanVar(value=(addr in self._selected_addrs))
            for addr in sorted(self._devices.keys())
        }

        def _toggle_all():
            state = all_var.get()
            for v in item_vars.values():
                v.set(state)

        ctk.CTkCheckBox(
            frame, text="All Devices", variable=all_var,
            font=(UI_FONT, 10, "bold"), text_color=CLR_TITLE,
            fg_color=NAV_ACTIVE_BG, hover_color="#0284C7",
            checkmark_color="#FFFFFF", border_width=2,
            command=_toggle_all,
        ).pack(anchor="w", padx=14, pady=(2, 6))

        ctk.CTkFrame(frame, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=10)

        for addr in sorted(self._devices.keys()):
            meta = self._devices[addr]
            line_clr = self._color_map[addr]
            dev_label = meta.get("device_name", f"Device {addr:03d}")
            ctk.CTkCheckBox(
                frame,
                text=f"{dev_label}  ({meta['gas_name']})",
                variable=item_vars[addr],
                font=(UI_FONT, 9),
                text_color=CLR_TITLE,
                fg_color=line_clr,
                hover_color=line_clr,
                checkmark_color="#FFFFFF", border_width=2,
                command=lambda: all_var.set(all(v.get() for v in item_vars.values())),
            ).pack(anchor="w", padx=14, pady=3)

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(8, 6))

        def _apply():
            chosen = {addr for addr, var in item_vars.items() if var.get()}
            self._selected_addrs = chosen if chosen else set(self._devices.keys())
            self._refresh_device_filter_text()
            self._apply_visibility()
            popup.destroy()

        ctk.CTkButton(
            actions, text="Apply", width=70, height=28,
            corner_radius=6, font=(UI_FONT, 10, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=_apply,
        ).pack(side="right")

        ctk.CTkFrame(frame, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(frame, text="Click outside to close",
                     font=(UI_FONT, 8), text_color="#94A3B8",
                     fg_color="transparent").pack(
                     anchor="w", padx=14, pady=(4, 8))

        self._dev_btn.update_idletasks()
        popup.update_idletasks()
        x = self._dev_btn.winfo_rootx()
        y = self._dev_btn.winfo_rooty() + self._dev_btn.winfo_height() + 2
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        popup.geometry(f"{w}x{h}+{x}+{y}")
        popup.bind("<FocusOut>",
                   lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.deiconify()
        popup.focus_force()

    def _open_from_cal(self):
        try:
            dt = self._parse_dt_entry(self._from_entry.get().strip(), is_end=False)
        except ValueError:
            dt = datetime.datetime.now().replace(second=0, microsecond=0)

        def _set(val):
            self._from_entry.delete(0, "end")
            self._from_entry.insert(0, val)
        _CalendarPopup(self._cal_from_btn, dt, _set)

    def _open_to_cal(self):
        try:
            dt = self._parse_dt_entry(self._to_entry.get().strip(), is_end=True)
        except ValueError:
            dt = datetime.datetime.now().replace(
                hour=23, minute=59, second=0, microsecond=0)

        def _set(val):
            self._to_entry.delete(0, "end")
            self._to_entry.insert(0, val)
        _CalendarPopup(self._cal_to_btn, dt, _set)

    def _parse_dt_entry(self, value: str, is_end: bool) -> datetime.datetime:
        txt = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(txt, fmt)
                if fmt == "%Y-%m-%d":
                    if is_end:
                        return dt.replace(hour=23, minute=59, second=59)
                    return dt.replace(hour=0, minute=0, second=0)
                if fmt == "%Y-%m-%d %H:%M" and is_end:
                    return dt.replace(second=59)
                return dt
            except ValueError:
                continue
        raise ValueError("Invalid date format")

    def _apply_visibility(self):
        self._hover_last_key = None
        self._hide_hover()
        for addr, line in self._lines.items():
            line.set_visible(addr in self._selected_addrs)
        self._render_legend()

    def _has_chart_data(self) -> bool:
        for line in self._lines.values():
            if not line.get_visible():
                continue
            xs = line.get_xdata()
            if len(xs) > 0:
                return True
        return False

    def _show_hist_export_menu(self):
        import tkinter as _tk

        popup = _tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=BG_CARD)

        inner = ctk.CTkFrame(
            popup,
            fg_color=BG_CARD,
            corner_radius=12,
            border_width=1,
            border_color="#CBD5E1",
        )
        inner.pack(fill="both", expand=True)

        ctk.CTkLabel(
            inner,
            text="EXPORT AS",
            font=(UI_FONT, 8, "bold"),
            text_color="#94A3B8",
            fg_color="transparent",
        ).pack(anchor="w", padx=14, pady=(10, 4))

        def _pick(cmd):
            popup.destroy()
            cmd()

        def _add_row(icon: str, title: str, sub: str, cmd):
            row = ctk.CTkFrame(inner, fg_color="transparent", corner_radius=8)
            row.pack(fill="x", padx=6, pady=2)

            ctk.CTkLabel(
                row,
                text=icon,
                font=(UI_FONT, 14),
                fg_color="#EFF6FF",
                corner_radius=6,
                width=28,
                height=28,
                text_color=NAV_ACTIVE_BG,
            ).pack(side="left", padx=(6, 8), pady=4)
            ctk.CTkLabel(
                row,
                text=f"{title}  ",
                font=(UI_FONT, 10, "bold"),
                text_color=CLR_TITLE,
                fg_color="transparent",
                anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text=sub,
                font=(UI_FONT, 9),
                text_color=CLR_LABEL,
                fg_color="transparent",
                anchor="w",
            ).pack(side="left")

            def _on_enter(_e):
                row.configure(fg_color=NAV_HOVER_BG)

            def _on_leave(_e):
                row.configure(fg_color="transparent")

            def _on_click(_e):
                _pick(cmd)

            for widget in [row] + list(row.winfo_children()):
                widget.bind("<Enter>", _on_enter)
                widget.bind("<Leave>", _on_leave)
                widget.bind("<Button-1>", _on_click)
                try:
                    widget.configure(cursor="hand2")
                except Exception:
                    pass

        _add_row("🖼", "Export PNG", "Chart image", self._export_png)
        _add_row("📄", "Export PDF", "Chart report", self._export_pdf)

        ctk.CTkFrame(inner, fg_color="#E2E8F0", height=1, corner_radius=0).pack(
            fill="x", padx=10, pady=(4, 0)
        )
        ctk.CTkLabel(
            inner,
            text="Click outside to dismiss",
            font=(UI_FONT, 8),
            text_color="#94A3B8",
            fg_color="transparent",
        ).pack(anchor="w", padx=14, pady=(4, 8))

        ref = self._export_btn
        ref.update_idletasks()
        popup.update_idletasks()
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        app = ref.winfo_toplevel()
        app.update_idletasks()
        app_x = app.winfo_rootx()
        app_y = app.winfo_rooty()
        app_w = app.winfo_width()
        app_h = app.winfo_height()
        x = ref.winfo_rootx()
        y = ref.winfo_rooty() + ref.winfo_height() + 4
        x = max(app_x + 8, min(x, app_x + app_w - w - 8))
        if y + h > app_y + app_h - 8:
            y = max(app_y + 8, ref.winfo_rooty() - h - 4)
        popup.geometry(f"{w}x{h}+{x}+{y}")
        popup.bind("<FocusOut>", lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.focus_force()

    def _export_png(self):
        if not self._has_chart_data():
            self._info_lbl.configure(
                text="No chart data to export. Click Fetch first.",
                text_color=CLR_WARN,
            )
            return

        try:
            from tkinter import filedialog
            default_name = f"historic_trend_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = filedialog.asksaveasfilename(
                defaultextension=".png",
                filetypes=[("PNG files", "*.png")],
                initialfile=default_name,
                title="Save Historic Trend as PNG",
            )
            if not path:
                return

            self._fig.savefig(
                path,
                dpi=180,
                bbox_inches="tight",
                facecolor=self._fig.get_facecolor(),
            )
            self._info_lbl.configure(
                text=f"Saved chart: {os.path.basename(path)}",
                text_color=CLR_SAFE,
            )
        except Exception as e:
            logger.exception("Historic Trend PNG export failed")
            self._info_lbl.configure(
                text=f"Export error: {e}",
                text_color=CLR_CRIT,
            )

    def _export_pdf(self):
        if not self._has_chart_data():
            self._info_lbl.configure(
                text="No chart data to export. Click Fetch first.",
                text_color=CLR_WARN,
            )
            return

        try:
            from tkinter import filedialog
            from io import BytesIO
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.enums import TA_CENTER
            from reportlab.platypus import (
                SimpleDocTemplate,
                Table,
                TableStyle,
                Paragraph,
                Spacer,
                HRFlowable,
                Image as RLImage,
            )

            default_name = f"historic_trend_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            path = filedialog.asksaveasfilename(
                defaultextension=".pdf",
                filetypes=[("PDF files", "*.pdf")],
                initialfile=default_name,
                title="Save Historic Trend as PDF",
            )
            if not path:
                return

            try:
                from db_repository import get_all_plants
                plants = get_all_plants()
                plant = plants[0] if plants else {}
            except Exception:
                plant = {}

            agg_label = self._agg_var.get()
            selected_count = len(self._selected_addrs)
            freq_txt = {"None": "Raw", "1 hour": "60 min"}.get(agg_label, agg_label)
            company_name = str(plant.get("company_name", "—") or "—")
            location = str(plant.get("location", "—") or "—")

            # Render current chart to in-memory PNG for embedding in PDF.
            png_bytes = BytesIO()
            self._fig.savefig(
                png_bytes,
                format="png",
                dpi=170,
                bbox_inches="tight",
                facecolor=self._fig.get_facecolor(),
            )
            png_bytes.seek(0)

            doc = SimpleDocTemplate(
                path,
                pagesize=landscape(A4),
                leftMargin=24,
                rightMargin=24,
                topMargin=20,
                bottomMargin=20,
                title="Historic Trend Report",
            )
            width, _height = landscape(A4)

            styles = getSampleStyleSheet()
            styles.add(
                ParagraphStyle(
                    "HistRptTitle",
                    parent=styles["Heading1"],
                    fontName="Helvetica-Bold",
                    fontSize=18,
                    alignment=TA_CENTER,
                    textColor=colors.HexColor("#0C4A6E"),
                    spaceAfter=6,
                )
            )

            story: list = []
            story.append(Paragraph("H2 Detector Historic Trend Report", styles["HistRptTitle"]))
            story.append(HRFlowable(color=colors.HexColor("#BFDBFE"), thickness=1.2))
            story.append(Spacer(1, 8))

            generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            range_txt = f"{self._from_entry.get().strip()}  to  {self._to_entry.get().strip()}"
            meta_rows = [
                [Paragraph("<b>Company Name</b>", styles["BodyText"]), Paragraph(company_name, styles["BodyText"])],
                [Paragraph("<b>Location</b>", styles["BodyText"]), Paragraph(location, styles["BodyText"])],
                [Paragraph("<b>Report Extracted Time</b>", styles["BodyText"]), Paragraph(generated_at, styles["BodyText"])],
                [Paragraph("<b>Report Range</b>", styles["BodyText"]), Paragraph(range_txt, styles["BodyText"])],
                [Paragraph("<b>Frequency</b>", styles["BodyText"]), Paragraph(freq_txt, styles["BodyText"])],
                [Paragraph("<b>Visible Devices</b>", styles["BodyText"]), Paragraph(str(selected_count), styles["BodyText"])],
            ]
            meta_tbl = Table(meta_rows, colWidths=[(width - 48) * 0.28, (width - 48) * 0.72])
            meta_tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(meta_tbl)
            story.append(Spacer(1, 10))

            chart_w = width - 60
            chart_h = 290
            chart_img = RLImage(png_bytes, width=chart_w, height=chart_h)
            story.append(chart_img)

            doc.build(story)

            self._info_lbl.configure(
                text=f"Saved report: {os.path.basename(path)}",
                text_color=CLR_SAFE,
            )
        except Exception as e:
            logger.exception("Historic Trend PDF export failed")
            self._info_lbl.configure(
                text=f"Export error: {e}",
                text_color=CLR_CRIT,
            )

    def _fetch(self):
        if not DB_AVAILABLE:
            self._info_lbl.configure(text="DB unavailable", text_color=CLR_CRIT)
            return

        try:
            start_dt = self._parse_dt_entry(self._from_entry.get().strip(), is_end=False)
            end_dt = self._parse_dt_entry(self._to_entry.get().strip(), is_end=True)
        except ValueError:
            self._info_lbl.configure(
                text="Invalid date — use YYYY-MM-DD [HH:MM[:SS]]", text_color=CLR_CRIT)
            return

        from db_repository import (get_all_readings_in_range,
                                   get_device_by_address,
                                   get_readings_for_devices_in_range,
                                   get_readings_in_range)

        selected_addrs = sorted(self._selected_addrs)
        selected_device_ids = []
        id_to_addr: dict[int, int] = {}

        for addr in sorted(self._devices.keys()):
            dev = get_device_by_address(addr)
            if dev:
                id_to_addr[int(dev["id"])] = int(addr)
                if addr in self._selected_addrs:
                    selected_device_ids.append(int(dev["id"]))

        try:
            if len(selected_addrs) >= len(self._devices):
                rows = get_all_readings_in_range(start_dt, end_dt)
            elif len(selected_device_ids) == 1:
                rows = get_readings_in_range(selected_device_ids[0], start_dt, end_dt)
            else:
                rows = get_readings_for_devices_in_range(start_dt, end_dt, selected_device_ids)
        except Exception as e:
            logger.exception("Historic trend fetch failed")
            self._info_lbl.configure(text=f"Error: {e}", text_color=CLR_CRIT)
            self._loaded_lbl.configure(text="", text_color=CLR_LABEL)
            return

        # Reset existing plotted data
        for line in self._lines.values():
            line.set_xdata([])
            line.set_ydata([])

        per_addr_points: dict[int, list[tuple[float, float]]] = {
            addr: [] for addr in self._devices.keys()
        }

        for row in rows:
            dev_id = row.get("device_id")
            if dev_id is None:
                continue
            addr = id_to_addr.get(int(dev_id))
            if addr is None or addr not in per_addr_points:
                continue

            raw_ts = str(row.get("recorded_at", "")).replace("T", " ")
            try:
                ts = datetime.datetime.fromisoformat(raw_ts)
            except Exception:
                try:
                    ts = datetime.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

            value = float(row.get("concentration_value") or 0.0)
            per_addr_points[addr].append((ts, value))

        # Aggregation
        agg_label = self._agg_var.get()
        agg_secs = {"None": 0, "1 min": 60, "5 min": 300,
                    "15 min": 900, "30 min": 1800, "1 hour": 3600}.get(agg_label, 0)

        for addr, points in per_addr_points.items():
            if not points:
                continue
            if agg_secs > 0:
                # Group into fixed-width time buckets, average concentration
                buckets: dict[int, list[float]] = {}
                for ts, val in points:
                    epoch = int(ts.timestamp())
                    bucket_key = (epoch // agg_secs) * agg_secs
                    buckets.setdefault(bucket_key, []).append(val)
                # Plot bucket averages at bucket END (right edge), so a value at
                # 13:45 represents aggregation over the preceding window.
                xs = [
                    mdates.date2num(datetime.datetime.fromtimestamp(k + agg_secs))
                    for k in sorted(buckets)
                ]
                ys = [sum(v) / len(v) for k, v in sorted(buckets.items())]
            else:
                xs = [mdates.date2num(ts) for ts, _ in points]
                ys = [val for _, val in points]
            self._lines[addr].set_xdata(xs)
            self._lines[addr].set_ydata(ys)

        self._apply_visibility()
        self._ax.relim()
        self._ax.autoscale_view()
        # Show date + time on x-axis
        self._ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
        self._ax.xaxis.set_major_locator(
            mdates.AutoDateLocator(minticks=3, maxticks=7))
        self._render_legend()
        self._fig.autofmt_xdate(rotation=30, ha="right")
        self._mpl_canvas.draw_idle()

        agg_note = f" · avg/{agg_label}" if agg_secs > 0 else ""
        self._loaded_lbl.configure(
            text=f"Loaded {len(rows)} record(s){agg_note}",
            text_color=CLR_SAFE if rows else CLR_WARN)

    def update_device_name(self, addr: int, new_name: str):
        if addr not in self._devices:
            return
        self._devices[addr]["device_name"] = new_name
        if addr in self._lines:
            meta = self._devices[addr]
            self._lines[addr].set_label(f"{new_name} ({meta['gas_name']})")
            self._render_legend()

    def add_device(self, info: dict):
        """Register a newly discovered device at runtime."""
        addr = info.get("address")
        if addr is None:
            return
        addr = int(addr)
        if addr in self._devices:
            return

        self._devices[addr] = info
        self._selected_addrs.add(addr)
        self._color_map[addr] = _LINE_PALETTE[len(self._color_map) % len(_LINE_PALETTE)]

        dev_label = info.get("device_name", f"Device {addr:03d}")
        lbl = f"{dev_label} ({info.get('gas_name', 'H2')})"
        line, = self._ax.plot([], [], color=self._color_map[addr], linewidth=1.8,
                              solid_capstyle="round", label=lbl)
        self._lines[addr] = line

        self._apply_visibility()
        self._refresh_device_filter_text()


# =============================================================================
# Analytics View  (Live Trend + Historic Trend + Device Logs segmented)
# =============================================================================
class AnalyticsView(ctk.CTkFrame):

    _TABS = [
        ("\U0001F4C8", "LIVE TREND", "Live Trend"),
        ("\U0001F4CA", "HISTORIC TREND", "Historic Trend"),
        ("\U0001F4D1", "DEVICE LOGS", "Device Logs"),
    ]

    def __init__(self, parent, devices: list):
        super().__init__(parent, fg_color="transparent")
        self._devices = devices

        # Rounded shell + inset card for visual consistency
        shell = ctk.CTkFrame(self, fg_color=BG_APP, corner_radius=18, border_width=0)
        shell.pack(fill="both", expand=True, padx=48, pady=24)

        outer = ctk.CTkFrame(shell, fg_color=BG_CARD, corner_radius=15, border_width=1, border_color="#C7D9EE")
        outer.pack(fill="both", expand=True, padx=3, pady=3)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        # Tab header strip
        tab_strip = ctk.CTkFrame(outer, fg_color="#F0F4F8", corner_radius=12, height=54, border_width=0)
        tab_strip.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        tab_strip.grid_propagate(False)
        for i in range(len(self._TABS)):
            tab_strip.columnconfigure(i, weight=1)

        self._tab_btns = {}
        self._tab_indicators = {}
        self._tab_cells = {}

        for col, (icon, label, key) in enumerate(self._TABS):
            cell = ctk.CTkFrame(tab_strip, fg_color="#F0F4F8", corner_radius=0, border_width=0)
            cell.grid(row=0, column=col, sticky="nsew")
            cell.rowconfigure(0, weight=1)
            cell.rowconfigure(1, minsize=3)
            cell.columnconfigure(0, weight=1)

            btn = ctk.CTkButton(
                cell,
                text=f"  {icon}  {label}",
                anchor="center",
                height=50,
                corner_radius=0,
                border_width=0,
                font=(UI_FONT, 12, "bold"),
                fg_color="transparent",
                text_color=CLR_LABEL,
                hover_color="#E2EBF6",
                command=lambda k=key: self._switch_tab(k),
            )
            btn.grid(row=0, column=0, sticky="nsew")

            indicator = ctk.CTkFrame(cell, fg_color="#F0F4F8", height=3, corner_radius=0, border_width=0)
            indicator.grid(row=1, column=0, sticky="ew")

            self._tab_btns[key] = btn
            self._tab_indicators[key] = indicator
            self._tab_cells[key] = cell

        # Content area
        self._content = ctk.CTkFrame(outer, fg_color="transparent", corner_radius=0)
        self._content.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # Build tab frames
        self._live_trend_frame = TrendGraphView(self._content, devices)
        self._historic_trend_frame = HistoricTrendView(self._content, devices)
        self._logs_frame = DeviceLogsView(self._content, devices)

        self._switch_tab("Live Trend")

    def _switch_tab(self, name: str):
        for key, btn in self._tab_btns.items():
            active = (key == name)
            btn.configure(
                text_color=NAV_ACTIVE_BG if active else CLR_LABEL,
                font=(UI_FONT, 12, "bold"),
                fg_color="#FFFFFF" if active else "transparent",
            )
            self._tab_cells[key].configure(fg_color="#FFFFFF" if active else "#F0F4F8")
            self._tab_indicators[key].configure(fg_color=NAV_ACTIVE_BG if active else "#C7D9EE")

        self._live_trend_frame.pack_forget()
        self._historic_trend_frame.pack_forget()
        self._logs_frame.pack_forget()
        if name == "Live Trend":
            self._live_trend_frame.pack(fill="both", expand=True)
        elif name == "Historic Trend":
            self._historic_trend_frame.pack(fill="both", expand=True)
        elif name == "Device Logs":
            self._logs_frame.pack(fill="both", expand=True)

    # Backward compatibility for update_device_name, push_reading, redraw_trend
    def update_device_name(self, addr: int, new_name: str):
        self._live_trend_frame.update_device_name(addr, new_name)
        self._historic_trend_frame.update_device_name(addr, new_name)
        self._logs_frame.update_device_name(addr, new_name)

    def push_reading(self, addr: int, ts: datetime.datetime, value: float):
        self._live_trend_frame.push_reading(addr, ts, value)

    def add_device(self, info: dict):
        addr = int(info.get("address", 0) or 0)
        if addr <= 0:
            return
        if all(int(d.get("address", 0) or 0) != addr for d in self._devices):
            self._devices.append(info)
        self._live_trend_frame.add_device(info)
        self._historic_trend_frame.add_device(info)
        self._logs_frame.add_device(info)

    def redraw_trend(self):
        self._live_trend_frame.redraw()


# =============================================================================
# Journal View
# =============================================================================
class JournalView(ctk.CTkFrame):
    def __init__(self, parent, current_user=None):
        super().__init__(parent, fg_color="transparent")
        self._current_user = current_user

        card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=14,
                            border_width=1, border_color="#C7D9EE")
        card.pack(fill="both", expand=True, padx=48, pady=24)

        # Header
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=32, pady=(28, 0))
        ctk.CTkLabel(hdr, text="Journal",
                     font=(UI_FONT, 16, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")
        ctk.CTkButton(hdr, text="\u21bb Refresh", width=90, height=34,
                      corner_radius=8, font=(UI_FONT, 11, "bold"),
                      fg_color="#E2E8F0", text_color=CLR_TITLE,
                      hover_color="#CBD5E1",
                      command=self.refresh).pack(side="right")

        ctk.CTkFrame(card, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=32, pady=(12, 0))

        # Column header
        col_hdr = ctk.CTkFrame(card, fg_color="#F1F5F9", corner_radius=0)
        col_hdr.pack(fill="x", padx=32, pady=(10, 0))
        for txt, w in [("Timestamp", 150), ("User", 100),
                       ("Section", 130), ("Action", 70), ("Detail", 300)]:
            ctk.CTkLabel(col_hdr, text=txt, width=w,
                         font=(UI_FONT, 9, "bold"), text_color=CLR_LABEL,
                         fg_color="transparent", anchor="w").pack(
                         side="left", padx=4, pady=8)

        # Scrollable rows
        self._scroll = ctk.CTkScrollableFrame(
            card, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8")
        self._scroll.pack(fill="both", expand=True, padx=32, pady=(4, 24))

    def refresh(self):
        if not ALERT_AVAILABLE:
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        entries = get_journal(200)
        if self._current_user and not self._current_user.is_admin:
            entries = [e for e in entries if e.username == self._current_user.username]
        if not entries:
            ctk.CTkLabel(self._scroll, text="No journal entries yet.",
                         font=(UI_FONT, 10), text_color="#94A3B8",
                         fg_color="transparent").pack(pady=12)
            return
        action_colors = {"ADD": CLR_SAFE, "UPDATE": "#F59E0B", "DELETE": CLR_CRIT}
        for i, e in enumerate(entries):
            bg = "#F0F4FF" if i % 2 == 0 else "transparent"
            row = ctk.CTkFrame(self._scroll, fg_color=bg, corner_radius=4)
            row.pack(fill="x", pady=1)
            clr = action_colors.get(e.action, CLR_LABEL)
            for txt, w in [(e.timestamp, 150), (e.username, 100),
                           (e.section, 130), (e.action, 70), (e.detail, 300)]:
                ctk.CTkLabel(row, text=txt, width=w, font=(UI_FONT, 9),
                             text_color=clr if txt == e.action else CLR_TITLE,
                             fg_color="transparent", anchor="w").pack(
                             side="left", padx=4, pady=4)


# =============================================================================
# Settings View
# =============================================================================
class SettingsView(ctk.CTkFrame):
    # (icon-char, label-text, tab-key)
    _TABS_ALL   = [
        ("\u2699", "GENERAL",            "General"),
        ("\U0001F514", "ALERT MANAGEMENT", "Alert Management"),
        ("\U0001F4C4", "REPORT MANAGEMENT", "Report Management")
    ]
    _TABS_ADMIN = [("\U0001F465", "USER MANAGEMENT",  "User Management")]

    def __init__(self, parent, current_user=None):
        super().__init__(parent, fg_color="transparent")
        self._current_user = current_user
        self._is_operator = bool(current_user and current_user.role == "operator")

        tabs = list(self._TABS_ALL)
        if current_user and current_user.is_admin:
            tabs += self._TABS_ADMIN

        # ── Rounded shell + inset card ───────────────────────────────────
        shell = ctk.CTkFrame(self, fg_color=BG_APP, corner_radius=18,
                     border_width=0)
        shell.pack(fill="both", expand=True, padx=48, pady=24)

        outer = ctk.CTkFrame(shell, fg_color=BG_CARD, corner_radius=15,
                     border_width=1, border_color="#C7D9EE")
        outer.pack(fill="both", expand=True, padx=3, pady=3)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        # ── Tab header strip ──────────────────────────────────────────────
        tab_strip = ctk.CTkFrame(outer, fg_color="#F0F4F8", corner_radius=12,
                     height=54, border_width=0)
        tab_strip.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        tab_strip.grid_propagate(False)
        for i in range(len(tabs)):
            tab_strip.columnconfigure(i, weight=1)

        self._tab_btns:       dict[str, ctk.CTkButton] = {}
        self._tab_indicators: dict[str, ctk.CTkFrame] = {}
        self._tab_cells:      dict[str, ctk.CTkFrame] = {}

        for col, (icon, label, key) in enumerate(tabs):
            # Each cell has 2 rows: button(weight=1) + 3-px indicator bar
            cell = ctk.CTkFrame(tab_strip, fg_color="#F0F4F8",
                                corner_radius=0, border_width=0)
            cell.grid(row=0, column=col, sticky="nsew")
            cell.rowconfigure(0, weight=1)
            cell.rowconfigure(1, minsize=3)
            cell.columnconfigure(0, weight=1)

            btn = ctk.CTkButton(
                cell,
                text=f"  {icon}  {label}",
                anchor="center",
                height=50,
                corner_radius=0,
                border_width=0,
                font=(UI_FONT, 11, "bold"),
                fg_color="transparent",
                text_color=CLR_LABEL,
                hover_color="#E2EBF6",
                command=lambda k=key: self._switch_tab(k),
            )
            btn.grid(row=0, column=0, sticky="nsew")

            # Indicator bar — always in row 1; colour changes on activation
            indicator = ctk.CTkFrame(cell, fg_color="#F0F4F8", height=3,
                                     corner_radius=0, border_width=0)
            indicator.grid(row=1, column=0, sticky="ew")

            self._tab_btns[key]      = btn
            self._tab_indicators[key] = indicator
            self._tab_cells[key]     = cell

        # ── Content area ──────────────────────────────────────────────────
        self._content = ctk.CTkFrame(outer, fg_color="transparent",
                         corner_radius=0)
        self._content.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # ── Build tab frames ──────────────────────────────────────────────

        self._general_frame = self._build_general(self._content)
        self._alert_frame   = self._build_alert_mgmt(self._content)
        self._report_frame  = self._build_report_mgmt(self._content)
        self._unauth_frame  = self._build_unauthorized(self._content)
        self._user_frame    = self._build_user_mgmt(self._content) \
                      if "User Management" in [k for _, _, k in tabs] \
                      else None

        self._switch_tab("Alert Management" if self._is_operator else "General")

    # ── Tab switching ─────────────────────────────────────────────────────

    def _switch_tab(self, name: str):
        # Update button + indicator styles
        for key, btn in self._tab_btns.items():
            active = (key == name)
            btn.configure(
                text_color=NAV_ACTIVE_BG if active else CLR_LABEL,
                font=(UI_FONT, 11, "bold"),
                fg_color="#FFFFFF" if active else "transparent",
            )
            self._tab_cells[key].configure(
                fg_color="#FFFFFF" if active else "#F0F4F8")
            self._tab_indicators[key].configure(
                fg_color=NAV_ACTIVE_BG if active else "#C7D9EE")

        # Swap content
        self._general_frame.pack_forget()
        self._alert_frame.pack_forget()
        self._report_frame.pack_forget()
        self._unauth_frame.pack_forget()
        if self._user_frame:
            self._user_frame.pack_forget()

        if self._is_operator and name != "Alert Management":
            self._unauth_frame.pack(fill="both", expand=True)
            return

        if name == "General":
            self._general_frame.pack(fill="both", expand=True)
            self._refresh_k_factor_device_options()
            self._refresh_k_factor_rules()
        elif name == "Alert Management":
            self._alert_frame.pack(fill="both", expand=True)
            self._refresh_offline_alert_config()
            self._refresh_alert_list()
        elif name == "Report Management":
            self._report_frame.pack(fill="both", expand=True)
            self._refresh_report_event_list()
        elif name == "User Management" and self._user_frame:
            self._user_frame.pack(fill="both", expand=True)
            self._refresh_user_list()

    def _build_unauthorized(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        card = ctk.CTkFrame(frame, fg_color="#F8FAFC", corner_radius=10,
                            border_width=1, border_color="#E2E8F0")
        card.pack(fill="both", expand=True, padx=28, pady=20)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            inner,
            text="Access Restricted",
            font=(UI_FONT, 18, "bold"),
            text_color="#9A3412",
            fg_color="transparent",
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            inner,
            text=(
                "You are not autherized to access this module.\n"
                "For configuration-related support, contact GreenEnviro:"
            ),
            font=(UI_FONT, 12, "bold"),
            text_color=CLR_WARN,
            justify="center",
            wraplength=700,
            fg_color="transparent",
        ).pack()

        support_box = ctk.CTkFrame(
            inner,
            fg_color="#EFF6FF",
            corner_radius=10,
            border_width=1,
            border_color="#BFDBFE",
        )
        support_box.pack(pady=(12, 0), fill="x")

        ctk.CTkLabel(
            support_box,
            text="Support Contact IDs",
            font=(UI_FONT, 11, "bold"),
            text_color="#1D4ED8",
            fg_color="transparent",
        ).pack(padx=14, pady=(8, 4), anchor="w")

        ctk.CTkLabel(
            support_box,
            text="baaji@greenenviro.co.in",
            font=(UI_FONT, 11, "bold"),
            text_color="#1E3A8A",
            fg_color="transparent",
        ).pack(padx=14, anchor="w")

        ctk.CTkLabel(
            support_box,
            text="suneel.s@greenenviro.co.in",
            font=(UI_FONT, 11, "bold"),
            text_color="#1E3A8A",
            fg_color="transparent",
        ).pack(padx=14, pady=(2, 10), anchor="w")
        return frame
    def _build_report_mgmt(self, parent) -> ctk.CTkScrollableFrame:
        outer = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
        )

        form_card = ctk.CTkFrame(outer, fg_color="#F8FAFC", corner_radius=10,
                                 border_width=1, border_color="#E2E8F0")
        form_card.pack(fill="x", padx=28, pady=(20, 0))

        form_inner = ctk.CTkFrame(form_card, fg_color="transparent")
        form_inner.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            form_inner,
            text="Scheduler Configuration",
            font=(UI_FONT, 12, "bold"),
            text_color=CLR_TITLE,
            fg_color="transparent",
        ).pack(anchor="w", pady=(0, 10))

        # Column-based layout so each label is pinned directly above its control.
        cols_row = ctk.CTkFrame(form_inner, fg_color="transparent")
        cols_row.pack(fill="x")

        # ── Frequency column ──────────────────────────────────
        _fc = ctk.CTkFrame(cols_row, fg_color="transparent")
        _fc.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(_fc, text="Frequency", font=(UI_FONT, 10, "bold"),
                     text_color=CLR_LABEL, fg_color="transparent",
                     anchor="w").pack(anchor="w")
        self._report_freq_var = ctk.StringVar(value="Daily")
        self._report_freq_menu = ctk.CTkOptionMenu(
            _fc, values=["Daily", "Weekly", "Monthly"],
            variable=self._report_freq_var, width=130, height=34,
            corner_radius=8, font=FONT_LABEL, command=self._on_freq_changed,
        )
        self._report_freq_menu.pack()

        # ── Avg At column ─────────────────────────────────────
        _ac = ctk.CTkFrame(cols_row, fg_color="transparent")
        _ac.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(_ac, text="Avg At", font=(UI_FONT, 10, "bold"),
                     text_color=CLR_LABEL, fg_color="transparent",
                     anchor="w").pack(anchor="w")
        self._report_avg_var = ctk.StringVar(value="Raw")
        self._report_avg_menu = ctk.CTkOptionMenu(
            _ac, values=["Raw", "5 min", "15 min", "30 min", "60 min"],
            variable=self._report_avg_var, width=120, height=34,
            corner_radius=8, font=FONT_LABEL,
        )
        self._report_avg_menu.pack()

        # ── Time column (12-h spinner) ────────────────────────
        _tc = ctk.CTkFrame(cols_row, fg_color="transparent")
        _tc.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(_tc, text="Time", font=(UI_FONT, 10, "bold"),
                     text_color=CLR_LABEL, fg_color="transparent",
                     anchor="w").pack(anchor="w")
        self._build_time_picker(_tc).pack(anchor="w")

        # ── Mail ID column ────────────────────────────────────
        _mc = ctk.CTkFrame(cols_row, fg_color="transparent")
        _mc.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(_mc, text="Mail ID", font=(UI_FONT, 10, "bold"),
                     text_color=CLR_LABEL, fg_color="transparent",
                     anchor="w").pack(anchor="w")
        self._report_mail_entry = ctk.CTkEntry(
            _mc, width=190, height=34, corner_radius=8,
            font=FONT_INPUT, border_color="#CBD5E1",
            placeholder_text="user@example.com")
        self._report_mail_entry.pack()

        # ── Button column ─────────────────────────────────────
        btn_row = ctk.CTkFrame(cols_row, fg_color="transparent")
        btn_row.pack(side="left", padx=(4, 0))
        ctk.CTkLabel(btn_row, text=" ", font=(UI_FONT, 10),
                     fg_color="transparent").pack()  # spacer to align with controls

        ctk.CTkButton(
            btn_row, text="+ Add Scheduler Event", width=160, height=34,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=self._add_report_event,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="🧪 Test Report", width=120, height=34,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color="#F59E0B", hover_color="#D97706",
            command=self._test_report_send,
        ).pack(side="left")

        self._report_status_lbl = ctk.CTkLabel(
            form_inner, text="", font=(UI_FONT, 9),
            text_color=CLR_SAFE, fg_color="transparent")
        self._report_status_lbl.pack(anchor="w", pady=(6, 0))

        rules_hdr = ctk.CTkFrame(outer, fg_color="transparent")
        rules_hdr.pack(fill="x", padx=28, pady=(18, 0))
        ctk.CTkLabel(rules_hdr, text="Scheduled Events",
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")

        ctk.CTkFrame(outer, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=28, pady=(6, 0))

        self._report_event_scroll = ctk.CTkScrollableFrame(
            outer, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
            height=180)
        self._report_event_scroll.pack(fill="x", padx=28, pady=(6, 18))

        return outer

    def _add_report_event(self):
        if not ALERT_AVAILABLE:
            return

        freq = self._report_freq_var.get().strip()
        avg_at = self._report_avg_var.get().strip()
        sched_time = self._get_report_sched_time()
        mail = self._report_mail_entry.get().strip()

        ok, msg, _scheduler_id = add_report_scheduler(
            freq, mail, avg_at, scheduled_time=sched_time, actor=self._get_actor())
        if ok:
            self._report_status_lbl.configure(
                text=f"✓  {msg}", text_color=CLR_SAFE)
            self._report_mail_entry.delete(0, "end")
            self._report_avg_var.set("Raw")
            self._refresh_report_event_list()
        else:
            self._report_status_lbl.configure(
                text=f"⚠  {msg}", text_color=CLR_WARN)

    def _refresh_report_event_list(self):
        if not ALERT_AVAILABLE:
            return

        for w in self._report_event_scroll.winfo_children():
            w.destroy()

        events = get_report_schedulers()
        if not events:
            ctk.CTkLabel(
                self._report_event_scroll,
                text="No scheduled events configured yet.",
                font=(UI_FONT, 11), text_color="#94A3B8",
                fg_color="transparent").pack(pady=20)
            return

        for event in events:
            self._render_report_event_card(event)

    def _render_report_event_card(self, event: dict):
        card = ctk.CTkFrame(self._report_event_scroll,
                            fg_color="#F0F9FF", corner_radius=8,
                            border_width=1, border_color="#BFDBFE")
        card.pack(fill="x", pady=(0, 5))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=5)

        badge = ctk.CTkFrame(row, fg_color=NAV_ACTIVE_BG, corner_radius=6)
        badge.pack(side="left")
        ctk.CTkLabel(badge, text=f"  {event['frequency']}  ",
                     font=(UI_FONT, 10, "bold"), text_color="#FFFFFF",
                     fg_color="transparent").pack(padx=4, pady=3)

        time_badge = ctk.CTkFrame(row, fg_color="#0369A1", corner_radius=6)
        time_badge.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(time_badge, text=f"  {event.get('scheduled_time', '09:00')}  ",
                 font=(UI_FONT, 10, "bold"), text_color="#FFFFFF",
                 fg_color="transparent").pack(padx=4, pady=3)

        avg_badge = ctk.CTkFrame(row, fg_color="#10B981", corner_radius=6)
        avg_badge.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(avg_badge, text=f"  {event.get('avg_at', 'Raw')}  ",
                     font=(UI_FONT, 10, "bold"), text_color="#FFFFFF",
                     fg_color="transparent").pack(padx=4, pady=3)

        for mail in event.get("mail_ids", []):
            chip = ctk.CTkFrame(row, fg_color="#DBEAFE", corner_radius=4)
            chip.pack(side="left", padx=(6, 0))
            ctk.CTkLabel(chip, text=mail, font=(UI_FONT, 9),
                         text_color="#1E3A5F",
                         fg_color="transparent").pack(
                         side="left", padx=(6, 2), pady=3)
            ctk.CTkButton(
                chip, text="×", width=16, height=16,
                corner_radius=3, font=(UI_FONT, 9, "bold"),
                fg_color="#BFDBFE", text_color="#1E40AF",
                hover_color="#93C5FD",
                command=lambda sid=event["id"], e=mail: self._delete_report_email(sid, e),
            ).pack(side="left", padx=(0, 3))

        ctk.CTkButton(
            row, text="+ Add Mail", width=92, height=26,
            corner_radius=6, font=(UI_FONT, 10, "bold"),
            fg_color="#E0F2FE", text_color="#0C4A6E",
            hover_color="#BAE6FD",
            command=lambda sid=event["id"]: self._open_add_report_mail_dialog(sid),
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            row, text="🗑", width=28, height=26,
            corner_radius=6, font=(UI_FONT, 12),
            fg_color="#FEE2E2", text_color=CLR_CRIT, hover_color="#FECACA",
            command=lambda sid=event["id"]: self._delete_report_event(sid),
        ).pack(side="right", padx=(4, 0))

    def _open_add_report_mail_dialog(self, scheduler_id: int):
        if not ALERT_AVAILABLE:
            return

        import tkinter.simpledialog

        mail = tkinter.simpledialog.askstring(
            "Add Mail ID", "Enter additional mail ID:")
        if not mail:
            return

        ok, msg = add_report_scheduler_email(
            scheduler_id, mail.strip(), actor=self._get_actor())
        if ok:
            self._refresh_report_event_list()
            self._report_status_lbl.configure(
                text=f"✓  {msg}", text_color=CLR_SAFE)
        else:
            self._report_status_lbl.configure(
                text=f"⚠  {msg}", text_color=CLR_WARN)

    def _delete_report_email(self, scheduler_id: int, mail: str):
        if not ALERT_AVAILABLE:
            return

        ok, msg = delete_report_scheduler_email(
            scheduler_id, mail, actor=self._get_actor())
        if ok:
            self._refresh_report_event_list()
            self._report_status_lbl.configure(
                text=f"✓  {msg}", text_color=CLR_SAFE)
        else:
            self._report_status_lbl.configure(
                text=f"⚠  {msg}", text_color=CLR_WARN)

    def _delete_report_event(self, scheduler_id: int):
        if not ALERT_AVAILABLE:
            return

        ok, msg = delete_report_scheduler(
            scheduler_id, actor=self._get_actor())
        if ok:
            self._refresh_report_event_list()
            self._report_status_lbl.configure(
                text=f"✓  {msg}", text_color=CLR_SAFE)
        else:
            self._report_status_lbl.configure(
                text=f"⚠  {msg}", text_color=CLR_WARN)

    def _build_time_picker(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """Build a compact 12-hour time spinner with side arrows and AM/PM toggle."""
        self._rtp_hour = 9
        self._rtp_min  = 0
        self._rtp_ampm_var = ctk.StringVar(value="AM")

        frame = ctk.CTkFrame(parent, fg_color="#F1F5F9", corner_radius=8,
                             border_width=1, border_color="#CBD5E1")

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(padx=6, pady=5)

        def _sync():
            self._rtp_hour_lbl.configure(text=f"{self._rtp_hour:02d}")
            self._rtp_min_lbl.configure(text=f"{self._rtp_min:02d}")

        _btn_kw = dict(corner_radius=4, font=(UI_FONT, 9, "bold"),
                       fg_color="#E2E8F0", text_color=CLR_TITLE,
                       hover_color="#CBD5E1")

        # ── Hour spinner (side arrows) ───────────────────────────────────
        h_col = ctk.CTkFrame(inner, fg_color="transparent")
        h_col.pack(side="left")
        ctk.CTkButton(h_col, text="◀", width=24, height=26, **_btn_kw,
                      command=lambda: (
                          setattr(self, '_rtp_hour', (self._rtp_hour - 2) % 12 + 1),
                          _sync())
                      ).pack(side="left")
        self._rtp_hour_lbl = ctk.CTkLabel(
            h_col, text="09", width=32, font=(UI_FONT, 12, "bold"),
            text_color=CLR_TITLE, fg_color="transparent")
        self._rtp_hour_lbl.pack(side="left", padx=(2, 2))
        ctk.CTkButton(h_col, text="▶", width=24, height=26, **_btn_kw,
                      command=lambda: (
                          setattr(self, '_rtp_hour', self._rtp_hour % 12 + 1),
                          _sync())
                      ).pack(side="left")

        ctk.CTkLabel(inner, text=":", font=(UI_FONT, 14, "bold"),
                     text_color=CLR_TITLE, fg_color="transparent",
                     width=10).pack(side="left")

        # ── Minute spinner (5-min step, side arrows) ─────────────────────
        m_col = ctk.CTkFrame(inner, fg_color="transparent")
        m_col.pack(side="left")
        ctk.CTkButton(m_col, text="◀", width=24, height=26, **_btn_kw,
                      command=lambda: (
                          setattr(self, '_rtp_min', (self._rtp_min - 5) % 60),
                          _sync())
                      ).pack(side="left")
        self._rtp_min_lbl = ctk.CTkLabel(
            m_col, text="00", width=32, font=(UI_FONT, 12, "bold"),
            text_color=CLR_TITLE, fg_color="transparent")
        self._rtp_min_lbl.pack(side="left", padx=(2, 2))
        ctk.CTkButton(m_col, text="▶", width=24, height=26, **_btn_kw,
                      command=lambda: (
                          setattr(self, '_rtp_min', (self._rtp_min + 5) % 60),
                          _sync())
                      ).pack(side="left")

        # ── AM / PM toggle ────────────────────────────────────────────────
        def _toggle_ampm():
            nv = "PM" if self._rtp_ampm_var.get() == "AM" else "AM"
            self._rtp_ampm_var.set(nv)
            self._rtp_ampm_btn.configure(
                fg_color="#7C3AED" if nv == "PM" else "#0369A1")

        self._rtp_ampm_btn = ctk.CTkButton(
            inner, textvariable=self._rtp_ampm_var,
            width=46, height=26, corner_radius=6,
            font=(UI_FONT, 10, "bold"), fg_color="#0369A1",
            text_color="#FFFFFF", hover_color="#075985",
            command=_toggle_ampm,
        )
        self._rtp_ampm_btn.pack(side="left", padx=(6, 0), pady=(0, 1))

        return frame

    def _get_report_sched_time(self) -> str:
        """Return picker value as HH:MM in 24-hour format."""
        h, m = self._rtp_hour, self._rtp_min
        ampm = self._rtp_ampm_var.get()
        if ampm == "AM":
            h24 = 0 if h == 12 else h
        else:
            h24 = 12 if h == 12 else h + 12
        return f"{h24:02d}:{m:02d}"

    def _on_freq_changed(self, value):
        """Dynamically update avg_at options based on frequency."""
        # All report frequencies can use all averaging options.
        self._report_avg_menu.configure(
            values=["Raw", "5 min", "15 min", "30 min", "60 min"])

    def _test_report_send(self):
        """Test report generation and mail sending."""
        # Import at method level to ensure availability in exception handlers
        import smtplib
        import ssl as _ssl
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.header import Header

        if not ALERT_AVAILABLE:
            self._report_status_lbl.configure(
                text="⚠  Alert system not available", text_color=CLR_WARN)
            return

        if not DB_AVAILABLE:
            self._report_status_lbl.configure(
                text="⚠  Database not available", text_color=CLR_WARN)
            return

        freq = self._report_freq_var.get().strip()
        avg_at = self._report_avg_var.get().strip()
        mail = self._report_mail_entry.get().strip()

        # Validate input
        if not freq:
            self._report_status_lbl.configure(
                text="⚠  Please select frequency", text_color=CLR_WARN)
            return

        if not mail:
            self._report_status_lbl.configure(
                text="⚠  Please enter mail ID", text_color=CLR_WARN)
            return

        # Validate email format
        if not _valid_email(mail):
            self._report_status_lbl.configure(
                text="⚠  Invalid email format", text_color=CLR_WARN)
            return

        # Show loading status
        self._report_status_lbl.configure(
            text="⏳ Generating report...", text_color="#F59E0B")
        self._report_status_lbl.update()

        try:
            # Get all device logs
            from db_repository import get_all_live_readings
            rows = get_all_live_readings()

            if not rows:
                self._report_status_lbl.configure(
                    text="⚠  No device data available", text_color=CLR_WARN)
                return

            # Generate report PDF bytes using shared export logic
            pdf_bytes = build_device_logs_report_pdf_bytes(
                rows,
                frequency=avg_at,
                schedule_frequency=freq,
            )

            # Send test email directly
            cfg = get_smtp_config()
            if not cfg or not cfg.get("host"):
                self._report_status_lbl.configure(
                    text="⚠  SMTP not configured in Settings",
                    text_color=CLR_WARN)
                return

            host = cfg.get("host", "").strip()
            port = int(cfg.get("port", 587))
            sec = int(cfg.get("use_tls", 1))

            from email.mime.application import MIMEApplication

            msg = MIMEMultipart()
            msg["From"] = cfg.get("from_email") or cfg.get("username", "")
            msg["To"] = mail
            msg["Subject"] = Header(
                f"[H2 Dashboard] Test Device Logs Report — {freq} (Avg: {avg_at})",
                "utf-8"
            )
            msg.attach(MIMEText(
                "Please find attached the test Device Logs report in PDF format.",
                "plain",
                "utf-8",
            ))
            pdf_name = f"device_logs_test_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_name)
            msg.attach(pdf_part)

            # Connect to SMTP server
            ctx = _ssl.create_default_context()
            if sec == 2:  # Direct SSL
                server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
                server.ehlo()
            elif sec == 1:  # STARTTLS
                server = smtplib.SMTP(host, port, timeout=15)
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
            else:  # No encryption
                server = smtplib.SMTP(host, port, timeout=15)
                server.ehlo()

            # Login if credentials provided
            uname = cfg.get("username", "").strip()
            pwd = "".join(cfg.get("password", "").split())
            if uname and pwd:
                server.login(uname, pwd)

            # Send email
            server.sendmail(
                cfg.get("from_email") or cfg.get("username", ""),
                [mail],
                msg.as_string()
            )
            server.quit()

            # Log journal entry
            log_journal(
                self._get_actor(),
                "Report Management",
                "SEND",
                f"Test report sent to {mail} (Freq: {freq}, Avg: {avg_at})",
            )

            self._report_status_lbl.configure(
                text=f"✓  Test report sent to {mail}",
                text_color=CLR_SAFE)

        except smtplib.SMTPAuthenticationError:
            self._report_status_lbl.configure(
                text="⚠  SMTP authentication failed", text_color=CLR_WARN)
        except smtplib.SMTPException as e:
            self._report_status_lbl.configure(
                text=f"⚠  SMTP error: {str(e)[:50]}", text_color=CLR_WARN)
        except Exception as e:
            logger.exception("Test report generation failed: %s", e)
            self._report_status_lbl.configure(
                text=f"⚠  Error: {str(e)[:50]}", text_color=CLR_WARN)

    # ── General tab ───────────────────────────────────────────────────────

    def _build_general(self, parent) -> ctk.CTkScrollableFrame:
        outer = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
        )

        row = ctk.CTkFrame(outer, fg_color="transparent")
        row.pack(anchor="w", padx=32, pady=28)

        ctk.CTkLabel(row, text="PCB Standard Value",
                     font=(UI_FONT, 11, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left", padx=(0, 12))

        self._std_entry = ctk.CTkEntry(row, width=110, height=34,
                                       corner_radius=8, font=FONT_LABEL,
                                       border_color="#CBD5E1",
                                       placeholder_text="e.g. 25")
        self._std_entry.insert(0, str(SensorCard._std_value))
        self._std_entry.pack(side="left", padx=(0, 8))

        ctk.CTkButton(row, text="Apply", width=80, height=34,
                      corner_radius=8, font=(UI_FONT, 11, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=self._apply).pack(side="left")

        self._status_lbl = ctk.CTkLabel(row, text="", font=(UI_FONT, 9),
                                        text_color=CLR_SAFE,
                                        fg_color="transparent")
        self._status_lbl.pack(side="left", padx=(10, 0))

        # ── K factor Rules ──────────────────────────────────────────────
        ctk.CTkLabel(outer, text="K factor Rule",
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(
                     anchor="w", padx=32, pady=(20, 0))
        ctk.CTkFrame(outer, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=32, pady=(6, 0))

        k_card = ctk.CTkFrame(outer, fg_color="#F8FAFC", corner_radius=10,
                              border_width=1, border_color="#E2E8F0")
        k_card.pack(fill="x", padx=32, pady=(8, 0))
        k_inner = ctk.CTkFrame(k_card, fg_color="transparent")
        k_inner.pack(fill="x", padx=16, pady=12)

        k_form = ctk.CTkFrame(k_inner, fg_color="transparent")
        k_form.pack(fill="x")

        self._k_rule_device_map: dict[str, int] = {}
        self._k_rule_device_var = ctk.StringVar(value="Select Device")
        self._k_rule_device_menu = ctk.CTkOptionMenu(
            k_form,
            variable=self._k_rule_device_var,
            values=["Select Device"],
            width=220,
            height=34,
            corner_radius=8,
            font=FONT_LABEL,
        )
        self._k_rule_device_menu.pack(side="left", padx=(0, 8))

        self._k_rule_value_entry = ctk.CTkEntry(
            k_form, width=110, height=34, corner_radius=8,
            font=FONT_LABEL, border_color="#CBD5E1",
            placeholder_text="K factor")
        self._k_rule_value_entry.insert(0, "1")
        self._k_rule_value_entry.pack(side="left", padx=(0, 8))

        self._k_rule_enabled_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            k_form, text="Enabled",
            variable=self._k_rule_enabled_var,
            font=(UI_FONT, 10), text_color=CLR_LABEL,
            fg_color=NAV_ACTIVE_BG,
            hover_color="#075985",
            border_color="#94A3B8",
            command=self._on_k_rule_form_enabled_toggle,
        ).pack(side="left", padx=(0, 10))

        self._on_k_rule_form_enabled_toggle()

        ctk.CTkButton(
            k_form,
            text="+ Add / Update Rule",
            width=150,
            height=34,
            corner_radius=8,
            font=(UI_FONT, 11, "bold"),
            fg_color=NAV_ACTIVE_BG,
            hover_color="#075985",
            command=self._add_k_factor_rule,
        ).pack(side="left")

        self._k_rule_status_lbl = ctk.CTkLabel(
            k_inner, text="", font=(UI_FONT, 9),
            text_color=CLR_SAFE, fg_color="transparent")
        self._k_rule_status_lbl.pack(anchor="w", pady=(6, 0))

        self._k_rules_scroll = ctk.CTkScrollableFrame(
            k_inner, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
            height=140,
        )
        self._k_rules_scroll.pack(fill="x", pady=(8, 0))

        # ── SMTP Configuration ───────────────────────────────────────────
        ctk.CTkLabel(outer, text="Email Server (SMTP)",
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(
                     anchor="w", padx=32, pady=(24, 0))
        ctk.CTkFrame(outer, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=32, pady=(6, 0))

        smtp_card = ctk.CTkFrame(outer, fg_color="#F8FAFC", corner_radius=10,
                                 border_width=1, border_color="#E2E8F0")
        smtp_card.pack(fill="x", padx=32, pady=(8, 0))
        smtp_inner = ctk.CTkFrame(smtp_card, fg_color="transparent")
        smtp_inner.pack(fill="x", padx=16, pady=12)

        cfg = get_smtp_config() if ALERT_AVAILABLE else {}

        def _smtp_row():
            r = ctk.CTkFrame(smtp_inner, fg_color="transparent")
            r.pack(fill="x", pady=(4, 0))
            return r

        r1 = _smtp_row()
        ctk.CTkLabel(r1, text="Host", width=100,
                     font=(UI_FONT, 10, "bold"), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        self._smtp_host = ctk.CTkEntry(r1, width=200, height=32,
                                       corner_radius=7, font=FONT_INPUT,
                                       border_color="#CBD5E1")
        self._smtp_host.insert(0, cfg.get("host", ""))
        self._smtp_host.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(r1, text="Port", width=100,
                     font=(UI_FONT, 10, "bold"), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        self._smtp_port = ctk.CTkEntry(r1, width=160, height=32,
                                       corner_radius=7, font=FONT_INPUT,
                                       border_color="#CBD5E1")
        self._smtp_port.insert(0, str(cfg.get("port", 587)))
        self._smtp_port.pack(side="left", padx=(0, 16))

        # Security mode selector: STARTTLS (port 587) / SSL (port 465) / None
        _sec_map = {1: "STARTTLS", 2: "SSL", 0: "None"}
        _sec_init = _sec_map.get(int(cfg.get("use_tls", 1)), "STARTTLS")
        self._smtp_sec_var = ctk.StringVar(value=_sec_init)

        ctk.CTkLabel(r1, text="Security", width=72,
                     font=FONT_LABEL, text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        ctk.CTkSegmentedButton(
            r1, values=["STARTTLS", "SSL", "None"],
            variable=self._smtp_sec_var,
            font=FONT_LABEL, height=30,
        ).pack(side="left")

        r2 = _smtp_row()
        ctk.CTkLabel(r2, text="Username", width=100,
                     font=(UI_FONT, 10, "bold"), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        self._smtp_user = ctk.CTkEntry(r2, width=200, height=32,
                                       corner_radius=7, font=FONT_INPUT,
                                       border_color="#CBD5E1")
        self._smtp_user.insert(0, cfg.get("username", ""))
        self._smtp_user.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(r2, text="Password", width=100,
                     font=(UI_FONT, 10, "bold"), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        self._smtp_pwd = ctk.CTkEntry(r2, width=160, height=32, show="*",
                                      corner_radius=7, font=FONT_INPUT,
                                      border_color="#CBD5E1")
        self._smtp_pwd.insert(0, cfg.get("password", ""))
        self._smtp_pwd.pack(side="left")

        r3 = _smtp_row()
        ctk.CTkLabel(r3, text="From Email", width=100,
                     font=(UI_FONT, 10, "bold"), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        self._smtp_from = ctk.CTkEntry(r3, width=200, height=32,
                                       corner_radius=7, font=FONT_INPUT,
                                       border_color="#CBD5E1")
        self._smtp_from.insert(0, cfg.get("from_email", ""))
        self._smtp_from.pack(side="left", padx=(0, 16))

           # App Password hint (commented out by request)
           # ctk.CTkLabel(smtp_card,
           #              text="\u2139  Gmail / Yahoo / Outlook: use an App Password, not your regular password. "
           #                   "(Google account \u2192 Security \u2192 2-Step Verification \u2192 App passwords)",
           #              font=(UI_FONT, 8), text_color="#64748B",
           #              fg_color="transparent", wraplength=520, justify="left",
           #              anchor="w").pack(anchor="w", padx=16, pady=(4, 0))

        self._smtp_status = ctk.CTkLabel(smtp_card, text="",
                                         font=(UI_FONT, 9),
                                         text_color=CLR_SAFE,
                                         fg_color="transparent")
        self._smtp_status.pack(anchor="w", padx=16, pady=(0, 4))

        btn_row = ctk.CTkFrame(smtp_card, fg_color="transparent")
        btn_row.pack(anchor="w", padx=16, pady=(0, 12))
        ctk.CTkButton(btn_row, text="Save SMTP Settings", width=160,
                      height=32, corner_radius=8,
                      font=(UI_FONT, 11, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=self._save_smtp).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="\U0001f4e7 Send Test Email", width=160,
                      height=32, corner_radius=8,
                      font=(UI_FONT, 11, "bold"),
                      fg_color="#E2E8F0", text_color=CLR_TITLE,
                      hover_color="#CBD5E1",
                      command=self._test_smtp).pack(side="left")

        self._refresh_k_factor_device_options()
        self._refresh_k_factor_rules()

        return outer

    def _apply(self):
        try:
            val = float(self._std_entry.get().strip())
            old = SensorCard._std_value
            SensorCard.update_all_std(val, SensorCard._std_unit)
            self._status_lbl.configure(
                text=f"\u2713  Applied: {val}", text_color=CLR_SAFE)
            if ALERT_AVAILABLE:
                actor = (self._current_user.username
                         if self._current_user else "system")
                log_journal(actor, "General", "UPDATE",
                            f"PCB Standard Value changed: {old} -> {val}")
        except ValueError:
            self._status_lbl.configure(
                text="Invalid value", text_color=CLR_CRIT)

    def _refresh_k_factor_device_options(self):
        if not DB_AVAILABLE:
            return
        try:
            devices = get_all_devices()
            values = ["Select Device"]
            self._k_rule_device_map = {}
            for d in devices:
                label = f"{d.get('device_name', '—')} (Addr {int(d.get('device_address', 0)):03d})"
                values.append(label)
                self._k_rule_device_map[label] = int(d["id"])
            self._k_rule_device_menu.configure(values=values)
            if self._k_rule_device_var.get() not in values:
                self._k_rule_device_var.set("Select Device")
        except Exception:
            logger.exception("Failed to refresh K factor device options")

    def _on_k_rule_form_enabled_toggle(self):
        """Keep K-factor multiplier editable only when the rule is enabled."""
        try:
            if bool(self._k_rule_enabled_var.get()):
                self._k_rule_value_entry.configure(state="normal")
            else:
                self._k_rule_value_entry.configure(state="disabled")
        except Exception:
            logger.exception("Failed to toggle K-factor multiplier input state")

    def _add_k_factor_rule(self):
        if not DB_AVAILABLE:
            return

        selected = self._k_rule_device_var.get().strip()
        device_id = self._k_rule_device_map.get(selected)
        if not device_id:
            self._k_rule_status_lbl.configure(
                text="⚠  Please select a device.", text_color=CLR_WARN)
            return

        try:
            factor = float(self._k_rule_value_entry.get().strip())
            if factor <= 0:
                raise ValueError
        except ValueError:
            self._k_rule_status_lbl.configure(
                text="⚠  K factor must be a positive number.", text_color=CLR_CRIT)
            return

        enabled = bool(self._k_rule_enabled_var.get())
        try:
            upsert_k_factor_rule(device_id=device_id, k_factor=factor, is_enabled=enabled)
            if ALERT_AVAILABLE:
                log_journal(
                    self._get_actor(),
                    "General",
                    "UPDATE",
                    f"K factor rule saved: device_id={device_id}, factor={factor}, status={'enabled' if enabled else 'disabled'}",
                )
            self._k_rule_status_lbl.configure(
                text="✓  K factor rule saved.", text_color=CLR_SAFE)
            self._refresh_k_factor_rules()
        except Exception:
            logger.exception("Failed to save K factor rule")
            self._k_rule_status_lbl.configure(
                text="⚠  Failed to save K factor rule.", text_color=CLR_CRIT)

    def _refresh_k_factor_rules(self):
        if not DB_AVAILABLE:
            return
        for w in self._k_rules_scroll.winfo_children():
            w.destroy()

        try:
            rules = get_k_factor_rules()
        except Exception:
            logger.exception("Failed to load K factor rules")
            rules = []

        if not rules:
            ctk.CTkLabel(
                self._k_rules_scroll,
                text="No K factor rules configured yet.",
                font=(UI_FONT, 11), text_color="#94A3B8",
                fg_color="transparent",
            ).pack(pady=12)
            return

        for rule in rules:
            self._render_k_factor_rule_card(rule)

    def _render_k_factor_rule_card(self, rule: dict):
        card = ctk.CTkFrame(self._k_rules_scroll,
                            fg_color="#F0F9FF", corner_radius=8,
                            border_width=1, border_color="#BFDBFE")
        card.pack(fill="x", pady=(0, 5))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=6)

        dev_name = str(rule.get("device_name", "—"))
        dev_addr = int(rule.get("device_address", 0) or 0)
        ctk.CTkLabel(
            row,
            text=f"{dev_name} (Addr {dev_addr:03d})",
            width=250,
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_TITLE,
            fg_color="transparent",
            anchor="w",
        ).pack(side="left", padx=(0, 8))

        k_var = ctk.StringVar(value=f"{float(rule.get('k_factor', 1.0)):.4g}")
        k_entry = ctk.CTkEntry(
            row, width=100, height=30, corner_radius=7,
            font=FONT_LABEL, border_color="#CBD5E1",
            textvariable=k_var,
        )
        k_entry.pack(side="left", padx=(0, 8))

        enabled_var = ctk.BooleanVar(value=bool(int(rule.get("is_enabled", 0) or 0)))
        def _sync_rule_entry_state():
            try:
                k_entry.configure(state="normal" if bool(enabled_var.get()) else "disabled")
            except Exception:
                logger.exception("Failed to sync K-factor row entry state")

        ctk.CTkCheckBox(
            row, text="Enabled", variable=enabled_var,
            font=(UI_FONT, 10), text_color=CLR_LABEL,
            fg_color=NAV_ACTIVE_BG, hover_color="#075985", border_color="#94A3B8",
            command=_sync_rule_entry_state,
        ).pack(side="left", padx=(0, 8))

        _sync_rule_entry_state()

        def _save_rule(r=rule, ksv=k_var, env=enabled_var):
            try:
                factor = float(ksv.get().strip())
                if factor <= 0:
                    raise ValueError
            except ValueError:
                self._k_rule_status_lbl.configure(
                    text="⚠  K factor must be a positive number.", text_color=CLR_CRIT)
                return

            try:
                old_factor = float(rule.get("k_factor", 1.0) or 1.0)
                old_enabled = bool(int(rule.get("is_enabled", 0) or 0))
                new_enabled = bool(env.get())
                upsert_k_factor_rule(
                    device_id=int(r["device_id"]),
                    k_factor=factor,
                    is_enabled=new_enabled,
                )
                if ALERT_AVAILABLE:
                    changed_parts = []
                    if abs(old_factor - factor) > 1e-12:
                        changed_parts.append(f"factor: {old_factor} -> {factor}")
                    if old_enabled != new_enabled:
                        changed_parts.append(
                            f"status: {'enabled' if old_enabled else 'disabled'} -> {'enabled' if new_enabled else 'disabled'}"
                        )
                    if not changed_parts:
                        changed_parts.append("no value change")
                    log_journal(
                        self._get_actor(),
                        "General",
                        "UPDATE",
                        f"K factor rule modified for device_id={int(r['device_id'])}: " + ", ".join(changed_parts),
                    )
                self._k_rule_status_lbl.configure(
                    text="✓  K factor rule updated.", text_color=CLR_SAFE)
                self._refresh_k_factor_rules()
            except Exception:
                logger.exception("Failed to update K factor rule id=%s", r.get("id"))
                self._k_rule_status_lbl.configure(
                    text="⚠  Failed to update K factor rule.", text_color=CLR_CRIT)

        ctk.CTkButton(
            row, text="Save", width=64, height=28,
            corner_radius=6, font=(UI_FONT, 10, "bold"),
            fg_color="#E0F2FE", text_color="#0C4A6E", hover_color="#BAE6FD",
            command=_save_rule,
        ).pack(side="left", padx=(0, 8))

        def _delete_rule(rid=int(rule.get("id", 0))):
            if rid <= 0:
                return
            try:
                delete_k_factor_rule(rid)
                if ALERT_AVAILABLE:
                    log_journal(
                        self._get_actor(),
                        "General",
                        "DELETE",
                        f"K factor rule deleted: id={rid}, device_id={int(rule.get('device_id', 0) or 0)}",
                    )
                self._k_rule_status_lbl.configure(
                    text="✓  K factor rule deleted.", text_color=CLR_SAFE)
                self._refresh_k_factor_rules()
            except Exception:
                logger.exception("Failed to delete K factor rule id=%d", rid)
                self._k_rule_status_lbl.configure(
                    text="⚠  Failed to delete K factor rule.", text_color=CLR_CRIT)

        ctk.CTkButton(
            row, text="🗑", width=30, height=28,
            corner_radius=6, font=(UI_FONT, 12),
            fg_color="#FEE2E2", text_color=CLR_CRIT, hover_color="#FECACA",
            command=_delete_rule,
        ).pack(side="right")

    # ── Alert Management tab ──────────────────────────────────────────────

    def _build_alert_mgmt(self, parent) -> ctk.CTkScrollableFrame:
        outer = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
        )

        # ── Offline Device Alert Configuration ────────────────────────────
        offline_card = ctk.CTkFrame(outer, fg_color="#F8FAFC", corner_radius=10,
                                    border_width=1, border_color="#E2E8F0")
        offline_card.pack(fill="x", padx=28, pady=(20, 0))

        offline_inner = ctk.CTkFrame(offline_card, fg_color="transparent")
        offline_inner.pack(fill="x", padx=16, pady=12)

        # Title
        ctk.CTkLabel(offline_inner, text="Device Offline Alerts",
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent", anchor="w").pack(fill="x", pady=(0, 10))

        config_row = ctk.CTkFrame(offline_inner, fg_color="transparent")
        config_row.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(config_row, text="Cooldown Period:",
                     font=(UI_FONT, 10), text_color=CLR_LABEL,
                     fg_color="transparent").pack(side="left", padx=(0, 12))
        self._offline_cooldown_var = ctk.StringVar(value="30 min")
        ctk.CTkOptionMenu(
            config_row, values=["5 min", "15 min", "30 min", "1 hour"],
            variable=self._offline_cooldown_var,
            width=140, height=34, corner_radius=8,
            font=FONT_LABEL,
        ).pack(side="left", padx=(0, 16))

        self._offline_alert_var = ctk.BooleanVar(value=True)
        self._offline_alert_toggle = ctk.CTkSwitch(
            config_row,
            text="Mail Notification",
            variable=self._offline_alert_var,
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_LABEL,
            progress_color=NAV_ACTIVE_BG,
            button_color="#FFFFFF",
            button_hover_color="#E2E8F0",
            fg_color="#CBD5E1",
            hover=False,
            command=self._on_offline_alert_toggle,
        )
        self._offline_alert_toggle.pack(side="left", padx=(0, 16))

        ctk.CTkButton(
            config_row, text="Save Offline Cooldown Period", width=160, height=34,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=self._save_offline_alert_config,
        ).pack(side="left")

        self._offline_status_lbl = ctk.CTkLabel(
            offline_inner, text="", font=(UI_FONT, 9),
            text_color=CLR_SAFE, fg_color="transparent")
        self._offline_status_lbl.pack(anchor="w", pady=(2, 0))

        # ── Add-alert form ───────────────────────────────────────────────
        form_card = ctk.CTkFrame(outer, fg_color="#F8FAFC", corner_radius=10,
                                 border_width=1, border_color="#E2E8F0")
        form_card.pack(fill="x", padx=28, pady=(20, 0))

        form_inner = ctk.CTkFrame(form_card, fg_color="transparent")
        form_inner.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            form_inner,
            text="Device Threshold Alert",
            font=(UI_FONT, 12, "bold"),
            text_color=CLR_TITLE,
            fg_color="transparent",
            anchor="w",
        ).pack(anchor="w", pady=(0, 10))

        # One compact row: label-field pairs
        inp_row = ctk.CTkFrame(form_inner, fg_color="transparent")
        inp_row.pack(fill="x")

        ctk.CTkLabel(
            inp_row,
            text="Email Address",
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_LABEL,
            fg_color="transparent",
            anchor="w",
        ).pack(side="left", padx=(0, 6))

        self._alert_email_entry = ctk.CTkEntry(
            inp_row, width=210, height=34, corner_radius=8,
            font=FONT_INPUT, border_color="#CBD5E1",
            placeholder_text="user@example.com")
        self._alert_email_entry.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(
            inp_row,
            text="Threshold",
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_LABEL,
            fg_color="transparent",
            anchor="w",
        ).pack(side="left", padx=(0, 6))

        self._alert_thresh_entry = ctk.CTkEntry(
            inp_row, width=110, height=34, corner_radius=8,
            font=FONT_INPUT, border_color="#CBD5E1",
            placeholder_text="e.g. 50")
        self._alert_thresh_entry.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(
            inp_row,
            text="Occurrence",
            font=(UI_FONT, 10, "bold"),
            text_color=CLR_LABEL,
            fg_color="transparent",
            anchor="w",
        ).pack(side="left", padx=(0, 6))

        self._alert_occ_var = ctk.StringVar(value="hourly")
        ctk.CTkOptionMenu(
            inp_row, values=["10-min", "hourly", "bi-hourly"],
            variable=self._alert_occ_var,
            width=120, height=34, corner_radius=8,
            font=FONT_LABEL,
        ).pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            inp_row, text="+ Add Alert", width=100, height=34,
            corner_radius=8, font=(UI_FONT, 11, "bold"),
            fg_color=NAV_ACTIVE_BG, hover_color="#075985",
            command=self._add_alert_entry,
        ).pack(side="left")

        self._alert_form_status = ctk.CTkLabel(
            form_inner, text="", font=(UI_FONT, 9),
            text_color=CLR_SAFE, fg_color="transparent")
        self._alert_form_status.pack(anchor="w", pady=(6, 0))

        # ── Rules list (collapsible) ──────────────────────────────────────
        self._rules_collapsed = False

        rules_hdr = ctk.CTkFrame(outer, fg_color="transparent")
        rules_hdr.pack(fill="x", padx=28, pady=(18, 0))

        self._rules_toggle_btn = ctk.CTkButton(
            rules_hdr, text="▼", width=24, height=24,
            corner_radius=6, font=(UI_FONT, 10, "bold"),
            fg_color="#E2E8F0", text_color=CLR_TITLE, hover_color="#CBD5E1",
            command=self._toggle_rules)
        self._rules_toggle_btn.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(rules_hdr, text="Configured Alert Rules",
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")
        self._rules_divider = ctk.CTkFrame(outer, fg_color="#E2E8F0", height=1,
                                              corner_radius=0)
        self._rules_divider.pack(fill="x", padx=28, pady=(6, 0))

        self._rules_body = ctk.CTkFrame(outer, fg_color="transparent")
        self._rules_body.pack(fill="x", after=self._rules_divider)

        self._alert_rules_scroll = ctk.CTkScrollableFrame(
            self._rules_body, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
            height=50)
        self._alert_rules_scroll.pack(fill="x", padx=28, pady=(6, 0))
        self._alert_rules_scroll.pack_forget()

        self._alert_rules_empty = ctk.CTkFrame(self._rules_body, fg_color="transparent", height=50)
        self._alert_rules_empty.pack(fill="x", padx=28, pady=(6, 0))
        self._alert_rules_empty.pack_propagate(False)

        self._alert_empty_lbl = ctk.CTkLabel(
            self._alert_rules_empty,
            text="No alert rules configured yet.",
            font=(UI_FONT, 11), text_color="#94A3B8",
            fg_color="transparent")
        self._alert_empty_lbl.place(relx=0.5, rely=0.5, anchor="center")

        return outer

    # ── Alert helpers ─────────────────────────────────────────────────────

    def _get_actor(self) -> str:
        return self._current_user.username if self._current_user else "system"

    def _toggle_rules(self):
        self._rules_collapsed = not self._rules_collapsed
        if self._rules_collapsed:
            self._rules_body.pack_forget()
            self._rules_toggle_btn.configure(text="▶")
        else:
            self._rules_body.pack(fill="x", after=self._rules_divider)
            self._rules_toggle_btn.configure(text="▼")

    def _show_toast(self, message: str, duration_ms: int = 2000) -> None:
        """Briefly show a small floating label then destroy it."""
        root = self.winfo_toplevel()
        lbl = ctk.CTkLabel(
            root, text=f"  {message}  ",
            fg_color="#1E3A5F", 
            bg_color="transparent", text_color="#FFFFFF",
            corner_radius=8, font=(UI_FONT, 13, "bold"),
        )
        lbl.place(relx=0.5, rely=0.96, anchor="s")
        root.after(duration_ms, lbl.destroy)

    def _add_alert_entry(self):
        if not ALERT_AVAILABLE:
            return
        email  = self._alert_email_entry.get().strip()
        thresh = self._alert_thresh_entry.get().strip()
        occ    = self._alert_occ_var.get()
        try:
            thresh_f = float(thresh)
        except ValueError:
            self._alert_form_status.configure(
                text="\u26a0  Threshold must be a number.", text_color=CLR_CRIT)
            return

        ok, msg = add_alert(email, thresh_f, occ, actor=self._get_actor())
        if ok:
            self._alert_form_status.configure(
                text=f"\u2713  {msg}", text_color=CLR_SAFE)
            self._alert_email_entry.delete(0, "end")
            self._alert_thresh_entry.delete(0, "end")
            self._refresh_alert_list()
        else:
            self._alert_form_status.configure(
                text=f"\u26a0  {msg}", text_color=CLR_WARN)

    def _save_offline_alert_config(self, source: str = "save"):
        """Save offline device alert configuration."""
        from db_repository import get_offline_alert_config, update_offline_alert_config
        
        try:
            prev_cfg = get_offline_alert_config()
            prev_enabled = int(prev_cfg.get("enabled", 1))
            prev_cooldown = int(prev_cfg.get("cooldown_minutes", 30))

            enabled = 1 if self._offline_alert_var.get() else 0
            cooldown_str = self._offline_cooldown_var.get()
            
            # Convert cooldown string to minutes
            cooldown_map = {"5 min": 5, "15 min": 15, "30 min": 30, "1 hour": 60}
            cooldown_minutes = cooldown_map.get(cooldown_str, 30)
            
            update_offline_alert_config(enabled, cooldown_minutes)

            if ALERT_AVAILABLE:
                changes = []
                if prev_enabled != enabled:
                    changes.append(
                        f"mail notification: {'enabled' if prev_enabled else 'disabled'} -> {'enabled' if enabled else 'disabled'}"
                    )
                if prev_cooldown != cooldown_minutes:
                    changes.append(f"cooldown: {prev_cooldown} min -> {cooldown_minutes} min")
                if not changes:
                    changes.append("no value change")
                log_journal(
                    self._get_actor(),
                    "Alert Management",
                    "UPDATE",
                    f"Offline device alert settings updated ({source}): " + ", ".join(changes),
                )
            
            self._offline_status_lbl.configure(
                text="✓ Settings saved successfully", text_color=CLR_SAFE)
            self.after(3000, lambda: self._offline_status_lbl.configure(text=""))
        except Exception as e:
            logger.exception("Failed to save offline alert config")
            self._offline_status_lbl.configure(
                text=f"✗ Error: {e}", text_color=CLR_CRIT)

    def _on_offline_alert_toggle(self):
        """Persist offline alert mail notification state immediately when toggled."""
        self._save_offline_alert_config(source="toggle")

    def _refresh_offline_alert_config(self):
        """Load current offline alert configuration from database."""
        from db_repository import get_offline_alert_config
        
        try:
            cfg = get_offline_alert_config()
            self._offline_alert_var.set(bool(cfg.get("enabled", 1)))
            
            cooldown_minutes = cfg.get("cooldown_minutes", 30)
            cooldown_map_inv = {5: "5 min", 15: "15 min", 30: "30 min", 60: "1 hour"}
            cooldown_str = cooldown_map_inv.get(cooldown_minutes, "30 min")
            self._offline_cooldown_var.set(cooldown_str)
        except Exception:
            logger.exception("Failed to refresh offline alert config")

    def _refresh_alert_list(self):
        if not ALERT_AVAILABLE:
            return
        for w in self._alert_rules_scroll.winfo_children():
            w.destroy()

        rules = get_alert_rules()
        if not rules:
            self._alert_rules_scroll.pack_forget()
            self._alert_rules_empty.pack(fill="x", padx=28, pady=(6, 0))
            return

        # Expand height when rules are present (min rules: ~100px, max: 200px)
        num_rules = len(rules)
        new_height = min(50 + (num_rules * 50), 200)
        self._alert_rules_scroll.configure(height=new_height)
        self._alert_rules_empty.pack_forget()
        self._alert_rules_scroll.pack(fill="x", padx=28, pady=(6, 0))

        for rule in rules:
            self._render_rule_card(rule)

    def _render_rule_card(self, rule):
        card = ctk.CTkFrame(self._alert_rules_scroll,
                            fg_color="#F0F9FF", corner_radius=8,
                            border_width=1, border_color="#BFDBFE")
        card.pack(fill="x", pady=(0, 5))

        # Single compact row
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=5)

        # Threshold badge
        badge = ctk.CTkFrame(row, fg_color=NAV_ACTIVE_BG, corner_radius=6)
        badge.pack(side="left")
        ctk.CTkLabel(badge, text=f"  {rule.threshold}  ",
                     font=(UI_FONT, 10, "bold"), text_color="#FFFFFF",
                     fg_color="transparent").pack(padx=4, pady=3)

        # Occurrence selector
        occ_var = ctk.StringVar(value=rule.occurrence)

        def _on_occ_change(val, rid=rule.rule_id, ov=occ_var):
            ok, msg = update_rule_occurrence(rid, val, actor=self._get_actor())
            if ok:
                self._show_toast("Occurrence updated")

        ctk.CTkOptionMenu(
            row, values=["10-min", "hourly", "bi-hourly"], variable=occ_var,
            width=104, height=26, corner_radius=6, font=FONT_LABEL,
            command=_on_occ_change,
        ).pack(side="left", padx=(6, 0))

        # Email chips — rectangular, same row
        for em in rule.emails:
            chip = ctk.CTkFrame(row, fg_color="#DBEAFE", corner_radius=4)
            chip.pack(side="left", padx=(6, 0))
            ctk.CTkLabel(chip, text=em, font=(UI_FONT, 9),
                         text_color="#1E3A5F",
                         fg_color="transparent").pack(
                         side="left", padx=(6, 2), pady=3)
            ctk.CTkButton(
                chip, text="×", width=16, height=16,
                corner_radius=3, font=(UI_FONT, 9, "bold"),
                fg_color="#BFDBFE", text_color="#1E40AF",
                hover_color="#93C5FD",
                command=lambda rid=rule.rule_id, e=em: self._delete_email(rid, e),
            ).pack(side="left", padx=(0, 3))

        # Trash bin icon — far right
        ctk.CTkButton(
            row, text="🗑", width=28, height=26,
            corner_radius=6, font=(UI_FONT, 12),
            fg_color="#FEE2E2", text_color=CLR_CRIT, hover_color="#FECACA",
            command=lambda rid=rule.rule_id: self._delete_rule(rid),
        ).pack(side="right", padx=(4, 0))

    def _delete_rule(self, rule_id: int):
        if not ALERT_AVAILABLE:
            return
        ok, msg = delete_alert_rule(rule_id, actor=self._get_actor())
        if ok:
            self._refresh_alert_list()
            self._alert_form_status.configure(
                text=f"\u2713  {msg}", text_color=CLR_SAFE)

    def _delete_email(self, rule_id: int, email: str):
        if not ALERT_AVAILABLE:
            return
        ok, msg = delete_alert_email(rule_id, email, actor=self._get_actor())
        if ok:
            self._refresh_alert_list()

    def _save_smtp(self):
        if not ALERT_AVAILABLE:
            return
        try:
            port = int(self._smtp_port.get().strip())
        except ValueError:
            self._smtp_status.configure(
                text="\u26a0  Port must be an integer.", text_color=CLR_CRIT)
            return
        _sec_to_int = {"STARTTLS": 1, "SSL": 2, "None": 0}
        ok, msg = save_smtp_config(
            host       = self._smtp_host.get().strip(),
            port       = port,
            username   = self._smtp_user.get().strip(),
            password   = self._smtp_pwd.get(),
            from_email = self._smtp_from.get().strip(),
            use_tls    = _sec_to_int.get(self._smtp_sec_var.get(), 1),
            actor      = self._get_actor(),
        )
        color = CLR_SAFE if ok else CLR_CRIT
        self._smtp_status.configure(text=msg, text_color=color)

    def _test_smtp(self):
        """Send a test email using the current form values (unsaved)."""
        if not ALERT_AVAILABLE:
            return
        import smtplib, ssl, threading
        from email.message import EmailMessage
        host     = self._smtp_host.get().strip()
        username = self._smtp_user.get().strip()
        # Join on split() removes ALL whitespace (App Passwords are often copied with spaces)
        password = "".join(self._smtp_pwd.get().split())
        from_em  = self._smtp_from.get().strip() or username
        security = self._smtp_sec_var.get()   # "STARTTLS" | "SSL" | "None"
        try:
            port = int(self._smtp_port.get().strip())
        except ValueError:
            self._smtp_status.configure(
                text="\u26a0  Port must be an integer.", text_color=CLR_CRIT)
            return
        if not host or not username or not password:
            self._smtp_status.configure(
                text="\u26a0  Fill in Host, Username and Password first.",
                text_color=CLR_CRIT)
            return

        pwd_len = len(password)
        self._smtp_status.configure(
            text=f"Connecting\u2026  ({security}, user={username}, pwd_len={pwd_len})",
            text_color=CLR_LABEL)
        self.update_idletasks()

        def _send():
            try:
                msg = EmailMessage()
                msg["From"] = from_em
                msg["To"] = username
                msg["Subject"] = "H2 Dashboard - SMTP Test"
                msg.set_content("SMTP connection is working correctly.")
                ctx = ssl.create_default_context()
                if security == "SSL":
                    # Direct SSL \u2014 use for port 465
                    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as s:
                        s.ehlo()
                        s.login(username, password)
                        s.send_message(msg)
                elif security == "STARTTLS":
                    with smtplib.SMTP(host, port, timeout=10) as s:
                        s.ehlo()
                        s.starttls(context=ctx)
                        s.ehlo()
                        s.login(username, password)
                        s.send_message(msg)
                else:   # "None" \u2014 plain, no encryption
                    with smtplib.SMTP(host, port, timeout=10) as s:
                        s.ehlo()
                        s.login(username, password)
                        s.send_message(msg)
                self.after(0, lambda: self._smtp_status.configure(
                    text=f"\u2713  Test email sent to {username}",
                    text_color=CLR_SAFE))
            except smtplib.SMTPAuthenticationError as e:
                code = e.smtp_code
                detail = e.smtp_error.decode(errors="ignore") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                first_line = detail.splitlines()[0] if detail else str(e)
                hint = "  \u2192 Use an App Password (not your regular password)." if "5.7." in first_line else ""
                err_msg = f"\u26a0  Auth error {code} [{pwd_len}-char pwd]: {first_line}{hint}"
                self.after(0, lambda m=err_msg: self._smtp_status.configure(
                    text=m, text_color=CLR_CRIT))
            except Exception as e:
                self.after(0, lambda m=str(e): self._smtp_status.configure(
                    text=f"\u26a0  {m}", text_color=CLR_CRIT))

        threading.Thread(target=_send, daemon=True).start()

    # ── User Management tab ───────────────────────────────────────────────

    def _build_user_mgmt(self, parent) -> ctk.CTkFrame:
        from auth import ROLES

        outer = ctk.CTkFrame(parent, fg_color="transparent")

        # ── Header row ──────────────────────────────────────────────────
        hdr = ctk.CTkFrame(outer, fg_color="transparent")
        hdr.pack(fill="x", padx=32, pady=(28, 0))
        ctk.CTkLabel(hdr, text="User Management",
                     font=(UI_FONT, 16, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(side="left")
        ctk.CTkButton(hdr, text="+ Add User", width=110, height=34,
                      corner_radius=8, font=(UI_FONT, 11, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=self._open_add_user).pack(side="right")

        ctk.CTkFrame(outer, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=32, pady=(16, 0))

        # ── Column header ───────────────────────────────────────────────
        col_hdr = ctk.CTkFrame(outer, fg_color="#F1F5F9", corner_radius=0)
        col_hdr.pack(fill="x", padx=32, pady=(10, 0))
        for txt, w in [("Username", 140), ("Full Name", 200),
                       ("Role", 100), ("Status", 80), ("Actions", 150)]:
            ctk.CTkLabel(col_hdr, text=txt, width=w,
                         font=(UI_FONT, 11, "bold"), text_color=CLR_LABEL,
                         fg_color="transparent", anchor="w").pack(
                         side="left", padx=6, pady=8)

        # ── Scrollable user list ─────────────────────────────────────────
        self._user_scroll = ctk.CTkScrollableFrame(
            outer, fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8")
        self._user_scroll.pack(fill="both", expand=True, padx=32, pady=(4, 4))

        # Status bar
        self._um_status = ctk.CTkLabel(outer, text="", font=(UI_FONT, 9),
                                       text_color=CLR_SAFE,
                                       fg_color="transparent")
        self._um_status.pack(pady=(0, 10))

        return outer

    def _refresh_user_list(self):
        """Rebuild the scrollable user rows from fresh DB data."""
        if not AUTH_AVAILABLE:
            return
        from auth import list_users
        # Clear existing rows
        for w in self._user_scroll.winfo_children():
            w.destroy()

        users = list_users()
        for u in users:
            self._add_user_row(u)

    def _add_user_row(self, user):
        row = ctk.CTkFrame(self._user_scroll, fg_color="#F8FAFC",
                           corner_radius=8)
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(row, text=user.username, width=140,
                     font=(UI_FONT, 12, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent", anchor="w").pack(
                     side="left", padx=6, pady=8)
        ctk.CTkLabel(row, text=user.full_name or "—", width=200,
                     font=(UI_FONT, 11), text_color=CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")
        ctk.CTkLabel(row, text=user.role.capitalize(), width=100,
                     font=(UI_FONT, 11),
                     text_color=NAV_ACTIVE_BG if user.role == "admin" else CLR_LABEL,
                     fg_color="transparent", anchor="w").pack(side="left")

        status_txt   = "Active"   if user.active else "Inactive"
        status_color = CLR_SAFE   if user.active else CLR_CRIT
        ctk.CTkLabel(row, text=status_txt, width=80,
                     font=(UI_FONT, 11), text_color=status_color,
                     fg_color="transparent", anchor="w").pack(side="left")

        # Action buttons
        act = ctk.CTkFrame(row, fg_color="transparent", width=150)
        act.pack(side="left", padx=4)
        is_self = (self._current_user and
                   self._current_user.user_id == user.user_id)
        ctk.CTkButton(
            act, text="Edit", width=58, height=26, corner_radius=6,
            font=(UI_FONT, 10), fg_color="#E2E8F0", text_color=CLR_TITLE,
            hover_color="#CBD5E1",
            command=lambda u=user: self._open_edit_user(u),
        ).pack(side="left", padx=2)
        if not is_self:
            ctk.CTkButton(
                act, text="Delete", width=60, height=26, corner_radius=6,
                font=(UI_FONT, 10), fg_color="#FEE2E2", text_color=CLR_CRIT,
                hover_color="#FECACA",
                command=lambda u=user: self._delete_user(u),
            ).pack(side="left", padx=2)

    # ── Add / Edit user dialogs ───────────────────────────────────────────

    def _open_add_user(self):
        self._user_form_popup(title="Add User", user=None)

    def _open_edit_user(self, user):
        self._user_form_popup(title="Edit User", user=user)

    def _user_form_popup(self, title: str, user):
        from auth import ROLES, create_user, update_user, change_password

        popup = ctk.CTkToplevel(self)
        popup.title(title)
        popup.resizable(False, False)
        popup.configure(fg_color=BG_APP)
        popup.grab_set()

        popup.withdraw()
        popup.update_idletasks()
        pw, ph = 420, (560 if user is None else 520)
        sx = self.winfo_rootx() + (self.winfo_width()  - pw) // 2
        sy = self.winfo_rooty() + (self.winfo_height() - ph) // 2
        popup.geometry(f"{pw}x{ph}+{sx}+{sy}")
        popup.deiconify()

        card = ctk.CTkFrame(popup, fg_color=BG_CARD, corner_radius=16)
        card.place(relx=0.5, rely=0.5, anchor="center",
                   relwidth=0.90, relheight=0.92)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True)

        ctk.CTkLabel(body, text=title,
                     font=(UI_FONT, 15, "bold"), text_color=CLR_TITLE,
                     fg_color="transparent").pack(pady=(22, 4))
        ctk.CTkFrame(body, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=20, pady=(0, 16))

        form_wrap = ctk.CTkScrollableFrame(
            body,
            fg_color="transparent",
            scrollbar_button_color=BG_PILL,
            scrollbar_button_hover_color="#8B9FE8",
        )
        form_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        form = ctk.CTkFrame(form_wrap, fg_color="transparent")
        form.pack(fill="x", padx=12)

        def _lbl(text):
            ctk.CTkLabel(form, text=text, font=(UI_FONT, 11, "bold"),
                         text_color=CLR_TITLE, fg_color="transparent",
                         anchor="w").pack(fill="x")

        _lbl("Username")
        e_user = ctk.CTkEntry(form, height=36, corner_radius=8,
                               font=(UI_FONT, 12), border_color="#CBD5E1")
        if user:
            e_user.insert(0, user.username)
            e_user.configure(state="disabled")
        e_user.pack(fill="x", pady=(4, 12))

        _lbl("Full Name")
        e_name = ctk.CTkEntry(form, height=36, corner_radius=8,
                               font=(UI_FONT, 12), border_color="#CBD5E1")
        if user:
            e_name.insert(0, user.full_name)
        e_name.pack(fill="x", pady=(4, 12))

        _lbl("Role")
        role_default = user.role if (user and user.role in ROLES) else ROLES[1]
        role_var = ctk.StringVar(value=role_default)
        ctk.CTkOptionMenu(form, values=ROLES, variable=role_var,
                          height=36, corner_radius=8,
                          font=(UI_FONT, 12)).pack(fill="x", pady=(4, 12))

        if user is None:
            _lbl("Password")
            e_pw = ctk.CTkEntry(form, show="●", height=36, corner_radius=8,
                                 font=(UI_FONT, 12), border_color="#CBD5E1")
            e_pw.pack(fill="x", pady=(4, 12))
        else:
            _lbl("New Password  (leave blank to keep current)")
            e_pw = ctk.CTkEntry(form, show="●", height=36, corner_radius=8,
                                 font=(UI_FONT, 12), border_color="#CBD5E1",
                                 placeholder_text="Unchanged")
            e_pw.pack(fill="x", pady=(4, 12))

        if user:
            active_var = ctk.BooleanVar(value=user.active)
            ctk.CTkCheckBox(form, text="Account active",
                            variable=active_var,
                            font=(UI_FONT, 11),
                            fg_color=NAV_ACTIVE_BG,
                            hover_color="#075985",
                            checkmark_color="#FFFFFF",
                            corner_radius=4).pack(anchor="w", pady=(0, 12))

        err_lbl = ctk.CTkLabel(body, text="", font=(UI_FONT, 10),
                               text_color=CLR_CRIT, fg_color="transparent")
        err_lbl.pack(pady=(0, 6), padx=22, anchor="w")

        def _save():
            if user is None:
                ok, msg = create_user(
                    username  = e_user.get().strip(),
                    password  = e_pw.get(),
                    full_name = e_name.get().strip(),
                    role      = role_var.get(),
                )
                if ok and ALERT_AVAILABLE:
                    log_journal(self._get_actor(), "User Management", "ADD",
                                f"User '{e_user.get().strip()}' created "
                                f"(role={role_var.get()})")
            else:
                ok, msg = update_user(
                    user_id   = user.user_id,
                    full_name = e_name.get().strip(),
                    role      = role_var.get(),
                    active    = active_var.get(),
                )
                if ok and e_pw.get():
                    ok, msg = change_password(user.user_id, e_pw.get())
                if ok and ALERT_AVAILABLE:
                    log_journal(self._get_actor(), "User Management", "UPDATE",
                                f"User '{user.username}' updated "
                                f"(role={role_var.get()}, "
                                f"active={active_var.get()})")

            if ok:
                popup.destroy()
                self._refresh_user_list()
                self._um_status.configure(
                    text="\u2713  Saved successfully.", text_color=CLR_SAFE)
            else:
                err_lbl.configure(text=f"\u26a0  {msg}")

        footer = ctk.CTkFrame(card, fg_color="#F8FAFC", corner_radius=0)
        footer.pack(fill="x", side="bottom")
        ctk.CTkFrame(footer, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x")
        btn_row = ctk.CTkFrame(footer, fg_color="transparent")
        btn_row.pack(fill="x", padx=22, pady=16)
        ctk.CTkButton(btn_row, text="Cancel", height=38, width=110,
                      corner_radius=10, font=(UI_FONT, 12),
                      fg_color="#E2E8F0", text_color=CLR_TITLE,
                      hover_color="#CBD5E1",
                      command=popup.destroy).pack(side="right")
        ctk.CTkButton(btn_row, text="Save", height=38, width=120,
                      corner_radius=10,
                      font=(UI_FONT, 12, "bold"),
                      fg_color=NAV_ACTIVE_BG, hover_color="#075985",
                      command=_save).pack(side="right", padx=(0, 10))

        popup.bind("<Return>", lambda _e: _save())

    def _delete_user(self, user):
        from auth import delete_user as _del
        if self._current_user and self._current_user.user_id == user.user_id:
            self._um_status.configure(
                text="\u26a0  Cannot delete your own account.",
                text_color=CLR_CRIT)
            return
        ok, msg = _del(user.user_id)
        if ok:
            self._refresh_user_list()
            self._um_status.configure(
                text=f"\u2713  User '{user.username}' deleted.",
                text_color=CLR_SAFE)
            if ALERT_AVAILABLE:
                log_journal(self._get_actor(), "User Management", "DELETE",
                            f"User '{user.username}' deleted")
        else:
            self._um_status.configure(
                text=f"\u26a0  {msg}", text_color=CLR_CRIT)


# =============================================================================
# Generic scroll router — installed once, works for all CTkScrollableFrames
# =============================================================================
def _install_global_scroll(root: ctk.CTk) -> None:
    """
    Bind a single <MouseWheel> handler on the root window that routes every
    scroll event to the innermost CTkScrollableFrame currently under the cursor.
    This replaces all per-widget / bind_all hacks and works automatically for
    any CTkScrollableFrame anywhere in the app, including nested ones.
    """
    def _find_scroll_canvas(widget):
        """Walk up the widget hierarchy and return the _parent_canvas of the
        nearest enclosing CTkScrollableFrame, or None."""
        w = widget
        while w:
            if isinstance(w, ctk.CTkScrollableFrame):
                return w._parent_canvas
            parent_name = w.winfo_parent()
            if not parent_name:       # reached the root — no scrollable frame found
                break
            try:
                w = w.nametowidget(parent_name)
            except Exception:
                break
        return None

    def _on_wheel(event):
        canvas = _find_scroll_canvas(event.widget)
        if canvas:
            canvas.yview_scroll(int(-event.delta / 6), "units")
            return "break"

    root.bind_all("<MouseWheel>", _on_wheel)


# =============================================================================
# Main Dashboard Window
# =============================================================================
class DashboardApp(ctk.CTk):
    _DB_WRITE_INTERVAL = 60.0   # seconds between DB writes per device
    _DB_REFRESH_INTERVAL_MS = 60000
    _STATS_SAMPLE_INTERVAL_MS = 1000
    _STATS_WINDOW_SECONDS = 60
    _ONLINE_STALE_SECONDS = 180.0

    def __init__(self, devices: list, scanner=None,
                 device_id_map: dict | None = None,
                 current_user=None):
        super().__init__()
        self._scanner       = scanner
        self._devices       = devices
        self._device_id_map: dict[int, int] = device_id_map or {}
        self._last_db_write: dict[int, float] = {}
        self._last_seen_recorded_at: dict[int, str] = {}
        self._db_refresh_after_id = None
        self._minute_samples: dict[int, collections.deque] = {}
        self._minute_stats: dict[int, dict[str, float]] = {}
        self._last_live_polled_at: dict[int, str] = {}
        self._last_stats_flush_ts: float = time.time()
        if scanner is None:
            self._mode = "Service"
        else:
            self._mode = "Mock" if isinstance(scanner, MockScanner) else "RS485"
        self._current_user  = current_user
        self._total_devices_count = self._resolve_total_devices_count(
            fallback=len(devices)
        )

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("H2 Gas Detector Dashboard")
        n    = len(devices)
        cols = min(MAX_COLS, n) if n > 0 else MAX_COLS
        self.minsize(1000, 640)
        self.configure(fg_color=BG_APP)
        # Build fully off-screen by making window transparent, then zoom + reveal
        self.wm_attributes("-alpha", 0.0)

        # ── Header ────────────────────────────────────────────────────────
        self._header = DashboardHeader(
            self, n, self._mode,
            current_user=current_user,
            on_logout=self._on_logout,
        )
        self._header.pack(fill="x", padx=16, pady=(8, 4))

        # ── Body: sidebar + content area ──────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG_APP, corner_radius=0, border_width=0)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._sidebar = Sidebar(body, on_navigate=self._navigate,
                    current_user=current_user)
        self._sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 10), pady=(24, 8))

        content = ctk.CTkFrame(body, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        # ── Views ─────────────────────────────────────────────────────────
        self._dash_view      = DashboardView(content, devices, cols,
                                             current_user=current_user,
                                             on_rename=self._on_device_rename)
        self._analytics_view = AnalyticsView(content, devices)
        self._settings_view  = SettingsView(content, current_user=current_user)
        self._journal_view   = JournalView(content, current_user=current_user)

        self._views: dict[str, ctk.CTkFrame] = {
            "Dashboard": self._dash_view,
            "Analytics": self._analytics_view,
            "Settings":  self._settings_view,
            "Journal":   self._journal_view,
        }
        for v in self._views.values():
            v.grid(row=0, column=0, sticky="nsew")
            v.grid_remove()

        self._current_view: ctk.CTkFrame | None = None
        self._navigate("Dashboard")

        # Install generic scroll router after all widgets are realised
        self.after_idle(lambda: _install_global_scroll(self))

        # All widgets built — maximise then reveal in one paint (no flicker)
        self.state("zoomed")
        self.update_idletasks()
        self.wm_attributes("-alpha", 1.0)

        # ── Runtime mode ───────────────────────────────────────────────────
        if scanner is not None:
            addresses = [d["address"] for d in devices if d.get("online", True)]
            self._engine = PollingEngine(scanner, addresses,
                                         callback=self._on_poll, interval=2.0)
            self._engine.start()
        else:
            self._engine = None
            self._seed_startup_stats()
            self.after(self._STATS_SAMPLE_INTERVAL_MS, self._collect_live_samples)
            self.after(300, self._refresh_from_db)

        self._tick_clock()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _seed_startup_stats(self):
        """Pre-populate minute stats from persistent live_cache
        so cards show Avg/Min/Max immediately on startup instead of '--'."""
        if not DB_AVAILABLE:
            return
        try:
            for dev in self._devices:
                addr = dev.get("address")
                device_id = self._device_id_map.get(addr)
                if not device_id:
                    continue
                rows = get_live_cache_recent(device_id, limit=self._STATS_WINDOW_SECONDS)
                if not rows:
                    continue
                values = [float(r["concentration_value"]) for r in rows]
                buf = self._minute_samples.setdefault(
                    addr,
                    collections.deque(maxlen=self._STATS_WINDOW_SECONDS),
                )
                buf.extend(reversed(values))
                self._last_live_polled_at[addr] = str(rows[0].get("polled_at", ""))
                self._minute_stats[addr] = {
                    "avg_1m": round(sum(values) / len(values), 2),
                    "min_1m": round(min(values), 2),
                    "max_1m": round(max(values), 2),
                }
        except Exception:
            logger.exception("Failed to seed startup stats from live_cache.")

    def _shutdown_runtime(self):
        """Stop polling and release external resources (DB + scanner)."""
        try:
            if self._engine is not None:
                self._engine.stop()
        except Exception:
            pass

        try:
            if self._scanner and hasattr(self._scanner, "close"):
                self._scanner.close()
        except Exception:
            logger.exception("Error closing scanner on shutdown.")

        if DB_AVAILABLE:
            try:
                close_connection()
            except Exception:
                logger.exception("Error closing DB on shutdown.")

    def _resolve_total_devices_count(self, fallback: int) -> int:
        """Return total devices from device_master (fallback to provided value)."""
        if not DB_AVAILABLE:
            return fallback
        try:
            plant_id = get_default_plant_id()
            return len(get_all_devices(plant_id))
        except Exception:
            logger.exception("Failed to resolve total devices count from DB.")
            return fallback

    def _sync_new_devices_from_db(self):
        """Add newly discovered DB devices to the UI at runtime (no restart)."""
        if not DB_AVAILABLE:
            return

        try:
            plant_id = get_default_plant_id()
            db_devices = get_all_devices(plant_id)
        except Exception:
            logger.exception("Failed to load devices for runtime sync.")
            return

        known_addrs = {
            int(d.get("address"))
            for d in self._devices
            if d.get("address") is not None
        }
        added = 0

        for db_dev in sorted(db_devices, key=lambda d: int(d.get("device_address", 0))):
            addr = int(db_dev.get("device_address", 0) or 0)
            if addr <= 0:
                continue

            device_id = int(db_dev.get("id", 0) or 0)
            if device_id > 0:
                self._device_id_map[addr] = device_id

            if addr in known_addrs:
                continue

            latest = get_latest_reading(device_id) if device_id > 0 else None
            concentration = float((latest or {}).get("concentration_value") or 0.0)
            low_alarm = float((latest or {}).get("low_alarm") or 0.0)
            high_alarm = float((latest or {}).get("high_alarm") or 0.0)
            alarm_status = int((latest or {}).get("alarm_status", 1) or 1)
            last_updated = (latest or {}).get("recorded_at")

            dev_info = {
                "address": addr,
                "device_name": db_dev.get("device_name", f"Device {addr:03d}"),
                "gas_name": db_dev.get("gas_type", "H2"),
                "gas_unit": "ppm",
                "device_range": db_dev.get("device_range", 0.0),
                "concentration": concentration,
                "low_alarm": low_alarm,
                "high_alarm": high_alarm,
                "alarm_status": alarm_status,
                "online": bool(int(db_dev.get("device_status_flag", 0) or 0)),
                "last_updated": last_updated,
            }

            self._devices.append(dev_info)
            self._dash_view.add_or_update_device(dev_info)
            self._analytics_view.add_device(dev_info)
            known_addrs.add(addr)
            added += 1

        if added:
            self._devices.sort(key=lambda d: int(d.get("address", 0) or 0))
            self._total_devices_count = len(known_addrs)
            logger.info("Runtime device sync: added %d new device(s) from DB", added)

    # ── Navigation ────────────────────────────────────────────────────────
    def _navigate(self, name: str):
        if self._current_user and self._current_user.role == "operator":
            if name not in {"Dashboard", "Analytics", "Settings", "Journal"}:
                return
        if self._current_view is not None:
            self._current_view.grid_remove()
        self._current_view = self._views[name]
        self._current_view.grid()
        if name == "Journal":
            self._journal_view.refresh()

    # ── Device rename propagation ─────────────────────────────────────────
    def _on_device_rename(self, addr: int, new_name: str):
        """Called by SensorCard after a successful rename; keeps analytics in sync."""
        for dev in self._devices:
            if dev.get("address") == addr:
                dev["device_name"] = new_name
                break
        self._analytics_view.update_device_name(addr, new_name)

    # ── Logout ────────────────────────────────────────────────────────────
    def _on_logout(self):
        """Stop polling, destroy dashboard, re-launch login + scan flow."""
        self._shutdown_runtime()
        self.destroy()
        main()   # restart from login

    # ── Poll handling ─────────────────────────────────────────────────────
    def _on_poll(self, results):
        self.after(0, self._process_poll, results)

    def _append_minute_sample(self, addr: int, concentration: float):
        buf = self._minute_samples.setdefault(
            addr,
            collections.deque(maxlen=self._STATS_WINDOW_SECONDS),
        )
        buf.append(float(concentration))

    def _flush_minute_stats(self):
        for dev in self._devices:
            addr = dev.get("address")
            buf = self._minute_samples.setdefault(
                addr,
                collections.deque(maxlen=self._STATS_WINDOW_SECONDS),
            )
            if buf:
                values = list(buf)
                self._minute_stats[addr] = {
                    "avg_1m": round(sum(values) / len(values), 2),
                    "min_1m": round(min(values), 2),
                    "max_1m": round(max(values), 2),
                }
            else:
                self._minute_stats[addr] = {
                    "avg_1m": None,
                    "min_1m": None,
                    "max_1m": None,
                }
            buf.clear()

        self._last_stats_flush_ts = time.time()

    def _collect_live_samples(self):
        has_new_points = False
        if DB_AVAILABLE:
            try:
                live_rows = get_all_live_cache_latest()
                for live in live_rows:
                    addr = live.get("device_address")
                    if addr is None:
                        continue
                    polled_at = str(live.get("polled_at", ""))
                    if not polled_at:
                        continue
                    if self._last_live_polled_at.get(int(addr)) == polled_at:
                        continue
                    self._last_live_polled_at[int(addr)] = polled_at
                    try:
                        polled_dt = datetime.datetime.fromisoformat(polled_at.replace("T", " "))
                    except Exception:
                        try:
                            polled_dt = datetime.datetime.strptime(polled_at.replace("T", " "), "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            polled_dt = datetime.datetime.now()

                    conc_val = float(live.get("concentration_value") or 0.0)
                    self._append_minute_sample(
                        int(addr),
                        conc_val,
                    )
                    self._analytics_view.push_reading(int(addr), polled_dt, conc_val)
                    has_new_points = True

                # Detect new transaction inserts and refresh cards immediately.
                # This removes the 1-minute perceived lag vs Device Logs.
                txn_changed = False
                txn_rows = get_all_latest_transaction_readings()
                for txn in txn_rows:
                    addr = txn.get("device_address")
                    recorded_at = str(txn.get("recorded_at", ""))
                    if addr is None or not recorded_at:
                        continue
                    addr_i = int(addr)
                    if self._last_seen_recorded_at.get(addr_i) != recorded_at:
                        self._last_seen_recorded_at[addr_i] = recorded_at
                        txn_changed = True
                if txn_changed:
                    self._refresh_from_db()
                
                # Also check device status flags periodically to detect offline state changes
                # even when no new transactions are being generated.
                self._check_device_status_flags()
            except Exception:
                logger.exception("Live sample collection failed.")

        if has_new_points and self._current_view == self._analytics_view:
            self._analytics_view.redraw_trend()

        self.after(self._STATS_SAMPLE_INTERVAL_MS, self._collect_live_samples)

    def _check_device_status_flags(self):
        """Check device status flags and refresh cards if any device status changed."""
        if not DB_AVAILABLE or not self._devices:
            return
        
        try:
            plant_id = get_default_plant_id()
            current_status_map: dict[int, bool] = {}
            for db_dev in get_all_devices(plant_id):
                addr = int(db_dev.get("device_address", 0))
                online = bool(int(db_dev.get("device_status_flag", 0) or 0))
                current_status_map[addr] = online
            
            # Check if any device status changed
            status_changed = False
            if not hasattr(self, '_last_status_map'):
                self._last_status_map = {}
            
            for addr, is_online in current_status_map.items():
                prev_online = self._last_status_map.get(addr)
                if prev_online is not None and prev_online != is_online:
                    # Status changed for this device, trigger full refresh
                    status_changed = True
                    break
            
            if status_changed:
                # Update the last status map
                self._last_status_map = current_status_map.copy()
                # Trigger a full DB refresh to update card visuals
                self._refresh_from_db()
        except Exception:
            logger.exception("Device status check failed.")

    def _schedule_next_db_refresh(self):
        """Schedule DB refresh at next minute boundary (HH:MM:00)."""
        now_dt = datetime.datetime.now()
        next_minute = now_dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        delay_ms = max(1, int((next_minute - now_dt).total_seconds() * 1000))
        try:
            if self._db_refresh_after_id is not None:
                self.after_cancel(self._db_refresh_after_id)
        except Exception:
            pass
        self._db_refresh_after_id = self.after(delay_ms, self._refresh_from_db)

    def _refresh_from_db(self):
        if not DB_AVAILABLE:
            self._schedule_next_db_refresh()
            return

        try:
            self._sync_new_devices_from_db()

            now_dt = datetime.datetime.now()
            if (time.time() - self._last_stats_flush_ts) >= self._STATS_WINDOW_SECONDS:
                self._flush_minute_stats()

            online_count = 0
            results = []

            txn_rows = get_all_latest_transaction_readings()
            txn_map = {r.get("device_address"): r for r in txn_rows}

            status_map: dict[int, bool] = {}
            try:
                plant_id = get_default_plant_id()
                for db_dev in get_all_devices(plant_id):
                    status_map[int(db_dev.get("device_address", 0))] = bool(int(db_dev.get("device_status_flag", 0) or 0))
            except Exception:
                logger.exception("Failed to read device status flags from DB.")

            for dev in self._devices:
                addr = dev.get("address")
                txn = txn_map.get(addr)

                info = {
                    "address": addr,
                    "device_name": dev.get("device_name", f"Device {addr:03d}"),
                    "gas_name": dev.get("gas_name", "H2"),
                    "gas_unit": "ppm",
                    "device_range": dev.get("device_range", 0.0),
                    "concentration": 0.0,
                    "low_alarm": 0.0,
                    "high_alarm": 0.0,
                    "alarm_status": 1,
                    "last_updated": dev.get("last_updated"),
                    "avg_1m": None,
                    "min_1m": None,
                    "max_1m": None,
                    "online": False,
                }

                minute_stats = self._minute_stats.get(addr)
                if minute_stats:
                    info.update(minute_stats)

                if txn and txn.get("recorded_at"):
                    txn_raw = str(txn.get("recorded_at", "")).replace("T", " ")
                    try:
                        txn_dt = datetime.datetime.fromisoformat(txn_raw)
                    except Exception:
                        try:
                            txn_dt = datetime.datetime.strptime(txn_raw, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            txn_dt = now_dt

                    info.update({
                        "concentration": float(txn.get("concentration_value") or 0.0),
                        "low_alarm": float(txn.get("low_alarm") or 0.0),
                        "high_alarm": float(txn.get("high_alarm") or 0.0),
                        "alarm_status": int(txn.get("alarm_status", 1) or 1),
                        "last_updated": txn.get("recorded_at"),
                    })

                    self._last_seen_recorded_at[int(addr)] = str(txn.get("recorded_at", ""))
                    dev["last_updated"] = txn.get("recorded_at")

                info["online"] = bool(status_map.get(int(addr), False))
                if info["online"]:
                    online_count += 1

                results.append(info)

            self._dash_view.update(results)
            self._analytics_view.redraw_trend()
            total = self._total_devices_count or len(results)
            self._sidebar.update_status(online_count, total)
        except Exception:
            logger.exception("DB refresh loop failed.")

        self._schedule_next_db_refresh()

    def _process_poll(self, results: list):
        now    = time.time()
        now_dt = datetime.datetime.now()

        if (now - self._last_stats_flush_ts) >= self._STATS_WINDOW_SECONDS:
            self._flush_minute_stats()

        # Update sensor cards
        for info in results:
            addr = info.get("address")
            if addr is None:
                continue

            if info.get("online", False):
                try:
                    self._append_minute_sample(int(addr), float(info.get("concentration", 0.0)))
                except Exception:
                    pass

            minute_stats = self._minute_stats.get(addr)
            if minute_stats:
                info.update(minute_stats)

        self._dash_view.update(results)

        n_online = 0
        for info in results:
            addr = info.get("address")
            if not info.get("online", False):
                continue
            n_online += 1

            if not info.get("last_updated"):
                info["last_updated"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")

            raw_conc = float(info.get("concentration", 0.0) or 0.0)
            k_factor = 1.0
            if DB_AVAILABLE:
                try:
                    k_factor = float(get_k_factor_for_device_address(int(addr)))
                except Exception:
                    k_factor = 1.0
            conc = raw_conc * k_factor
            info["concentration"] = conc

            # Push to analytics trend graph
            self._analytics_view.push_reading(addr, now_dt, conc)

            # Check and fire email alerts
            if ALERT_AVAILABLE:
                try:
                    check_and_fire(addr, conc)
                except Exception:
                    pass

            # Per-minute DB write
            if DB_AVAILABLE and addr in self._device_id_map:
                elapsed = now - self._last_db_write.get(addr, 0.0)
                if elapsed >= self._DB_WRITE_INTERVAL:
                    try:
                        insert_reading(
                            device_id           = self._device_id_map[addr],
                            concentration_value = conc,
                            low_alarm           = info.get("low_alarm",  0.0),
                            high_alarm          = info.get("high_alarm", 0.0),
                            alarm_status        = info.get("alarm_status", 0),
                        )
                        self._last_db_write[addr] = now
                    except Exception:
                        logger.exception(
                            "DB write failed for device addr=%d.", addr)

        # Redraw trend graph (only when analytics view is active)
        self._analytics_view.redraw_trend()

        # Update sidebar status
        total = self._total_devices_count or len(results)
        self._sidebar.update_status(n_online, total)

    # ── Clock ─────────────────────────────────────────────────────────────
    def _tick_clock(self):
        self._header.update_time()
        self.after(1000, self._tick_clock)

    # ── Shutdown ──────────────────────────────────────────────────────────
    def _on_close(self):
        self._shutdown_runtime()
        self.destroy()


# =============================================================================
# Entry Point
# =============================================================================
def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("dark-blue")

    if DB_AVAILABLE:
        try:
            initialise_schema()
        except Exception:
            logger.exception("Failed to initialise database schema during dashboard startup")

    root = ctk.CTk()
    root.withdraw()                                # hidden until scan done

    # ── Step 1: Login ─────────────────────────────────────────────────────
    _logged_in_user = [None]   # mutable container to capture across closures

    def load_devices_from_db() -> tuple[list, dict[int, int]]:
        devices: list[dict] = []
        device_id_map: dict[int, int] = {}
        if not DB_AVAILABLE:
            return devices, device_id_map

        try:
            initialise_schema()
            plant_id = get_default_plant_id()

            for db_dev in get_all_devices(plant_id):
                addr = db_dev["device_address"]
                device_id = db_dev["id"]
                device_id_map[addr] = device_id

                latest = get_latest_reading(device_id)
                concentration = 0.0
                low_alarm = 0.0
                high_alarm = 0.0
                alarm_status = 1
                online = bool(int(db_dev.get("device_status_flag", 0) or 0))

                if latest:
                    concentration = float(latest.get("concentration_value") or 0.0)
                    low_alarm = float(latest.get("low_alarm") or 0.0)
                    high_alarm = float(latest.get("high_alarm") or 0.0)
                    alarm_status = int(latest.get("alarm_status", 1) or 1)

                # Add last_updated from latest reading's recorded_at
                last_updated = None
                if latest and latest.get("recorded_at"):
                    last_updated = latest["recorded_at"]
                elif db_dev.get("last_seen"):
                    last_updated = db_dev["last_seen"]

                devices.append({
                    "address": addr,
                    "device_name": db_dev.get("device_name", f"Device {addr:03d}"),
                    "gas_name": db_dev.get("gas_type", "H2"),
                    "gas_unit": "ppm",
                    "device_range": db_dev.get("device_range", 0.0),
                    "concentration": concentration,
                    "low_alarm": low_alarm,
                    "high_alarm": high_alarm,
                    "alarm_status": alarm_status,
                    "online": online,
                    "last_updated": last_updated,
                })
        except Exception:
            logger.exception("Failed to load devices from DB for UI startup.")

        devices.sort(key=lambda d: d.get("address", 0))
        return devices, device_id_map

    def on_login_success(user):
        _logged_in_user[0] = user
        devices, device_id_map = load_devices_from_db()
        root.destroy()
        app = DashboardApp(
            devices,
            scanner=None,
            device_id_map=device_id_map,
            current_user=_logged_in_user[0],
        )
        app.mainloop()

    from login_window import LoginWindow
    LoginWindow(root, on_success=on_login_success)

    root.mainloop()


if __name__ == "__main__":
    main()
