#!/usr/bin/env python3
"""
SoundTouch Controller
Web-based controller for Bose SoundTouch speakers.
Runs a local web server; open http://<this-machine-ip>:8888 in any browser.

Features:
  - Auto-discovers all SoundTouch speakers on the network
  - Full playback / volume / preset controls
  - Local preset backup & restore  (survives Bose cloud shutdown)
  - Custom internet-radio stream presets via LOCAL_INTERNET_RADIO
  - Built-in station server so the speaker can fetch stream metadata

Usage:
    python3 soundtouch_controller.py
    python3 soundtouch_controller.py --port 9090
    python3 soundtouch_controller.py --ip 192.168.1.50
"""

import argparse
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import struct
import sys
import threading
import time
import uuid as _uuid
import datetime as _dt
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape, quoteattr
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse, urlencode, quote as urlquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not found.  Run:  pip3 install requests")
    sys.exit(1)

try:
    from gtts import gTTS as _gTTS
    _TTS_AVAILABLE = True
except ImportError:
    _TTS_AVAILABLE = False

# In-memory store for TTS audio files: {audio_id: bytes}
_tts_cache: dict = {}
# Debounce duplicate requests: last (text, hosts_key) → timestamp
_tts_last: dict = {}
_tts_lock = threading.Lock()

WEB_PORT      = 8888
DATA_DIR      = pathlib.Path(__file__).parent / "data"
PRESETS_DIR   = DATA_DIR / "presets"
STATIONS_DIR  = DATA_DIR / "stations"
LOG_FILE      = pathlib.Path(__file__).parent / "soundtouch.log"
SCENES_DIR    = DATA_DIR / "scenes"
ALARMS_FILE   = DATA_DIR / "alarms.json"
AUDIO_MODE_FILE = DATA_DIR / "audio_mode.json"
MAINTENANCE_FILE = DATA_DIR / "maintenance.json"
# A soundbar that wakes from standby into a music source and hasn't started
# playing after this many seconds was woken by the TV (CEC) → switch to TV.
TV_WAKE_GRACE   = 8

# Sources that route through the Bose cloud — will break on 6 May 2026
CLOUD_SOURCES = {
    "TUNEIN":          ("TuneIn Radio",     "Replace with a Custom Radio Station using a direct stream URL"),
    "AMAZON":          ("Amazon Music",     "Amazon Music presets require the Bose cloud — replace with Bluetooth or a local stream"),
    "DEEZER":          ("Deezer",           "Deezer presets require the Bose cloud — replace with a local stream"),
    "PANDORA":         ("Pandora",          "Pandora presets require the Bose cloud — replace with a local stream"),
    "NAPSTER":         ("Napster",          "Napster presets require the Bose cloud — replace with a local stream"),
    "IHEART":          ("iHeartRadio",      "Replace with a Custom Radio Station using the station's direct stream URL"),
    "TIDAL":           ("Tidal",            "Tidal presets require the Bose cloud — replace with a local stream"),
    "SIRIUSXM":        ("SiriusXM",         "SiriusXM presets require the Bose cloud — replace with a local stream"),
    "SOUNDCLOUD":      ("SoundCloud",       "SoundCloud presets require the Bose cloud — replace with a local stream"),
    "INTERNET_RADIO":  ("Internet Radio",   "Bose Internet Radio presets are cloud-routed — replace with a Custom Radio Station"),
    "SPOTIFY":         ("Spotify",          "Spotify presets are recalled via the Bose cloud — replace with Bluetooth or Spotify Connect"),
}
# Sources that are fully local and will continue to work after shutdown.
# UPNP presets point at our own DLNA stream redirect (a local custom station),
# so they are cloud-independent and must pass the preset health check.
SAFE_SOURCES = {"LOCAL_INTERNET_RADIO", "BLUETOOTH", "AUX", "AIRPLAY", "TV",
                "STORED_MUSIC", "PRODUCT", "STANDBY", "UPNP"}

# ── App icon SVG (served at /icon.svg) ──────────────────────────────────────
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect width="100" height="100" rx="22" fill="#0b0c11"/>'
    '<circle cx="50" cy="50" r="36" fill="#2277ee" opacity=".9"/>'
    '<text x="50" y="67" text-anchor="middle" '
    'font-family="system-ui,sans-serif" font-size="48" fill="white">&#9836;</text>'
    '</svg>'
)

# ── PNG icon generator (for PWA manifest + apple-touch-icon) ────────────────
_icon_cache: dict = {}

def _make_icon_png(size: int) -> bytes | None:
    """Render a SoundTouch PNG icon using Pillow. Returns bytes or None."""
    if size in _icon_cache:
        return _icon_cache[size]
    try:
        import io
        from PIL import Image, ImageDraw
        s = size
        img  = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d    = ImageDraw.Draw(img)
        # Dark rounded-rect background
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 5,
                             fill=(11, 12, 17, 255))
        # Blue filled circle
        pad = s // 10
        d.ellipse([pad, pad, s - pad - 1, s - pad - 1], fill=(34, 119, 238, 255))
        # White speaker body (rectangle)
        cx, cy   = s // 2, s // 2
        bw, bh   = s // 10, s // 5
        bx       = cx - s // 8 - bw
        d.rectangle([bx, cy - bh, bx + bw, cy + bh], fill=(255, 255, 255, 255))
        # White speaker cone (trapezoid pointing right)
        cone = [
            (bx + bw, cy - bh),
            (cx + s // 8, cy - s // 3),
            (cx + s // 8, cy + s // 3),
            (bx + bw, cy + bh),
        ]
        d.polygon(cone, fill=(255, 255, 255, 255))
        # Sound arcs (two white arcs to the right of the cone)
        lw = max(2, s // 40)
        for i, r in enumerate([s // 6, s // 4]):
            ax = cx + s // 8
            d.arc([ax, cy - r, ax + 2 * r, cy + r],
                  start=-50, end=50,
                  fill=(255, 255, 255, 200 - i * 40), width=lw)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        _icon_cache[size] = data
        return data
    except Exception as e:
        log.debug(f"[ICON] PNG generation failed ({size}px): {e}")
        _icon_cache[size] = None
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_logger():
    logger = logging.getLogger("soundtouch")
    if logger.handlers:
        return logger          # already configured (e.g. reloaded module)
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── rotating file handler — DEBUG and above (1 MB × 5 files) ────────────
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # ── console — INFO and above ──────────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _setup_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# SoundTouch device API
# ═══════════════════════════════════════════════════════════════════════════════

_PRESET_TTL = 30.0  # seconds before preset cache expires

class SoundTouchDevice:
    def __init__(self, host, port=8090):
        self.host  = host
        self.port  = port
        self.url   = f"http://{host}:{port}"
        self.name      = host
        self.model     = ""
        self.mac       = ""
        self.device_id = ""
        self.has_backup = False          # cached by AppState; avoids disk reads on /api/speakers
        self._session  = requests.Session()  # reuse TCP connections across requests
        self._presets_cache = None       # cached preset list
        self._presets_ts    = 0.0        # monotonic time of last preset fetch
        self._zone_cache    = None       # cached zone info
        self._zone_ts       = 0.0        # monotonic time of last zone fetch
        self._dsp_supported = None       # dialogue-mode support, probed lazily
        self._dsp_checked   = None       # monotonic time of last failed probe
        self._tv_input      = None       # soundbar with a PRODUCT/TV source?
        self._tone_supported = None      # /audioproducttonecontrols (soundbars), probed lazily
        self._capabilities  = None       # capability names from /capabilities, read once
        self._has_clock     = None       # front-panel clock (ST20)? read once

    # ── low-level ─────────────────────────────────────────────────────────────
    def _get(self, path, timeout=4):
        url = f"{self.url}{path}"
        log.debug(f"[SPK GET ] {url}")
        try:
            r = self._session.get(url, timeout=timeout)
            r.raise_for_status()
            snippet = r.text[:400].replace("\n", " ")
            log.debug(f"[SPK GET ] ← {r.status_code}  {snippet}")
            return ET.fromstring(r.content)   # bytes: requests would guess Latin-1 ("PeÃ±a")
        except Exception as e:
            log.warning(f"[SPK GET ] {url} → ERROR: {e}")
            return None

    def _post(self, path, body, timeout=4):
        url = f"{self.url}{path}"
        log.debug(f"[SPK POST] {url}  body={body[:300]}")
        try:
            r = self._session.post(url, data=body,
                                   headers={"Content-Type": "application/xml"},
                                   timeout=timeout)
            log.debug(f"[SPK POST] ← {r.status_code}  {r.text[:200].replace(chr(10),' ')}")
            if r.status_code != 200:
                log.warning(f"[SPK POST] {url} non-200 → {r.status_code}  {r.text[:300]}")
            return r.status_code == 200
        except Exception as e:
            log.warning(f"[SPK POST] {url} → ERROR: {e}")
            return False

    def _key(self, k):
        self._post("/key", f'<key state="press"   sender="Gabbo">{k}</key>')
        self._post("/key", f'<key state="release" sender="Gabbo">{k}</key>')

    # ── info ──────────────────────────────────────────────────────────────────
    def fetch_info(self):
        xml = self._get("/info")
        if xml is None:
            return False
        for tag, attr in [("name","name"),("type","model"),("macAddress","mac")]:
            el = xml.find(tag)
            if el is not None:
                setattr(self, attr, el.text or "")
        # deviceID is an attribute on the root <info> element, not a child tag
        self.device_id = xml.get("deviceID", "")
        if not self.name:
            self.name = self.host
        return True

    def detail_info(self):
        """Return network/firmware details for the Settings tab."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_info = ex.submit(self._get, "/info")
            f_net  = ex.submit(self._get, "/netStats")
        xml = f_info.result()
        nsx = f_net.result()

        if xml is None:
            return {"name": self.name, "model": self.model, "ip": self.host}
        result = {
            "name":      xml.findtext("name") or self.name,
            "model":     xml.findtext("type") or self.model,
            "device_id": xml.get("deviceID", ""),
            "firmware":  "",
            "serial":    "",
            "ip":        self.host,
            "mac":       "",
            "country":   xml.findtext("countryCode") or "",
            "region":    xml.findtext("regionCode") or "",
            "spotify_connect": (xml.findtext("variant") or "").lower() == "spotty",
            "wifi_ssid":   "",
            "wifi_signal": "",
            "wifi_band":   "",
        }
        for comp in xml.findall("components/component"):
            cat = comp.findtext("componentCategory", "")
            if cat == "SCM":
                fw = comp.findtext("softwareVersion", "")
                result["firmware"] = fw.split()[0] if fw else ""
                result["serial"]   = comp.findtext("serialNumber", "")
            elif cat == "PackagedProduct" and not result["serial"]:
                result["serial"] = comp.findtext("serialNumber", "")
        for ni in xml.findall("networkInfo"):
            if ni.get("type") == "SCM":
                result["ip"]  = ni.findtext("ipAddress") or self.host
                result["mac"] = ni.findtext("macAddress") or ""
                break
        # Network stats
        if nsx is not None:
            iface = nsx.find(".//interface")
            if iface is not None:
                result["wifi_ssid"]   = iface.findtext("ssid") or ""
                result["wifi_signal"] = iface.findtext("rssi") or ""
                try:
                    khz = int(iface.findtext("frequencyKHz") or 0)
                    result["wifi_band"] = "5 GHz" if khz >= 3_000_000 else "2.4 GHz" if khz else ""
                except ValueError:
                    pass
        return result

    def get_bass_capabilities(self):
        xml = self._get("/bassCapabilities")
        if xml is not None and (xml.findtext("bassAvailable") or "false").lower() == "true":
            return {
                "available": True, "kind": "bass", "step": 1,
                "min":     int(xml.findtext("bassMin")     or "-9"),
                "max":     int(xml.findtext("bassMax")     or "0"),
                "default": int(xml.findtext("bassDefault") or "0"),
            }
        # Soundbars (SoundTouch 300) report bassAvailable=false and expose
        # bass through the tone controls instead (-100…100 in steps of 25).
        tone = self.get_audio_controls("tone")
        if tone and "bass" in tone:
            b = tone["bass"]
            return {"available": True, "kind": "tone", "min": b["min"], "max": b["max"],
                    "step": b["step"], "default": 0, "current": b["value"]}
        return {"available": False, "min": -9, "max": 0, "default": 0}

    def get_bass(self):
        xml = self._get("/bass")
        if xml is None: return 0
        return int(xml.findtext("actualbass") or "0")

    def set_bass(self, value):
        if self._tone_supported:
            return self.set_audio_control("tone", "bass", value)
        return self._post("/bass", f"<bass>{max(-9, min(9, int(value)))}</bass>")

    # ── tone & speaker-level controls — SoundTouch 300 / soundbars only ─────
    # /audioproducttonecontrols holds bass + treble, /audioproductlevelcontrols
    # the centre and rear-surround levels; both use -100…100 in steps.
    AUDIO_CONTROL_PATHS = {"tone":  "/audioproducttonecontrols",
                           "level": "/audioproductlevelcontrols"}

    def get_audio_controls(self, group):
        """{name: {value, min, max, step}} for a control group, or None on
        speakers without it. Only a soundbar's positive answer is cached —
        _get can't tell a 404 from the speaker being offline."""
        path = self.AUDIO_CONTROL_PATHS[group]
        caps = self.capabilities()
        if caps is not None and path.lstrip("/") not in caps:
            return None
        xml = self._get(path)
        if xml is None or xml.tag != path.lstrip("/"):
            return None
        if group == "tone": self._tone_supported = True
        out = {}
        for el in xml:
            try:
                out[el.tag] = {k: int(el.get(a)) for k, a in
                               (("value","value"),("min","minValue"),("max","maxValue"),("step","step"))}
            except (TypeError, ValueError):
                continue
        return out

    def set_audio_control(self, group, name, value):
        """Set one control, resending the rest of its group unchanged (the bar
        expects every control in the body). Clamped to the bar's range."""
        ctl = self.get_audio_controls(group)
        if not ctl or name not in ctl: return False
        c = ctl[name]
        c["value"] = max(c["min"], min(c["max"], int(value)))
        tag  = self.AUDIO_CONTROL_PATHS[group].lstrip("/")
        body = "".join(f'<{k} value="{v["value"]}"/>' for k, v in ctl.items())
        return self._post(self.AUDIO_CONTROL_PATHS[group], f"<{tag}>{body}</{tag}>")

    # ── soundbar settings: AV delay, auto-off, HDMI-CEC, attached speakers ──
    # Read only the endpoints the speaker lists in /capabilities — some
    # SoundTouch paths act on a plain GET (/lowPowerStandby drops the speaker
    # off the network), so never probe blind.
    AV_DELAY_MAX = 300   # ms; the bar accepts more but nothing sensible needs it

    def capabilities(self):
        """Set of capability names from /capabilities, or None if it couldn't
        be read (only a successful read is cached)."""
        if self._capabilities is not None:
            return self._capabilities
        xml = self._get("/capabilities")
        if xml is None or xml.tag != "capabilities":
            return None
        self._capabilities = {c.get("name") for c in xml.findall("capability")}
        return self._capabilities

    def has_clock(self):
        """True for speakers with a front-panel clock (ST20) — /capabilities
        carries <clockDisplay>true</clockDisplay>."""
        if self._has_clock is None:
            xml = self._get("/capabilities")
            if xml is None or xml.tag != "capabilities":
                return False
            self._has_clock = (xml.findtext("clockDisplay") or "").strip().lower() == "true"
        return self._has_clock

    def get_soundbar_settings(self):
        """Whatever subset this speaker supports, e.g. {av_delay: 0,
        auto_off: True, cec: True, rear: False, subwoofer: False}."""
        caps, out = self.capabilities() or set(), {}
        if "audiodspcontrols" in caps:
            x = self._get("/audiodspcontrols")
            if x is not None and x.get("videosyncaudiodelay") is not None:
                out["av_delay"] = int(x.get("videosyncaudiodelay"))
        if "systemtimeoutcontrol" in caps:
            x = self._get("/systemtimeoutcontrol")
            if x is not None and x.get("autopowerdown") is not None:
                out["auto_off"] = x.get("autopowerdown") == "true"
        if "productcechdmicontrol" in caps:
            x = self._get("/productcechdmicontrol")
            if x is not None and x.get("cecmode"):
                out["cec"] = x.get("cecmode") != "CEC_MODE_OFF"
        if "audiospeakerattributeandsetting" in caps:
            x = self._get("/audiospeakerattributeandsetting")
            if x is not None:
                for tag, key in (("rear", "rear"), ("subwoofer01", "subwoofer")):
                    el = x.find(tag)
                    if el is not None: out[key] = el.get("available") == "true"
        return out

    def set_soundbar_setting(self, name, value):
        """name: av_delay (ms, clamped 0…AV_DELAY_MAX) / auto_off / cec (bool-ish)."""
        caps = self.capabilities() or set()
        on = str(value).lower() in ("1", "true", "on", "yes")
        if name == "av_delay" and "audiodspcontrols" in caps:
            ms = max(0, min(self.AV_DELAY_MAX, int(value)))
            return self._post("/audiodspcontrols", f'<audiodspcontrols videosyncaudiodelay="{ms}"/>')
        if name == "auto_off" and "systemtimeoutcontrol" in caps:
            return self._post("/systemtimeoutcontrol",
                              f'<systemtimeoutcontrol autopowerdown="{str(on).lower()}"/>')
        if name == "cec" and "productcechdmicontrol" in caps:
            mode = "CEC_MODE_ON" if on else "CEC_MODE_OFF"
            return self._post("/productcechdmicontrol", f'<productcechdmicontrol cecmode="{mode}"/>')
        return False

    # ── audio DSP (dialogue mode) — SoundTouch 300 / soundbars only ──────────
    def supports_dialog_mode(self):
        """True if /audiodspcontrols lists AUDIO_MODE_DIALOG. Speakers without
        it (ST10/20/30) 404 the endpoint — that answer is cached for good. A
        network error is re-probed after 10 min so a speaker that was briefly
        offline doesn't lose the feature until restart."""
        if self._dsp_supported is not None:
            return self._dsp_supported
        now = time.monotonic()
        if self._dsp_checked is not None and now - self._dsp_checked < 600:
            return False
        self._dsp_checked = now
        try:
            r = self._session.get(f"{self.url}/audiodspcontrols", timeout=4)
        except Exception as e:
            log.debug(f"[DSP] {self.host} probe failed, will retry: {e}")
            return False
        try:
            xml = ET.fromstring(r.text) if r.status_code == 200 else None
        except ET.ParseError:
            xml = None
        self._dsp_supported = (xml is not None and xml.tag == "audiodspcontrols" and
                               "AUDIO_MODE_DIALOG" in xml.get("supportedaudiomodes", ""))
        log.info(f"[DSP] {self.host} dialogue mode supported: {self._dsp_supported}")
        return self._dsp_supported

    def get_audio_mode(self):
        """Return "dialog" / "normal", or None if dialogue mode is unsupported."""
        xml = self._get("/audiodspcontrols")
        if xml is None or xml.tag != "audiodspcontrols":
            return None
        return "dialog" if xml.get("audiomode") == "AUDIO_MODE_DIALOG" else "normal"

    def has_tv_input(self):
        """True for soundbars — speakers whose /sources include PRODUCT/TV.
        Cached once /sources answers; an unreachable speaker is re-checked."""
        if self._tv_input is None:
            sources = self.get_sources()
            if sources:
                self._tv_input = any(s["source"] == "PRODUCT" and s["sourceAccount"] == "TV"
                                     for s in sources)
        return bool(self._tv_input)

    def set_audio_mode(self, mode):
        am = "AUDIO_MODE_DIALOG" if mode == "dialog" else "AUDIO_MODE_NORMAL"
        return self._post("/audiodspcontrols", f'<audiodspcontrols audiomode="{am}"/>')

    def get_sources(self):
        xml = self._get("/sources")
        if xml is None: return []
        SKIP_ACCOUNTS = {"qplay1username","qplay2username","storedmusicusername",
                         "upnpusername","spotifyconnectusername","spotifyalexausername"}
        SKIP_SOURCES  = {"NOTIFICATION","STORED_MUSIC_MEDIA_RENDERER"}
        out = []
        for item in xml.findall("sourceItem"):
            src  = item.get("source","")
            acct = item.get("sourceAccount","")
            if src in SKIP_SOURCES or acct.lower() in SKIP_ACCOUNTS:
                continue
            out.append({
                "source":        src,
                "sourceAccount": acct,
                "status":        item.get("status",""),
                "name":          (item.text or src).strip(),
                "isLocal":       item.get("isLocal","false") == "true",
            })
        return out

    def select_source(self, source, account=""):
        body = f'<ContentItem source="{source}" sourceAccount="{account}"></ContentItem>'
        return self._post("/select", body)

    def has_local_internet_radio(self):
        try:
            sources = self.get_sources()
            # An empty list means we couldn't read /sources (unreachable speaker /
            # parse error) — a real speaker always reports BLUETOOTH/AUX/etc. Fail
            # safe to True so a temporarily-unreachable normal speaker isn't
            # misclassified as Kitchen-like and have its presets converted to UPNP.
            if not sources:
                return True
            return any(s["source"] == "LOCAL_INTERNET_RADIO" for s in sources)
        except Exception:
            return True  # assume available on error so existing speakers aren't broken

    def reboot(self):
        """Restart the speaker. There's no reboot in the port-8090 API, but every
        SoundTouch runs a diagnostic console (TAP) on TCP 17000 that takes
        `sys reboot`. Only that exact command is ever sent — the same console
        has `sys factorydefault`. Returns True once the speaker confirms.

        Fixes ST20s whose front-panel clock goes blank. The speaker may come
        back on a new DHCP address — see AppState.reboot_device()."""
        try:
            with socket.create_connection((self.host, 17000), timeout=5) as s:
                s.settimeout(3)
                s.recv(256)                      # "->" prompt
                s.sendall(b"sys reboot\r\n")
                reply = b""
                deadline = time.monotonic() + 3
                while b"Rebooting" not in reply and time.monotonic() < deadline:
                    chunk = s.recv(256)
                    if not chunk: break
                    reply += chunk
            ok = b"Rebooting" in reply
            log.info(f"[REBOOT] {self.host} ({self.name}) → {reply.decode(errors='replace').strip()!r}")
            return ok
        except Exception as e:
            log.warning(f"[REBOOT] {self.host} console error: {e}")
            return False

    def set_name(self, new_name):
        self._post("/name", f"<name>{new_name}</name>")

    # ── state snapshot ────────────────────────────────────────────────────────
    def state(self):
        d = dict(host=self.host, name=self.name, model=self.model,
                 volume=0, muted=False, source="", track="", artist="",
                 album="", art="", playing=False, presets=[])

        # Fetch all endpoints in parallel to minimise poll latency
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            f_vol  = ex.submit(self._get, "/volume")
            f_np   = ex.submit(self._get, "/now_playing")
            f_pre  = ex.submit(self.get_presets_detail)
            f_zone = ex.submit(self.get_zone)
            f_dsp  = ex.submit(lambda: self.get_audio_mode()
                               if self.supports_dialog_mode() else None)

        # dialogue mode — None means unsupported (UI hides the toggle)
        d["audio_mode"] = f_dsp.result()

        # volume
        vx = f_vol.result()
        if vx is not None:
            for t in ("actualvolume","targetvolume"):
                el = vx.find(t)
                if el is not None:
                    d["volume"] = int(el.text); break
            me = vx.find("muteenabled")
            if me is not None:
                d["muted"] = me.text.lower() == "true"
        # now playing
        np = f_np.result()
        if np is not None:
            d["source"]     = np.get("source","")
            d["source_account"] = np.get("sourceAccount","")
            play_status     = np.get("playStatus") or np.findtext("playStatus") or ""
            d["playing"]    = play_status in ("PLAY_STATE", "BUFFERING_STATE")
            d["playStatus"] = play_status
            for tag, key in [("track","track"),("artist","artist"),
                              ("album","album"),("stationName","track"),("art","art")]:
                el = np.find(tag)
                if el is not None and el.text:
                    d[key] = el.text
            ci = np.find("ContentItem")
            if ci is not None:
                d["_upnp_location"] = ci.get("location", "")
                d["presetable"] = ci.get("isPresetable") == "true" and bool(ci.get("location"))
                d["item_name"]  = ci.findtext("itemName") or ""
        # cloud source warning
        src_key = d.get("source", "").upper()
        if src_key in CLOUD_SOURCES:
            d["cloud_warning"] = CLOUD_SOURCES[src_key][1]
        else:
            d["cloud_warning"] = ""
        # presets
        d["presets"] = f_pre.result()
        # zone / group role
        try:
            z = f_zone.result()
            if z["is_master"]:
                d["group_role"] = "master"
                d["group_members"] = len(z["members"])
            elif z["is_slave"]:
                d["group_role"] = "member"
                d["group_master_ip"] = z["master_ip"]
            else:
                d["group_role"] = ""
        except Exception:
            d["group_role"] = ""
        return d

    def invalidate_preset_cache(self):
        """Force the next get_presets_detail() call to re-fetch from the speaker."""
        self._presets_ts = 0.0

    def get_presets_detail(self):
        """Return list of dicts with full preset info for backup / display.
        Result is cached for _PRESET_TTL seconds to avoid fetching on every poll."""
        now = time.monotonic()
        if self._presets_cache is not None and (now - self._presets_ts) < _PRESET_TTL:
            return self._presets_cache
        px = self._get("/presets")
        out = []
        if px is not None:
            for p in px.findall("preset"):
                ci = p.find("ContentItem")
                rec = {
                    "id":       p.get("id",""),
                    "name":     "",
                    "source":   "",
                    "type":     "",
                    "location": "",
                    "account":  "",
                    "art":      "",
                }
                if ci is not None:
                    rec["source"]   = ci.get("source","")
                    rec["type"]     = ci.get("type","")
                    rec["location"] = ci.get("location","")
                    rec["account"]  = ci.get("sourceAccount","")
                    nm = ci.find("itemName")
                    if nm is not None:
                        rec["name"] = nm.text or ""
                    ca = ci.find("containerArt")
                    if ca is not None:
                        rec["art"] = ca.text or ""
                out.append(rec)
        self._presets_cache = out
        self._presets_ts    = time.monotonic()
        return out

    # ── commands ──────────────────────────────────────────────────────────────
    def play_pause(self):  self._key("PLAY_PAUSE")
    def next_track(self):  self._key("NEXT_TRACK")
    def prev_track(self):  self._key("PREV_TRACK")
    def power(self):       self._key("POWER")
    def mute(self):        self._key("MUTE")
    def volume_up(self):   self._key("VOLUME_UP")
    def volume_down(self): self._key("VOLUME_DOWN")
    def preset(self, n):   self._key(f"PRESET_{n}")

    def play_preset(self, n):
        """Play preset n the way that actually starts audio. UPNP presets (our
        DLNA radio stations): a key press only loads the ContentItem and the
        speaker waits for an AVTransport Play, so send that directly. Spotify
        presets go through the controller's Spotify login (see SpotifyClient).
        Anything else: the preset key."""
        self.invalidate_preset_cache()   # presets can be changed on the speaker itself
        p = next((x for x in self.get_presets_detail() if x.get("id") == str(n)), None)
        if p and p.get("source") == "UPNP" and p.get("location", ""):
            return self.play_via_avt(p["location"])
        # Spotify presets only play while the speaker holds a Spotify login,
        # which a restart clears — so log it in and start via the Web API.
        uri = spotify_uri_from_location(p.get("location")) if p and p.get("source") == "SPOTIFY" else None
        if uri and SPOTIFY and SPOTIFY.store.get(p.get("account", "")):
            try:
                SPOTIFY.play(uri, [self], p["account"])
                return True
            except Exception as e:
                log.warning(f"[SPOTIFY] preset {n} on {self.host} via Web API failed ({e}); pressing the key")
        self.preset(n)
        return True

    def is_playing(self):
        np = self._get("/now_playing")
        if np is None: return False
        ps = np.get("playStatus") or np.findtext("playStatus") or ""
        return ps in ("PLAY_STATE", "BUFFERING_STATE")

    def set_volume(self, v):
        self._post("/volume", f"<volume>{max(0,min(100,int(v)))}</volume>")

    # ── preset management ─────────────────────────────────────────────────────
    def store_preset(self, preset_id, name, source, stype, location, account="", art=""):
        """Write a preset to the speaker via /storePreset. Values are XML-escaped
        (a "Rock & Roll" playlist or a URL with & would otherwise be rejected);
        `art` becomes containerArt, which the preset tiles show."""
        acct = f' sourceAccount={quoteattr(account)}' if account else ''
        art_el = f'<containerArt>{xml_escape(art)}</containerArt>' if art else ''
        xml = (
            f'<preset id={quoteattr(str(preset_id))}>'
            f'<ContentItem source={quoteattr(source)} type={quoteattr(stype or "")} '
            f'location={quoteattr(location)}{acct}>'
            f'<itemName>{xml_escape(name or "")}</itemName>{art_el}'
            f'</ContentItem></preset>'
        )
        return self._post("/storePreset", xml)

    def now_playing_item(self):
        """What's playing, as a preset-ready dict — or None when nothing
        presettable is on (standby, TV, Bluetooth, AUX…). For Spotify this is
        the playlist/album the track came from, not the single track."""
        np = self._get("/now_playing")
        ci = np.find("ContentItem") if np is not None else None
        if ci is None or ci.get("isPresetable") != "true" or not ci.get("location"):
            return None
        return {"source": ci.get("source", ""), "type": ci.get("type", ""),
                "location": ci.get("location", ""), "account": ci.get("sourceAccount", ""),
                "name": (ci.findtext("itemName") or np.findtext("stationName")
                         or np.findtext("track") or "Preset").strip(),
                "art": (np.findtext("art") or "").strip()}

    def select_content(self, source, stype, location, name="", account=""):
        """Play a ContentItem immediately via /select."""
        acct = f' sourceAccount="{account}"' if account else ''
        xml = (
            f'<ContentItem source="{source}" type="{stype}" '
            f'location="{location}"{acct}>'
            f'<itemName>{name}</itemName>'
            f'</ContentItem>'
        )
        return self._post("/select", xml)

    def play_via_avt(self, stream_url, title="", art=""):
        """Play a stream URL via UPnP AVTransport (port 8091).
        Used for speakers that lack LOCAL_INTERNET_RADIO. The URL must be HTTP
        (not HTTPS) — the speaker follows redirects but rejects https:// URIs.

        The DIDL-Lite metadata is what the speaker shows on its display and in
        now_playing. Sent empty, an ST20 showed just "Q". For our own station
        URLs (/dlna/stream/<id>) the name and logo are looked up automatically."""
        if not title and "/dlna/stream/" in stream_url:
            st = PresetStore().get_station(stream_url.rstrip("/").split("/")[-1])
            if st:
                title, art = st.get("name", ""), art or st.get("art_url", "")
        avt = f"http://{self.host}:8091/AVTransport/Control"
        esc = xml_escape(stream_url)
        meta = xml_escape(avt_didl(stream_url, title, art)) if title else ""
        set_soap = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            f'<CurrentURI>{esc}</CurrentURI>'
            f'<CurrentURIMetaData>{meta}</CurrentURIMetaData>'
            '</u:SetAVTransportURI></s:Body></s:Envelope>'
        )
        play_soap = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Speed>1</Speed>'
            '</u:Play></s:Body></s:Envelope>'
        )
        h_set  = {"Content-Type": 'text/xml; charset="utf-8"',
                  "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'}
        h_play = {"Content-Type": 'text/xml; charset="utf-8"',
                  "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#Play"'}
        try:
            r = self._session.post(avt, data=set_soap.encode(), headers=h_set, timeout=4)
            if r.status_code != 200:
                log.warning(f"[AVT] SetAVTransportURI failed {r.status_code}: {r.text[:200]}")
                return False
            r = self._session.post(avt, data=play_soap.encode(), headers=h_play, timeout=4)
            ok = r.status_code == 200
            if not ok:
                log.warning(f"[AVT] Play failed {r.status_code}: {r.text[:200]}")
            return ok
        except Exception as e:
            log.warning(f"[AVT] Error: {e}")
            return False

    # ── group / multi-room ─────────────────────────────────────────────────────
    def invalidate_zone_cache(self):
        """Force the next get_zone() call to re-fetch from the speaker."""
        self._zone_ts = 0.0

    def get_zone(self):
        """Return zone membership info for this speaker.
        Result is cached for 10 s — zone membership changes only on explicit group ops."""
        _ZONE_TTL = 10.0
        now = time.monotonic()
        if self._zone_cache is not None and (now - self._zone_ts) < _ZONE_TTL:
            return self._zone_cache
        zx = self._get("/getZone")
        if zx is None:
            return {"is_master": False, "is_slave": False,
                    "master_id": "", "master_ip": "", "members": []}
        master_id = zx.get("master", "")
        members = [{"ip": m.get("ipaddress",""), "id": m.text or ""}
                   for m in zx.findall("member")]
        is_master = bool(master_id and master_id == self.device_id and
                         len(members) > 1)
        is_slave  = bool(master_id and master_id != self.device_id)
        master_ip = ""
        if is_slave:
            for m in members:
                if m["id"] == master_id:
                    master_ip = m["ip"]; break
        result = {
            "is_master": is_master,
            "is_slave":  is_slave,
            "master_id": master_id,
            "master_ip": master_ip,
            "members":   members,
        }
        self._zone_cache = result
        self._zone_ts    = time.monotonic()
        return result

    def set_zone(self, slave_devices):
        """Create a zone with self as master and slave_devices as the slaves.

        The firmware treats setZone as additive — it merges new members into
        the existing zone rather than replacing it. To properly remove members
        we must dissolve first (empty setZone) then recreate. A short pause
        is required between the two calls; the firmware ignores a setZone
        that arrives too quickly after a dissolve.
        """
        self._post("/setZone", f'<zone master="{self.device_id}"></zone>')
        time.sleep(0.5)
        members_xml = f'<member ipaddress="{self.host}">{self.device_id}</member>'
        for d in slave_devices:
            members_xml += f'<member ipaddress="{d.host}">{d.device_id}</member>'
        return self._post("/setZone",
                          f'<zone master="{self.device_id}">{members_xml}</zone>')

    def remove_zone(self):
        """Dissolve the zone this speaker is master of.

        The firmware has no working removeZone/removeZoneSlaves endpoint.
        Posting an empty <zone> body to /setZone is the only way to dissolve.
        """
        zinfo = self.get_zone()
        if not zinfo["is_master"]:
            return True
        return self._post("/setZone", f'<zone master="{self.device_id}"></zone>')


# ═══════════════════════════════════════════════════════════════════════════════
# Local preset store  (JSON files on disk, survives cloud shutdown)
# ═══════════════════════════════════════════════════════════════════════════════

class PresetStore:
    """Manages backed-up presets and custom stations on the local filesystem."""

    def __init__(self, presets_dir=PRESETS_DIR, stations_dir=STATIONS_DIR):
        self.presets_dir  = pathlib.Path(presets_dir)
        self.stations_dir = pathlib.Path(stations_dir)
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.stations_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()   # serialise concurrent writers

    # ── per-speaker preset backup ─────────────────────────────────────────────
    def _speaker_file(self, host):
        return self.presets_dir / f"{host.replace('.','_')}.json"

    def backup_presets(self, host, presets):
        """Save a speaker's presets to disk."""
        path = self._speaker_file(host)
        data = {
            "host":       host,
            "backed_up":  time.strftime("%Y-%m-%dT%H:%M:%S"),
            "presets":    presets,
        }
        with self._lock:
            _atomic_write(path, json.dumps(data, indent=2))
        log.info(f"Backed up {len(presets)} presets for {host}")
        return data

    def load_backup(self, host):
        path = self._speaker_file(host)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def backup_presets_raw(self, host, data):
        """Save pre-validated backup data (e.g. after user editing)."""
        path = self._speaker_file(host)
        with self._lock:
            _atomic_write(path, json.dumps(data, indent=2))
        log.info(f"[BACKUP] Saved edited backup for {host}")

    def list_backups(self):
        out = []
        for f in sorted(self.presets_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text())
                out.append(d)
            except Exception:
                pass
        return out

    # ── custom stations ───────────────────────────────────────────────────────
    def save_station(self, station_id, name, stream_url, art_url=""):
        """Save a custom radio station definition."""
        data = {
            "id":         station_id,
            "name":       name,
            "stream_url": stream_url,
            "art_url":    art_url,
        }
        path = self.stations_dir / f"{station_id}.json"
        with self._lock:
            _atomic_write(path, json.dumps(data, indent=2))
        return data

    def delete_station(self, station_id):
        path = self.stations_dir / f"{station_id}.json"
        with self._lock:
            if path.exists():
                path.unlink()
                return True
        return False

    def list_stations(self):
        out = []
        for f in sorted(self.stations_dir.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
        return out

    def get_station(self, station_id):
        path = self.stations_dir / f"{station_id}.json"
        if path.exists():
            return json.loads(path.read_text())
        return None

    def station_descriptor(self, station_id):
        """Return the JSON blob the speaker fetches from our station server."""
        st = self.get_station(station_id)
        if not st:
            return None
        return json.dumps({
            "name":       st["name"],
            "imageUrl":   st.get("art_url", ""),
            "streamType": "liveRadio",
            "audio": {
                "streamUrl":  st["stream_url"],
                "hasPlaylist": False,
                "isRealtime":  True,
            }
        })


def plan_preset_restore(preset, has_local_ir, store, dlna):
    """Decide how a single backed-up preset should be re-stored on a speaker.

    Pure function (no I/O of its own beyond reading the station store) so the
    restore conversion can be unit-tested without a speaker. Returns:
      ("store", kwargs) — caller should call dev.store_preset(**kwargs)
      ("skip", reason)  — referenced custom station is gone; skip with a log note
      None              — preset is malformed (missing id or source); ignore

    For speakers that lack LOCAL_INTERNET_RADIO, a LOCAL_INTERNET_RADIO preset is
    converted to a UPNP preset pointing at our DLNA stream redirect, provided the
    custom station it references still exists.
    """
    pid = preset.get("id", "")
    if not pid or not preset.get("source"):
        return None
    src  = preset["source"]
    name = preset.get("name", "")
    loc  = preset.get("location", "")
    if src == "LOCAL_INTERNET_RADIO" and not has_local_ir:
        station_id = loc.rstrip("/").split("/")[-1]
        if not store.get_station(station_id):
            return ("skip", f"no station for {station_id!r}")
        return ("store", dict(preset_id=pid, name=name, source="UPNP", stype="",
                              location=dlna.stream_url(station_id),
                              account="UPnPUserName"))
    return ("store", dict(preset_id=pid, name=name, source=src,
                          stype=preset.get("type", ""), location=loc,
                          account=preset.get("account", "")))


# ═══════════════════════════════════════════════════════════════════════════════
# DLNA / UPnP ContentDirectory server
# ═══════════════════════════════════════════════════════════════════════════════

class DLNAServer:
    """Minimal UPnP MediaServer so speakers without LOCAL_INTERNET_RADIO can play
    our custom radio stations as STORED_MUSIC presets.

    Announces itself via SSDP so the speaker discovers and trusts our UUID.
    HTTP endpoints (served by Handler) provide device.xml, SCPD, SOAP Browse,
    and a stream passthrough so the speaker can play any of our custom stations.
    """

    _MCAST_ADDR     = "239.255.255.250"
    _MCAST_PORT     = 1900
    _ALIVE_INTERVAL = 60
    _CACHE_CONTROL  = "max-age=1800"
    DEVICE_TYPE     = "urn:schemas-upnp-org:device:MediaServer:1"
    CD_SERVICE      = "urn:schemas-upnp-org:service:ContentDirectory:1"

    def __init__(self, uuid, http_port, local_ip, store):
        self.uuid      = uuid
        self.udn       = f"uuid:{uuid}"
        self.http_port = http_port
        self.local_ip  = local_ip
        self.store     = store
        self._running  = False
        self._sock     = None

    @property
    def base_url(self):
        return f"http://{self.local_ip}:{self.http_port}"

    @property
    def device_url(self):
        return f"{self.base_url}/dlna/device.xml"

    def stream_url(self, station_id):
        return f"{self.base_url}/dlna/stream/{station_id}"

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="dlna-ssdp").start()
        log.info(f"[DLNA] SSDP started  uuid={self.uuid}  base={self.base_url}")

    def stop(self):
        self._running = False
        if self._sock:
            try: self._sock.close()
            except Exception: pass

    # ── SSDP ─────────────────────────────────────────────────────────────────

    def _nt_pairs(self):
        return [
            ("upnp:rootdevice",  f"{self.udn}::upnp:rootdevice"),
            (self.udn,            self.udn),
            (self.DEVICE_TYPE,   f"{self.udn}::{self.DEVICE_TYPE}"),
            (self.CD_SERVICE,    f"{self.udn}::{self.CD_SERVICE}"),
        ]

    def _send_alive(self, sock):
        for nt, usn in self._nt_pairs():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {self._MCAST_ADDR}:{self._MCAST_PORT}\r\n"
                "NTS: ssdp:alive\r\n"
                f"NT: {nt}\r\n"
                f"USN: {usn}\r\n"
                f"LOCATION: {self.device_url}\r\n"
                f"CACHE-CONTROL: {self._CACHE_CONTROL}\r\n"
                "SERVER: Linux/1.0 UPnP/1.0 SoundTouchRadio/1.0\r\n"
                "\r\n"
            )
            try: sock.sendto(msg.encode(), (self._MCAST_ADDR, self._MCAST_PORT))
            except Exception: pass

    def _send_byebye(self, sock):
        for nt, usn in self._nt_pairs():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {self._MCAST_ADDR}:{self._MCAST_PORT}\r\n"
                "NTS: ssdp:byebye\r\n"
                f"NT: {nt}\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            try: sock.sendto(msg.encode(), (self._MCAST_ADDR, self._MCAST_PORT))
            except Exception: pass

    def _respond_msearch(self, sock, addr, st):
        our_types = {"ssdp:all", "upnp:rootdevice", self.udn,
                     self.DEVICE_TYPE, self.CD_SERVICE}
        if st not in our_types:
            return
        pairs = self._nt_pairs() if st == "ssdp:all" else \
                [(t, u) for t, u in self._nt_pairs() if t == st]
        for nt, usn in pairs:
            msg = (
                "HTTP/1.1 200 OK\r\n"
                f"CACHE-CONTROL: {self._CACHE_CONTROL}\r\n"
                f"LOCATION: {self.device_url}\r\n"
                f"ST: {nt}\r\n"
                f"USN: {usn}\r\n"
                "SERVER: Linux/1.0 UPnP/1.0 SoundTouchRadio/1.0\r\n"
                f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
                "\r\n"
            )
            try: sock.sendto(msg.encode(), addr)
            except Exception: pass

    def _run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError: pass
            sock.bind(("", self._MCAST_PORT))
            mreq = struct.pack("4sL", socket.inet_aton(self._MCAST_ADDR), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(2.0)
            self._sock = sock
            self._send_alive(sock)
            next_alive = time.monotonic() + self._ALIVE_INTERVAL
            while self._running:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    if time.monotonic() >= next_alive:
                        self._send_alive(sock)
                        next_alive = time.monotonic() + self._ALIVE_INTERVAL
                    continue
                try:
                    text = data.decode("utf-8", errors="replace")
                    if text.startswith("M-SEARCH"):
                        m = re.search(r"ST:\s*(\S+)", text, re.IGNORECASE)
                        if m:
                            self._respond_msearch(sock, addr, m.group(1).strip())
                except Exception:
                    pass
            self._send_byebye(sock)
        except Exception as e:
            log.error(f"[DLNA] SSDP thread error: {e}")
        finally:
            try:
                if self._sock: self._sock.close()
            except Exception:
                pass

    # ── HTTP content (called from Handler) ───────────────────────────────────

    def device_xml(self):
        return (
            '<?xml version="1.0"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">'
            '<specVersion><major>1</major><minor>0</minor></specVersion>'
            f'<URLBase>{self.base_url}</URLBase>'
            '<device>'
            f'<deviceType>{self.DEVICE_TYPE}</deviceType>'
            '<friendlyName>SoundTouch Radio</friendlyName>'
            '<manufacturer>SoundTouchController</manufacturer>'
            '<modelName>Radio Station Server</modelName>'
            f'<UDN>{self.udn}</UDN>'
            '<serviceList><service>'
            f'<serviceType>{self.CD_SERVICE}</serviceType>'
            '<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
            '<SCPDURL>/dlna/cd.xml</SCPDURL>'
            '<controlURL>/dlna/cd/control</controlURL>'
            '<eventSubURL>/dlna/cd/events</eventSubURL>'
            '</service></serviceList>'
            '</device>'
            '</root>'
        ).encode()

    def cd_scpd_xml(self):
        return b"""<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType>
      <allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList>
    </stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

    # ── DIDL-Lite helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    def _item_xml(self, st):
        sid = st["id"]
        url = self._esc(self.stream_url(sid))
        return (
            f'<item id="station/{self._esc(sid)}" parentID="0" restricted="1">'
            f'<dc:title>{self._esc(st["name"])}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            f'<res protocolInfo="http-get:*:audio/mpeg:*">{url}</res>'
            '</item>'
        )

    _DIDL_NS = (
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
    )

    def browse_response(self, object_id, browse_flag):
        stations = self.store.list_stations()
        if object_id == "0":
            if browse_flag == "BrowseMetadata":
                didl = (
                    f'<DIDL-Lite {self._DIDL_NS}>'
                    f'<container id="0" parentID="-1" restricted="1" childCount="{len(stations)}">'
                    '<dc:title>Radio Stations</dc:title>'
                    '<upnp:class>object.container</upnp:class>'
                    '</container></DIDL-Lite>'
                )
                return didl, 1, 1
            items = "".join(self._item_xml(s) for s in stations)
            return f'<DIDL-Lite {self._DIDL_NS}>{items}</DIDL-Lite>', len(stations), len(stations)
        if object_id.startswith("station/"):
            sid = object_id[len("station/"):]
            st = self.store.get_station(sid)
            if st:
                didl = f'<DIDL-Lite {self._DIDL_NS}>{self._item_xml(st)}</DIDL-Lite>'
                return didl, 1, 1
        empty = f'<DIDL-Lite {self._DIDL_NS}></DIDL-Lite>'
        return empty, 0, 0

    # ── SOAP ─────────────────────────────────────────────────────────────────

    def handle_soap(self, body_bytes):
        try:
            xml = ET.fromstring(body_bytes)
            ns_s = "http://schemas.xmlsoap.org/soap/envelope/"
            ns_u = "urn:schemas-upnp-org:service:ContentDirectory:1"
            body_el = xml.find(f"{{{ns_s}}}Body")
            if body_el is None:
                return self._soap_error(401, "Invalid Action")
            browse_el = body_el.find(f"{{{ns_u}}}Browse")
            if browse_el is None:
                return self._soap_error(401, "Invalid Action")
            object_id   = (browse_el.findtext("ObjectID",   default="0") or "0").strip()
            browse_flag = (browse_el.findtext("BrowseFlag", default="BrowseDirectChildren") or "").strip()
            if browse_flag not in ("BrowseMetadata", "BrowseDirectChildren"):
                browse_flag = "BrowseDirectChildren"
            didl, returned, total = self.browse_response(object_id, browse_flag)
            log.debug(f"[DLNA] Browse({object_id!r},{browse_flag}) → {returned} item(s)")
            return (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body>'
                '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                f'<Result>{self._esc(didl)}</Result>'
                f'<NumberReturned>{returned}</NumberReturned>'
                f'<TotalMatches>{total}</TotalMatches>'
                '<UpdateID>1</UpdateID>'
                '</u:BrowseResponse>'
                '</s:Body>'
                '</s:Envelope>'
            ).encode("utf-8")
        except Exception as e:
            log.error(f"[DLNA] SOAP error: {e}")
            return self._soap_error(501, "Action Failed")

    @staticmethod
    def _soap_error(code, desc):
        return (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body><s:Fault>'
            '<faultcode>s:Client</faultcode>'
            '<faultstring>UPnPError</faultstring>'
            '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f'<errorCode>{code}</errorCode>'
            f'<errorDescription>{desc}</errorDescription>'
            '</UPnPError></detail>'
            '</s:Fault></s:Body>'
            '</s:Envelope>'
        ).encode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Scene store  (named zone + preset + volume snapshots)
# ═══════════════════════════════════════════════════════════════════════════════

class SceneStore:
    """Stores named scenes as JSON files in data/scenes/."""

    def __init__(self, scenes_dir=SCENES_DIR):
        self.scenes_dir = pathlib.Path(scenes_dir)
        self.scenes_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()   # serialise concurrent writers

    def _path(self, scene_id):
        return self.scenes_dir / f"{scene_id}.json"

    def save(self, scene_id, data):
        with self._lock:
            _atomic_write(self._path(scene_id), json.dumps(data, indent=2))

    def load(self, scene_id):
        p = self._path(scene_id)
        return json.loads(p.read_text()) if p.exists() else None

    def delete(self, scene_id):
        with self._lock:
            p = self._path(scene_id)
            if p.exists(): p.unlink(); return True
        return False

    def list_scenes(self):
        out = []
        for f in sorted(self.scenes_dir.glob("*.json")):
            try: out.append(json.loads(f.read_text()))
            except Exception: pass
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# Alarm store + scheduler  (wake-up / timed playback)
# ═══════════════════════════════════════════════════════════════════════════════

class AlarmStore:
    """Persists alarm definitions to data/alarms.json."""

    def __init__(self, alarm_file=ALARMS_FILE):
        self._file = pathlib.Path(alarm_file)
        self._lock = threading.Lock()

    def _load(self):
        if not self._file.exists(): return []
        try: return json.loads(self._file.read_text())
        except Exception: return []

    def _save(self, alarms):
        _atomic_write(self._file, json.dumps(alarms, indent=2))

    def list_alarms(self):
        with self._lock: return list(self._load())

    def save_alarm(self, alarm):
        with self._lock:
            alarms = self._load()
            idx = next((i for i, a in enumerate(alarms) if a["id"] == alarm["id"]), None)
            if idx is not None: alarms[idx] = alarm
            else: alarms.append(alarm)
            self._save(alarms)

    def delete_alarm(self, alarm_id):
        with self._lock:
            self._save([a for a in self._load() if a["id"] != alarm_id])

    def toggle_alarm(self, alarm_id, enabled):
        with self._lock:
            alarms = self._load()
            for a in alarms:
                if a["id"] == alarm_id: a["enabled"] = enabled; break
            self._save(alarms)

    def record_result(self, alarm_id, fired_at, result):
        """Keep the outcome of the last ring on the alarm itself — the main
        log rotates in about an hour, so this is the only lasting record."""
        with self._lock:
            alarms = self._load()
            for a in alarms:
                if a["id"] == alarm_id:
                    a["last_fired"], a["last_result"] = fired_at, result
                    break
            self._save(alarms)


class AudioModeStore:
    """Per-speaker "auto dialogue mode on TV" setting, persisted to
    data/audio_mode.json. Keyed by deviceID (falls back to IP) so the setting
    survives DHCP address changes. Defaults to enabled."""

    def __init__(self, path=AUDIO_MODE_FILE):
        self._file = pathlib.Path(path)
        self._lock = threading.Lock()

    def _load(self):
        if not self._file.exists(): return {}
        try: return json.loads(self._file.read_text())
        except Exception: return {}

    @staticmethod
    def _key(dev):
        return dev.device_id or dev.host

    def auto_enabled(self, dev):
        with self._lock:
            return bool(self._load().get(self._key(dev), {}).get("auto_tv", True))

    def set_auto(self, dev, enabled):
        with self._lock:
            data = self._load()
            data.setdefault(self._key(dev), {})["auto_tv"] = bool(enabled)
            _atomic_write(self._file, json.dumps(data, indent=2))


def _source_kind(source):
    """Classify a now_playing source: "tv" (PRODUCT = TV/HDMI input), "idle"
    (standby / nothing selected), or "music" (everything else)."""
    if source == "PRODUCT":
        return "tv"
    if source in ("", "STANDBY", "INVALID_SOURCE"):
        return "idle"
    return "music"


def audio_mode_for_transition(prev_source, source):
    """Audio mode to apply when a soundbar moves prev_source → source, or None.

    Entering the TV input (from music *or* standby — the bar tends to drop
    dialogue mode when it sleeps) → "dialog". Entering a music source from TV
    or standby → "normal". Music → music and TV → TV leave the mode alone, so
    a manual change made mid-session sticks. prev_source=None (first time we
    see the speaker) never acts, so starting the controller doesn't override."""
    if prev_source is None:
        return None
    before, after = _source_kind(prev_source), _source_kind(source)
    if before == after:
        return None
    if after == "tv":
        return "dialog"
    if after == "music":
        return "normal"
    return None


def avt_didl(url, title, art=""):
    """DIDL-Lite for AVTransport's CurrentURIMetaData: one radio-broadcast item
    whose title (and album art) the speaker puts on its display."""
    e = lambda v: xml_escape(v, {'"': "&quot;"})
    art_el = f'<upnp:albumArtURI>{e(art)}</upnp:albumArtURI>' if art else ''
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1">'
            f'<dc:title>{e(title)}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            f'{art_el}<res protocolInfo="http-get:*:audio/mpeg:*">{e(url)}</res>'
            '</item></DIDL-Lite>')


def preset_target_ok(item, sources):
    """Can a speaker with `sources` (get_sources() output) play preset `item`?
    Returns (ok, reason). Spotify needs the same linked account, ready; our
    DLNA radio (UPNP) plays anywhere; anything else needs that source."""
    src = item.get("source")
    if src == "UPNP":
        return True, ""
    if not sources:
        return False, "unreachable"
    if src == "SPOTIFY":
        if any(s["source"] == "SPOTIFY" and s["sourceAccount"] == item.get("account")
               and s["status"] == "READY" for s in sources):
            return True, ""
        return False, "no Spotify account linked"
    if any(s["source"] == src for s in sources):
        return True, ""
    return False, f"no {src} source"


def tv_wake_action(woke_source, elapsed, source, playing, grace=TV_WAKE_GRACE):
    """Decide what to do after a soundbar woke from standby into a music source.

    Turning the TV on wakes the bar over HDMI-CEC, but the bar comes up on
    whatever source it was last on, not the TV. A wake you caused (Spotify,
    AirPlay, the app, Alexa) starts playing within a few seconds; a CEC wake
    from the TV just sits there silent. So: still on the same music source
    and not playing once `grace` seconds have passed → switch to TV.

    This never selects the TV input while the TV might be off, which is what
    would turn the TV on over CEC.

    Returns "switch" (select PRODUCT/TV), "done" (stop watching — it played,
    or the source changed) or "wait"."""
    if playing or source != woke_source:
        return "done"
    return "switch" if elapsed >= grace else "wait"


class MaintenanceStore:
    """Scheduled restart of the clock speakers (ST20s), persisted to
    data/maintenance.json. Their front-panel clock stalls after running for a
    while — the API still reports the right time — and a restart fixes it."""

    DEFAULTS = {"enabled": True, "day": 6, "time": "04:00", "tz": "Europe/London"}

    def __init__(self, path=MAINTENANCE_FILE):
        self._file = pathlib.Path(path)
        self._lock = threading.Lock()

    def _load(self):
        try: return json.loads(self._file.read_text())
        except Exception: return {}

    def get(self):
        with self._lock:
            return {**self.DEFAULTS, **self._load()}

    def update(self, **fields):
        with self._lock:
            data = {**self.DEFAULTS, **self._load(), **fields}
            _atomic_write(self._file, json.dumps(data, indent=2))
            return data


def maintenance_due_key(cfg, now_utc):
    """Same rules as an alarm: weekly on cfg["day"] (0=Mon … 6=Sun) at
    cfg["time"] in cfg["tz"]."""
    return alarm_due_key({"id": "auto_restart", "enabled": cfg.get("enabled"),
                          "time": cfg.get("time"), "days": [int(cfg.get("day", 6))],
                          "tz": cfg.get("tz")}, now_utc)


def alarm_tz(alarm):
    """The timezone an alarm's time is written in. New alarms carry the phone's
    zone ("Europe/London"); older ones fall back to SOUNDTOUCH_TZ, then the
    server's local time. The server runs in UTC, so without this a 07:00 alarm
    rang at 08:00 during British Summer Time."""
    for name in (alarm.get("tz"), os.environ.get("SOUNDTOUCH_TZ")):
        if name:
            try: return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError): pass
    return None   # → server local time


def alarm_due_key(alarm, now_utc):
    """Dedup key if `alarm` should ring at `now_utc` (an aware datetime), else
    None. The key is unique per alarm, local day and time, so the 30 s ticks
    fire it exactly once — and an alarm edited to a later time the same day
    still rings at the new time."""
    if not alarm.get("enabled"):
        return None
    now = now_utc.astimezone(alarm_tz(alarm))   # tz None → server local time
    if alarm.get("time") != now.strftime("%H:%M"):
        return None
    if now.weekday() not in alarm.get("days", list(range(7))):   # 0=Mon … 6=Sun
        return None
    return f"{alarm['id']}_{now.date().isoformat()}_{alarm['time']}"


class AlarmScheduler:
    """Background thread that fires alarms at their scheduled time."""

    VERIFY_AFTER = 15   # seconds to wait for the speaker to start before retrying

    def __init__(self, alarm_store, app_state):
        self._store     = alarm_store
        self._app       = app_state
        self._fired     = {}   # alarm_due_key → True
        self._thread    = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("[ALARM] Scheduler started")

    def _run(self):
        while True:
            try: self._tick()
            except Exception as e: log.warning(f"[ALARM] tick error: {e}")
            time.sleep(30)

    def _tick(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        store = getattr(self._app, "maintenance_store", None)
        if store:
            key = maintenance_due_key(store.get(), now)
            if key and not self._fired.get(key):
                self._fired[key] = True
                threading.Thread(target=self._app.restart_clock_speakers,
                                 args=("weekly restart",), daemon=True).start()
        for alarm in self._store.list_alarms():
            key = alarm_due_key(alarm, now)
            if not key or self._fired.get(key): continue
            self._fired[key] = True
            threading.Thread(target=self._fire, args=(alarm,), daemon=True).start()

    def _device(self, alarm):
        """Find the alarm's speaker by IP, falling back to its deviceID in case
        DHCP has moved it since the alarm was set."""
        dev = self._app.get_device(alarm.get("host"))
        if dev or not alarm.get("device_id"):
            return dev
        with self._app._lock:
            return next((d for d in self._app.devices
                         if d.device_id == alarm["device_id"]), None)

    def _wait_playing(self, dev):
        """Poll once a second until the speaker plays (True) or VERIFY_AFTER
        runs out (False). Returning as soon as it plays matters: a single check
        at the end missed alarms that were switched off within 15 s, and the
        retry then turned the radio back on."""
        deadline = time.monotonic() + self.VERIFY_AFTER
        while True:
            if dev.is_playing():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)

    def _fire(self, alarm):
        name, n = alarm.get("name"), alarm.get("preset", 1)
        fired_at = _dt.datetime.now(alarm_tz(alarm) or _dt.timezone.utc).isoformat(timespec="seconds")
        dev = self._device(alarm)
        if not dev:
            log.warning(f"[ALARM] '{name}': speaker {alarm.get('host')} not found")
            self._store.record_result(alarm["id"], fired_at, "speaker not found")
            return
        log.info(f"[ALARM] Firing '{name}' — {dev.name} ({dev.host}) preset {n}")
        result = "failed"
        for attempt in (1, 2):
            vol = alarm.get("volume")
            if vol is not None:
                dev.set_volume(vol); time.sleep(0.5)
            dev.invalidate_preset_cache()
            dev.play_preset(n)
            if self._wait_playing(dev):
                result = "played" if attempt == 1 else "played (retry)"
                if vol is not None:
                    dev.set_volume(vol)   # a speaker in standby can ignore the first one
                break
            log.warning(f"[ALARM] '{name}' not playing after attempt {attempt}")
        log.info(f"[ALARM] '{name}' result: {result}")
        self._store.record_result(alarm["id"], fired_at, result)


# ═══════════════════════════════════════════════════════════════════════════════
# Speaker discovery
# ═══════════════════════════════════════════════════════════════════════════════

def _probe(ip, results, lock):
    try:
        r = requests.get(f"http://{ip}:8090/info", timeout=1.5)
        if r.status_code == 200 and ("SoundTouch" in r.text or "Bose" in r.text):
            dev = SoundTouchDevice(ip)
            dev.fetch_info()
            with lock:
                if not any(d.host == ip for d in results):
                    results.append(dev)
                    log.info(f"Found speaker: {dev.name} ({ip})")
    except Exception:
        pass

def discover_mdns(results, lock, timeout=4):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
        class _L:
            def add_service(self, zc, t, name):
                info = zc.get_service_info(t, name)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    _probe(ip, results, lock)
            def remove_service(self, *_): pass
            def update_service(self, *_): pass
        zc = Zeroconf()
        ServiceBrowser(zc, "_soundtouch._tcp.local.", _L())
        time.sleep(timeout)
        zc.close()
    except Exception as e:
        log.warning(f"[mDNS] {e}")

def discover_subnet_scan(results, lock, timeout=1.5):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        prefix = ".".join(local_ip.split(".")[:3])
    except Exception:
        return
    log.info(f"Scanning {prefix}.0/24 …")
    sem = threading.Semaphore(64)
    threads = []
    for i in range(1, 255):
        ip = f"{prefix}.{i}"
        def _w(ip=ip):
            with sem: _probe(ip, results, lock)
        t = threading.Thread(target=_w, daemon=True)
        threads.append(t); t.start()
    for t in threads:
        t.join(timeout=timeout + 1)

def discover_all(timeout=4):
    results, lock = [], threading.Lock()
    t1 = threading.Thread(target=discover_mdns, args=(results, lock, timeout), daemon=True)
    t2 = threading.Thread(target=discover_subnet_scan, args=(results, lock, timeout), daemon=True)
    t1.start(); t2.start(); t1.join(); t2.join()
    results.sort(key=lambda d: d.name.lower())
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


def _atomic_write(path, text):
    """Write text to `path` atomically: write to a temp file then os.replace().
    A crash mid-write can never leave a truncated/corrupt target file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)   # atomic on POSIX


def _is_local_hostname(hostname):
    """True if `hostname` (no port) is safe to serve: an IP literal, loopback, or
    an mDNS .local name. DNS-rebinding attacks rely on an attacker-controlled DNS
    *name* resolving to our LAN IP, so rejecting arbitrary names defeats them."""
    if not hostname:
        return True                       # non-browser clients (UPnP, curl) may omit it
    hostname = hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return True
    try:
        ipaddress.ip_address(hostname)    # any IPv4/IPv6 literal is rebinding-proof
        return True
    except ValueError:
        return False


def _host_header_hostname(host_header):
    """Extract the bare hostname from a Host header value (strip port / IPv6 brackets)."""
    h = (host_header or "").strip()
    if h.startswith("["):                 # [::1] or [::1]:8888
        return h[1:].split("]")[0]
    if h.count(":") == 1:                 # host:port (IPv4 or name)
        return h.split(":")[0]
    return h                              # bare host, or bracketless IPv6


# ═══════════════════════════════════════════════════════════════════════════════
# Web UI assets (served from the web/ directory beside this file)
# ═══════════════════════════════════════════════════════════════════════════════

WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"

_WEB_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
}
_web_cache = {}

def web_asset(name):
    """Return (body_bytes, content_type) for a file in web/, cached after first read.
    Raises FileNotFoundError if the asset is missing."""
    hit = _web_cache.get(name)
    if hit is None:
        path = WEB_DIR / name
        ctype = _WEB_CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        hit = _web_cache[name] = (path.read_bytes(), ctype)
    return hit


def _check_network(web_port):
    """
    Print a network diagnostic at startup so firewall / config problems
    are immediately visible in the log.
    """
    import subprocess

    local_ip = get_local_ip()

    log.info("── Network diagnostic ─────────────────────────────────")
    log.info(f"  Local IP   : {local_ip}")
    log.info(f"  Web UI     : http://{local_ip}:{web_port}")
    log.info(f"  Matter     : UDP 5540  (Alexa smart home via Matter bridge)")

    # ── ufw status ────────────────────────────────────────────────────────────
    try:
        ufw_out = subprocess.check_output(
            ["sudo", "-n", "ufw", "status"], stderr=subprocess.DEVNULL,
            timeout=3).decode()
        if "Status: active" in ufw_out:
            if str(web_port) not in ufw_out:
                log.warning("  ⚠  ufw is ACTIVE — web UI port may be blocked:")
                log.warning(f"       sudo ufw allow {web_port}/tcp      # web UI")
                log.warning("     Run install.sh to fix this automatically.")
            else:
                log.info("  ✓  ufw is active and required ports appear open")
        elif "Status: inactive" in ufw_out:
            log.info("  ✓  ufw is installed but inactive — no firewall blocking")
        else:
            log.info(f"  ufw status: {ufw_out.strip()[:80]}")
    except FileNotFoundError:
        log.info("  ufw not found — assuming no firewall (non-Ubuntu?)")
    except subprocess.CalledProcessError:
        log.info("  ufw found but could not query status without sudo")
        log.info(f"  Ensure port {web_port}/tcp is open if a firewall is running")
    except Exception as e:
        log.debug(f"  ufw check skipped: {e}")

    log.info("───────────────────────────────────────────────────────")


# ═══════════════════════════════════════════════════════════════════════════════
# TTS announcement engine
# ═══════════════════════════════════════════════════════════════════════════════

def _tts_announce(devices, text, volume, web_port):
    """Generate TTS MP3, serve it, play on each device, then restore state."""
    import io
    if not _TTS_AVAILABLE:
        log.error("[TTS] gTTS not installed — run: pip3 install gtts")
        return
    try:
        buf = io.BytesIO()
        _gTTS(text, lang="en", tld="co.uk").write_to_fp(buf)
        mp3_bytes = buf.getvalue()
    except Exception as e:
        log.error(f"[TTS] gTTS generation failed: {e}")
        return

    audio_id = _uuid.uuid4().hex
    _tts_cache[audio_id] = mp3_bytes
    local_ip = get_local_ip()
    # Descriptor URL (JSON) — what the speaker fetches for stationurl type
    desc_url = f"http://{local_ip}:{web_port}/api/tts/desc/{audio_id}"
    mp3_url  = f"http://{local_ip}:{web_port}/api/tts/audio/{audio_id}.mp3"
    # 128 kbps MP3 = 16 000 bytes/s; add 4 s buffer (network + speaker decode latency)
    play_duration = max(len(mp3_bytes) / 16000.0 + 4.0, 5.0)
    log.info(f"[TTS] '{text}' → {mp3_url}  ({len(mp3_bytes)} bytes, ~{play_duration:.1f}s wait)")

    def announce_one(dev):
        try:
            # ── capture current state ────────────────────────────────────────
            np = dev._get("/now_playing")
            was_playing, was_standby, saved_ci = False, False, None
            if np is not None:
                ps  = np.get("playStatus") or np.findtext("playStatus") or ""
                src = np.get("source") or np.findtext("source") or ""
                was_playing = ps in ("PLAY_STATE", "BUFFERING_STATE")
                was_standby = src.upper() in ("STANDBY", "") or not was_playing and not src
                ci = np.find("ContentItem")
                if ci is not None:
                    saved_ci = ET.tostring(ci, encoding="unicode")
            vx = dev._get("/volume")
            saved_vol = None
            if vx is not None:
                for tag in ("actualvolume", "targetvolume"):
                    el = vx.find(tag)
                    if el is not None:
                        saved_vol = int(el.text); break

            log.info(f"[TTS] {dev.host} was_playing={was_playing} was_standby={was_standby} saved_vol={saved_vol}")

            # ── play announcement ────────────────────────────────────────────
            dev.set_volume(volume)
            time.sleep(0.5)
            dev.select_content("LOCAL_INTERNET_RADIO", "stationurl", desc_url, "Announcement")

            # Wait for speaker to reach PLAY_STATE (not just BUFFERING — audio must
            # actually be flowing before we start the duration countdown).
            # Handles standby wake-up which can take 10-20 s.
            started = False
            for _ in range(60):          # 60 × 0.5 s = 30 s max wake-up wait
                time.sleep(0.5)
                np2 = dev._get("/now_playing")
                if np2 is None:
                    break
                ps2 = np2.get("playStatus") or np2.findtext("playStatus") or ""
                if ps2 == "PLAY_STATE":
                    started = True
                    break

            if started:
                # Audio is flowing — now wait for the clip to finish
                log.info(f"[TTS] {dev.host} playing — waiting {play_duration:.1f}s")
                time.sleep(play_duration)
            else:
                log.warning(f"[TTS] {dev.host} never reached PLAY_STATE — skipping wait")

            # ── restore ───────────────────────────────────────────────────────
            if saved_vol is not None:
                dev.set_volume(saved_vol)
            time.sleep(0.3)
            if was_standby:
                dev.power()
                log.info(f"[TTS] {dev.host} returned to standby")
            elif was_playing and saved_ci:
                dev._post("/select", saved_ci)
                log.info(f"[TTS] {dev.host} resumed previous content")
        except Exception as e:
            log.error(f"[TTS] announce_one({dev.host}) error: {e}")

    threads = [threading.Thread(target=announce_one, args=(d,), daemon=True) for d in devices]
    for t in threads: t.start()
    for t in threads: t.join()

    # Remove cached audio after 5 minutes
    def _cleanup():
        time.sleep(300)
        _tts_cache.pop(audio_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP handler
# ═══════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    server_state = None

    def log_message(self, *_): pass   # silence the default access log

    def _request_allowed(self):
        """DNS-rebinding / cross-origin guard. Rejects requests whose Host (or, when
        present, Origin) is an arbitrary DNS name rather than an IP literal, loopback,
        or .local mDNS name. Speakers reach our DLNA endpoints via IP, so they pass."""
        if not _is_local_hostname(_host_header_hostname(self.headers.get("Host", ""))):
            return False
        origin = self.headers.get("Origin")
        if origin and not _is_local_hostname(urlparse(origin).hostname or ""):
            return False
        return True

    def do_GET(self):
        if not self._request_allowed():
            self._respond(403, "text/plain", b"Forbidden: host not allowed")
            return
        p    = urlparse(self.path)
        path = p.path
        qs   = parse_qs(p.query)
        # Log all API calls; /api/state is noisy so keep it at DEBUG
        if path.startswith("/api/"):
            lvl = logging.DEBUG if path == "/api/state" else logging.INFO
            log.log(lvl, f"[API GET ] {self.path}")

        if path in ("/", "/index.html"):
            self._web("index.html")

        elif path in ("/wall", "/wall.html", "/tab", "/panel"):
            self._web("wall.html")

        elif path in ("/app.css", "/app.js", "/spotify.js"):
            self._web(path.lstrip("/"))

        # ── speaker list / scan ───────────────────────────────────────────────
        elif path == "/api/speakers":
            store = self.server_state.store
            self._json([{"host":d.host,"name":d.name,"model":d.model,
                         "has_backup": d.has_backup}
                        for d in self.server_state.devices])

        elif path == "/api/scan":
            self.server_state.scan()
            self._json([{"host":d.host,"name":d.name,"model":d.model}
                        for d in self.server_state.devices])

        # ── lightweight ping (playing + online only, for background chips) ──────
        elif path == "/api/ping":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev:
                self._json({"online": False, "playing": False})
            else:
                np = dev._get("/now_playing")
                if np is None:
                    self._json({"online": False, "playing": False})
                else:
                    ps = np.get("playStatus") or np.findtext("playStatus") or ""
                    self._json({"online": True,
                                "playing": ps in ("PLAY_STATE","BUFFERING_STATE")})

        # ── device state / commands ───────────────────────────────────────────
        elif path == "/api/state":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev:
                self._json({"error": "no_device"})
            else:
                st = dev.state()
                loc = st.pop("_upnp_location", "")
                dlna_pfx = f"http://{get_local_ip()}:{self.server_state.web_port}/dlna/stream/"
                if loc.startswith(dlna_pfx):
                    sid = loc.rstrip("/").split("/")[-1]
                    station = self.server_state.store.get_station(sid)
                    if station:
                        if not st.get("track"):
                            st["track"] = station.get("name", "")
                        if not st.get("art"):
                            st["art"] = station.get("art_url", "")
                if st.get("audio_mode") is not None:
                    st["auto_dialog_tv"] = self.server_state.audio_mode_store.auto_enabled(dev)
                self._json(st)

        elif path == "/api/cmd":
            host   = qs.get("host",[None])[0]
            action = qs.get("action",[""])[0]
            value  = qs.get("value",[None])[0]
            dev = self.server_state.get_device(host)
            ok = False
            if dev:
                if   action=="playpause":        dev.play_pause(); ok=True
                elif action=="next":             dev.next_track(); ok=True
                elif action=="prev":             dev.prev_track(); ok=True
                elif action=="power":            dev.power();      ok=True
                elif action=="mute":             dev.mute();       ok=True
                elif action=="volume" and value: dev.set_volume(value); ok=True
                elif action=="bass"   and value: dev.set_bass(value);   ok=True
                elif action.startswith("preset"):
                    ok = dev.play_preset(int(action.replace("preset","")))
            self._json({"ok":ok})

        # ── preset backup / restore ───────────────────────────────────────────
        elif path == "/api/presets/backup":
            host = qs.get("host",[None])[0]
            dev = self.server_state.get_device(host)
            if dev:
                dev.invalidate_preset_cache()
                presets = dev.get_presets_detail()
                data = self.server_state.store.backup_presets(host, presets)
                dev.has_backup = True
                self._json(data)
            else:
                self._json({"error":"no_device"})

        elif path == "/api/presets/backup-json":
            host = qs.get("host", [None])[0]
            data = self.server_state.store.load_backup(host)
            if data:
                self._json(data)
            else:
                self._json({"error": "no_backup"})

        elif path == "/api/presets/health":
            host = qs.get("host", [None])[0]
            dev  = self.server_state.get_device(host)
            # try live fetch first, fall back to saved backup
            presets = None
            source_label = "live"
            if dev:
                try:
                    dev.invalidate_preset_cache()
                    presets = dev.get_presets_detail()
                except Exception:
                    presets = None
            if not presets:
                backup = self.server_state.store.load_backup(host)
                if backup:
                    presets = backup.get("presets", [])
                    source_label = "backup"
            if presets is None:
                self._json({"error": "no_data"}); return
            result = []
            for p in presets:
                src  = (p.get("source") or "").upper()
                name = p.get("name") or ""
                if not src or not name:
                    result.append({"id": p.get("id",""), "name": name or f"Preset {p.get('id','')}",
                                   "source": src, "risk": "empty", "label": "", "suggestion": "",
                                   "location": ""})
                    continue
                loc = p.get("location") or ""
                if src in CLOUD_SOURCES:
                    lbl, sug = CLOUD_SOURCES[src]
                    result.append({"id": p.get("id",""), "name": name, "source": src,
                                   "risk": "high", "label": lbl, "suggestion": sug,
                                   "location": loc})
                elif src in SAFE_SOURCES:
                    label = "Custom Radio (UPnP)" if src == "UPNP" else src.replace("_"," ").title()
                    result.append({"id": p.get("id",""), "name": name, "source": src,
                                   "risk": "safe", "label": label, "suggestion": "",
                                   "location": loc})
                else:
                    result.append({"id": p.get("id",""), "name": name, "source": src,
                                   "risk": "unknown", "label": src, "suggestion": "Source type unknown — verify it will still work after the Bose cloud shutdown",
                                   "location": loc})
            at_risk = sum(1 for r in result if r["risk"] == "high")
            self._json({"presets": result, "at_risk": at_risk, "total": len(result),
                        "data_source": source_label})

        elif path == "/api/presets/backup-info":
            host = qs.get("host",[None])[0]
            data = self.server_state.store.load_backup(host)
            self._json(data or {"backed_up":None,"presets":[]})

        elif path == "/api/presets/restore":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            data = self.server_state.store.load_backup(host)
            if not dev:
                self._json({"ok":False,"error":"no_device"})
            elif not data:
                self._json({"ok":False,"error":"no_backup"})
            else:
                has_local_ir = dev.has_local_internet_radio()
                dlna = self.server_state.dlna
                count = 0; skipped = 0
                for p in data.get("presets",[]):
                    plan = plan_preset_restore(p, has_local_ir,
                                               self.server_state.store, dlna)
                    if plan is None:
                        continue
                    kind, payload = plan
                    if kind == "skip":
                        log.warning(f"[restore] {payload} — skipping preset {p.get('id')}")
                        skipped += 1
                    else:
                        dev.store_preset(**payload)
                        count += 1
                self._json({"ok":True,"count":count,"skipped":skipped,
                            "dlna_mode": not has_local_ir})

        # ── custom stations ───────────────────────────────────────────────────
        elif path == "/api/stations/stream-search":
            q = qs.get("q",[""])[0].strip()
            if not q:
                self._json([]); return
            try:
                ua = {"User-Agent": "SoundTouchController/1.0"}
                # Step 1 — search TuneIn for matching stations
                sr = requests.get(
                    f"http://opml.radiotime.com/Search.ashx"
                    f"?query={urlquote(q)}&render=json&type=station",
                    timeout=6, headers=ua)
                body = sr.json().get("body", [])
                stations = []
                def _collect(items):
                    for item in (items or []):
                        if item.get("type") == "audio" and item.get("item") == "station":
                            stations.append(item)
                        elif item.get("children"):
                            _collect(item["children"])
                _collect(body)
                stations = stations[:8]

                # Step 2 — resolve each station's direct stream URL in parallel
                def _resolve(st):
                    gid = st.get("guide_id","")
                    if not gid: return None
                    try:
                        tr = requests.get(
                            f"http://opml.radiotime.com/Tune.ashx?id={gid}&render=json",
                            timeout=4, headers=ua)
                        streams = [b for b in tr.json().get("body",[])
                                   if b.get("element") == "audio"]
                        def _u(b): return b.get("url") or b.get("URL","")
                        valid = [s for s in streams
                                 if _u(s) and "notcompatible" not in _u(s)]
                        if not valid: return None
                        stream_url = _u(valid[0])
                        return {
                            "name":    st.get("text","").strip(),
                            "url":     stream_url,
                            "country": st.get("subtext",""),
                            "bitrate": st.get("bitrate",""),
                            "codec":   st.get("formats",""),
                            "favicon": st.get("image",""),
                        }
                    except Exception: return None

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    resolved = list(ex.map(_resolve, stations))
                self._json([r for r in resolved if r])
            except Exception as e:
                self._json({"error": str(e)})

        elif path == "/api/stations":
            self._json(self.server_state.store.list_stations())

        elif path == "/api/stations/delete":
            sid = qs.get("id",[""])[0]
            self.server_state.store.delete_station(sid)
            self._json({"ok":True})

        elif path == "/api/stations/play":
            host = qs.get("host",[None])[0]
            sid  = qs.get("id",[""])[0]
            dev  = self.server_state.get_device(host)
            st   = self.server_state.store.get_station(sid)
            if dev and st:
                if dev.has_local_internet_radio():
                    local_ip = get_local_ip()
                    loc = f"http://{local_ip}:{self.server_state.web_port}/api/station-desc/{sid}"
                    dev.select_content("LOCAL_INTERNET_RADIO", "stationurl", loc, st["name"])
                else:
                    # Speaker lacks LOCAL_INTERNET_RADIO — push via UPnP AVTransport
                    dev.play_via_avt(self.server_state.dlna.stream_url(sid))
                self._json({"ok":True})
            else:
                self._json({"ok":False})

        elif path == "/api/stations/set-preset":
            host = qs.get("host",[None])[0]
            sid  = qs.get("id",[""])[0]
            slot = qs.get("slot",["1"])[0]
            dev  = self.server_state.get_device(host)
            st   = self.server_state.store.get_station(sid)
            if dev and st:
                if dev.has_local_internet_radio():
                    local_ip = get_local_ip()
                    loc = f"http://{local_ip}:{self.server_state.web_port}/api/station-desc/{sid}"
                    dev.store_preset(slot, st["name"], "LOCAL_INTERNET_RADIO", "stationurl", loc)
                else:
                    # Store as UPNP preset pointing at our HTTP stream redirect
                    dev.store_preset(slot, st["name"], "UPNP", "",
                                     self.server_state.dlna.stream_url(sid), "UPnPUserName")
                self._json({"ok":True})
            else:
                self._json({"ok":False})

        # ── group / multi-room ─────────────────────────────────────────────────
        elif path == "/api/group":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            self._json(dev.get_zone() if dev else {"error":"no_device"})

        elif path == "/api/group/create":
            master_host = qs.get("master",[None])[0]
            raw_slaves  = qs.get("slaves",[""])[0]
            slave_hosts = [h for h in raw_slaves.split(",") if h]
            master_dev  = self.server_state.get_device(master_host)
            if not master_dev:
                self._json({"ok":False,"error":"no_master"})
            else:
                slave_devs = [self.server_state.get_device(h)
                              for h in slave_hosts]
                slave_devs = [d for d in slave_devs if d]
                master_dev.set_zone(slave_devs)
                for d in [master_dev] + slave_devs: d.invalidate_zone_cache()
                self._json({"ok":True})

        elif path == "/api/group/remove":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if dev:
                ok = dev.remove_zone()
                for d in list(self.server_state.devices): d.invalidate_zone_cache()
                self._json({"ok": bool(ok)})
            else:
                self._json({"ok":False,"error":"no_device"})

        # ── group helpers for Matter / Alexa ───────────────────────────────────

        elif path == "/api/group/party":
            # Join ALL speakers into one group. The currently-playing speaker
            # becomes master; if none is playing, use the first speaker.
            devices = list(self.server_state.devices)
            if len(devices) < 2:
                self._json({"ok": False, "error": "need_two_speakers"})
            else:
                master = None
                for d in devices:
                    try:
                        st = d.state()
                        if st.get("playStatus") not in ("STOP_STATE", None, ""):
                            master = d; break
                    except Exception:
                        pass
                if master is None:
                    master = devices[0]
                slaves = [d for d in devices if d is not master]
                master.set_zone(slaves)
                for d in devices: d.invalidate_zone_cache()
                log.info(f"[GROUP] Party mode — master={master.host} "
                         f"slaves={[d.host for d in slaves]}")
                self._json({"ok": True, "master": master.host,
                            "slaves": [d.host for d in slaves]})

        elif path == "/api/group/dissolve-all":
            # Dissolve every active group across all speakers.
            devices = list(self.server_state.devices)
            dissolved = []
            for d in devices:
                try:
                    zinfo = d.get_zone()
                    if zinfo.get("is_master"):
                        d.remove_zone()
                        dissolved.append(d.host)
                except Exception:
                    pass
            for d in devices: d.invalidate_zone_cache()
            log.info(f"[GROUP] Dissolved groups on: {dissolved}")
            self._json({"ok": True, "dissolved": dissolved})

        elif path == "/api/group/join":
            # Add a specific speaker to the current group. If no zone exists,
            # the currently-playing speaker becomes master with host as slave.
            host    = qs.get("host", [None])[0]
            target  = self.server_state.get_device(host)
            if not target:
                self._json({"ok": False, "error": "no_device"}); return

            devices = list(self.server_state.devices)
            # Find existing group master
            master = None
            existing_slaves = []
            for d in devices:
                try:
                    zinfo = d.get_zone()
                    if zinfo.get("is_master"):
                        master = d
                        existing_slaves = [
                            self.server_state.get_device(m["ip"])
                            for m in zinfo.get("members", [])
                            if m["ip"] != d.host
                        ]
                        existing_slaves = [s for s in existing_slaves if s]
                        break
                except Exception:
                    pass

            if master is None:
                # No existing group — find a playing speaker to be master
                for d in devices:
                    if d is target:
                        continue
                    try:
                        st = d.state()
                        if st.get("playStatus") not in ("STOP_STATE", None, ""):
                            master = d; break
                    except Exception:
                        pass
                if master is None:
                    # Fall back to first speaker that isn't the target
                    others = [d for d in devices if d is not target]
                    master = others[0] if others else None

            if master is None:
                self._json({"ok": False, "error": "no_master_found"})
            elif target.host == master.host:
                self._json({"ok": False, "error": "target_is_master"})
            else:
                # Add target to slaves if not already present
                slave_hosts = {d.host for d in existing_slaves}
                if target.host not in slave_hosts:
                    existing_slaves.append(target)
                master.set_zone(existing_slaves)
                for d in devices: d.invalidate_zone_cache()
                log.info(f"[GROUP] Join — master={master.host} "
                         f"slaves={[d.host for d in existing_slaves]}")
                self._json({"ok": True, "master": master.host,
                            "slaves": [d.host for d in existing_slaves]})

        # ── device detail info ────────────────────────────────────────────────
        elif path == "/api/device-info":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev:
                self._json({"error": "no_device"})
            else:
                self._json(dev.detail_info())

        # ── bass ─────────────────────────────────────────────────────────────
        elif path == "/api/bass":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev: self._json({"error":"no_device"})
            else:
                caps = dev.get_bass_capabilities()
                if "current" not in caps: caps["current"] = dev.get_bass()
                self._json(caps)

        # ── soundbar tone / speaker levels (bass has its own slider) ─────────
        elif path in ("/api/audio-controls", "/api/audio-controls/set"):
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev: self._json({"error":"no_device"})
            elif path.endswith("/set"):
                group = qs.get("group",[""])[0]
                name  = qs.get("name", [""])[0]
                value = qs.get("value",[""])[0]
                try:
                    ok = (group in dev.AUDIO_CONTROL_PATHS and
                          dev.set_audio_control(group, name, int(value)))
                except ValueError:
                    ok = False
                self._json({"ok": bool(ok)})
            else:
                self._json({g: dev.get_audio_controls(g) for g in dev.AUDIO_CONTROL_PATHS})

        # ── soundbar settings (AV delay, auto-off, CEC, attached speakers) ───
        elif path in ("/api/soundbar", "/api/soundbar/set"):
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if not dev: self._json({"error":"no_device"})
            elif path.endswith("/set"):
                try:
                    ok = dev.set_soundbar_setting(qs.get("name",[""])[0], qs.get("value",[""])[0])
                except ValueError:
                    ok = False
                self._json({"ok": bool(ok), **dev.get_soundbar_settings()})
            else:
                self._json(dev.get_soundbar_settings())

        # ── save what's playing as a preset ───────────────────────────────────
        elif path == "/api/presets/save-current":
            st   = self.server_state
            dev  = st.get_device(qs.get("host",[None])[0])
            slot = qs.get("slot",[""])[0]
            to_all = qs.get("all",["false"])[0].lower() == "true"
            item = dev.now_playing_item() if dev else None
            if not dev or not slot.isdigit() or not 1 <= int(slot) <= 6:
                self._json({"ok": False, "error": "bad request"})
            elif not item:
                self._json({"ok": False, "error": "Nothing that can be saved is playing"})
            else:
                targets = [dev] + ([d for d in st.devices if d is not dev] if to_all else [])
                results = {}
                for d in targets:
                    ok, why = (True, "") if d is dev else preset_target_ok(item, d.get_sources())
                    if not ok:
                        results[d.name] = f"skipped ({why})"
                        continue
                    if d.store_preset(slot, item["name"], item["source"], item["type"],
                                      item["location"], item["account"], item["art"]):
                        d.invalidate_preset_cache()
                        # Keep the backup in step so a restore doesn't undo it
                        st.store.backup_presets(d.host, d.get_presets_detail())
                        d.has_backup = True
                        results[d.name] = "saved"
                    else:
                        results[d.name] = "failed"
                log.info(f"[PRESET] saved '{item['name']}' ({item['source']}) to slot {slot}: {results}")
                self._json({"ok": True, "name": item["name"], "slot": int(slot), "results": results})

        # ── Spotify (see the Spotify section above) ───────────────────────────
        elif path.startswith("/api/spotify/"):
            self._spotify(path[len("/api/spotify/"):], qs)

        # ── reboot ────────────────────────────────────────────────────────────
        elif path == "/api/reboot":
            dev = self.server_state.get_device(qs.get("host",[None])[0])
            if not dev:
                self._json({"ok": False, "error": "no_device"})
            elif self.server_state.reboot_device(dev):
                self._json({"ok": True, "device_id": dev.device_id})
            else:
                self._json({"ok": False, "error": "speaker didn't accept the reboot"})

        elif path in ("/api/maintenance", "/api/maintenance/set"):
            ms = self.server_state.maintenance_store
            if path.endswith("/set"):
                fields = {}
                if "enabled" in qs: fields["enabled"] = qs["enabled"][0].lower() == "true"
                if "day" in qs and qs["day"][0].isdigit() and 0 <= int(qs["day"][0]) <= 6:
                    fields["day"] = int(qs["day"][0])
                if "time" in qs and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", qs["time"][0]):
                    fields["time"] = qs["time"][0]
                if "tz" in qs:
                    try: ZoneInfo(qs["tz"][0]); fields["tz"] = qs["tz"][0]
                    except (ZoneInfoNotFoundError, ValueError): pass
                ms.update(**fields)
                log.info(f"[MAINT] settings → {ms.get()}")
            cfg = ms.get()
            cfg["speakers"] = [d.name for d in self.server_state.devices if d.has_clock()]
            self._json(cfg)

        elif path == "/api/maintenance/run":
            threading.Thread(target=self.server_state.restart_clock_speakers,
                             args=("manual run",), daemon=True).start()
            self._json({"ok": True})

        elif path == "/api/reboot/status":
            did = qs.get("device_id",[""])[0]
            self._json(self.server_state.reboot_status.get(did, {"state": "unknown"}))

        # ── dialogue mode (soundbars) ─────────────────────────────────────────
        elif path in ("/api/audio-mode", "/api/audio-mode/set", "/api/audio-mode/auto"):
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            st   = self.server_state
            if not dev or not dev.supports_dialog_mode():
                self._json({"supported": False})
            else:
                if path.endswith("/set"):
                    mode = qs.get("mode",[""])[0]
                    if mode in ("dialog", "normal"):
                        st.cancel_audio_mode_pending(dev.host)
                        dev.set_audio_mode(mode)
                elif path.endswith("/auto"):
                    enabled = qs.get("enabled",["true"])[0].lower() == "true"
                    st.audio_mode_store.set_auto(dev, enabled)
                    log.info(f"[DSP] {dev.host} auto dialogue mode on TV: {enabled}")
                self._json({"supported": True, "mode": dev.get_audio_mode(),
                            "auto_tv": st.audio_mode_store.auto_enabled(dev)})

        # ── sources ───────────────────────────────────────────────────────────
        elif path == "/api/sources":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            self._json(dev.get_sources() if dev else [])

        elif path == "/api/select":
            host    = qs.get("host",   [None])[0]
            source  = qs.get("source", [""])[0]
            account = qs.get("account",[""])[0]
            dev     = self.server_state.get_device(host)
            self._json({"ok": bool(dev and source and dev.select_source(source, account))})

        # ── rename ────────────────────────────────────────────────────────────
        elif path == "/api/rename":
            host = qs.get("host",[None])[0]
            name = qs.get("name",[""])[0].strip()
            dev  = self.server_state.get_device(host)
            if dev and name:
                dev.set_name(name); dev.name = name
                self._json({"ok":True,"name":name})
            else:
                self._json({"ok":False})

        # ── backup all speakers ───────────────────────────────────────────────
        elif path == "/api/presets/backup-all":
            results = []
            for dev in list(self.server_state.devices):
                try:
                    dev.invalidate_preset_cache()
                    presets = dev.get_presets_detail()
                    data    = self.server_state.store.backup_presets(dev.host, presets)
                    dev.has_backup = True
                    results.append({"host":dev.host,"name":dev.name,"ok":True,"count":len(presets)})
                except Exception as e:
                    results.append({"host":dev.host,"name":dev.name,"ok":False,"error":str(e)})
            self._json({"results":results})

        # ── Matter bridge QR code ─────────────────────────────────────────────
        elif path == "/api/matter/qr":
            try:
                r = requests.get("http://localhost:8889/qr", timeout=3)
                self._respond(200, "application/json", r.content)
            except Exception as e:
                self._json({"error": str(e), "qrPairingCode": None,
                            "manualPairingCode": None, "commissioned": False, "qrText": None})

        # ── station descriptor (fetched by the speaker itself) ────────────────
        elif path.startswith("/api/station-desc/"):
            sid = path.split("/")[-1]
            desc = self.server_state.store.station_descriptor(sid)
            if desc:
                self._respond(200, "application/json", desc.encode())
            else:
                self._respond(404, "text/plain", b"Station not found")

        # ── all-speaker volume ────────────────────────────────────────────────
        elif path == "/api/volume/all":
            value = qs.get("value", [None])[0]
            if value:
                for dev in list(self.server_state.devices):
                    try: dev.set_volume(value)
                    except Exception: pass
            self._json({"ok": bool(value)})

        # ── scenes ────────────────────────────────────────────────────────────
        elif path == "/api/scenes":
            self._json(self.server_state.scene_store.list_scenes())

        elif path == "/api/scenes/delete":
            sid = qs.get("id", [""])[0]
            self.server_state.scene_store.delete(sid)
            self._json({"ok": True})

        elif path == "/api/scenes/activate":
            sid   = qs.get("id", [""])[0]
            scene = self.server_state.scene_store.load(sid)
            if not scene:
                self._json({"ok": False, "error": "not_found"})
            else:
                master_host = scene.get("master")
                slave_hosts = scene.get("slaves", [])
                master_dev  = self.server_state.get_device(master_host)
                if not master_dev:
                    self._json({"ok": False, "error": "master_not_found"})
                else:
                    slave_devs = [self.server_state.get_device(h) for h in slave_hosts]
                    slave_devs = [d for d in slave_devs if d]
                    if slave_devs:
                        master_dev.set_zone(slave_devs)
                        for d in [master_dev] + slave_devs: d.invalidate_zone_cache()
                    for host, vol in scene.get("volumes", {}).items():
                        d = self.server_state.get_device(host)
                        if d: d.set_volume(vol)
                    time.sleep(0.3)
                    master_dev.preset(scene.get("preset", 1))
                    log.info(f"[SCENE] Activated '{scene.get('name')}' on {master_host}")
                    self._json({"ok": True})

        # ── alarms ────────────────────────────────────────────────────────────
        elif path == "/api/alarms":
            # Add each alarm's current preset name/source (looked up live, so
            # the card follows a preset that's been re-saved since)
            alarms = self.server_state.alarm_store.list_alarms()
            for a in alarms:
                dev = self.server_state.get_device(a.get("host"))
                if dev is None and a.get("device_id"):
                    dev = next((d for d in self.server_state.devices
                                if d.device_id == a["device_id"]), None)
                if dev:
                    a["host"] = dev.host   # current address, in case DHCP moved it
                p = next((x for x in (dev.get_presets_detail() if dev else [])
                          if x.get("id") == str(a.get("preset"))), None)
                if p and p.get("name"):
                    a["preset_name"], a["preset_source"] = p["name"], p.get("source", "")
            self._json(alarms)

        elif path == "/api/alarms/delete":
            aid = qs.get("id", [""])[0]
            self.server_state.alarm_store.delete_alarm(aid)
            self._json({"ok": True})

        elif path == "/api/alarms/toggle":
            aid     = qs.get("id", [""])[0]
            enabled = qs.get("enabled", ["true"])[0].lower() == "true"
            self.server_state.alarm_store.toggle_alarm(aid, enabled)
            self._json({"ok": True})

        # ── PWA manifest + service worker + icons ─────────────────────────────
        elif path == "/manifest.json":
            icons = [
                {"src": "/icon.svg",     "type": "image/svg+xml",
                 "sizes": "any",         "purpose": "any"},
                {"src": "/icon-192.png", "type": "image/png",
                 "sizes": "192x192",     "purpose": "any"},
                {"src": "/icon-512.png", "type": "image/png",
                 "sizes": "512x512",     "purpose": "maskable"},
            ]
            manifest = {
                "name": "SoundTouch", "short_name": "SoundTouch",
                "description": "Bose SoundTouch local controller",
                "start_url": "/", "display": "standalone",
                "orientation": "portrait",
                "background_color": "#0b0c11", "theme_color": "#0b0c11",
                "icons": icons,
            }
            self._respond(200, "application/manifest+json",
                          json.dumps(manifest).encode())

        elif path == "/sw.js":
            self._web("sw.js")

        elif path == "/icon.svg":
            self._respond(200, "image/svg+xml", ICON_SVG.encode())

        elif path in ("/icon-192.png", "/icon-512.png"):
            size = 512 if "512" in path else 192
            data = _make_icon_png(size)
            if data:
                self._respond(200, "image/png", data)
            else:
                # Pillow unavailable — redirect to SVG
                self.send_response(302)
                self.send_header("Location", "/icon.svg")
                self.end_headers()

        elif path.startswith("/api/tts/desc/"):
            audio_id = path.split("/")[-1]
            if audio_id in _tts_cache:
                mp3_url = (f"http://{get_local_ip()}:{self.server_state.web_port}"
                           f"/api/tts/audio/{audio_id}.mp3")
                desc = json.dumps({
                    "name": "Announcement",
                    "imageUrl": "",
                    "streamType": "liveRadio",
                    "audio": {"streamUrl": mp3_url, "hasPlaylist": False, "isRealtime": False},
                })
                self._respond(200, "application/json", desc.encode())
            else:
                self._respond(404, "text/plain", b"TTS descriptor not found")

        elif path.startswith("/api/tts/audio/"):
            audio_id = path.split("/")[-1].replace(".mp3", "")
            data = _tts_cache.get(audio_id)
            if data:
                self._respond(200, "audio/mpeg", data)
            else:
                self._respond(404, "text/plain", b"TTS audio not found")

        elif path == "/api/tts/status":
            self._json({"available": _TTS_AVAILABLE})

        # ── DLNA / UPnP ──────────────────────────────────────────────────────
        elif path == "/dlna/device.xml":
            self._respond(200, "text/xml", self.server_state.dlna.device_xml())

        elif path == "/dlna/cd.xml":
            self._respond(200, "text/xml", self.server_state.dlna.cd_scpd_xml())

        elif path.startswith("/dlna/stream/"):
            sid = path.split("/")[-1]
            st  = self.server_state.store.get_station(sid)
            if st and st.get("stream_url"):
                self.send_response(302)
                self.send_header("Location", st["stream_url"])
                self.end_headers()
            else:
                self._respond(404, "text/plain", b"Station not found")

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if not self._request_allowed():
            self._respond(403, "text/plain", b"Forbidden: host not allowed")
            return
        p    = urlparse(self.path)
        path = p.path
        qs   = parse_qs(p.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if path.startswith("/api/"):
            log.info(f"[API POST] {path}  body={body[:300].decode('utf-8','replace')}")

        if path == "/dlna/cd/control":
            resp = self.server_state.dlna.handle_soap(body)
            self._respond(200, 'text/xml; charset="utf-8"', resp)
            return

        elif path == "/api/stations/add":
            try:
                data = json.loads(body)
                name = data.get("name","").strip()
                url  = data.get("stream_url","").strip()
                art  = data.get("art_url","").strip()
                sid  = name.lower().replace(" ","_").replace("/","_")[:32]
                # Ensure unique ID
                existing = [s["id"] for s in self.server_state.store.list_stations()]
                base = sid
                n = 1
                while sid in existing:
                    sid = f"{base}_{n}"; n += 1
                self.server_state.store.save_station(sid, name, url, art)
                self._json({"ok":True,"id":sid})
            except Exception as e:
                self._json({"ok":False,"error":str(e)})

        elif path == "/api/presets/backup-json":
            host = qs.get("host", [None])[0]
            try:
                data = json.loads(body)
                if "presets" not in data:
                    self._json({"ok": False, "error": "invalid: missing 'presets' key"})
                else:
                    self.server_state.store.backup_presets_raw(host, data)
                    dev = self.server_state.get_device(host)
                    if dev: dev.has_backup = True
                    self._json({"ok": True})
            except json.JSONDecodeError as e:
                self._json({"ok": False, "error": f"Invalid JSON: {e}"})

        elif path == "/api/scenes":
            try:
                data = json.loads(body)
                name = data.get("name", "").strip()
                if not name:
                    self._json({"ok": False, "error": "name required"})
                else:
                    safe = re.sub(r"[^a-z0-9]+", "_", name.lower())[:20].strip("_")
                    sid = "scene_" + safe + "_" + str(int(time.time()))[-5:]
                    scene = {
                        "id":      sid,
                        "name":    name,
                        "master":  data.get("master"),
                        "slaves":  data.get("slaves", []),
                        "volumes": data.get("volumes", {}),
                        "preset":  int(data.get("preset", 1)),
                        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    self.server_state.scene_store.save(sid, scene)
                    log.info(f"[SCENE] Saved '{name}'")
                    self._json({"ok": True, "id": sid})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif path == "/api/alarms":
            try:
                data    = json.loads(body)
                # Editing: keep the alarm's id, on/off state and ring history
                existing = next((a for a in self.server_state.alarm_store.list_alarms()
                                 if data.get("id") and a["id"] == data["id"]), None)
                alarm_id = existing["id"] if existing else "alarm_" + str(int(time.time()))
                alarm = {
                    "id":      alarm_id,
                    "name":    data.get("name", "Alarm").strip() or "Alarm",
                    "host":    data.get("host"),
                    "preset":  int(data.get("preset", 1)),
                    "time":    data.get("time", "07:00"),
                    "days":    [int(d) for d in data.get("days", list(range(7)))],
                    "enabled": existing.get("enabled", True) if existing else True,
                    "volume":  int(data["volume"]) if data.get("volume") not in (None, "") else None,
                }
                if existing:
                    for k in ("last_fired", "last_result"):
                        if k in existing: alarm[k] = existing[k]
                # The phone's timezone — the server clock is UTC
                tz = (data.get("tz") or "").strip()
                if tz:
                    try: ZoneInfo(tz); alarm["tz"] = tz
                    except (ZoneInfoNotFoundError, ValueError): pass
                dev = self.server_state.get_device(alarm["host"])
                if dev and dev.device_id:
                    alarm["device_id"] = dev.device_id
                self.server_state.alarm_store.save_alarm(alarm)
                log.info(f"[ALARM] {'Updated' if existing else 'Saved'} '{alarm['name']}' at {alarm['time']}")
                self._json({"ok": True, "id": alarm_id})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif path == "/api/tts/announce":
            try:
                data    = json.loads(body)
                text    = data.get("text", "").strip()
                hosts   = data.get("hosts", [])
                volume  = int(data.get("volume", 60))
                if not text:
                    self._json({"ok": False, "error": "no text"})
                elif not _TTS_AVAILABLE:
                    self._json({"ok": False, "error": "gTTS not installed — run: pip3 install gtts"})
                else:
                    devices = [d for d in self.server_state.devices if d.host in hosts]
                    if not devices:
                        self._json({"ok": False, "error": "no matching speakers"})
                    else:
                        # Debounce: ignore duplicate within 3 seconds (lock prevents race)
                        dedup_key = (text, ",".join(sorted(hosts)))
                        now = time.monotonic()
                        with _tts_lock:
                            duplicate = now - _tts_last.get(dedup_key, 0) < 3.0
                            if not duplicate:
                                _tts_last[dedup_key] = now
                        if duplicate:
                            self._json({"ok": True, "speakers": len(devices), "deduped": True})
                        else:
                            threading.Thread(
                                target=_tts_announce,
                                args=(devices, text, volume, self.server_state.web_port),
                                daemon=True
                            ).start()
                            self._json({"ok": True, "speakers": len(devices)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_SUBSCRIBE(self):
        # UPnP eventing stub — acknowledge so the speaker doesn't retry
        self.send_response(200)
        self.send_header("SID", f"uuid:{_uuid.uuid4()}")
        self.send_header("TIMEOUT", "Second-1800")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        self.send_response(200)
        self.end_headers()

    def _spotify_save_preset(self, sp, arg):
        """Store a Spotify URI into preset slot N on one speaker, or every
        speaker linked to the account (same rule as "Save what's playing")."""
        st   = self.server_state
        uid  = sp.account_for(arg("account") or None)
        dev  = st.get_device(arg("host"))
        slot = arg("slot")
        uri  = arg("uri")
        if not dev or not slot.isdigit() or not 1 <= int(slot) <= 6 or not uri.startswith("spotify:"):
            raise SpotifyError("Bad preset request")
        item = {"source": "SPOTIFY", "type": "tracklisturl", "location": spotify_location(uri),
                "account": uid, "name": arg("name") or "Spotify", "art": arg("image")}
        targets = [dev] + ([d for d in st.devices if d is not dev] if arg("all") == "true" else [])
        results = {}
        for d in targets:
            ok, why = preset_target_ok(item, d.get_sources())
            if not ok:
                results[d.name] = f"skipped ({why})"
                continue
            if d.store_preset(slot, item["name"], item["source"], item["type"],
                              item["location"], item["account"], item["art"]):
                d.invalidate_preset_cache()
                st.store.backup_presets(d.host, d.get_presets_detail())
                d.has_backup = True
                results[d.name] = "saved"
            else:
                results[d.name] = "failed"
        log.info(f"[SPOTIFY] saved {uri} to preset {slot}: {results}")
        return {"slot": int(slot), "name": item["name"], "results": results}

    def _spotify(self, action, qs):
        """GET /api/spotify/<action>. Errors come back as {ok: false, error}
        with a message fit for a toast; tokens never leave the controller."""
        sp  = self.server_state.spotify
        arg = lambda k, d="": qs.get(k, [d])[0]
        try:
            if action == "accounts":
                self._json({"ok": True, "client_id_set": bool(sp.store.client_id()),
                            "accounts": sp.store.accounts(),
                            "speakers": [{"host": d.host, "name": d.name,
                                          "accounts": [s["sourceAccount"] for s in d.get_sources()
                                                       if s["source"] == "SPOTIFY" and s["status"] == "READY"]}
                                         for d in self.server_state.devices]})
            elif action == "login":
                self._json({"ok": True, "url": sp.login_url()})
            elif action == "link":
                self._json({"ok": True, "account": sp.link(arg("url"))})
            elif action == "unlink":
                sp.store.remove(arg("account")); self._json({"ok": True})
            elif action == "home":
                self._json({"ok": True, **sp.home(arg("account") or None)})
            elif action == "search":
                q = arg("q").strip()
                self._json({"ok": True, **(sp.search(q, arg("type"), arg("account") or None) if q else {})})
            elif action == "item":
                self._json({"ok": True, **sp.item(arg("uri"), arg("account") or None)})
            elif action == "save-preset":
                self._json({"ok": True, **self._spotify_save_preset(sp, arg)})
            elif action == "play":
                hosts = [h for h in arg("hosts", arg("host")).split(",") if h]
                devs  = [d for d in (self.server_state.get_device(h) for h in hosts) if d]
                if len(devs) != len(hosts):
                    raise SpotifyError("Speaker not found — try Discover Speakers")
                names = sp.play(arg("uri"), devs, arg("account") or None, arg("offset") or None)
                self._json({"ok": True, "playing_on": names})
            else:
                self._json({"ok": False, "error": "unknown Spotify action"})
        except SpotifyError as e:
            self._json({"ok": False, "error": str(e)})
        except requests.RequestException as e:
            log.warning(f"[SPOTIFY] {action}: {e}")
            self._json({"ok": False, "error": "Couldn't reach Spotify or the speaker"})

    def _json(self, obj):
        payload = json.dumps(obj)
        p = urlparse(self.path).path
        lvl = logging.DEBUG if p == "/api/state" else logging.INFO
        log.log(lvl, f"[API RESP] {p} → {payload[:400]}")
        self._respond(200, "application/json", payload.encode())

    def _html(self, s):
        self._respond(200, "text/html; charset=utf-8", s.encode())

    def _web(self, name):
        """Serve a static UI asset from the web/ directory (cached)."""
        try:
            body, ctype = web_asset(name)
        except FileNotFoundError:
            self._respond(404, "text/plain", b"Not found")
            return
        # no-cache: the browser may keep a copy but must check it's current, so
        # a deploy reaches phones on the next open
        self._respond(200, ctype, body, extra={"Cache-Control": "no-cache"})

    def _respond(self, code, ctype, body, extra=None):
        if code >= 400:
            log.warning(f"[API RESP] {code} {ctype}  {body[:200].decode('utf-8','replace')}")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


# ═══════════════════════════════════════════════════════════════════════════════
# Spotify — browse via the Web API, play by logging the speaker in
# ═══════════════════════════════════════════════════════════════════════════════
#
# A speaker can only play Spotify while its Connect ZeroConf endpoint (port
# 8200) reports an activeUser. That login normally comes from a phone casting
# to it, and a restart clears it, while /sources keeps claiming "READY". So
# before playing, the controller pushes a fresh login itself (ZeroConf addUser
# with the account's access token). The speaker then appears in that account's
# Connect device list and the Web API starts playback on it. The same steps
# work for any linked account (both halves of a Spotify Duo).
#
# Login is OAuth PKCE with a paste-back: Spotify only accepts https or
# loopback redirect URIs, so it sends the phone to http://127.0.0.1:8888/…
# (which doesn't load) and the user pastes that address back into the app.

SPOTIFY_DIR      = DATA_DIR / "spotify"
SPOTIFY_REDIRECT = "http://127.0.0.1:8888/api/spotify/callback"
SPOTIFY_SCOPES   = ("playlist-read-private user-library-read user-read-recently-played "
                    "user-read-private streaming user-read-playback-state user-modify-playback-state")
SPOTIFY_LOGIN_MAX_AGE = 45 * 60   # re-push a speaker login before its 1 h token can lapse
SPOTIFY = None                    # the AppState's SpotifyClient (play_preset uses it)


class SpotifyError(Exception):
    """A Spotify call failed; the message is safe to show the user."""


def spotify_location(uri):
    """spotify:playlist:abc → the ContentItem location SoundTouch presets use."""
    return "/playback/container/" + base64.b64encode(uri.encode()).decode()


def spotify_uri_from_location(location):
    """Inverse of spotify_location(); None if it isn't one."""
    pfx = "/playback/container/"
    if not location or not location.startswith(pfx):
        return None
    try:
        uri = base64.b64decode(location[len(pfx):]).decode()
    except Exception:
        return None
    return uri if uri.startswith("spotify:") else None


def _write_private(path, obj):
    """Atomic JSON write readable only by this user (tokens live here)."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, indent=2))
    os.replace(tmp, path)


class SpotifyAccountStore:
    """Linked Spotify accounts, one file each: data/spotify/<user_id>.json.
    The app's Client ID comes from $SPOTIFY_CLIENT_ID or data/spotify/app.json
    (PKCE — there is no client secret)."""

    def __init__(self, directory=SPOTIFY_DIR):
        self._dir  = pathlib.Path(directory)
        self._lock = threading.Lock()

    def client_id(self):
        cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
        if cid:
            return cid
        try: return json.loads((self._dir / "app.json").read_text()).get("client_id", "")
        except Exception: return ""

    def _path(self, user_id):
        if not re.fullmatch(r"[\w.\-]+", user_id or ""):
            raise SpotifyError("bad account id")
        return self._dir / f"{user_id}.json"

    def accounts(self):
        out = []
        for p in sorted(self._dir.glob("*.json")) if self._dir.exists() else []:
            if p.stem in ("app", "pending"):
                continue
            try: rec = json.loads(p.read_text())
            except Exception: continue
            out.append({"user_id": rec["user_id"], "display_name": rec.get("display_name") or rec["user_id"],
                        "product": rec.get("product", ""), "image": rec.get("image", "")})
        return out

    def get(self, user_id):
        try: return json.loads(self._path(user_id).read_text())
        except FileNotFoundError: return None

    def save(self, rec):
        with self._lock:
            _write_private(self._path(rec["user_id"]), rec)

    def remove(self, user_id):
        with self._lock:
            try: self._path(user_id).unlink()
            except FileNotFoundError: pass


class SpotifyClient:
    """Web API access for linked accounts, plus "play this on that speaker"."""

    API  = "https://api.spotify.com/v1"
    AUTH = "https://accounts.spotify.com"

    def __init__(self, store, get_devices=None):
        self.store        = store
        self._get_devices = get_devices or (lambda: [])   # → list of SoundTouchDevice
        self._http        = requests.Session()
        self._pending     = {}    # login state → (code_verifier, created)
        self._cache       = {}    # (user_id, path) → (expires, data)
        self._pushed      = {}    # (host, user_id) → monotonic time of last addUser
        self._lock        = threading.Lock()

    # ── accounts / login ──────────────────────────────────────────────────────
    def default_account(self):
        accts = self.store.accounts()
        return accts[0]["user_id"] if accts else None

    def account_for(self, user_id):
        uid = user_id or self.default_account()
        if not uid or not self.store.get(uid):
            raise SpotifyError("No Spotify account linked — link one in Settings → Spotify")
        return uid

    def login_url(self):
        cid = self.store.client_id()
        if not cid:
            raise SpotifyError("No Spotify Client ID configured")
        verifier  = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state     = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            self._pending = {s: v for s, v in self._pending.items() if now - v[1] < 900}
            self._pending[state] = (verifier, now)
        q = {"client_id": cid, "response_type": "code", "redirect_uri": SPOTIFY_REDIRECT,
             "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
             "scope": SPOTIFY_SCOPES, "show_dialog": "true"}
        return f"{self.AUTH}/authorize?" + urlencode(q)

    def link(self, pasted_url):
        """Finish a login from the pasted callback address. Returns the account."""
        qs = parse_qs(urlparse((pasted_url or "").strip()).query)
        if qs.get("error"):
            raise SpotifyError(f"Spotify said: {qs['error'][0]}")
        code, state = qs.get("code", [""])[0], qs.get("state", [""])[0]
        with self._lock:
            pend = self._pending.pop(state, None)
        if not code or not pend:
            raise SpotifyError("That address doesn't match a login started here — start the login again")
        r = self._http.post(f"{self.AUTH}/api/token", timeout=15, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT,
            "client_id": self.store.client_id(), "code_verifier": pend[0]})
        if r.status_code != 200:
            raise SpotifyError("Spotify rejected the login — it may have expired, try again")
        tok = r.json()
        me = self._http.get(f"{self.API}/me", timeout=15,
                            headers={"Authorization": "Bearer " + tok["access_token"]}).json()
        rec = {"user_id": me["id"], "display_name": me.get("display_name") or me["id"],
               "product": me.get("product", ""), "image": _sp_image(me.get("images")),
               "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
               "expires_at": time.time() + tok.get("expires_in", 3600), "scope": tok.get("scope", "")}
        self.store.save(rec)
        log.info(f"[SPOTIFY] linked account {rec['user_id']} ({rec['product']})")
        return {k: rec[k] for k in ("user_id", "display_name", "product", "image")}

    def token(self, user_id, force=False):
        rec = self.store.get(user_id)
        if not rec:
            raise SpotifyError("Spotify account not linked")
        if force or time.time() > rec.get("expires_at", 0) - 60:
            r = self._http.post(f"{self.AUTH}/api/token", timeout=15, data={
                "grant_type": "refresh_token", "refresh_token": rec["refresh_token"],
                "client_id": self.store.client_id()})
            if r.status_code != 200:
                log.warning(f"[SPOTIFY] token refresh for {user_id} failed: {r.status_code}")
                raise SpotifyError("Spotify login has expired — link the account again in Settings")
            t = r.json()
            rec["access_token"], rec["expires_at"] = t["access_token"], time.time() + t.get("expires_in", 3600)
            if t.get("refresh_token"):
                rec["refresh_token"] = t["refresh_token"]
            self.store.save(rec)
        return rec["access_token"]

    # ── Web API ───────────────────────────────────────────────────────────────
    def api(self, user_id, path, method="GET", params=None, body=None, cache=0):
        key = (user_id, method, path, json.dumps(params, sort_keys=True))
        if cache and method == "GET":
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        r = None
        for attempt in range(3):
            force = r is not None and r.status_code == 401     # token rejected → refresh once
            r = self._http.request(method, self.API + path, params=params, json=body, timeout=15,
                                   headers={"Authorization": "Bearer " + self.token(user_id, force=force)})
            if r.status_code == 429 and attempt < 2:
                time.sleep(min(float(r.headers.get("Retry-After", 1)), 5))
                continue
            if r.status_code == 401 and attempt == 0:
                continue
            break
        if r.status_code >= 400:
            log.debug(f"[SPOTIFY] {method} {path} → {r.status_code} {r.text[:200]}")
            raise SpotifyError(f"Spotify {r.status_code} for {path.split('?')[0]}")
        data = r.json() if r.content and "json" in r.headers.get("content-type", "") else {}
        if cache and method == "GET":
            self._cache[key] = (time.monotonic() + cache, data)
        return data

    def oembed(self, uri):
        """Title + cover for any public Spotify URI, no auth. The Web API won't
        read Spotify-made playlists (Discover Weekly, mixes) for new apps; this
        still names them."""
        key = ("-", "oembed", uri, "")
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        out = {}
        try:
            r = self._http.get("https://open.spotify.com/oembed", timeout=8,
                               params={"url": f"https://open.spotify.com/{uri.split(':')[1]}/{uri.split(':')[-1]}"})
            if r.ok:
                j = r.json(); out = {"name": j.get("title", ""), "image": j.get("thumbnail_url", "")}
        except Exception:
            pass
        self._cache[key] = (time.monotonic() + 86400, out)
        return out

    # ── browse ────────────────────────────────────────────────────────────────
    def playlists(self, uid):
        out, offset = [], 0
        while offset < 1000:
            page = self.api(uid, "/me/playlists", params={"limit": 50, "offset": offset}, cache=300)
            out += [_sp_card(p) for p in page.get("items", []) if p]
            if not page.get("next"):
                break
            offset += 50
        return out

    def home(self, user_id=None):
        uid = self.account_for(user_id)
        playlists = self.playlists(uid)
        by_uri = {p["uri"]: p for p in playlists}
        recent, seen = [], set()
        hist = self.api(uid, "/me/player/recently-played", params={"limit": 50}, cache=60)
        for it in hist.get("items", []):
            ctx = (it.get("context") or {}).get("uri")
            if not ctx or ctx in seen:
                continue
            seen.add(ctx)
            card = self._context_card(uid, ctx, by_uri)
            if card:
                recent.append(card)
            if len(recent) >= 10:
                break
        albums = [_sp_card(a.get("album")) for a in
                  self.api(uid, "/me/albums", params={"limit": 30}, cache=300).get("items", []) if a.get("album")]
        liked_total = self.api(uid, "/me/tracks", params={"limit": 1}, cache=300).get("total", 0)
        return {"account": uid, "recent": recent, "playlists": playlists, "albums": albums,
                "liked": {"type": "collection", "uri": f"spotify:user:{uid}:collection",
                          "name": "Liked Songs", "sub": f"{liked_total} songs", "image": ""}}

    def _context_card(self, uid, uri, playlists_by_uri):
        if uri in playlists_by_uri:
            return playlists_by_uri[uri]
        kind, sid = uri.split(":")[1], uri.split(":")[-1]
        if uri.endswith(":collection"):
            return {"type": "collection", "uri": uri, "name": "Liked Songs", "sub": "Your library", "image": ""}
        try:
            if kind in ("album", "artist", "playlist"):
                return _sp_card(self.api(uid, f"/{kind}s/{sid}", cache=3600))
        except SpotifyError:
            pass
        meta = self.oembed(uri)   # Spotify-made playlists: Web API 404s them
        return {"type": kind, "uri": uri, "name": meta.get("name") or "Spotify mix",
                "sub": "Made by Spotify", "image": meta.get("image", ""), "readonly": True} if meta else None

    def search(self, q, kind="", user_id=None):
        uid = self.account_for(user_id)
        types = kind if kind in ("track", "album", "artist", "playlist") else "track,album,artist,playlist"
        res = self.api(uid, "/search", params={"q": q, "type": types, "limit": 10}, cache=120)
        out = {}
        for key in ("artists", "albums", "tracks", "playlists"):
            items = (res.get(key) or {}).get("items") or []
            out[key] = [_sp_card(i) for i in items if i]   # Spotify returns nulls for filtered playlists
        return out

    def item(self, uri, user_id=None):
        """Detail page: card + tracks (+ albums for an artist)."""
        uid = self.account_for(user_id)
        parts = uri.split(":")
        kind, sid = parts[1], parts[-1]
        if uri.endswith(":collection"):
            page = self.api(uid, "/me/tracks", params={"limit": 50}, cache=120)
            tracks = [_sp_track(i.get("track") or i.get("item"), n) for n, i in enumerate(page.get("items", []))]
            return {"card": {"type": "collection", "uri": uri, "name": "Liked Songs",
                             "sub": f"{page.get('total', len(tracks))} songs", "image": ""}, "tracks": tracks}
        if kind == "album":
            a = self.api(uid, f"/albums/{sid}", cache=3600)
            tracks = [_sp_track({**t, "album": a}, n) for n, t in enumerate((a.get("tracks") or {}).get("items", []))]
            return {"card": _sp_card(a), "tracks": tracks}
        if kind == "artist":
            a = self.api(uid, f"/artists/{sid}", cache=3600)
            albums = self.api(uid, f"/artists/{sid}/albums",
                              params={"include_groups": "album,single", "limit": 10}, cache=3600)  # Feb 2026: max 10
            out = {"card": _sp_card(a), "tracks": [], "albums": [_sp_card(x) for x in albums.get("items", []) if x]}
            try:
                top = self.api(uid, f"/artists/{sid}/top-tracks", params={"market": "from_token"}, cache=3600)
                out["tracks"] = [_sp_track(t, n) for n, t in enumerate(top.get("tracks", []))]
            except SpotifyError:
                pass   # 403 for development-mode apps since Feb 2026 — albums still work
            return out
        if kind == "playlist":
            try:
                p = self.api(uid, f"/playlists/{sid}", cache=300)
            except SpotifyError:
                meta = self.oembed(uri)   # Spotify-made: playable, but no track list
                return {"card": {"type": "playlist", "uri": uri, "name": meta.get("name") or "Spotify mix",
                                 "sub": "Made by Spotify", "image": meta.get("image", ""), "readonly": True},
                        "tracks": []}
            items = self.api(uid, f"/playlists/{sid}/items", params={"limit": 100}, cache=300)
            tracks = [_sp_track(i.get("track") or i.get("item"), n) for n, i in enumerate(items.get("items", []))]
            return {"card": _sp_card(p), "tracks": [t for t in tracks if t]}
        if kind == "track":
            t = self.api(uid, f"/tracks/{sid}", cache=3600)
            return {"card": _sp_card(t), "tracks": [_sp_track(t, 0)]}
        raise SpotifyError("Unsupported Spotify link")

    # ── play ──────────────────────────────────────────────────────────────────
    def ensure_login(self, dev, user_id, force=False):
        """Make sure the speaker's Spotify is logged in as user_id (ZeroConf
        addUser). Re-pushed when it's someone else, nobody, or getting old."""
        base = f"http://{dev.host}:8200/zc"
        info = self._http.get(base, params={"action": "getInfo"}, timeout=5).json()
        fresh = time.monotonic() - self._pushed.get((dev.host, user_id), -1e9) < SPOTIFY_LOGIN_MAX_AGE
        if not force and info.get("activeUser") == user_id and fresh:
            return False
        r = self._http.post(base, timeout=10, data={
            "action": "addUser", "userName": user_id, "blob": self.token(user_id),
            "clientKey": info.get("clientID", ""), "tokenType": info.get("tokenType", "accesstoken")})
        ok = r.ok and r.json().get("status") == 101
        if not ok:
            raise SpotifyError(f"{dev.name} wouldn't accept the Spotify login")
        self._pushed[(dev.host, user_id)] = time.monotonic()
        log.info(f"[SPOTIFY] logged {dev.name} in as {user_id}")
        return True

    def _device_id(self, uid, dev, wait=10, active=False):
        """The speaker's Connect device id in this account's device list. Just
        after a login push the old listing can linger (play → 404), so callers
        that just logged in ask for it to be listed as active."""
        deadline = time.monotonic() + wait
        while True:
            devices = self.api(uid, "/me/player/devices").get("devices", [])
            hit = next((d for d in devices if d.get("name", "").strip().lower() == dev.name.strip().lower()
                        and (d.get("is_active") or not active)), None)
            if hit or time.monotonic() > deadline:
                return hit["id"] if hit else None
            time.sleep(1.5)

    def _play_body(self, uid, uri, offset=None):
        """Web API play body. Liked Songs has no context URI, so it's sent as a
        track list; a single song plays inside its album when we know it."""
        if uri.endswith(":collection"):
            page = self.api(uid, "/me/tracks", params={"limit": 50})
            uris = [(i.get("track") or i.get("item") or {}).get("uri") for i in page.get("items", [])]
            body = {"uris": [u for u in uris if u]}
        elif uri.startswith("spotify:track:"):
            body = {"uris": [uri]}
        else:
            body = {"context_uri": uri}
        if offset not in (None, ""):
            body["offset"] = {"position": int(offset)} if str(offset).isdigit() else {"uri": offset}
        return body

    def play(self, uri, devices, user_id=None, offset=None):
        """Play `uri` on one speaker, or several in sync (grouped under the
        first). Returns the speaker names it's playing on."""
        uid = self.account_for(user_id)
        if not devices:
            raise SpotifyError("No speaker chosen")
        master, slaves = devices[0], list(devices[1:])
        if slaves:
            master.set_zone(slaves)
            master.invalidate_zone_cache()
        body = self._play_body(uid, uri, offset)
        # 1 wake, 2 log in (only if needed), 3 play, 4 confirm. A second login
        # push resets the speaker's Spotify session, so it's the last resort.
        self._wake(master, uid)
        pushed = self.ensure_login(master, uid)
        # After a fresh login, prefer the listing once it's active (the old one
        # can linger and 404) but don't wait long — "active" often only flips
        # once playback starts, and the retries below cover a 404.
        dev_id = (self._device_id(uid, master, wait=4, active=True) if pushed else None) \
            or self._device_id(uid, master, wait=6)
        for attempt in (1, 2, 3):
            if not dev_id:
                dev_id = self._device_id(uid, master, wait=12)
            if not dev_id and attempt < 3:
                continue
            if not dev_id:
                raise SpotifyError(f"{master.name} didn't appear in Spotify — try again in a moment")
            try:
                self.api(uid, "/me/player/play", "PUT", params={"device_id": dev_id}, body=body)
            except SpotifyError:
                if attempt == 3:
                    raise
                if attempt == 2:          # still not found: fresh login, then wait for it
                    self.ensure_login(master, uid, force=True)
                    self._wake(master, uid)
                    dev_id = self._device_id(uid, master, wait=12, active=True)
                else:
                    time.sleep(2)
                    dev_id = self._device_id(uid, master, wait=8)
                continue
            if self._started(master, body):
                break
            log.info(f"[SPOTIFY] {master.name} accepted play but didn't start (attempt {attempt})")
            if attempt == 3:
                raise SpotifyError(f"{master.name} didn't start playing — try again")
        log.info(f"[SPOTIFY] playing {uri} on {[d.name for d in devices]} as {uid}")
        return [d.name for d in devices]

    @staticmethod
    def _source(dev):
        np = dev._get("/now_playing") if hasattr(dev, "_get") else None
        return (np.get("source"), np.get("playStatus") or np.findtext("playStatus") or "") if np is not None else (None, "")

    def _wake(self, dev, uid):
        """A speaker in standby is still listed in Spotify, but a Web API play
        is accepted and then ignored. Wake it onto its Spotify input first
        (not POWER, which would resume radio at full whack)."""
        if self._source(dev)[0] != "STANDBY":
            return
        dev.select_source("SPOTIFY", uid)
        for _ in range(12):
            time.sleep(0.75)
            if self._source(dev)[0] not in ("STANDBY", None):
                return

    def _started(self, dev, body, wait=8):
        """True once the speaker is playing *what we asked for*. Just "Spotify
        is playing" isn't enough: waking a speaker resumes its last Spotify
        context, which would pass for success. Contexts are matched on the
        ContentItem location (base64 of the context URI), track lists on
        the current trackID."""
        if not hasattr(dev, "_get"):
            return True
        want_ctx, want_tracks = body.get("context_uri"), set(body.get("uris") or [])
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(1)
            np = dev._get("/now_playing")
            if np is None or np.get("source") != "SPOTIFY":
                continue
            if (np.get("playStatus") or np.findtext("playStatus") or "") not in ("PLAY_STATE", "BUFFERING_STATE"):
                continue
            ci = np.find("ContentItem")
            ctx = spotify_uri_from_location(ci.get("location") if ci is not None else "")
            if want_ctx and ctx == want_ctx:
                return True
            if want_tracks and np.findtext("trackID") in want_tracks:
                return True
        return False

    def keep_speakers_logged_in(self):
        """Background (every few minutes): keep each speaker's Spotify login
        fresh so a tap plays in ~2 s instead of waiting ~10 s for a login push.

        Only the account the speaker is linked to (per /sources) is pushed, and
        only when the speaker has no login, or has that same login and our last
        push is getting old. A speaker logged in as someone else (e.g. the other
        Duo account casting from a phone) is never touched, and neither is one
        that's playing Spotify — a push resets its Spotify session."""
        linked = {a["user_id"] for a in self.store.accounts()}
        if not linked:
            return
        for dev in self._get_devices():
            try:
                acct = next((s["sourceAccount"] for s in dev.get_sources()
                             if s["source"] == "SPOTIFY" and s["sourceAccount"] in linked), None)
                if not acct:
                    continue
                info = self._http.get(f"http://{dev.host}:8200/zc", params={"action": "getInfo"}, timeout=5).json()
                user = info.get("activeUser", "")
                if user not in ("", acct):
                    continue
                age = time.monotonic() - self._pushed.get((dev.host, acct), -1e9)
                if user == acct and age < SPOTIFY_LOGIN_MAX_AGE - 10 * 60:
                    continue
                src, status = self._source(dev)
                if src == "SPOTIFY" and status in ("PLAY_STATE", "BUFFERING_STATE"):
                    continue
                self.ensure_login(dev, acct, force=True)
            except Exception as e:
                log.debug(f"[SPOTIFY] keep-login {dev.host}: {e}")

def _sp_image(images):
    """A ~300px image URL from a Spotify images list."""
    imgs = [i for i in (images or []) if i and i.get("url")]
    if not imgs:
        return ""
    mid = [i for i in imgs if (i.get("width") or 0) and 200 <= i["width"] <= 400]
    return (mid or imgs)[0]["url"]


def _sp_card(o):
    """Compact UI card for a playlist / album / artist / track object."""
    if not o:
        return None
    kind = o.get("type", "")
    card = {"type": kind, "uri": o.get("uri", ""), "name": o.get("name", ""), "sub": "", "image": ""}
    if kind == "playlist":
        total = (o.get("items") or o.get("tracks") or {}).get("total")
        owner = (o.get("owner") or {})
        card["image"] = _sp_image(o.get("images"))
        card["sub"] = " · ".join(x for x in [owner.get("display_name") or owner.get("id"),
                                             f"{total} songs" if total is not None else ""] if x)
        if owner.get("id") == "spotify":
            card["readonly"] = True
    elif kind == "album":
        card["image"] = _sp_image(o.get("images"))
        year = (o.get("release_date") or "")[:4]
        card["sub"] = " · ".join(x for x in [", ".join(a["name"] for a in o.get("artists", [])), year] if x)
    elif kind == "artist":
        card["image"] = _sp_image(o.get("images"))
        card["sub"] = "Artist"
    elif kind == "track":
        alb = o.get("album") or {}
        card["image"] = _sp_image(alb.get("images"))
        card["sub"] = " · ".join(x for x in [", ".join(a["name"] for a in o.get("artists", [])), alb.get("name", "")] if x)
        card["album_uri"] = alb.get("uri", "")
    return card


def _sp_track(t, position):
    if not t or not t.get("uri"):
        return None
    return {"uri": t["uri"], "name": t.get("name", ""), "position": position,
            "sub": ", ".join(a["name"] for a in t.get("artists", [])),
            "duration_ms": t.get("duration_ms", 0), "album_uri": (t.get("album") or {}).get("uri", "")}


# ═══════════════════════════════════════════════════════════════════════════════
# App state
# ═══════════════════════════════════════════════════════════════════════════════

class AppState:
    def __init__(self, web_port=WEB_PORT):
        self.devices      = []
        self._lock        = threading.Lock()
        self.store        = PresetStore()
        self.scene_store  = SceneStore()
        self.alarm_store  = AlarmStore()
        self.scheduler    = None   # set in main() after state is created
        self.web_port     = web_port

        uuid_path = DATA_DIR / "dlna_uuid.txt"
        if uuid_path.exists():
            dlna_uuid = uuid_path.read_text().strip()
        else:
            dlna_uuid = str(_uuid.uuid4())
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            uuid_path.write_text(dlna_uuid)
        self.dlna = DLNAServer(dlna_uuid, web_port, get_local_ip(), self.store)
        self.dlna.start()

        self._kitchen_like = {}  # host → bool, cached after first check
        t = threading.Thread(target=self._upnp_autoplay_loop, daemon=True)
        t.start()

        self.audio_mode_store = AudioModeStore()
        self._audio_pending   = {}  # host → [mode, deadline, attempts] being enforced
        self.reboot_status    = {}  # deviceID → {state, host, name} after reboot_device()
        self.maintenance_store = MaintenanceStore()
        threading.Thread(target=self._audio_mode_loop, daemon=True).start()

        global SPOTIFY
        self.spotify = SPOTIFY = SpotifyClient(SpotifyAccountStore(), lambda: list(self.devices))
        threading.Thread(target=self._spotify_login_loop, daemon=True).start()

    def _spotify_login_loop(self):
        """Every 5 min, keep speakers' Spotify logins fresh (see
        SpotifyClient.keep_speakers_logged_in)."""
        time.sleep(30)
        while True:
            try: self.spotify.keep_speakers_logged_in()
            except Exception as e: log.debug(f"[SPOTIFY] keep-login loop: {e}")
            time.sleep(300)

    def cancel_audio_mode_pending(self, host):
        """Stop enforcing an automatic mode — called when the user sets one by hand."""
        self._audio_pending.pop(host, None)

    def _audio_mode_loop(self):
        """Soundbar watcher (every 2 s, soundbars only):

        - TV wake: the bar woke from standby into a music source and nothing
          started playing within TV_WAKE_GRACE → the TV woke it over CEC, so
          switch it to the TV input (see tv_wake_action).
        - Auto dialogue mode: dialogue mode when the TV input is selected,
          normal for music sources. After a switch the target mode is re-checked
          for a few seconds and re-applied if the bar reverts it — it can reset
          its DSP mode while still settling into the new input."""
        prev_source = {}   # host → source from the previous poll
        woke        = {}   # host → (music source it woke into, monotonic time)
        while True:
            time.sleep(2)
            with self._lock:
                devices = list(self.devices)
            for dev in devices:
                try:
                    if not dev.has_tv_input():
                        continue
                    np = dev._get("/now_playing")
                    if np is None:
                        continue
                    source = np.get("source", "")
                    if source == "NOTIFICATION":
                        continue   # TTS announcement — not a real source change
                    play_status = np.get("playStatus") or np.findtext("playStatus") or ""
                    playing = play_status in ("PLAY_STATE", "BUFFERING_STATE")
                    prev = prev_source.get(dev.host)
                    prev_source[dev.host] = source

                    if prev == "STANDBY" and _source_kind(source) == "music":
                        woke[dev.host] = (source, time.monotonic())
                        log.info(f"[TV-WAKE] {dev.host} woke into {source} — "
                                 f"switching to TV unless it plays within {TV_WAKE_GRACE}s")
                    if dev.host in woke:
                        wsrc, t0 = woke[dev.host]
                        action = tv_wake_action(wsrc, time.monotonic() - t0, source, playing)
                        if action == "switch":
                            log.info(f"[TV-WAKE] {dev.host} still silent on {source} → TV")
                            dev.select_source("PRODUCT", "TV")
                        if action != "wait":
                            woke.pop(dev.host, None)

                    if not (dev.supports_dialog_mode() and self.audio_mode_store.auto_enabled(dev)):
                        self._audio_pending.pop(dev.host, None)
                        continue
                    target = audio_mode_for_transition(prev, source)
                    if target:
                        log.info(f"[DSP-AUTO] {dev.host} source → {source}: want {target} mode")
                        self._audio_pending[dev.host] = [target, time.monotonic() + 10, 0]

                    pend = self._audio_pending.get(dev.host)
                    if not pend:
                        continue
                    mode, deadline, attempts = pend
                    if time.monotonic() > deadline or attempts >= 4:
                        self._audio_pending.pop(dev.host, None)
                        continue
                    current = dev.get_audio_mode()
                    if current is not None and current != mode:
                        log.info(f"[DSP-AUTO] {dev.host} {current} → {mode}")
                        dev.set_audio_mode(mode)
                        pend[2] += 1
                except Exception as e:
                    log.debug(f"[DSP-AUTO] {dev.host} error: {e}")

    def _upnp_autoplay_loop(self):
        """Watch Kitchen-like speakers (no LOCAL_INTERNET_RADIO) for UPNP preset presses.

        Physical preset buttons cause two distinct speaker behaviours:
          - UPNP + stopped: speaker loaded the ContentItem but won't auto-play it
          - INVALID_SOURCE: speaker went straight to error (most common on first press)

        For UPNP+stopped we fire AVTransport immediately.
        For INVALID_SOURCE we fire on the *transition* into that state using the last
        UPNP location we observed — this handles the physical-button first-press case."""
        dlna_prefix  = f"http://{get_local_ip()}:{self.web_port}/dlna/stream/"
        last_upnp_loc = {}   # host → most recent UPNP/dlna location seen
        last_fired    = {}   # host → location we last sent AVTransport Play for
        prev_source   = {}   # host → source from the previous poll cycle
        inv_retry     = {}   # host → location to retry once if still INVALID_SOURCE next poll
        while True:
            time.sleep(2)
            with self._lock:
                devices = list(self.devices)
            for dev in devices:
                try:
                    if dev.host not in self._kitchen_like:
                        self._kitchen_like[dev.host] = not dev.has_local_internet_radio()
                    if not self._kitchen_like[dev.host]:
                        continue
                    np = dev._get("/now_playing")
                    if np is None:
                        continue
                    source = np.get("source", "")
                    play_status = np.get("playStatus") or np.findtext("playStatus") or ""
                    ci  = np.find("ContentItem")
                    loc = ci.get("location", "") if ci is not None else ""

                    if source == "UPNP":
                        if loc.startswith(dlna_prefix):
                            last_upnp_loc[dev.host] = loc
                        if play_status in ("PLAY_STATE", "BUFFERING_STATE"):
                            # Now playing — allow the same location to be re-triggered later
                            last_fired.pop(dev.host, None)
                            inv_retry.pop(dev.host, None)
                        elif (loc and loc.startswith(dlna_prefix) and loc != last_fired.get(dev.host)
                              and prev_source.get(dev.host) == "STANDBY" and dev.has_tv_input()):
                            # A soundbar coming out of standby onto its last radio
                            # station is almost always the TV waking it over CEC —
                            # auto-playing would start the radio over the TV. Leave
                            # it for the TV-wake check in _audio_mode_loop.
                            log.info(f"[AVT-AUTO] {dev.host} soundbar woke onto {loc} — not auto-playing")
                            last_fired[dev.host] = loc
                        elif loc and loc.startswith(dlna_prefix) and loc != last_fired.get(dev.host):
                            log.info(f"[AVT-AUTO] {dev.host} UPNP+stopped → auto-play {loc}")
                            if dev.play_via_avt(loc):
                                last_fired[dev.host] = loc

                    elif source == "INVALID_SOURCE":
                        is_transition = prev_source.get(dev.host) not in (None, "INVALID_SOURCE")
                        if is_transition:
                            # Fresh transition into INVALID_SOURCE — physical preset button pressed.
                            # Delay 1.5 s before sending AVTransport: the speaker's renderer silently
                            # discards Play commands issued immediately after entering this state.
                            target = last_upnp_loc.get(dev.host)
                            if target:
                                log.info(f"[AVT-AUTO] {dev.host} → INVALID_SOURCE, waiting 1.5 s for renderer")
                                time.sleep(1.5)
                                log.info(f"[AVT-AUTO] {dev.host} auto-play {target}")
                                dev.play_via_avt(target)
                                inv_retry[dev.host] = target  # allow one retry next poll if still stuck
                        elif dev.host in inv_retry:
                            # Still in INVALID_SOURCE after first attempt — retry once.
                            target = inv_retry.pop(dev.host)
                            log.info(f"[AVT-AUTO] {dev.host} INVALID_SOURCE retry → {target}")
                            dev.play_via_avt(target)
                            last_fired[dev.host] = target

                    else:
                        last_upnp_loc.pop(dev.host, None)
                        last_fired.pop(dev.host, None)
                        inv_retry.pop(dev.host, None)

                    prev_source[dev.host] = source
                except Exception as e:
                    log.debug(f"[AVT-AUTO] {dev.host} error: {e}")

    def reboot_device(self, dev):
        """Reboot a speaker, then rescan until it's back (matched by deviceID —
        it can come back on a different IP). Returns False if the speaker
        didn't accept the command."""
        if not dev.reboot():
            return False
        self.reboot_status[dev.device_id] = {"state": "rebooting", "host": None, "name": dev.name}
        threading.Thread(target=self._rediscover, args=({dev.device_id: dev.name},),
                         daemon=True).start()
        return True

    def _rediscover(self, pending):
        """After reboots: rescan until every deviceID in `pending` (id → name)
        is back — a speaker can return on a new DHCP address. One scan loop
        covers any number of speakers. Updates reboot_status; returns
        {id: new host or None}."""
        pending, found = dict(pending), {}
        time.sleep(40)   # an ST20 is back on the network in ~40 s
        for _ in range(8):
            self.scan()
            for did in list(pending):
                back = next((d for d in self.devices if d.device_id == did), None)
                if back:
                    log.info(f"[REBOOT] {pending[did]} back at {back.host}")
                    self.reboot_status[did] = {"state": "back", "host": back.host, "name": pending[did]}
                    found[did] = back.host
                    del pending[did]
            if not pending:
                break
            time.sleep(20)
        for did, name in pending.items():
            log.warning(f"[REBOOT] {name} not found after reboot")
            self.reboot_status[did] = {"state": "lost", "host": None, "name": name}
            found[did] = None
        return found

    def restart_clock_speakers(self, reason):
        """Restart every speaker with a front-panel clock, skipping any that
        are playing, then wait for them all to come back. The outcome is saved
        as last_run / last_result in the maintenance settings."""
        with self._lock:
            devices = list(self.devices)
        started = _dt.datetime.now(ZoneInfo(self.maintenance_store.get().get("tz") or "UTC"))
        log.info(f"[MAINT] {reason}: restarting clock speakers")
        result, pending = {}, {}
        for dev in devices:
            if not dev.has_clock():
                continue
            if dev.is_playing():
                result[dev.name] = "skipped (playing)"
                continue
            if dev.reboot():
                self.reboot_status[dev.device_id] = {"state": "rebooting", "host": None, "name": dev.name}
                pending[dev.device_id] = dev.name
            else:
                result[dev.name] = "restart refused"
        for did, host in (self._rediscover(pending) if pending else {}).items():
            result[pending[did]] = f"restarted ({host})" if host else "not back yet"
        log.info(f"[MAINT] {reason} done: {result}")
        self.maintenance_store.update(last_run=started.isoformat(timespec="seconds"),
                                      last_result=result)
        return result

    def scan(self):
        log.info("Scanning network…")
        found = discover_all(timeout=3)
        for dev in found:
            dev.has_backup = self.store.load_backup(dev.host) is not None
        with self._lock:
            self.devices = found
        log.info(f"Scan complete — {len(self.devices)} speaker(s).")

    def add_device(self, host, port=8090):
        dev = SoundTouchDevice(host, port)
        if dev.fetch_info():
            with self._lock:
                if not any(d.host == host for d in self.devices):
                    self.devices.append(dev)
            return dev
        return None

    def get_device(self, host):
        with self._lock:
            for d in self.devices:
                if d.host == host:
                    return d
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _daemonise(log_path):
    """
    Double-fork daemonisation (POSIX).
    Detaches from the terminal, redirects stdout/stderr to log_path,
    and writes the new PID to <log_path>.pid.
    """
    if os.name != "posix":
        print("ERROR: --daemon is only supported on Linux/macOS.")
        sys.exit(1)

    # First fork — detach from parent
    if os.fork() > 0:
        sys.exit(0)

    os.setsid()

    # Second fork — prevent re-acquiring a terminal
    if os.fork() > 0:
        sys.exit(0)

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    log_path = pathlib.Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as lf:
        os.dup2(lf.fileno(), sys.stdout.fileno())
        os.dup2(lf.fileno(), sys.stderr.fileno())
    with open("/dev/null") as nf:
        os.dup2(nf.fileno(), sys.stdin.fileno())

    # Write PID file
    pid_path = log_path.with_suffix(".pid")
    pid_path.write_text(str(os.getpid()))


def main():
    parser = argparse.ArgumentParser(
        description="SoundTouch web controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 soundtouch_controller.py                   # foreground, auto-discover
  python3 soundtouch_controller.py --ip 192.168.1.50 # connect directly
  python3 soundtouch_controller.py --daemon          # run in background
  python3 soundtouch_controller.py --daemon --log /var/log/soundtouch.log
        """,
    )
    parser.add_argument("--port", type=int, default=WEB_PORT,
                        help=f"Web server port (default {WEB_PORT})")
    parser.add_argument("--ip", metavar="IP",
                        help="Skip discovery; connect to this speaker IP directly")
    parser.add_argument("--daemon", action="store_true",
                        help="Detach from terminal and run in the background")
    parser.add_argument("--log", metavar="FILE",
                        default=str(DATA_DIR / "soundtouch.log"),
                        help="Log file path when running with --daemon "
                             f"(default: {DATA_DIR}/soundtouch.log)")
    args = parser.parse_args()

    # Ensure data dirs exist before any potential fork
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_DIR.mkdir(parents=True, exist_ok=True)
    SCENES_DIR.mkdir(parents=True, exist_ok=True)

    local_ip = get_local_ip()
    url      = f"http://{local_ip}:{args.port}"

    if args.daemon:
        log_path = pathlib.Path(args.log)
        pid_path = log_path.with_suffix(".pid")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  SoundTouch Controller — starting in background")
        print(f"  Web UI : {url}")
        print(f"  Log    : {log_path}")
        print(f"  PID    : {pid_path}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        _daemonise(args.log)
        # Everything below here runs in the detached child process

    _check_network(args.port)

    state = AppState(web_port=args.port)
    state.scheduler = AlarmScheduler(state.alarm_store, state)
    Handler.server_state = state

    if not args.daemon:
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  SoundTouch Controller")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if args.ip:
        log.info(f"Connecting to {args.ip} …")
        dev = state.add_device(args.ip)
        log.info(f"{'Connected: ' + dev.name if dev else 'Could not reach ' + args.ip}")
    else:
        threading.Thread(target=state.scan, daemon=True).start()

    if not args.daemon:
        print(f"\n  Open in any browser (same Wi-Fi):\n    {url}")
        print(f"\n  Data stored in: {DATA_DIR}")
        print(f"  Press Ctrl+C to stop.")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    else:
        print(f"  SoundTouch Controller running — {url}")

    # ThreadingHTTPServer: each request runs on its own thread so a slow/offline
    # speaker (blocking 4s _get/_post) can't stall the UI for every other client.
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
