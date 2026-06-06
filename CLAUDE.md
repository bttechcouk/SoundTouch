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
bash install.sh   # Python deps, firewall ports (8888/tcp, 1900/udp, 5540/udp),
                  # Matter bridge npm install, optional systemd services
```

The installer detects Node.js 20 LTS+ and runs `npm install` in `matter_bridge/`
automatically. If Node.js is missing it prints install instructions and continues
(the web controller works without it). To install Node.js manually:
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs
cd matter_bridge && npm install
```

## Service management

```bash
systemctl --user restart|stop|status soundtouch          # web controller
systemctl --user restart|stop|status soundtouch-matter   # Matter bridge
journalctl --user -u soundtouch -f                       # controller live logs
journalctl --user -u soundtouch-matter -f                # bridge live logs
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8888 | TCP | Web UI and DLNA redirect endpoints |
| 8090 | TCP | SoundTouch speaker API (outbound to speaker) |
| 8091 | TCP | UPnP AVTransport on speaker (outbound, used for UPNP-only speakers) |
| 1900 | UDP | SSDP multicast — DLNA server announcements |
| 5540 | UDP | Matter protocol (Alexa smart home) |

## Logs

- Controller: `soundtouch.log` in project root (rotates at 1 MB, keeps 5 files). Console shows INFO+; file shows DEBUG+.
- Matter bridge: `matter_bridge/matter_bridge.log` (appended, not rotated) and systemd journal.

## Dependencies

Python: declared in `requirements.txt`. Required: `requests`, `zeroconf`. Optional: `Pillow` (album art), `gTTS` (TTS announcements). Install: `pip3 install -r requirements.txt`.  
Node.js: `@project-chip/matter-node.js` (ESM, `"type": "module"` in package.json)

No test suite. No linter configuration.

---

## Architecture

Two processes, both must run together for Alexa integration to work.

### `soundtouch_controller.py` (~2500 lines)

Python app — all backend classes in one file. The web UI lives separately in `web/` (see below).

**`SoundTouchDevice` (line 209)** — HTTP REST client for a speaker's port-8090 XML API. `_get()` / `_post()` are the low-level transport; `_key()` sends remote-key presses.

Key methods:
- `state()` — aggregates volume, now-playing, presets, and zone role into a dict for the web UI. Includes `_upnp_location` (ContentItem location from now_playing) which the `/api/state` handler uses to overlay station metadata for UPNP speakers.
- `has_local_internet_radio()` (line 368) — checks `/sources` for `LOCAL_INTERNET_RADIO`. Returns `True` on error (fail-safe for normal speakers). Used to detect "Kitchen-like" speakers provisioned after Bose disabled internet radio.
- `play_via_avt(stream_url)` (line 511) — plays a stream via UPnP AVTransport SOAP on port 8091. Used for speakers without `LOCAL_INTERNET_RADIO`. URL must be HTTP (not HTTPS); the speaker follows 302 redirects.
- `get_zone()` / `set_zone()` / `remove_zone()` — multi-room zone management
- `get_bass_capabilities()` / `get_bass()` / `set_bass()` — bass control
- `detail_info()` — device details from `/info`
- `set_name()` — rename via `POST /name`

**`PresetStore` (line 624)** — Reads/writes preset backups as JSON to `data/presets/<ip>.json` and custom station definitions to `data/stations/<id>.json`. `station_descriptor()` (line 706) returns the JSON the speaker fetches to resolve a `LOCAL_INTERNET_RADIO` stream URL.

**`DLNAServer` (line 727)** — Embedded UPnP MediaServer for speakers that lack `LOCAL_INTERNET_RADIO`. Runs SSDP announcements on UDP 1900 and serves:
- `GET /dlna/device.xml` — UPnP device description
- `GET /dlna/cd.xml` — ContentDirectory SCPD
- `POST /dlna/cd/control` — SOAP Browse handler (returns DIDL-Lite for all custom stations)
- `GET /dlna/stream/<id>` — HTTP 302 redirect to the real HTTPS stream URL

`stream_url(station_id)` (line 760) returns the HTTP redirect URL used as the ContentItem location in UPNP presets. UUID is persisted to `data/dlna_uuid.txt`.

**`SceneStore` (line 1040)** — Persists named scenes as JSON to `data/scenes/<id>.json`.

**`AlarmStore` / `AlarmScheduler` (lines 1074, 1113)** — Persist alarm definitions to `data/alarms.json`; background thread fires alarms at scheduled times.

**Discovery (line 1173)** — `discover_mdns()` uses zeroconf for `_soundtouch._tcp.local.`; `discover_subnet_scan()` concurrently probes all 254 hosts on the local /24. Both run in parallel via `discover_all()`.

**Web UI assets (`web/` directory)** — The single-page web UI lives in `web/` and is served from disk (cached) by `web_asset()` / `Handler._web()`: `web/index.html` (markup), `web/app.css` (styles), `web/app.js` (logic), plus `web/wall.html` (kiosk panel) and `web/sw.js` (service worker). Tabs: Player, Presets, Groups, Settings. Editing the UI no longer means editing a Python string.

**`Handler` (line 4257)** — `BaseHTTPRequestHandler` serving the web UI and REST API.

Key API endpoints:
- `GET /api/state?host=` — full speaker state; overlays station name/art from `PresetStore` when `source=UPNP` and location matches `/dlna/stream/`
- `GET /api/cmd?host=&action=&value=` — actions: `playpause`, `next`, `prev`, `power`, `mute`, `preset1`–`6`, `volume`, `bass`. Preset actions check if the preset is `UPNP` source and call `play_via_avt()` instead of sending a key press.
- `GET /api/speakers` — discovered speakers list
- `GET /api/scan` — trigger rediscovery
- `GET /api/bass?host=` / `GET /api/device-info?host=` / `GET /api/rename?host=&name=`
- `GET /api/presets/backup?host=` / `GET /api/presets/restore?host=` — restore converts `LOCAL_INTERNET_RADIO` presets to `UPNP` for Kitchen-like speakers
- `GET /api/presets/backup-all` / `GET /api/presets/health?host=`
- `GET /api/group?host=` / `POST /api/group/create|remove|party|dissolve-all|join`
- `GET /api/stations` / `POST /api/stations/add|delete` / `GET /api/stations/play?host=&id=` / `POST /api/stations/set-preset` / `GET /api/stations/stream-search`
- `GET /api/scenes` / `POST /api/scenes|scenes/delete|scenes/activate`
- `GET /api/alarms` / `POST /api/alarms|alarms/delete|alarms/toggle`
- `POST /api/tts/announce` / `GET /api/tts/status`
- `GET /api/volume/all` / `GET /api/sources?host=` / `POST /api/select`
- `GET /api/matter/qr`

**`AppState` (line 5074)** — Singleton holding the device list, `PresetStore`, `SceneStore`, `AlarmStore`, `AlarmScheduler`, and `DLNAServer`. On init, starts the DLNA server and the `_upnp_autoplay_loop` daemon thread.

`_upnp_autoplay_loop` (line 5098) — Polls Kitchen-like speakers every 2s. Fires `play_via_avt()` when it detects either `source=UPNP`+stopped (ContentItem has our DLNA URL) or a fresh transition into `source=INVALID_SOURCE` (the more common case when a physical preset button is pressed). Uses `prev_source`, `last_upnp_loc`, and `last_fired` dicts for debounce.

**`main()` (line 5225)** — Parses `--port`, `--ip`, `--daemon`, runs `_check_network()`, starts `AppState.scan()`, launches `ThreadingHTTPServer`.

### UPNP-only speakers (Kitchen-like)

Some speakers (provisioned after Bose disabled internet radio) have no `LOCAL_INTERNET_RADIO` source. The controller handles these transparently:

- **Playing a custom station** → `play_via_avt()` via DLNA redirect URL instead of `select_content("LOCAL_INTERNET_RADIO", ...)`
- **Setting a preset** → stored as `source=UPNP` with `location=http://<host>:8888/dlna/stream/<id>` instead of `LOCAL_INTERNET_RADIO`
- **Restoring presets** → `LOCAL_INTERNET_RADIO` entries in the backup are converted to `UPNP` on restore
- **Web UI preset buttons** → `/api/cmd` detects `UPNP` source presets and calls `play_via_avt()` instead of sending a key press
- **Physical preset buttons** → handled by `_upnp_autoplay_loop` (see known issue #44 — first press may not work reliably)
- **`/api/state` station metadata** → name and art injected from station store when `_upnp_location` matches `/dlna/stream/`

### `matter_bridge/matter_bridge.js`

Node.js process using `@project-chip/matter-node.js` v0.7.5. Registers each speaker's preset slots (1–6), power toggle, and volume control as Matter devices inside an Aggregator bridge. Calls `/api/cmd` on the Python controller when Alexa sends a command.

Key config constants at the top:
- `LABEL_FORMAT` / `POWER_FORMAT` / `VOLUME_FORMAT` — device name templates (`{preset}`, `{room}` tokens)
- `PASSCODE` / `DISCRIMINATOR` — Matter commissioning credentials (fixed; change requires recommissioning)
- `BRIDGE_API_PORT = 8889` — local HTTP server serving `/qr` for the web UI QR panel

Commissioning state is persisted to `matter_bridge/data/matter/bridge.json`. Delete this file and restart to force recommissioning.

---

## Web UI (`web/` directory — `index.html`, `app.css`, `app.js`)

**Tabs:** Player, Presets, Groups, Settings. Active tab is saved to `localStorage`.

**Key JS functions:**
- `setActive(host)` — switch active speaker; triggers poll and reloads any open Settings sections
- `pollNow()` / `schedPoll()` — 3s active-speaker poll loop
- `bgPollAll()` — 12s background poll of all non-active speakers
- `applyState(d)` — applies `/api/state` response to the Player UI
- `toggleSection(bodyId, chevronId)` — expand/collapse a collapsible panel; triggers lazy-load of section data on first open
- `switchTab(name)` — switches visible page

Settings sections (all collapsible via `toggleSection`): Discover Speakers, Speaker Details (with bass slider), Radio Presets (UPNP preset list with art + play, `loadUpnpStations()`), Preset Backup, Alarms, Scenes, Announce (TTS), Alexa Integration (Matter QR). When adding a new Settings section, register its lazy-load function in both `toggleSection()` and `setActive()`.

## Data directory

```
data/
  presets/<ip>.json     # per-speaker preset backups
  stations/<id>.json    # custom station definitions (name, stream_url, art_url)
  scenes/<id>.json      # named multi-speaker scenes
  alarms.json           # alarm definitions
  dlna_uuid.txt         # persistent UUID for the embedded DLNA server
```

## Custom internet radio presets

Edit the `LOCAL_INTERNET_RADIO` list near the top of `soundtouch_controller.py` to add hardcoded stream presets. Dynamic custom stations are added via the web UI (Presets tab → Custom Radio Stations) and stored in `data/stations/`.
