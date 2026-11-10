#!/usr/bin/env python3
"""
UDP Broadcaster & Server for ADS-B / Mode-S Traffic.
Streams raw ADS-B packets received from USB-UART serial receiver
or simulated flight logs to UDP clients or local network broadcast.
Zero external dependencies beyond pyserial and Python standard library.
"""

import sys
import os
import time
import socket
import json
import argparse
import threading
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config() -> dict:
    default_config = {
        "serial": {
            "port": "COM12",
            "baudrate": 115200,
            "timeout": 0.2
        },
        "udp": {
            "target_host": "127.0.0.1",
            "target_port": 5005,
            "broadcast": False
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


class UdpBroadcaster:
    """Manages UDP socket transmission with optional subnet broadcasting."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5005, broadcast: bool = False):
        self.host = host
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
                print(f"[ERROR] UDP sendto failed: {e}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def run_serial_server(port: str, baud: int, broadcaster: UdpBroadcaster, stop_event: threading.Event):
    import serial
    print(f"[*] Opening serial port {port} @ {baud} baud...")
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2
        )
        ser.dtr = True
        ser.rts = True
        time.sleep(0.1)
        ser.reset_input_buffer()
        print(f"[+] Serial port {port} online. Streaming to UDP {broadcaster.host}:{broadcaster.port} (Broadcast: {broadcaster.broadcast})")
    except Exception as e:
        print(f"[!] Error opening serial port: {e}")
        stop_event.set()
        return

    line_buf = bytearray()
    try:
        while not stop_event.is_set():
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                for b in chunk:
                    if b in (ord(b'\n'), ord(b'\r')):
                        if line_buf:
                            raw_line = line_buf.decode('ascii', errors='ignore').strip()
                            line_buf.clear()
                            if raw_line:
                                broadcaster.send_line(raw_line)
                                if broadcaster.packets_sent % 10 == 0:
                                    print(f"[*] Forwarded {broadcaster.packets_sent} packets ({broadcaster.bytes_sent} bytes) to {broadcaster.host}:{broadcaster.port}", end='\r')
                    else:
                        line_buf.append(b)
    finally:
        ser.close()
        broadcaster.close()
        print("\n[*] Serial connection closed.")


def run_file_server(file_path: str, broadcaster: UdpBroadcaster, stop_event: threading.Event, speed: float = 1.0):
    if not os.path.exists(file_path):
        print(f"[!] File not found: {file_path}")
        stop_event.set()
        return

    print(f"[+] Replaying flight log: {file_path}")
    print(f"[+] Streaming to UDP {broadcaster.host}:{broadcaster.port} at {speed}x speed")

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = [line.strip() for line in f if line.strip()]

    delay = 0.4 / speed if speed > 0 else 0
    try:
        while not stop_event.is_set():
            for line in lines:
                if stop_event.is_set():
                    break
                broadcaster.send_line(line)
                if broadcaster.packets_sent % 10 == 0:
                    print(f"[*] Forwarded {broadcaster.packets_sent} packets to {broadcaster.host}:{broadcaster.port}", end='\r')
                if delay > 0:
                    time.sleep(delay)
            time.sleep(1.0)
    finally:
        broadcaster.close()
        print("\n[*] File replay stopped.")


def main():
    config = load_config()
    udp_cfg = config.get("udp", {})
    serial_cfg = config.get("serial", {})

    parser = argparse.ArgumentParser(description="ADS-B UDP Streaming Server / Broadcaster")
    parser.add_argument('--port', help=f"Serial port (default: {serial_cfg.get('port', 'COM12')})")
    parser.add_argument('--baud', type=int, help=f"Baud rate (default: {serial_cfg.get('baudrate', 115200)})")
    parser.add_argument('--target-host', default=udp_cfg.get('target_host', '127.0.0.1'), help="Target UDP IP (default: 127.0.0.1)")
    parser.add_argument('--target-port', type=int, default=udp_cfg.get('target_port', 5005), help="Target UDP port (default: 5005)")
    parser.add_argument('--broadcast', action='store_true', default=udp_cfg.get('broadcast', False), help="Enable UDP subnet broadcast")
    parser.add_argument('--demo', action='store_true', help="Stream sample flight data instead of serial")
    parser.add_argument('--file', help="Stream recorded flight dump file")
    parser.add_argument('--speed', type=float, default=2.0, help="Replay speed multiplier for demo/file (default: 2.0)")
    args = parser.parse_args()

    port = args.port or serial_cfg.get('port', 'COM12')
    baud = args.baud or serial_cfg.get('baudrate', 115200)

    target_host = "255.255.255.255" if args.broadcast and args.target_host == "127.0.0.1" else args.target_host
    broadcaster = UdpBroadcaster(host=target_host, port=args.target_port, broadcast=args.broadcast)

    stop_event = threading.Event()

    target_file = None
    if args.demo:
        demo_candidate = os.path.join(os.path.dirname(__file__), 'sample_flight.txt')
        target_file = demo_candidate
    elif args.file:
        target_file = args.file

    try:
        if target_file:
            run_file_server(target_file, broadcaster, stop_event, speed=args.speed)
        else:
            run_serial_server(port, baud, broadcaster, stop_event)
    except KeyboardInterrupt:
        print("\n[*] Stopping UDP server...")
        stop_event.set()


if __name__ == '__main__':
    main()
