"""Tests for Phase 3: pandora_cast (discovery, CastOutput) and player cast mode."""

from __future__ import annotations

import types

import pytest

from pandora import pandora_cast, pandora_tools
from pandora.pandora_cast import CastError, _normalize

pytestmark = pytest.mark.skipif(not pandora_cast._CAST_AVAILABLE,
                                reason="pychromecast not installed")


def _payload(result: str) -> dict:
    import json
    return json.loads(result)


# ── fakes ───────────────────────────────────────────────────────────────────

class FakeCI:
    def __init__(self, name, host="192.168.0.50", port=8009, uuid="",
                 cast_type="audio", model="Nest Audio"):
        self.friendly_name = name
        self.model_name = model
        self.host = host
        self.port = port
        self.uuid = uuid
        self.cast_type = cast_type
        self.services = set()


class FakeBrowser:
    def __init__(self, devices):
        self._devices = devices
        self.zeroconf = None

    def stop_discovery(self):
        pass


FAKES = [
    FakeCI("Living Room speaker", "192.168.0.173", uuid="fac39cd4", cast_type="audio"),
    FakeCI("Kitchen display", "192.168.0.232", uuid="bafae0b0", cast_type="cast", model="Google Nest Hub"),
    FakeCI("Speakers", "192.168.0.232", 32071, uuid="7f95ae5e", cast_type="group", model="Google Cast Group"),
]


def _fake_discovery(timeout=10.0, known_hosts=None):
    return list(FAKES), FakeBrowser(FAKES)


@pytest.fixture
def fake_discovery(monkeypatch):
    monkeypatch.setattr("pychromecast.discovery.discover_chromecasts", _fake_discovery)


@pytest.fixture
def fake_chromecast(monkeypatch):
    class FakeVolume:
        def __init__(self):
            self._v = 0.5

        def set_volume(self, v):
            self._v = v

        @property
        def volume(self):
            return self._v

    class FakeStatus:
        def __init__(self):
            self.player_state = "IDLE"
            self.current_time = 0.0
            self.play_duration = 0.0
            self.metadata = None
            self.media_session_id = None

    class FakeMedia:
        def __init__(self):
            self.status = FakeStatus()
            self.calls = []

        def play_media(self, url, ctype, **kw):
            self.calls.append((url, ctype, kw))
            self.status.player_state = "PLAYING"
            self.status.current_time = 0.5
            self.status.play_duration = 212.0

        def play(self, timeout=10.0):
            self.status.player_state = "PLAYING"

        def pause(self, timeout=10.0):
            self.status.player_state = "PAUSED"

        def stop(self, timeout=10.0):
            self.status.player_state = "IDLE"

        def seek(self, seconds, timeout=10.0):
            # pychromecast's seek() takes an ABSOLUTE position (it sends
            # `currentTime: position`) — verified in pychromecast 14.1.1.
            self.status.current_time = max(0.0, float(seconds))

        def register_status_listener(self, listener):
            self.listeners = getattr(self, "listeners", []) + [listener]

    class FakeCast:
        def __init__(self, cast_info, zconf=None):
            self.cast_info = cast_info
            self.media_controller = FakeMedia()
            self.volume_controller = FakeVolume()
            self.is_connected = True

        def wait(self, timeout=None):
            return True

        def close(self, block=False):
            self.is_connected = False

    monkeypatch.setattr(pandora_cast, "Chromecast", FakeCast)
    monkeypatch.setattr(pandora_cast, "_content_type_for_url", lambda url: "audio/mpeg")


@pytest.fixture(autouse=True)
def _fresh_cast_output():
    pandora_cast.reset_cast_output()
    yield
    pandora_cast.reset_cast_output()


# ── discovery / matching ────────────────────────────────────────────────────

def test_discover_devices_lists_and_sorts(fake_discovery):
    devices = pandora_cast.discover_devices(timeout=5.0)
    assert [d["name"] for d in devices] == ["Kitchen display", "Living Room speaker", "Speakers"]
    lr = next(d for d in devices if d["name"] == "Living Room speaker")
    assert lr["host"] == "192.168.0.173"
    assert lr["port"] == 8009
    assert lr["type"] == "audio"
    sp = next(d for d in devices if d["name"] == "Speakers")
    assert sp["type"] == "group"
    assert sp["port"] == 32071


def test_pick_device_matching():
    devs = [{"name": "Living Room speaker"}, {"name": "Kitchen display"}, {"name": "Speakers"}]
    assert pandora_cast._pick_device(devs, "Living Room Speaker")["name"] == "Living Room speaker"
    assert pandora_cast._pick_device(devs, "living  room   speaker")["name"] == "Living Room speaker"
    assert pandora_cast._pick_device(devs, "kitchen")["name"] == "Kitchen display"
    with pytest.raises(CastError):
        pandora_cast._pick_device(devs, "Bathroom Speaker")


def test_normalize_is_lenient():
    assert _normalize("  Living  Room  Speaker ") == "livingroomspeaker"
    assert _normalize("Kitchen-display!") == "kitchendisplay"


# ── CastOutput ──────────────────────────────────────────────────────────────

def test_connect_and_media_controls(fake_discovery, fake_chromecast):
    out = pandora_cast.get_cast_output()
    info = out.connect("Living Room Speaker")
    assert info["name"] == "Living Room speaker"
    assert info["type"] == "audio"
    assert out.connected

    res = out.load("https://cdn.example/song.mp3?sig=x",
                   title="Make You Feel My Love", artist="Adele", volume=0.3)
    assert res["content_type"] == "audio/mpeg"
    url, ctype, kw = out._media.calls[0]
    assert url == "https://cdn.example/song.mp3?sig=x"
    assert ctype == "audio/mpeg"
    assert kw["stream_type"] == pandora_cast.STREAM_TYPE_BUFFERED
    assert "Make You Feel My Love" in kw["title"]

    st = out.status()
    assert st["player_state"] == "PLAYING"
    assert st["play_duration"] == 212.0
    assert st["volume"] == 0.3

    out.pause()
    assert out.status()["player_state"] == "PAUSED"
    out.play()
    assert out.status()["player_state"] == "PLAYING"
    out.stop()
    assert out.status()["player_state"] == "IDLE"
    out.close()
    assert not out.connected


def test_connect_unknown_device(fake_discovery, fake_chromecast):
    out = pandora_cast.get_cast_output()
    with pytest.raises(CastError, match="No Cast device matching"):
        out.connect("Bathroom Speaker")


def test_connect_explicit_host(fake_discovery, fake_chromecast):
    """A host:port target dials the fallback CastInfo path (no discovery match)."""
    out = pandora_cast.get_cast_output()
    info = out.connect("127.0.0.1:8009")
    assert info["host"] == "127.0.0.1"
    assert info["port"] == 8009
    assert out.connected


# ── player in cast mode ─────────────────────────────────────────────────────

class FakePlayerClient:
    def __init__(self, *a, **kw):
        self.calls = []
        self.device_uuid = "test-device"

    def playback_set_source(self, source_id):
        self.calls.append(("setSource", source_id))
        return {"playback": {"setSource": {"id": "ST:0:1", "type": "ST", "current": {
            "index": 0, "__typename": "Track", "trackToken": "tok1",
            "audioUrl": "https://cdn.example/1.mp3",
            "track": {"name": "Make You Feel My Love", "artist": {"name": "Adele"},
                       "art": {"url": "https://a/1.jpg"}, "duration": 212}}}}}

    def playback_started(self, sid, idx, elapsed):
        self.calls.append(("started", sid, idx, elapsed))

    def playback_ended(self, sid, idx, elapsed, kind="NORMAL"):
        self.calls.append(("ended", sid, idx, elapsed, kind))
        return {"playback": {"setEnded": {"id": "ST:0:1", "type": "ST", "current": {
            "index": 1, "__typename": "Track", "trackToken": "tok2",
            "audioUrl": "https://cdn.example/2.mp3",
            "track": {"name": "Someone Like You", "artist": {"name": "Adele"},
                       "art": {"url": "https://a/2.jpg"}, "duration": 263}}}}}

    def playback_pause(self, sid, idx, elapsed):
        self.calls.append(("pause", sid, idx, elapsed))

    def playback_progress(self, sid, idx, elapsed):
        self.calls.append(("progress", sid, idx, elapsed))

    def playback_current(self, sid):
        return {"playback": {"current": {}}}

    def playback_replay(self, sid, idx, token, elapsed):
        self.calls.append(("replay", sid, idx, token, elapsed))

    def playback_previous(self, sid, idx, elapsed):
        self.calls.append(("previous", sid, idx, elapsed))
        return {"playback": {"previous": {"id": "ST:0:1", "type": "ST", "current": {
            "index": 0, "__typename": "Track", "trackToken": "tok1",
            "audioUrl": "https://cdn.example/1.mp3",
            "track": {"name": "Make You Feel My Love", "artist": {"name": "Adele"},
                      "art": {"url": "https://a/1.jpg"}, "duration": 212}}}}}


def test_player_cast_play_and_track_end(fake_discovery, fake_chromecast, monkeypatch):
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()

    st = player.set_output("Living Room Speaker")
    assert st["output"] == "Living Room speaker"

    st = player.play_source("SF:17231:21727")
    assert st["playing"] is True
    assert st["track"] == "Make You Feel My Love"
    assert st["output"] == "Living Room speaker"
    media = pandora_cast.get_cast_output()._media
    assert media.calls[0][0].endswith("1.mp3")

    # The device starts playing, then finishes the track -> IDLE push -> player advances.
    player.new_media_status(types.SimpleNamespace(player_state="PLAYING"))
    player.new_media_status(types.SimpleNamespace(player_state="IDLE"))
    assert player.track_name == "Someone Like You"
    assert media.calls[1][0].endswith("2.mp3")
    kinds = [c[0] for c in player._client.calls]
    assert "started" in kinds
    assert "ended" in kinds

    # Remote pause/resume syncs back through the status listener.
    player.new_media_status(types.SimpleNamespace(player_state="PAUSED"))
    assert player.paused is True
    player.new_media_status(types.SimpleNamespace(player_state="PLAYING"))
    assert player.paused is False

    player.stop()
    st = player.state()
    assert st["playing"] is False
    assert st["output"] == "Living Room speaker"


def test_idle_before_start_is_ignored(fake_discovery, fake_chromecast, monkeypatch):
    """IDLE before the device ever started (pre-buffer / failed load) must not
    trigger auto-advance — otherwise the player spins through the station
    while the device stays IDLE (runaway loop seen live on 2026-11-06)."""
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()

    player.set_output("Living Room Speaker")
    st = player.play_source("SF:17231:21727")
    assert st["track"] == "Make You Feel My Love"

    # Device reports IDLE without ever having played (e.g. pre-buffer state).
    player.new_media_status(types.SimpleNamespace(player_state="IDLE"))
    assert player.track_name == "Make You Feel My Love"
    assert [c[0] for c in player._client.calls] == ["setSource", "started"]
    # A second IDLE push must not start a loop either.
    player.new_media_status(types.SimpleNamespace(player_state="IDLE"))
    assert player.track_name == "Make You Feel My Love"

    # Once the device actually plays, IDLE means track end again.
    player.new_media_status(types.SimpleNamespace(player_state="PLAYING"))
    player.new_media_status(types.SimpleNamespace(player_state="IDLE"))
    assert player.track_name == "Someone Like You"

    player.stop()


def test_player_replay_restarts_from_top(fake_discovery, fake_chromecast, monkeypatch):
    """Replay must rewind the active output to 0 (observed live 2026-11-06:
    without the rewind the Cast device just kept playing from the same spot)."""
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()

    player.set_output("Kitchen Display")
    st = player.play_source("SF:17231:21727")
    assert st["track"] == "Make You Feel My Love"
    # Push a mid-track status the way the device would: pychromecast notifies
    # every registered listener (the player AND the cast output itself), and the
    # cast output re-baselines its local position clock from the pushed
    # current_time, so the absolute seek(0) in replay() rewinds fully.
    push = types.SimpleNamespace(player_state="PLAYING",
                                 current_time=45.0, play_duration=212.0)
    player.new_media_status(push)
    pandora_cast.get_cast_output().new_media_status(push)

    st = player.replay()
    assert st["track"] == "Make You Feel My Love"  # same song, from the top
    media = pandora_cast.get_cast_output()._media
    assert media.status.current_time == 0.0
    kinds = [c[0] for c in player._client.calls]
    assert "replay" in kinds

    player.stop()


def test_player_previous_restarts_track_instead_of_asking_pandora(fake_discovery, fake_chromecast, monkeypatch):
    """Pandora answers NOT_SUPPORTED for the previous mutation on station
    sources, so previous() must replay the current track rather than call it."""
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()

    player.set_output("Kitchen Display")
    player.play_source("SF:17231:21727")
    push = types.SimpleNamespace(player_state="PLAYING",
                                 current_time=45.0, play_duration=212.0)
    player.new_media_status(push)
    pandora_cast.get_cast_output().new_media_status(push)

    st = player.previous()
    assert st["track"] == "Make You Feel My Love"
    assert pandora_cast.get_cast_output()._media.status.current_time == 0.0
    kinds = [c[0] for c in player._client.calls]
    assert "replay" in kinds
    assert "previous" not in kinds

    player.stop()


def test_player_seek_uses_absolute_position_on_cast(fake_discovery, fake_chromecast, monkeypatch):
    """seek(n) must land at n seconds — pychromecast takes an absolute position."""
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()
    player.set_output("Kitchen Display")
    player.play_source("SF:17231:21727")

    push = types.SimpleNamespace(player_state="PLAYING",
                                 current_time=45.0, play_duration=212.0)
    player.new_media_status(push)
    pandora_cast.get_cast_output().new_media_status(push)

    player.seek(30.0)
    assert pandora_cast.get_cast_output()._media.status.current_time == 30.0
    player.stop()


def test_player_previous_ensures_output_and_replays(monkeypatch):
    """previous() restarts the current track, re-establishing the output first
    (a dead mpv fd silently eats commands) and never asking Pandora for a
    previous track — the API answers NOT_SUPPORTED on station sources."""
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()
    player._set_item({"id": "x", "trackToken": "tok", "trackName": "T", "artistName": "A"},
                     source_id="ST:0:1", source_type="ST", index=0)

    calls = []
    monkeypatch.setattr(player, "_ensure_output", lambda: calls.append("ensure_output"))
    monkeypatch.setattr(player, "replay", lambda: (calls.append("replay"), player.state())[1])

    player.previous()
    assert calls == ["ensure_output", "replay"]


def test_player_previous_without_a_track_raises(monkeypatch):
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player._client = FakePlayerClient()
    with pytest.raises(pp.PlayerError):
        player.previous()


def test_player_local_untouched(fake_chromecast, monkeypatch):
    import pandora.pandora_player as pp
    player = pp.PandoraPlayer()
    player.output = "local"
    assert not player._in_cast()
    st = player.state()
    assert st["output"] == "local"
    assert st["playing"] is False


# ── tools layer ─────────────────────────────────────────────────────────────

def test_devices_tool_list_and_set(fake_discovery, fake_chromecast, monkeypatch):
    monkeypatch.setattr(
        pandora_tools.pandora_auth, "auth_status", lambda: {"logged_in": True})

    class P:
        output = "local"

        def set_output(self, name):
            self.output = name
            return {"output": name, "playing": False, "paused": False,
                    "track": "", "artist": ""}

        def state(self):
            return {"output": self.output, "playing": False, "paused": False,
                    "track": "", "artist": "", "position_seconds": 0}

    monkeypatch.setattr(pandora_tools, "player", P())
    r = _payload(pandora_tools.handle_pandora_devices({"action": "list"}))
    assert r["success"] is True
    assert [d["name"] for d in r["devices"]] == ["Kitchen display", "Living Room speaker", "Speakers"]
    assert r["output"] == "local"
    assert all(not d["is_output"] for d in r["devices"])

    r = _payload(pandora_tools.handle_pandora_devices({"action": "set", "device": "Speakers"}))
    assert r["success"] is True
    assert r["output"] == "Speakers"

    r = _payload(pandora_tools.handle_pandora_devices({"action": "set"}))
    assert "error" in r


def test_devices_tool_status(fake_discovery, fake_chromecast, monkeypatch):
    class P:
        output = "local"

        def state(self):
            return {"output": self.output, "playing": False, "paused": False,
                    "track": "", "artist": "", "position_seconds": 0}

    monkeypatch.setattr(pandora_tools, "player", P())
    r = _payload(pandora_tools.handle_pandora_devices({"action": "status"}))
    assert r["success"] is True
    assert r["output"] == "local"
    assert "cast" not in r
