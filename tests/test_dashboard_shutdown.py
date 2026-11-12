"""App-exit teardown: the desktop chip's /shutdown route kills the player.

Closing the Desktop window must kill the mpv child process. mpv's
kill-on-close job is owned by the gateway process, which outlives the window,
so the renderer fires ``POST /shutdown`` on exit; the route must tear the
player down completely (not just silence it) and invalidate the state cache.
"""
from __future__ import annotations

import pytest


def _api():
    from pandora.dashboard import plugin_api

    return plugin_api


class _FakePlayer:
    output = "local"

    def __init__(self):
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


def test_shutdown_route_tears_down_the_player(monkeypatch):
    api = _api()
    fake = _FakePlayer()
    monkeypatch.setattr(api, "_player", lambda: fake)

    assert api.post_shutdown() == {"ok": True}
    assert fake.shutdowns == 1


def test_shutdown_route_invalidates_the_state_cache(monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "_player", lambda: _FakePlayer())
    api._state_cache.update(at=9.9e9, value={"ok": True, "track": "stale"})

    api.post_shutdown()

    assert api._state_cache["value"] is None


def test_shutdown_route_propagates_the_missing_backend_503(monkeypatch):
    api = _api()
    from fastapi import HTTPException

    def _no_player():
        raise HTTPException(status_code=503, detail="plugin not loaded")

    monkeypatch.setattr(api, "_player", _no_player)

    with pytest.raises(HTTPException) as excinfo:
        api.post_shutdown()
    assert excinfo.value.status_code == 503


def test_shutdown_failure_is_reported_not_raised(monkeypatch):
    api = _api()

    class _Broken(_FakePlayer):
        def shutdown(self):
            raise RuntimeError("job already closed")

    monkeypatch.setattr(api, "_player", lambda: _Broken())

    result = api.post_shutdown()

    assert result == {"ok": False, "error": "job already closed"}
