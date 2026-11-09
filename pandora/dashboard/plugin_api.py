"""Backend for the Pandora desktop status-bar plugin.

Routes mount at ``/api/plugins/pandora/``. Everything here reuses the *live*
agent plugin modules (the same ``player`` singleton and the same tool handlers),
so the status bar and the agent can never disagree about what is playing.

The backend runs inside the gateway process, which is where the agent plugin is
imported (``plugins.isolation: in_process``, the default). Under ``host``
isolation the modules live in the plugin-host process instead and every route
returns 503 with an explanation.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()
log = logging.getLogger(__name__)

PLAN_TTL_SECONDS = 600.0
PLAN_FAILURE_TTL_SECONDS = 30.0
STATE_TTL_SECONDS = 1.0

_plan_lock = threading.Lock()
_plan_cache: dict[str, Any] = {"at": 0.0, "value": None}
_state_cache: dict[str, Any] = {"at": 0.0, "value": None}
_device_warm = {"started": False}

PREMIUM_PREFIXES = {"TR": "on_demand", "AL": "on_demand", "PL": "playlists"}


# ── live plugin modules ─────────────────────────────────────────────────────

def _plugin_modules(suffix: str) -> list[Any]:
    """Every live ``hermes_plugins.<package>.<suffix>`` module, launch copy first."""
    prefix = "hermes_plugins."
    tail = "." + suffix
    found: list[tuple[int, Any]] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith(prefix) or not name.endswith(tail):
            continue
        package = name[len(prefix):-len(tail)]
        if package == "pandora":
            found.append((0, module))
        elif package.startswith("pandora__home_"):
            found.append((1, module))
    found.sort(key=lambda item: item[0])
    return [module for _, module in found]


def _plugin_module(suffix: str):
    """Return the live ``hermes_plugins.<package>.<suffix>`` module.

    The loader names the package ``pandora`` or ``pandora__home_<hash>`` when
    several profiles load it in one process, so match both. The bare name is
    the launch profile's copy — the one the dashboard router serves — so prefer
    it; a ``__home_`` copy belongs to another profile and would answer with
    that profile's auth and player.
    """
    modules = _plugin_modules(suffix)
    if modules:
        return modules[0]
    if _load_local_package():
        modules = _plugin_modules(suffix)
        if modules:
            return modules[0]
    prefix = "hermes_plugins."
    tail = "." + suffix
    for package in ("pandora",):
        try:
            return importlib.import_module(f"{prefix}{package}{tail}")
        except ImportError:
            continue
    raise HTTPException(
        status_code=503,
        detail="The Pandora agent plugin is not loaded in this process "
               "(plugins.isolation: host separates it from the dashboard backend).",
    )


_local_package_loaded = False


def _load_local_package() -> bool:
    """Import this plugin's package from its own directory into this process.

    With ``plugins.isolation: host`` the agent copy lives in a separate host
    process, so the gateway (where every dashboard route runs) has no
    ``hermes_plugins.pandora`` modules. The dashboard half sits inside the same
    install, so importing the package here gives the routes a working
    client/player. Idempotent and attempted once.
    """
    global _local_package_loaded
    if _local_package_loaded:
        return "hermes_plugins.pandora" in sys.modules
    _local_package_loaded = True
    root = Path(__file__).resolve().parent.parent
    init = root / "__init__.py"
    if not init.is_file():
        return False
    parent_name = "hermes_plugins"
    if parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []  # synthetic namespace, same as the plugin loader
        sys.modules[parent_name] = parent
    name = f"{parent_name}.pandora"
    if name in sys.modules:
        return True
    try:
        spec = importlib.util.spec_from_file_location(
            name, init, submodule_search_locations=[str(root)])
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        log.warning("pandora: dashboard backend imported the plugin package locally "
                    "(pid %s, %s)", os.getpid(), root)
        return True
    except Exception as exc:  # keep the route answering with a clear 503
        sys.modules.pop(name, None)
        log.warning("pandora: local package import failed in pid %s: %s", os.getpid(), exc)
        return False


def _player():
    """The player that is actually playing — whichever profile owns it.

    Every profile gets its own plugin instance, so a fixed module would show
    "nothing playing" for music started from another profile. Prefer the first
    instance with a live mpv process; otherwise the launch copy.
    """
    fallback = None
    for module in _plugin_modules("pandora_player"):
        player = getattr(module, "player", None)
        if player is None:
            continue
        if fallback is None:
            fallback = player
        proc = getattr(player, "_proc", None)
        if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
            return player
    return fallback if fallback is not None else _plugin_module("pandora_player").player


def _tools():
    return _plugin_module("pandora_tools")


def _auth():
    return _plugin_module("pandora_auth")


def _client_module():
    return _plugin_module("pandora_client")


def _cast_module():
    return _plugin_module("pandora_cast")


# ── plan detection ──────────────────────────────────────────────────────────

def _plan_from_template(template: dict[str, Any]) -> dict[str, Any]:
    search = {str(t).upper() for t in template.get("searchTypes") or []}
    collection = {str(t).upper() for t in template.get("collectionTypes") or []}
    on_demand = bool({"TR", "AL"} & search)
    playlists = "PL" in (search | collection)
    premium = on_demand or playlists
    return {
        "tier": "premium" if premium else "standard",
        "label": "Premium" if premium else "Free or Plus",
        "on_demand": on_demand,
        "playlists": playlists,
        "podcasts": bool({"PC", "PE"} & search),
        "stations": bool({"ST", "SF"} & search),
        "note": ("On-demand songs, albums and playlists"
                 if premium else "Stations and podcasts; on-demand needs Premium"),
    }


def _signed_out_plan() -> dict[str, Any]:
    return {
        "tier": "unknown", "label": "Not signed in",
        "on_demand": False, "playlists": False,
        "podcasts": True, "stations": True,
        "note": "Run `hermes pandora login`",
    }


def _plan() -> dict[str, Any]:
    now = time.monotonic()
    with _plan_lock:
        cached = _plan_cache["value"]
        if cached is not None and now - _plan_cache["at"] < (
                PLAN_TTL_SECONDS if cached.get("tier") != "unknown" else PLAN_FAILURE_TTL_SECONDS):
            return cached
    try:
        template = (_client_module().PandoraClient().listener_template()
                    or {}).get("listenerTemplate") or {}
        value = _plan_from_template(template)
    except Exception as exc:  # auth/network: keep the chip usable
        log.debug("pandora plan lookup failed: %s", exc)
        value = _signed_out_plan()
    with _plan_lock:
        _plan_cache.update(at=time.monotonic(), value=value)
    return value


def _plan_quiet() -> dict[str, Any]:
    try:
        return _plan()
    except Exception:
        return _signed_out_plan()


# ── helpers ─────────────────────────────────────────────────────────────────

def _envelope(raw: str) -> dict[str, Any]:
    """Parse a tool handler's JSON envelope into ``{ok, ...}``."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": str(raw)[:300]}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Unexpected tool response"}
    if payload.get("error"):
        return {"ok": False, "error": str(payload["error"])}
    return {"ok": True, **payload}


def _call_tool(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "playback": "handle_pandora_playback",
        "feedback": "handle_pandora_feedback",
        "stations": "handle_pandora_stations",
        "playlists": "handle_pandora_playlists",
        "devices": "handle_pandora_devices",
    }
    module = _tools()
    handler = getattr(module, handlers[tool])
    return _envelope(handler(args))


def _require_premium(prefix: str, plan: dict[str, Any]) -> Optional[str]:
    capability = PREMIUM_PREFIXES.get(prefix.upper())
    if capability and not plan.get(capability):
        return ("On-demand songs and playlists need Pandora Premium; "
                "this account is on Free/Plus.")
    return None


def _state_now(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not force and _state_cache["value"] is not None and now - _state_cache["at"] < STATE_TTL_SECONDS:
        return _state_cache["value"]
    status = _auth().auth_status()
    if not status.get("logged_in"):
        value = {"ok": True, "logged_in": False, "playing": False, "paused": False,
                 "track": "", "artist": "", "kind": "", "plan": _signed_out_plan()}
    else:
        state = _player().state()
        value = {"ok": True, "logged_in": True, **state, "plan": _plan_quiet()}
    _state_cache.update(at=time.monotonic(), value=value)
    return value


# ── routes ──────────────────────────────────────────────────────────────────

@router.get("/state")
def get_state() -> dict[str, Any]:
    _warm_device_cache()
    return _state_now()


def _merge_stations(recent: list[dict[str, Any]], collection: list[dict[str, Any]],
                    recent_limit: int) -> list[dict[str, Any]]:
    """Recents first (capped), then the collection minus anything already listed.

    Keyed on the station id; the name is the fallback key for an item without one.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(recent)[:recent_limit] + list(collection):
        key = str(item.get("id") or item.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append({"id": item.get("id"), "name": str(item.get("name") or "")})
    return merged


def _all_stations(client, *, page_size: int = 50, max_pages: int = 4) -> list[dict[str, Any]]:
    """The account's stations; Pandora caps a page at 50, so follow the offset."""
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        payload = client.collection(types=["ST"], limit=page_size, offset=page * page_size)
        items = ((payload.get("collection") or {}).get("items") or [])
        for item in items:
            if str(item.get("__typename") or "Station") == "Station":
                out.append({"id": item.get("id"), "name": str(item.get("name") or "")})
        if len(items) < page_size:
            break
    return out


@router.get("/stations")
def get_stations(limit: int = 10) -> dict[str, Any]:
    """Recent stations first, then the rest of the collection (hybrid list).

    ``limit`` is how many *recents* lead the list; everything else follows in
    collection order so the menu can be scrolled instead of truncated.
    """
    recent_limit = max(1, min(int(limit or 10), 20))
    try:
        client = _client_module().PandoraClient()
        payload = client.recent_favorites(limit=min(20, recent_limit * 2))
        items = ((payload.get("recentFavorites") or {}).get("items") or [])
        recents = [{"id": i.get("id"), "name": str(i.get("name") or "")}
                   for i in items if str(i.get("__typename") or "") == "Station"]
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        collection = _all_stations(client)
    except Exception:
        log.debug("station collection failed", exc_info=True)
        collection = []
    stations = _merge_stations(recents, collection, recent_limit)
    if not stations:
        fallback = _call_tool("stations", {"action": "list", "limit": 50})
        if not fallback.get("ok"):
            return fallback
        stations = fallback.get("stations") or []
    return {"ok": True, "stations": stations, "recent": recent_limit}


@router.get("/search")
def get_search(q: str = "", limit: int = 9) -> dict[str, Any]:
    """Artists and songs matching ``q`` — the seeds a station can be built from."""
    text = (q or "").strip()
    if len(text) < 2:
        return {"ok": True, "query": text, "results": []}
    limit = max(1, min(int(limit or 9), 20))
    try:
        client = _client_module().PandoraClient()
        payload = client.search(text, types=["AR", "TR"], limit=limit * 2)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    kinds = {"Artist": "artist", "Track": "track"}
    results: list[dict[str, Any]] = []
    for item in ((payload.get("search") or {}).get("items") or []):
        kind = kinds.get(str(item.get("__typename") or ""))
        if not kind or not item.get("id"):
            continue
        results.append({"id": item.get("id"), "name": str(item.get("name") or ""), "kind": kind})
        if len(results) >= limit:
            break
    return {"ok": True, "query": text, "results": results}


@router.post("/station")
def post_station(body: dict[str, Any]) -> dict[str, Any]:
    """Create a station from an artist or song seed and start it.

    Every plan can do this — it is Pandora's core feature, so there is no
    premium gate here. Only the click in the menu triggers it.
    """
    seed_id = str(body.get("seed_id") or "").strip()
    if ":" not in seed_id:
        return {"ok": False, "error": "seed_id is required (e.g. AR:269367 from /search)"}
    if seed_id.split(":", 1)[0].upper() not in {"AR", "TR", "ST"}:
        return {"ok": False, "error": f"Unsupported seed type: {seed_id.split(':', 1)[0]}"}
    try:
        client = _client_module().PandoraClient()
        data = client.create_station_from_seeds([seed_id])
        node = data.get("createStationFromSeeds") or {}
        station = node.get("station") or {}
        station_id = str(station.get("id") or "")
        station_name = str(station.get("name") or "")
        if not station_id:
            return {"ok": False,
                    "error": f"Pandora did not create a station (status: {node.get('status') or 'unknown'})"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        played = _player().play_source(station_id)
    except Exception as exc:
        return {"ok": False,
                "error": f"Station '{station_name}' was created, but playback failed: {exc}"}
    _state_cache.update(at=0.0, value=None)
    return {"ok": True, "station_id": station_id, "station": station_name, **played}


@router.get("/playlists")
def get_playlists() -> dict[str, Any]:
    plan = _plan_quiet()
    if not plan.get("playlists"):
        return {"ok": True, "premium": False, "playlists": [],
                "note": "Playlists need Pandora Premium."}
    result = _call_tool("playlists", {"action": "list"})
    if not result.get("ok"):
        return result
    return {"ok": True, "premium": True, "playlists": result.get("items") or []}


@router.get("/devices")
def get_devices(refresh: bool = False) -> dict[str, Any]:
    """Cast devices on the LAN from the live browser (started on first use)."""
    try:
        devices = _cast_module().list_devices()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "output": _player().output, "devices": devices}


def _warm_device_cache() -> None:
    """Start the cast browser once, in the background, so the first menu open is instant."""
    if _device_warm["started"]:
        return
    _device_warm["started"] = True
    try:
        threading.Thread(target=lambda: _cast_module().list_devices(settle=0.0),
                         name="pandora-cast-warm", daemon=True).start()
    except Exception:
        logger.debug("cast warm-up thread failed", exc_info=True)


@router.post("/action")
def post_action(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "").strip().lower()
    allowed = {"play", "now_playing", "pause", "resume", "next", "previous",
               "replay", "stop", "set_volume", "seek"}
    if action not in allowed:
        return {"ok": False, "error": f"Unsupported playback action: {action}"}
    args: dict[str, Any] = {"action": action}
    for key in ("source_id", "volume", "position_seconds"):
        if key in body:
            args[key] = body[key]
    if action == "play" and body.get("source_id"):
        problem = _require_premium(str(body["source_id"]).split(":", 1)[0], _plan_quiet())
        if problem:
            return {"ok": False, "error": problem}
    result = _call_tool("playback", args)
    if result.get("ok"):
        _state_cache.update(at=0.0, value=None)
    return result


@router.post("/feedback")
def post_feedback(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "love").strip().lower()
    if action not in {"love", "ban", "undo"}:
        return {"ok": False, "error": f"Unsupported feedback action: {action}"}
    result = _call_tool("feedback", {"action": action})
    if result.get("ok"):
        _state_cache.update(at=0.0, value=None)
    return result


@router.post("/play-station")
def post_play_station(body: dict[str, Any]) -> dict[str, Any]:
    source_id = str(body.get("source_id") or "").strip()
    station = str(body.get("station") or "").strip()
    if not source_id and not station:
        return {"ok": False, "error": "station or source_id is required"}
    prefix = source_id.split(":", 1)[0] if source_id else "ST"
    problem = _require_premium(prefix, _plan_quiet())
    if problem:
        return {"ok": False, "error": problem}
    args = {"action": "play"}
    if source_id:
        args["source_id"] = source_id
    else:
        args["station"] = station
    result = _call_tool("stations", args)
    if result.get("ok"):
        _state_cache.update(at=0.0, value=None)
    return result


@router.post("/output")
def post_output(body: dict[str, Any]) -> dict[str, Any]:
    device = str(body.get("device") or "").strip()
    if not device:
        return {"ok": False, "error": "device is required ('local' for this PC)"}
    try:
        cached = _cast_module().list_devices(settle=0.0)
        _player().set_output(device, device_list=cached or None)
        _state_cache.update(at=0.0, value=None)
        return {"ok": True, "state": _state_now(force=True)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
