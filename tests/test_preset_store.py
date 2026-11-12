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


# ── backups keyed by deviceID (survive a DHCP address change) ────────────────
def _spk(host, device_id="AABBCCDDEEFF", name="Kitchen"):
    import types
    return types.SimpleNamespace(host=host, device_id=device_id, name=name)


def test_backup_follows_the_speaker_to_a_new_address(tmp_path):
    store = stc.PresetStore(tmp_path / "presets", tmp_path / "stations")
    store.backup_presets(_spk("10.0.0.5"), [{"id": "1", "name": "KISS"}])
    assert (tmp_path / "presets" / "AABBCCDDEEFF.json").exists()
    moved = store.load_backup(_spk("10.0.0.99"))
    assert moved["presets"] == [{"id": "1", "name": "KISS"}]
    assert moved["device_id"] == "AABBCCDDEEFF" and moved["name"] == "Kitchen"
    assert store.load_backup("AABBCCDDEEFF") == moved          # offline: by ID


def test_ip_named_backup_is_adopted_once(tmp_path):
    store = stc.PresetStore(tmp_path / "presets", tmp_path / "stations")
    store.backup_presets("10.0.0.5", [{"id": "1", "name": "KISS"}])   # old naming
    got = store.load_backup(_spk("10.0.0.5"))
    assert got["presets"] == [{"id": "1", "name": "KISS"}] and got["device_id"] == "AABBCCDDEEFF"
    assert not (tmp_path / "presets" / "10_0_0_5.json").exists()
    assert (tmp_path / "presets" / "AABBCCDDEEFF.json").exists()


def test_another_speakers_old_ip_backup_is_not_adopted(tmp_path):
    store = stc.PresetStore(tmp_path / "presets", tmp_path / "stations")
    store.backup_presets("10.0.0.5", [{"id": "1", "name": "Theirs"}])
    store.backup_presets(_spk("10.0.0.7"), [{"id": "1", "name": "Mine"}])
    # the speaker later lands on 10.0.0.5: its own backup wins, the old file stays
    assert store.load_backup(_spk("10.0.0.5"))["presets"][0]["name"] == "Mine"
    assert (tmp_path / "presets" / "10_0_0_5.json").exists()


def test_speaker_without_device_id_still_keys_by_host(tmp_path):
    store = stc.PresetStore(tmp_path / "presets", tmp_path / "stations")
    store.backup_presets(_spk("10.0.0.5", device_id=""), [])
    assert (tmp_path / "presets" / "10_0_0_5.json").exists()


# ── scenes find their speakers by deviceID ───────────────────────────────────
def test_scene_follows_speakers_to_new_addresses():
    before = [_spk("10.0.0.5", "AAA"), _spk("10.0.0.6", "BBB")]
    scene = {"name": "Party", "master": "10.0.0.5", "slaves": ["10.0.0.6"],
             "volumes": {"10.0.0.5": 30, "10.0.0.6": 20}, "preset": 2}
    scene["device_ids"] = stc.scene_device_ids(scene, before)
    assert scene["device_ids"] == {"10.0.0.5": "AAA", "10.0.0.6": "BBB"}

    after = [_spk("10.0.0.50", "AAA"), _spk("10.0.0.6", "BBB")]
    r = stc.resolve_scene_hosts(scene, after)
    assert r["master"] == "10.0.0.50" and r["slaves"] == ["10.0.0.6"]
    assert r["volumes"] == {"10.0.0.50": 30, "10.0.0.6": 20}
    assert r["preset"] == 2


def test_scene_without_device_ids_is_left_alone():
    scene = {"master": "10.0.0.5", "slaves": ["10.0.0.6"], "volumes": {"10.0.0.5": 30}}
    r = stc.resolve_scene_hosts(scene, [_spk("10.0.0.50", "AAA")])
    assert (r["master"], r["slaves"], r["volumes"]) == ("10.0.0.5", ["10.0.0.6"], {"10.0.0.5": 30})
