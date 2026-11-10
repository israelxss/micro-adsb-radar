#!/usr/bin/env python3
"""
ADS-B Raw Serial Data Stream Recorder
Records live serial bytes from 1090 MHz receiver to text and binary dumps.
"""

import sys
import os
import time
import argparse
from datetime import datetime
import serial

def record_stream(port: str, baud: int, output_file: str, duration_sec: float | None = None):
    print(f"Opening {port} @ {baud} baud (DTR/RTS enabled)...")
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2
        )
    except Exception as e:
        print(f"[ERROR] Failed to open port {port}: {e}")
        return

    ser.dtr = True
    ser.rts = True
    time.sleep(0.1)
    ser.reset_input_buffer()

    limit_str = f"for {duration_sec}s" if duration_sec else "until Ctrl+C"
    print(f"Recording live ADS-B packets to '{output_file}' {limit_str}...")

    start_time = time.time()
    packet_count = 0
    total_bytes = 0
    line_buf = bytearray()

    try:
        with open(output_file, 'a', encoding='utf-8') as f_out:
            while True:
                if duration_sec and (time.time() - start_time >= duration_sec):
                    break

                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    total_bytes += len(chunk)
                    for b in chunk:
                        if b in (ord(b'\n'), ord(b'\r')):
                            if line_buf:
                                raw_line = line_buf.decode('ascii', errors='ignore').strip()
                                line_buf.clear()
                                if raw_line:
                                    packet_count += 1
                                    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                                    log_line = f"[{ts}] {raw_line}\n"
                                    f_out.write(log_line)
                                    f_out.flush()
                                    print(f"[{ts}] {raw_line}")
                        else:
                            line_buf.append(b)

    except KeyboardInterrupt:
        print("\nRecording stopped by user.")
    finally:
        ser.close()
        elapsed = round(time.time() - start_time, 1)
        print(f"\nRecording Finished: {packet_count} packets ({total_bytes} bytes) captured in {elapsed}s.")
        print(f"Output saved to: {os.path.abspath(output_file)}")

def main():
    parser = argparse.ArgumentParser(description="Record live ADS-B serial packets")
    parser.add_argument('--port', default='COM12', help="Serial port (default: COM12)")
    parser.add_argument('--baud', type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument('--out', default='adsb_traffic.log', help="Output file (default: adsb_traffic.log)")
    parser.add_argument('--duration', type=float, help="Recording duration in seconds (default: infinite)")
    args = parser.parse_args()

    record_stream(args.port, args.baud, args.out, args.duration)

if __name__ == '__main__':
    main()
