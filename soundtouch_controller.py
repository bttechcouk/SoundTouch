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
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not found.  Run:  pip3 install requests")
    sys.exit(1)

WEB_PORT      = 8888
DATA_DIR      = pathlib.Path(__file__).parent / "data"
PRESETS_DIR   = DATA_DIR / "presets"
STATIONS_DIR  = DATA_DIR / "stations"
LOG_FILE      = pathlib.Path(__file__).parent / "soundtouch.log"


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

class SoundTouchDevice:
    def __init__(self, host, port=8090):
        self.host  = host
        self.port  = port
        self.url   = f"http://{host}:{port}"
        self.name      = host
        self.model     = ""
        self.mac       = ""
        self.device_id = ""

    # ── low-level ─────────────────────────────────────────────────────────────
    def _get(self, path, timeout=4):
        url = f"{self.url}{path}"
        log.debug(f"[SPK GET ] {url}")
        try:
            r = requests.get(url, timeout=timeout)
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
            r = requests.post(url, data=body,
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

    # ── state snapshot ────────────────────────────────────────────────────────
    def state(self):
        d = dict(host=self.host, name=self.name, model=self.model,
                 volume=0, muted=False, source="", track="", artist="",
                 album="", art="", playing=False, presets=[])
        # volume
        vx = self._get("/volume")
        if vx is not None:
            for t in ("actualvolume","targetvolume"):
                el = vx.find(t)
                if el is not None:
                    d["volume"] = int(el.text); break
            me = vx.find("muteenabled")
            if me is not None:
                d["muted"] = me.text.lower() == "true"
        # now playing
        np = self._get("/now_playing")
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
        # presets
        d["presets"] = self.get_presets_detail()
        return d

    def get_presets_detail(self):
        """Return list of dicts with full preset info for backup / display."""
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
    def get_zone(self):
        """Return zone membership info for this speaker."""
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
        return {
            "is_master": is_master,
            "is_slave":  is_slave,
            "master_id": master_id,
            "master_ip": master_ip,
            "members":   members,
        }

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
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="theme-color" content="#1a0a0a">
<title>SoundTouch</title>
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
#source-badge{font-size:10px;font-weight:700;letter-spacing:.06em;
  color:var(--blue-light);background:var(--surface);border:1px solid var(--border);
  padding:3px 8px;border-radius:10px;white-space:nowrap;align-self:center;flex-shrink:0}

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

/* Cloud source warning */
#cloud-warning{margin:10px 4px 0;background:rgba(245,158,11,.08);
  border:1px solid var(--amber-dim);border-radius:8px;
  padding:8px 12px;font-size:11px;color:var(--amber);line-height:1.5;display:none}
#cloud-warning strong{color:var(--amber)}

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

.qr-section{margin-top:18px;padding:16px;background:var(--surface1);
  border:1px solid var(--border);border-radius:var(--radius)}
.qr-section h3{margin:0 0 12px;font-size:13px;color:var(--fg1)}
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

/* ── Toast ───────────────────────────────────────────────── */
#toast{position:fixed;bottom:32px;left:50%;transform:translateX(-50%);
  background:rgba(19,21,29,.96);color:var(--blue-light);border:1px solid var(--blue-dim);
  padding:9px 20px;border-radius:22px;font-size:13px;font-weight:600;
  opacity:0;pointer-events:none;transition:opacity .25s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  white-space:nowrap;box-shadow:0 4px 24px rgba(0,0,0,.5)}
#toast.show{opacity:1}

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
      <button id="preset-toggle" onclick="togglePresets()">
        Presets <span class="arrow">▼</span>
      </button>
      <button id="scan-btn" onclick="rescan()">Scan</button>
    </div>
  </header>

  <!-- Tabs -->
  <div id="tabs">
    <div class="tab active" data-tab="player" onclick="switchTab('player')">Player</div>
    <div class="tab"        data-tab="manage" onclick="switchTab('manage')">Presets</div>
    <div class="tab"        data-tab="groups"  onclick="switchTab('groups')">Groups</div>
    <div class="tab"        data-tab="alexa"  onclick="switchTab('alexa')">Alexa</div>
  </div>

  <!-- Speaker chips -->
  <div id="rooms-section">
    <div class="section-label">Speakers</div>
    <div id="rooms-list"><div id="no-speakers">Scanning…</div></div>
  </div>

  <!-- ═══ PAGE: Player ═══ -->
  <div id="page-player" class="page visible" style="padding:0 20px">

    <div id="art-wrap">
      <img id="art" src="" alt="" class="hidden">
      <div id="art-placeholder">&#9835;</div>
    </div>

    <div id="track-info">
      <div id="track-text">
        <div id="track-name">—</div>
        <div id="track-artist"></div>
      </div>
      <div id="source-badge" style="display:none"></div>
    </div>

    <div id="cloud-warning">
      ⚠ <strong id="cloud-source-name"></strong> requires Bose cloud —
      will stop working when the cloud shuts down on 6 May 2026.
      Use a Custom Station or local backup instead.
    </div>

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
      <button class="t-btn t-btn-sm" onclick="cmd('prev')" title="Previous">
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
          <polygon points="28,8 14,18 28,28" fill="currentColor"/>
          <rect x="8" y="8" width="4" height="20" rx="2" fill="currentColor"/>
        </svg>
      </button>
      <button class="t-btn" id="btn-play" onclick="cmd('playpause')">
        <svg id="ico-play" width="30" height="30" viewBox="0 0 32 32">
          <polygon points="8,4 28,16 8,28" fill="currentColor"/>
        </svg>
        <svg id="ico-pause" width="30" height="30" viewBox="0 0 32 32" style="display:none">
          <rect x="5" y="4" width="8" height="24" rx="2" fill="currentColor"/>
          <rect x="19" y="4" width="8" height="24" rx="2" fill="currentColor"/>
        </svg>
      </button>
      <button class="t-btn t-btn-sm" onclick="cmd('next')" title="Next">
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
          <polygon points="8,8 22,18 8,28" fill="currentColor"/>
          <rect x="24" y="8" width="4" height="20" rx="2" fill="currentColor"/>
        </svg>
      </button>
    </div>

    <div id="power-row">
      <button id="btn-power" onclick="cmd('power')">
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path d="M8 1v6M4.5 3.5A5 5 0 1 0 11.5 3.5" stroke="currentColor"
                stroke-width="1.6" stroke-linecap="round" fill="none"/>
        </svg>
        Power
      </button>
      <button id="btn-mute" onclick="cmd('mute')">
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
    </div>
  </div>

  <!-- ═══ PAGE: Alexa ═══ -->
  <div id="page-alexa" class="page">
    <div class="manage-section">
      <h2>Alexa Integration</h2>

      <div class="alexa-hint">
        <strong>How it works:</strong> A separate Matter bridge process runs alongside
        this app, exposing each speaker preset and power toggle as a Matter On/Off
        device — <strong>no cloud, no account linking.</strong><br><br>
        <strong>Step 1 —</strong> Scan for speakers (tap Scan in the header)<br>
        <strong>Step 2 —</strong> Commission the Matter bridge once in the Alexa app:<br>
        &nbsp;&nbsp;Add Device → Other → Matter → scan QR code below<br>
        <strong>Step 3 —</strong> Use phrases like:<br>
        &nbsp;&nbsp;<span class="alexa-phrase">Alexa, turn on KISSTORY in Kitchen Bose</span><br>
        &nbsp;&nbsp;<span class="alexa-phrase">Alexa, turn on Kitchen Bose power</span><br><br>
        <strong>Bridge logs:</strong>
        <span class="alexa-phrase">journalctl --user -u soundtouch-matter -f</span>
      </div>

      <div class="qr-section">
        <h3>Commission Matter Bridge</h3>
        <div id="qr-box" class="qr-box">Loading…</div>
        <div id="qr-manual" class="qr-manual"></div>
        <div id="qr-status" class="qr-status"></div>
        <br><button class="qr-refresh" onclick="loadAlexaQR()">Refresh</button>
      </div>
    </div>
  </div>

  <!-- ═══ PAGE: Manage Presets ═══ -->
  <div id="page-manage" class="page">

    <!-- Backup section -->
    <div class="manage-section">
      <h2>Preset Backup</h2>
      <p id="backup-info" style="font-size:12px;color:var(--fg3);margin-bottom:12px">
        Back up your current presets locally so they survive the Bose cloud shutdown.
      </p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="mc-btn primary" onclick="backupPresets()">Backup Now</button>
        <button class="mc-btn" onclick="restorePresets()">Restore to Speaker</button>
      </div>
      <div id="backup-status" style="font-size:12px;color:var(--fg3);margin-top:10px"></div>
      <div id="backup-list" style="margin-top:12px"></div>
    </div>

    <!-- Custom stations section -->
    <div class="manage-section">
      <h2>Custom Radio Stations</h2>
      <p style="font-size:12px;color:var(--fg3);margin-bottom:12px">
        Add your own internet radio streams. These use LOCAL_INTERNET_RADIO and don't need the Bose cloud.
      </p>

      <!-- Add station form -->
      <div class="add-form" id="add-form">
        <label>Station Name</label>
        <input id="st-name" placeholder="e.g. BBC Radio 1">
        <label>Stream URL (HTTP)</label>
        <input id="st-url" placeholder="http://stream.live.vc.bbcmedia.co.uk/bbc_radio_one">
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

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  fetchSpeakers(false); schedPoll(); loadStations();
  const savedTab = localStorage.getItem('activeTab');
  if (savedTab) switchTab(savedTab);
});

// ── Tabs ─────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('visible', p.id === 'page-' + name));
  if (name === 'manage') { loadStations(); loadBackupInfo(); }
  if (name === 'groups')  { loadGroups(); }
  if (name === 'alexa')   { loadAlexaQR(); }
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
  if (!speakers.length) { el.innerHTML='<div id="no-speakers">No speakers found</div>'; return; }
  // Single speaker: full-width; 2+: 2-column grid
  el.style.gridTemplateColumns = speakers.length === 1 ? '1fr' : 'repeat(2,1fr)';
  el.innerHTML = speakers.map(s=>`
    <div class="room-chip${s.host===activeHost?' active':''}"
         id="chip-${s.host.replace(/\./g,'_')}"
         onclick="setActive('${s.host}')">
      <span class="dot"></span><span class="name">${s.name}</span></div>`).join('');
}
function setActive(h) {
  activeHost=h; clearTimeout(pollTimer); renderRooms(); pollNow();
  const tab = document.querySelector('.tab.active')?.dataset?.tab;
  if (tab === 'manage') loadBackupInfo();
  if (tab === 'groups') loadGroups();
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
let bgPollTimer = null;
async function bgPollAll() {
  for (const s of speakers) {
    if (s.host === activeHost) continue;
    try {
      const st = await (await fetch('/api/state?host='+s.host)).json();
      speakerErrors[s.host] = 0;
      setChipOffline(s.host, false);
      const chip = document.getElementById('chip-'+s.host.replace(/\./g,'_'));
      if (chip) chip.classList.toggle('playing', st.playing);
    } catch(e) {
      speakerErrors[s.host] = (speakerErrors[s.host]||0) + 1;
      if (speakerErrors[s.host] >= 2) setChipOffline(s.host, true);
    }
  }
  bgPollTimer = setTimeout(bgPollAll, 12000);
}
setTimeout(bgPollAll, 5000); // stagger start so it doesn't clash with boot poll
function applyState(d) {
  if (!d) return; lastState = d;
  const track = d.track||(d.source||'—'), artist = d.artist||d.album||'';
  setText('track-name',track); setText('track-artist',artist);
  const badge=document.getElementById('source-badge');
  badge.textContent=d.source||''; badge.style.display=d.source?'':'none';
  // art
  const artEl=document.getElementById('art'), ph=document.getElementById('art-placeholder');
  if (d.art && d.art!==lastArt) {
    lastArt=d.art; const tmp=new Image();
    tmp.onload=()=>{artEl.src=d.art;artEl.classList.remove('hidden');ph.style.display='none'};
    tmp.onerror=()=>{artEl.classList.add('hidden');ph.style.display=''};
    tmp.src=d.art;
  } else if (!d.art) { artEl.classList.add('hidden'); ph.style.display=''; }
  // cloud-source warning
  const CLOUD_SOURCES=['AMAZON','DEEZER','SIRIUSXM','IHEART','PANDORA','TUNEIN'];
  const srcUp=(d.source||'').toUpperCase();
  const warnEl=document.getElementById('cloud-warning');
  const warnSrc=document.getElementById('cloud-source-name');
  const isCloud=CLOUD_SOURCES.some(s=>srcUp.includes(s));
  warnEl.style.display=isCloud?'':'none';
  if(isCloud) warnSrc.textContent=d.source;
  // play icon
  document.getElementById('ico-play').style.display=d.playing?'none':'';
  document.getElementById('ico-pause').style.display=d.playing?'':'none';
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
      div.onclick=()=>{ cmd('preset'+(i+1)); closePresets(); };
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
async function cmd(a) {
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


// ── Alexa / Matter QR ────────────────────────────────────────────────────────
async function loadAlexaQR() {
  const box    = document.getElementById('qr-box');
  const manual = document.getElementById('qr-manual');
  const status = document.getElementById('qr-status');
  box.textContent = 'Loading…';
  manual.textContent = '';
  status.textContent = '';
  try {
    const d = await (await fetch('/api/matter/qr')).json();
    if (d.qrText) {
      box.textContent = d.qrText;
    } else {
      box.textContent = '(QR not available)';
    }
    manual.textContent = d.manualPairingCode ? 'Manual code: ' + d.manualPairingCode : '';
    if (d.commissioned) {
      status.textContent = '✓ Already commissioned with Alexa';
      status.style.color = 'var(--green)';
    } else {
      status.textContent = 'Not yet commissioned — scan QR in Alexa app: Add Device → Other → Matter';
      status.style.color = 'var(--fg2)';
    }
  } catch(e) {
    box.textContent = 'Bridge not running';
    status.textContent = 'Start the Matter bridge: systemctl --user start soundtouch-matter';
    status.style.color = 'var(--fg3)';
  }
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
</script>
</body>
</html>
"""


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

        # ── speaker list / scan ───────────────────────────────────────────────
        elif path == "/api/speakers":
            self._json([{"host":d.host,"name":d.name,"model":d.model}
                        for d in self.server_state.devices])

        elif path == "/api/scan":
            self.server_state.scan()
            self._json([{"host":d.host,"name":d.name,"model":d.model}
                        for d in self.server_state.devices])

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
                elif action.startswith("preset"):
                    dev.preset(int(action.replace("preset",""))); ok=True
            self._json({"ok":ok})

        # ── preset backup / restore ───────────────────────────────────────────
        elif path == "/api/presets/backup":
            host = qs.get("host",[None])[0]
            dev = self.server_state.get_device(host)
            if dev:
                presets = dev.get_presets_detail()
                data = self.server_state.store.backup_presets(host, presets)
                self._json(data)
            else:
                self._json({"error":"no_device"})

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
                self._json({"ok":True})

        elif path == "/api/group/remove":
            host = qs.get("host",[None])[0]
            dev  = self.server_state.get_device(host)
            if dev:
                dev.remove_zone(); self._json({"ok":True})
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
                log.info(f"[GROUP] Join — master={master.host} "
                         f"slaves={[d.host for d in existing_slaves]}")
                self._json({"ok": True, "master": master.host,
                            "slaves": [d.host for d in existing_slaves]})

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

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        p    = urlparse(self.path)
        path = p.path
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
        self.devices  = []
        self._lock    = threading.Lock()
        self.store    = PresetStore()
        self.web_port = web_port

    def scan(self):
        log.info("Scanning network…")
        found = discover_all(timeout=3)
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
