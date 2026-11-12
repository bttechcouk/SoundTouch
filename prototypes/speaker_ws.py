#!/usr/bin/env python3
"""Prototype for #77: listen to a SoundTouch speaker's push notifications
instead of polling it.

Speakers publish state changes (now playing, volume, preset selection, zone…)
over a WebSocket on port 8080, subprotocol "gabbo". This connects to one
speaker and prints every notification with a millisecond timestamp, so the
delay between a physical button press and the notification can be compared
with the controller's 2-second poll.

Uses the controller's own SpeakerSocket (a small stdlib-only RFC 6455 client),
which now drives preset-button handling for UPnP-only speakers; this script
stays as a way to watch what a speaker pushes.

    python3 prototypes/speaker_ws.py 10.10.10.128            # until Ctrl-C
    python3 prototypes/speaker_ws.py 10.10.10.128 --seconds 600
"""
import argparse
import datetime
import pathlib
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from soundtouch_controller import SpeakerSocket  # noqa: E402


def summarise(xml_text):
    """One line saying what a notification is about."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "unparsed"
    if root.tag != "updates":
        return root.tag
    out = []
    for u in root:
        if u.tag == "nowPlayingUpdated":
            np = u.find("nowPlaying")
            ci = np.find("ContentItem") if np is not None else None
            out.append("nowPlaying source=%s status=%s item=%s" % (
                np.get("source") if np is not None else "?",
                np.findtext("playStatus") if np is not None else "?",
                (ci.findtext("itemName") or ci.get("location") or "") if ci is not None else ""))
        elif u.tag == "volumeUpdated":
            out.append("volume target=%s actual=%s" % (u.findtext("volume/targetvolume"),
                                                        u.findtext("volume/actualvolume")))
        elif u.tag == "nowSelectionUpdated":
            p = u.find("preset")
            out.append("presetSelected id=%s name=%s" % (
                p.get("id") if p is not None else "?",
                p.findtext("ContentItem/itemName") if p is not None else "?"))
        else:
            out.append(u.tag)
    return "; ".join(out) or "updates (empty)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("host")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl-C)")
    ap.add_argument("--raw", action="store_true", help="also print each notification's XML")
    a = ap.parse_args()

    stamp = lambda: datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    ws = SpeakerSocket(a.host)
    print(f"{stamp()}  connected to {a.host}:8080 (gabbo)", flush=True)
    end = time.monotonic() + a.seconds if a.seconds else None
    try:
        while end is None or time.monotonic() < end:
            msg = ws.recv(timeout=1)
            if msg is None:
                continue
            print(f"{stamp()}  {summarise(msg)}", flush=True)
            if a.raw:
                print(f"              {msg}", flush=True)
    except KeyboardInterrupt:
        pass
    except ConnectionError as e:
        print(f"{stamp()}  disconnected: {e}", flush=True)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
