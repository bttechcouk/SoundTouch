"""DLNAServer: DIDL-Lite item/Browse generation, XML escaping, stream URLs."""
import xml.etree.ElementTree as ET

import soundtouch_controller as stc


def test_stream_url(dlna):
    assert dlna.stream_url("abc") == "http://192.168.1.10:8888/dlna/stream/abc"


def test_esc_escapes_xml_metacharacters():
    assert stc.DLNAServer._esc('a & b < c > d "e"') == "a &amp; b &lt; c &gt; d &quot;e&quot;"


def test_item_xml_contains_title_and_stream(dlna, store):
    store.save_station("s1", "Jazz & Blues", "https://x/jazz")
    item = dlna._item_xml(store.get_station("s1"))
    assert 'id="station/s1"' in item
    assert "<dc:title>Jazz &amp; Blues</dc:title>" in item
    assert "http://192.168.1.10:8888/dlna/stream/s1" in item
    assert "object.item.audioItem.audioBroadcast" in item


def test_browse_root_metadata_is_container(dlna, store):
    store.save_station("a", "A", "https://x/a")
    store.save_station("b", "B", "https://x/b")
    didl, returned, total = dlna.browse_response("0", "BrowseMetadata")
    assert (returned, total) == (1, 1)
    assert 'childCount="2"' in didl
    assert "object.container" in didl


def test_browse_root_direct_children_lists_all_stations(dlna, store):
    store.save_station("a", "A", "https://x/a")
    store.save_station("b", "B", "https://x/b")
    didl, returned, total = dlna.browse_response("0", "BrowseDirectChildren")
    assert (returned, total) == (2, 2)
    # Parsing as XML proves the DIDL-Lite is well-formed.
    root = ET.fromstring(didl)
    items = root.findall("{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item")
    assert len(items) == 2


def test_browse_single_station_by_object_id(dlna, store):
    store.save_station("s1", "Solo", "https://x/solo")
    didl, returned, total = dlna.browse_response("station/s1", "BrowseDirectChildren")
    assert (returned, total) == (1, 1)
    assert "Solo" in didl


def test_browse_unknown_object_id_is_empty(dlna, store):
    store.save_station("s1", "Solo", "https://x/solo")
    didl, returned, total = dlna.browse_response("station/missing", "BrowseDirectChildren")
    assert (returned, total) == (0, 0)
    root = ET.fromstring(didl)
    assert list(root) == []


def test_device_xml_is_wellformed_and_has_udn(dlna):
    root = ET.fromstring(dlna.device_xml())
    # UDN should match the configured uuid.
    assert "uuid:test-uuid" in dlna.device_xml().decode()
    assert root.tag.endswith("root")


def test_cd_scpd_is_wellformed(dlna):
    root = ET.fromstring(dlna.cd_scpd_xml())
    assert root.tag.endswith("scpd")
