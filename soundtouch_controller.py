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
import concurrent.futures
import ipaddress
import json
import logging
import os
import pathlib
import re
import socket
import struct
import sys
import threading
import time
import uuid as _uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse, quote as urlquote

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

    # ── low-level ─────────────────────────────────────────────────────────────
    def _get(self, path, timeout=4):
        url = f"{self.url}{path}"
        log.debug(f"[SPK GET ] {url}")
        try:
            r = self._session.get(url, timeout=timeout)
            r.raise_for_status()
            snippet = r.text[:400].replace("\n", " ")
            log.debug(f"[SPK GET ] ← {r.status_code}  {snippet}")
            return ET.fromstring(r.text)
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
        if xml is None:
            return {"available": False, "min": -9, "max": 0, "default": 0}
        return {
            "available": (xml.findtext("bassAvailable") or "false").lower() == "true",
            "min":     int(xml.findtext("bassMin")     or "-9"),
            "max":     int(xml.findtext("bassMax")     or "0"),
            "default": int(xml.findtext("bassDefault") or "0"),
        }

    def get_bass(self):
        xml = self._get("/bass")
        if xml is None: return 0
        return int(xml.findtext("actualbass") or "0")

    def set_bass(self, value):
        self._post("/bass", f"<bass>{max(-9, min(9, int(value)))}</bass>")

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
        self._post("/select", body)

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

    def set_name(self, new_name):
        self._post("/name", f"<name>{new_name}</name>")

    # ── state snapshot ────────────────────────────────────────────────────────
    def state(self):
        d = dict(host=self.host, name=self.name, model=self.model,
                 volume=0, muted=False, source="", track="", artist="",
                 album="", art="", playing=False, presets=[])

        # Fetch all four endpoints in parallel to minimise poll latency
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            f_vol  = ex.submit(self._get, "/volume")
            f_np   = ex.submit(self._get, "/now_playing")
            f_pre  = ex.submit(self.get_presets_detail)
            f_zone = ex.submit(self.get_zone)

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

    def set_volume(self, v):
        self._post("/volume", f"<volume>{max(0,min(100,int(v)))}</volume>")

    # ── preset management ─────────────────────────────────────────────────────
    def store_preset(self, preset_id, name, source, stype, location, account=""):
        """Write a preset to the speaker via /storePreset."""
        acct = f' sourceAccount="{account}"' if account else ''
        xml = (
            f'<preset id="{preset_id}">'
            f'<ContentItem source="{source}" type="{stype}" '
            f'location="{location}"{acct}>'
            f'<itemName>{name}</itemName>'
            f'</ContentItem></preset>'
        )
        return self._post("/storePreset", xml)

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

    def play_via_avt(self, stream_url):
        """Play a stream URL via UPnP AVTransport (port 8091).
        Used for speakers that lack LOCAL_INTERNET_RADIO. The URL must be HTTP
        (not HTTPS) — the speaker follows redirects but rejects https:// URIs."""
        avt = f"http://{self.host}:8091/AVTransport/Control"
        esc = stream_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        set_soap = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            f'<CurrentURI>{esc}</CurrentURI>'
            '<CurrentURIMetaData></CurrentURIMetaData>'
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


class AlarmScheduler:
    """Background thread that fires alarms at their scheduled time."""

    def __init__(self, alarm_store, app_state):
        self._store     = alarm_store
        self._app       = app_state
        self._fired     = {}   # alarm_id+date key → True
        self._thread    = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("[ALARM] Scheduler started")

    def _run(self):
        while True:
            try: self._tick()
            except Exception as e: log.warning(f"[ALARM] tick error: {e}")
            time.sleep(30)

    def _tick(self):
        now    = time.localtime()
        hhmm   = f"{now.tm_hour:02d}:{now.tm_min:02d}"
        wday   = now.tm_wday   # 0=Mon … 6=Sun
        today  = f"{now.tm_year}{now.tm_yday}"
        for alarm in self._store.list_alarms():
            if not alarm.get("enabled"): continue
            if alarm.get("time") != hhmm: continue
            if wday not in alarm.get("days", list(range(7))): continue
            key = f"{alarm['id']}_{hhmm}_{today}"
            if self._fired.get(key): continue
            self._fired[key] = True
            threading.Thread(target=self._fire, args=(alarm,), daemon=True).start()

    def _fire(self, alarm):
        host = alarm.get("host")
        dev  = self._app.get_device(host) if host else None
        if not dev:
            log.warning(f"[ALARM] Device not found for alarm '{alarm.get('name')}'"); return
        vol = alarm.get("volume")
        if vol is not None:
            dev.set_volume(vol); time.sleep(0.5)
        dev.preset(alarm.get("preset", 1))
        log.info(f"[ALARM] Fired '{alarm.get('name')}' — {host} preset {alarm.get('preset',1)}")


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

        elif path in ("/app.css", "/app.js"):
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
                    n = int(action.replace("preset",""))
                    # UPNP presets: key press loads the ContentItem but the speaker
                    # waits for an external AVTransport Play to start audio.
                    # Detect this case from the cached preset list and use AVTransport.
                    presets = dev.get_presets_detail()
                    p = next((x for x in presets if x.get("id") == str(n)), None)
                    if p and p.get("source") == "UPNP" and p.get("location",""):
                        ok = dev.play_via_avt(p["location"])
                    else:
                        dev.preset(n); ok=True
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
                caps["current"] = dev.get_bass()
                self._json(caps)

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
            if dev and source: dev.select_source(source, account); self._json({"ok":True})
            else:              self._json({"ok":False})

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
            self._json(self.server_state.alarm_store.list_alarms())

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
                alarm_id = "alarm_" + str(int(time.time()))
                alarm = {
                    "id":      alarm_id,
                    "name":    data.get("name", "Alarm").strip() or "Alarm",
                    "host":    data.get("host"),
                    "preset":  int(data.get("preset", 1)),
                    "time":    data.get("time", "07:00"),
                    "days":    [int(d) for d in data.get("days", list(range(7)))],
                    "enabled": True,
                    "volume":  int(data["volume"]) if data.get("volume") not in (None, "") else None,
                }
                self.server_state.alarm_store.save_alarm(alarm)
                log.info(f"[ALARM] Saved '{alarm['name']}' at {alarm['time']}")
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
        self._respond(200, ctype, body)

    def _respond(self, code, ctype, body):
        if code >= 400:
            log.warning(f"[API RESP] {code} {ctype}  {body[:200].decode('utf-8','replace')}")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


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
