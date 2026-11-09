"""``hermes pandora login|capture|import-session|status|logout``.

Login is email + password against pandora.com's web login endpoint. Below the
email/password prompts the CLI shows a "No account? Sign up" line with a
clickable hyperlink (OSC 8, plus the bare URL as a fallback). The browser is
only opened when the caller asks for it (``--signup``), never on a plain login.

``capture`` and ``import-session`` are the browser fallback for when
Pandora's PerimeterX bot protection blocks headless logins (HTTP 403):
``capture`` starts a loopback listener (127.0.0.1:18642) and prints a
browser-console snippet that a signed-in pandora.com tab can run to hand the
session token over; ``import-session`` stores the same values by hand.
"""

from __future__ import annotations

import http.server
import json
import logging
import queue
import socketserver
import sys
import threading
import webbrowser
from getpass import getpass
from typing import Any, Optional

from . import pandora_auth
from .pandora_auth import (
    CAPTURE_HOST,
    CAPTURE_PORT,
    CAPTURE_PATH,
    PandoraAuthError,
    SIGNIN_URL,
    SIGNUP_URL,
    capture_snippet,
)

logger = logging.getLogger(__name__)


def _hyperlink(url: str, text: str) -> str:
    """OSC 8 terminal hyperlink (clickable in Windows Terminal, WezTerm, VS Code...)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _print_signup_line() -> None:
    marker = _hyperlink(SIGNUP_URL, "Sign up")
    print(f"  No account? {marker}: {SIGNUP_URL}")
    print(f"  (or open {SIGNIN_URL} to sign in with an existing account)\n")


def _open_signup() -> None:
    try:
        opened = webbrowser.open(SIGNUP_URL)
    except Exception:
        opened = False
    if opened:
        print(f"Opened the Pandora sign-up page in your browser: {SIGNUP_URL}\n")


def _open_signin() -> None:
    try:
        opened = webbrowser.open(SIGNIN_URL)
    except Exception:
        opened = False
    if opened:
        print(f"Opened the Pandora sign-in page in your browser: {SIGNIN_URL}\n")


def _verify_session() -> None:
    """Best-effort: hit the listener template to prove the session works, and
    report the tier's allowed search/collection types."""
    try:
        from .pandora_client import PandoraClient
        template = (PandoraClient().listener_template() or {}).get("listenerTemplate") or {}
        search_types = template.get("searchTypes") or []
        collection_types = template.get("collectionTypes") or []
        if search_types:
            premium = any(t in {"TR", "AL", "PL"} for t in search_types)
            print(f"  Tier access: search={', '.join(search_types)}"
                  f"{' (on-demand available)' if premium else ' (stations/podcasts only)'}")
        if collection_types:
            print(f"  Collection: {', '.join(collection_types)}")
    except Exception as exc:  # verification is best-effort
        print(f"  Warning: could not verify the session ({exc}).")
        print("  If playback tools fail with 401, run `hermes pandora login` again.")


def _open_status_page() -> None:
    try:
        webbrowser.open("https://www.pandora.com/account")
    except Exception:
        pass


# ── capture listener (browser fallback) ─────────────────────────────────────

class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only: receives one POST with {auth_token, csrf_token, username}."""

    server_version = "HermesPandoraCapture/1.0"
    inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Session captured")
        self.inbox.put(payload)

    def log_message(self, *args: Any) -> None:  # keep the terminal clean
        pass


def _run_capture_listener(port: int, inbox: "queue.Queue[dict[str, Any]]") -> None:
    handler = type("Handler", (_CaptureHandler,), {"inbox": inbox})

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server((CAPTURE_HOST, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


# ── argparse wiring ─────────────────────────────────────────────────────────

def setup_parser(parser) -> None:  # noqa: ANN001 - argparse subparser
    sub = parser.add_subparsers(dest="pandora_command")

    login = sub.add_parser("login", help="Sign in to Pandora (email + password)")
    login.add_argument("--email", default="", help="Pandora account email (prompted if omitted)")
    login.add_argument("--password", default="", help=argparse_suppress(), metavar="PASSWORD")
    login.add_argument("--no-browser", action="store_true",
                       help="Never open a browser window during login")
    login.add_argument("--signup", action="store_true",
                       help="Open the Pandora sign-up page in your browser")

    capture = sub.add_parser(
        "capture",
        help="Receive a Pandora session from your signed-in browser (PerimeterX fallback)")
    capture.add_argument("--port", type=int, default=CAPTURE_PORT,
                         help=f"Loopback port to listen on (default {CAPTURE_PORT})")
    capture.add_argument("--wait", type=int, default=600,
                         help="Seconds to wait for the browser to send the session (default 600)")
    capture.add_argument("--no-browser", action="store_true",
                         help="Do not open the pandora.com sign-in page")

    import_session = sub.add_parser(
        "import-session",
        help="Store a Pandora session token obtained in the browser (PerimeterX fallback)")
    import_session.add_argument("--token", default="",
                                help="Pandora auth token (loginAuthToken from devtools)")
    import_session.add_argument("--csrf", default="",
                                help="csrftoken cookie value (optional but recommended)")
    import_session.add_argument("--username", default="",
                                help="Account email/username for display (optional)")

    status = sub.add_parser("status", help="Show Pandora login and player status")

    setup = sub.add_parser(
        "setup",
        help="Download the portable mpv used for local playback (checksum-verified)")
    setup.add_argument("--force", action="store_true", help="Re-download even if mpv is present")
    setup.add_argument("--url", default="",
                       help="Portable zip URL (https:// or file://); defaults to PANDORA_MPV_URL")

    logout = sub.add_parser("logout", help="Sign out of Pandora and clear saved credentials")
    logout.add_argument("--keep-credentials", action="store_true",
                        help="Remove the session but keep the saved email/password")


def argparse_suppress() -> str:
    """Sentinel help text for the password flag (kept out of shell history by the caller)."""
    return "Only for scripted use; prefer the interactive prompt"


# ── commands ────────────────────────────────────────────────────────────────

def cmd_login(args) -> int:
    saved_email = pandora_auth._secret("PANDORA_EMAIL")
    saved_password = pandora_auth._secret("PANDORA_PASSWORD")

    if getattr(args, "signup", False) and not getattr(args, "no_browser", False):
        _open_signup()

    print("Pandora sign-in")
    print("---------------")
    _print_signup_line()

    email = (getattr(args, "email", "") or saved_email or "").strip()
    if not email:
        try:
            email = input("Pandora email: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
    if not email:
        print("An email address is required.")
        return 1

    password = getattr(args, "password", "") or ""
    if not password and saved_password and not sys.stdin.isatty():
        # Non-interactive (scripted) run: reuse the saved password.
        password = saved_password
    if not password:
        prompt = "Pandora password: "
        if saved_password and saved_password.strip() not in {"***", "REDACTED"}:
            prompt = "Pandora password [Enter to reuse the saved one]: "
        try:
            password = getpass(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if not password and saved_password:
            password = saved_password
    if not password:
        print("A password is required.")
        return 1

    print("\nSigning in to Pandora...")
    try:
        state = pandora_auth.login(email, password)
    except PandoraAuthError as exc:
        print(f"\nLogin failed: {exc}")
        if "perimeterx" in str(exc).lower() or "blocked" in str(exc).lower():
            print("\nBrowser fallback: run `hermes pandora capture`, then execute the")
            print("snippet it prints in the console of your signed-in pandora.com tab.")
        return 1

    pandora_auth.save_credentials(email, password)
    print(f"Signed in as {state.get('username')}")

    # Smoke-test the session and report the tier's allowed search/collection types.
    _verify_session()

    print(f"  Session stored in auth.json under providers.{pandora_auth.PROVIDER}")
    print("  Enable the toolset once: hermes tools enable pandora")
    return 0


def cmd_capture(args) -> int:
    port = int(getattr(args, "port", CAPTURE_PORT) or CAPTURE_PORT)
    wait_seconds = int(getattr(args, "wait", 600) or 600)

    if not getattr(args, "no_browser", False):
        _open_signin()

    # Pre-bind check so a busy port fails fast instead of timing out later.
    import socket
    probe = socket.socket()
    try:
        probe.bind((CAPTURE_HOST, port))
    except OSError as exc:
        print(f"Port {port} on {CAPTURE_HOST} is already in use ({exc}).")
        print("Close the stale process or use a different --port.")
        return 1
    finally:
        probe.close()

    inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()
    errors: "queue.Queue[str]" = queue.Queue()

    def _start() -> None:
        try:
            _run_capture_listener(port, inbox)
        except OSError as exc:
            errors.put(str(exc))

    thread = threading.Thread(target=_start, daemon=True)
    thread.start()
    try:
        err = errors.get_nowait()
        print(f"Could not bind the capture listener on {CAPTURE_HOST}:{port} ({err}).")
        print("Close the stale process or use a different --port.")
        return 1
    except queue.Empty:
        pass

    print(f"Pandora session capture listening on http://{CAPTURE_HOST}:{port}{CAPTURE_PATH}")
    print("\n1. In your signed-in pandora.com tab, open DevTools (F12) → Console.")
    print("2. Paste the snippet below and press Enter.")
    print("3. The session is sent to this terminal and stored automatically.")
    print(f"\nWaiting up to {wait_seconds} s for the browser to send the session...\n")
    print(capture_snippet(port))
    print()

    try:
        payload = inbox.get(timeout=wait_seconds)
    except queue.Empty:
        print("\nTimed out — no session received. Re-run the snippet in the browser console,")
        print("or copy the token manually and use `hermes pandora import-session`.")
        return 1

    token = str(payload.get("auth_token") or "").strip()
    csrf = str(payload.get("csrf_token") or "").strip()
    username = str(payload.get("username") or "").strip() or pandora_auth._secret("PANDORA_EMAIL")
    if not token:
        print(f"Received a payload without an auth token: {payload!r}")
        return 1

    try:
        pandora_auth.import_session(token, csrf, username)
    except PandoraAuthError as exc:
        print(f"Storing the session failed: {exc}")
        return 1

    print(f"\nSession received and stored (user: {username or 'unknown'}).")
    _verify_session()
    print("  Enable the toolset once: hermes tools enable pandora")
    return 0


def cmd_import_session(args) -> int:
    token = (getattr(args, "token", "") or pandora_auth._secret("PANDORA_AUTH_TOKEN") or "").strip()
    csrf = (getattr(args, "csrf", "") or pandora_auth._secret("PANDORA_CSRF_TOKEN") or "").strip()
    username = (getattr(args, "username", "") or pandora_auth._secret("PANDORA_EMAIL") or "").strip()
    if not token:
        print("No session token provided.")
        print("  Pass --token <loginAuthToken> (from devtools → Application → Local Storage),")
        print("  or run `hermes pandora capture` and use the browser-console snippet.")
        return 1
    if not csrf:
        print("  Note: no --csrf given; the csrftoken cookie will be re-derived on first use.")
    try:
        pandora_auth.import_session(token, csrf, username)
    except PandoraAuthError as exc:
        print(f"Import failed: {exc}")
        return 1
    print(f"Session imported (user: {username or 'unknown'}).")
    _verify_session()
    print("  Enable the toolset once: hermes tools enable pandora")
    return 0


def cmd_status(args) -> Optional[int]:
    status = pandora_auth.auth_status()
    if not status["logged_in"]:
        print("Pandora: not signed in.")
        print("  Run `hermes pandora login` (a sign-up link is shown if you need an account).")
        return 1
    print("Pandora: signed in")
    print(f"  Account:  {status['username']}")
    auth_type = status.get("auth_type") or "unknown"
    print(f"  Auth:     {auth_type}{' (browser fallback)' if auth_type == 'browser' else ''}")
    print(f"  Creds:    {pandora_auth.credential_source()}")
    print(f"  Device:   {status['device_uuid']}")
    print(f"  Login at: {status['last_login']}")
    try:
        from .pandora_player import player
        state = player.state()
        if state.get("kind"):
            print(f"  Playing:  {state.get('track')} — {state.get('artist')} "
                  f"({state.get('position_seconds')}s / {state.get('duration_seconds')}s)")
            print(f"  Source:   {state.get('source')} ({state.get('source_type')})")
        else:
            print("  Player:   idle")
    except Exception:
        pass
    return 0


def cmd_logout(args) -> int:
    try:
        from .pandora_player import player
        player.stop()
    except Exception:
        pass
    result = pandora_auth.logout(clear_credentials=not getattr(args, "keep_credentials", False))
    print("Signed out of Pandora.")
    if result.get("previous_username"):
        print(f"  Removed the session for {result['previous_username']}.")
    print("  Saved credentials were cleared." if not getattr(args, "keep_credentials", False)
          else "  Saved email/password kept for the next login.")
    return 0


def cmd_setup(args) -> int:
    """Download the portable mpv (verified) — the plugin no longer ships it."""
    from . import pandora_mpv

    url = getattr(args, "url", "") or None
    force = bool(getattr(args, "force", False))
    try:
        exe = pandora_mpv.download(url=url, force=force)
    except pandora_mpv.MpvSetupError as exc:
        print(f"mpv setup failed: {exc}")
        print("  Set PANDORA_MPV_URL to a portable zip, or PANDORA_MPV_PATH to an installed mpv.")
        return 1
    print(f"mpv: {exe}")
    try:
        import subprocess
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15)
        first = next((line for line in (out.stdout or out.stderr).splitlines() if line.strip()), "")
        if first:
            print(f"     {first}")
    except Exception:
        pass
    print("Local playback is ready.")
    print("  → Restart the Hermes desktop app once (or Rescan in Capabilities → Plugins)")
    print("    so the Pandora chip is copied into the app and appears in the status bar.")
    return 0


_COMMANDS = {
    "login": cmd_login,
    "capture": cmd_capture,
    "import-session": cmd_import_session,
    "status": cmd_status,
    "logout": cmd_logout,
    "setup": cmd_setup,
}


def handle(args) -> int:
    command = getattr(args, "pandora_command", None)
    func = _COMMANDS.get(command)
    if func is None:
        print("Usage: hermes pandora {login|capture|import-session|status|logout|setup}")
        return 1
    return func(args) or 0
