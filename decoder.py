"""
High-Performance Mode-S / ADS-B Decoder Module
Pure Python implementation compliant with ICAO Annex 10 & RTCA DO-260B.
Zero external library dependencies (standard library only).
"""

import math
from bisect import bisect_right
from math import floor

# ============================================================================
# 1. CRC-24 Error Detection (Mode-S Generator Polynomial)
# ============================================================================

_CRC_POLY = 0xFFF409

def _build_crc_table() -> tuple[int, ...]:
    """Precompute 256-entry table for byte-at-a-time CRC-24 calculation."""
    table = [0] * 256
    for i in range(256):
        c = i << 16
        for _ in range(8):
            c = ((c << 1) ^ _CRC_POLY) if (c & 0x800000) else (c << 1)
        table[i] = c & 0xFFFFFF
    return tuple(table)

_CRC_TABLE: tuple[int, ...] = _build_crc_table()

def crc_remainder(n: int, length: int) -> int:
    """
    Computes Mode-S CRC-24 remainder.
    For Downlink Format DF17/18, valid messages yield remainder == 0.
    """
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
    """Validates 24-bit Mode-S parity for a given hex string."""
    try:
        return crc_remainder(int(msg_hex, 16), len(msg_hex) * 4) == 0
    except ValueError:
        return False

# ============================================================================
# 2. Callsign Decoding (BDS 0,8 / Typecodes 1 - 4)
# ============================================================================

def _build_callsign_table() -> str:
    """Builds ICAO 6-bit alphabet lookup table."""
    table = []
    for i in range(64):
        if 1 <= i <= 26:
            table.append(chr(i | 0x40))  # A - Z
        elif i == 32 or (48 <= i <= 57):
            table.append(chr(i))         # Space or 0 - 9
        else:
            table.append("#")            # Invalid character sentinel
    return "".join(table)

_CALLSIGN_TABLE: str = _build_callsign_table()

def decode_callsign(bits: int) -> str:
    """Decodes 8-character callsign from 48-bit integer."""
    chars = [_CALLSIGN_TABLE[(bits >> (42 - 6 * i)) & 0x3F] for i in range(8)]
    return "".join(chars).strip("# ").strip()

# ============================================================================
# 3. Altitude Decoding (25-foot Linear & 100-foot Gillham Code)
# ============================================================================

def _gray_to_int(gray: int) -> int:
    """Converts reflected Gray code to integer."""
    num = 0
    while gray > 0:
        num ^= gray
        gray >>= 1
    return num

def altcode_to_altitude(ac: int) -> int | None:
    """
    Decodes 13-bit Mode-S altitude field to feet.
    Supports both 25-foot resolution (Q-bit == 1) and 100-foot Gillham Gray code.
    """
    if ac == 0:
        return None

    m_bit = (ac >> 6) & 0x1
    q_bit = (ac >> 4) & 0x1

    if m_bit == 0 and q_bit == 1:
        # 25-ft linear interval
        n = ((ac >> 2) & 0x7E0) | ((ac >> 1) & 0x10) | (ac & 0xF)
        return n * 25 - 1000

    if m_bit == 0 and q_bit == 0:
        # 100-ft Gillham Gray code
        def bit_val(pos: int) -> int:
            return (ac >> (12 - pos)) & 0x1

        c1, a1 = bit_val(0), bit_val(1)
        c2, a2 = bit_val(2), bit_val(3)
        c4, a4 = bit_val(4), bit_val(5)
        b1 = bit_val(7)
        b2, d2 = bit_val(9), bit_val(10)
        b4, d4 = bit_val(11), bit_val(12)

        gc500 = (
            (d2 << 7) | (d4 << 6) | (a1 << 5) | (a2 << 4) |
            (a4 << 3) | (b1 << 2) | (b2 << 1) | b4
        )
        gc100 = (c1 << 2) | (c2 << 1) | c4

        n500 = _gray_to_int(gc500)
        n100 = _gray_to_int(gc100)

        if n100 in (0, 5, 6):
            return None
        if n100 == 7:
            n100 = 5
        if n500 % 2:
            n100 = 6 - n100

        return n500 * 500 + n100 * 100 - 1300

    return None

# ============================================================================
# 4. Compact Position Reporting (CPR) Engine
# ============================================================================

_NL_BOUNDARIES: tuple[float, ...] = (
    10.47047129996848, 14.828174368686794, 18.186263570713354, 21.029394926028463,
    23.545044865570706, 25.829247070587755, 27.938987101219045, 29.911356857318083,
    31.77209707681077, 33.53993436298484, 35.22899597796385, 36.85025107593526,
    38.41241892412256, 39.922566843338615, 41.38651832260239, 42.80914012243555,
    44.194549514192744, 45.546267226602346, 46.867332524987454, 48.160391280966216,
    49.42776439255687, 50.67150165553835, 51.893424691687684, 53.09516152796003,
    54.278174722729, 55.44378444495043, 56.59318756205918, 57.72747353866114,
    58.84763776148457, 59.954592766940294, 61.04917774246351, 62.13216659210329,
    63.20427479381928, 64.2661652256744, 65.31845309682089, 66.36171008382617,
    67.39646774084667, 68.4232202208333, 69.44242631144024, 70.454510749876,
    71.45986473028982, 72.45884544728945, 73.45177441667865, 74.43893415725137,
    75.42056256653356, 76.39684390794469, 77.36789461328188, 78.33374082922747,
    79.29428225456925, 80.24923213280512, 81.19801349271948, 82.13956980510606,
    83.07199444719814, 83.99173562980565, 84.89166190702085, 85.75541620944418,
    86.535369975121, 87.0
)

def cpr_nl(lat: float) -> int:
    """Returns the CPR longitude zone count NL for a given latitude."""
    abs_lat = abs(lat)
    if abs_lat > 87.0:
        return 1
    if abs_lat == 87.0:
        return 2
    idx = bisect_right(_NL_BOUNDARIES, abs_lat)
    return 59 - idx

_CPR_DENOM = 131072.0  # 2**17

def decode_cpr_airborne_local(
    cpr_format: int,
    cpr_lat_raw: int,
    cpr_lon_raw: int,
    ref_lat: float,
    ref_lon: float
) -> tuple[float, float]:
    """
    Decodes single airborne CPR frame using a known local observer reference.
    Yields instant position on the first received position packet.
    """
    cpr_lat = cpr_lat_raw / _CPR_DENOM
    cpr_lon = cpr_lon_raw / _CPR_DENOM
    d_lat = 360.0 / 59 if cpr_format else 360.0 / 60

    j = floor(0.5 + ref_lat / d_lat - cpr_lat)
    lat = d_lat * (j + cpr_lat)

    ni = cpr_nl(lat) - cpr_format
    d_lon = 360.0 / ni if ni > 0 else 360.0

    m = floor(0.5 + ref_lon / d_lon - cpr_lon)
    lon = d_lon * (m + cpr_lon)

    if lon > 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0

    return lat, lon

def decode_cpr_airborne_pair(
    lat_even_raw: int,
    lon_even_raw: int,
    lat_odd_raw: int,
    lon_odd_raw: int,
    even_is_newer: bool
) -> tuple[float, float] | None:
    """
    Global CPR decoding resolving latitude and longitude from an Even/Odd pair.
    Used when no observer coordinates are configured.
    """
    cprlat_even = lat_even_raw / _CPR_DENOM
    cprlon_even = lon_even_raw / _CPR_DENOM
    cprlat_odd = lat_odd_raw / _CPR_DENOM
    cprlon_odd = lon_odd_raw / _CPR_DENOM

    j = floor(59 * cprlat_even - 60 * cprlat_odd + 0.5)
    lat_even = (360.0 / 60) * (j % 60 + cprlat_even)
    lat_odd = (360.0 / 59) * (j % 59 + cprlat_odd)

    if lat_even >= 270.0:
        lat_even -= 360.0
    if lat_odd >= 270.0:
        lat_odd -= 360.0

    if cpr_nl(lat_even) != cpr_nl(lat_odd):
        return None

    if even_is_newer:
        lat = lat_even
        nl = cpr_nl(lat)
        ni = max(nl, 1)
        m = floor(cprlon_even * (nl - 1) - cprlon_odd * nl + 0.5)
        lon = (360.0 / ni) * (m % ni + cprlon_even)
    else:
        lat = lat_odd
        nl = cpr_nl(lat)
        ni = max(nl - 1, 1)
        m = floor(cprlon_even * (nl - 1) - cprlon_odd * nl + 0.5)
        lon = (360.0 / ni) * (m % ni + cprlon_odd)

    if lon > 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0

    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return None

    return lat, lon

# ============================================================================
# 5. Geodesic Calculations (Ground Distance & Compass Bearing)
# ============================================================================

def calculate_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle ground distance in kilometers (Haversine formula)."""
    r_earth = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_earth * c

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, str]:
    """Computes initial compass bearing from observer (lat1, lon1) to aircraft."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    cardinals = [
        'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
        'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
    ]
    cardinal = cardinals[int((deg + 11.25) / 22.5) % 16]
    return deg, cardinal

# ============================================================================
# 6. Master ADS-B Extended Squitter Message Decoder
# ============================================================================

def decode_message(hex_str: str) -> dict:
    """
    Parses a 28-character hex Mode-S ADS-B message (112 bits).
    Returns structured dictionary with decoded aviation telemetry.
    """
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

    # TC 1 - 4: Identification & Callsign
    if 1 <= tc <= 4:
        category = (payload >> 48) & 0x7
        cs_bits = payload & 0xFFFFFFFFFFFF
        result['callsign'] = decode_callsign(cs_bits)
        result['category'] = category

    # TC 9 - 18: Airborne Position & Altitude
    elif 9 <= tc <= 18:
        ss = (payload >> 49) & 0x3
        ac = (payload >> 36) & 0xFFF
        cpr_fmt = (payload >> 34) & 0x1
        cpr_lat = (payload >> 17) & 0x1FFFF
        cpr_lon = payload & 0x1FFFF

        altcode = ((ac >> 6) << 7) | (ac & 0x3F)
        result['altitude'] = altcode_to_altitude(altcode)
        result['surveillance_status'] = ss
        result['cpr_format'] = cpr_fmt
        result['cpr_lat'] = cpr_lat
        result['cpr_lon'] = cpr_lon

    # TC 19: Airborne Velocity
    elif tc == 19:
        subtype = (payload >> 48) & 0x7
        result['subtype'] = subtype

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
