"""DPAPI credential storage (Windows) and the environment fallback."""
import sys

import pytest

WINDOWS = sys.platform == "win32"


@pytest.mark.skipif(not WINDOWS, reason="DPAPI is Windows-only")
def test_protect_roundtrip():
    from pandora import pandora_secretstore as ss

    assert ss.available() is True
    blob = ss.protect(b"hello pandora")
    assert blob != b"hello pandora"
    assert ss.unprotect(blob) == b"hello pandora"


@pytest.mark.skipif(not WINDOWS, reason="DPAPI is Windows-only")
def test_save_load_and_clear(monkeypatch, tmp_path):
    from pandora import pandora_secretstore as ss

    target = tmp_path / "credentials.bin"
    monkeypatch.setattr(ss, "path", lambda: target)

    assert ss.save("me@example.com", "hunter2") is True
    assert target.is_file()
    assert b"hunter2" not in target.read_bytes()      # encrypted at rest

    assert ss.load() == {"email": "me@example.com", "password": "hunter2"}
    assert ss.clear() is True
    assert ss.load() is None


def test_secret_prefers_the_stored_password(monkeypatch):
    from pandora import pandora_auth

    monkeypatch.setattr(pandora_auth, "_stored_credentials",
                        lambda: {"email": "stored@example.com", "password": "stored-pass"})
    monkeypatch.setattr(pandora_auth, "_default_env_value", lambda name: "env-pass")

    assert pandora_auth._secret("PANDORA_PASSWORD") == "stored-pass"


def test_email_falls_back_to_the_store(monkeypatch):
    import agent.secret_scope as scope

    from pandora import pandora_auth

    monkeypatch.setattr(scope, "get_secret", lambda name, default="": "")
    monkeypatch.setattr(pandora_auth, "_stored_credentials",
                        lambda: {"email": "stored@example.com", "password": "x"})
    monkeypatch.setattr(pandora_auth, "_default_env_value", lambda name: "")

    assert pandora_auth._secret("PANDORA_EMAIL") == "stored@example.com"


def test_credential_source_reports_how_it_is_stored(monkeypatch):
    from pandora import pandora_auth

    monkeypatch.setattr(pandora_auth, "_stored_credentials", lambda: {"password": "x"})
    assert pandora_auth.credential_source() == "dpapi"

    monkeypatch.setattr(pandora_auth, "_stored_credentials", lambda: {})
    monkeypatch.setattr(pandora_auth, "_default_env_value", lambda name: "plain")
    assert pandora_auth.credential_source() == "env"

    monkeypatch.setattr(pandora_auth, "_default_env_value", lambda name: "")
    assert pandora_auth.credential_source() == "none"
