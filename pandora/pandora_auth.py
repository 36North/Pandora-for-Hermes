"""Pandora authentication: email+password login, session-token store, re-login.

Pandora has no public developer OAuth for listeners, so this plugin logs in the
same way the pandora.com web player does:

1. ``GET /`` to obtain the ``csrftoken`` cookie (sent afterwards as
   ``X-CsrfToken``);
2. ``POST /api/v1/auth/login`` with ``{username, password, redirectPath}``.

The response carries the listener auth token at
``entities.listeners[<listenerId>].authToken`` (sent afterwards as
``X-AuthToken``). The legacy ``/api/v1/listener/emailPassword`` endpoint is
DEAD (always returns ``200 {}`` with no session) — do not use it.

PerimeterX: Pandora fronts ``/api/v1/auth/login`` with PerimeterX bot
detection. Headless attempts usually get through, but when they do not the
API answers 403 (``errorCode 1215 s2s_high_score``). The fallback is a
one-time sign-in in the user's real browser:

- ``hermes pandora capture`` starts a loopback listener (127.0.0.1:18642) and
  prints a browser-console snippet; the signed-in browser POSTs the token +
  csrftoken, and the plugin stores them; or
- ``hermes pandora import-session --token ... --csrf ...`` stores values
  copied from devtools (Application → Local Storage, the ``loginAuthToken``
  state key; Network → any request's ``X-CsrfToken`` header).

State lives in the profile's ``auth.json`` under ``providers.pandora`` (same
convention as the Spotify plugin); the password lives in the profile's
``.env`` as ``PANDORA_PASSWORD`` so an expired session (Pandora's
``inactivityTimeout`` is 14400 s = 4 h) can be renewed without prompting.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROVIDER = "pandora"
DEFAULT_BASE_URL = "https://www.pandora.com"
LOGIN_PATH = "/api/v1/auth/login"
SIGNIN_URL = "https://www.pandora.com/account/sign-in"
SIGNUP_URL = "https://www.pandora.com/account/register"
DOCS_URL = "https://github.com/ChickenFingers/hermes-pandora"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Loopback capture listener used by the browser-console fallback.
CAPTURE_HOST = "127.0.0.1"
CAPTURE_PORT = 18642
CAPTURE_PATH = "/capture"
CAPTURE_CONSOLE_SNIPPET = """\
(() => {
  // Hermes Pandora plugin: send your Pandora session to the local capture
  // listener. Run in the browser console while signed in at pandora.com.
  let token = "", username = "";
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    try {
      const value = JSON.parse(localStorage.getItem(key) || "null");
      const state = value && typeof value === "object" ? (value.state || value) : null;
      if (state && typeof state.loginAuthToken === "string" && state.loginAuthToken) {
        token = state.loginAuthToken;
        username = state.username || "";
        break;
      }
    } catch (e) { /* keep scanning */ }
  }
  if (!token) {
    alert("No Pandora session token found. Make sure you are signed in at pandora.com, then re-run this snippet.");
    return;
  }
  const csrf = ((document.cookie.match(/csrftoken=([^;]+)/) || [])[1]) || "";
  fetch("http://127.0.0.1:__PORT__/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auth_token: token, csrf_token: decodeURIComponent(csrf), username: username }),
  })
    .then((r) => r.text())
    .then((t) => alert("Session sent to Hermes: " + t))
    .catch((e) => alert("Could not reach the Hermes capture listener (is `hermes pandora capture` running?): " + e));
})();
"""


class PandoraAuthError(RuntimeError):
    """Authentication failed in a way the caller should surface."""

    def __init__(self, message: str, *, code: str = "pandora_auth_error",
                 relogin_required: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


class PandoraAuthRequiredError(PandoraAuthError):
    """No usable session — the user must run ``hermes pandora login``."""


def capture_snippet(port: int = CAPTURE_PORT) -> str:
    """Browser-console snippet wired to the given capture listener port."""
    return CAPTURE_CONSOLE_SNIPPET.replace("__PORT__", str(port))


# ── settings ────────────────────────────────────────────────────────────────

@contextmanager
def _account_home():
    """Run auth-store work against the launch home, not the active profile.

    Pandora credentials belong to the *account*, not to a bot profile: a
    profile home is ``<root>/profiles/<name>``, and when one is active the
    login should be shared with every other profile. Any other home (tests, a
    custom deployment) is left untouched.
    """
    from hermes_constants import (
        get_default_hermes_root, get_hermes_home,
        reset_hermes_home_override, set_hermes_home_override)

    home = Path(get_hermes_home())
    if home.parent.name != "profiles":
        yield
        return
    token = set_hermes_home_override(get_default_hermes_root())
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _default_env_value(name: str) -> str:
    """Read ``NAME`` from the launch home's .env (the shared credential store)."""
    try:
        from hermes_constants import get_default_hermes_root
        path = get_default_hermes_root() / ".env"
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _stored_credentials() -> dict[str, Any]:
    try:
        from .pandora_secretstore import load

        return load() or {}
    except Exception:
        return {}


def _secret(name: str, default: str = "") -> str:
    if name == "PANDORA_PASSWORD":
        stored = str(_stored_credentials().get("password") or "").strip()
        if stored:
            return stored
    try:
        from agent.secret_scope import get_secret
        value = str(get_secret(name, default) or "").strip()
    except Exception:
        value = ""
    value = value or _default_env_value(name)
    if value:
        return value
    if name == "PANDORA_EMAIL":
        return str(_stored_credentials().get("email") or "").strip() or default
    return default


def credential_source() -> str:
    """How the saved password is stored — never the value itself."""
    if str(_stored_credentials().get("password") or "").strip():
        return "dpapi"
    if _default_env_value("PANDORA_PASSWORD"):
        return "env"
    return "none"


def base_url() -> str:
    return (_secret("PANDORA_API_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def save_credentials(email: str, password: str) -> None:
    """Persist credentials: email as metadata, password encrypted when possible.

    Windows DPAPI keeps the password out of ``.env`` while still readable
    unattended for automatic re-login. Every profile shares the launch home, so
    one login serves all of them. Without DPAPI the password stays in ``.env``
    exactly as before.
    """
    from hermes_cli.config import remove_env_value, save_env_value

    encrypted = False
    if password:
        try:
            from . import pandora_secretstore

            encrypted = pandora_secretstore.save(email, password)
        except Exception:
            encrypted = False
    with _account_home():
        if email:
            save_env_value("PANDORA_EMAIL", email)
        if password and not encrypted:
            save_env_value("PANDORA_PASSWORD", password)
        elif encrypted:
            remove_env_value("PANDORA_PASSWORD")  # migrate off the plaintext copy


def forget_credentials(*, keep_email: bool = True) -> None:
    from hermes_cli.config import remove_env_value

    try:
        from .pandora_secretstore import clear

        clear()
    except Exception:
        pass
    with _account_home():
        if not keep_email:
            remove_env_value("PANDORA_EMAIL")
        remove_env_value("PANDORA_PASSWORD")


# ── state (auth.json: providers.pandora) ────────────────────────────────────

def load_state() -> dict[str, Any]:
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _load_provider_state
    with _account_home(), _auth_store_lock():
        auth_store = _load_auth_store()
        state = _load_provider_state(auth_store, PROVIDER)
    return dict(state or {})


def save_state(state: dict[str, Any]) -> None:
    from hermes_cli.auth import (
        _auth_store_lock, _load_auth_store, _save_auth_store, _store_provider_state)
    with _account_home(), _auth_store_lock():
        auth_store = _load_auth_store()
        _store_provider_state(auth_store, PROVIDER, state, set_active=False)
        _save_auth_store(auth_store)


def clear_state() -> None:
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    with _account_home(), _auth_store_lock():
        auth_store = _load_auth_store()
        providers = auth_store.get("providers")
        if isinstance(providers, dict):
            providers.pop(PROVIDER, None)
        _save_auth_store(auth_store)


def device_uuid() -> str:
    """Stable per-install device id. Pandora scopes playback state to devices."""
    state = load_state()
    existing = str(state.get("device_uuid") or "").strip()
    if existing:
        return existing
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-pandora:{_install_id()}"))


def _install_id() -> str:
    from hermes_constants import get_hermes_home
    with _account_home():
        path = get_hermes_home() / "install_id"
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    return f"{uuid.getnode():x}"


def auth_status() -> dict[str, Any]:
    state = load_state()
    token = str(state.get("auth_token") or "")
    return {
        "logged_in": bool(token),
        "username": state.get("username") or "",
        "device_uuid": state.get("device_uuid") or device_uuid(),
        "last_login": state.get("last_login") or "",
        "auth_type": str(state.get("auth_type") or ""),
        "base_url": base_url(),
        "login_command": "hermes pandora login",
    }


# ── login ───────────────────────────────────────────────────────────────────

def _find_token(payload: Any, depth: int = 0) -> str:
    """Find the auth token in whatever shape the login response uses."""
    if depth > 6:
        return ""
    if isinstance(payload, dict):
        # Direct hits first: exact/partial key names seen across Pandora API eras.
        for key in ("authToken", "auth_token", "userAuthToken", "token", "sessionToken"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key, value in payload.items():
            if isinstance(value, str) and value.strip() and "token" in str(key).lower():
                if "csrf" not in str(key).lower() and "refresh" not in str(key).lower():
                    return value.strip()
        # Then recurse (e.g. {"listener": {"authToken": ...}}).
        for value in payload.values():
            found = _find_token(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_token(value, depth + 1)
            if found:
                return found
    return ""


def _extract_token(payload: Any) -> str:
    """Primary extraction: the web login puts the token in entities.listeners."""
    if isinstance(payload, dict):
        entities = payload.get("entities")
        if isinstance(entities, dict):
            listeners = entities.get("listeners")
            if isinstance(listeners, dict):
                for value in listeners.values():
                    if isinstance(value, dict):
                        token = str(value.get("authToken") or "").strip()
                        if token:
                            return token
    return _find_token(payload)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return (response.text or "").strip()[:300]
    if isinstance(payload, dict):
        for key in ("message", "errorMessage", "error", "errorString", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"]).strip()
    return (response.text or "").strip()[:300]


def _perimeterx_fallback() -> str:
    return (
        "Pandora's bot protection (PerimeterX) blocked this login attempt (HTTP 403). "
        f"Sign in once in your real browser at {SIGNIN_URL}, then either: "
        "(1) run `hermes pandora capture` and execute the browser-console snippet it "
        "prints (F12 → Console on the signed-in pandora.com tab), or "
        "(2) copy the session token from devtools (Application → Local Storage → the "
        "`loginAuthToken` state key) and run `hermes pandora import-session "
        "--token <token> --csrf <csrftoken cookie>`."
    )


def login(username: str, password: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Log in via the web player's ``/api/v1/auth/login`` flow and persist the session."""
    username = (username or "").strip()
    if not username or not password:
        raise PandoraAuthError(
            "Pandora email and password are required.",
            code="pandora_credentials_missing", relogin_required=True)

    url = f"{base_url()}{LOGIN_PATH}"
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": base_url(),
        "Referer": f"{base_url()}/account/sign-in",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            # Page load first: the csrftoken cookie is set on it.
            try:
                client.get(base_url() + "/", headers={"Accept": "text/html"})
            except httpx.HTTPError:
                pass
            cookies = {c.name: c.value for c in client.cookies.jar}
            csrf = cookies.get("csrftoken", "")
            try:
                response = client.post(
                    url,
                    json={"username": username, "password": password, "redirectPath": "/"},
                    headers={"X-CsrfToken": csrf} if csrf else None)
            except httpx.HTTPError as exc:
                raise PandoraAuthError(
                    f"Could not reach Pandora ({type(exc).__name__}: {exc})",
                    code="pandora_login_network") from exc
            cookies = {c.name: c.value for c in client.cookies.jar}
            status_code = response.status_code
            body = response
    except httpx.HTTPError as exc:
        raise PandoraAuthError(
            f"Could not reach Pandora ({type(exc).__name__}: {exc})", code="pandora_login_network"
        ) from exc

    if status_code == 403:
        raise PandoraAuthError(_perimeterx_fallback(), code="pandora_login_http_403",
                               relogin_required=True)
    if status_code in (400, 401):
        detail = _error_message(body)
        suffix = "" if detail.lower().startswith("invalid username") else f" ({detail})"
        raise PandoraAuthError(
            f"Pandora rejected the login: invalid email or password{suffix}.",
            code=f"pandora_login_http_{status_code}", relogin_required=True)
    if status_code == 429:
        raise PandoraAuthError(
            "Pandora rate-limited the login. Wait a minute and retry.",
            code="pandora_login_http_429", relogin_required=True)
    if status_code >= 400:
        raise PandoraAuthError(
            f"Pandora login failed (HTTP {status_code}): {_error_message(body)}",
            code=f"pandora_login_http_{status_code}", relogin_required=True)

    try:
        payload = body.json()
    except Exception:
        payload = None
    token = _extract_token(payload)

    if not token:
        detail = _error_message(body) or "no auth token in the response"
        raise PandoraAuthError(
            f"Pandora login did not return a session token ({detail}). "
            f"The sign-in may need a browser instead — see {SIGNIN_URL}",
            code="pandora_login_no_token", relogin_required=True)

    now = datetime.now(timezone.utc).isoformat()
    state = {
        "auth_token": token,
        "csrf_token": cookies.get("csrftoken") or _find_token(
            payload.get("csrfToken") if isinstance(payload, dict) else None),
        "cookies": cookies,
        "username": username,
        "device_uuid": device_uuid(),
        "base_url": base_url(),
        "obtained_at": now,
        "last_login": now,
        "auth_type": "password",
    }
    save_state(state)
    logger.info("Pandora login successful for %s", username)
    return state


def relogin(*, timeout: float = 30.0) -> dict[str, Any]:
    """Renew the session from credentials saved in .env (no prompt).

    Sessions expire after Pandora's inactivityTimeout (~4 h), so this runs
    whenever a 401 hits. A masked/placeholder password (e.g. ``***``) is
    detected up front: re-logging in with it would just fail with a
    confusing "invalid password" error.
    """
    email = _secret("PANDORA_EMAIL")
    password = _secret("PANDORA_PASSWORD")
    if not email or not password:
        raise PandoraAuthRequiredError(
            "Pandora session expired and no saved credentials were found. "
            "Run `hermes pandora login` to sign in again.",
            code="pandora_relogin_credentials_missing", relogin_required=True)
    if password.strip() in {"***", "REDACTED"}:
        raise PandoraAuthRequiredError(
            "Pandora session expired and the saved password is a masked placeholder "
            "(***), so it cannot be used to re-login. Run `hermes pandora login` "
            "and enter the real password when prompted.",
            code="pandora_password_placeholder", relogin_required=True)
    return login(email, password, timeout=timeout)


def resolve_credentials(*, force_relogin: bool = False) -> dict[str, Any]:
    """Return usable credentials, re-logging in from saved credentials if needed."""
    state = load_state()
    if state.get("auth_token") and not force_relogin:
        if not state.get("device_uuid"):
            state["device_uuid"] = device_uuid()
            save_state(state)
        return state
    return relogin()


def import_session(token: str, csrf_token: str = "", username: str = "") -> dict[str, Any]:
    """Store a session obtained outside the headless flow (browser fallback).

    Used by ``hermes pandora import-session`` and by the loopback capture
    listener started by ``hermes pandora capture``.
    """
    token = (token or "").strip()
    if not token:
        raise PandoraAuthError(
            "A Pandora session token is required (auth_token).",
            code="pandora_import_no_token", relogin_required=True)
    now = datetime.now(timezone.utc).isoformat()
    cookies = {}
    if (csrf_token or "").strip():
        cookies["csrftoken"] = csrf_token.strip()
    state = {
        "auth_token": token,
        "csrf_token": (csrf_token or "").strip(),
        "cookies": cookies,
        "username": (username or "").strip(),
        "device_uuid": device_uuid(),
        "base_url": base_url(),
        "obtained_at": now,
        "last_login": now,
        "auth_type": "browser",
    }
    save_state(state)
    logger.info("Pandora session imported (%s)", state["username"] or "unknown user")
    return state


def logout(*, clear_credentials: bool = True) -> dict[str, Any]:
    previous = load_state()
    clear_state()
    if clear_credentials:
        forget_credentials(keep_email=False)
    return {"logged_out": True, "previous_username": previous.get("username") or ""}
