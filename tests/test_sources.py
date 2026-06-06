"""SoundTouchDevice.get_sources / has_local_internet_radio with mocked /sources XML.

These exercise the parsing path without touching a real speaker by stubbing the
low-level `_get`, which returns a parsed ElementTree element (or None on error).
"""
import xml.etree.ElementTree as ET

import soundtouch_controller as stc


def _device_with_sources(xml_text):
    """Build a device whose _get('/sources') returns the parsed XML (None for others)."""
    dev = stc.SoundTouchDevice("192.168.1.50")
    dev._get = lambda path, timeout=4: (ET.fromstring(xml_text) if path == "/sources" else None)
    return dev


SOURCES_WITH_LIR = """
<sources>
  <sourceItem source="LOCAL_INTERNET_RADIO" status="READY" isLocal="true">Radio</sourceItem>
  <sourceItem source="BLUETOOTH" status="READY" isLocal="true">Bluetooth</sourceItem>
  <sourceItem source="AUX" status="READY" isLocal="true">AUX</sourceItem>
</sources>
"""

SOURCES_WITHOUT_LIR = """
<sources>
  <sourceItem source="BLUETOOTH" status="READY" isLocal="true">Bluetooth</sourceItem>
  <sourceItem source="AUX" status="READY" isLocal="true">AUX</sourceItem>
</sources>
"""

SOURCES_WITH_SKIPPED = """
<sources>
  <sourceItem source="LOCAL_INTERNET_RADIO" status="READY" isLocal="true">Radio</sourceItem>
  <sourceItem source="NOTIFICATION" status="READY">Notify</sourceItem>
  <sourceItem source="STORED_MUSIC_MEDIA_RENDERER" status="READY">Renderer</sourceItem>
  <sourceItem source="STORED_MUSIC" sourceAccount="storedmusicusername" status="READY">Music</sourceItem>
</sources>
"""


def test_get_sources_parses_items():
    dev = _device_with_sources(SOURCES_WITH_LIR)
    srcs = {s["source"] for s in dev.get_sources()}
    assert srcs == {"LOCAL_INTERNET_RADIO", "BLUETOOTH", "AUX"}


def test_get_sources_marks_local_flag():
    dev = _device_with_sources(SOURCES_WITH_LIR)
    bt = next(s for s in dev.get_sources() if s["source"] == "BLUETOOTH")
    assert bt["isLocal"] is True


def test_get_sources_filters_skipped_sources_and_accounts():
    dev = _device_with_sources(SOURCES_WITH_SKIPPED)
    srcs = {s["source"] for s in dev.get_sources()}
    # NOTIFICATION + STORED_MUSIC_MEDIA_RENDERER skipped by source;
    # STORED_MUSIC skipped by its skipped sourceAccount.
    assert srcs == {"LOCAL_INTERNET_RADIO"}


def test_has_local_internet_radio_true():
    dev = _device_with_sources(SOURCES_WITH_LIR)
    assert dev.has_local_internet_radio() is True


def test_has_local_internet_radio_false_for_kitchen_like():
    dev = _device_with_sources(SOURCES_WITHOUT_LIR)
    assert dev.has_local_internet_radio() is False


def test_has_local_internet_radio_failsafe_on_error():
    # _get returns None (speaker unreachable / parse error) -> get_sources() == []
    # has_local_internet_radio must fail safe to True so normal speakers aren't broken.
    dev = stc.SoundTouchDevice("192.168.1.50")
    dev._get = lambda path, timeout=4: None
    assert dev.get_sources() == []
    assert dev.has_local_internet_radio() is True
