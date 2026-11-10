#!/usr/bin/env python3
"""
ADS-B Real-Time Terminal Radar & Telemetry Tracker
Zero dependency on pyModeS. Only requires 'pyserial'.
Compatible with USB-UART (CH340 / CH341 / FTDI) at 3.3V logic level.
"""

import sys
import os
import re
import json
import time
import math
import threading
import argparse
from datetime import datetime
from collections import deque

# Enable UTF-8 code page (65001) and Virtual Terminal Processing on Windows
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

import decoder

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

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
            "latitude": 51.4700,
            "longitude": -0.4543
        },
        "radar": {
            "prune_timeout_seconds": 30.0,
            "stale_timeout_seconds": 10.0,
            "cpr_pair_max_interval_seconds": 10.0
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

def parse_raw_line(line: str) -> tuple[str | None, int | None]:
    """
    Parses hardware output:
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
# Aircraft Telemetry Store
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

        dec = decoder.decode_message(msg_hex)
        if not dec:
            return

        icao = dec.get('icao')
        if not icao:
            return

        tc = dec.get('typecode')

        with self.lock:
            if icao not in self.aircraft:
                self.aircraft[icao] = {
                    'icao': icao,
                    'callsign': '------',
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
                rssi_str = f" (RSSI: {rssi} dBm)" if rssi is not None else ""
                self.log_event(f"NEW AIRCRAFT: ICAO {icao}{rssi_str}")

            ac = self.aircraft[icao]
            ac['msg_count'] += 1
            ac['last_seen_epoch'] = now
            ac['last_seen_ts'] = ts
            if rssi is not None:
                ac['last_rssi'] = rssi

            # 1. Identification & Callsign
            if 'callsign' in dec and dec['callsign']:
                cs = dec['callsign'].strip('_ ')
                if cs and ac['callsign'] != cs:
                    ac['callsign'] = cs
                    self.log_event(f"ICAO {icao} Callsign: {cs}")

            # 2. Airborne Position & Altitude (CPR)
            if tc and 9 <= tc <= 18:
                alt = dec.get('altitude')
                if alt is not None:
                    ac['altitude'] = alt

                cpr_fmt = dec.get('cpr_format')
                cpr_lat = dec.get('cpr_lat')
                cpr_lon = dec.get('cpr_lon')

                pos = None
                # Mode A: Single-frame Local CPR if observer position is configured
                if self.has_observer and cpr_fmt in (0, 1) and cpr_lat is not None and cpr_lon is not None:
                    try:
                        ref_lat = ac['lat'] if ac['lat'] is not None else self.observer_lat
                        ref_lon = ac['lon'] if ac['lon'] is not None else self.observer_lon
                        pos = decoder.decode_cpr_airborne_local(cpr_fmt, cpr_lat, cpr_lon, ref_lat, ref_lon)
                    except Exception:
                        pos = None

                # Mode B: Global CPR Pair (works with or without observer)
                if cpr_fmt == 0:
                    ac['cpr_even'] = (cpr_lat, cpr_lon, now)
                elif cpr_fmt == 1:
                    ac['cpr_odd'] = (cpr_lat, cpr_lon, now)

                if not pos and ac['cpr_even'] and ac['cpr_odd']:
                    lat_e, lon_e, t_e = ac['cpr_even']
                    lat_o, lon_o, t_o = ac['cpr_odd']
                    if abs(t_e - t_o) <= self.cpr_max_interval:
                        try:
                            pos = decoder.decode_cpr_airborne_pair(
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
                        dist_km = decoder.calculate_distance_km(self.observer_lat, self.observer_lon, lat, lon)
                        ac['dist_km'] = dist_km
                        ac['dist_nm'] = dist_km / 1.852
                        b_deg, b_card = decoder.calculate_bearing(self.observer_lat, self.observer_lon, lat, lon)
                        ac['bearing_deg'] = b_deg
                        ac['bearing_cardinal'] = b_card
                        if first_fix:
                            self.log_event(f"ICAO {icao} Position solved: {dist_km:.1f} km away ({b_card})")
                    else:
                        if first_fix:
                            self.log_event(f"ICAO {icao} Global Position solved: {lat:.4f}, {lon:.4f}")

            # 3. Velocity & Heading
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
# Dashboard Renderers
# ============================================================================

def get_heading_arrow(deg: float) -> str:
    arrows = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    idx = int((deg + 22.5) / 45.0) % 8
    return arrows[idx]

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

    obs_str = f"Observer: ({tracker.observer_lat:.4f}, {tracker.observer_lon:.4f})" if has_obs else "Observer: Not Set (Global CPR Mode)"
    status_style = "bold green" if is_conn else "bold yellow"

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
    header_text.append(f"| Prune: {int(tracker.prune_timeout)}s", style="dim")

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
            "[dim]IDLE[/]",
            "------",
            "------",
            "[dim]---[/]",
            "[dim]---[/]",
            "[dim]Searching for aircraft in range...[/]",
            "---",
            "---",
            "0"
        ]
        if has_obs:
            idle_row.insert(6, "---")
        table.add_row(*idle_row)
    else:
        for ac in aircraft_list:
            age = now - ac['last_seen_epoch']
            status = "[bold green]LIVE[/]" if age < tracker.stale_timeout else f"[bold yellow]{int(age)}s[/]"
            icao = ac['icao']
            callsign = ac['callsign'] if ac['callsign'] != '------' else "[dim]------[/]"

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

            row_items = [
                status,
                icao,
                callsign,
                alt_str,
                spd_str,
                coord_str
            ]
            if has_obs:
                dist_str = f"[bold magenta]{ac['dist_km']:.1f} km[/] [yellow]{ac['bearing_cardinal']}[/]" if ac['dist_km'] else "[dim]---[/]"
                row_items.append(dist_str)

            row_items.extend([
                rssi_str,
                f"{age:.1f}s",
                str(ac['msg_count'])
            ])

            table.add_row(*row_items)

    log_renderables = [Text(ev) for ev in events] if events else [Text("Listening on 1090 MHz. Place antenna near window for best reception.", style="dim")]
    log_panel = Panel(Group(*log_renderables), title="[bold] Radio Activity Feed [/]", border_style="dim", height=6)
    return Group(header_panel, table, log_panel)

def render_ansi_fallback(tracker: RadarTracker, port_info: str):
    """Pure-Python terminal rendering when 'rich' is not installed."""
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

    obs_str = f"Observer: ({tracker.observer_lat:.4f}, {tracker.observer_lon:.4f})" if has_obs else "Observer: Not Configured"

    out = ["\033[H"]
    out.append("=" * 96)
    out.append(f" ADS-B RADAR  [{conn_status}]  Source: {port_info} | {obs_str}")
    out.append(f" Active: {active_count} | Rate: {msg_rate} msg/s | Decoded: {total_msgs} | Packets: {raw_packets} | Prune: {int(tracker.prune_timeout)}s")
    out.append("=" * 96)

    if has_obs:
        out.append(f" {'STAT':<5} | {'ICAO':<6} | {'CALLSIGN':<8} | {'ALTITUDE (V/S)':<18} | {'SPEED & HDG':<16} | {'COORDINATES':<15} | {'RANGE & BRG':<11} | {'RSSI':<7} | {'SEEN':<5} | {'MSGS':<4}")
    else:
        out.append(f" {'STAT':<5} | {'ICAO':<6} | {'CALLSIGN':<8} | {'ALTITUDE (V/S)':<18} | {'SPEED & HDG':<16} | {'COORDINATES':<22} | {'RSSI':<7} | {'SEEN':<5} | {'MSGS':<4}")
    out.append("-" * 96)

    if not aircraft_list:
        out.append(" Searching for aircraft in range...")
    else:
        for ac in aircraft_list:
            age = now - ac['last_seen_epoch']
            stat = "LIVE" if age < tracker.stale_timeout else f"{int(age)}s"
            icao = ac['icao']
            call = ac['callsign']
            alt = f"{ac['altitude']:,}ft" if ac['altitude'] else "---"
            if ac['vertical_rate']:
                alt += f" ({ac['vertical_rate']:+d})"
            spd = f"{ac['speed']}kts @ {ac['track']:.0f}°" if ac['speed'] and ac['track'] else "---"
            coords = f"{ac['lat']:.3f}, {ac['lon']:.3f}" if ac['lat'] else "Syncing..."
            rssi = f"{ac['last_rssi']}dBm" if ac['last_rssi'] else "---"

            if has_obs:
                dist = f"{ac['dist_km']:.1f}km {ac['bearing_cardinal']}" if ac['dist_km'] else "---"
                out.append(f" {stat:<5} | {icao:<6} | {call:<8} | {alt:<18} | {spd:<16} | {coords:<15} | {dist:<11} | {rssi:<7} | {age:.1f}s | {ac['msg_count']:<4}")
            else:
                out.append(f" {stat:<5} | {icao:<6} | {call:<8} | {alt:<18} | {spd:<16} | {coords:<22} | {rssi:<7} | {age:.1f}s | {ac['msg_count']:<4}")

    out.append("=" * 96)
    out.append("Radio Activity Feed:")
    if events:
        for ev in events:
            out.append(f"  {ev}")
    else:
        out.append("  Listening for 1090 MHz squitters...")
    out.append("=" * 96)

    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()

# ============================================================================
# Background Threads (Serial / Playback)
# ============================================================================

def serial_reader_worker(port: str, baud: int, timeout: float, tracker: RadarTracker, stop_event: threading.Event, save_file: str | None):
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

                                tracker.process_raw_line(raw_line)
                    else:
                        line_buf.append(b)
    finally:
        ser.close()
        if f_save:
            f_save.close()
        tracker.is_connected = False
        tracker.status_message = "DISCONNECTED"

def file_reader_worker(file_path: str, tracker: RadarTracker, stop_event: threading.Event, speed: float = 1.0):
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

            tracker.total_raw_packets += 1
            tracker.process_raw_line(line)

            if delay > 0:
                time.sleep(delay)

        time.sleep(1.0)

# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    config = load_config()

    parser = argparse.ArgumentParser(description="Live ADS-B Terminal Radar")
    parser.add_argument('--port', help=f"Serial port (default from config: {config['serial']['port']})")
    parser.add_argument('--baud', type=int, help=f"Baud rate (default from config: {config['serial']['baudrate']})")
    parser.add_argument('--file', help="Replay messages from a recorded file")
    parser.add_argument('--demo', action='store_true', help="Run demo mode using bundled sample flight")
    parser.add_argument('--save', help="Save incoming serial traffic to file")
    parser.add_argument('--lat', type=float, help="Override observer latitude")
    parser.add_argument('--lon', type=float, help="Override observer longitude")
    parser.add_argument('--no-observer', action='store_true', help="Disable observer coordinates (Global CPR mode)")
    parser.add_argument('--speed', type=float, default=2.0, help="Replay speed multiplier (default: 2.0)")
    args = parser.parse_args()

    # Apply command-line overrides
    port = args.port or config['serial']['port']
    baud = args.baud or config['serial']['baudrate']
    timeout = config['serial'].get('timeout', 0.2)

    if args.no_observer:
        config['observer']['enabled'] = False
    elif args.lat is not None and args.lon is not None:
        config['observer']['enabled'] = True
        config['observer']['latitude'] = args.lat
        config['observer']['longitude'] = args.lon

    save_file = args.save or (config['logging']['raw_traffic_file'] if config['logging'].get('save_raw_traffic') else None)

    tracker = RadarTracker(config)
    stop_event = threading.Event()

    target_file = None
    if args.demo:
        demo_candidate = os.path.join(os.path.dirname(__file__), 'sample_flight.txt')
        if not os.path.exists(demo_candidate):
            demo_candidate = os.path.join(os.path.dirname(__file__), 'serial_dump.txt')
        target_file = demo_candidate
    elif args.file:
        target_file = args.file

    source_info = f"Demo ({os.path.basename(target_file)})" if target_file else f"{port} @ {baud}"

    if target_file:
        reader = threading.Thread(
            target=file_reader_worker,
            args=(target_file, tracker, stop_event, args.speed),
            daemon=True
        )
    else:
        reader = threading.Thread(
            target=serial_reader_worker,
            args=(port, baud, timeout, tracker, stop_event, save_file),
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
            reader.join(timeout=1.0)
            print("\nRadar tracker stopped.")

if __name__ == '__main__':
    main()
