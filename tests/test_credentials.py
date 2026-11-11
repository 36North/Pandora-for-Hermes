"""Tests for credential persistence: only Hermes's profile secret scope is used."""

from __future__ import annotations

import pytest

from pandora import pandora_auth


class _Writer:
    """Stand-in for hermes_cli.config's .env writers."""

    def __init__(self):
        self.saved: dict[str, str] = {}
        self.removed: list[str] = []

    def save(self, name: str, value: str) -> None:
        self.saved[name] = value

    def remove(self, name: str) -> None:
        self.removed.append(name)


@pytest.fixture
def env_writer(monkeypatch):
    import hermes_cli.config as config

    writer = _Writer()
    monkeypatch.setattr(config, "save_env_value", writer.save)
    monkeypatch.setattr(config, "remove_env_value", writer.remove)
    return writer


def test_save_credentials_writes_the_declared_names(env_writer):
    pandora_auth.save_credentials("me@example.com", "pw")
    assert env_writer.saved == {"PANDORA_EMAIL": "me@example.com", "PANDORA_PASSWORD": "pw"}


def test_save_credentials_skips_empty_values(env_writer):
    pandora_auth.save_credentials("", "")
    assert env_writer.saved == {}
    pandora_auth.save_credentials("me@example.com", "")
    assert env_writer.saved == {"PANDORA_EMAIL": "me@example.com"}


def test_forget_credentials_removes_password_only_by_default(env_writer):
    pandora_auth.forget_credentials()
    assert env_writer.removed == ["PANDORA_PASSWORD"]


def test_forget_credentials_removes_both_when_asked(env_writer):
    pandora_auth.forget_credentials(keep_email=False)
    assert env_writer.removed == ["PANDORA_EMAIL", "PANDORA_PASSWORD"]


def test_secret_resolves_from_the_process_env(monkeypatch):
    # Single-profile execution: get_secret falls through to os.environ.
    monkeypatch.setenv("PANDORA_PASSWORD", "pw")
    assert pandora_auth._secret("PANDORA_PASSWORD") == "pw"
    monkeypatch.delenv("PANDORA_PASSWORD", raising=False)


def test_secret_email_falls_back_to_provider_state(monkeypatch):
    monkeypatch.delenv("PANDORA_EMAIL", raising=False)
    monkeypatch.setattr(pandora_auth, "load_state", lambda: {"username": "state@example.com"})
    assert pandora_auth._secret("PANDORA_EMAIL") == "state@example.com"


def test_credential_source_env(monkeypatch):
    monkeypatch.setenv("PANDORA_PASSWORD", "pw")
    assert pandora_auth.credential_source() == "env"


def test_credential_source_none(monkeypatch):
    monkeypatch.delenv("PANDORA_PASSWORD", raising=False)
    assert pandora_auth.credential_source() == "none"
