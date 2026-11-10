#!/usr/bin/env python3
"""
UDP ADS-B Client & Live Decoder Module.
Receives raw Mode-S / ADS-B datagrams over UDP socket, validates CRC,
decodes telemetry via decoder.py, and tracks aircraft state.
Can be imported as a Python module or executed directly via CLI.
"""

import sys
import os
import re
import json
import socket
import argparse
import time
from datetime import datetime
from typing import Generator, Optional, Dict, Any

import decoder

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config() -> dict:
    default_config = {
        "observer": {
            "enabled": True,
            "latitude": 51.4700,
            "longitude": -0.4543
        },
        "udp": {
            "listen_host": "0.0.0.0",
            "listen_port": 5005
        }
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                for section in default_config:
                    if section in loaded and isinstance(loaded[section], dict):
                        default_config[section].update(loaded[section])
        except Exception:
            pass
    return default_config


class UdpAdsbClient:
    """
    Standalone UDP ADS-B Decoder Client.
    Binds to a UDP port, receives raw packets, and decodes telemetry in real-time.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5005,
        observer_lat: Optional[float] = None,
        observer_lon: Optional[float] = None,
        timeout: Optional[float] = None
    ):
        self.host = host
        self.port = port
        self.observer_lat = observer_lat
        self.observer_lon = observer_lon
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)

        # Track state for Global CPR resolution across odd/even frames
        self.aircraft_history: Dict[str, Dict[str, Any]] = {}
        self.total_received = 0
        self.total_decoded = 0

    def parse_packet_line(self, raw_line: str) -> tuple[Optional[str], Optional[int]]:
        """Extracts hex message and optional RSSI from hardware or AVR text format."""
        raw_line = raw_line.strip()
        if not raw_line:
            return None, None
        m = re.search(r'#MDS\*([0-9A-Fa-f]+);\s*\((-?\d+),(-?\d+)', raw_line)
        if m:
            return m.group(1).upper(), int(m.group(3))
        m = re.search(r'\*([0-9A-Fa-f]{14,28});', raw_line)
        if m:
            return m.group(1).upper(), None
        return None, None

    def decode_message(self, hex_msg: str, rssi: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Validates CRC and decodes BDS/Typecode fields into a structured dictionary.
        """
        dec = decoder.decode_message(hex_msg)
        if not dec or not dec.get('crc_valid'):
            return None

        df = dec.get('df')
        if df not in (17, 18):
            return None

        icao = dec.get('icao')
        tc = dec.get('typecode')
        now = time.time()

        if icao not in self.aircraft_history:
            self.aircraft_history[icao] = {
                "icao": icao,
                "first_seen": now,
                "last_seen": now,
                "callsign": None,
                "altitude": None,
                "speed": None,
                "heading": None,
                "vrate": None,
                "latitude": None,
                "longitude": None,
                "distance_km": None,
                "cpr_even": None,
                "cpr_odd": None,
                "cpr_even_time": None,
                "cpr_odd_time": None,
                "messages": 0
            }

        entry = self.aircraft_history[icao]
        entry["last_seen"] = now
        entry["messages"] += 1
        if rssi is not None:
            entry["rssi"] = rssi

        decoded_info = {
            "icao": icao,
            "df": df,
            "typecode": tc,
            "timestamp": now,
            "rssi": rssi,
            "field_updated": None
        }

        # 1-4: Aircraft Identification (Callsign)
        if "callsign" in dec and dec["callsign"]:
            cs = dec["callsign"].strip("_ ")
            if cs:
                entry["callsign"] = cs
                decoded_info["field_updated"] = "callsign"
                decoded_info["callsign"] = cs

        # 9-18: Airborne Position
        elif tc is not None and 9 <= tc <= 18:
            alt = dec.get("altitude")
            if alt is not None:
                entry["altitude"] = alt
                decoded_info["altitude"] = alt

            cpr_format = dec.get("cpr_format")
            cpr_lat = dec.get("cpr_lat")
            cpr_lon = dec.get("cpr_lon")
            pos_resolved = False

            if (
                self.observer_lat is not None and self.observer_lon is not None and
                cpr_lat is not None and cpr_lon is not None
            ):
                ref_lat = entry["latitude"] if entry["latitude"] is not None else self.observer_lat
                ref_lon = entry["longitude"] if entry["longitude"] is not None else self.observer_lon
                try:
                    pos = decoder.decode_cpr_airborne_local(cpr_format, cpr_lat, cpr_lon, ref_lat, ref_lon)
                    if pos:
                        entry["latitude"], entry["longitude"] = pos
                        pos_resolved = True
                except Exception:
                    pass

            if not pos_resolved and cpr_lat is not None and cpr_lon is not None:
                if cpr_format == 0:
                    entry["cpr_even"] = (cpr_lat, cpr_lon)
                    entry["cpr_even_time"] = now
                else:
                    entry["cpr_odd"] = (cpr_lat, cpr_lon)
                    entry["cpr_odd_time"] = now

                if (
                    entry["cpr_even"] and entry["cpr_odd"] and
                    entry["cpr_even_time"] and entry["cpr_odd_time"] and
                    abs(entry["cpr_even_time"] - entry["cpr_odd_time"]) <= 10.0
                ):
                    try:
                        pos = decoder.decode_cpr_airborne_pair(
                            entry["cpr_even"][0], entry["cpr_even"][1],
                            entry["cpr_odd"][0], entry["cpr_odd"][1],
                            even_is_newer=(entry["cpr_even_time"] >= entry["cpr_odd_time"])
                        )
                        if pos:
                            entry["latitude"], entry["longitude"] = pos
                            pos_resolved = True
                    except Exception:
                        pass

            if pos_resolved and self.observer_lat is not None and self.observer_lon is not None:
                entry["distance_km"] = decoder.calculate_distance_km(
                    self.observer_lat, self.observer_lon,
                    entry["latitude"], entry["longitude"]
                )

            if pos_resolved:
                decoded_info["field_updated"] = "position"
                decoded_info["latitude"] = entry["latitude"]
                decoded_info["longitude"] = entry["longitude"]
                decoded_info["distance_km"] = entry.get("distance_km")

        # 19: Airborne Velocity
        elif tc == 19:
            speed = dec.get("groundspeed")
            heading = dec.get("track")
            vrate = dec.get("vertical_rate")
            if speed is not None:
                entry["speed"] = speed
                decoded_info["speed"] = speed
            if heading is not None:
                entry["heading"] = heading
                decoded_info["heading"] = heading
            if vrate is not None:
                entry["vrate"] = vrate
                decoded_info["vrate"] = vrate
            decoded_info["field_updated"] = "velocity"

        self.total_decoded += 1
        return {**entry, "update": decoded_info}

    def listen(self) -> Generator[Dict[str, Any], None, None]:
        """
        Yields decoded aircraft telemetry events continuously as datagrams arrive.
        """
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.total_received += 1
                text = data.decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    hex_msg, rssi = self.parse_packet_line(line)
                    if hex_msg:
                        decoded = self.decode_message(hex_msg, rssi)
                        if decoded:
                            yield decoded
            except socket.timeout:
                continue
            except (KeyboardInterrupt, SystemExit):
                break

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    config = load_config()
    udp_cfg = config.get("udp", {})
    obs_cfg = config.get("observer", {})

    parser = argparse.ArgumentParser(description="Standalone UDP ADS-B Decoder Client")
    parser.add_argument('--host', default=udp_cfg.get('listen_host', '0.0.0.0'), help="UDP bind host (default: 0.0.0.0)")
    parser.add_argument('--port', type=int, default=udp_cfg.get('listen_port', 5005), help="UDP bind port (default: 5005)")
    parser.add_argument('--json', action='store_true', help="Output telemetry as JSON lines")
    parser.add_argument('--lat', type=float, help="Observer latitude (optional)")
    parser.add_argument('--lon', type=float, help="Observer longitude (optional)")
    parser.add_argument('--no-observer', action='store_true', help="Disable observer coordinates (Global CPR mode)")
    args = parser.parse_args()

    obs_lat = None
    obs_lon = None
    if not args.no_observer:
        if args.lat is not None and args.lon is not None:
            obs_lat, obs_lon = args.lat, args.lon
        elif obs_cfg.get("enabled", True):
            obs_lat = obs_cfg.get("latitude")
            obs_lon = obs_cfg.get("longitude")

    print(f"[*] ADS-B UDP Decoder listening on {args.host}:{args.port}")
    if obs_lat is not None and obs_lon is not None:
        print(f"[*] Observer set to ({obs_lat:.4f}, {obs_lon:.4f}) - Local CPR Enabled")
    else:
        print("[*] Observer coordinates disabled - Global CPR Mode Enabled")

    client = UdpAdsbClient(
        host=args.host,
        port=args.port,
        observer_lat=obs_lat,
        observer_lon=obs_lon
    )

    try:
        for update in client.listen():
            icao = update["icao"]
            call = update.get("callsign") or "----"
            alt = f"{update.get('altitude'):,}ft" if update.get('altitude') is not None else "----"
            spd = f"{update.get('speed')}kts" if update.get('speed') is not None else "----"
            hdg = f"{update.get('heading'):.0f}°" if update.get('heading') is not None else "---"
            lat = f"{update.get('latitude'):.4f}" if update.get('latitude') is not None else "----"
            lon = f"{update.get('longitude'):.4f}" if update.get('longitude') is not None else "----"
            dist = f"{update.get('distance_km'):.1f}km" if update.get('distance_km') is not None else "---"
            rssi = f"{update.get('rssi')}dBm" if update.get('rssi') is not None else "---"

            if args.json:
                print(json.dumps(update, default=str))
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] ICAO: {icao} | CALL: {call:<8} | ALT: {alt:<10} | SPD: {spd:<7} | HDG: {hdg:<5} | POS: ({lat}, {lon}) | DIST: {dist} | RSSI: {rssi}")
    except KeyboardInterrupt:
        print("\n[*] Stopping UDP receiver.")
    finally:
        client.close()


if __name__ == '__main__':
    main()
