"""Tests for mpv IPC hardening: per-process pipe, write deadline, reply cleanup."""

from __future__ import annotations

import os

import pytest

import pandora.pandora_player as pp


def test_ipc_path_is_per_process():
    """A shared pipe name wedges when a killed process leaves a stale handle."""
    assert str(os.getpid()) in pp._ipc_path()


def test_write_deadline_tears_down_a_stalled_player(monkeypatch):
    player = pp.PandoraPlayer()
    player._fd = 4242
    closed = []

    def stuck_write(fd, view):
        raise BlockingIOError()

    monkeypatch.setattr(pp.os, "write", stuck_write)
    monkeypatch.setattr(pp.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(pp, "WRITE_DEADLINE_SECONDS", 0.05)

    with pytest.raises(pp.PlayerUnavailable):
        player._write_bytes(b'{"command": ["status"]}\n')

    assert player._fd is None
    assert closed == [4242]


def test_write_oserror_tears_down_the_player(monkeypatch):
    player = pp.PandoraPlayer()
    player._fd = 77

    def broken_write(fd, view):
        raise OSError(32, "broken pipe")

    monkeypatch.setattr(pp.os, "write", broken_write)
    monkeypatch.setattr(pp.os, "close", lambda fd: None)

    with pytest.raises(pp.PlayerUnavailable):
        player._write_bytes(b"x")

    assert player._fd is None


def test_request_timeout_does_not_leak_reply_state(monkeypatch):
    player = pp.PandoraPlayer()
    player._fd = 99
    monkeypatch.setattr(player, "_write_bytes", lambda payload: None)

    assert player._request(["get_property", "pause"], timeout=0.01) is None
    assert player._replies == {}
    assert player._reply_events == {}


def test_pipe_nonblocking_sets_the_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(pp.os, "set_blocking", lambda fd, flag: seen.append((fd, flag)), raising=False)
    pp._set_pipe_nonblocking(9)
    assert seen == [(9, False)]


def test_pipe_refuses_builds_without_set_blocking(monkeypatch):
    """A blocking pipe wedges the first read — fail loudly instead of hanging."""
    # Simulates Python builds that predate Windows support for os.set_blocking.
    monkeypatch.setattr(pp.os, "set_blocking", None, raising=False)
    with pytest.raises(pp.PlayerUnavailable, match="set_blocking"):
        pp._set_pipe_nonblocking(9)


def test_track_end_without_a_source_is_ignored(monkeypatch):
    """mpv emits end-file(reason=stop) on stop(); there is nothing to report."""
    called = []

    class Client:
        def playback_ended(self, *a, **k):
            called.append(a)
            return {}

    player = pp.PandoraPlayer()
    player._client = Client()
    player.source_id = ""
    player._handle_track_end()
    assert called == []
    assert player.last_error == ""


def test_write_bytes_is_a_noop_without_a_player():
    player = pp.PandoraPlayer()
    player._write_bytes(b"x")  # _fd is None: must not raise
    assert player._fd is None
