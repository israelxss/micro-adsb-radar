# Micro ADS-B Radar (1090 MHz)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-israelxss%2Fmicro--adsb--radar-green)](https://github.com/israelxss/micro-adsb-radar)

A lightweight, standalone ADS-B and Mode-S receiver and flight radar for terminal environments. 
Repository: [https://github.com/israelxss/micro-adsb-radar](https://github.com/israelxss/micro-adsb-radar) 

The project has **zero dependency on `pyModeS`** or any heavy SDR framework. The core decoder is written in 100% pure Python, relying solely on **`pyserial`** for serial communications.

> [!NOTE]
> **Privacy Notice:**  
> All aircraft identifiers, flight numbers, coordinates, and observer locations presented throughout this documentation and in the example configuration are **entirely fictional / synthetic sample data**, used solely for demonstration purposes to protect personal location privacy.

---

## 🛰️ Hardware Module & Specifications

This software is built and verified to interface with the dedicated 1090 MHz Mode-S / ADS-B hardware receiver module:
* **AliExpress Module Link:** [1090MHz ADS-B Receiver Module](https://a.aliexpress.com/_c2uQiMXH)

### ⚠️ Critical Hardware Warnings & Corrections

> [!CAUTION]
> **Logic Level MUST be 3.3V:**  
> When connecting the module to a USB-to-UART adapter (e.g., FTDI FT232R, CH340, CP2102), **ensure the VCC / IO jumper is set to 3.3V**. Applying 5V to the module's serial lines can permanently damage the onboard RF and microcontroller ICs.

> [!IMPORTANT]
> **Baud Rate is 115200, NOT 9600:**  
> While the AliExpress listing states 9600 baud, **the actual hardware firmware outputs serial data at 115200 baud (8-N-1)**. Connecting at 9600 baud will yield zero data or framing errors.

---

## 🔌 Wiring & Pinout Guide

Connect the module to your 3.3V FTDI / USB-UART bridge as follows:

| Module Pin | FTDI / USB-UART Pin | Notes |
| :--- | :--- | :--- |
| **VCC** | **3.3V** | Set FTDI jumper to **3.3V** |
| **GND** | **GND** | Common ground |
| **TX** | **RXD** | Serial data output from module to PC |
| **RX** | **TXD** | (Optional) Module input |

### RF & Antenna Setup
* Use a tuned **1090 MHz antenna** (standard SMA connector).
* For optimal reception, place the antenna near a window or outdoors. ADS-B signals travel strictly via **Line-of-Sight (LOS)** and are attenuated by thick walls and reinforced concrete.

---

## 🚀 Key Features

* **Zero External Decoding Dependencies:** Built-in pure-Python Mode-S CRC-24 error validation, ICAO 6-bit callsign parsing, 25-ft & 100-ft Gillham altitude decoders, velocity/heading calculation, and Compact Position Reporting (CPR).
* **Dual CPR Positioning Engine:**
  * **Local Single-Frame CPR:** Instant latitude/longitude decoding from the very first frame using the observer location as a reference.
  * **Global CPR (No Observer Required):** Automatically resolves aircraft positions without requiring observer coordinates by pairing Even and Odd frames.
* **Auto-Pruning Inactive Aircraft:** Automatically drops stale aircraft that haven't transmitted for 30 seconds.
* **Rich Terminal UI with Fallback:** Beautiful real-time dashboard powered by `rich` (if installed) with an automatic pure-Python ANSI fallback.
* **Configurable:** Centralized `config.json` file for serial port, observer location, timeouts, and logging.

---

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/israelxss/micro-adsb-radar.git
   cd micro-adsb-radar
   ```
2. Install the single required dependency:
   ```bash
   pip install pyserial
   ```
3. *(Optional)* Install `rich` for enhanced terminal graphics and colors:
   ```bash
   pip install rich
   ```

---

## ⚙️ Configuration (`config.json`)

The application parameters can be customized in `config.json` *(values below are synthetic sample values)*:

```json
{
  "serial": {
    "port": "COM12",
    "baudrate": 115200,
    "timeout": 0.2
  },
  "observer": {
    "enabled": true,
    "latitude": 51.4700,
    "longitude": -0.4543
  },
  "radar": {
    "prune_timeout_seconds": 30.0,
    "stale_timeout_seconds": 10.0,
    "cpr_pair_max_interval_seconds": 10.0
  },
  "logging": {
    "save_raw_traffic": false,
    "raw_traffic_file": "adsb_traffic.log"
  }
}
```

### Observer Configuration Modes
* **With Observer Coordinates (`"enabled": true`):** Enables instant single-frame position decoding, real-time ground distance (km / NM), and relative compass bearing (e.g., `095° E`).
* **Without Observer Coordinates (`"enabled": false` or `--no-observer`):** The radar runs in **Global CPR Mode**. Position is computed globally by pairing even and odd frames. Distance and Bearing fields are omitted.

---

## 🖥️ Usage

### 1. Live Radar Mode (Default)
Starts the live radar listening on the configured serial port:
```bash
python main.py
```

Override serial port or baud rate via CLI:
```bash
python main.py --port COM12 --baud 115200
```

Run without observer coordinates (Global CPR mode):
```bash
python main.py --no-observer
```

### 2. Instant Demo Mode (Simulation Replay)
Tests the dashboard immediately using the bundled flight data (`sample_flight.txt`):
```bash
python main.py --demo --speed 2.0
```

### 3. Replay a Recorded Traffic File
```bash
python main.py --file sample_flight.txt --speed 1.0
```

### 4. Record Raw Traffic Stream
Use the bundled `recorder.py` utility to capture raw packets:
```bash
python recorder.py --port COM12 --baud 115200 --out my_flight_dump.log
```

---

## 📊 Terminal Dashboard Preview

*(The sample table below uses synthetic / fictional flight data for privacy protection)*

```text
╭──────────────────────────────────────────────────────────────────────────────╮
│  ADS-B RADAR   [ONLINE (Listening 1090MHz)] Port: COM12 @ 115200 |           │
│ Observer: (51.4700, -0.4543)                                                 │
│ ⠴ Active: 1 | Rate: 5 msg/s | Decoded: 42 | Packets: 42 | Prune: 30s        │
╰──────────────────────────────────────────────────────────────────────────────╯
╭──────┬───────┬────────┬──────────────┬─────────────┬─────────────┬────────────┬─────────┬──────┬──────╮
│ STAT │ ICAO  │ CALL   │ ALTITUDE(V/S)│ SPEED & HDG │ COORDINATES │ RANGE & BRG│ RSSI    │ SEEN │ MSGS │
├──────┼───────┼────────┼──────────────┼─────────────┼─────────────┼────────────┼─────────┼──────┼──────┤
│ LIVE │ 4840D6│ KLM1023│ 6,500ft(-850)│ 260kts @ 095│ 51.505,-0.12│ 24.5km (E) │ -98 dBm │ 0.8s │   42 │
╰──────┴───────┴────────┴──────────────┴─────────────┴─────────────┴────────────┴─────────┴──────┴──────╯
╭─────────────────────────── Radio Activity Feed ──────────────────────────────╮
│ [14:22:05] NEW AIRCRAFT: ICAO 4840D6 (RSSI: -98 dBm)                         │
│ [14:22:06] ICAO 4840D6 Callsign: KLM1023                                     │
│ [14:22:08] ICAO 4840D6 Position solved: 24.5 km away (E)                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

---

## 🔬 Protocol & Decoding Technical Details

The module decodes **112-bit Downlink Format 17 (DF17)** and **DF18** extended squitter packets:
* **CRC-24 Parity Check:** Generator polynomial $G(x) = \text{0x1FFF409}$ verifies data integrity.
* **Typecodes 1–4 (BDS 0,8):** 6-bit ICAO character mapping extracts aircraft flight numbers and callsigns.
* **Typecodes 9–18 (BDS 0,5):** Barometric altitude decoding supports both 25-ft linear resolution and 100-ft Gillham Gray code.
* **Typecode 19 (BDS 0,9):** Airborne velocity extracts East-West and North-South vector components, calculating groundspeed, true track, and vertical climb/descent rates.
* **Compact Position Reporting (CPR):** Implements both local single-frame resolution ($NL(\text{lat})$ transition tables) and global even/odd pair ambiguity resolution.

---

## 📁 Repository Structure

```text
├── config.json         # Centralized configuration (serial, observer, radar)
├── decoder.py          # Standalone pure-Python ADS-B/Mode-S decoding engine
├── main.py             # Live terminal radar dashboard application
├── recorder.py         # Standalone raw packet recording utility
├── sample_flight.txt   # Flight data sample for offline testing & demo
└── README.md           # Documentation and hardware setup guide
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
GitHub Repository: [https://github.com/israelxss/micro-adsb-radar](https://github.com/israelxss/micro-adsb-radar)
