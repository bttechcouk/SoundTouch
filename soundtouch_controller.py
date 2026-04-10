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
# Sources that are fully local and will continue to work after shutdown
SAFE_SOURCES = {"LOCAL_INTERNET_RADIO", "BLUETOOTH", "AUX", "AIRPLAY", "TV",
                "STORED_MUSIC", "PRODUCT", "STANDBY"}

# ── PWA service worker (served at /sw.js) ───────────────────────────────────
SW_JS = r"""
const CACHE='soundtouch-v3';
self.addEventListener('install',e=>{
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/'])));
  self.skipWaiting();
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys().then(ks=>
    Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.pathname.startsWith('/api/')||u.pathname==='/sw.js'||e.request.method!=='GET')return;
  e.respondWith(caches.match(e.request).then(cached=>{
    const net=fetch(e.request).then(r=>{
      if(r&&r.status===200&&r.type==='basic'){
        caches.open(CACHE).then(c=>c.put(e.request,r.clone()));
      }return r;
    }).catch(()=>cached);
    return cached||net;
  }));
});
"""

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
                }
                if ci is not None:
                    rec["source"]   = ci.get("source","")
                    rec["type"]     = ci.get("type","")
                    rec["location"] = ci.get("location","")
                    rec["account"]  = ci.get("sourceAccount","")
                    nm = ci.find("itemName")
                    if nm is not None:
                        rec["name"] = nm.text or ""
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
        """Create a zone with self as master and slave_devices as the slaves."""
        members_xml = f'<member ipaddress="{self.host}">{self.device_id}</member>'
        for d in slave_devices:
            members_xml += f'<member ipaddress="{d.host}">{d.device_id}</member>'
        return self._post("/setZone",
                          f'<zone master="{self.device_id}">{members_xml}</zone>')

    def remove_zone(self):
        """Dissolve the zone this speaker is master of."""
        zinfo = self.get_zone()
        if not zinfo["is_master"]:
            return
        slaves_xml = "".join(
            f'<member ipaddress="{m["ip"]}">{m["id"]}</member>'
            for m in zinfo["members"] if m["id"] != self.device_id
        )
        if slaves_xml:
            self._post("/removeZoneSlaves",
                       f'<zone master="{self.device_id}">{slaves_xml}</zone>')


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
        path.write_text(json.dumps(data, indent=2))
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
        path.write_text(json.dumps(data, indent=2))
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
        path.write_text(json.dumps(data, indent=2))
        return data

    def delete_station(self, station_id):
        path = self.stations_dir / f"{station_id}.json"
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


# ═══════════════════════════════════════════════════════════════════════════════
# Scene store  (named zone + preset + volume snapshots)
# ═══════════════════════════════════════════════════════════════════════════════

class SceneStore:
    """Stores named scenes as JSON files in data/scenes/."""

    def __init__(self, scenes_dir=SCENES_DIR):
        self.scenes_dir = pathlib.Path(scenes_dir)
        self.scenes_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, scene_id):
        return self.scenes_dir / f"{scene_id}.json"

    def save(self, scene_id, data):
        self._path(scene_id).write_text(json.dumps(data, indent=2))

    def load(self, scene_id):
        p = self._path(scene_id)
        return json.loads(p.read_text()) if p.exists() else None

    def delete(self, scene_id):
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
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(alarms, indent=2))

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
# Web UI  — R34 Skyline GT-R colour scheme
# ═══════════════════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SoundTouch">
<meta name="theme-color" content="#0b0c11">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>SoundTouch</title>
<script>if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});</script>
<style>
/* ── Reset ───────────────────────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}

/* ── R34 Skyline GT-R palette ────────────────────────────── */
/* Inspired by Brian O'Conner's silver 2 Fast 2 Furious R34: */
/* dark asphalt body, electric-blue neon underglow,          */
/* pearl-silver bodywork, warm amber strip lights            */
:root{
  --bg:         #0b0c11;
  --surface:    #13151d;
  --surface2:   #1a1d28;
  --surface3:   #222636;
  --border:     #2c3047;
  --blue:       #2277ee;
  --blue-light: #55aaff;
  --blue-dim:   #0d3d7a;
  --blue-glow:  rgba(34,119,238,.35);
  --silver:     #c8d4e8;
  --silver-dim: #6a7a94;
  --amber:      #f59e0b;
  --amber-dim:  #92600a;
  --fg:         #d4dcf0;
  --fg2:        #6a7a94;
  --fg3:        #353c52;
  --white:      #eef2fc;
  --radius:     14px;
}
html,body{height:100%;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;overscroll-behavior:none}

/* ── Layout ──────────────────────────────────────────────── */
#app{max-width:440px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;position:relative}

/* ── Header ──────────────────────────────────────────────── */
header{padding:16px 20px 0;display:flex;align-items:center;justify-content:space-between}
.logo{font-size:12px;font-weight:700;letter-spacing:.2em;text-transform:uppercase;
  display:flex;align-items:center;gap:8px}
.logo-stripe{width:28px;height:3px;
  background:linear-gradient(90deg,var(--blue) 60%,var(--amber) 100%);
  border-radius:2px}
.logo-txt{color:var(--silver)}
#scan-btn{background:var(--surface);border:1px solid var(--border);color:var(--blue-light);
  padding:6px 14px;border-radius:20px;font-size:12px;cursor:pointer;
  font-weight:600;letter-spacing:.04em;transition:all .2s}
#scan-btn:active{background:var(--blue-dim);border-color:var(--blue)}
#scan-btn.spinning::after{content:" ↻";display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.speaker-info-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
.speaker-info-table tr{border-bottom:1px solid var(--border)}
.speaker-info-table td{padding:7px 4px}
.speaker-info-table td:first-child{color:var(--fg3);width:38%}
.speaker-info-table td:last-child{color:var(--fg1);font-family:monospace;font-size:11px;word-break:break-all}

.qr-collapse-hdr{display:flex;align-items:center;gap:10px;cursor:pointer;
  padding:11px 14px;user-select:none;background:var(--surface2);
  border-radius:var(--radius);border:1px solid var(--border);margin-top:4px}
.qr-collapse-hdr:hover{background:var(--surface2)}
.qr-collapse-hdr span.title{font-size:13px;font-weight:600;color:var(--fg1);flex:1}
.qr-collapse-badge{font-size:11px;padding:2px 8px;border-radius:10px;
  background:var(--surface2);border:1px solid var(--border)}
.qr-collapse-badge.ok{color:#4caf50;border-color:#4caf5055;background:rgba(76,175,80,.08)}
.qr-collapse-badge.warn{color:var(--fg3);border-color:var(--border)}
.qr-chevron{font-size:10px;color:var(--fg3);transition:transform .2s;flex-shrink:0}
.qr-chevron.open{transform:rotate(180deg)}
.qr-body{padding:14px 4px 4px}

/* ── Tabs ────────────────────────────────────────────────── */
#tabs{display:flex;border-bottom:1px solid var(--border);margin:12px 20px 0;gap:0}
.tab{flex:1;text-align:center;padding:9px 0;font-size:12px;font-weight:600;
  letter-spacing:.08em;color:var(--fg3);text-transform:uppercase;cursor:pointer;
  border-bottom:2px solid transparent;transition:all .15s}
.tab.active{color:var(--blue-light);border-color:var(--blue)}

/* ── Speaker chips ───────────────────────────────────────── */
#rooms-section{padding:12px 20px 0}
.section-label{font-size:10px;font-weight:700;letter-spacing:.12em;
  color:var(--fg3);text-transform:uppercase;margin-bottom:8px}
#rooms-list{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.room-chip{background:var(--surface);border:1.5px solid var(--border);
  border-radius:20px;padding:6px 14px;font-size:13px;cursor:pointer;
  display:flex;align-items:center;gap:7px;transition:all .18s;
  min-width:0;overflow:hidden;color:var(--fg2)}
.room-chip .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.room-chip .dot{width:6px;height:6px;border-radius:50%;background:var(--fg3);flex-shrink:0}
.room-chip.active{border-color:var(--blue);color:var(--blue-light);
  background:linear-gradient(135deg,var(--surface2),var(--surface))}
.room-chip.active .dot{background:var(--blue-light);box-shadow:0 0 6px var(--blue-glow)}
.room-chip.playing .dot{background:var(--blue-light);
  box-shadow:0 0 8px var(--blue-glow);animation:pulse 2s infinite}
.room-chip.offline{opacity:.4;border-style:dashed}
.room-chip.offline .dot{background:var(--fg3);animation:none;box-shadow:none}
@keyframes pulse{0%,100%{box-shadow:0 0 6px var(--blue-glow)}
                 50%{box-shadow:0 0 14px rgba(34,119,238,.1)}}
#no-speakers{color:var(--fg3);font-size:13px;padding:4px 0}

/* ── Page ────────────────────────────────────────────────── */
.page{display:none;flex-direction:column;flex:1}
.page.visible{display:flex}

/* Art — full square */
#art-wrap{position:relative;width:100%;padding-bottom:100%;margin:12px 0 0;
  background:var(--surface);border-radius:var(--radius);overflow:hidden;
  box-shadow:0 8px 32px rgba(0,0,0,.5),0 0 0 1px var(--border),
             0 1px 0 rgba(34,119,238,.15) inset}
#art{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:opacity .4s}
#art.hidden{opacity:0}
#art-placeholder{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:52px;opacity:.1;color:var(--blue-light)}

/* Track info */
#track-info{padding:12px 4px 0;display:flex;align-items:flex-start;
  justify-content:space-between;gap:10px}
#track-text{min-width:0;flex:1}
#track-name{font-size:18px;font-weight:700;color:var(--white);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.2}
#track-artist{font-size:13px;color:var(--fg2);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Bass */
#bass-row{padding:6px 4px 0;display:flex;align-items:center;gap:10px}
#bass-track{flex:1;position:relative;padding-top:22px}
#bass-tooltip{position:absolute;top:0;transform:translateX(-50%);
  background:var(--amber);color:#000;font-size:11px;font-weight:700;
  padding:2px 7px;border-radius:10px;pointer-events:none;
  opacity:0;transition:opacity .2s}
#bass-tooltip.visible{opacity:1}
#bass-slider{width:100%;height:4px;-webkit-appearance:none;appearance:none;
  border-radius:2px;outline:none;cursor:pointer;
  background:linear-gradient(to right,var(--amber) var(--pct,100%),var(--surface2) var(--pct,100%))}
#bass-slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
  border-radius:50%;background:var(--silver);cursor:pointer;
  box-shadow:0 0 6px rgba(245,158,11,.5)}
#bass-slider::-moz-range-thumb{width:16px;height:16px;border-radius:50%;
  background:var(--silver);cursor:pointer;border:none}
.bass-label{font-size:10px;color:var(--fg3);flex-shrink:0;text-transform:uppercase;
  letter-spacing:.06em}

/* Chip backup warning */
.chip-warn{font-size:9px;color:var(--amber);flex-shrink:0;margin-left:1px}

/* Volume */
#vol-row{padding:12px 4px 0;display:flex;align-items:center;gap:10px}
.vol-icon{color:var(--fg3);font-size:15px;flex-shrink:0}
.vol-btn{cursor:pointer;user-select:none;transition:color .15s}
.vol-btn:hover{color:var(--fg1)}
.vol-btn:active{color:var(--blue-light)}
#vol-track{flex:1;position:relative;padding-top:22px}
#vol-tooltip{position:absolute;top:0;transform:translateX(-50%);
  background:var(--blue);color:#fff;font-size:11px;font-weight:700;
  padding:2px 7px;border-radius:10px;pointer-events:none;white-space:nowrap;
  opacity:0;transition:opacity .2s;left:var(--pct,20%)}
#vol-tooltip.visible{opacity:1}
#vol-slider{width:100%;height:4px;-webkit-appearance:none;appearance:none;
  border-radius:2px;outline:none;cursor:pointer;
  background:linear-gradient(to right,var(--blue) var(--pct,20%),var(--surface2) var(--pct,20%))}
#vol-slider::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;
  border-radius:50%;background:var(--silver);cursor:pointer;
  box-shadow:0 0 8px var(--blue-glow)}
#vol-slider::-moz-range-thumb{width:18px;height:18px;border-radius:50%;
  background:var(--silver);cursor:pointer;border:none}

/* Transport */
#transport{padding:14px 4px 6px;display:flex;align-items:center;justify-content:center;gap:20px}
.t-btn{background:none;border:none;color:var(--fg);cursor:pointer;
  padding:10px;border-radius:50%;transition:all .15s;flex-shrink:0;
  display:flex;align-items:center;justify-content:center}
.t-btn:active{background:var(--surface2)}
.t-btn svg{display:block}
#btn-play{background:var(--blue);width:64px;height:64px;
  box-shadow:0 4px 22px var(--blue-glow),0 0 0 1px var(--blue-dim)}
#btn-play:active{background:var(--blue-dim)}
.t-btn-sm{opacity:.75}.t-btn-sm:hover{opacity:1}

/* Presets dropdown */
#preset-toggle{background:var(--surface);border:1px solid var(--border);
  color:var(--blue-light);padding:6px 14px;border-radius:20px;font-size:12px;
  cursor:pointer;font-weight:600;letter-spacing:.04em;transition:all .2s;
  display:flex;align-items:center;gap:5px}
#preset-toggle.open{background:var(--blue-dim);border-color:var(--blue)}
#preset-toggle .arrow{font-size:9px;transition:transform .2s}
#preset-toggle.open .arrow{transform:rotate(180deg)}
/* Clip wrapper: positioned dynamically via JS below the tabs bar */
#presets-clip{position:absolute;top:94px;left:0;right:0;z-index:50;
  max-height:0;overflow:hidden;pointer-events:none;
  transition:max-height .22s ease}
#presets-clip.open{max-height:300px;pointer-events:auto}
/* Panel itself just slides within the clip wrapper */
#presets-panel{background:var(--surface);border-bottom:1px solid var(--border);
  box-shadow:0 8px 32px rgba(0,0,0,.7);
  transform:translateY(-100%);transition:transform .22s ease}
#presets-clip.open #presets-panel{transform:translateY(0)}
#presets-panel-inner{padding:14px 20px 18px;max-width:440px;margin:0 auto}
#presets-panel-label{font-size:10px;font-weight:700;letter-spacing:.12em;
  color:var(--fg3);text-transform:uppercase;margin-bottom:10px}
#presets-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.preset{background:var(--surface2);border:1px solid var(--border);
  border-radius:var(--radius);padding:12px 8px;cursor:pointer;
  text-align:center;transition:all .15s;user-select:none}
.preset:active{background:var(--blue-dim);border-color:var(--blue)}
.preset-num{font-size:9px;font-weight:700;letter-spacing:.1em;
  color:var(--fg3);text-transform:uppercase;margin-bottom:3px}
.preset-name{font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--fg3)}
.preset.has-name .preset-name{color:var(--fg)}
#presets-backdrop{display:none;position:fixed;inset:0;z-index:49}
#presets-backdrop.open{display:block}

#source-badge{font-size:10px;font-weight:700;letter-spacing:.06em;
  color:var(--blue-light);background:var(--surface);border:1px solid var(--border);
  padding:3px 8px;border-radius:10px;white-space:nowrap;align-self:center;flex-shrink:0}
#group-badge{font-size:10px;font-weight:700;letter-spacing:.06em;
  color:var(--amber);background:var(--surface);border:1px solid var(--amber-dim);
  padding:3px 8px;border-radius:10px;white-space:nowrap;align-self:center;flex-shrink:0}
#cloud-warn{margin:8px 20px 0;padding:8px 12px;
  background:rgba(245,158,11,.08);border:1px solid var(--amber-dim);
  border-radius:8px;font-size:11px;color:var(--amber);line-height:1.5}

/* Power / Mute */
#power-row{display:flex;justify-content:center;gap:10px;padding:10px 20px 18px}
#btn-power,#btn-mute{background:var(--surface);border:1px solid var(--border);
  color:var(--fg2);border-radius:20px;padding:8px 22px;font-size:13px;
  cursor:pointer;display:flex;align-items:center;gap:7px;transition:all .18s}
#btn-power:active,#btn-mute:active{background:var(--surface2);color:var(--silver);border-color:var(--silver-dim)}
#btn-mute.muted,#btn-power.playing{background:rgba(34,119,238,.12);border-color:var(--blue);color:var(--blue-light)}

/* ── Page: Manage Presets ────────────────────────────────── */
.manage-section{padding:14px 20px}
.manage-section h2{font-size:13px;font-weight:700;color:var(--blue-light);
  letter-spacing:.06em;margin-bottom:10px}
.manage-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:12px 14px;margin-bottom:8px;
  display:flex;align-items:center;justify-content:space-between;gap:10px}
.manage-card .mc-left{min-width:0;flex:1}
.manage-card .mc-name{font-size:14px;font-weight:600;color:var(--fg)}
.manage-card .mc-meta{font-size:11px;color:var(--fg3);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mc-actions{display:flex;gap:6px;flex-shrink:0}
.mc-btn{background:var(--surface2);border:1px solid var(--border);color:var(--fg2);
  border-radius:8px;padding:6px 12px;font-size:11px;font-weight:600;
  cursor:pointer;transition:all .15s;white-space:nowrap}
.mc-btn:active{background:var(--blue-dim);color:var(--white)}
.mc-btn.primary{background:var(--blue-dim);color:var(--blue-light);border-color:var(--blue)}
.mc-btn.primary:active{background:var(--blue);color:var(--white)}
.mc-btn.danger{border-color:var(--amber-dim);color:var(--amber)}
/* Preset health check */
.health-summary{font-size:12px;font-weight:600;padding:8px 12px;border-radius:8px;
  margin-bottom:10px;display:flex;align-items:center;gap:8px}
.health-summary.all-safe{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.3)}
.health-summary.has-risk{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.3)}
.health-card{border-radius:8px;padding:10px 12px;margin-bottom:6px;
  display:flex;align-items:flex-start;gap:10px;border:1px solid transparent}
.health-card.risk-high{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.25)}
.health-card.risk-safe{background:rgba(34,197,94,.06);border-color:rgba(34,197,94,.2)}
.health-card.risk-empty{background:var(--surface);border-color:var(--border);opacity:.55}
.health-card.risk-unknown{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.25)}
.health-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:4px}
.risk-high .health-dot{background:#f87171}
.risk-safe .health-dot{background:#4ade80}
.risk-empty .health-dot{background:var(--fg3)}
.risk-unknown .health-dot{background:#fbbf24}
.health-name{font-size:13px;font-weight:600;color:var(--fg);line-height:1.3}
.health-source{font-size:11px;color:var(--fg3);margin-top:1px}
.health-sug{font-size:11px;color:#f87171;margin-top:4px;line-height:1.4}
#btn-health-check{color:#4ade80}
/* Stream search results */
#st-search-results{margin-top:6px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.sr-item{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;
  background:var(--surface2);border-bottom:1px solid var(--border);transition:background .12s}
.sr-item:last-child{border-bottom:none}
.sr-item:hover{background:var(--blue-dim)}
.sr-logo{width:28px;height:28px;border-radius:4px;object-fit:cover;flex-shrink:0;background:var(--surface3)}
.sr-info{min-width:0;flex:1}
.sr-name{font-size:12px;font-weight:600;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sr-meta{font-size:10px;color:var(--fg3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sr-use{font-size:10px;color:var(--blue-light);flex-shrink:0;font-weight:600}

/* Add station form */
.add-form{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:14px;margin:0 20px 14px}
.add-form label{display:block;font-size:11px;font-weight:600;color:var(--fg3);
  letter-spacing:.06em;margin-bottom:4px;margin-top:10px}
.add-form label:first-child{margin-top:0}
.add-form input{width:100%;background:var(--surface2);border:1px solid var(--border);
  color:var(--fg);padding:9px 12px;border-radius:8px;font-size:13px;outline:none}
.add-form input:focus{border-color:var(--blue)}
.add-form input::placeholder{color:var(--fg3)}
.form-row{display:flex;gap:8px;margin-top:12px}

.alexa-hint{background:rgba(34,119,238,.07);border:1px solid var(--blue-dim);
  border-radius:var(--radius);padding:13px 16px;margin-bottom:14px;
  font-size:12px;color:var(--fg2);line-height:1.6}
.alexa-hint strong{color:var(--blue-light)}
.alexa-phrase{display:inline-block;background:var(--surface2);
  border:1px solid var(--border);border-radius:6px;padding:2px 8px;
  font-family:monospace;font-size:12px;color:var(--silver);margin:1px 0}

.qr-section{margin-top:18px;padding:16px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--radius)}
.qr-section h3{margin:0 0 12px;font-size:13px;color:var(--fg)}
.qr-box{background:#fff;color:#000;font-family:monospace;font-size:13px;
  line-height:1;padding:14px;border-radius:6px;display:inline-block;
  border:2px solid #ccc;white-space:pre}
.qr-manual{font-family:monospace;font-size:15px;letter-spacing:2px;
  color:var(--fg1);margin-top:10px}
.qr-status{font-size:11px;color:var(--fg2);margin-top:6px}
.qr-refresh{margin-top:10px;padding:5px 14px;font-size:12px;
  background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;color:var(--fg1);cursor:pointer}
.qr-refresh:hover{background:var(--surface3)}

/* ── Ambient art glow ────────────────────────────────────── */
#art-glow{position:absolute;inset:-15%;width:130%;height:130%;object-fit:cover;
  filter:blur(50px) saturate(1.4);opacity:0;transition:opacity .8s;
  pointer-events:none;z-index:0;border-radius:0}
#art-glow.visible{opacity:0.55}
#art{z-index:1}
#art-placeholder{z-index:1}

/* ── EQ visualiser ───────────────────────────────────────── */
.eq-bars{display:flex;align-items:flex-end;gap:2.5px;height:18px;
  margin-left:8px;flex-shrink:0;align-self:center;opacity:0;transition:opacity .3s}
.eq-bars.playing{opacity:1}
.eq-bar{width:3px;border-radius:2px 2px 0 0;background:var(--blue-light);
  transform-origin:bottom;animation:eq-bounce 1.2s ease-in-out infinite}
.eq-bar:nth-child(1){height:35%;animation-delay:0s}
.eq-bar:nth-child(2){height:75%;animation-delay:.18s}
.eq-bar:nth-child(3){height:100%;animation-delay:.08s}
.eq-bar:nth-child(4){height:55%;animation-delay:.28s}
.eq-bar:nth-child(5){height:40%;animation-delay:.13s}
@keyframes eq-bounce{0%,100%{transform:scaleY(.35)}50%{transform:scaleY(1)}}
.eq-bars:not(.playing) .eq-bar{animation-play-state:paused}

/* ── Play button pulse ring ──────────────────────────────── */
#btn-play{position:relative}
#btn-play::before{content:'';position:absolute;inset:-4px;border-radius:50%;
  border:2px solid var(--blue);opacity:0;pointer-events:none;
  animation:play-ring 2.4s ease-out infinite;animation-play-state:paused}
#btn-play.playing::before{animation-play-state:running}
@keyframes play-ring{
  0%{opacity:.8;transform:scale(1)}
  100%{opacity:0;transform:scale(1.55)}}

/* ── Track name marquee ──────────────────────────────────── */
#track-name{overflow:hidden;white-space:nowrap}
#track-name span{display:inline-block;white-space:nowrap}
#track-name.marquee span{animation:marquee-scroll 10s linear 1.5s infinite}
@keyframes marquee-scroll{0%,12%{transform:translateX(0)}88%,100%{transform:translateX(var(--sw,0))}}

/* ── Tab page fade-in ────────────────────────────────────── */
.page.visible{animation:page-in .18s ease forwards}
@keyframes page-in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}

/* ── Ripple ──────────────────────────────────────────────── */
.t-btn,.preset,.mc-btn{overflow:hidden}
.ripple{position:absolute;border-radius:50%;transform:scale(0);
  background:rgba(255,255,255,.18);animation:ripple-out .45s linear;
  pointer-events:none}
@keyframes ripple-out{to{transform:scale(5);opacity:0}}

/* ── Glassmorphism collapsible headers ───────────────────── */
.qr-collapse-hdr{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  background:rgba(19,21,29,.75)}

/* ── Speaker chip mini-EQ ────────────────────────────────── */
.chip-eq{display:none;align-items:flex-end;gap:1.5px;height:11px;
  margin-left:3px;flex-shrink:0}
.room-chip.playing .chip-eq{display:flex}
.room-chip.playing .dot{display:none}
.chip-eq-bar{width:2.5px;border-radius:1px 1px 0 0;background:var(--blue-light);
  transform-origin:bottom;animation:eq-bounce 1.2s ease-in-out infinite}
.chip-eq-bar:nth-child(1){height:40%;animation-delay:.05s}
.chip-eq-bar:nth-child(2){height:100%;animation-delay:.2s}
.chip-eq-bar:nth-child(3){height:60%;animation-delay:0s}

/* ── Toast ───────────────────────────────────────────────── */
#toast{position:fixed;bottom:32px;left:50%;transform:translateX(-50%);
  background:rgba(19,21,29,.96);color:var(--blue-light);border:1px solid var(--blue-dim);
  padding:9px 20px;border-radius:22px;font-size:13px;font-weight:600;
  opacity:0;pointer-events:none;transition:opacity .25s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  white-space:nowrap;box-shadow:0 4px 24px rgba(0,0,0,.5)}
#toast.show{opacity:1}

/* ── All-speaker volume row ──────────────────────────────── */
#all-vol-row{padding:14px 4px 4px;border-top:1px solid var(--border);margin-top:18px}
#all-vol-label{font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--fg3);
  text-transform:uppercase;margin-bottom:6px}

/* ── Header icon buttons (scenes / alarms quick-view) ────── */
.icon-btn{background:var(--surface);border:1px solid var(--border);color:var(--fg2);
  width:32px;height:32px;border-radius:50%;font-size:15px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .2s;padding:0;
  flex-shrink:0}
.icon-btn:active{background:var(--blue-dim);color:var(--blue-light);border-color:var(--blue)}

/* ── Modal overlays ──────────────────────────────────────── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(7,8,12,.85);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
  align-items:center;justify-content:center;z-index:150}
.modal-overlay.open{display:flex}
.modal-box{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);width:92%;max-width:380px;max-height:78vh;
  overflow-y:auto;padding:20px;box-shadow:0 8px 40px rgba(0,0,0,.6)}
.modal-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.modal-hdr span{font-size:15px;font-weight:700;color:var(--white)}
/* Select input for forms */
.form-select{width:100%;background:var(--surface2);border:1px solid var(--border);
  color:var(--fg);padding:9px 12px;border-radius:8px;font-size:13px;outline:none;
  -webkit-appearance:none;appearance:none}
#all-vol-inner{display:flex;align-items:center;gap:10px}
#all-vol-track{flex:1;position:relative;padding-top:22px}
#all-vol-tip{position:absolute;top:0;transform:translateX(-50%);
  background:var(--silver-dim);color:#000;font-size:11px;font-weight:700;
  padding:2px 7px;border-radius:10px;pointer-events:none;white-space:nowrap;
  opacity:0;transition:opacity .2s;left:var(--pct,50%)}
#all-vol-tip.visible{opacity:1}
#all-vol-slider{width:100%;height:4px;-webkit-appearance:none;appearance:none;
  border-radius:2px;outline:none;cursor:pointer;
  background:linear-gradient(to right,var(--silver-dim) var(--pct,50%),var(--surface2) var(--pct,50%))}
#all-vol-slider::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;
  border-radius:50%;background:var(--silver);cursor:pointer;
  box-shadow:0 0 6px rgba(200,212,232,.3)}
#all-vol-slider::-moz-range-thumb{width:18px;height:18px;border-radius:50%;
  background:var(--silver);cursor:pointer;border:none}

/* ── Alarm day buttons ───────────────────────────────────── */
.day-btn{display:inline-flex;align-items:center;justify-content:center;
  background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  padding:5px 10px;font-size:11px;font-weight:600;color:var(--fg2);cursor:pointer;
  user-select:none;transition:all .15s}
.day-btn:has(input:checked){background:var(--blue-dim);border-color:var(--blue);color:var(--blue-light)}
.day-btn{position:relative}
.day-btn input{position:absolute;opacity:0;width:0;height:0;pointer-events:none}

/* ── PWA install banner (Android Chrome) ─────────────────── */
#install-banner{display:none;position:fixed;bottom:0;left:50%;transform:translateX(-50%);
  width:100%;max-width:440px;background:var(--surface2);border-top:1px solid var(--border);
  padding:12px 16px;align-items:center;gap:10px;z-index:90;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
#install-banner.show{display:flex}
#install-banner .ib-icon{font-size:22px;flex-shrink:0}
#install-banner .ib-text{flex:1;min-width:0}
#install-banner .ib-title{font-size:13px;font-weight:700;color:var(--white)}
#install-banner .ib-sub{font-size:11px;color:var(--fg3);margin-top:1px}

/* ── Scanning overlay ────────────────────────────────────── */
#scanning{display:none;position:fixed;inset:0;background:rgba(7,8,12,.85);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
  align-items:center;justify-content:center;z-index:100}
#scanning.show{display:flex}
.scan-box{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:30px 40px;text-align:center}
.scan-spinner{font-size:30px;color:var(--blue-light);
  animation:spin .7s linear infinite;display:block;margin-bottom:10px}
.scan-label{font-size:14px;color:var(--fg2)}
</style>
</head>
<body>
<div id="app">

  <!-- Presets dropdown panel (sits at top of #app, slides down over content) -->
  <div id="presets-backdrop" onclick="closePresets()"></div>
  <div id="presets-clip">
    <div id="presets-panel">
      <div id="presets-panel-inner">
        <div id="presets-panel-label">Presets</div>
        <div id="presets-grid"></div>
      </div>
    </div>
  </div>

  <!-- Header -->
  <header style="position:relative;z-index:51">
    <div class="logo"><div class="logo-stripe"></div><span class="logo-txt">SOUNDTOUCH</span></div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="icon-btn" onclick="openScenesModal()" title="Scenes">&#9654;</button>
      <button class="icon-btn" onclick="openAlarmsModal()" title="Alarms">&#9201;</button>
      <button id="preset-toggle" onclick="togglePresets()">
        Presets <span class="arrow">▼</span>
      </button>
    </div>
  </header>

  <!-- Tabs -->
  <div id="tabs">
    <div class="tab active" data-tab="player"   onclick="switchTab('player')">Player</div>
    <div class="tab"        data-tab="manage"   onclick="switchTab('manage')">Presets</div>
    <div class="tab"        data-tab="groups"   onclick="switchTab('groups')">Groups</div>
    <div class="tab"        data-tab="settings" onclick="switchTab('settings')">Settings</div>
  </div>

  <!-- Speaker chips -->
  <div id="rooms-section">
    <div class="section-label">Speakers</div>
    <div id="rooms-list"><div id="no-speakers">Scanning…</div></div>
  </div>

  <!-- ═══ PAGE: Player ═══ -->
  <div id="page-player" class="page visible" style="padding:0 20px">

    <div id="art-wrap">
      <img id="art-glow" src="" alt="" aria-hidden="true">
      <img id="art" src="" alt="" class="hidden">
      <div id="art-placeholder">&#9835;</div>
    </div>

    <div id="track-info">
      <div id="track-text">
        <div id="track-name"><span>—</span></div>
        <div id="track-artist"></div>
      </div>
      <div class="eq-bars" id="eq-bars">
        <div class="eq-bar"></div><div class="eq-bar"></div><div class="eq-bar"></div>
        <div class="eq-bar"></div><div class="eq-bar"></div>
      </div>
      <div id="group-badge" style="display:none"></div>
      <div id="source-badge" style="display:none"></div>
    </div>
    <div id="cloud-warn" style="display:none"></div>

    <div id="vol-row">
      <span class="vol-icon vol-btn" onclick="nudgeVol(-1)">&#128264;</span>
      <div id="vol-track">
        <div id="vol-tooltip">20</div>
        <input type="range" id="vol-slider" min="0" max="100" value="20"
               oninput="onVolInput(this.value)" onchange="sendVol(this.value)">
      </div>
      <span class="vol-icon vol-btn" onclick="nudgeVol(1)">&#128266;</span>
    </div>

    <div id="transport">
      <button class="t-btn t-btn-sm" onclick="cmd('prev',this,event)" title="Previous">
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
          <polygon points="28,8 14,18 28,28" fill="currentColor"/>
          <rect x="8" y="8" width="4" height="20" rx="2" fill="currentColor"/>
        </svg>
      </button>
      <button class="t-btn" id="btn-play" onclick="cmd('playpause',this,event)">
        <svg id="ico-play" width="30" height="30" viewBox="0 0 32 32">
          <polygon points="8,4 28,16 8,28" fill="currentColor"/>
        </svg>
        <svg id="ico-pause" width="30" height="30" viewBox="0 0 32 32" style="display:none">
          <rect x="5" y="4" width="8" height="24" rx="2" fill="currentColor"/>
          <rect x="19" y="4" width="8" height="24" rx="2" fill="currentColor"/>
        </svg>
      </button>
      <button class="t-btn t-btn-sm" onclick="cmd('next',this,event)" title="Next">
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
          <polygon points="8,8 22,18 8,28" fill="currentColor"/>
          <rect x="24" y="8" width="4" height="20" rx="2" fill="currentColor"/>
        </svg>
      </button>
    </div>

    <div id="power-row">
      <button id="btn-power" onclick="cmd('power',this,event)" style="position:relative">
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path d="M8 1v6M4.5 3.5A5 5 0 1 0 11.5 3.5" stroke="currentColor"
                stroke-width="1.6" stroke-linecap="round" fill="none"/>
        </svg>
        Power
      </button>
      <button id="btn-mute" onclick="cmd('mute',this,event)" style="position:relative">
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path d="M2 5.5h2.5L8 2v12l-3.5-3.5H2z" fill="currentColor"/>
          <path id="ico-mute-lines" d="M10 6a3 3 0 0 1 0 4M11.5 4a5 5 0 0 1 0 8"
                stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/>
          <path id="ico-mute-cross" d="M10 6l3 4M13 6l-3 4"
                stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
                fill="none" style="display:none"/>
        </svg>
        Mute
      </button>
    </div>

  </div>

  <!-- ═══ PAGE: Groups ═══ -->
  <div id="page-groups" class="page">
    <div class="manage-section">
      <h2>Multi-Room Groups</h2>
      <p style="font-size:12px;color:var(--fg3);margin-bottom:14px">
        Group speakers together so they all play the same audio in sync.
        The active speaker becomes the group master.
      </p>
      <div id="group-status"></div>
      <div id="group-builder"></div>

      <!-- All-speaker volume -->
      <div id="all-vol-row">
        <div id="all-vol-label">All Speakers Volume</div>
        <div id="all-vol-inner">
          <span class="vol-icon vol-btn" onclick="nudgeAllVol(-1)">&#128264;</span>
          <div id="all-vol-track">
            <div id="all-vol-tip">50</div>
            <input type="range" id="all-vol-slider" min="0" max="100" value="50"
                   oninput="onAllVolInput(this.value)" onchange="sendAllVol(this.value)">
          </div>
          <span class="vol-icon vol-btn" onclick="nudgeAllVol(1)">&#128266;</span>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ PAGE: Settings ═══ -->
  <div id="page-settings" class="page">
    <div class="manage-section">

      <!-- Discover Speakers -->
      <div class="qr-section" style="margin-top:0">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-discover','chev-discover')">
          <span class="title">Discover Speakers</span>
          <span id="chev-discover" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-discover" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg3);margin-bottom:10px">
            Scan the local network to find all SoundTouch speakers.
          </p>
          <button id="scan-btn" onclick="rescan()">Scan for Speakers</button>
        </div>
      </div>

      <!-- Speaker Details -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-speaker','chev-speaker')">
          <span class="title">Speaker Details</span>
          <span id="chev-speaker" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-speaker" class="qr-body" style="display:none">
          <div id="speaker-info">
            <p style="font-size:12px;color:var(--fg3)">Select a speaker to view its details.</p>
          </div>
        </div>
      </div>

      <!-- Preset Backup -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-backup','chev-backup')">
          <span class="title">Preset Backup</span>
          <span id="chev-backup" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-backup" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg3);margin-bottom:10px">
            Back up all speakers in one click. Speakers with no backup show ⚠ on their chip.
            Critical before the Bose cloud shuts down on 6 May 2026.
          </p>
          <button class="mc-btn primary" onclick="backupAll()">Backup All Speakers</button>
          <span id="backup-all-status" style="font-size:12px;color:var(--fg3);margin-left:10px"></span>
        </div>
      </div>

      <!-- Alarms -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-alarms','chev-alarms')">
          <span class="title">Alarms</span>
          <span id="chev-alarms" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-alarms" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg3);margin-bottom:12px">
            Schedule a speaker to start playing a preset at a set time.
          </p>
          <div id="alarms-list"></div>
          <div class="add-form" style="margin:10px 0 0">
            <label>Speaker</label>
            <select id="alarm-speaker-select" class="form-select">
              <option value="">Select a speaker…</option>
            </select>
            <label>Name (optional)</label>
            <input id="alarm-name" placeholder="Morning Radio">
            <label>Time</label>
            <input id="alarm-time" type="time" value="07:00">
            <label>Preset (1–6)</label>
            <input id="alarm-preset" type="number" min="1" max="6" value="1">
            <label>Volume (optional — leave blank to keep current)</label>
            <input id="alarm-vol" type="number" min="0" max="100" placeholder="0–100">
            <label>Days</label>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="0" checked>Mon</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="1" checked>Tue</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="2" checked>Wed</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="3" checked>Thu</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="4" checked>Fri</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="5">Sat</label>
              <label class="day-btn"><input type="checkbox" class="alarm-day-chk" value="6">Sun</label>
            </div>
            <div class="form-row">
              <button class="mc-btn primary" onclick="addAlarm()">Add Alarm</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Scenes -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-scenes','chev-scenes')">
          <span class="title">Scenes</span>
          <span id="chev-scenes" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-scenes" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg3);margin-bottom:12px">
            Save the current speaker group, volumes, and a preset as a named scene.
            One tap replays everything exactly.
          </p>
          <div id="scenes-list"></div>
          <div class="add-form" style="margin:10px 0 0">
            <label>Scene Name</label>
            <input id="scene-name-input" placeholder="e.g. Morning, Party, Bedtime">
            <label>Preset to play (1–6)</label>
            <input id="scene-preset-input" type="number" min="1" max="6" value="1">
            <div class="form-row">
              <button class="mc-btn primary" onclick="saveScene()">Save Scene</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Announce -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-announce','chev-announce')">
          <span class="title">Send Announcement</span>
          <span id="chev-announce" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-announce" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg2);margin-bottom:12px;line-height:1.5">
            Speak a message through one or more speakers. Playback is paused,
            the announcement plays, then resumes automatically.
          </p>
          <textarea id="main-ann-text" rows="3"
            style="width:100%;padding:10px 12px;background:var(--surface2);border:1px solid var(--border);
                   border-radius:8px;color:var(--fg);font-size:14px;resize:none;outline:none;
                   font-family:inherit;line-height:1.4;margin-bottom:10px"
            placeholder="e.g. Dinner is ready…"></textarea>
          <div style="font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
                      color:var(--fg3);margin-bottom:6px">Accent / Voice</div>
          <select id="main-ann-accent" class="form-select" style="margin-bottom:12px">
            <option value="british">🇬🇧 British</option>
            <option value="american">🇺🇸 American</option>
            <option value="irish">🇮🇪 Irish</option>
            <option value="australian">🇦🇺 Australian</option>
            <option value="posh">🎩 Posh English</option>
            <option value="gangster">🔪 Gangster (Danny Dyer)</option>
          </select>
          <div style="font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
                      color:var(--fg3);margin-bottom:8px">Speakers</div>
          <div id="main-ann-speakers" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px"></div>
          <div style="font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
                      color:var(--fg3);margin-bottom:6px">Volume</div>
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
            <input type="range" id="main-ann-vol" min="10" max="100" value="60"
              style="flex:1;height:3px;-webkit-appearance:none;appearance:none;border-radius:2px;
                     outline:none;cursor:pointer;
                     background:linear-gradient(to right,var(--blue) 55.6%,var(--surface2) 55.6%)"
              oninput="this.style.background='linear-gradient(to right,var(--blue) '+((this.value-this.min)/(this.max-this.min)*100).toFixed(1)+'%,var(--surface2) '+((this.value-this.min)/(this.max-this.min)*100).toFixed(1)+'%)'">
            <span id="main-ann-vol-lbl" style="font-size:12px;font-weight:700;color:var(--fg2);width:26px;text-align:right">60</span>
          </div>
          <div id="main-ann-status" style="font-size:12px;color:var(--amber);min-height:16px;margin-bottom:8px"></div>
          <button class="mc-btn primary" onclick="sendMainAnnounce()">Send Announcement</button>
        </div>
      </div>

      <!-- Alexa Integration -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-alexa','chev-alexa')">
          <span class="title">Alexa Integration</span>
          <span id="chev-alexa" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-alexa" class="qr-body" style="display:none">
          <div class="alexa-hint">
            <strong>How it works:</strong> A separate Matter bridge process runs alongside
            this app, exposing each speaker preset and power toggle as a Matter On/Off
            device — <strong>no cloud, no account linking.</strong><br><br>
            <strong>Step 1 —</strong> Scan for speakers above<br>
            <strong>Step 2 —</strong> Commission the Matter bridge once in the Alexa app:<br>
            &nbsp;&nbsp;Add Device → Other → Matter → expand panel below and scan QR<br>
            <strong>Step 3 —</strong> Use phrases like:<br>
            &nbsp;&nbsp;<span class="alexa-phrase">Alexa, turn on KISSTORY in Kitchen Bose</span><br>
            &nbsp;&nbsp;<span class="alexa-phrase">Alexa, turn on Kitchen Bose power</span><br>
            &nbsp;&nbsp;<span class="alexa-phrase">Alexa, set Kitchen Bose volume to 40%</span><br><br>
            <strong>Bridge logs:</strong>
            <span class="alexa-phrase">journalctl --user -u soundtouch-matter -f</span>
          </div>

          <div class="qr-section" style="margin-top:12px">
            <div class="qr-collapse-hdr" onclick="toggleQR()">
              <span class="title">Commission Matter Bridge</span>
              <span id="qr-status-badge" class="qr-collapse-badge warn">checking…</span>
              <span id="qr-chevron" class="qr-chevron">&#9660;</span>
            </div>
            <div id="qr-body" class="qr-body" style="display:none">
              <div id="qr-box" class="qr-box">Loading…</div>
              <div id="qr-manual" class="qr-manual"></div>
              <div id="qr-status" class="qr-status" style="margin-top:8px"></div>
              <button class="qr-refresh" onclick="loadAlexaQR()">Refresh</button>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <!-- ═══ PAGE: Manage Presets ═══ -->
  <div id="page-manage" class="page">
    <div class="manage-section">

      <!-- How to set a preset -->
      <div class="qr-section" style="margin-top:0">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-preset-howto','chev-preset-howto')">
          <span class="title">How to Set a Preset</span>
          <span id="chev-preset-howto" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-preset-howto" class="qr-body" style="display:none">
          <div class="alexa-hint">
            Presets are saved directly on the speaker — each button (1–6) remembers
            whatever was playing when you held it down.<br><br>
            <strong>Step 1 —</strong> Start playing the station, playlist, or source you want to save.<br>
            <strong>Step 2 —</strong> On the speaker itself, <strong>press and hold</strong> the preset
            button (1–6) for about 3 seconds until you hear a chime.<br>
            <strong>Step 3 —</strong> The button is now saved. Press it briefly at any time to play
            that station again.<br><br>
            <strong>Tip —</strong> If you use Custom Radio Stations (see below), play the station
            from the Presets tab first so it is the active source, then hold the preset button on
            the speaker to save it.<br><br>
            <strong>After saving —</strong> Click <em>Backup Now</em> below to save a local copy
            so your presets survive the Bose cloud shutdown on 6 May 2026.
          </div>
        </div>
      </div>

      <!-- Preset Backup -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-manage-backup','chev-manage-backup')">
          <span class="title">Preset Backup</span>
          <span id="chev-manage-backup" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-manage-backup" class="qr-body" style="display:none">
          <p id="backup-info" style="font-size:12px;color:var(--fg3);margin-bottom:12px">
            Back up your current presets locally so they survive the Bose cloud shutdown.
          </p>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="mc-btn primary" onclick="backupPresets()">Backup Now</button>
            <button class="mc-btn" onclick="restorePresets()">Restore to Speaker</button>
            <button class="mc-btn" onclick="openBackupEditor()">Open / Edit JSON</button>
            <button class="mc-btn" onclick="checkPresetHealth()" id="btn-health-check">&#9679; Health Check</button>
          </div>
          <div id="backup-status" style="font-size:12px;color:var(--fg3);margin-top:10px"></div>
          <div id="backup-list" style="margin-top:12px"></div>
          <div id="health-results" style="margin-top:12px;display:none"></div>
        </div>
      </div>

      <!-- Custom Radio Stations -->
      <div class="qr-section">
        <div class="qr-collapse-hdr" onclick="toggleSection('sec-stations','chev-stations')">
          <span class="title">Custom Radio Stations</span>
          <span id="chev-stations" class="qr-chevron">&#9660;</span>
        </div>
        <div id="sec-stations" class="qr-body" style="display:none">
          <p style="font-size:12px;color:var(--fg3);margin-bottom:12px">
            Add your own internet radio streams. These are served locally and don't need the Bose cloud.
          </p>

          <!-- How to add a custom station -->
          <div class="qr-section" style="margin-top:0;margin-bottom:14px">
            <div class="qr-collapse-hdr" onclick="toggleSection('sec-station-howto','chev-station-howto')">
              <span class="title">How to add a Custom Station</span>
              <span id="chev-station-howto" class="qr-chevron">&#9660;</span>
            </div>
            <div id="sec-station-howto" class="qr-body" style="display:none">
              <div class="alexa-hint">
                Custom stations let you play any internet radio stream that has a direct
                HTTP audio URL — no Bose cloud required.<br><br>
                <strong>Step 1 —</strong> Find a direct stream URL for the station.
                Most public broadcasters publish these; search for
                <em>"[station name] stream URL m3u"</em> to find one.<br>
                <strong>Step 2 —</strong> Enter a name, paste the URL, and optionally
                add an album art URL below, then click <em>Add Station</em>.<br>
                <strong>Step 3 —</strong> The station will appear in the list. Click
                <em>Play</em> to start it on the active speaker.<br>
                <strong>Step 4 —</strong> While it's playing, hold a preset button
                on the speaker to save it — see <em>How to Set a Preset</em> above.<br><br>
                <strong>Note —</strong> Stream URLs must start with <code>http://</code>
                (not https) as the SoundTouch firmware does not support TLS streams.
              </div>
            </div>
          </div>

          <!-- Add station form -->
          <div class="add-form" id="add-form">
            <label>Station Name</label>
            <input id="st-name" placeholder="e.g. BBC Radio 1">
            <label>Stream URL (HTTP)</label>
            <div style="display:flex;gap:6px;align-items:center">
              <input id="st-url" placeholder="http://stream.live.vc.bbcmedia.co.uk/bbc_radio_one" style="flex:1">
              <button class="mc-btn" onclick="searchStationStream()" title="Search RadioBrowser for a direct stream URL" style="white-space:nowrap">&#128269; Find stream</button>
            </div>
            <div id="st-search-results" style="display:none"></div>
            <label>Album Art URL (optional)</label>
            <input id="st-art" placeholder="https://example.com/logo.png">
            <div class="form-row">
              <button class="mc-btn primary" onclick="addStation()">Add Station</button>
            </div>
          </div>

          <div id="stations-list"></div>
        </div>
      </div>

    </div>
  </div>

</div>

<!-- PWA install banner (shown by beforeinstallprompt on Android Chrome) -->
<div id="install-banner">
  <div class="ib-icon">&#127911;</div>
  <div class="ib-text">
    <div class="ib-title">Install SoundTouch</div>
    <div class="ib-sub">Add to your home screen for instant access</div>
  </div>
  <button class="mc-btn primary" onclick="installPWA()" style="flex-shrink:0">Install</button>
  <button class="mc-btn" onclick="dismissInstall()" style="flex-shrink:0;padding:6px 10px">&#10005;</button>
</div>

<!-- Backup JSON editor modal -->
<div id="backup-json-modal" class="modal-overlay" onclick="if(event.target===this)closeModal('backup-json-modal')">
  <div class="modal-box" style="max-width:500px;width:96%">
    <div class="modal-hdr">
      <span id="backup-json-title">Preset Backup JSON</span>
      <button class="mc-btn" onclick="closeModal('backup-json-modal')">&#10005;</button>
    </div>
    <p style="font-size:11px;color:var(--fg3);margin-bottom:10px">
      Edit the JSON below. Changes are validated before saving. Use <em>Save &amp; Restore</em>
      to write edits directly to the speaker.
    </p>
    <textarea id="backup-json-editor" spellcheck="false"
      style="width:100%;height:320px;background:var(--surface2);border:1px solid var(--border);
             color:var(--fg);font-family:monospace;font-size:11px;padding:10px 12px;
             border-radius:8px;resize:vertical;outline:none;line-height:1.6;
             tab-size:2"></textarea>
    <div id="backup-json-error"
      style="display:none;font-size:11px;color:#ef4444;margin-top:7px;
             background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);
             border-radius:6px;padding:6px 10px"></div>
    <div class="form-row" style="margin-top:12px;flex-wrap:wrap;gap:8px">
      <button class="mc-btn primary" onclick="saveBackupJson()">Save Changes</button>
      <button class="mc-btn primary" onclick="saveAndRestoreJson()">Save &amp; Restore to Speaker</button>
      <button class="mc-btn" onclick="closeModal('backup-json-modal')">Cancel</button>
    </div>
  </div>
</div>

<!-- Scenes quick-view modal -->
<div id="scenes-modal" class="modal-overlay" onclick="if(event.target===this)closeModal('scenes-modal')">
  <div class="modal-box">
    <div class="modal-hdr">
      <span>Scenes</span>
      <button class="mc-btn" onclick="closeModal('scenes-modal')">&#10005;</button>
    </div>
    <div id="scenes-modal-body"><p style="font-size:12px;color:var(--fg3)">Loading…</p></div>
  </div>
</div>

<!-- Alarms quick-view modal -->
<div id="alarms-modal" class="modal-overlay" onclick="if(event.target===this)closeModal('alarms-modal')">
  <div class="modal-box">
    <div class="modal-hdr">
      <span>Alarms</span>
      <button class="mc-btn" onclick="closeModal('alarms-modal')">&#10005;</button>
    </div>
    <div id="alarms-modal-body"><p style="font-size:12px;color:var(--fg3)">Loading…</p></div>
  </div>
</div>

<div id="toast"></div>
<div id="scanning">
  <div class="scan-box">
    <span class="scan-spinner">&#8635;</span>
    <div class="scan-label" id="scan-label">Scanning…</div>
  </div>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let speakers=[], activeHost=null, pollTimer=null, lastArt="", lastState=null;

// ── PWA install prompt ───────────────────────────────────────────────────────
let _installPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  _installPrompt = e;
  if (!localStorage.getItem('pwa-dismissed')) {
    document.getElementById('install-banner')?.classList.add('show');
  }
});
window.addEventListener('appinstalled', () => {
  document.getElementById('install-banner')?.classList.remove('show');
  _installPrompt = null;
});
async function installPWA() {
  if (!_installPrompt) return;
  _installPrompt.prompt();
  const { outcome } = await _installPrompt.userChoice;
  _installPrompt = null;
  document.getElementById('install-banner')?.classList.remove('show');
}
function dismissInstall() {
  document.getElementById('install-banner')?.classList.remove('show');
  localStorage.setItem('pwa-dismissed', '1');
}

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  fetchSpeakers(false); schedPoll();
  const savedTab = localStorage.getItem('activeTab');
  if (savedTab) switchTab(savedTab);
});

// ── Page Visibility — pause polls when tab is hidden ─────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
    clearTimeout(bgPollTimer);
  } else {
    pollNow();
    bgPollAll();
  }
});

// ── Tabs ─────────────────────────────────────────────────────────────────────
function collapseAll(pageId) {
  const page = document.getElementById(pageId);
  if (!page) return;
  page.querySelectorAll('.qr-body').forEach(b => b.style.display = 'none');
  page.querySelectorAll('.qr-chevron').forEach(c => c.classList.remove('open'));
}
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('visible', p.id === 'page-' + name));
  collapseAll('page-' + name);
  if (name === 'manage')   { /* sections load on expand */ }
  if (name === 'groups')   { loadGroups(); }
  if (name === 'settings') { /* sections load on expand */ }
  localStorage.setItem('activeTab', name);
}

// ── Speakers ─────────────────────────────────────────────────────────────────
async function fetchSpeakers(overlay) {
  if (overlay) showScanning("Scanning network…");
  try { speakers = await (await fetch('/api/speakers')).json(); } catch(e){}
  if (overlay) hideScanning();
  renderRooms();
  if (!activeHost && speakers.length) setActive(speakers[0].host);
}
async function rescan() {
  const b = document.getElementById('scan-btn');
  b.classList.add('spinning'); b.disabled = true;
  showScanning("Scanning for speakers…");
  try { speakers = await (await fetch('/api/scan')).json(); } catch(e){}
  hideScanning(); b.classList.remove('spinning'); b.disabled = false;
  renderRooms();
  if (!activeHost && speakers.length) setActive(speakers[0].host);
  toast(speakers.length ? `Found ${speakers.length} speaker${speakers.length>1?'s':''}` : 'No speakers found');
}
function renderRooms() {
  const el = document.getElementById('rooms-list');
  if (!speakers.length) { el.innerHTML='<div id="no-speakers">No speakers found</div>';
    document.getElementById('all-vol-row')?.classList.remove('visible'); return; }
  // Single speaker: full-width; 2+: 2-column grid
  el.style.gridTemplateColumns = speakers.length === 1 ? '1fr' : 'repeat(2,1fr)';
  el.innerHTML = speakers.map(s=>`
    <div class="room-chip${s.host===activeHost?' active':''}"
         id="chip-${s.host.replace(/\./g,'_')}"
         onclick="setActive('${s.host}')">
      <span class="dot"></span>
      <span class="chip-eq"><span class="chip-eq-bar"></span><span class="chip-eq-bar"></span><span class="chip-eq-bar"></span></span>
      <span class="name">${s.name}</span>${s.has_backup===false?'<span class="chip-warn" title="No preset backup">⚠</span>':''}</div>`).join('');
  updateAlarmSpeakerSelect();
}
function setActive(h) {
  activeHost=h; clearTimeout(pollTimer); renderRooms(); pollNow();
  const tab = document.querySelector('.tab.active')?.dataset?.tab;
  if (tab === 'manage') {
    const sec = document.getElementById('sec-manage-backup');
    if (sec && sec.style.display !== 'none') loadBackupInfo();
  }
  if (tab === 'groups')   loadGroups();
  if (tab === 'settings') {
    const sec = document.getElementById('sec-speaker');
    if (sec && sec.style.display !== 'none') loadSpeakerInfo();
  }
}

// ── Polling ──────────────────────────────────────────────────────────────────
const speakerErrors = {};
function schedPoll() { pollTimer = setTimeout(pollNow, 3000); }
async function pollNow() {
  clearTimeout(pollTimer);
  if (!activeHost) { schedPoll(); return; }
  try {
    applyState(await (await fetch('/api/state?host='+activeHost)).json());
    speakerErrors[activeHost] = 0;
    setChipOffline(activeHost, false);
  } catch(e) {
    speakerErrors[activeHost] = (speakerErrors[activeHost]||0) + 1;
    if (speakerErrors[activeHost] >= 2) setChipOffline(activeHost, true);
  }
  schedPoll();
}
function setChipOffline(host, offline) {
  const chip = document.getElementById('chip-'+host.replace(/\./g,'_'));
  if (chip) chip.classList.toggle('offline', offline);
}

// Background poll — updates playing/offline state for all non-active speakers
// Persistently offline speakers (≥5 consecutive failures) are checked every
// 5th cycle (~60 s) instead of every cycle (~12 s) to avoid pointless traffic.
let bgPollTimer = null;
const bgPollSkip = {};  // host → cycles remaining to skip
async function bgPollAll() {
  for (const s of speakers) {
    if (s.host === activeHost) continue;
    // Back-off: skip this cycle if the counter is still running
    if (bgPollSkip[s.host] > 0) { bgPollSkip[s.host]--; continue; }
    try {
      const st = await (await fetch('/api/ping?host='+s.host)).json();
      speakerErrors[s.host] = 0;
      bgPollSkip[s.host] = 0;
      setChipOffline(s.host, !st.online);
      const chip = document.getElementById('chip-'+s.host.replace(/\./g,'_'));
      if (chip) chip.classList.toggle('playing', st.playing);
    } catch(e) {
      speakerErrors[s.host] = (speakerErrors[s.host]||0) + 1;
      if (speakerErrors[s.host] >= 2) setChipOffline(s.host, true);
      // After 5 consecutive failures start skipping 4 out of every 5 cycles
      if (speakerErrors[s.host] >= 5) bgPollSkip[s.host] = 4;
    }
  }
  bgPollTimer = setTimeout(bgPollAll, 12000);
}
setTimeout(bgPollAll, 5000); // stagger start so it doesn't clash with boot poll
function setTrackName(text) {
  const el = document.getElementById('track-name');
  if (!el) return;
  el.classList.remove('marquee');
  el.style.removeProperty('--sw');
  let span = el.querySelector('span');
  if (!span) { el.innerHTML='<span></span>'; span = el.querySelector('span'); }
  span.textContent = text;
  requestAnimationFrame(() => {
    if (span.scrollWidth > el.offsetWidth + 2) {
      el.style.setProperty('--sw', `-${span.scrollWidth - el.offsetWidth + 24}px`);
      el.classList.add('marquee');
    }
  });
}
function applyState(d) {
  if (!d) return; lastState = d;
  const track = d.track||(d.source||'—'), artist = d.artist||d.album||'';
  setTrackName(track); setText('track-artist',artist);
  const badge=document.getElementById('source-badge');
  badge.textContent=d.source||''; badge.style.display=d.source?'':'none';
  const cw=document.getElementById('cloud-warn');
  if(d.cloud_warning){cw.textContent='⚠ '+d.cloud_warning; cw.style.display='';}
  else{cw.style.display='none';}
  const gbadge=document.getElementById('group-badge');
  if (d.group_role==='master') {
    gbadge.textContent=`GROUP MASTER (${d.group_members||0})`; gbadge.style.display='';
  } else if (d.group_role==='member') {
    gbadge.textContent='GROUP MEMBER'; gbadge.style.display='';
  } else {
    gbadge.style.display='none';
  }
  // art + ambient glow
  const artEl=document.getElementById('art'), ph=document.getElementById('art-placeholder');
  const glowEl=document.getElementById('art-glow');
  if (d.art && d.art!==lastArt) {
    lastArt=d.art; const tmp=new Image();
    tmp.onload=()=>{
      artEl.src=d.art; artEl.classList.remove('hidden'); ph.style.display='none';
      if(glowEl){glowEl.src=d.art; glowEl.classList.add('visible');}
    };
    tmp.onerror=()=>{
      artEl.classList.add('hidden'); ph.style.display='';
      if(glowEl){glowEl.src=''; glowEl.classList.remove('visible');}
    };
    tmp.src=d.art;
  } else if (!d.art) {
    artEl.classList.add('hidden'); ph.style.display='';
    if(glowEl){glowEl.src=''; glowEl.classList.remove('visible');}
  }
  // EQ visualiser + play button ring
  document.getElementById('eq-bars')?.classList.toggle('playing', d.playing);
  document.getElementById('ico-play').style.display=d.playing?'none':'';
  document.getElementById('ico-pause').style.display=d.playing?'':'none';
  document.getElementById('btn-play').classList.toggle('playing', d.playing);
  // power button — highlight while playing
  document.getElementById('btn-power').classList.toggle('playing', d.playing);
  // mute button
  const muteBtn=document.getElementById('btn-mute');
  muteBtn.classList.toggle('muted', !!d.muted);
  document.getElementById('ico-mute-lines').style.display=d.muted?'none':'';
  document.getElementById('ico-mute-cross').style.display=d.muted?'':'none';
  // volume
  const sl=document.getElementById('vol-slider');
  if (!sl.matches(':active')) { sl.value=d.volume; updateVol(d.volume); }
  // chip
  const chip=document.getElementById('chip-'+activeHost.replace(/\./g,'_'));
  if (chip) { chip.classList.toggle('playing',d.playing); chip.classList.add('active'); }
  // presets — populate dropdown grid
  const g=document.getElementById('presets-grid');
  const presets = d.presets || [];
  if (g.children.length===0) {
    g.innerHTML='';
    for (let i=0;i<6;i++) {
      const nm=presets[i]?.name||'';
      const div=document.createElement('div');
      div.className='preset'+(nm?' has-name':'');
      div.innerHTML=`<div class="preset-num">Preset ${i+1}</div>
                     <div class="preset-name">${nm||'—'}</div>`;
      div.onclick=(e)=>{ ripple(div,e); if(navigator.vibrate)navigator.vibrate(8); cmd('preset'+(i+1)); closePresets(); };
      g.appendChild(div);
    }
  } else {
    [...g.children].forEach((el,i)=>{
      const nm=presets[i]?.name||'';
      el.className='preset'+(nm?' has-name':'');
      el.querySelector('.preset-name').textContent=nm||'—';
    });
  }
}

// ── Volume ───────────────────────────────────────────────────────────────────
let volTooltipTimer=null;
function onVolInput(v) {
  updateVol(v);
  const tip=document.getElementById('vol-tooltip');
  tip.textContent=v; tip.classList.add('visible');
  clearTimeout(volTooltipTimer);
  volTooltipTimer=setTimeout(()=>tip.classList.remove('visible'), 1200);
}
function updateVol(v) {
  const pct=v+'%';
  document.getElementById('vol-track').style.setProperty('--pct',pct);
  document.getElementById('vol-slider').style.setProperty('--pct',pct);
  document.getElementById('vol-tooltip').style.left=pct;
}
let volD=null;
function sendVol(v) { clearTimeout(volD); volD=setTimeout(()=>{
  if (activeHost) fetch(`/api/cmd?host=${activeHost}&action=volume&value=${v}`);
}, 200); }
function nudgeVol(delta) {
  const s = document.getElementById('vol-slider');
  const v = Math.min(100, Math.max(0, parseInt(s.value) + delta));
  s.value = v; onVolInput(v); sendVol(v);
}

// ── Commands ─────────────────────────────────────────────────────────────────
function ripple(el, e) {
  const r = document.createElement('span');
  r.className = 'ripple';
  const rect = el.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  const x = (e.clientX||rect.left+rect.width/2) - rect.left - size/2;
  const y = (e.clientY||rect.top+rect.height/2) - rect.top - size/2;
  r.style.cssText=`width:${size}px;height:${size}px;left:${x}px;top:${y}px`;
  el.appendChild(r);
  r.addEventListener('animationend', ()=>r.remove());
}
async function cmd(a, el, e) {
  if (el && e) ripple(el, e);
  if (navigator.vibrate) navigator.vibrate(8);
  if (!activeHost) { toast('No speaker selected'); return; }
  await fetch(`/api/cmd?host=${activeHost}&action=${a}`);
  setTimeout(pollNow,500);
}

// ── Preset backup ────────────────────────────────────────────────────────────
async function backupPresets() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  try {
    const r = await fetch(`/api/presets/backup?host=${activeHost}`);
    const d = await r.json();
    toast(`Backed up ${(d.presets||[]).length} presets`);
    loadBackupInfo();
  } catch(e) { toast('Backup failed'); }
}
async function restorePresets() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  if (!confirm('Restore backed-up presets to this speaker? This will overwrite current presets.')) return;
  try {
    const r = await fetch(`/api/presets/restore?host=${activeHost}`);
    const d = await r.json();
    toast(d.ok ? `Restored ${d.count} presets` : (d.error||'Restore failed'));
  } catch(e) { toast('Restore failed'); }
}
async function loadBackupInfo() {
  if (!activeHost) return;
  try {
    const r = await fetch(`/api/presets/backup-info?host=${activeHost}`);
    const d = await r.json();
    const el = document.getElementById('backup-status');
    if (d.backed_up) {
      el.innerHTML = `Last backup: <strong style="color:var(--gold)">${d.backed_up}</strong>
                      — ${(d.presets||[]).length} presets saved`;
      const list = document.getElementById('backup-list');
      list.innerHTML = (d.presets||[]).map((p,i)=>`
        <div class="manage-card">
          <div class="mc-left">
            <div class="mc-name">${p.name||('Preset '+(i+1))}</div>
            <div class="mc-meta">${p.source} ${p.location?'• '+p.location:''}</div>
          </div>
        </div>`).join('');
    } else {
      el.textContent = 'No backup yet for this speaker.';
      document.getElementById('backup-list').innerHTML = '';
    }
  } catch(e){}
}

// ── Preset health check ──────────────────────────────────────────────────────
window._healthPresets = {};  // id → {name, location, source}
async function checkPresetHealth() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const btn = document.getElementById('btn-health-check');
  const box = document.getElementById('health-results');
  btn.textContent = 'Checking…'; btn.disabled = true;
  box.style.display = 'none'; box.innerHTML = '';
  try {
    const d = await (await fetch(`/api/presets/health?host=${activeHost}`)).json();
    if (d.error) { toast('Could not fetch preset data'); return; }
    // Store preset data keyed by id so onclick buttons can reference without escaping issues
    window._healthPresets = {};
    d.presets.forEach(p => { window._healthPresets[p.id] = p; });
    const summary = d.at_risk === 0
      ? `<div class="health-summary all-safe">✓ All ${d.total} presets are safe — no cloud dependency</div>`
      : `<div class="health-summary has-risk">⚠ ${d.at_risk} of ${d.total} preset${d.at_risk>1?'s':''} at risk — will stop working after the Bose cloud shuts down on 6 May 2026</div>`;
    const cards = d.presets.map(p => {
      const srcLine = p.label ? `${p.label} (${p.source})` : (p.source || 'Empty slot');
      const sug = p.suggestion ? `<div class="health-sug">→ ${p.suggestion}</div>` : '';
      const replBtn = (p.risk==='high'||p.risk==='unknown') ? `
        <button class="mc-btn" onclick="prefillCustomStation('${p.id}')"
          style="margin-top:6px;font-size:10px;padding:4px 8px">
          + Use as Custom Station template
        </button>` : '';
      return `<div class="health-card risk-${p.risk}">
        <div class="health-dot"></div>
        <div style="flex:1;min-width:0">
          <div class="health-name">Preset ${p.id}${p.name?' — '+p.name:''}</div>
          <div class="health-source">${srcLine}</div>
          ${sug}
          ${replBtn}
        </div>
      </div>`;
    }).join('');
    const note = d.data_source === 'backup'
      ? '<p style="font-size:11px;color:var(--fg3);margin-bottom:8px">⚠ Speaker offline — results based on last backup</p>' : '';
    box.innerHTML = note + summary + cards;
    box.style.display = 'block';
  } catch(e) { toast('Health check failed'); }
  finally { btn.textContent = '● Health Check'; btn.disabled = false; }
}

function prefillCustomStation(presetId) {
  const p = window._healthPresets[presetId];
  if (!p) return;
  const isDirectUrl = /^https?:\/\//i.test(p.location||'');
  // Switch to Presets tab and open Custom Radio Stations section
  switchTab('manage');
  const body = document.getElementById('sec-stations');
  const chev = document.getElementById('chev-stations');
  if (body && body.style.display === 'none') {
    body.style.display = 'block';
    if (chev) chev.classList.add('open');
    loadStations();
  }
  // Fill the form
  document.getElementById('st-name').value = p.name || '';
  document.getElementById('st-url').value  = isDirectUrl ? (p.location||'') : '';
  document.getElementById('st-art').value  = '';
  document.getElementById('st-search-results').style.display = 'none';
  // Scroll and auto-search if no direct URL
  const form = document.getElementById('add-form');
  if (form) form.scrollIntoView({behavior:'smooth', block:'center'});
  if (isDirectUrl) {
    toast('Form pre-filled — review then click Add Station');
    document.getElementById('st-url').focus();
  } else {
    // Auto-search RadioBrowser so the user can pick a direct stream
    setTimeout(() => searchStationStream(), 400);
  }
}

async function searchStationStream(nameOverride) {
  const name = nameOverride || document.getElementById('st-name').value.trim();
  if (!name) { toast('Enter a station name first'); return; }
  const resultsEl = document.getElementById('st-search-results');
  resultsEl.style.display = 'block';
  resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">Searching…</div>';
  try {
    const d = await (await fetch(`/api/stations/stream-search?q=${encodeURIComponent(name)}`)).json();
    if (d.error || !d.length) {
      resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">No results found — try a shorter name or paste the URL manually</div>';
      return;
    }
    resultsEl.innerHTML = d.map((s,i) => `
      <div class="sr-item" onclick="pickStreamResult(${i})">
        ${s.favicon ? `<img class="sr-logo" src="${s.favicon}" onerror="this.style.display='none'">` : '<div class="sr-logo"></div>'}
        <div class="sr-info">
          <div class="sr-name">${s.name}</div>
          <div class="sr-meta">${[s.country, s.bitrate?s.bitrate+'kbps':'', s.codec].filter(Boolean).join(' · ')}</div>
          <div class="sr-meta" style="font-size:9px;opacity:.6">${s.url}</div>
        </div>
        <div class="sr-use">Use ›</div>
      </div>`).join('');
    window._streamResults = d;
  } catch(e) {
    resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">Search failed</div>';
  }
}

function pickStreamResult(idx) {
  const s = (window._streamResults||[])[idx];
  if (!s) return;
  document.getElementById('st-url').value  = s.url;
  if (!document.getElementById('st-name').value) document.getElementById('st-name').value = s.name;
  if (s.favicon && !document.getElementById('st-art').value) document.getElementById('st-art').value = s.favicon;
  document.getElementById('st-search-results').style.display = 'none';
  toast('Stream URL selected — click Add Station to save');
}

// ── Custom stations ──────────────────────────────────────────────────────────
async function loadStations() {
  try {
    const stations = await (await fetch('/api/stations')).json();
    const el = document.getElementById('stations-list');
    if (!stations.length) { el.innerHTML='<p style="font-size:12px;color:var(--fg3)">No custom stations yet.</p>'; return; }
    el.innerHTML = stations.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">${s.stream_url}</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn" onclick="playStation('${s.id}')">Play</button>
          <button class="mc-btn" onclick="pushStation('${s.id}')">Set Preset</button>
          <button class="mc-btn danger" onclick="deleteStation('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  } catch(e){}
}
async function addStation() {
  const name = document.getElementById('st-name').value.trim();
  const url  = document.getElementById('st-url').value.trim();
  const art  = document.getElementById('st-art').value.trim();
  if (!name || !url) { toast('Name and URL are required'); return; }
  try {
    await fetch('/api/stations/add', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, stream_url:url, art_url:art})
    });
    document.getElementById('st-name').value='';
    document.getElementById('st-url').value='';
    document.getElementById('st-art').value='';
    toast('Station added'); loadStations();
  } catch(e) { toast('Failed to add station'); }
}
async function deleteStation(id) {
  if (!confirm('Delete this station?')) return;
  await fetch(`/api/stations/delete?id=${id}`);
  toast('Deleted'); loadStations();
}
async function playStation(id) {
  if (!activeHost) { toast('Select a speaker first'); return; }
  await fetch(`/api/stations/play?host=${activeHost}&id=${id}`);
  toast('Playing…'); setTimeout(pollNow,1000);
}
async function pushStation(id) {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const n = prompt('Which preset slot? (1-6)','1');
  if (!n || n<1 || n>6) return;
  await fetch(`/api/stations/set-preset?host=${activeHost}&id=${id}&slot=${n}`);
  toast(`Saved to preset ${n}`); setTimeout(pollNow,1000);
}

// ── Presets dropdown ─────────────────────────────────────────────────────────
function togglePresets() {
  const clip     = document.getElementById('presets-clip');
  const backdrop = document.getElementById('presets-backdrop');
  const btn      = document.getElementById('preset-toggle');
  const isOpen   = clip.classList.contains('open');
  if (isOpen) {
    closePresets();
  } else {
    // Position the clip right below the tabs bar so it doesn't matter
    // how tall the speaker chips section is
    const tabsEl = document.getElementById('tabs');
    const tabsBottom = tabsEl.getBoundingClientRect().bottom +
                       document.getElementById('app').scrollTop;
    clip.style.top = tabsBottom + 'px';
    clip.classList.add('open');
    backdrop.classList.add('open');
    btn.classList.add('open');
  }
}
function closePresets() {
  document.getElementById('presets-clip').classList.remove('open');
  document.getElementById('presets-backdrop').classList.remove('open');
  document.getElementById('preset-toggle').classList.remove('open');
}

// ── Groups ────────────────────────────────────────────────────────────────────
async function loadGroups() {
  if (!activeHost) {
    document.getElementById('group-status').innerHTML =
      '<p style="font-size:12px;color:var(--fg3)">Select a speaker first.</p>';
    document.getElementById('group-builder').innerHTML = '';
    return;
  }
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); }
  catch(e){ return; }

  const statusEl  = document.getElementById('group-status');
  const builderEl = document.getElementById('group-builder');

  // ─ Status card ─────────────────────────────────────────────────────────────
  if (zone.is_master) {
    const count = (zone.members||[]).length;
    statusEl.innerHTML = `<div class="manage-card">
      <div class="mc-left">
        <div class="mc-name">🔊 Group Master</div>
        <div class="mc-meta">${count} speaker${count!==1?'s':''} grouped</div>
      </div>
      <div class="mc-actions">
        <button class="mc-btn danger" onclick="dissolveGroup()">Dissolve</button>
      </div></div>`;
  } else if (zone.is_slave) {
    statusEl.innerHTML = `<div class="manage-card">
      <div class="mc-left">
        <div class="mc-name">🔉 Group Member</div>
        <div class="mc-meta">Following master at ${zone.master_ip||'unknown'}</div>
      </div></div>`;
  } else {
    statusEl.innerHTML =
      '<p style="font-size:12px;color:var(--fg3);margin-bottom:12px">Not in a group. Add speakers below to create one.</p>';
  }

  // Slaves can't be used to add/remove — only the master can
  if (zone.is_slave) { builderEl.innerHTML=''; return; }

  const others = speakers.filter(s=>s.host!==activeHost);
  if (!others.length) {
    builderEl.innerHTML =
      '<p style="font-size:12px;color:var(--fg3)">No other speakers found. Tap Scan to search again.</p>';
    return;
  }

  const memberIPs = new Set((zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost));
  builderEl.innerHTML = `
    <div class="section-label" style="margin-bottom:8px">Other Speakers</div>
    ${others.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">${s.host}${memberIPs.has(s.host)?' · In group':''}</div>
        </div>
        <div class="mc-actions">
          ${memberIPs.has(s.host)
            ? `<button class="mc-btn danger" onclick="removeFromGroup('${s.host}')">Remove</button>`
            : `<button class="mc-btn primary" onclick="addToGroup('${s.host}')">Add</button>`}
        </div>
      </div>`).join('')}
    <button class="mc-btn primary" onclick="groupAll()"
      style="width:calc(100% - 0px);margin-top:10px;padding:10px">
      Group All Speakers
    </button>`;
}

async function addToGroup(slaveHost) {
  if (!activeHost) return;
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); } catch(e){ return; }
  const existing = (zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost);
  if (!existing.includes(slaveHost)) existing.push(slaveHost);
  await fetch(`/api/group/create?master=${activeHost}&slaves=${existing.join(',')}`);
  toast('Group updated'); setTimeout(loadGroups, 700);
}

async function removeFromGroup(slaveHost) {
  if (!activeHost) return;
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); } catch(e){ return; }
  const remaining = (zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost && ip!==slaveHost);
  if (remaining.length) {
    await fetch(`/api/group/create?master=${activeHost}&slaves=${remaining.join(',')}`);
  } else {
    await fetch('/api/group/remove?host='+activeHost);
  }
  toast('Group updated'); setTimeout(loadGroups, 700);
}

async function dissolveGroup() {
  if (!confirm('Dissolve this group? All speakers will play independently.')) return;
  await fetch('/api/group/remove?host='+activeHost);
  toast('Group dissolved'); setTimeout(loadGroups, 700);
}

async function groupAll() {
  if (!activeHost) return;
  const slaves = speakers.filter(s=>s.host!==activeHost).map(s=>s.host).join(',');
  if (!slaves) { toast('No other speakers to group'); return; }
  await fetch(`/api/group/create?master=${activeHost}&slaves=${slaves}`);
  toast('All speakers grouped'); setTimeout(loadGroups, 700);
}


// ── Bass ──────────────────────────────────────────────────────────────────────
let bassTooltipTimer=null;
async function loadBass() {
  const row = document.getElementById('bass-row');
  if (!row || !activeHost) { if(row) row.style.display='none'; return; }
  try {
    const d = await (await fetch('/api/bass?host='+activeHost)).json();
    if (d.available) {
      document.getElementById('bass-slider').min = d.min;
      document.getElementById('bass-slider').max = d.max;
      document.getElementById('bass-slider').value = d.current;
      updateBass(d.current, d.min, d.max);
      row.style.display = 'block';
    } else {
      row.style.display = 'none';
    }
  } catch(e) { row.style.display='none'; }
}
function onBassInput(v) {
  const sl = document.getElementById('bass-slider');
  updateBass(v, parseInt(sl.min), parseInt(sl.max));
  const tip = document.getElementById('bass-tooltip');
  tip.textContent = v; tip.classList.add('visible');
  clearTimeout(bassTooltipTimer);
  bassTooltipTimer = setTimeout(()=>tip.classList.remove('visible'), 1200);
}
function updateBass(v, min=-9, max=0) {
  const pct = ((parseInt(v)-min)/(max-min)*100)+'%';
  const track = document.getElementById('bass-track');
  const slider = document.getElementById('bass-slider');
  if(track) { track.style.setProperty('--pct',pct); document.getElementById('bass-tooltip').style.left=pct; }
  if(slider) slider.style.setProperty('--pct',pct);
}
let bassD=null;
function sendBass(v) { clearTimeout(bassD); bassD=setTimeout(()=>{
  if (activeHost) fetch(`/api/cmd?host=${activeHost}&action=bass&value=${v}`);
}, 200); }

// ── Backup All ────────────────────────────────────────────────────────────────
async function backupAll() {
  const st = document.getElementById('backup-all-status');
  if(st) st.textContent = 'Backing up…';
  try {
    const d = await (await fetch('/api/presets/backup-all')).json();
    const ok = d.results.filter(r=>r.ok).length;
    const fail = d.results.filter(r=>!r.ok).length;
    if(st) st.textContent = `✓ ${ok} backed up${fail?' — '+fail+' failed':''}`;
    // Refresh speaker list so warning badges update
    await fetchSpeakers(false);
  } catch(e) { if(st) st.textContent='Backup failed'; }
}

// ── Rename ────────────────────────────────────────────────────────────────────
async function renameSpeaker() {
  const input = document.getElementById('rename-input');
  if (!input || !activeHost) return;
  const name = input.value.trim();
  if (!name) return;
  try {
    const d = await (await fetch(`/api/rename?host=${activeHost}&name=${encodeURIComponent(name)}`)).json();
    if (d.ok) {
      const sp = speakers.find(s=>s.host===activeHost);
      if (sp) sp.name = d.name;
      renderRooms();
      toast('Speaker renamed');
    }
  } catch(e) { toast('Rename failed'); }
}

// ── Settings — Speaker info ───────────────────────────────────────────────────
async function loadSpeakerInfo() {
  const el = document.getElementById('speaker-info');
  if (!el) return;
  if (!activeHost) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Select a speaker to view its details.</p>';
    return;
  }
  el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Loading…</p>';
  try {
    const d = await (await fetch('/api/device-info?host='+activeHost)).json();
    const sigColour = {Poor:'#ef4444',Fair:'#f59e0b',Good:'#4caf50',Excellent:'#4caf50'}[d.wifi_signal]||'var(--fg2)';
    const spotifyBadge = d.spotify_connect
      ? `<span style="font-size:10px;font-weight:700;color:#1db954;background:rgba(29,185,84,.1);
           border:1px solid rgba(29,185,84,.3);padding:2px 8px;border-radius:10px;
           letter-spacing:.04em;margin-left:6px">Spotify Connect</span>` : '';
    el.innerHTML = `<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px">
      <span style="font-size:14px;font-weight:700;color:var(--white)">${d.name||d.model||'Speaker'}</span>
      ${spotifyBadge}
    </div>
    <table class="speaker-info-table">
      <tr><td>Model</td><td>${d.model||'—'}</td></tr>
      <tr><td>Firmware</td><td>${d.firmware||'—'}</td></tr>
      <tr><td>IP Address</td><td>${d.ip||activeHost}</td></tr>
      <tr><td>MAC Address</td><td>${d.mac||'—'}</td></tr>
      <tr><td>Serial Number</td><td>${d.serial||'—'}</td></tr>
      <tr><td>Device ID</td><td>${d.device_id||'—'}</td></tr>
      <tr><td>Country / Region</td><td>${[d.country,d.region].filter((v,i,a)=>v&&a.indexOf(v)===i).join(' / ')||'—'}</td></tr>
      ${d.wifi_ssid?`<tr><td>Wi-Fi Network</td><td>${d.wifi_ssid}</td></tr>`:''}
      ${d.wifi_signal?`<tr><td>Signal Strength</td><td style="color:${sigColour};font-weight:700">${d.wifi_signal}${d.wifi_band?' · '+d.wifi_band:''}</td></tr>`:''}
    </table>
    <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
      <input id="rename-input" style="flex:1;background:var(--surface2);border:1px solid var(--border);
        color:var(--fg1);border-radius:8px;padding:6px 10px;font-size:13px"
        value="${d.name||''}" placeholder="Speaker name">
      <button class="mc-btn primary" onclick="renameSpeaker()">Rename</button>
    </div>
    <div id="bass-row" style="display:none;margin-top:16px">
      <div style="font-size:12px;color:var(--fg3);font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Bass</div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="bass-label">−</span>
        <div id="bass-track" style="flex:1;position:relative;padding-top:22px">
          <div id="bass-tooltip">0</div>
          <input type="range" id="bass-slider" min="-9" max="0" value="0"
                 oninput="onBassInput(this.value)" onchange="sendBass(this.value)">
        </div>
        <span class="bass-label">+</span>
      </div>
    </div>`;
    loadBass();
  } catch(e) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Could not load device info.</p>';
  }
}

// ── Settings — Alexa / Matter QR ─────────────────────────────────────────────
async function loadAlexaQR() {
  const box    = document.getElementById('qr-box');
  const manual = document.getElementById('qr-manual');
  const status = document.getElementById('qr-status');
  const badge  = document.getElementById('qr-status-badge');
  if (box) box.textContent = 'Loading…';
  try {
    const d = await (await fetch('/api/matter/qr')).json();
    if (box) box.textContent = d.qrText || '(QR not available)';
    if (manual) manual.textContent = d.manualPairingCode ? 'Manual code: ' + d.manualPairingCode : '';
    const ok = d.commissioned;
    if (badge) { badge.textContent = ok ? '✓ Commissioned' : 'Not commissioned';
                 badge.className = 'qr-collapse-badge ' + (ok ? 'ok' : 'warn'); }
    if (status) { status.textContent = ok
        ? '✓ Commissioned with Alexa — devices are available'
        : 'Not yet commissioned — Add Device → Other → Matter in the Alexa app';
      status.style.color = ok ? '#4caf50' : 'var(--fg2)'; }
  } catch(e) {
    if (box)   box.textContent = 'Bridge not running';
    if (badge) { badge.textContent = 'offline'; badge.className = 'qr-collapse-badge warn'; }
    if (status){ status.textContent = 'systemctl --user start soundtouch-matter';
                 status.style.color = 'var(--fg3)'; }
  }
}
function toggleSection(bodyId, chevronId) {
  const body    = document.getElementById(bodyId);
  const chevron = document.getElementById(chevronId);
  const opening = body.style.display === 'none';
  body.style.display = opening ? 'block' : 'none';
  if (chevron) chevron.classList.toggle('open', opening);
  if (opening && bodyId === 'sec-speaker')       loadSpeakerInfo();
  if (opening && bodyId === 'sec-alexa')         loadAlexaQR();
  if (opening && bodyId === 'sec-manage-backup') loadBackupInfo();
  if (opening && bodyId === 'sec-stations')      loadStations();
  if (opening && bodyId === 'sec-scenes')        loadScenes();
  if (opening && bodyId === 'sec-alarms')        loadAlarms();
  if (opening && bodyId === 'sec-announce')      loadAnnounceSection();
}
function loadAnnounceSection() {
  const container = document.getElementById('main-ann-speakers');
  if (!container || container.children.length) return;
  speakers.forEach(sp => {
    const id = 'main-ann-chk-' + sp.host.replace(/\./g,'_');
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;cursor:pointer;padding:7px 10px;' +
      'background:var(--surface2);border-radius:8px;border:1px solid var(--border)';
    row.innerHTML = `<input type="checkbox" id="${id}" checked style="accent-color:var(--blue);width:15px;height:15px">` +
      `<span style="font-size:13px;font-weight:600">${sp.name}</span>`;
    container.appendChild(row);
  });
  const volEl = document.getElementById('main-ann-vol');
  const lblEl = document.getElementById('main-ann-vol-lbl');
  if (volEl && lblEl) volEl.oninput = function() {
    lblEl.textContent = this.value;
    this.style.background = `linear-gradient(to right,var(--blue) ${this.value}%,var(--surface2) ${this.value}%)`;
  };
}
async function sendMainAnnounce() {
  const text = document.getElementById('main-ann-text').value.trim();
  const statusEl = document.getElementById('main-ann-status');
  if (!text) { statusEl.textContent = 'Please enter a message.'; return; }
  const hosts = speakers
    .filter(sp => document.getElementById('main-ann-chk-' + sp.host.replace(/\./g,'_'))?.checked)
    .map(sp => sp.host);
  if (!hosts.length) { statusEl.textContent = 'Select at least one speaker.'; return; }
  const volume = parseInt(document.getElementById('main-ann-vol').value);
  const accent = document.getElementById('main-ann-accent').value;
  statusEl.textContent = 'Sending…';
  try {
    const r = await fetch('/api/tts/announce', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, hosts, volume, accent})});
    const d = await r.json();
    statusEl.textContent = d.ok
      ? `Announcing on ${d.speakers} speaker${d.speakers!==1?'s':''}…`
      : 'Error: ' + (d.error||'unknown');
  } catch(e) { statusEl.textContent = 'Request failed.'; }
}
function toggleQR() {
  const body    = document.getElementById('qr-body');
  const chevron = document.getElementById('qr-chevron');
  const opening = body.style.display === 'none';
  body.style.display = opening ? 'block' : 'none';
  chevron.classList.toggle('open', opening);
  if (opening) loadAlexaQR();
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function setText(id,v) { const e=document.getElementById(id); if(e) e.textContent=v; }
let toastT;
function toast(m) {
  const t=document.getElementById('toast');
  t.textContent=m; t.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('show'),2400);
}
function showScanning(m) { document.getElementById('scan-label').textContent=m||'Scanning…';
  document.getElementById('scanning').classList.add('show'); }
function hideScanning() { document.getElementById('scanning').classList.remove('show'); }

// ── Backup JSON editor ────────────────────────────────────────────────────────
async function openBackupEditor() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const errEl = document.getElementById('backup-json-error');
  const ta    = document.getElementById('backup-json-editor');
  errEl.style.display = 'none';
  ta.value = 'Loading…';
  openModal('backup-json-modal');
  const spk = speakers.find(s => s.host === activeHost);
  document.getElementById('backup-json-title').textContent =
    'Backup JSON' + (spk ? ' — ' + spk.name : '');
  try {
    const d = await (await fetch('/api/presets/backup-json?host=' + activeHost)).json();
    if (d.error) {
      ta.value = '// No backup found for this speaker.\n// Click "Backup Now" first, then reopen.';
    } else {
      ta.value = JSON.stringify(d, null, 2);
    }
  } catch(e) { ta.value = '// Failed to load backup.'; }
}

async function _postBackupJson() {
  const ta    = document.getElementById('backup-json-editor');
  const errEl = document.getElementById('backup-json-error');
  errEl.style.display = 'none';
  let data;
  try { data = JSON.parse(ta.value); }
  catch(e) {
    errEl.textContent = 'Invalid JSON: ' + e.message;
    errEl.style.display = '';
    return null;
  }
  try {
    const r = await fetch('/api/presets/backup-json?host=' + activeHost, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const d = await r.json();
    if (!d.ok) {
      errEl.textContent = d.error || 'Save failed';
      errEl.style.display = '';
      return null;
    }
    return true;
  } catch(e) {
    errEl.textContent = 'Save failed';
    errEl.style.display = '';
    return null;
  }
}

async function saveBackupJson() {
  if (await _postBackupJson()) { toast('Backup saved'); }
}

async function saveAndRestoreJson() {
  if (!await _postBackupJson()) return;
  try {
    const d = await (await fetch('/api/presets/restore?host=' + activeHost)).json();
    if (d.ok) {
      toast('Saved & restored ' + d.count + ' preset' + (d.count !== 1 ? 's' : ''));
      closeModal('backup-json-modal');
      setTimeout(pollNow, 600);
    } else {
      const errEl = document.getElementById('backup-json-error');
      errEl.textContent = d.error || 'Restore failed';
      errEl.style.display = '';
    }
  } catch(e) {
    const errEl = document.getElementById('backup-json-error');
    errEl.textContent = 'Restore failed';
    errEl.style.display = '';
  }
}

// ── All-speaker volume ────────────────────────────────────────────────────────
let allVolTT=null;
function onAllVolInput(v) {
  const pct=v+'%';
  document.getElementById('all-vol-track').style.setProperty('--pct',pct);
  document.getElementById('all-vol-slider').style.setProperty('--pct',pct);
  const tip=document.getElementById('all-vol-tip');
  tip.style.left=pct; tip.textContent=v; tip.classList.add('visible');
  clearTimeout(allVolTT); allVolTT=setTimeout(()=>tip.classList.remove('visible'),1200);
}
function sendAllVol(v) { fetch(`/api/volume/all?value=${v}`); }
function nudgeAllVol(delta) {
  const s=document.getElementById('all-vol-slider');
  const v=Math.min(100,Math.max(0,parseInt(s.value)+delta));
  s.value=v; onAllVolInput(v); sendAllVol(v);
}

// ── Scenes ────────────────────────────────────────────────────────────────────
async function loadScenes() {
  const el=document.getElementById('scenes-list');
  if(!el)return;
  try {
    const scenes=await(await fetch('/api/scenes')).json();
    if(!scenes.length){
      el.innerHTML='<p style="font-size:12px;color:var(--fg3);margin-bottom:10px">No scenes saved yet.</p>';
      return;
    }
    el.innerHTML=scenes.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">Preset ${s.preset} · ${[s.master,...(s.slaves||[])].length} speaker(s)</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn primary" onclick="activateScene('${s.id}')">Play</button>
          <button class="mc-btn danger" onclick="deleteScene('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  }catch(e){}
}

async function saveScene() {
  if(!activeHost){toast('Select a speaker first');return;}
  const name=document.getElementById('scene-name-input').value.trim();
  if(!name){toast('Enter a scene name');return;}
  const presetSlot=parseInt(document.getElementById('scene-preset-input').value)||1;
  // Capture zone members
  let slaves=[];
  try{const z=await(await fetch('/api/group?host='+activeHost)).json();
      slaves=(z.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost);}catch(e){}
  // Capture volumes
  const hosts=[activeHost,...slaves];
  const volumes={};
  await Promise.all(hosts.map(async h=>{
    try{const st=await(await fetch('/api/state?host='+h)).json(); volumes[h]=st.volume||30;}catch(e){}
  }));
  try{
    await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,master:activeHost,slaves,volumes,preset:presetSlot})});
    document.getElementById('scene-name-input').value='';
    toast('Scene saved'); loadScenes();
  }catch(e){toast('Failed to save scene');}
}

async function activateScene(id) {
  try{
    const d=await(await fetch('/api/scenes/activate?id='+encodeURIComponent(id))).json();
    toast(d.ok?'Scene activated':'Could not activate scene');
    if(d.ok)setTimeout(pollNow,1200);
  }catch(e){toast('Failed');}
}

async function deleteScene(id) {
  if(!confirm('Delete this scene?'))return;
  await fetch('/api/scenes/delete?id='+encodeURIComponent(id));
  toast('Scene deleted'); loadScenes();
}

// ── Alarms ────────────────────────────────────────────────────────────────────
const ALARM_DAYS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

function updateAlarmSpeakerSelect() {
  const sel=document.getElementById('alarm-speaker-select');
  if(!sel)return;
  const cur=sel.value;
  sel.innerHTML='<option value="">Select a speaker…</option>'+
    speakers.map(s=>`<option value="${s.host}"${s.host===cur?' selected':''}>${s.name}</option>`).join('');
}

function _alarmHtml(a, closeModalId) {
  const dayStr=a.days.length===7?'Every day':
    (a.days.length===5&&!a.days.includes(5)&&!a.days.includes(6)?'Weekdays':
     a.days.map(d=>ALARM_DAYS[d]).join(', '));
  const spk=speakers.find(s=>s.host===a.host);
  const closeArg=closeModalId?`,'${closeModalId}'`:'';
  return`<div class="manage-card">
    <div class="mc-left">
      <div class="mc-name">${a.name} · ${a.time}</div>
      <div class="mc-meta">${spk?spk.name:a.host} · Preset ${a.preset} · ${dayStr}${a.volume!=null?' · Vol '+a.volume:''}</div>
    </div>
    <div class="mc-actions">
      <button class="mc-btn${a.enabled?' primary':''}" onclick="toggleAlarm('${a.id}',${!a.enabled}${closeArg})">${a.enabled?'On':'Off'}</button>
      <button class="mc-btn danger" onclick="deleteAlarm('${a.id}'${closeArg})">✕</button>
    </div>
  </div>`;
}

async function loadAlarms() {
  const el=document.getElementById('alarms-list');
  if(!el)return;
  updateAlarmSpeakerSelect();
  try{
    const alarms=await(await fetch('/api/alarms')).json();
    el.innerHTML=alarms.length
      ?alarms.map(a=>_alarmHtml(a)).join('')
      :'<p style="font-size:12px;color:var(--fg3);margin-bottom:10px">No alarms set.</p>';
  }catch(e){}
}

async function addAlarm() {
  const host=document.getElementById('alarm-speaker-select').value;
  if(!host){toast('Select a speaker');return;}
  const time=document.getElementById('alarm-time').value;
  if(!time){toast('Set a time');return;}
  const days=[];
  document.querySelectorAll('.alarm-day-chk:checked').forEach(cb=>days.push(parseInt(cb.value)));
  if(!days.length){toast('Select at least one day');return;}
  const name=document.getElementById('alarm-name').value.trim()||'Alarm';
  const preset=parseInt(document.getElementById('alarm-preset').value)||1;
  const volRaw=document.getElementById('alarm-vol').value;
  const volume=volRaw!==''?parseInt(volRaw):null;
  try{
    await fetch('/api/alarms',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,host,preset,time,days,volume})});
    document.getElementById('alarm-name').value='';
    toast('Alarm saved'); loadAlarms();
  }catch(e){toast('Failed to save alarm');}
}

async function deleteAlarm(id, modalId) {
  if(!confirm('Delete this alarm?'))return;
  await fetch('/api/alarms/delete?id='+id);
  toast('Alarm deleted'); loadAlarms();
  if(modalId) _refreshAlarmsModal();
}

async function toggleAlarm(id, enabled, modalId) {
  await fetch(`/api/alarms/toggle?id=${id}&enabled=${enabled}`);
  loadAlarms();
  if(modalId) _refreshAlarmsModal();
}

// ── Modals ────────────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id)?.classList.add('open'); }
function closeModal(id) { document.getElementById(id)?.classList.remove('open'); }

async function _refreshScenesModal() {
  const el=document.getElementById('scenes-modal-body'); if(!el)return;
  try{
    const scenes=await(await fetch('/api/scenes')).json();
    if(!scenes.length){el.innerHTML='<p style="font-size:12px;color:var(--fg3)">No scenes saved yet.</p>';return;}
    el.innerHTML=scenes.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">Preset ${s.preset} · ${[s.master,...(s.slaves||[])].length} speaker(s)</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn primary" onclick="activateScene('${s.id}');closeModal('scenes-modal')">Play</button>
          <button class="mc-btn danger" onclick="deleteSceneModal('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  }catch(e){el.innerHTML='<p style="font-size:12px;color:var(--fg3)">Failed to load scenes.</p>';}
}

async function _refreshAlarmsModal() {
  const el=document.getElementById('alarms-modal-body'); if(!el)return;
  try{
    const alarms=await(await fetch('/api/alarms')).json();
    el.innerHTML=alarms.length
      ?alarms.map(a=>_alarmHtml(a,'alarms-modal')).join('')
      :'<p style="font-size:12px;color:var(--fg3)">No alarms set.</p>';
  }catch(e){}
}

async function openScenesModal() { openModal('scenes-modal'); await _refreshScenesModal(); }
async function openAlarmsModal() { openModal('alarms-modal'); await _refreshAlarmsModal(); }

async function deleteSceneModal(id) {
  if(!confirm('Delete this scene?'))return;
  await fetch('/api/scenes/delete?id='+encodeURIComponent(id));
  toast('Scene deleted'); loadScenes(); _refreshScenesModal();
}
</script>
</body>
</html>
"""

WALL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>SoundTouch — Wall Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0f;
  --surface:#18181c;
  --surface2:#222228;
  --border:#2a2a32;
  --fg:#e8e8f0;
  --fg2:#a0a0b8;
  --fg3:#5a5a72;
  --blue:#2277ee;
  --blue-light:#60a5fa;
  --blue-glow:rgba(34,119,238,.35);
  --amber:#f59e0b;
  --amber-dim:#92600a;
  --silver:#c8c8d8;
  --radius:16px;
}
html,body{height:100%;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  overflow:hidden}

/* ── Header ───────────────────────────────────── */
#header{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 28px 12px;
  border-bottom:1px solid var(--border);
  flex-shrink:0;
}
#header-logo{display:flex;align-items:center;gap:10px}
#header-logo svg{opacity:.7}
#header-title{font-size:15px;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--fg2)}
#clock{font-size:28px;font-weight:300;letter-spacing:.04em;
  font-variant-numeric:tabular-nums;color:var(--fg)}
#date-line{font-size:11px;color:var(--fg3);text-align:right;margin-top:2px;
  letter-spacing:.04em}

/* ── Grid ─────────────────────────────────────── */
#grid{
  display:grid;
  gap:16px;
  padding:16px 20px 20px;
  flex:1;
  overflow:hidden;
  /* columns set by JS based on speaker count */
}
body{display:flex;flex-direction:column;height:100vh}

/* ── Room card ────────────────────────────────── */
.room-card{
  position:relative;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  overflow:hidden;
  display:flex;
  flex-direction:column;
  min-height:0;
  transition:border-color .3s;
}
.room-card.playing{border-color:rgba(34,119,238,.4)}

/* blurred art backdrop */
.card-bg{
  position:absolute;inset:0;
  background-size:cover;background-position:center;
  filter:blur(32px) saturate(1.4);
  opacity:0;
  transition:opacity .8s,background-image .8s;
  transform:scale(1.15);
  pointer-events:none;
}
.room-card.has-art .card-bg{opacity:.18}

/* card body */
.card-inner{
  position:relative;
  display:flex;
  flex:1;
  gap:0;
  min-height:0;
  padding:46px 20px 12px;
  align-items:center;
  gap:20px;
}

/* art square */
.card-art{
  flex-shrink:0;
  width:100px;height:100px;
  border-radius:10px;
  overflow:hidden;
  background:var(--surface2);
  position:relative;
}
.card-art img{width:100%;height:100%;object-fit:cover;display:block}
.card-art-placeholder{
  width:100%;height:100%;
  display:flex;align-items:center;justify-content:center;
}
.card-art-placeholder svg{opacity:.25}

/* eq bars inside art */
.card-eq{
  position:absolute;bottom:6px;right:6px;
  display:flex;align-items:flex-end;gap:2px;
  opacity:0;transition:opacity .3s;
}
.room-card.playing .card-eq{opacity:1}
.card-eq span{
  display:block;width:3px;border-radius:2px;
  background:var(--blue-light);
  animation:none;
}
.room-card.playing .card-eq span:nth-child(1){animation:eq 0.9s ease-in-out infinite alternate}
.room-card.playing .card-eq span:nth-child(2){animation:eq 0.7s ease-in-out .15s infinite alternate}
.room-card.playing .card-eq span:nth-child(3){animation:eq 1.1s ease-in-out .05s infinite alternate}
.room-card.playing .card-eq span:nth-child(4){animation:eq 0.8s ease-in-out .25s infinite alternate}
@keyframes eq{from{height:4px}to{height:18px}}

/* info */
.card-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.card-room{
  position:absolute;top:14px;left:18px;
  font-size:15px;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--fg);z-index:1;
}
.card-track{
  font-size:17px;font-weight:600;color:var(--fg);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.card-artist{
  font-size:13px;color:var(--fg2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.card-badges{display:flex;gap:6px;margin-top:4px;flex-wrap:wrap;align-items:center}
.badge{
  font-size:9px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  padding:2px 7px;border-radius:8px;white-space:nowrap;
}
.badge-source{color:var(--blue-light);border:1px solid rgba(96,165,250,.3);background:rgba(96,165,250,.08)}
.badge-cloud{color:var(--amber);border:1px solid var(--amber-dim);background:rgba(245,158,11,.08)}
.badge-group{color:var(--amber);border:1px solid var(--amber-dim);background:rgba(245,158,11,.08)}

/* ── Controls row ─────────────────────────────── */
.card-controls{
  flex-shrink:0;
  padding:0 20px 18px;
  display:flex;align-items:center;
  gap:14px;
}
.ctrl-btn{
  background:none;border:none;color:var(--fg2);cursor:pointer;
  padding:6px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  transition:color .15s,background .15s;
  flex-shrink:0;
}
.ctrl-btn:hover{color:var(--fg);background:var(--surface2)}
.ctrl-btn:active{color:var(--blue-light)}
.ctrl-btn.play-btn{
  width:44px;height:44px;border-radius:50%;
  background:var(--surface2);border:1px solid var(--border);
  color:var(--fg);
}
.ctrl-btn.play-btn.playing{
  background:rgba(34,119,238,.15);border-color:var(--blue);color:var(--blue-light)
}
.ctrl-btn.power-btn{color:var(--fg3)}
.ctrl-btn.power-btn.playing{color:var(--blue-light)}
.ctrl-btn.muted{color:var(--amber)}

/* volume */
.vol-wrap{flex:1;display:flex;align-items:center;gap:8px;min-width:0}
.vol-label{font-size:10px;font-weight:700;color:var(--fg3);
  font-variant-numeric:tabular-nums;width:26px;text-align:right;flex-shrink:0}
input[type=range].wall-vol{
  flex:1;height:3px;-webkit-appearance:none;appearance:none;
  border-radius:2px;outline:none;cursor:pointer;min-width:0;
  background:linear-gradient(to right,var(--blue) var(--vp,50%),var(--surface2) var(--vp,50%));
}
input[type=range].wall-vol::-webkit-slider-thumb{
  -webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--silver);cursor:pointer;
  box-shadow:0 0 6px var(--blue-glow);
}
input[type=range].wall-vol::-moz-range-thumb{
  width:14px;height:14px;border-radius:50%;
  background:var(--silver);cursor:pointer;border:none;
}
.offline-overlay{
  position:absolute;inset:0;
  background:rgba(13,13,15,.6);
  display:flex;align-items:center;justify-content:center;
  border-radius:var(--radius);
  font-size:13px;color:var(--fg3);letter-spacing:.05em;
  backdrop-filter:blur(2px);
}
.room-card{cursor:pointer}
.room-card.selected{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 0 18px var(--blue-glow)}
.room-card.selected .card-room{color:var(--blue-light)}

/* ── Header preset bar ────────────────────────── */
#preset-bar{
  display:flex;align-items:center;gap:8px;
  flex:1;justify-content:center;
  padding:0 24px;
}
#preset-bar-label{
  font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--fg3);white-space:nowrap;margin-right:4px;
}
.hdr-preset{
  background:var(--surface2);border:1px solid var(--border);
  color:var(--fg2);border-radius:10px;
  padding:6px 14px;font-size:12px;font-weight:600;
  cursor:pointer;white-space:nowrap;max-width:130px;
  overflow:hidden;text-overflow:ellipsis;
  transition:background .15s,border-color .15s,color .15s;
  flex-shrink:1;
}
.hdr-preset:hover{background:var(--surface);color:var(--fg);border-color:var(--blue)}
.hdr-preset:active{border-color:var(--blue-light);color:var(--blue-light)}
.hdr-preset.empty{opacity:.3;pointer-events:none}

/* ── Announce button & modal ──────────────────── */
#announce-btn{
  background:var(--surface2);border:1px solid var(--border);
  color:var(--fg2);border-radius:10px;padding:7px 16px;
  font-size:13px;font-weight:600;cursor:pointer;
  display:flex;align-items:center;gap:7px;flex-shrink:0;
  transition:background .15s,border-color .15s,color .15s;
}
#announce-btn:hover{background:var(--surface);color:var(--fg);border-color:var(--blue)}
#announce-overlay{
  display:none;position:fixed;inset:0;z-index:100;
  background:rgba(0,0,0,.65);backdrop-filter:blur(4px);
  align-items:center;justify-content:center;
}
#announce-overlay.open{display:flex}
#announce-box{
  background:var(--surface);border:1px solid var(--border);
  border-radius:18px;padding:28px;width:min(520px,94vw);
  display:flex;flex-direction:column;gap:18px;
}
#announce-box h2{font-size:17px;font-weight:700;color:var(--fg)}
#announce-text{
  width:100%;padding:12px 14px;
  background:var(--surface2);border:1px solid var(--border);
  border-radius:10px;color:var(--fg);font-size:16px;
  outline:none;resize:none;line-height:1.4;
  font-family:inherit;
}
#announce-text:focus{border-color:var(--blue)}
.ann-section-label{font-size:11px;font-weight:700;letter-spacing:.07em;
  text-transform:uppercase;color:var(--fg3);margin-bottom:6px}
.ann-speakers{display:flex;flex-direction:column;gap:8px}
.ann-spk{display:flex;align-items:center;gap:10px;cursor:pointer;
  padding:8px 12px;background:var(--surface2);border-radius:8px;
  border:1px solid var(--border);transition:border-color .15s}
.ann-spk:has(input:checked){border-color:var(--blue)}
.ann-spk input{accent-color:var(--blue);width:16px;height:16px;cursor:pointer}
.ann-spk-name{font-size:13px;font-weight:600;color:var(--fg)}
.ann-vol-row{display:flex;align-items:center;gap:10px}
.ann-vol-row input{flex:1;height:3px;-webkit-appearance:none;appearance:none;
  border-radius:2px;outline:none;cursor:pointer;
  background:linear-gradient(to right,var(--blue) var(--avp,60%),var(--surface2) var(--avp,60%));}
.ann-vol-row input::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--silver);box-shadow:0 0 6px var(--blue-glow);}
.ann-vol-row input::-moz-range-thumb{width:14px;height:14px;border-radius:50%;
  background:var(--silver);border:none;}
.ann-vol-lbl{font-size:12px;font-weight:700;color:var(--fg2);width:28px;text-align:right}
.ann-btns{display:flex;gap:10px;justify-content:flex-end}
.ann-btn{padding:10px 22px;border-radius:10px;font-size:14px;font-weight:600;
  border:1px solid var(--border);cursor:pointer;transition:all .15s}
.ann-btn.cancel{background:var(--surface2);color:var(--fg2)}
.ann-btn.cancel:hover{color:var(--fg)}
.ann-btn.send{background:var(--blue);border-color:var(--blue);color:#fff}
.ann-btn.send:hover{filter:brightness(1.15)}
.ann-btn.send:disabled{opacity:.4;cursor:default;filter:none}
#announce-status{font-size:12px;color:var(--amber);min-height:16px;text-align:center}
</style>
</head>
<body>

<div id="header">
  <div id="header-logo" style="display:flex;align-items:center;gap:16px">
    <div style="display:flex;align-items:center;gap:10px">
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
        <circle cx="11" cy="11" r="10" stroke="#60a5fa" stroke-width="1.5"/>
        <circle cx="11" cy="11" r="5.5" stroke="#60a5fa" stroke-width="1.5"/>
        <circle cx="11" cy="11" r="1.5" fill="#60a5fa"/>
      </svg>
      <span id="header-title">SoundTouch</span>
    </div>
    <button id="announce-btn" onclick="openAnnounce()">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M2 6h2l5-4v12l-5-4H2a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z" stroke="currentColor" stroke-width="1.4" fill="none"/>
        <path d="M11 5.5c1 .8 1.5 1.5 1.5 2.5s-.5 1.7-1.5 2.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        <path d="M12.5 3.5C14.2 4.9 15 6.4 15 8s-.8 3.1-2.5 4.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
      </svg>
      Announce
    </button>
  </div>
  <div id="preset-bar">
    <span id="preset-bar-label" style="display:none"></span>
  </div>
  <div style="text-align:right;flex-shrink:0">
    <div id="clock">--:--</div>
    <div id="date-line"></div>
  </div>
</div>

<div id="grid"></div>

<script>
'use strict';
const DAYS=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const MONTHS=['January','February','March','April','May','June','July',
               'August','September','October','November','December'];

// ── Clock ──────────────────────────────────────────────────────────────────
function tickClock(){
  const now=new Date();
  const h=String(now.getHours()).padStart(2,'0');
  const m=String(now.getMinutes()).padStart(2,'0');
  document.getElementById('clock').textContent=h+':'+m;
  document.getElementById('date-line').textContent=
    DAYS[now.getDay()]+', '+now.getDate()+' '+MONTHS[now.getMonth()]+' '+now.getFullYear();
}
tickClock(); setInterval(tickClock,10000);

// ── State ──────────────────────────────────────────────────────────────────
let speakers=[], lastState={}, activeHost=null;

async function api(url){
  try{const r=await fetch(url); return r.ok?await r.json():null;}
  catch{return null;}
}

// ── Card selection & presets ───────────────────────────────────────────────
function selectCard(host){
  activeHost=host;
  document.querySelectorAll('.room-card').forEach(c=>c.classList.remove('selected'));
  const card=document.getElementById('card-'+host.replace(/\./g,'_'));
  if(card) card.classList.add('selected');
  renderPresets();
}

function renderPresets(){
  const bar=document.getElementById('preset-bar');
  const label=document.getElementById('preset-bar-label');
  // remove old preset buttons (keep label)
  bar.querySelectorAll('.hdr-preset').forEach(b=>b.remove());
  if(!activeHost){label.style.display='none'; return;}
  const d=lastState[activeHost];
  const sp=speakers.find(s=>s.host===activeHost);
  if(sp){label.textContent=sp.name; label.style.display='';}
  const presets=(d&&d.presets)||[];
  for(let i=0;i<6;i++){
    const nm=presets[i]?.name||'';
    const btn=document.createElement('button');
    btn.className='hdr-preset'+(nm?'':' empty');
    btn.textContent=nm||`Preset ${i+1}`;
    btn.title=nm||`Preset ${i+1}`;
    if(nm) btn.onclick=()=>cmd(activeHost,'preset'+(i+1));
    bar.appendChild(btn);
  }
}

// ── Card builder ───────────────────────────────────────────────────────────
function buildCard(sp){
  const id=sp.host.replace(/\./g,'_');
  const card=document.createElement('div');
  card.className='room-card'; card.id='card-'+id;
  card.addEventListener('click',e=>{
    // ignore clicks on interactive controls
    if(e.target.closest('button,input')) return;
    selectCard(sp.host);
  });
  card.innerHTML=`
    <div class="card-bg" id="bg-${id}"></div>
    <div class="card-room">${sp.name}</div>
    <div class="card-inner">
      <div class="card-art" id="art-${id}">
        <img id="artimg-${id}" src="" alt="" style="display:none">
        <div class="card-art-placeholder" id="artph-${id}">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
            <circle cx="20" cy="20" r="18" stroke="currentColor" stroke-width="1.5"/>
            <circle cx="20" cy="20" r="8" stroke="currentColor" stroke-width="1.5"/>
            <circle cx="20" cy="20" r="2.5" fill="currentColor"/>
          </svg>
        </div>
        <div class="card-eq">
          <span style="height:8px"></span><span style="height:14px"></span>
          <span style="height:6px"></span><span style="height:11px"></span>
        </div>
      </div>
      <div class="card-info">
        <div class="card-track" id="track-${id}">—</div>
        <div class="card-artist" id="artist-${id}"></div>
        <div class="card-badges" id="badges-${id}"></div>
      </div>
      <div style="display:flex;flex-direction:column;gap:10px;align-items:flex-end;flex-shrink:0">
        <button class="ctrl-btn power-btn" id="power-${id}" onclick="cmd('${sp.host}','power')" title="Power">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path d="M10 3v7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
            <path d="M6.3 5.3A7 7 0 1 0 13.7 5.3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/>
          </svg>
        </button>
        <button class="ctrl-btn" id="mute-${id}" onclick="cmd('${sp.host}','mute')" title="Mute">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" id="ico-vol-${id}">
            <polygon points="3,6 7,6 11,2 11,16 7,12 3,12" stroke="currentColor" stroke-width="1.5" fill="none"/>
            <path d="M13 6.5c.8.8 1.3 1.9 1.3 3.1s-.5 2.3-1.3 3.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none" id="ico-vol-lines-${id}"/>
            <line x1="13" y1="6.5" x2="15" y2="8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" id="ico-mute-x1-${id}" style="display:none"/>
            <line x1="15" y1="6.5" x2="13" y2="8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" id="ico-mute-x2-${id}" style="display:none"/>
          </svg>
        </button>
      </div>
    </div>
    <div class="card-controls">
      <button class="ctrl-btn" onclick="cmd('${sp.host}','prev')" title="Previous">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
          <polygon points="18,4 8,11 18,18" fill="currentColor"/>
          <rect x="4" y="4" width="3" height="14" rx="1.5" fill="currentColor"/>
        </svg>
      </button>
      <button class="ctrl-btn play-btn" id="play-${id}" onclick="cmd('${sp.host}','playpause')" title="Play/Pause">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
          <polygon id="ico-play-${id}" points="8,5 18,11 8,17" fill="currentColor"/>
          <g id="ico-pause-${id}" style="display:none">
            <rect x="5" y="4" width="4" height="14" rx="2" fill="currentColor"/>
            <rect x="13" y="4" width="4" height="14" rx="2" fill="currentColor"/>
          </g>
        </svg>
      </button>
      <button class="ctrl-btn" onclick="cmd('${sp.host}','next')" title="Next">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
          <polygon points="4,4 14,11 4,18" fill="currentColor"/>
          <rect x="15" y="4" width="3" height="14" rx="1.5" fill="currentColor"/>
        </svg>
      </button>
      <div class="vol-wrap">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style="flex-shrink:0;opacity:.5">
          <polygon points="1,5 5,5 8,2 8,12 5,9 1,9" stroke="currentColor" stroke-width="1.3" fill="none"/>
        </svg>
        <input type="range" class="wall-vol" id="vol-${id}"
          min="0" max="100" value="20"
          oninput="onVol(this,'${sp.host}')"
          onchange="sendVol(this,'${sp.host}')">
        <div class="vol-label" id="vollbl-${id}">20</div>
      </div>
    </div>
    <div class="offline-overlay" id="offline-${id}" style="display:none">Offline</div>
  `;
  return card;
}

// ── Apply state to a card ──────────────────────────────────────────────────
function applyCard(host, d){
  const id=host.replace(/\./g,'_');
  const card=document.getElementById('card-'+id); if(!card) return;
  const offline=document.getElementById('offline-'+id);

  if(!d || d.error){offline.style.display=''; return;}
  offline.style.display='none';

  // playing state
  card.classList.toggle('playing', !!d.playing);
  const playBtn=document.getElementById('play-'+id);
  playBtn?.classList.toggle('playing',!!d.playing);
  document.getElementById('ico-play-'+id).style.display=d.playing?'none':'';
  document.getElementById('ico-pause-'+id).style.display=d.playing?'':'none';

  // power
  document.getElementById('power-'+id)?.classList.toggle('playing',!!d.playing);

  // mute
  const muteBtn=document.getElementById('mute-'+id);
  muteBtn?.classList.toggle('muted',!!d.muted);
  document.getElementById('ico-vol-lines-'+id).style.display=d.muted?'none':'';
  document.getElementById('ico-mute-x1-'+id).style.display=d.muted?'':'none';
  document.getElementById('ico-mute-x2-'+id).style.display=d.muted?'':'none';

  // track info
  const track=d.track||(d.source||'—');
  document.getElementById('track-'+id).textContent=track;
  document.getElementById('artist-'+id).textContent=d.artist||d.album||'';

  // badges
  const bEl=document.getElementById('badges-'+id);
  let badges='';
  if(d.source) badges+=`<span class="badge badge-source">${d.source}</span>`;
  if(d.cloud_warning) badges+=`<span class="badge badge-cloud" title="${d.cloud_warning}">⚠ Cloud</span>`;
  if(d.group_role==='master') badges+=`<span class="badge badge-group">Master</span>`;
  if(d.group_role==='member') badges+=`<span class="badge badge-group">Member</span>`;
  bEl.innerHTML=badges;

  // album art
  const img=document.getElementById('artimg-'+id);
  const ph=document.getElementById('artph-'+id);
  const bg=document.getElementById('bg-'+id);
  if(d.art){
    if(img.dataset.src!==d.art){
      img.dataset.src=d.art;
      const tmp=new Image();
      tmp.onload=()=>{
        img.src=d.art; img.style.display='block'; ph.style.display='none';
        card.classList.add('has-art');
        bg.style.backgroundImage=`url('${d.art}')`;
      };
      tmp.onerror=()=>{
        img.style.display='none'; ph.style.display='';
        card.classList.remove('has-art'); bg.style.backgroundImage='';
      };
      tmp.src=d.art;
    }
  } else {
    img.style.display='none'; ph.style.display='';
    card.classList.remove('has-art'); bg.style.backgroundImage='';
    img.dataset.src='';
  }

  // volume
  const sl=document.getElementById('vol-'+id);
  const lbl=document.getElementById('vollbl-'+id);
  if(sl && !sl.matches(':active')){
    sl.value=d.volume;
    const pct=(d.volume/100*100).toFixed(1)+'%';
    sl.style.setProperty('--vp',pct);
    lbl.textContent=d.volume;
  }
}

// ── Commands ───────────────────────────────────────────────────────────────
function cmd(host,action,value=''){
  api('/api/cmd?host='+host+'&action='+action+(value?'&value='+value:''))
    .then(()=>pollOne(host));
}
function onVol(el,host){
  const pct=(el.value/100*100).toFixed(1)+'%';
  el.style.setProperty('--vp',pct);
  const id=host.replace(/\./g,'_');
  document.getElementById('vollbl-'+id).textContent=el.value;
}
function sendVol(el,host){ cmd(host,'volume',el.value); }

// ── Polling ────────────────────────────────────────────────────────────────
async function pollOne(host){
  const d=await api('/api/state?host='+host);
  lastState[host]=d; applyCard(host,d);
  if(host===activeHost) renderPresets();
}
function pollAll(){ return Promise.all(speakers.map(s=>pollOne(s.host))); }

// ── Init ───────────────────────────────────────────────────────────────────
async function init(){
  speakers=await api('/api/speakers')||[];
  const grid=document.getElementById('grid');

  // responsive column count
  const n=speakers.length;
  const cols=n<=1?1:n<=2?2:n<=4?2:3;
  grid.style.gridTemplateColumns=`repeat(${cols},1fr)`;
  // rows fill viewport
  const rows=Math.ceil(n/cols);
  grid.style.gridTemplateRows=`repeat(${rows},1fr)`;

  speakers.forEach(sp=>{
    const card=buildCard(sp);
    grid.appendChild(card);
  });

  pollAll().then(()=>{
    if(speakers.length) selectCard(speakers[0].host);
  });
  setInterval(pollAll, 4000);
}

init();

// ── Announce modal ─────────────────────────────────────────────────────────
function openAnnounce(){
  // Populate speaker checkboxes
  const list=document.getElementById('ann-speakers-list');
  list.innerHTML='';
  speakers.forEach(sp=>{
    const id='ann-chk-'+sp.host.replace(/\./g,'_');
    const row=document.createElement('label');
    row.className='ann-spk';
    row.innerHTML=`<input type="checkbox" id="${id}" checked>
      <span class="ann-spk-name">${sp.name}</span>`;
    list.appendChild(row);
  });
  document.getElementById('announce-status').textContent='';
  document.getElementById('announce-text').value='';
  document.getElementById('announce-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('announce-text').focus(),80);
}
function closeAnnounce(){
  document.getElementById('announce-overlay').classList.remove('open');
}
function onAnnVol(el){
  const pct=((el.value-el.min)/(el.max-el.min)*100).toFixed(1)+'%';
  el.style.setProperty('--avp',pct);
  document.getElementById('ann-vol-lbl').textContent=el.value;
}
async function sendAnnounce(){
  const text=document.getElementById('announce-text').value.trim();
  if(!text){document.getElementById('announce-status').textContent='Please enter a message.';return;}
  const hosts=speakers
    .filter(sp=>document.getElementById('ann-chk-'+sp.host.replace(/\./g,'_'))?.checked)
    .map(sp=>sp.host);
  if(!hosts.length){document.getElementById('announce-status').textContent='Select at least one speaker.';return;}
  const volume=parseInt(document.getElementById('ann-vol-slider').value);
  const accent=document.getElementById('ann-accent').value;
  const btn=document.getElementById('ann-send-btn');
  btn.disabled=true;
  document.getElementById('announce-status').textContent='Sending…';
  try{
    const r=await fetch('/api/tts/announce',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,hosts,volume,accent})});
    const d=await r.json();
    if(d.ok){
      document.getElementById('announce-status').textContent=
        `Announcing on ${d.speakers} speaker${d.speakers!==1?'s':''}…`;
      setTimeout(closeAnnounce,1800);
    } else {
      document.getElementById('announce-status').textContent='Error: '+(d.error||'unknown');
    }
  }catch(e){
    document.getElementById('announce-status').textContent='Request failed.';
  }finally{ btn.disabled=false; }
}
document.getElementById('announce-text')?.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)) sendAnnounce();
});
// Close on backdrop click
document.getElementById('announce-overlay')?.addEventListener('click',e=>{
  if(e.target===e.currentTarget) closeAnnounce();
});
</script>

<!-- Announce modal -->
<div id="announce-overlay">
  <div id="announce-box">
    <h2>📢 Send Announcement</h2>
    <div>
      <div class="ann-section-label">Message</div>
      <textarea id="announce-text" rows="3" placeholder="e.g. Dinner is ready…"></textarea>
      <div style="font-size:10px;color:var(--fg3);margin-top:4px">Ctrl+Enter to send</div>
    </div>
    <div>
      <div class="ann-section-label">Accent / Voice</div>
      <select id="ann-accent" class="form-select" style="margin-bottom:4px">
        <option value="british">🇬🇧 British</option>
        <option value="american">🇺🇸 American</option>
        <option value="irish">🇮🇪 Irish</option>
        <option value="australian">🇦🇺 Australian</option>
        <option value="posh">🎩 Posh English</option>
        <option value="gangster">🔪 Gangster (Danny Dyer)</option>
      </select>
    </div>
    <div>
      <div class="ann-section-label">Speakers</div>
      <div class="ann-speakers" id="ann-speakers-list"></div>
    </div>
    <div>
      <div class="ann-section-label">Announcement Volume</div>
      <div class="ann-vol-row">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style="opacity:.5;flex-shrink:0">
          <polygon points="1,5 5,5 8,2 8,12 5,9 1,9" stroke="currentColor" stroke-width="1.3" fill="none"/>
        </svg>
        <input type="range" id="ann-vol-slider" min="10" max="100" value="60"
          oninput="onAnnVol(this)" style="--avp:55.6%">
        <div class="ann-vol-lbl" id="ann-vol-lbl">60</div>
      </div>
    </div>
    <div id="announce-status"></div>
    <div class="ann-btns">
      <button class="ann-btn cancel" onclick="closeAnnounce()">Cancel</button>
      <button class="ann-btn send" id="ann-send-btn" onclick="sendAnnounce()">Send</button>
    </div>
  </div>
</div>

</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# TTS announcement engine
# ═══════════════════════════════════════════════════════════════════════════════

def _tts_transform_posh(text):
    """Rewrite text to sound a bit more frightfully posh, what."""
    import re
    swaps = [
        (r'\bhello\b',        'good day'),
        (r'\bhi\b',           'good day'),
        (r'\bhey\b',          'I say'),
        (r'\bthanks\b',       'many thanks'),
        (r'\bthank you\b',    'thank you ever so much'),
        (r'\byes\b',          'quite'),
        (r'\byeah\b',         'quite'),
        (r'\byep\b',          'quite'),
        (r'\bno\b',           "I'm afraid not"),
        (r'\bnope\b',         "I'm afraid not"),
        (r'\bgood\b',         'rather good'),
        (r'\bgreat\b',        'splendid'),
        (r'\bexcellent\b',    'simply marvellous'),
        (r'\bfantastic\b',    'simply marvellous'),
        (r'\bamazing\b',      'jolly impressive'),
        (r'\bcool\b',         'frightfully good'),
        (r'\bok\b',           'very well'),
        (r'\bokay\b',         'very well'),
        (r'\bdinner\b',       'supper'),
        (r'\blunch\b',        'luncheon'),
        (r'\bbreakfast\b',    'breakfast'),
        (r'\bfood\b',         'nourishment'),
        (r'\bcar\b',          'motor car'),
        (r'\bhouse\b',        'residence'),
        (r'\bhome\b',         'residence'),
        (r'\bmoney\b',        'funds'),
        (r'\bkids\b',         'children'),
        (r'\bguy\b',          'chap'),
        (r'\bguys\b',         'chaps'),
        (r'\bman\b',          'chap'),
        (r'\bmen\b',          'chaps'),
        (r'\bwoman\b',        'lady'),
        (r'\bwomen\b',        'ladies'),
        (r'\bfriend\b',       'dear friend'),
        (r'\bsorry\b',        'I do beg your pardon'),
        (r'\bplease\b',       'if you would be so kind'),
        (r'\bwant\b',         'should rather like'),
        (r'\bneed\b',         'require'),
        (r'\bgoing to\b',     'shall'),
        (r'\bgonna\b',        'shall'),
        (r'\bproblem\b',      'frightful inconvenience'),
        (r'\bready\b',        'quite prepared'),
    ]
    result = text
    for pattern, replacement in swaps:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def _tts_transform_gangster(text):
    """Danny Dyer-esque East London rewrite, innit."""
    import re
    swaps = [
        (r'\bhello\b',        'oi oi'),
        (r'\bhi\b',           'alright'),
        (r'\bhey\b',          'oi'),
        (r'\bgood day\b',     'aight'),
        (r'\bthank you\b',    'cheers mate'),
        (r'\bthanks\b',       'cheers'),
        (r'\byes\b',          'yeah mate'),
        (r'\byep\b',          'yeah'),
        (r'\bno\b',           'nah'),
        (r'\bnope\b',         'nah'),
        (r'\bgood\b',         'proper'),
        (r'\bvery\b',         'proper'),
        (r'\bgreat\b',        'blinding'),
        (r'\bexcellent\b',    'blinding'),
        (r'\bfantastic\b',    'blinding'),
        (r'\bamazing\b',      'mental'),
        (r'\bcool\b',         'well tasty'),
        (r'\bok\b',           'sorted'),
        (r'\bokay\b',         'sorted'),
        (r'\bready\b',        'sorted'),
        (r'\bproblem\b',      'bovver'),
        (r'\bhouse\b',        'gaff'),
        (r'\bhome\b',         'gaff'),
        (r'\bmoney\b',        'dough'),
        (r'\bcar\b',          'motor'),
        (r'\bwife\b',         'missus'),
        (r'\bgirlfriend\b',   'bird'),
        (r'\bfriend\b',       'geezer'),
        (r'\bman\b',          'geezer'),
        (r'\bguy\b',          'geezer'),
        (r'\bguys\b',         'geezers'),
        (r'\bpeople\b',       'lot'),
        (r'\bplease\b',       'do us a favour and'),
        (r'\bsorry\b',        'my bad'),
        (r'\byou\b',          'ya'),
        (r'\byour\b',         'yer'),
        (r'\bgoing to\b',     'gonna'),
        (r'\bwant to\b',      'wanna'),
        (r'\bneed to\b',      'gotta'),
        (r'\bfood\b',         'grub'),
        (r'\bdinner\b',       'grub'),
        (r'\blunch\b',        'bit of grub'),
        (r'\bpolice\b',       'old bill'),
    ]
    result = text
    for pattern, replacement in swaps:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = result.rstrip()
    if result and not result[-1] in '.?!':
        result += ', innit'
    return result


# Map accent name → (gTTS tld, optional text transform function)
_TTS_ACCENT_MAP = {
    "british":    ("co.uk",    None),
    "american":   ("com",      None),
    "irish":      ("ie",       None),
    "australian": ("com.au",   None),
    "posh":       ("co.uk",    _tts_transform_posh),
    "gangster":   ("co.uk",    _tts_transform_gangster),
}


def _tts_announce(devices, text, volume, web_port, accent="british"):
    """Generate TTS MP3, serve it, play on each device, then restore state."""
    import io
    if not _TTS_AVAILABLE:
        log.error("[TTS] gTTS not installed — run: pip3 install gtts")
        return
    tld, transform = _TTS_ACCENT_MAP.get(accent, ("co.uk", None))
    tts_text = transform(text) if transform else text
    if transform:
        log.info(f"[TTS] accent={accent} transform: {text!r} → {tts_text!r}")
    try:
        buf = io.BytesIO()
        _gTTS(tts_text, lang="en", tld=tld).write_to_fp(buf)
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

    def do_GET(self):
        p    = urlparse(self.path)
        path = p.path
        qs   = parse_qs(p.query)
        # Log all API calls; /api/state is noisy so keep it at DEBUG
        if path.startswith("/api/"):
            lvl = logging.DEBUG if path == "/api/state" else logging.INFO
            log.log(lvl, f"[API GET ] {self.path}")

        if path in ("/", "/index.html"):
            self._html(HTML)

        elif path in ("/wall", "/wall.html", "/tab", "/panel"):
            self._html(WALL_HTML)

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
            self._json(dev.state() if dev else {"error":"no_device"})

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
                    dev.preset(int(action.replace("preset",""))); ok=True
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
                    result.append({"id": p.get("id",""), "name": name, "source": src,
                                   "risk": "safe", "label": src.replace("_"," ").title(), "suggestion": "",
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
                count = 0
                for p in data.get("presets",[]):
                    pid = p.get("id","")
                    if pid and p.get("source"):
                        dev.store_preset(pid, p.get("name",""),
                                         p["source"], p.get("type",""),
                                         p.get("location",""), p.get("account",""))
                        count += 1
                self._json({"ok":True,"count":count})

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
                local_ip = get_local_ip()
                loc = f"http://{local_ip}:{self.server_state.web_port}/api/station-desc/{sid}"
                dev.select_content("LOCAL_INTERNET_RADIO","stationurl",loc,st["name"])
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
                local_ip = get_local_ip()
                loc = f"http://{local_ip}:{self.server_state.web_port}/api/station-desc/{sid}"
                dev.store_preset(slot, st["name"], "LOCAL_INTERNET_RADIO", "stationurl", loc)
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
                dev.remove_zone()
                for d in list(self.server_state.devices): d.invalidate_zone_cache()
                self._json({"ok":True})
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
            self._respond(200, "application/javascript; charset=utf-8",
                          SW_JS.encode())

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

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        p    = urlparse(self.path)
        path = p.path
        qs   = parse_qs(p.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if path.startswith("/api/"):
            log.info(f"[API POST] {path}  body={body[:300].decode('utf-8','replace')}")

        if path == "/api/stations/add":
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
                accent  = data.get("accent", "british")
                if accent not in _TTS_ACCENT_MAP:
                    accent = "british"
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
                        dedup_key = (text, ",".join(sorted(hosts)), accent)
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
                                args=(devices, text, volume, self.server_state.web_port, accent),
                                daemon=True
                            ).start()
                            self._json({"ok": True, "speakers": len(devices)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self._respond(404, "text/plain", b"Not found")

    def _json(self, obj):
        payload = json.dumps(obj)
        p = urlparse(self.path).path
        lvl = logging.DEBUG if p == "/api/state" else logging.INFO
        log.log(lvl, f"[API RESP] {p} → {payload[:400]}")
        self._respond(200, "application/json", payload.encode())

    def _html(self, s):
        self._respond(200, "text/html; charset=utf-8", s.encode())

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

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
