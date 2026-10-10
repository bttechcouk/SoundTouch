"""Spotify backend: account store, browse shaping, login push and Web API play.
Spotify and the speakers are faked — no network."""
import json
import os

import pytest

import soundtouch_controller as stc


# ── helpers ──────────────────────────────────────────────────────────────────
def test_location_round_trip():
    uri = "spotify:playlist:0NF0MrykiPRhGTqx4sQskL"
    loc = stc.spotify_location(uri)
    assert loc == "/playback/container/c3BvdGlmeTpwbGF5bGlzdDowTkYwTXJ5a2lQUmhHVHF4NHNRc2tM"
    assert stc.spotify_uri_from_location(loc) == uri
    assert stc.spotify_uri_from_location("http://h/dlna/stream/x") is None
    assert stc.spotify_uri_from_location("/playback/container/!!notbase64") is None


def test_cards():
    pl = stc._sp_card({"type": "playlist", "uri": "spotify:playlist:a", "name": "Mix",
                       "owner": {"id": "spotify", "display_name": "Spotify"}, "items": {"total": 50},
                       "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300}]})
    assert pl == {"type": "playlist", "uri": "spotify:playlist:a", "name": "Mix",
                  "sub": "Spotify · 50 songs", "image": "mid", "readonly": True}
    tr = stc._sp_card({"type": "track", "uri": "spotify:track:t", "name": "505",
                       "artists": [{"name": "Arctic Monkeys"}],
                       "album": {"uri": "spotify:album:f", "name": "FWN", "images": [{"url": "x"}]}})
    assert tr["sub"] == "Arctic Monkeys · FWN" and tr["album_uri"] == "spotify:album:f"
    assert stc._sp_card(None) is None


# ── store ────────────────────────────────────────────────────────────────────
def test_store_files_are_private(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    st = stc.SpotifyAccountStore(tmp_path / "spotify")
    st.save({"user_id": "turnerben37", "display_name": "Ben", "product": "premium",
             "refresh_token": "r", "access_token": "a", "expires_at": 0})
    f = tmp_path / "spotify" / "turnerben37.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "spotify").stat().st_mode & 0o777) == "0o700"
    assert st.accounts() == [{"user_id": "turnerben37", "display_name": "Ben", "product": "premium", "image": ""}]
    assert "refresh_token" not in json.dumps(st.accounts())
    (tmp_path / "spotify" / "app.json").write_text('{"client_id": "abc"}')
    assert st.client_id() == "abc" and len(st.accounts()) == 1
    with pytest.raises(stc.SpotifyError):
        st.get("../etc/passwd")


# ── fakes ────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, code=200, body=None, headers=None):
        self.status_code, self._body = code, body if body is not None else {}
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()
        self.ok = code < 400
    def json(self): return self._body


class _Http:
    """Routes requests by (method, url-substring) to canned responses and records calls."""
    def __init__(self, routes): self.routes, self.calls = routes, []
    def _hit(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp(kw) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")
    def get(self, url, **kw): return self._hit("GET", url, **kw)
    def post(self, url, **kw): return self._hit("POST", url, **kw)
    def request(self, method, url, **kw): return self._hit(method, url, **kw)


class _Dev:
    def __init__(self, name="Conservatory", host="10.0.0.72"):
        self.name, self.host, self.zoned = name, host, None
    def set_zone(self, slaves): self.zoned = [s.name for s in slaves]
    def invalidate_zone_cache(self): pass


def _client(tmp_path, routes, monkeypatch):
    monkeypatch.setattr(stc.time, "sleep", lambda s: None)
    st = stc.SpotifyAccountStore(tmp_path / "spotify")
    st.save({"user_id": "turnerben37", "display_name": "Ben", "product": "premium",
             "refresh_token": "r", "access_token": "tok", "expires_at": 9e12})
    c = stc.SpotifyClient(st)
    c._http = _Http(routes)
    return c


# ── login / link ─────────────────────────────────────────────────────────────
def test_link_rejects_unknown_state(tmp_path, monkeypatch):
    c = _client(tmp_path, {}, monkeypatch)
    with pytest.raises(stc.SpotifyError, match="start the login again"):
        c.link("http://127.0.0.1:8888/api/spotify/callback?code=x&state=forged")


def test_login_and_link(tmp_path, monkeypatch):
    c = _client(tmp_path, {
        ("POST", "/api/token"): _Resp(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600, "scope": "s"}),
        ("GET", "/v1/me"): _Resp(200, {"id": "duo2", "display_name": "Duo 2", "product": "premium"})}, monkeypatch)
    (tmp_path / "spotify" / "app.json").write_text('{"client_id": "cid"}')
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    url = c.login_url()
    state = stc.parse_qs(stc.urlparse(url).query)["state"][0]
    acct = c.link(f"http://127.0.0.1:8888/api/spotify/callback?code=C&state={state}")
    assert acct["user_id"] == "duo2"
    token_call = next(kw for m, u, kw in c._http.calls if "/api/token" in u)
    assert token_call["data"]["code_verifier"] and "client_secret" not in token_call["data"]
    assert {a["user_id"] for a in c.store.accounts()} == {"turnerben37", "duo2"}


# ── play ─────────────────────────────────────────────────────────────────────
def _play_routes(active_user="", devices_seq=None, play_codes=None):
    devices_seq = list(devices_seq or [[{"name": "Conservatory", "id": "DEV1", "is_active": True}]])
    play_codes = list(play_codes or [204])
    state = {"adds": 0}
    def devices(kw):
        return _Resp(200, {"devices": devices_seq.pop(0) if len(devices_seq) > 1 else devices_seq[0]})
    def add_user(kw):
        state["adds"] += 1
        assert kw["data"]["clientKey"] == "BOSECLIENT" and kw["data"]["blob"] == "tok"
        return _Resp(200, {"status": 101})
    def play(kw):
        code = play_codes.pop(0) if play_codes else 204
        return _Resp(code, {} if code < 400 else {"error": {"status": code}})
    return {("GET", ":8200/zc"): _Resp(200, {"activeUser": active_user, "clientID": "BOSECLIENT", "tokenType": "accesstoken"}),
            ("POST", ":8200/zc"): add_user,
            ("GET", "/me/player/devices"): devices,
            ("PUT", "/me/player/play"): play}, state


def test_play_logs_in_then_plays_with_offset(tmp_path, monkeypatch):
    routes, state = _play_routes(active_user="")
    c = _client(tmp_path, routes, monkeypatch)
    assert c.play("spotify:playlist:p", [_Dev()], offset="4") == ["Conservatory"]
    assert state["adds"] == 1
    put = next(kw for m, u, kw in c._http.calls if m == "PUT")
    assert put["params"] == {"device_id": "DEV1"}
    assert put["json"] == {"context_uri": "spotify:playlist:p", "offset": {"position": 4}}


def test_already_logged_in_recently_skips_login(tmp_path, monkeypatch):
    routes, state = _play_routes(active_user="turnerben37")
    c = _client(tmp_path, routes, monkeypatch)
    dev = _Dev()
    c._pushed[(dev.host, "turnerben37")] = stc.time.monotonic()
    c.play("spotify:album:a", [dev])
    assert state["adds"] == 0


def test_stale_login_device_missing_repushes(tmp_path, monkeypatch):
    # first device lookups don't show the speaker; after a forced re-login it appears
    routes, state = _play_routes(active_user="turnerben37",
                                 devices_seq=[[]] * 8 + [[{"name": "Conservatory", "id": "DEV9", "is_active": True}]])
    c = _client(tmp_path, routes, monkeypatch)
    c.play("spotify:album:a", [_Dev()])
    assert state["adds"] >= 1
    assert next(kw for m, u, kw in c._http.calls if m == "PUT")["params"]["device_id"] == "DEV9"


def test_group_play_zones_then_plays_on_master(tmp_path, monkeypatch):
    routes, _ = _play_routes()
    c = _client(tmp_path, routes, monkeypatch)
    master, slave = _Dev(), _Dev("Kitchen", "10.0.0.58")
    c.play("spotify:playlist:p", [master, slave])
    assert master.zoned == ["Kitchen"]


def test_play_bodies(tmp_path, monkeypatch):
    c = _client(tmp_path, {("GET", "/me/tracks"): _Resp(200, {"items": [
        {"track": {"uri": "spotify:track:1"}}, {"item": {"uri": "spotify:track:2"}}]})}, monkeypatch)
    assert c._play_body("turnerben37", "spotify:user:turnerben37:collection") == {"uris": ["spotify:track:1", "spotify:track:2"]}
    assert c._play_body("turnerben37", "spotify:track:9") == {"uris": ["spotify:track:9"]}
    assert c._play_body("turnerben37", "spotify:album:a", "spotify:track:3") == {
        "context_uri": "spotify:album:a", "offset": {"uri": "spotify:track:3"}}


def test_keep_logged_in_only_touches_empty_speakers(tmp_path, monkeypatch):
    class Dev(_Dev):
        def __init__(self, name, host, user):
            super().__init__(name, host); self.user = user
        def get_sources(self):
            return [{"source": "SPOTIFY", "sourceAccount": "turnerben37", "status": "READY"}]
    devs = [Dev("Hallie", "10.0.0.64", ""), Dev("Kitchen", "10.0.0.58", "duo2"),
            Dev("Bedroom", "10.0.0.68", "turnerben37")]
    adds = []
    routes = {("GET", ":8200/zc"): lambda kw: _Resp(200, {"activeUser": next(
                  d.user for d in devs if d.host in c._http.calls[-1][1]), "clientID": "BOSECLIENT"}),
              ("POST", ":8200/zc"): lambda kw: adds.append(c._http.calls[-1][1]) or _Resp(200, {"status": 101})}
    c = _client(tmp_path, routes, monkeypatch)
    c._get_devices = lambda: devs
    c._pushed[("10.0.0.68", "turnerben37")] = stc.time.monotonic()   # Bedroom pushed just now
    c.keep_speakers_logged_in()
    # Hallie (no login) only: Kitchen is the other Duo account, Bedroom is fresh
    assert len(adds) == 1 and "10.0.0.64" in adds[0]
    # Bedroom's push ages out → refreshed on a later pass
    c._pushed[("10.0.0.68", "turnerben37")] = -1e12
    adds.clear(); c.keep_speakers_logged_in()
    assert any("10.0.0.68" in a for a in adds) and not any("10.0.0.58" in a for a in adds)


# ── presets use the Spotify route ────────────────────────────────────────────
def test_spotify_preset_goes_through_web_api(monkeypatch):
    calls = []
    fake = type("S", (), {"store": type("St", (), {"get": staticmethod(lambda u: {"user_id": u})})(),
                          "play": lambda self, uri, devs, acct: calls.append((uri, acct))})()
    monkeypatch.setattr(stc, "SPOTIFY", fake)
    dev = stc.SoundTouchDevice("10.0.0.72")
    dev.get_presets_detail = lambda: [{"id": "5", "source": "SPOTIFY", "account": "turnerben37",
                                       "location": stc.spotify_location("spotify:playlist:p")}]
    dev._key = lambda k: calls.append(("key", k))
    assert dev.play_preset(5)
    assert calls == [("spotify:playlist:p", "turnerben37")]


class _NpDev(_Dev):
    """A fake speaker with a now_playing sequence and select_source()."""
    def __init__(self, states):
        super().__init__(); self.states, self.selected = list(states), []
    def _get(self, path, timeout=4):
        import xml.etree.ElementTree as ET
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        src, ps, ctx = (st + (None,))[:3]
        loc = f' location="{stc.spotify_location(ctx)}"' if ctx else ""
        return ET.fromstring(f'<nowPlaying source="{src}"><ContentItem source="{src}"{loc}/>'
                             f'<playStatus>{ps}</playStatus></nowPlaying>')
    def select_source(self, source, account=""):
        self.selected.append((source, account))


def test_wakes_standby_speaker_on_spotify_input(tmp_path, monkeypatch):
    routes, _ = _play_routes()
    c = _client(tmp_path, routes, monkeypatch)
    dev = _NpDev([("STANDBY", ""), ("SPOTIFY", "BUFFERING_STATE"), ("SPOTIFY", "PLAY_STATE", "spotify:album:a")])
    c.play("spotify:album:a", [dev])
    assert dev.selected == [("SPOTIFY", "turnerben37")]


def test_accepted_but_not_started_retries_play(tmp_path, monkeypatch):
    routes, _ = _play_routes()
    c = _client(tmp_path, routes, monkeypatch)
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(stc.time, "monotonic", lambda: next(ticks) * 3.0)   # each poll ~3 s
    dev = _NpDev([("SPOTIFY", "STOP_STATE")] * 6 + [("SPOTIFY", "PLAY_STATE", "spotify:playlist:p")])
    c._pushed[(dev.host, "turnerben37")] = -1e12   # force a login push, harmless here
    c.play("spotify:playlist:p", [dev])
    assert sum(1 for m, u, kw in c._http.calls if m == "PUT") >= 2   # retried until it started


def test_resumed_old_context_is_not_success(tmp_path, monkeypatch):
    # Waking resumes the speaker's last context (Liked Songs here); only the
    # requested album counts, so the play is re-sent until it shows up.
    routes, _ = _play_routes()
    c = _client(tmp_path, routes, monkeypatch)
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(stc.time, "monotonic", lambda: next(ticks) * 3.0)
    old = ("SPOTIFY", "PLAY_STATE", "spotify:playlist:old")
    dev = _NpDev([old] * 3 + [("SPOTIFY", "PLAY_STATE", "spotify:album:a")])
    c.play("spotify:album:a", [dev])
    assert sum(1 for m, u, kw in c._http.calls if m == "PUT") >= 2
