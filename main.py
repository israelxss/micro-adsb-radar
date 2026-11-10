#!/usr/bin/env python3
"""
Micro ADS-B Radar & Telemetry Suite (1090 MHz)
All-in-one pure Python ADS-B / Mode-S system:
  1. Complete Pure-Python Decoding Engine (Zero pyModeS dependency)
  2. Offline Aircraft Metadata Database & ICAO Country Resolver
  3. Live Terminal Radar Dashboard (Rich UI + ANSI fallback)
  4. UDP Server / Broadcaster (Unicast & Subnet Broadcast)
  5. UDP Client & Headless JSON Streamer
  6. Raw Traffic Recorder & Log Replay Simulator
Only requires 'pyserial' for hardware serial communications.
"""

import sys
import os
import re
import csv
import gzip
import json
import time
import math
import socket
import threading
import argparse
from datetime import datetime
from collections import deque
from typing import Generator, Optional, Dict, Any, Tuple
from bisect import bisect_right
from math import floor

# ============================================================================
# Windows Console UTF-8 & Virtual Terminal Setup
# ============================================================================

if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        h_out = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Optional Rich rendering with automatic pure-Python fallback
try:
    from rich.console import Console, Group
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

import serial

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

# ============================================================================
# SECTION 1: Configuration Management
# ============================================================================

def load_config() -> dict:
    """Loads configuration from config.json with fallback defaults."""
    default_config = {
        "serial": {
            "port": "COM12",
            "baudrate": 115200,
            "timeout": 0.2
        },
        "observer": {
            "enabled": True,
            "latitude": 31.9830,
            "longitude": 34.7818
        },
        "radar": {
            "prune_timeout_seconds": 30.0,
            "stale_timeout_seconds": 10.0,
            "cpr_pair_max_interval_seconds": 10.0
        },
        "udp": {
            "listen_host": "0.0.0.0",
            "listen_port": 5005,
            "target_host": "127.0.0.1",
            "target_port": 5005,
            "broadcast": False
        },
        "logging": {
            "save_raw_traffic": False,
            "raw_traffic_file": "adsb_traffic.log"
        }
    }

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                for section in default_config:
                    if section in loaded and isinstance(loaded[section], dict):
                        default_config[section].update(loaded[section])
        except Exception as e:
            print(f"[WARNING] Failed to load config.json: {e}. Using defaults.")

    return default_config

def parse_raw_line(line: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parses hardware and log lines:
    1. #MDS*<HEX>;(count,rssi,err,flags)
    2. AVR format: *<HEX>;
    """
    line = line.strip()
    if not line:
        return None, None

    m = re.search(r'#MDS\*([0-9A-Fa-f]+);\s*\((-?\d+),(-?\d+)', line)
    if m:
        return m.group(1).upper(), int(m.group(3))

    m = re.search(r'\*([0-9A-Fa-f]{14,28});', line)
    if m:
        return m.group(1).upper(), None

    return None, None

# ============================================================================
# SECTION 2: Offline Aircraft Metadata Database & Country Allocation
# ============================================================================

_ICAO_COUNTRY_RANGES = (
    (0x738000, 0x738FFF, "Israel", "4X"),
    (0xA00000, 0xAFFFFF, "United States", "N"),
    (0x400000, 0x43FFFF, "United Kingdom", "G"),
    (0x380000, 0x3BFFFF, "France", "F"),
    (0x3C0000, 0x3FFFFF, "Germany", "D"),
    (0x300000, 0x33FFFF, "Italy", "I"),
    (0x4B8000, 0x4BFFFF, "Turkey", "TC"),
    (0x468000, 0x46FFFF, "Greece", "SX"),
    (0x4C8000, 0x4CFFFF, "Cyprus", "5B"),
    (0x440000, 0x447FFF, "Austria", "OE"),
    (0x4B0000, 0x4B7FFF, "Switzerland", "HB"),
    (0x480000, 0x487FFF, "Netherlands", "PH"),
    (0x448000, 0x44FFFF, "Belgium", "OO"),
    (0x4CA000, 0x4CAFFF, "Ireland", "EI"),
    (0x470000, 0x477FFF, "Hungary", "HA"),
    (0x4D2000, 0x4D2FFF, "Malta", "9H"),
    (0x4A0000, 0x4A7FFF, "Romania", "YR"),
    (0x896000, 0x896FFF, "United Arab Emirates", "A6"),
    (0x750000, 0x750FFF, "Jordan", "JY"),
    (0x010000, 0x017FFF, "Egypt", "SU"),
    (0x140000, 0x15FFFF, "Russia", "RA"),
    (0x508000, 0x50FFFF, "Ukraine", "UR"),
    (0x760000, 0x767FFF, "Saudi Arabia", "HZ"),
    (0x06A000, 0x06AFFF, "Qatar", "A7"),
    (0x700000, 0x700FFF, "Kuwait", "9K"),
    (0x70C000, 0x70CFFF, "Bahrain", "A9C"),
    (0x780000, 0x7BFFFF, "China", "B"),
    (0x840000, 0x87FFFF, "Japan", "JA"),
    (0xC00000, 0xC3FFFF, "Canada", "C"),
    (0x7C0000, 0x7FFFFF, "Australia", "VH"),
    (0xE00000, 0xE3FFFF, "Brazil", "PP"),
    (0x340000, 0x37FFFF, "Spain", "EC"),
    (0x490000, 0x497FFF, "Portugal", "CS"),
    (0x458000, 0x45FFFF, "Denmark", "OY"),
    (0x4A8000, 0x4AFFFF, "Sweden", "SE"),
    (0x478000, 0x47FFFF, "Norway", "LN"),
    (0x460000, 0x467FFF, "Finland", "OH"),
    (0x498000, 0x49FFFF, "Poland", "SP"),
    (0x488000, 0x48FFFF, "Czech Republic", "OK"),
)

def get_icao_country(icao_hex: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolves country name and civil registration prefix from ICAO 24-bit address."""
    try:
        val = int(icao_hex, 16)
    except ValueError:
        return None, None
    for low, high, country, prefix in _ICAO_COUNTRY_RANGES:
        if low <= val <= high:
            return country, prefix
    return None, None


class AircraftMetadataDB:
    """
    Offline Aircraft Metadata Database.
    Automatically detects and parses offline database files:
      1. aircraft.csv.gz / aircraft.csv (tar1090-db / readsb semicolon-delimited)
      2. aircraftDatabase.csv (OpenSky Network comma-delimited)
      3. aircraft_db.csv (bundled starter database)
    Works 100% offline with zero network connectivity required.
    """
    def __init__(self, search_dir: Optional[str] = None):
        self.db: Dict[str, Dict[str, str]] = {}
        target_dir = search_dir or os.path.dirname(__file__)
        self.loaded_source: Optional[str] = None
        self._load_database(target_dir)

    def _load_database(self, base_dir: str):
        candidates = [
            os.path.join(base_dir, "aircraft.csv.gz"),
            os.path.join(base_dir, "aircraft.csv"),
            os.path.join(base_dir, "aircraftDatabase.csv"),
            os.path.join(base_dir, "aircraft-database.csv"),
            os.path.join(base_dir, "aircraft_db.csv"),
        ]

        for path in candidates:
            if os.path.exists(path):
                try:
                    self._parse_file(path)
                    self.loaded_source = os.path.basename(path)
                    break
                except Exception as e:
                    print(f"[WARNING] Failed to parse {path}: {e}")

    def _parse_file(self, path: str):
        is_gz = path.endswith(".gz")
        opener = gzip.open(path, "rt", encoding="utf-8", errors="ignore") if is_gz else open(path, "r", encoding="utf-8", errors="ignore")

        with opener as f:
            first_line = f.readline()
            if not first_line:
                return

            if ";" in first_line:
                # tar1090-db format: icao;registration;type;flags;description;operator
                f.seek(0)
                for line in f:
                    parts = line.strip().split(";")
                    if len(parts) >= 3:
                        icao = parts[0].strip().upper()
                        if icao and len(icao) == 6:
                            self.db[icao] = {
                                "reg": parts[1].strip(),
                                "type": parts[2].strip(),
                                "model": parts[4].strip() if len(parts) > 4 else "",
                                "operator": parts[5].strip() if len(parts) > 5 else ""
                            }
            else:
                # CSV format (bundled or OpenSky)
                f.seek(0)
                reader = csv.reader(f)
                header = [h.strip().lower() for h in next(reader, [])]

                icao_idx = -1
                reg_idx = -1
                type_idx = -1
                model_idx = -1
                op_idx = -1

                for i, col in enumerate(header):
                    if col in ("icao", "icao24"):
                        icao_idx = i
                    elif col in ("registration", "reg"):
                        reg_idx = i
                    elif col in ("type", "typecode"):
                        type_idx = i
                    elif col in ("model", "description"):
                        model_idx = i
                    elif col in ("operator", "owner", "operatorcallsign"):
                        op_idx = i

                if icao_idx != -1:
                    for row in reader:
                        if len(row) > icao_idx:
                            icao = row[icao_idx].strip().upper()
                            if icao and len(icao) == 6:
                                self.db[icao] = {
                                    "reg": row[reg_idx].strip() if reg_idx != -1 and len(row) > reg_idx else "",
                                    "type": row[type_idx].strip() if type_idx != -1 and len(row) > type_idx else "",
                                    "model": row[model_idx].strip() if model_idx != -1 and len(row) > model_idx else "",
                                    "operator": row[op_idx].strip() if op_idx != -1 and len(row) > op_idx else ""
                                }

    def lookup(self, icao_hex: str) -> Dict[str, str]:
        icao_hex = icao_hex.strip().upper()
        res = self.db.get(icao_hex, {}).copy()
        country, prefix = get_icao_country(icao_hex)
        res["country"] = country or "Unknown"
        res["country_prefix"] = prefix or ""
        if "reg" not in res:
            res["reg"] = ""
        if "type" not in res:
            res["type"] = ""
        if "model" not in res:
            res["model"] = ""
        if "operator" not in res:
            res["operator"] = ""
        return res

# ============================================================================
# SECTION 3: Pure-Python ADS-B / Mode-S Decoding Engine
# ============================================================================

_CRC_POLY = 0xFFF409

def _build_crc_table() -> tuple[int, ...]:
    table = [0] * 256
    for i in range(256):
        c = i << 16
        for _ in range(8):
            c = ((c << 1) ^ _CRC_POLY) if (c & 0x800000) else (c << 1)
        table[i] = c & 0xFFFFFF
    return tuple(table)

_CRC_TABLE: tuple[int, ...] = _build_crc_table()

def crc_remainder(n: int, length: int) -> int:
    n_data_bytes = (length - 24) // 8
    crc = 0
    shift = length - 8
    for _ in range(n_data_bytes):
        byte = (n >> shift) & 0xFF
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC_TABLE[((crc >> 16) ^ byte) & 0xFF]
        shift -= 8
    parity = n & 0xFFFFFF
    return (crc ^ parity) & 0xFFFFFF

def is_valid_crc(msg_hex: str) -> bool:
    try:
        return crc_remainder(int(msg_hex, 16), len(msg_hex) * 4) == 0
    except ValueError:
        return False

def _build_callsign_table() -> str:
    table = []
    for i in range(64):
        if 1 <= i <= 26:
            table.append(chr(i | 0x40))
        elif i == 32 or (48 <= i <= 57):
            table.append(chr(i))
        else:
            table.append("#")
    return "".join(table)

_CALLSIGN_TABLE: str = _build_callsign_table()

def decode_callsign(bits: int) -> str:
    chars = [_CALLSIGN_TABLE[(bits >> (42 - 6 * i)) & 0x3F] for i in range(8)]
    return "".join(chars).strip("# ").strip()

def _gray_to_int(gray: int) -> int:
    num = 0
    while gray > 0:
        num ^= gray
        gray >>= 1
    return num

def altcode_to_altitude(ac: int) -> Optional[int]:
    if ac == 0:
        return None
    m_bit = (ac >> 6) & 0x1
    q_bit = (ac >> 4) & 0x1
    if m_bit == 0 and q_bit == 1:
        n = ((ac >> 2) & 0x7E0) | ((ac >> 1) & 0x10) | (ac & 0xF)
        return n * 25 - 1000

    if m_bit == 0 and q_bit == 0:
        c1 = (ac >> 11) & 1
        a1 = (ac >> 10) & 1
        c2 = (ac >> 9) & 1
        a2 = (ac >> 8) & 1
        c4 = (ac >> 7) & 1
        a4 = (ac >> 6) & 1
        b1 = (ac >> 4) & 1
        d1 = (ac >> 3) & 1
        b2 = (ac >> 2) & 1
        d2 = (ac >> 1) & 1
        b4 = ac & 1
        gray_500 = (d2 << 7) | (d1 << 6) | (a4 << 5) | (a2 << 4) | (a1 << 3) | (b4 << 2) | (b2 << 1) | b1
        gray_100 = (c4 << 2) | (c2 << 1) | c1
        n_500 = _gray_to_int(gray_500)
        n_100 = _gray_to_int(gray_100)
        gillham_map = {0: None, 1: 1, 2: 2, 3: 0, 4: 4, 5: None, 6: 3, 7: None}
        sub100 = gillham_map.get(n_100)
        if sub100 is None:
            return None
        if n_500 % 2 == 1:
            sub100 = 4 - sub100
        return n_500 * 500 + (sub100 - 2) * 100
    return None

_NL_BOUNDARIES = (
    10.47047130, 14.82817437, 18.18626357, 21.02939493, 23.54504424,
    25.82924707, 27.93898710, 29.91135686, 31.77256438, 33.54178436,
    35.23307598, 36.85817774, 38.42613147, 39.94418609, 41.41846811,
    42.85399720, 44.25456676, 45.62361714, 46.96427243, 48.27938130,
    49.57148879, 50.84282380, 52.09540059, 53.33098909, 54.55119701,
    55.75743407, 56.95093856, 58.13276437, 59.30386827, 60.46507008,
    61.61706851, 62.76049798, 63.89594048, 65.02394541, 66.14502570,
    67.25966159, 68.36830508, 69.47137943, 70.56928824, 71.66241086,
    72.75111166, 73.83573226, 74.91660007, 75.99402508, 77.06829762,
    78.13969745, 79.20848847, 80.27491972, 81.33922464, 82.40162541,
    83.46233019, 84.52153024, 85.57940251, 86.63610990, 87.69179979,
    88.74660785, 89.80066226,
)

def cpr_nl(lat: float) -> int:
    lat_abs = abs(lat)
    if lat_abs >= 87.0:
        return 1
    return 59 - bisect_right(_NL_BOUNDARIES, lat_abs)

_CPR_DENOM = 131072.0

def decode_cpr_airborne_local(
    cpr_format: int, cpr_lat: int, cpr_lon: int, ref_lat: float, ref_lon: float
) -> Optional[Tuple[float, float]]:
    d_lat = 360.0 / (59 if cpr_format == 1 else 60)
    j = floor(ref_lat / d_lat) + floor(0.5 + ((ref_lat % d_lat) / d_lat) - (cpr_lat / _CPR_DENOM))
    lat = d_lat * (j + (cpr_lat / _CPR_DENOM))
    if not (-90.0 <= lat <= 90.0):
        return None

    nl = cpr_nl(lat) - cpr_format
    if nl > 0:
        d_lon = 360.0 / nl
    elif nl == 0:
        d_lon = 360.0
    else:
        return None

    m = floor(ref_lon / d_lon) + floor(0.5 + ((ref_lon % d_lon) / d_lon) - (cpr_lon / _CPR_DENOM))
    lon = d_lon * (m + (cpr_lon / _CPR_DENOM))
    lon = (lon + 180.0) % 360.0 - 180.0
    return round(lat, 5), round(lon, 5)

def decode_cpr_airborne_pair(
    cpr_lat_even: int, cpr_lon_even: int, cpr_lat_odd: int, cpr_lon_odd: int, even_is_newer: bool
) -> Optional[Tuple[float, float]]:
    d_lat_even = 360.0 / 60.0
    d_lat_odd = 360.0 / 59.0
    j = floor((59.0 * cpr_lat_even - 60.0 * cpr_lat_odd) / _CPR_DENOM + 0.5)
    lat_even = d_lat_even * ((j % 60) + cpr_lat_even / _CPR_DENOM)
    lat_odd = d_lat_odd * ((j % 59) + cpr_lat_odd / _CPR_DENOM)
    if lat_even >= 270.0:
        lat_even -= 360.0
    if lat_odd >= 270.0:
        lat_odd -= 360.0
    if not (-90.0 <= lat_even <= 90.0 and -90.0 <= lat_odd <= 90.0):
        return None

    target_lat = lat_even if even_is_newer else lat_odd
    nl_even = cpr_nl(lat_even)
    nl_odd = cpr_nl(lat_odd)
    if nl_even != nl_odd:
        return None
    nl = nl_even

    if nl > 0:
        if even_is_newer:
            d_lon = 360.0 / nl
            m = floor((cpr_lon_even * (nl - 1) - cpr_lon_odd * nl) / _CPR_DENOM + 0.5)
            lon = d_lon * ((m % nl) + cpr_lon_even / _CPR_DENOM)
        else:
            d_lon = 360.0 / max(nl - 1, 1)
            m = floor((cpr_lon_even * (nl - 1) - cpr_lon_odd * nl) / _CPR_DENOM + 0.5)
            lon = d_lon * ((m % (nl - 1)) + cpr_lon_odd / _CPR_DENOM)
    else:
        return None

    lon = (lon + 180.0) % 360.0 - 180.0
    return round(target_lat, 5), round(lon, 5)

def calculate_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return round(r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a)), 2)

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, str]:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    deg = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    cardinals = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return round(deg, 1), cardinals[int((deg + 22.5) / 45.0) % 8]

def decode_message(hex_str: str) -> dict:
    hex_str = hex_str.strip().upper()
    if len(hex_str) != 28:
        return {}
    n = int(hex_str, 16)
    df = (n >> 107) & 0x1F
    if df not in (17, 18):
        return {'df': df}

    crc_valid = (crc_remainder(n, 112) == 0)
    icao = f"{(n >> 80) & 0xFFFFFF:06X}"
    payload = (n >> 24) & 0xFFFFFFFFFFFFFF
    tc = (payload >> 51) & 0x1F

    result = {
        'df': df,
        'icao': icao,
        'crc_valid': crc_valid,
        'typecode': tc
    }

    # TC 1 - 4: Callsign
    if 1 <= tc <= 4:
        cs_bits = payload & 0xFFFFFFFFFFFF
        result['callsign'] = decode_callsign(cs_bits)

    # TC 9 - 18: Position & Altitude
    elif 9 <= tc <= 18:
        ac = (payload >> 36) & 0xFFF
        cpr_fmt = (payload >> 34) & 0x1
        cpr_lat = (payload >> 17) & 0x1FFFF
        cpr_lon = payload & 0x1FFFF
        altcode = ((ac >> 6) << 7) | (ac & 0x3F)
        result['altitude'] = altcode_to_altitude(altcode)
        result['cpr_format'] = cpr_fmt
        result['cpr_lat'] = cpr_lat
        result['cpr_lon'] = cpr_lon

    # TC 19: Velocity
    elif tc == 19:
        subtype = (payload >> 48) & 0x7
        if subtype in (1, 2):
            v_ew_sign = (payload >> 42) & 0x1
            v_ew_mag = (payload >> 32) & 0x3FF
            v_ns_sign = (payload >> 31) & 0x1
            v_ns_mag = (payload >> 21) & 0x3FF
            if v_ew_mag > 0 and v_ns_mag > 0:
                mult = 4 if subtype == 2 else 1
                v_ew = (v_ew_mag - 1) * mult
                v_ns = (v_ns_mag - 1) * mult
                v_we = -v_ew if v_ew_sign == 1 else v_ew
                v_sn = -v_ns if v_ns_sign == 1 else v_ns
                result['groundspeed'] = int(math.sqrt(v_we * v_we + v_sn * v_sn))
                trk = math.degrees(math.atan2(v_we, v_sn))
                result['track'] = (trk + 360.0) % 360.0
        elif subtype in (3, 4):
            hdg_status = (payload >> 42) & 0x1
            hdg_raw = (payload >> 32) & 0x3FF
            as_mag = (payload >> 21) & 0x3FF
            if hdg_status == 1:
                result['track'] = hdg_raw / 1024.0 * 360.0
            if as_mag > 0:
                result['groundspeed'] = (as_mag - 1) * (4 if subtype == 4 else 1)

        vr_sign = (payload >> 19) & 0x1
        vr_mag = (payload >> 10) & 0x1FF
        if vr_mag > 0:
            result['vertical_rate'] = (vr_mag - 1) * 64 * (-1 if vr_sign == 1 else 1)

    return result

# ============================================================================
# SECTION 4: UDP Broadcaster & Client Networking
# ============================================================================

class UdpBroadcaster:
    """Manages outgoing UDP transmission to unicast or subnet broadcast."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5005, broadcast: bool = False):
        self.host = "255.255.255.255" if broadcast and host in ("127.0.0.1", "0.0.0.0") else host
        self.port = port
        self.broadcast = broadcast
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.broadcast:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.packets_sent = 0
        self.bytes_sent = 0
        self.lock = threading.Lock()

    def send_line(self, line: str):
        if not line:
            return
        data = (line.strip() + "\n").encode("utf-8", errors="replace")
        with self.lock:
            try:
                self.sock.sendto(data, (self.host, self.port))
                self.packets_sent += 1
                self.bytes_sent += len(data)
            except Exception as e:
                print(f"[ERROR] UDP send failed: {e}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class UdpAdsbClient:
    """Standalone UDP ADS-B Decoder Client for streaming & headless decoding."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5005,
        observer_lat: Optional[float] = None,
        observer_lon: Optional[float] = None,
        timeout: Optional[float] = 0.5
    ):
        self.host = host
        self.port = port
        self.observer_lat = observer_lat
        self.observer_lon = observer_lon
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        if self.timeout:
            self.sock.settimeout(self.timeout)
        self.db = AircraftMetadataDB()
        self.aircraft_history: Dict[str, Dict[str, Any]] = {}

    def listen(self) -> Generator[Dict[str, Any], None, None]:
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                text = data.decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    hex_msg, rssi = parse_raw_line(line)
                    if hex_msg:
                        dec = decode_message(hex_msg)
                        if dec and dec.get('crc_valid'):
                            icao = dec.get('icao')
                            now = time.time()
                            if icao not in self.aircraft_history:
                                meta = self.db.lookup(icao)
                                self.aircraft_history[icao] = {
                                    'icao': icao, 'callsign': None, 'altitude': None,
                                    'speed': None, 'heading': None, 'latitude': None,
                                    'longitude': None, 'distance_km': None, 'last_seen': now,
                                    'reg': meta.get('reg', ''),
                                    'type': meta.get('type', ''),
                                    'model': meta.get('model', ''),
                                    'operator': meta.get('operator', ''),
                                    'country': meta.get('country', '')
                                }
                            entry = self.aircraft_history[icao]
                            entry['last_seen'] = now
                            if 'callsign' in dec:
                                entry['callsign'] = dec['callsign'].strip('_ ')
                            if 'altitude' in dec and dec['altitude'] is not None:
                                entry['altitude'] = dec['altitude']
                            if 'groundspeed' in dec and dec['groundspeed'] is not None:
                                entry['speed'] = dec['groundspeed']
                            if 'track' in dec and dec['track'] is not None:
                                entry['heading'] = dec['track']

                            # Position
                            if 'cpr_lat' in dec and 'cpr_lon' in dec:
                                cpr_fmt = dec['cpr_format']
                                cpr_lat = dec['cpr_lat']
                                cpr_lon = dec['cpr_lon']
                                if self.observer_lat and self.observer_lon:
                                    pos = decode_cpr_airborne_local(
                                        cpr_fmt, cpr_lat, cpr_lon,
                                        entry['latitude'] or self.observer_lat,
                                        entry['longitude'] or self.observer_lon
                                    )
                                    if pos:
                                        entry['latitude'], entry['longitude'] = pos
                                        entry['distance_km'] = calculate_distance_km(
                                            self.observer_lat, self.observer_lon, pos[0], pos[1]
                                        )

                            if rssi is not None:
                                entry['rssi'] = rssi
                            yield entry
            except socket.timeout:
                continue
            except (KeyboardInterrupt, SystemExit):
                break

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

# ============================================================================
# SECTION 5: Aircraft Telemetry Tracker Store
# ============================================================================

class RadarTracker:
    def __init__(self, config: dict):
        self.config = config
        obs = config.get("observer", {})
        if obs.get("enabled", True) and obs.get("latitude") is not None and obs.get("longitude") is not None:
            self.observer_lat = float(obs["latitude"])
            self.observer_lon = float(obs["longitude"])
            self.has_observer = True
        else:
            self.observer_lat = None
            self.observer_lon = None
            self.has_observer = False

        self.prune_timeout = config.get("radar", {}).get("prune_timeout_seconds", 30.0)
        self.stale_timeout = config.get("radar", {}).get("stale_timeout_seconds", 10.0)
        self.cpr_max_interval = config.get("radar", {}).get("cpr_pair_max_interval_seconds", 10.0)

        self.db = AircraftMetadataDB()
        self.aircraft = {}
        self.lock = threading.RLock()
        self.total_msgs = 0
        self.total_raw_packets = 0
        self.total_bytes = 0
        self.msg_rate_window = deque()
        self.recent_events = deque(maxlen=4)
        self.is_connected = False
        self.status_message = "Initializing..."

    def log_event(self, text: str):
        with self.lock:
            ts = datetime.now().strftime("%H:%M:%S")
            self.recent_events.append(f"[{ts}] {text}")

    def update_message_rate(self, now: float):
        self.msg_rate_window.append(now)
        while self.msg_rate_window and now - self.msg_rate_window[0] > 1.0:
            self.msg_rate_window.popleft()

    def get_message_rate(self) -> int:
        now = time.time()
        with self.lock:
            valid = [t for t in self.msg_rate_window if now - t <= 1.0]
            return len(valid)

    def process_raw_line(self, line: str):
        msg_hex, rssi = parse_raw_line(line)
        if not msg_hex:
            return

        now = time.time()
        ts = datetime.now().strftime("%H:%M:%S")

        with self.lock:
            self.total_msgs += 1
            self.update_message_rate(now)

        dec = decode_message(msg_hex)
        if not dec or not dec.get('crc_valid'):
            return

        icao = dec.get('icao')
        if not icao:
            return

        tc = dec.get('typecode')

        with self.lock:
            if icao not in self.aircraft:
                meta = self.db.lookup(icao)
                reg = meta.get('reg') or ''
                atype = meta.get('type') or ''
                op = meta.get('operator') or ''
                country = meta.get('country') or ''

                self.aircraft[icao] = {
                    'icao': icao,
                    'callsign': '------',
                    'reg': reg,
                    'type': atype,
                    'model': meta.get('model', ''),
                    'operator': op,
                    'country': country,
                    'country_prefix': meta.get('country_prefix', ''),
                    'altitude': None,
                    'speed': None,
                    'track': None,
                    'vertical_rate': None,
                    'lat': None,
                    'lon': None,
                    'dist_km': None,
                    'dist_nm': None,
                    'bearing_deg': None,
                    'bearing_cardinal': None,
                    'cpr_even': None,
                    'cpr_odd': None,
                    'last_seen_epoch': now,
                    'last_seen_ts': ts,
                    'last_rssi': rssi,
                    'msg_count': 0
                }

                desc_parts = []
                if reg:
                    desc_parts.append(reg)
                if atype:
                    desc_parts.append(f"({atype})")
                if op:
                    desc_parts.append(f"- {op}")
                elif country and country != "Unknown":
                    desc_parts.append(f"[{country}]")

                desc_str = f" {' '.join(desc_parts)}" if desc_parts else ""
                rssi_str = f" (RSSI: {rssi} dBm)" if rssi is not None else ""
                self.log_event(f"NEW AIRCRAFT: ICAO {icao}{desc_str}{rssi_str}")

            ac = self.aircraft[icao]
            ac['msg_count'] += 1
            ac['last_seen_epoch'] = now
            ac['last_seen_ts'] = ts
            if rssi is not None:
                ac['last_rssi'] = rssi

            # 1. Callsign
            if 'callsign' in dec and dec['callsign']:
                cs = dec['callsign'].strip('_ ')
                if cs and ac['callsign'] != cs:
                    ac['callsign'] = cs
                    meta_tag = f" ({ac['reg']} {ac['type']})" if ac.get('reg') else ""
                    self.log_event(f"ICAO {icao} Callsign: {cs}{meta_tag}")

            # 2. Position & Altitude
            if tc and 9 <= tc <= 18:
                alt = dec.get('altitude')
                if alt is not None:
                    ac['altitude'] = alt

                cpr_fmt = dec.get('cpr_format')
                cpr_lat = dec.get('cpr_lat')
                cpr_lon = dec.get('cpr_lon')

                pos = None
                if self.has_observer and cpr_fmt in (0, 1) and cpr_lat is not None and cpr_lon is not None:
                    try:
                        ref_lat = ac['lat'] if ac['lat'] is not None else self.observer_lat
                        ref_lon = ac['lon'] if ac['lon'] is not None else self.observer_lon
                        pos = decode_cpr_airborne_local(cpr_fmt, cpr_lat, cpr_lon, ref_lat, ref_lon)
                    except Exception:
                        pos = None

                if cpr_fmt == 0:
                    ac['cpr_even'] = (cpr_lat, cpr_lon, now)
                elif cpr_fmt == 1:
                    ac['cpr_odd'] = (cpr_lat, cpr_lon, now)

                if not pos and ac['cpr_even'] and ac['cpr_odd']:
                    lat_e, lon_e, t_e = ac['cpr_even']
                    lat_o, lon_o, t_o = ac['cpr_odd']
                    if abs(t_e - t_o) <= self.cpr_max_interval:
                        try:
                            pos = decode_cpr_airborne_pair(
                                lat_e, lon_e, lat_o, lon_o, even_is_newer=(t_e >= t_o)
                            )
                        except Exception:
                            pos = None

                if pos:
                    lat, lon = pos
                    first_fix = (ac['lat'] is None)
                    ac['lat'] = lat
                    ac['lon'] = lon

                    if self.has_observer:
                        dist_km = calculate_distance_km(self.observer_lat, self.observer_lon, lat, lon)
                        ac['dist_km'] = dist_km
                        ac['dist_nm'] = dist_km / 1.852
                        b_deg, b_card = calculate_bearing(self.observer_lat, self.observer_lon, lat, lon)
                        ac['bearing_deg'] = b_deg
                        ac['bearing_cardinal'] = b_card
                        if first_fix:
                            reg_info = f" ({ac['reg']})" if ac.get('reg') else ""
                            self.log_event(f"ICAO {icao}{reg_info} Position solved: {dist_km:.1f} km away ({b_card})")
                    else:
                        if first_fix:
                            reg_info = f" ({ac['reg']})" if ac.get('reg') else ""
                            self.log_event(f"ICAO {icao}{reg_info} Global Position: {lat:.4f}, {lon:.4f}")

            # 3. Velocity
            elif tc == 19:
                spd = dec.get('groundspeed')
                trk = dec.get('track')
                vr = dec.get('vertical_rate')
                if spd is not None:
                    ac['speed'] = spd
                if trk is not None:
                    ac['track'] = round(trk, 1)
                if vr is not None:
                    ac['vertical_rate'] = vr

    def prune_stale_aircraft(self) -> int:
        now = time.time()
        with self.lock:
            to_remove = [
                icao for icao, ac in self.aircraft.items()
                if now - ac['last_seen_epoch'] > self.prune_timeout
            ]
            for icao in to_remove:
                del self.aircraft[icao]
                self.log_event(f"Aircraft {icao} pruned (inactive >{int(self.prune_timeout)}s)")
        return len(to_remove)

# ============================================================================
# SECTION 6: Terminal Dashboard Renderers (Rich & Pure-ANSI)
# ============================================================================

def render_rich_view(tracker: RadarTracker, port_info: str):
    tracker.prune_stale_aircraft()
    now = time.time()
    spinner = SPINNER_FRAMES[int(now * 4) % len(SPINNER_FRAMES)]

    with tracker.lock:
        aircraft_list = sorted(
            tracker.aircraft.values(),
            key=lambda a: (a['dist_km'] if a['dist_km'] is not None else 9999, -a['last_seen_epoch'])
        )
        active_count = len(aircraft_list)
        total_msgs = tracker.total_msgs
        raw_packets = tracker.total_raw_packets
        msg_rate = tracker.get_message_rate()
        events = list(tracker.recent_events)
        conn_status = tracker.status_message
        is_conn = tracker.is_connected
        has_obs = tracker.has_observer
        db_count = len(tracker.db.db)
        db_src = tracker.db.loaded_source

    obs_str = f"Observer: ({tracker.observer_lat:.4f}, {tracker.observer_lon:.4f})" if has_obs else "Observer: Not Set (Global CPR Mode)"
    status_style = "bold green" if is_conn else "bold yellow"
    db_tag = f" | DB: {db_count:,} ({db_src})" if db_src else ""

    header_text = Text()
    header_text.append(" ADS-B TERMINAL RADAR ", style="bold white on blue")
    header_text.append(f"  [{conn_status}] ", style=status_style)
    header_text.append(f"Source: {port_info} | {obs_str}\n", style="dim")
    header_text.append(f"{spinner} Active: ", style="bold")
    header_text.append(f"{active_count} ", style="bold green" if active_count > 0 else "bold yellow")
    header_text.append(f"| Rate: ", style="bold")
    header_text.append(f"{msg_rate} msg/s ", style="bold cyan")
    header_text.append(f"| Decoded: ", style="bold")
    header_text.append(f"{total_msgs} ", style="bold yellow")
    header_text.append(f"| Packets: ", style="bold")
    header_text.append(f"{raw_packets} ", style="dim")
    header_text.append(f"{db_tag} | Prune: {int(tracker.prune_timeout)}s", style="dim")

    header_panel = Panel(header_text, border_style="blue", padding=(0, 1))

    table = Table(
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="bright_blue",
        show_edge=True,
        expand=False
    )

    table.add_column("STAT", justify="center")
    table.add_column("ICAO", justify="center", style="bold cyan")
    table.add_column("CALLSIGN", justify="center", style="bold yellow")
    table.add_column("REG / TYPE", justify="center", style="bold magenta")
    table.add_column("ALTITUDE (V/S)", justify="right")
    table.add_column("SPEED & HDG", justify="center")
    table.add_column("COORDINATES", justify="center")
    if has_obs:
        table.add_column("RANGE & BRG", justify="center")
    table.add_column("RSSI", justify="center")
    table.add_column("SEEN", justify="right")
    table.add_column("MSGS", justify="right")

    if not aircraft_list:
        idle_row = [
            "[dim]IDLE[/]", "------", "------", "[dim]---[/]", "[dim]---[/]", "[dim]---[/]",
            "[dim]Searching for aircraft in range...[/]", "---", "---", "0"
        ]
        if has_obs:
            idle_row.insert(7, "---")
        table.add_row(*idle_row)
    else:
        for ac in aircraft_list:
            age = now - ac['last_seen_epoch']
            status = "[bold green]LIVE[/]" if age < tracker.stale_timeout else f"[bold yellow]{int(age)}s[/]"
            icao = ac['icao']
            callsign = ac['callsign'] if ac['callsign'] != '------' else "[dim]------[/]"

            # Metadata Registration & Type
            reg_val = ac.get('reg')
            type_val = ac.get('type')
            if reg_val and type_val:
                reg_type_str = f"[bold magenta]{reg_val}[/] [dim]({type_val})[/]"
            elif reg_val:
                reg_type_str = f"[bold magenta]{reg_val}[/]"
            elif type_val:
                reg_type_str = f"[dim]({type_val})[/]"
            elif ac.get('country') and ac.get('country') != "Unknown":
                reg_type_str = f"[dim]{ac.get('country')}[/]"
            else:
                reg_type_str = "[dim]---[/]"

            alt_val = ac['altitude']
            vr_val = ac['vertical_rate']
            if alt_val is not None:
                if vr_val is not None and vr_val > 100:
                    alt_str = f"[bold white]{alt_val:,} ft[/] [green](+{vr_val})[/]"
                elif vr_val is not None and vr_val < -100:
                    alt_str = f"[bold white]{alt_val:,} ft[/] [red]({vr_val})[/]"
                else:
                    alt_str = f"[bold white]{alt_val:,} ft[/]"
            else:
                alt_str = "[dim]---[/]"

            spd_val = ac['speed']
            trk_val = ac['track']
            if spd_val is not None and trk_val is not None:
                spd_str = f"{spd_val} kts @ {trk_val:.0f}°"
            elif spd_val is not None:
                spd_str = f"{spd_val} kts"
            elif trk_val is not None:
                spd_str = f"{trk_val:.0f}°"
            else:
                spd_str = "[dim]---[/]"

            coord_str = f"[cyan]{ac['lat']:.3f}, {ac['lon']:.3f}[/]" if ac['lat'] and ac['lon'] else "[dim]Syncing...[/]"

            rssi = ac['last_rssi']
            if rssi is not None:
                rssi_str = f"[green]{rssi} dBm[/]" if rssi > -95 else f"[yellow]{rssi} dBm[/]" if rssi > -105 else f"[red]{rssi} dBm[/]"
            else:
                rssi_str = "[dim]---[/]"

            row_items = [status, icao, callsign, reg_type_str, alt_str, spd_str, coord_str]
            if has_obs:
                dist_str = f"[bold magenta]{ac['dist_km']:.1f} km[/] [yellow]{ac['bearing_cardinal']}[/]" if ac['dist_km'] else "[dim]---[/]"
                row_items.append(dist_str)

            row_items.extend([rssi_str, f"{age:.1f}s", str(ac['msg_count'])])
            table.add_row(*row_items)

    log_renderables = [Text(ev) for ev in events] if events else [Text("Listening on 1090 MHz. Place antenna near window for best reception.", style="dim")]
    log_panel = Panel(Group(*log_renderables), title="[bold] Radio Activity Feed [/]", border_style="dim", height=6)
    return Group(header_panel, table, log_panel)

def render_ansi_fallback(tracker: RadarTracker, port_info: str):
    tracker.prune_stale_aircraft()
    now = time.time()

    with tracker.lock:
        aircraft_list = sorted(
            tracker.aircraft.values(),
            key=lambda a: (a['dist_km'] if a['dist_km'] is not None else 9999, -a['last_seen_epoch'])
        )
        active_count = len(aircraft_list)
        total_msgs = tracker.total_msgs
        raw_packets = tracker.total_raw_packets
        msg_rate = tracker.get_message_rate()
        events = list(tracker.recent_events)
        conn_status = tracker.status_message
        has_obs = tracker.has_observer
        db_count = len(tracker.db.db)
        db_src = tracker.db.loaded_source

    obs_str = f"Observer: ({tracker.observer_lat:.4f}, {tracker.observer_lon:.4f})" if has_obs else "Observer: Not Configured"
    db_tag = f" | DB: {db_count:,} ({db_src})" if db_src else ""

    out = ["\033[H", "=" * 110]
    out.append(f" ADS-B RADAR  [{conn_status}]  Source: {port_info} | {obs_str}")
    out.append(f" Active: {active_count} | Rate: {msg_rate} msg/s | Decoded: {total_msgs} | Packets: {raw_packets}{db_tag} | Prune: {int(tracker.prune_timeout)}s")
    out.append("=" * 110)

    if has_obs:
        out.append(f" {'STAT':<5} | {'ICAO':<6} | {'CALLSIGN':<8} | {'REG / TYPE':<14} | {'ALTITUDE (V/S)':<17} | {'SPEED & HDG':<15} | {'COORDINATES':<15} | {'RANGE & BRG':<11} | {'RSSI':<7} | {'SEEN':<5} | {'MSGS':<4}")
    else:
        out.append(f" {'STAT':<5} | {'ICAO':<6} | {'CALLSIGN':<8} | {'REG / TYPE':<14} | {'ALTITUDE (V/S)':<17} | {'SPEED & HDG':<15} | {'COORDINATES':<22} | {'RSSI':<7} | {'SEEN':<5} | {'MSGS':<4}")
    out.append("-" * 110)

    if not aircraft_list:
        out.append(" Searching for aircraft in range...")
    else:
        for ac in aircraft_list:
            age = now - ac['last_seen_epoch']
            stat = "LIVE" if age < tracker.stale_timeout else f"{int(age)}s"
            icao = ac['icao']
            call = ac['callsign']

            reg_val = ac.get('reg')
            type_val = ac.get('type')
            if reg_val and type_val:
                reg_type = f"{reg_val} ({type_val})"
            elif reg_val:
                reg_type = reg_val
            elif type_val:
                reg_type = f"({type_val})"
            elif ac.get('country') and ac.get('country') != "Unknown":
                reg_type = ac.get('country')[:14]
            else:
                reg_type = "---"

            alt = f"{ac['altitude']:,}ft" if ac['altitude'] else "---"
            if ac['vertical_rate']:
                alt += f" ({ac['vertical_rate']:+d})"
            spd = f"{ac['speed']}kts @ {ac['track']:.0f}°" if ac['speed'] and ac['track'] else "---"
            coords = f"{ac['lat']:.3f}, {ac['lon']:.3f}" if ac['lat'] else "Syncing..."
            rssi = f"{ac['last_rssi']}dBm" if ac['last_rssi'] else "---"

            if has_obs:
                dist = f"{ac['dist_km']:.1f}km {ac['bearing_cardinal']}" if ac['dist_km'] else "---"
                out.append(f" {stat:<5} | {icao:<6} | {call:<8} | {reg_type:<14} | {alt:<17} | {spd:<15} | {coords:<15} | {dist:<11} | {rssi:<7} | {age:.1f}s | {ac['msg_count']:<4}")
            else:
                out.append(f" {stat:<5} | {icao:<6} | {call:<8} | {reg_type:<14} | {alt:<17} | {spd:<15} | {coords:<22} | {rssi:<7} | {age:.1f}s | {ac['msg_count']:<4}")

    out.append("=" * 110)
    out.append("Radio Activity Feed:")
    if events:
        for ev in events:
            out.append(f"  {ev}")
    else:
        out.append("  Listening for 1090 MHz squitters...")
    out.append("=" * 110)

    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()

# ============================================================================
# SECTION 7: Background Worker Threads
# ============================================================================

def udp_reader_worker(
    host: str,
    port: int,
    tracker: RadarTracker,
    stop_event: threading.Event,
    save_file: Optional[str] = None
):
    tracker.status_message = f"Binding UDP {host}:{port}..."
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.settimeout(0.5)
        tracker.is_connected = True
        tracker.status_message = f"ONLINE (UDP :{port})"
        tracker.log_event(f"UDP listener bound @ {host}:{port}")
    except Exception as e:
        tracker.status_message = f"UDP ERROR: {e}"
        tracker.log_event(f"Failed to bind UDP {host}:{port}: {e}")
        stop_event.set()
        return

    f_save = open(save_file, 'a', encoding='utf-8') if save_file else None

    try:
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    tracker.log_event(f"UDP receive error: {e}")
                break

            if data:
                tracker.total_bytes += len(data)
                text = data.decode('utf-8', errors='ignore')
                for raw_line in text.splitlines():
                    raw_line = raw_line.strip()
                    if raw_line:
                        tracker.total_raw_packets += 1
                        if f_save:
                            ts_full = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                            f_save.write(f"[{ts_full}] {raw_line}\n")
                            f_save.flush()
                        tracker.process_raw_line(raw_line)
    finally:
        sock.close()
        if f_save:
            f_save.close()
        tracker.is_connected = False
        tracker.status_message = "UDP STOPPED"

def serial_reader_worker(
    port: str,
    baud: int,
    timeout: float,
    tracker: RadarTracker,
    stop_event: threading.Event,
    save_file: Optional[str],
    broadcaster: Optional[UdpBroadcaster] = None
):
    tracker.status_message = f"Connecting to {port}..."
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout
        )
        ser.dtr = True
        ser.rts = True
        time.sleep(0.1)
        ser.reset_input_buffer()
        tracker.is_connected = True
        tracker.status_message = "ONLINE (Listening 1090MHz)"
        tracker.log_event(f"Serial port {port} opened @ {baud} baud")
    except Exception as e:
        tracker.status_message = f"ERROR: {e}"
        tracker.log_event(f"Failed to open port {port}: {e}")
        stop_event.set()
        return

    f_save = open(save_file, 'a', encoding='utf-8') if save_file else None
    line_buf = bytearray()

    try:
        while not stop_event.is_set():
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                tracker.total_bytes += len(chunk)
                for b in chunk:
                    if b in (ord(b'\n'), ord(b'\r')):
                        if line_buf:
                            tracker.total_raw_packets += 1
                            raw_line = line_buf.decode('ascii', errors='ignore').strip()
                            line_buf.clear()

                            if raw_line:
                                if f_save:
                                    ts_full = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                                    f_save.write(f"[{ts_full}] {raw_line}\n")
                                    f_save.flush()

                                if broadcaster:
                                    broadcaster.send_line(raw_line)

                                tracker.process_raw_line(raw_line)
                    else:
                        line_buf.append(b)
    finally:
        ser.close()
        if f_save:
            f_save.close()
        tracker.is_connected = False
        tracker.status_message = "DISCONNECTED"

def file_reader_worker(
    file_path: str,
    tracker: RadarTracker,
    stop_event: threading.Event,
    speed: float = 1.0,
    broadcaster: Optional[UdpBroadcaster] = None
):
    if not os.path.exists(file_path):
        tracker.status_message = f"File Not Found: {file_path}"
        tracker.log_event(f"File not found: {file_path}")
        stop_event.set()
        return

    tracker.is_connected = True
    tracker.status_message = f"REPLAY ({speed}x)"
    tracker.log_event(f"Replaying log: {file_path}")

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    delay = 0.4 / speed if speed > 0 else 0
    while not stop_event.is_set():
        for line in lines:
            if stop_event.is_set():
                break

            line_clean = line.strip()
            if line_clean:
                tracker.total_raw_packets += 1
                if broadcaster:
                    broadcaster.send_line(line_clean)
                tracker.process_raw_line(line_clean)

            if delay > 0:
                time.sleep(delay)

        time.sleep(1.0)

# ============================================================================
# SECTION 8: Main Entrypoint & Mode Dispatcher
# ============================================================================

def main():
    config = load_config()
    serial_cfg = config.get('serial', {})
    udp_cfg = config.get('udp', {})

    parser = argparse.ArgumentParser(description="Micro ADS-B Terminal Radar & Telemetry Suite (1090 MHz)")
    parser.add_argument('--port', help=f"Serial port (default: {serial_cfg.get('port', 'COM12')})")
    parser.add_argument('--baud', type=int, help=f"Baud rate (default: {serial_cfg.get('baudrate', 115200)})")
    parser.add_argument('--file', help="Replay messages from a recorded file")
    parser.add_argument('--demo', action='store_true', help="Run demo mode using bundled sample flight")
    parser.add_argument('--record', '--save', dest='save', help="Record incoming raw traffic to file")
    parser.add_argument('--lat', type=float, help="Override observer latitude")
    parser.add_argument('--lon', type=float, help="Override observer longitude")
    parser.add_argument('--no-observer', action='store_true', help="Disable observer coordinates (Global CPR mode)")
    parser.add_argument('--speed', type=float, default=2.0, help="Replay speed multiplier (default: 2.0)")

    # UDP Options
    parser.add_argument('--udp', nargs='?', const=udp_cfg.get('listen_port', 5005), type=int, help="Receive ADS-B over UDP port (default: 5005)")
    parser.add_argument('--udp-host', default=udp_cfg.get('listen_host', '0.0.0.0'), help="UDP bind host (default: 0.0.0.0)")
    parser.add_argument('--server-udp', nargs='?', const=udp_cfg.get('target_port', 5005), type=int, help="Run as standalone UDP server / broadcaster on port")
    parser.add_argument('--forward-udp', help="Forward received packets to UDP target host:port (e.g. 127.0.0.1:5005)")
    parser.add_argument('--broadcast', action='store_true', default=udp_cfg.get('broadcast', False), help="Enable UDP subnet broadcast")

    # Headless / Output Options
    parser.add_argument('--headless', action='store_true', help="Run in headless CLI mode without terminal radar GUI")
    parser.add_argument('--json', action='store_true', help="Output decoded telemetry as JSON lines (NDJSON)")

    args = parser.parse_args()

    port = args.port or serial_cfg.get('port', 'COM12')
    baud = args.baud or serial_cfg.get('baudrate', 115200)
    timeout = serial_cfg.get('timeout', 0.2)

    if args.no_observer:
        config['observer']['enabled'] = False
    elif args.lat is not None and args.lon is not None:
        config['observer']['enabled'] = True
        config['observer']['latitude'] = args.lat
        config['observer']['longitude'] = args.lon

    save_file = args.save or (config['logging']['raw_traffic_file'] if config['logging'].get('save_raw_traffic') else None)

    # ------------------------------------------------------------------------
    # Sub-Mode A: Headless UDP Client Streamer
    # ------------------------------------------------------------------------
    if args.headless and args.udp:
        obs_lat = config['observer']['latitude'] if config['observer']['enabled'] else None
        obs_lon = config['observer']['longitude'] if config['observer']['enabled'] else None
        print(f"[*] Starting Headless UDP Client on {args.udp_host}:{args.udp}")
        client = UdpAdsbClient(host=args.udp_host, port=args.udp, observer_lat=obs_lat, observer_lon=obs_lon)
        try:
            for ac in client.listen():
                if args.json:
                    print(json.dumps(ac, default=str))
                else:
                    ts = datetime.now().strftime("%H:%M:%S")
                    reg_info = f" | {ac.get('reg')} ({ac.get('type')})" if ac.get('reg') else ""
                    print(f"[{ts}] ICAO: {ac['icao']}{reg_info} | CALL: {ac.get('callsign') or '----':<8} | ALT: {ac.get('altitude') or '----'} | POS: ({ac.get('latitude')}, {ac.get('longitude')})")
        except KeyboardInterrupt:
            print("\n[*] Stopped UDP client.")
        finally:
            client.close()
        return

    # ------------------------------------------------------------------------
    # Sub-Mode B: Standalone UDP Server / Broadcaster
    # ------------------------------------------------------------------------
    if args.server_udp:
        target_port = args.server_udp
        target_host = "255.255.255.255" if args.broadcast else udp_cfg.get('target_host', '127.0.0.1')
        broadcaster = UdpBroadcaster(host=target_host, port=target_port, broadcast=args.broadcast)
        stop_event = threading.Event()
        target_file = os.path.join(os.path.dirname(__file__), 'sample_flight.txt') if args.demo else args.file
        dummy_tracker = RadarTracker(config)

        print(f"[*] ADS-B UDP Server broadcasting to {target_host}:{target_port} (Broadcast: {args.broadcast})")
        try:
            if target_file:
                file_reader_worker(target_file, dummy_tracker, stop_event, speed=args.speed, broadcaster=broadcaster)
            else:
                serial_reader_worker(port, baud, timeout, dummy_tracker, stop_event, save_file, broadcaster=broadcaster)
        except KeyboardInterrupt:
            print("\n[*] Stopping UDP server...")
            stop_event.set()
        finally:
            broadcaster.close()
        return

    # ------------------------------------------------------------------------
    # Sub-Mode C: Visual Terminal Radar (Serial, UDP, or Replay)
    # ------------------------------------------------------------------------
    tracker = RadarTracker(config)
    stop_event = threading.Event()

    # Optional Broadcaster for UDP Forwarding
    broadcaster = None
    if args.forward_udp:
        parts = args.forward_udp.split(':')
        fwd_host = parts[0]
        fwd_port = int(parts[1]) if len(parts) > 1 else 5005
        broadcaster = UdpBroadcaster(host=fwd_host, port=fwd_port, broadcast=args.broadcast)
        tracker.log_event(f"UDP forward active to {fwd_host}:{fwd_port}")

    target_file = None
    if args.demo:
        demo_candidate = os.path.join(os.path.dirname(__file__), 'sample_flight.txt')
        target_file = demo_candidate
    elif args.file:
        target_file = args.file

    if args.udp:
        source_info = f"UDP :{args.udp}"
        reader = threading.Thread(
            target=udp_reader_worker,
            args=(args.udp_host, args.udp, tracker, stop_event, save_file),
            daemon=True
        )
    elif target_file:
        source_info = f"Demo ({os.path.basename(target_file)})"
        reader = threading.Thread(
            target=file_reader_worker,
            args=(target_file, tracker, stop_event, args.speed, broadcaster),
            daemon=True
        )
    else:
        source_info = f"{port} @ {baud}"
        reader = threading.Thread(
            target=serial_reader_worker,
            args=(port, baud, timeout, tracker, stop_event, save_file, broadcaster),
            daemon=True
        )

    reader.start()

    if HAVE_RICH:
        console = Console(legacy_windows=False)
        try:
            with Live(render_rich_view(tracker, source_info), console=console, refresh_per_second=4, screen=False) as live:
                while not stop_event.is_set():
                    live.update(render_rich_view(tracker, source_info))
                    time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            if broadcaster:
                broadcaster.close()
            reader.join(timeout=1.0)
            console.print("\n[bold yellow]Radar tracker stopped.[/]")
    else:
        os.system('cls' if os.name == 'nt' else 'clear')
        try:
            while not stop_event.is_set():
                render_ansi_fallback(tracker, source_info)
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            if broadcaster:
                broadcaster.close()
            reader.join(timeout=1.0)
            print("\nRadar tracker stopped.")

if __name__ == '__main__':
    main()
