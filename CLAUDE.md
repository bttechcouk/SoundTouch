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

```bash
cd matter_bridge && node matter_bridge.js           # Matter bridge (Alexa integration)
```

## Installation

```bash
bash install.sh   # installs Python deps, opens firewall ports, optional systemd services
```

Node.js 20 LTS+ is required for the Matter bridge. If not present:
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs
cd matter_bridge && npm install
```

## Service management

```bash
systemctl --user status|restart|stop soundtouch          # web controller
systemctl --user status|restart|stop soundtouch-matter   # Matter bridge
journalctl --user -u soundtouch -f                       # controller live logs
journalctl --user -u soundtouch-matter -f                # bridge live logs
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8888 | TCP | Web UI |
| 8090 | TCP | SoundTouch speaker API (outbound to speaker) |
| 5540 | UDP | Matter protocol (Alexa smart home) |

## Architecture

Two processes, both must run together for Alexa integration to work.

### `soundtouch_controller.py` (~1782 lines)

Single-file Python app. All classes in one file.

**`SoundTouchDevice` (line 89)** — HTTP REST client for a speaker's port-8090 XML API. `_get()` / `_post()` are the low-level transport; `_key()` sends remote-key presses. `state()` aggregates volume + now-playing + presets into a single dict for the web UI.

**`PresetStore` (line 291)** — Reads/writes preset backups as JSON to `data/presets/<ip>.json` and custom station definitions to `data/stations/<id>.json`. The station server in `Handler` serves `station_descriptor()` JSON so the speaker can resolve a custom stream URL.

**Discovery (line 401)** — `discover_mdns()` uses zeroconf to find `_soundtouch._tcp.local.` services. `discover_subnet_scan()` concurrently probes all 254 hosts on the local /24. Both run in parallel via `discover_all()`.

**`Handler` (line 1320)** — `BaseHTTPRequestHandler` serving the single-page web UI (the `HTML` string embedded at line ~507) and a REST API under `/api/`. Endpoints: `/api/state`, `/api/cmd`, `/api/scan`, `/api/speakers`, `/api/presets/*`, `/api/stations/*`, `/api/group`, `/api/group/create`, `/api/group/remove`, `/api/group/party`, `/api/group/dissolve-all`, `/api/group/join`, `/api/station-desc/*`.

**`AppState` (line 1634)** — Singleton holding the discovered device list and the `PresetStore`. Passed to `Handler` via `Handler.server_state`.

**`main()` (line 1705)** — Parses `--port`, `--ip`, `--daemon`. Runs a network diagnostic (`_check_network()`), starts `AppState.scan()`, then launches `ThreadingHTTPServer`.

### `matter_bridge/matter_bridge.js`

Node.js process using `@project-chip/matter-node.js` v0.7.5.

On startup it calls `GET /api/speakers` (retrying for up to 120 s until the Python controller is ready), then `GET /api/state?host=<ip>` for each speaker to read preset names. It registers each preset slot (1–6) and a power toggle per speaker as a `OnOffPluginUnitDevice` inside a Matter `Aggregator` (bridge device type). When Alexa sends an on command, it calls `GET /api/cmd?host=<ip>&action=preset<n>` or `action=power`. Group helpers (`/api/group/party`, `/api/group/dissolve-all`, `/api/group/join`) are also used for multi-room Alexa commands.

Key config constants at the top of `matter_bridge.js`:
- `LABEL_FORMAT` / `POWER_FORMAT` — device name templates (`{preset}`, `{room}` tokens)
- `PASSCODE` / `DISCRIMINATOR` — Matter commissioning credentials (fixed; change requires recommissioning)

Commissioning state is persisted to `matter_bridge/data/matter/bridge.json`. Delete this file and restart to force recommissioning.

## Custom internet radio presets

Edit the `LOCAL_INTERNET_RADIO` list near the top of `soundtouch_controller.py` to add hardcoded stream presets. Dynamic custom stations are saved via the web UI and stored in `data/stations/`.

## Logs

- Controller: `soundtouch.log` in project root (rotates at 1 MB, keeps 5 files). Console shows INFO+; file shows DEBUG+.
- Matter bridge: `matter_bridge/matter_bridge.log` (appended, not rotated) and systemd journal.

## Dependencies

Python: `requests`, `zeroconf`, `Pillow` (optional — album art)  
Node.js: `@project-chip/matter-node.js` (ESM, `"type": "module"` in package.json)

No test suite. No linter configuration.
