# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 soundtouch_controller.py                    # foreground, auto-discover speakers
python3 soundtouch_controller.py --port 9090        # custom port
python3 soundtouch_controller.py --ip 192.168.1.50  # skip discovery, connect directly
python3 soundtouch_controller.py --daemon           # detach to background
```

Web UI is served at `http://<machine-ip>:8888` by default.

## Installation

```bash
bash install.sh   # installs Python deps, opens firewall ports, optional systemd service
```

## Service management

```bash
systemctl --user status  soundtouch
systemctl --user restart soundtouch
journalctl --user -u soundtouch -f    # live logs
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8888 | TCP | Web UI |
| 8090 | TCP | SoundTouch speaker API (outbound to speaker) |
| 1900 | UDP | SSDP multicast (Alexa discovery) |
| 8082 | TCP | Hue bridge HTTP API (Alexa smart home) |
| 80   | TCP | Redirected via iptables to 8082 (Alexa hardcodes port 80) |

## Architecture

Everything lives in one file: `soundtouch_controller.py` (~2150 lines).

**`SoundTouchDevice` (line 94)** — HTTP REST client for a speaker's port-8090 XML API. `_get()` and `_post()` are the low-level transport; `_key()` sends remote-key presses. `state()` aggregates volume + now-playing + presets into a single dict for the web UI.

**`PresetStore` (line 296)** — Reads/writes preset backups as JSON to `data/presets/<ip>.json` and custom station definitions to `data/stations/<id>.json`. The station server in `Handler` serves `station_descriptor()` JSON so the speaker can resolve a custom stream URL.

**Discovery (line 393)** — `discover_mdns()` uses zeroconf to find `_soundtouch._tcp.local.` services. `discover_subnet_scan()` concurrently probes all 254 hosts on the local /24. Both run in parallel via `discover_all()`.

**`Handler` (line 1404)** — `BaseHTTPRequestHandler` serving the single-page web UI (the `HTML` string embedded at line ~547) and a REST API under `/api/`. Notable endpoints: `/api/state`, `/api/cmd`, `/api/scan`, `/api/presets/*`, `/api/stations/*`, `/api/zones/*`.

**`AlexaBridge` (line 1776)** — Emulates a Philips Hue BSB002 bridge. `build_devices()` creates one virtual Hue light per preset slot (6) plus one power light per speaker. The Hue HTTP API runs on `HUE_PORT` (8082); an SSDP listener on UDP 1900 responds to Alexa M-SEARCH probes. Amazon Echo firmware always probes port 80, so `install.sh` adds an iptables NAT rule redirecting 80 → 8082.

**`AppState` (line 2048)** — Singleton holding the discovered device list, the `PresetStore`, and the `AlexaBridge`. Passed to `Handler` via `Handler.server_state`.

**`main()` (line 2127)** — Parses `--port`, `--ip`, `--daemon`. Runs a network diagnostic (`_check_network()`), starts `AppState.scan()`, then launches `ThreadingHTTPServer`.

## Custom internet radio presets

Edit the `LOCAL_INTERNET_RADIO` list near the top of `soundtouch_controller.py` to add hardcoded stream presets. Dynamic custom stations are saved via the web UI and stored in `data/stations/`.

## Logs

`soundtouch.log` in the project root (rotates at 1 MB, keeps 5 files). Console shows INFO+; file shows DEBUG+.

## Dependencies

- `requests` — HTTP to speaker API
- `zeroconf` — mDNS speaker discovery
- `Pillow` (optional) — album art processing

No test suite exists. No linter configuration.
