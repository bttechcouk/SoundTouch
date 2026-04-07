# SoundTouch Controller

A lightweight web-based controller for **Bose SoundTouch** speakers on your local Wi-Fi network. Runs a local HTTP server so you can control your speakers from any device with a browser — desktop, iPhone, Android, or tablet — with no cloud dependency.

A companion **Matter bridge** exposes every speaker preset and power toggle as a Matter On/Off device, enabling full Alexa voice control with no account linking and no cloud.

---

## Features

**Playback & Controls**
- Play / Pause / Next / Previous
- Volume slider
- Power toggle
- Now-playing display with album art
- 6 Preset buttons with names loaded directly from the speaker

**Speaker Discovery**
- Auto-discovers SoundTouch speakers via mDNS and subnet scan
- Supports multiple speakers simultaneously
- Manual IP entry fallback if auto-discovery fails

**Preset Backup & Custom Stations**
- Backs up presets locally as JSON — survives a Bose cloud shutdown
- Restore backed-up presets back to the speaker at any time
- Add custom internet radio streams via the web UI or `LOCAL_INTERNET_RADIO` in the source

**Multi-Room Zones**
- Group speakers together to play the same audio in sync
- Set and dissolve zones from the web UI

**Alexa Integration (Matter)**
- Companion Matter bridge exposes each preset slot and power toggle as a Matter On/Off smart home device
- Discovered and controlled locally — no cloud, no account linking
- Supports Amazon Echo (5th gen Dot and later with Matter firmware)
- Say `"Alexa, turn on KISS in Bedroom"` or use Alexa Routines for fully custom phrases like `"Alexa, play KISS in the bedroom"`

**Service Mode**
- Systemd user services for both the controller and the Matter bridge
- Auto-start on login, auto-restart on crash

---

## Architecture

```
Browser / iPhone
      │  HTTP :8888
      ▼
soundtouch_controller.py   ←──────── REST API ──────────→  Bose SoundTouch speakers
      │                                                        (port 8090, XML)
      │  HTTP :8888/api/*
      ▼
matter_bridge/matter_bridge.js
      │  Matter (UDP :5540, mDNS)
      ▼
Amazon Echo (Alexa)
```

| Component | Language | Entry point |
|-----------|----------|-------------|
| Web controller | Python 3 | `soundtouch_controller.py` |
| Matter bridge | Node.js (matter.js) | `matter_bridge/matter_bridge.js` |

---

## Quick Start

```bash
bash install.sh
```

Then open `http://<your-machine-ip>:8888` in any browser on the same Wi-Fi network.

---

## Usage

```bash
# Default — web UI on port 8888, auto-discovers speakers
python3 soundtouch_controller.py

# Custom port
python3 soundtouch_controller.py --port 9090

# Connect directly to a specific speaker IP (skips discovery)
python3 soundtouch_controller.py --ip 192.168.1.50

# Run as a background daemon (survives SSH disconnect)
python3 soundtouch_controller.py --daemon
```

The Matter bridge is started separately:

```bash
cd matter_bridge
node matter_bridge.js
```

Or manage both as systemd user services (see below).

---

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8888 | TCP | Web UI |
| 8090 | TCP | SoundTouch speaker API (outbound) |
| 5540 | UDP | Matter protocol (Alexa smart home) |

---

## Installation

```bash
bash install.sh
```

The installer will:
1. Check for Python 3 and install pip if needed
2. Install required Python packages (`requests`, `zeroconf`, `Pillow`)
3. Check for Node.js 20+ (required for the Matter bridge)
4. Run `npm install` inside `matter_bridge/`
5. Open firewall ports if ufw is active
6. Optionally install systemd user services for both components

### Node.js requirement

The Matter bridge requires **Node.js 20 LTS or later**. If it isn't installed:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

---

## Systemd Services

Both components run as systemd **user** services, starting automatically on login.

```bash
# Web controller
systemctl --user status  soundtouch
systemctl --user restart soundtouch
journalctl --user -u soundtouch -f

# Matter bridge
systemctl --user status  soundtouch-matter
systemctl --user restart soundtouch-matter
journalctl --user -u soundtouch-matter -f
```

The Matter bridge service is configured to start after the web controller, so it can query the speaker list on startup.

---

## Alexa / Matter Integration

### How it works

The `matter_bridge/` directory contains a Node.js process that:

1. On startup, queries the web controller's `/api/speakers` endpoint to get the current speaker list
2. For each speaker, fetches `/api/state` to read preset names
3. Registers each preset slot (1–6) and a power toggle per speaker as a **Matter On/Off device** inside a Matter Aggregator (bridge)
4. Announces itself via mDNS on UDP port 5540 so Alexa can discover it

When Alexa sends an on command, the bridge calls the web controller's `/api/cmd` endpoint to trigger the preset or power toggle on the speaker.

### First-time setup

1. Make sure the web controller is running and has discovered your speakers.
2. Start the Matter bridge — it prints a QR code and manual pairing code to the log:

   ```bash
   journalctl --user -u soundtouch-matter --no-pager | grep -A 15 "pairing code"
   ```

3. In the Alexa app: **Devices → + → Add Device → Other → Matter** and scan the QR code.
4. Alexa will discover all devices (up to 28 for 4 speakers).

### Voice commands

Devices are named `"<Preset> in <Room>"` and `"<Room> power"`, so you can say:

```
"Alexa, turn on KISS in Bedroom"
"Alexa, turn on BBC Radio 1 in Kitchen"
"Alexa, turn on Dining Room power"
```

### Custom phrases with Alexa Routines

To use fully custom trigger phrases (e.g. `"Alexa, play KISS in the bedroom"`):

1. Alexa app → **More → Routines → +**
2. **When:** Voice → type your custom phrase
3. **Then:** Smart Home → Control device → select the preset device → On

### Device name format

The naming template is configurable at the top of `matter_bridge/matter_bridge.js`:

```js
const LABEL_FORMAT = "{preset} in {room}";   // preset devices
const POWER_FORMAT = "{room} power";          // power toggle devices
```

### Recommissioning

If you add speakers or change preset names, clear the stored Matter state and recommission:

```bash
systemctl --user stop soundtouch-matter
rm matter_bridge/data/matter/bridge.json
systemctl --user start soundtouch-matter
# Then re-add the device in the Alexa app
```

---

## iPhone / Mobile Access

1. Ensure your device is on the **same Wi-Fi network** as the server.
2. Open `http://<server-ip>:8888` in Safari.
3. Tap Share → **Add to Home Screen** for an app-like shortcut.

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Ubuntu | 20.04+ |
| Python | 3.8+ |
| Node.js | 20 LTS+ |

Python packages: `requests`, `zeroconf`, `Pillow` (optional, for album art)  
Node.js packages: `@project-chip/matter-node.js` (installed via `npm install`)

---

## Troubleshooting

**Speaker not found automatically**
- Click Scan in the web UI header, or enter the IP manually.
- Find the IP via your router's device list or the Bose SoundTouch app → Settings → About.

**Web UI not reachable from phone**
- Open the firewall: `sudo ufw allow 8888/tcp`
- Confirm both devices are on the same Wi-Fi network.

**No album art**
- Install Pillow: `pip3 install Pillow --break-system-packages`

**Alexa can't find Matter devices**
- Check the bridge is running: `systemctl --user status soundtouch-matter`
- Check Matter port is open: `sudo ufw allow 5540/udp`
- Check bridge logs: `journalctl --user -u soundtouch-matter -f`
- Ensure the Echo and server are on the same subnet.

**Matter bridge starts with no devices**
- The bridge queries the web controller on startup. Make sure the controller has finished its network scan before the bridge starts (the bridge retries for up to 2 minutes automatically).

**Logs**
- Controller: `soundtouch.log` (rotates at 1 MB, keeps 5 files)
- Matter bridge: `matter_bridge/matter_bridge.log` + `journalctl --user -u soundtouch-matter`
