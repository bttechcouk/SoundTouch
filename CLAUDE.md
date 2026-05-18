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

### `soundtouch_controller.py` (~4700 lines)

Single-file Python app. All classes in one file.

**`SoundTouchDevice` (line 209)** — HTTP REST client for a speaker's port-8090 XML API. `_get()` / `_post()` are the low-level transport; `_key()` sends remote-key presses. `state()` aggregates volume + now-playing + presets + zone role into a single dict for the web UI.

Key methods:
- `state()` — aggregates volume, now-playing, presets, and zone role (`group_role`: `"master"`, `"member"`, or `""`)
- `get_zone()` / `set_zone()` / `remove_zone()` — multi-room zone management via `/getZone` / `/setZone` / `/removeZone`
- `get_bass_capabilities()` / `get_bass()` / `set_bass()` — bass control via `/bassCapabilities` and `/bass`
- `get_sources()` / `select_source()` — source switching (backend only; no UI)
- `detail_info()` — device details from `/info` (firmware, IP, MAC, serial, model, deviceID)
- `set_name()` — rename via `POST /name`

**`PresetStore` (line 573)** — Reads/writes preset backups as JSON to `data/presets/<ip>.json` and custom station definitions to `data/stations/<id>.json`. The station server in `Handler` serves `station_descriptor()` JSON so the speaker can resolve a custom stream URL.

**`SceneStore` (line 676)** — Persists named scenes as JSON to `data/scenes/<id>.json`. A scene captures the playing state of multiple speakers so it can be replayed in one action.

**`AlarmStore` / `AlarmScheduler` (lines 710, 749)** — `AlarmStore` persists alarm definitions to `data/alarms.json`. `AlarmScheduler` runs a background thread that fires alarms at their scheduled time, triggering playback on configured speakers.

**Discovery (line 809)** — `discover_mdns()` uses zeroconf to find `_soundtouch._tcp.local.` services. `discover_subnet_scan()` concurrently probes all 254 hosts on the local /24. Both run in parallel via `discover_all()`.

**PWA assets (lines 82–120)** — Service worker (`/sw.js`), app icon SVG (`/icon.svg`), and PNG icon generator for the PWA manifest and apple-touch-icon are embedded as constants before the class definitions.

**`Handler` (line 3830)** — `BaseHTTPRequestHandler` serving the web UI and a REST API under `/api/`.

Key API endpoints:
- `GET /api/state?host=` — full speaker state (volume, playing, presets, group role)
- `GET /api/cmd?host=&action=&value=` — actions: `playpause`, `next`, `prev`, `power`, `mute`, `preset1`–`6`, `volume`, `bass`
- `GET /api/speakers` — list of discovered speakers with `has_backup` field
- `GET /api/scan` — trigger rediscovery
- `GET /api/bass?host=` — bass capabilities and current level
- `GET /api/device-info?host=` — model, firmware, IP, MAC, serial, device ID
- `GET /api/rename?host=&name=` — rename speaker
- `GET /api/presets/backup-all` — backup all speakers at once
- `GET /api/presets/backup?host=` / `GET /api/presets/restore?host=` — per-speaker backup/restore
- `GET /api/presets/health?host=` — compare live presets against backup
- `GET /api/group?host=` — zone membership info
- `POST /api/group/create`, `/api/group/remove`, `/api/group/party`, `/api/group/dissolve-all`, `/api/group/join`
- `GET /api/stations` / `POST /api/stations/add` / `POST /api/stations/delete` — custom radio stations
- `GET /api/stations/play?host=&id=` — play a custom station immediately
- `POST /api/stations/set-preset` — assign a custom station to a preset slot
- `GET /api/stations/stream-search` — search for a stream URL by station name
- `GET /api/scenes` / `POST /api/scenes` / `POST /api/scenes/delete` / `POST /api/scenes/activate` — named multi-speaker scenes
- `GET /api/alarms` / `POST /api/alarms` / `POST /api/alarms/delete` / `POST /api/alarms/toggle` — wake-up alarms
- `POST /api/tts/announce` — generate gTTS audio and play on speakers; `GET /api/tts/status` — check gTTS availability
- `GET /api/volume/all` — set volume on all speakers simultaneously
- `GET /api/sources?host=` / `POST /api/select` — source listing and switching
- `GET /api/matter/qr` — Matter bridge commissioning QR and status

**`AppState` (line 4564)** — Singleton holding the discovered device list, `PresetStore`, `SceneStore`, `AlarmStore`, and `AlarmScheduler`. Passed to `Handler` via `Handler.server_state`.

**`main()` (line 4640)** — Parses `--port`, `--ip`, `--daemon`. Runs a network diagnostic (`_check_network()`), starts `AppState.scan()`, then launches `ThreadingHTTPServer`.

### `matter_bridge/matter_bridge.js`

Node.js process using `@project-chip/matter-node.js` v0.7.5.

On startup it calls `GET /api/speakers` (retrying for up to 120 s until the Python controller is ready), then `GET /api/state?host=<ip>` for each speaker to read preset names. It registers:
- Each preset slot (1–6) per speaker as an `OnOffPluginUnitDevice`
- A power toggle per speaker as an `OnOffPluginUnitDevice`
- A volume control per speaker as a `DimmablePluginUnitDevice` (maps 0–254 Matter level to 0–100 speaker volume)

All devices are registered inside a Matter `Aggregator` (bridge device type). When Alexa sends a command, the bridge calls `/api/cmd` on the Python controller.

Key config constants at the top of `matter_bridge.js`:
- `LABEL_FORMAT` / `POWER_FORMAT` / `VOLUME_FORMAT` — device name templates (`{preset}`, `{room}` tokens)
- `PASSCODE` / `DISCRIMINATOR` — Matter commissioning credentials (fixed; change requires recommissioning)
- `BRIDGE_API_PORT = 8889` — local HTTP server serving `/qr` for the web UI QR panel

Commissioning state is persisted to `matter_bridge/data/matter/bridge.json`. Delete this file and restart to force recommissioning.

## Web UI structure (inside `HTML` string)

**Tabs:** Player, Presets, Groups, Settings  
**Tab persistence:** active tab saved to `localStorage` and restored on load

**Player tab key elements:**
- `#art-wrap` / `#art` — album art with placeholder
- `#track-info` — track name, artist, source badge (`#source-badge`), group role badge (`#group-badge`)
- `#vol-row` — volume slider with nudge buttons and floating `#vol-tooltip`
- `#transport` — play/pause, prev, next, power (`#btn-power`), mute (`#btn-mute`)
- `#presets-grid` — 6 preset buttons rendered from poll state

**Settings tab** — four collapsible panels using `toggleSection(bodyId, chevronId)`:
1. Discover Speakers — scan button
2. Speaker Details — device info table, rename, bass slider (`#bass-row`, shown when `bassAvailable`)
3. Preset Backup — backup-all button
4. Alexa Integration — how-to text + nested Commission Matter Bridge panel (`toggleQR()`)

**Key JS functions:**
- `setActive(host)` — switch active speaker; triggers poll and reloads open Settings sections
- `pollNow()` / `schedPoll()` — 3s active-speaker poll loop
- `bgPollAll()` — 12s background poll of all non-active speakers (updates chip playing state)
- `applyState(d)` — applies poll response to UI (track, volume, playing state, badges, presets)
- `loadSpeakerInfo()` — fetches `/api/device-info` and renders Settings > Speaker Details
- `loadBass()` — fetches `/api/bass` and shows/hides bass slider in Speaker Details
- `loadAlexaQR()` — fetches `/api/matter/qr` and updates commissioning status badge
- `toggleSection(bodyId, chevronId)` — expand/collapse a Settings panel; triggers lazy load
- `switchTab(name)` — switches visible page, saves to localStorage

## Custom internet radio presets

Edit the `LOCAL_INTERNET_RADIO` list near the top of `soundtouch_controller.py` to add hardcoded stream presets. Dynamic custom stations are saved via the web UI and stored in `data/stations/`.

## Logs

- Controller: `soundtouch.log` in project root (rotates at 1 MB, keeps 5 files). Console shows INFO+; file shows DEBUG+.
- Matter bridge: `matter_bridge/matter_bridge.log` (appended, not rotated) and systemd journal.

## Dependencies

Python: `requests`, `zeroconf`, `Pillow` (optional — album art)  
Node.js: `@project-chip/matter-node.js` (ESM, `"type": "module"` in package.json)

No test suite. No linter configuration.
