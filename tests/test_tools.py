"""Tests for pandora_tools: dispatch, error envelopes, search/feedback wiring."""

from __future__ import annotations

import json

import pytest

from pandora import pandora_tools


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(pandora_tools.pandora_auth, "auth_status", lambda: {"logged_in": True})


def _payload(result: str) -> dict:
    return json.loads(result)


class FakeClient:
    def __init__(self, *a, **kw):
        pass
        self.calls = []

    def search(self, query, *, types=None, limit=10):
        return {"search": {"items": [
            {"__typename": "StationFactory", "id": "SF:1:2", "name": "Adele Radio"},
            {"__typename": "Track", "id": "TR:9", "name": "Hello"},
        ]}}

    def collection(self, *, types=None, limit=50, offset=0):
        items = []
        types = [str(t).upper() for t in (types or ["ST"])]
        if "ST" in types:
            items.append({"__typename": "Station", "id": "ST:0:1", "name": "Today's Hits"})
        if "PL" in types:
            items.append({"__typename": "Playlist", "id": "PL:5", "name": "Road Trip", "isPrivate": True})
        return {"collection": {"items": items}}

    def recent_favorites(self, *, limit=10):
        return {"recentFavorites": {"items": [{"__typename": "Station", "id": "ST:0:2", "name": "Chill"}]}}

    def listener_template(self):
        return {"listenerTemplate": {"collectionTypes": ["PC", "PE", "ST"],
                                     "premiumStatus": "NONE"}}

    def create_station_from_seeds(self, seed_ids, *, station_name=None,
                                  view_mode=None, page_view=None):
        return {"createStationFromSeeds": {"status": "OK",
                "station": {"id": "ST:0:9", "name": station_name or "Custom", "description": None}}}

    def add_station_seed(self, station_id, seed_id):
        return {"addStationSeed": {"status": "OK"}}

    def remove_station_seed(self, station_id, seed_id):
        return {"removeStationSeed": {"status": "OK"}}

    def set_station_details(self, station_id, *, name, description="",
                            enable_artist_messages=None):
        return {"setStationDetails": {"status": "OK", "station": {"id": station_id, "name": name}}}

    def create_playlist(self, name, *, description=None, item_ids=None, is_private=None):
        from pandora.pandora_client import PandoraAPIError
        raise PandoraAPIError("An error occurred.", code="X")

    def add_to_playlist(self, playlist_id, item_ids, *, index=None):
        from pandora.pandora_client import PandoraAPIError
        raise PandoraAPIError("An error occurred.", code="X")

    def update_playlist(self, playlist_id, *, name=None, is_private=None, item_ids=None):
        return {"updatePlaylist": {"status": "OK",
                "playlist": {"id": playlist_id, "name": name or "Road Trip",
                             "isPrivate": True, "totalTracks": len(item_ids or [])}}}

    def collect(self, ids):
        return {"collect": {"status": "OK", "message": None}}

    def uncollect(self, ids):
        return {"uncollect": {"status": "OK", "message": None}}


def test_unknown_action_is_an_error():
    result = _payload(pandora_tools.handle_pandora_playback({"action": "dance"}))
    assert "error" in result
    assert "Unknown pandora_playback action" in result["error"]


def test_search_compacts_results(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools._handle_pandora_search({"query": "adele"}))
    assert result["count"] == 2
    assert result["results"][0] == {"id": "SF:1:2", "type": "StationFactory",
                                    "name": "Adele Radio", "kind": "station"}


def test_search_requires_query():
    result = _payload(pandora_tools._handle_pandora_search({}))
    assert "error" in result


def test_stations_list(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_stations({"action": "list"}))
    assert result["stations"] == [{"id": "ST:0:1", "name": "Today's Hits"}]


def test_station_play_by_name(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    played = {}

    class FakePlayer:
        def play_source(self, source_id):
            played["id"] = source_id
            return {"playing": True, "source": source_id}

    monkeypatch.setattr(pandora_tools, "player", FakePlayer())
    result = _payload(pandora_tools.handle_pandora_stations({"action": "play", "station": "today"}))
    assert played["id"] == "ST:0:1"
    assert result["playing"] is True


def test_feedback_without_playback_is_explained(monkeypatch):
    class IdlePlayer:
        item = {}

        def state(self):
            return {"source": "", "position_seconds": 0}

    monkeypatch.setattr(pandora_tools, "player", IdlePlayer())
    result = _payload(pandora_tools.handle_pandora_feedback({"action": "love"}))
    assert "error" in result
    assert "Nothing is playing" in result["error"]


def test_feedback_ban_uses_active_output(monkeypatch):
    """Regression: ban advanced playback through _ensure_mpv, forcing local mpv even on Cast."""
    calls = []

    class _NullLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakePlayer:
        item = {"__typename": "TrackItem", "track": {"id": "TR:1"}, "trackToken": "tok"}
        source_type = "ST"
        index = 0
        _lock = _NullLock()

        def state(self):
            return {"source": "ST:0:1", "position_seconds": 12, "playing": True}

        def _set_item(self, *a, **kw):
            calls.append("set_item")

        def _ensure_output(self):
            calls.append("ensure_output")

        def _ensure_mpv(self):
            calls.append("ensure_mpv")

        def _play_item_audio(self):
            calls.append("play_audio")

    class Client(FakeClient):
        def set_feedback(self, **kw):
            return {"feedback": {"setFeedback": {"status": "OK", "current": {
                "index": 1, "__typename": "TrackItem", "trackToken": "tok2",
                "audioUrl": "https://cdn.example/2.mp3"}}}}

    monkeypatch.setattr(pandora_tools, "PandoraClient", Client)
    monkeypatch.setattr(pandora_tools, "player", FakePlayer())
    result = _payload(pandora_tools.handle_pandora_feedback({"action": "ban"}))

    assert result["success"] is True
    assert calls == ["set_item", "ensure_output", "play_audio"]
    assert "ensure_mpv" not in calls


def test_missing_auth_returns_login_instruction(monkeypatch):
    def boom(*a, **kw):
        raise pandora_tools.pandora_auth.PandoraAuthRequiredError(
            "Pandora is not signed in.", code="x")

    monkeypatch.setattr(pandora_tools, "PandoraClient", boom)
    result = _payload(pandora_tools._handle_pandora_search({"query": "x"}))
    assert "hermes pandora login" in result["error"]


def test_schemas_are_well_formed():
    names = {schema["name"] for schema, _ in pandora_tools.TOOL_DEFS}
    assert {"pandora_playback", "pandora_search", "pandora_stations",
            "pandora_feedback", "pandora_collection", "pandora_playlists",
            "pandora_devices"} <= names
    assert all(n.startswith("pandora_") for n in names)
    for schema, handler in pandora_tools.TOOL_DEFS:
        assert schema["parameters"]["type"] == "object"
        assert callable(handler)
        assert schema["description"]


def test_station_create_from_seeds(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_stations(
        {"action": "create", "seed_id": "AR:1", "name": "My Mix"}))
    assert result["station_id"] == "ST:0:9"
    assert result["status"] == "OK"


def test_station_seed_requires_both(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    assert "error" in _payload(pandora_tools.handle_pandora_stations(
        {"action": "add_seed", "station": "today"}))
    assert "error" in _payload(pandora_tools.handle_pandora_stations(
        {"action": "remove_seed", "seed_id": "AR:1"}))


def test_station_add_seed_by_name(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_stations(
        {"action": "add_seed", "station": "today", "seed_id": "AR:1"}))
    assert result["status"] == "OK"
    assert result["station_id"] == "ST:0:1"
    result = _payload(pandora_tools.handle_pandora_stations(
        {"action": "remove_seed", "station": "ST:0:1", "seed_id": "AR:1"}))
    assert result["status"] == "OK"


def test_station_rename(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_stations(
        {"action": "rename", "station": "today", "new_name": "Better Name"}))
    assert result["status"] == "OK"
    assert result["name"] == "Better Name"


def test_collection_favorite_by_id_and_name(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools._handle_pandora_collection(
        {"action": "favorite", "id": "SF:1:2"}))
    assert result["status"] == "OK"
    assert result["id"] == "SF:1:2"
    result = _payload(pandora_tools._handle_pandora_collection(
        {"action": "unfavorite", "name": "adele"}))
    assert result["status"] == "OK"
    assert result["id"] == "SF:1:2"  # first search hit


def test_collection_favorite_requires_target(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    assert "error" in _payload(pandora_tools._handle_pandora_collection({"action": "favorite"}))


def test_playlist_create_blocked_on_free_tier(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_playlists({"action": "create", "name": "x"}))
    assert "error" in result
    assert "Premium" in result["error"]


def test_playlist_add_blocked_on_free_tier(monkeypatch):
    monkeypatch.setattr(pandora_tools, "PandoraClient", FakeClient)
    result = _payload(pandora_tools.handle_pandora_playlists(
        {"action": "add", "playlist": "PL:5", "item_ids": ["TR:1"]}))
    assert "error" in result
    assert "Premium" in result["error"]


def test_playlist_rename_works_on_premium(monkeypatch):
    class PremiumClient(FakeClient):
        def listener_template(self):
            return {"listenerTemplate": {"collectionTypes": ["PC", "PE", "PL", "ST"],
                                         "premiumStatus": "PREMIUM"}}

        def add_to_playlist(self, playlist_id, item_ids, *, index=None):
            return {"addToPlaylist": {"status": "OK",
                    "playlist": {"id": playlist_id, "name": "Road Trip", "totalTracks": len(item_ids)}}}

    monkeypatch.setattr(pandora_tools, "PandoraClient", PremiumClient)
    result = _payload(pandora_tools.handle_pandora_playlists(
        {"action": "rename", "playlist": "road trip", "new_name": "Beach Trip"}))
    assert result["status"] == "OK"
    assert result["name"] == "Beach Trip"
    result = _payload(pandora_tools.handle_pandora_playlists(
        {"action": "add", "playlist_id": "PL:5", "item_ids": ["TR:1", "TR:2"]}))
    assert result["status"] == "OK"
    assert result["tracks"] == 2
    result = _payload(pandora_tools.handle_pandora_playlists(
        {"action": "set_items", "playlist_id": "PL:5", "item_ids": ["TR:9"]}))
    assert result["status"] == "OK"
    assert result["tracks"] == 1


def test_playlist_list_shows_note_when_empty(monkeypatch):
    class NoPlaylists(FakeClient):
        def collection(self, *, types=None, limit=50, offset=0):
            return {"collection": {"items": []}}

    monkeypatch.setattr(pandora_tools, "PandoraClient", NoPlaylists)
    result = _payload(pandora_tools.handle_pandora_playlists({"action": "list"}))
    assert result["count"] == 0
    assert "Premium feature" in result["note"]
