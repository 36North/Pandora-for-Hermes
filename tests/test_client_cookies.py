"""Mid-session cookie rotation is written back to the auth state."""
from pandora import pandora_client as pc


BASE_STATE = {
    "auth_token": "tok-a",
    "csrf_token": "csrf-a",
    "cookies": {"at": "tok-a", "csrftoken": "csrf-a", "_pxhd": "pxhd-a"},
    "base_url": "https://www.pandora.com",
    "device_uuid": "dev-1",
}


def _client(monkeypatch):
    saved = []
    monkeypatch.setattr(pc.pandora_auth, "save_state",
                        lambda state: saved.append(dict(state)))
    state = dict(BASE_STATE, cookies=dict(BASE_STATE["cookies"]))
    return pc.PandoraClient(state=state), saved


def test_rotated_pxhd_is_persisted(monkeypatch):
    client, saved = _client(monkeypatch)

    client._client.cookies.set("_pxhd", "pxhd-b", domain=".pandora.com")
    client._persist_cookies()

    assert saved, "a rotated cookie must trigger a write"
    assert saved[-1]["cookies"]["_pxhd"] == "pxhd-b"
    assert client._state["cookies"]["_pxhd"] == "pxhd-b"


def test_rotated_csrf_updates_the_header_value(monkeypatch):
    client, saved = _client(monkeypatch)

    client._client.cookies.set("csrftoken", "csrf-b", domain=".pandora.com")
    client._persist_cookies()

    assert saved[-1]["csrf_token"] == "csrf-b"
    assert client._headers["X-CsrfToken"] == "csrf-b"


def test_unchanged_jar_does_not_rewrite_auth_json(monkeypatch):
    client, saved = _client(monkeypatch)

    client._persist_cookies()
    client._persist_cookies()

    assert saved == []
