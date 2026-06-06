"""plan_preset_restore: LOCAL_INTERNET_RADIO -> UPNP conversion on restore."""
import soundtouch_controller as stc


def test_malformed_preset_returns_none(store, dlna):
    assert stc.plan_preset_restore({}, True, store, dlna) is None
    assert stc.plan_preset_restore({"id": "1"}, True, store, dlna) is None          # no source
    assert stc.plan_preset_restore({"source": "AUX"}, True, store, dlna) is None    # no id


def test_normal_speaker_keeps_local_internet_radio(store, dlna):
    p = {"id": "1", "name": "Radio 1", "source": "LOCAL_INTERNET_RADIO",
         "type": "stationurl", "location": "loc-1", "account": "acct"}
    kind, payload = stc.plan_preset_restore(p, has_local_ir=True, store=store, dlna=dlna)
    assert kind == "store"
    assert payload == dict(preset_id="1", name="Radio 1", source="LOCAL_INTERNET_RADIO",
                           stype="stationurl", location="loc-1", account="acct")


def test_non_radio_preset_passes_through_unchanged(store, dlna):
    p = {"id": "2", "name": "Bluetooth", "source": "BLUETOOTH",
         "type": "", "location": "", "account": ""}
    kind, payload = stc.plan_preset_restore(p, has_local_ir=False, store=store, dlna=dlna)
    assert kind == "store"
    assert payload["source"] == "BLUETOOTH"


def test_upnp_speaker_converts_radio_to_upnp_when_station_exists(store, dlna):
    store.save_station("st99", "My Station", "https://x/s")
    p = {"id": "3", "name": "My Station",
         "source": "LOCAL_INTERNET_RADIO",
         "location": "http://old-host:8888/dlna/stream/st99"}
    kind, payload = stc.plan_preset_restore(p, has_local_ir=False, store=store, dlna=dlna)
    assert kind == "store"
    assert payload["source"] == "UPNP"
    assert payload["account"] == "UPnPUserName"
    assert payload["stype"] == ""
    # Rewritten to point at *our* DLNA redirect, derived from the station id.
    assert payload["location"] == "http://192.168.1.10:8888/dlna/stream/st99"


def test_station_id_extracted_from_bare_id_location(store, dlna):
    store.save_station("plain", "Plain", "https://x/p")
    p = {"id": "4", "name": "Plain", "source": "LOCAL_INTERNET_RADIO",
         "location": "plain"}
    kind, payload = stc.plan_preset_restore(p, has_local_ir=False, store=store, dlna=dlna)
    assert payload["location"] == "http://192.168.1.10:8888/dlna/stream/plain"


def test_trailing_slash_location_is_handled(store, dlna):
    store.save_station("ts", "TS", "https://x/ts")
    p = {"id": "5", "name": "TS", "source": "LOCAL_INTERNET_RADIO",
         "location": "http://h:8888/dlna/stream/ts/"}
    kind, payload = stc.plan_preset_restore(p, has_local_ir=False, store=store, dlna=dlna)
    assert payload["location"] == "http://192.168.1.10:8888/dlna/stream/ts"


def test_upnp_speaker_skips_radio_when_station_missing(store, dlna):
    p = {"id": "6", "name": "Ghost", "source": "LOCAL_INTERNET_RADIO",
         "location": "http://h:8888/dlna/stream/ghost"}
    kind, reason = stc.plan_preset_restore(p, has_local_ir=False, store=store, dlna=dlna)
    assert kind == "skip"
    assert "ghost" in reason
