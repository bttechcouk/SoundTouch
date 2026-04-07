# SoundTouch Controller

A lightweight web-based controller for **Bose SoundTouch** speakers on your local Wi-Fi network. Runs a local HTTP server so you can control your speaker from any device with a browser — desktop, iPhone, Android, or tablet — with no cloud dependency.

---

## Features

**Playback & Controls**
- Play / Pause / Next / Previous
- Volume slider and mute toggle
- Power toggle
- Now-playing display with album art
- 6 Preset buttons with names loaded directly from the speaker

**Speaker Discovery**
- Auto-discovers SoundTouch speakers via mDNS and subnet scan
- Manual IP entry fallback if auto-discovery doesn't work
- Supports multiple speakers on the same network

**Preset Backup & Restore**
- Backs up your presets locally as JSON files — they survive a Bose cloud shutdown
- Restore presets back to the speaker at any time
- Add custom internet radio stream presets via `LOCAL_INTERNET_RADIO`

**Alexa Integration**
- Emulates a Philips Hue bridge so Alexa can discover the speaker as a smart home device
- SSDP listener on UDP 1900 for Alexa device discovery
- Hue bridge HTTP server on TCP 8082 for Alexa smart home control

**Daemon / Service Mode**
- Run in the background with `--daemon` flag (survives SSH disconnect)
- Optional systemd user service — auto-starts on login and restarts on crash

---

## Quick Start

```bash
bash install.sh
python3 soundtouch_controller.py
```

Then open `http://<your-machine-ip>:8080` in any browser on the same Wi-Fi network.

---

## Usage

```bash
# Default — web UI on port 8080, auto-discovers speaker
python3 soundtouch_controller.py

# Custom port
python3 soundtouch_controller.py --port 9090

# Connect directly to a specific speaker IP
python3 soundtouch_controller.py --ip 192.168.1.50

# Run as a background daemon (survives SSH disconnect)
python3 soundtouch_controller.py --daemon
```

---

## iPhone / Mobile Access

1. Make sure your iPhone is on the **same Wi-Fi network** as the machine running the controller.
2. Launch the controller — note the URL shown in the terminal (e.g. `http://192.168.1.42:8080`).
3. Open that URL in **Safari** on your iPhone.
4. Tap the Share button → **Add to Home Screen** to get an app-like icon.

---

## Requirements

- Ubuntu 20.04 or later
- Python 3.8+
- Same Wi-Fi network as the SoundTouch speaker

Python packages (installed automatically by `install.sh`):
- `requests`
- `zeroconf` (for mDNS auto-discovery)
- `Pillow` (for album art — optional but recommended)
- `python3-tk` (tkinter, installed via apt)

---

## Installation

The installer handles everything in one step:

```bash
bash install.sh
```

It will:
1. Check for Python 3 and install pip if needed
2. Install required Python packages (via apt and/or pip)
3. Open the necessary firewall ports if ufw is active (8080/tcp, 1900/udp, 8082/tcp)
4. Optionally install a systemd user service that auto-starts on login

**Ports used:**

| Port | Protocol | Purpose |
|------|----------|---------|
| 8080 | TCP | Web UI |
| 1900 | UDP | SSDP / Alexa discovery |
| 8082 | TCP | Alexa Hue bridge emulator |

---

## Run as a Service

If you chose to install the systemd service during `install.sh`, the controller will start automatically when you log in. Useful commands:

```bash
systemctl --user status  soundtouch   # check status
systemctl --user stop    soundtouch   # stop
systemctl --user start   soundtouch   # start
systemctl --user restart soundtouch   # restart
journalctl --user -u soundtouch -f    # live logs
```

To install the service manually at any time, re-run `install.sh`.

---

## Preset Backup & Custom Stations

Presets are automatically backed up to `data/presets/<speaker-ip>.json` whenever the controller connects to a speaker. To add custom internet radio stations, edit the `LOCAL_INTERNET_RADIO` list near the top of `soundtouch_controller.py`.

---

## Alexa Setup

The controller emulates a Philips Hue bridge, letting Alexa discover your SoundTouch speaker as a smart home device.

1. Ensure ports 1900/udp and 8082/tcp are open (the installer does this automatically).
2. In the Alexa app, go to **Devices → Add Device → Other** and run discovery.
3. Alexa should find the speaker and allow voice control.

If avahi-daemon is running on the same machine, it also binds to port 1900. Alexa discovery usually still works since both processes receive multicast packets, but if it doesn't try stopping avahi: `sudo systemctl stop avahi-daemon`.

---

## Troubleshooting

**Speaker not found automatically**
- Click Connect and enter the speaker's IP address manually.
- Find the IP in your router's device list or via the Bose SoundTouch app → Settings → About.
- Make sure both devices are on the same subnet.

**iPhone can't reach the web interface**
- Open the firewall: `sudo ufw allow 8080/tcp`
- Confirm your iPhone and the host machine are on the same Wi-Fi network.

**No album art**
- Install Pillow: `pip3 install Pillow --break-system-packages`

**Alexa can't discover the speaker**
- Confirm ports 1900/udp and 8082/tcp are open.
- Try stopping avahi-daemon: `sudo systemctl stop avahi-daemon`
- Re-run discovery in the Alexa app.

**Logs**
- Logs are written to `soundtouch.log` (rotates at 1 MB, keeps 5 files).
- For live output: `journalctl --user -u soundtouch -f` (if running as a service).
