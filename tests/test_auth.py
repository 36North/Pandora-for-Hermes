"""Tests for pandora_auth: token extraction, login persistence, re-login paths."""

from __future__ import annotations

import httpx
import pytest

from pandora import pandora_auth


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.setattr(pandora_auth, "_secret", lambda name, default="": default)
    monkeypatch.setattr(pandora_auth, "save_state", lambda state: None)
    monkeypatch.setattr(pandora_auth, "base_url", lambda: "https://www.pandora.com")


class FakeClient:
    """httpx.Client stand-in: GET sets nothing, POST returns canned responses."""

    def __init__(self, *a, **kw):
        self.cookies = httpx.Cookies()
        self.cookies.set("csrftoken", "csrf-123", domain="www.pandora.com")
        self.post_response = None
        self.post_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        return httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

    def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self.post_response(url)


@pytest.fixture
def fake_httpx(monkeypatch):
    """Patch pandora_auth.httpx.Client to FakeClient; returns set_response.

    Works whether the response is configured before or after login() creates
    the client: configured responses are re-applied to every instance.
    """
    state = {"response": None, "clients": []}

    class ClientFactory:
        def __call__(self, *a, **kw):
            client = FakeClient()
            client.post_response = state["response"]
            state["clients"].append(client)
            return client

    monkeypatch.setattr(pandora_auth.httpx, "Client", ClientFactory())

    def _set_response(response_for_url):
        state["response"] = response_for_url
        for client in state["clients"]:
            client.post_response = response_for_url

    return _set_response


def test_find_token_direct_and_nested():
    assert pandora_auth._find_token({"authToken": "abc"}) == "abc"
    assert pandora_auth._find_token({"result": {"userAuthToken": "xyz"}}) == "xyz"
    assert pandora_auth._find_token({"listener": {"sessionToken": "s1"}}) == "s1"
    assert pandora_auth._find_token({"csrfToken": "csrf"}) == ""
    assert pandora_auth._find_token({"nope": 1}) == ""
    assert pandora_auth._find_token(None) == ""


def test_extract_token_prefers_entities_listeners():
    payload = {
        "entities": {
            "listeners": {"791": {"authToken": "tok-ent", "username": "u"}},
        }
    }
    assert pandora_auth._extract_token(payload) == "tok-ent"
    # Falls back to the generic search when entities are absent.
    assert pandora_auth._extract_token({"authToken": "tok-flat"}) == "tok-flat"


def test_login_success_stores_token(monkeypatch, fake_httpx):
    captured = {}

    def response_for(url):
        return httpx.Response(
            200,
            json={"entities": {"listeners": {"791": {"authToken": "tok-9", "username": "me"}}}},
            request=httpx.Request("POST", url),
        )

    fake_httpx(response_for)
    monkeypatch.setattr(pandora_auth, "save_state", lambda state: captured.setdefault("state", state))

    state = pandora_auth.login("me@example.com", "pw")
    assert state["auth_token"] == "tok-9"
    assert state["csrf_token"] == "csrf-123"
    assert state["username"] == "me@example.com"
    assert state["auth_type"] == "password"
    assert state["device_uuid"]
    assert captured["state"]["cookies"]["csrftoken"] == "csrf-123"


def test_login_request_shape(monkeypatch):
    seen = {}

    class CapturingClient(FakeClient):
        def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return httpx.Response(200, json={"authToken": "t"},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(pandora_auth.httpx, "Client", CapturingClient)
    monkeypatch.setattr(pandora_auth, "save_state", lambda state: None)
    pandora_auth.login("me@example.com", "pw")
    assert seen["url"].endswith("/api/v1/auth/login")
    assert seen["json"] == {"username": "me@example.com", "password": "pw", "redirectPath": "/"}
    assert seen["headers"] == {"X-CsrfToken": "csrf-123"}


def test_login_wrong_password_requires_relogin(monkeypatch, fake_httpx):
    fake_httpx(lambda url: httpx.Response(
        400,
        json={"message": "Invalid username and/or password",
              "errorString": "AUTH_INVALID_USERNAME_PASSWORD"},
        request=httpx.Request("POST", url)))
    with pytest.raises(pandora_auth.PandoraAuthError) as exc:
        pandora_auth.login("me@example.com", "wrong")
    assert "invalid email or password" in str(exc.value)
    assert exc.value.code == "pandora_login_http_400"
    assert exc.value.relogin_required is True


def test_login_perimeterx_blocked_suggests_browser_fallback(monkeypatch, fake_httpx):
    fake_httpx(lambda url: httpx.Response(
        403, json={"errorCode": 1215, "s2s": "high_score"},
        request=httpx.Request("POST", url)))
    with pytest.raises(pandora_auth.PandoraAuthError) as exc:
        pandora_auth.login("me@example.com", "pw")
    message = str(exc.value)
    assert "PerimeterX" in message
    assert "capture" in message
    assert exc.value.code == "pandora_login_http_403"
    assert exc.value.relogin_required is True


def test_login_without_token_is_an_error(monkeypatch, fake_httpx):
    fake_httpx(lambda url: httpx.Response(200, json={"status": "ok"},
                                         request=httpx.Request("POST", url)))
    with pytest.raises(pandora_auth.PandoraAuthError) as exc:
        pandora_auth.login("me@example.com", "pw")
    assert exc.value.code == "pandora_login_no_token"


def test_login_requires_credentials():
    with pytest.raises(pandora_auth.PandoraAuthError) as exc:
        pandora_auth.login("", "pw")
    assert exc.value.code == "pandora_credentials_missing"


def test_relogin_without_saved_credentials(monkeypatch):
    monkeypatch.setattr(pandora_auth, "_secret", lambda name, default="": "")
    with pytest.raises(pandora_auth.PandoraAuthRequiredError):
        pandora_auth.relogin()


def test_relogin_with_placeholder_password_refuses(monkeypatch):
    secrets = {"PANDORA_EMAIL": "me@example.com", "PANDORA_PASSWORD": "***"}
    monkeypatch.setattr(pandora_auth, "_secret", lambda name, default="": secrets.get(name, ""))
    with pytest.raises(pandora_auth.PandoraAuthRequiredError) as exc:
        pandora_auth.relogin()
    assert exc.value.code == "pandora_password_placeholder"


def test_relogin_calls_login(monkeypatch):
    secrets = {"PANDORA_EMAIL": "me@example.com", "PANDORA_PASSWORD": "real-pw"}
    monkeypatch.setattr(pandora_auth, "_secret", lambda name, default="": secrets.get(name, ""))
    state = {"auth_token": "tok-new"}
    monkeypatch.setattr(pandora_auth, "login", lambda email, password, **kw: state)
    assert pandora_auth.relogin() is state


def test_import_session_stores_browser_state(monkeypatch):
    captured = {}
    monkeypatch.setattr(pandora_auth, "save_state", lambda state: captured.setdefault("state", state))
    state = pandora_auth.import_session("tok-imp", "csrf-imp", "me@example.com")
    assert state["auth_token"] == "tok-imp"
    assert state["auth_type"] == "browser"
    assert state["cookies"] == {"csrftoken": "csrf-imp"}
    assert captured["state"]["username"] == "me@example.com"


def test_import_session_requires_token(monkeypatch):
    with pytest.raises(pandora_auth.PandoraAuthError) as exc:
        pandora_auth.import_session("  ")
    assert exc.value.code == "pandora_import_no_token"


def test_capture_snippet_points_at_listener():
    snippet = pandora_auth.capture_snippet()
    assert "127.0.0.1:18642/capture" in snippet
    assert "loginAuthToken" in snippet
    assert "loginAuthToken" in pandora_auth.capture_snippet(9999)
    assert "127.0.0.1:9999/capture" in pandora_auth.capture_snippet(9999)


def test_auth_status_shape(monkeypatch):
    monkeypatch.setattr(pandora_auth, "load_state",
                        lambda: {"auth_token": "t", "username": "u", "auth_type": "password"})
    status = pandora_auth.auth_status()
    assert status["logged_in"] is True
    assert status["username"] == "u"
    assert status["auth_type"] == "password"
    assert status["login_command"] == "hermes pandora login"


def test_signup_url_points_at_register_page():
    assert pandora_auth.SIGNUP_URL == "https://www.pandora.com/account/register"
