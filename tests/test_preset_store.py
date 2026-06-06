"""PresetStore: custom-station CRUD, station_descriptor JSON, preset backups."""
import json

import soundtouch_controller as stc


def test_save_get_roundtrip(store):
    store.save_station("s1", "Jazz FM", "https://stream.example/jazz", "https://art/x.png")
    st = store.get_station("s1")
    assert st == {
        "id": "s1",
        "name": "Jazz FM",
        "stream_url": "https://stream.example/jazz",
        "art_url": "https://art/x.png",
    }


def test_get_missing_returns_none(store):
    assert store.get_station("nope") is None


def test_art_url_defaults_empty(store):
    store.save_station("s2", "No Art", "https://stream.example/x")
    assert store.get_station("s2")["art_url"] == ""


def test_list_stations_sorted_by_filename(store):
    store.save_station("b", "B", "https://x/b")
    store.save_station("a", "A", "https://x/a")
    names = [s["name"] for s in store.list_stations()]
    assert names == ["A", "B"]


def test_delete_station(store):
    store.save_station("s3", "Gone", "https://x/g")
    assert store.delete_station("s3") is True
    assert store.get_station("s3") is None
    # deleting again is a no-op, returns False
    assert store.delete_station("s3") is False


def test_station_descriptor_shape(store):
    store.save_station("s4", "Talk Radio", "https://x/talk", "https://art/t.png")
    desc = json.loads(store.station_descriptor("s4"))
    assert desc == {
        "name": "Talk Radio",
        "imageUrl": "https://art/t.png",
        "streamType": "liveRadio",
        "audio": {
            "streamUrl": "https://x/talk",
            "hasPlaylist": False,
            "isRealtime": True,
        },
    }


def test_station_descriptor_missing_returns_none(store):
    assert store.station_descriptor("missing") is None


def test_backup_and_load_presets(store):
    presets = [{"id": "1", "name": "R1", "source": "LOCAL_INTERNET_RADIO"}]
    saved = store.backup_presets("192.168.1.50", presets)
    assert saved["host"] == "192.168.1.50"
    assert saved["presets"] == presets
    assert "backed_up" in saved

    loaded = store.load_backup("192.168.1.50")
    assert loaded["presets"] == presets


def test_load_backup_missing_returns_none(store):
    assert store.load_backup("10.0.0.1") is None


def test_backup_file_uses_underscored_host(store):
    store.backup_presets("192.168.1.99", [])
    assert (store.presets_dir / "192_168_1_99.json").exists()
