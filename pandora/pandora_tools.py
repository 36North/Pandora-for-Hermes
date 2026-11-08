"""Pandora tools registered by this plugin's ``register(ctx)``.

Tools (toolset ``pandora``):
  pandora_playback    — play a source, now-playing, pause/resume, next/prev, replay, volume, seek
  pandora_search      — search stations/tracks/albums/playlists/podcasts/artists
  pandora_stations    — list your stations, play one by name/id, recent, shuffle/thumbprint,
                        create from seeds, add/remove seeds, rename
  pandora_feedback    — thumbs up / down / undo on the current track
  pandora_collection  — list collected items by type, recent favorites, favorite/unfavorite
  pandora_playlists   — list/create/add/rename/replace items (Premium)
  pandora_devices     — list/set/status of the playback output (local mpv or Cast)

Handlers return the Hermes tool JSON envelope (``tool_result`` / ``tool_error``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from tools.registry import tool_error, tool_result

from . import pandora_auth
from .pandora_client import PandoraAPIError, PandoraClient, PandoraError, normalize_source_id
from .pandora_player import PlayerError, PlayerUnavailable, player

logger = logging.getLogger(__name__)

TOOLSET = "pandora"

# ── helpers ─────────────────────────────────────────────────────────────────

def _check_pandora() -> bool:
    try:
        return bool(pandora_auth.auth_status().get("logged_in"))
    except Exception:
        return False


def _pandora_error(exc: Exception) -> str:
    from .pandora_auth import PandoraAuthError, PandoraAuthRequiredError
    if isinstance(exc, PandoraAuthRequiredError):
        return tool_error(
            f"{exc} Pandora is not signed in — ask the user to run `hermes pandora login` "
            "(email + password; a sign-up link is shown if they need an account).")
    if isinstance(exc, PandoraAuthError):
        return tool_error(f"{exc} Run `hermes pandora login` again.")
    if isinstance(exc, PlayerUnavailable):
        return tool_error(str(exc))
    if isinstance(exc, (PandoraAPIError, PandoraError, PlayerError)):
        return tool_error(str(exc))
    try:
        from .pandora_cast import CastError, CastUnavailable
        if isinstance(exc, (CastUnavailable, CastError)):
            return tool_error(str(exc))
    except ImportError:
        pass
    logger.exception("pandora tool failure")
    return tool_error(f"Pandora tool failed: {type(exc).__name__}: {exc}")


def _required(value: Any, message: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PandoraError(message)
    return value


def _ok(action: str, **fields: Any) -> str:
    return tool_result({"success": True, "action": action, **fields})


def _items(payload: dict, *path: str) -> list[dict]:
    node: Any = payload
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    items = node.get("items") if isinstance(node, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def _compact(entity: dict) -> dict:
    out = {"id": entity.get("id"), "type": entity.get("__typename")}
    if entity.get("name"):
        out["name"] = entity.get("name")
    return out


def _dispatcher(name: str, default: str, table: dict[str, Callable[..., str]],
                prepare: Optional[Callable[[dict], dict]] = None):
    def handle(args: dict, **kw) -> str:
        action = str(args.get("action") or default).strip().lower()
        try:
            if prepare is not None:
                args = prepare(args)
            handler = table.get(action)
            if handler is None:
                return tool_error(f"Unknown {name} action: {action}. Try one of: {', '.join(sorted(table))}")
            return handler(args, action)
        except Exception as exc:
            return _pandora_error(exc)
    handle.__name__ = handle.__qualname__ = f"_handle_{name}"
    return handle


# ── pandora_playback ────────────────────────────────────────────────────────

def _pb_play(args: dict, action: str) -> str:
    source_id = args.get("source_id") or args.get("id")
    if not source_id:
        raise PandoraError("source_id is required for action='play' (get one from pandora_search).")
    return _ok(action, **player.play_source(str(source_id)))


def _pb_now(args: dict, action: str) -> str:
    return _ok(action, **player.state())


def _pb_pause(args: dict, action: str) -> str:
    return _ok(action, **player.pause())


def _pb_resume(args: dict, action: str) -> str:
    return _ok(action, **player.resume())


def _pb_next(args: dict, action: str) -> str:
    return _ok(action, **player.next())


def _pb_previous(args: dict, action: str) -> str:
    return _ok(action, **player.previous())


def _pb_replay(args: dict, action: str) -> str:
    return _ok(action, **player.replay())


def _pb_stop(args: dict, action: str) -> str:
    return _ok(action, **player.stop())


def _pb_volume(args: dict, action: str) -> str:
    level = args.get("volume")
    return _ok(action, **player.set_volume(int(_required(level, "volume (0-100) is required."))))


def _pb_seek(args: dict, action: str) -> str:
    position = args.get("position_seconds")
    return _ok(action, **player.seek(float(_required(position, "position_seconds is required."))))


handle_pandora_playback = _dispatcher("pandora_playback", "now_playing", {
    "play": _pb_play, "now_playing": _pb_now, "pause": _pb_pause, "resume": _pb_resume,
    "next": _pb_next, "previous": _pb_previous, "replay": _pb_replay, "stop": _pb_stop,
    "set_volume": _pb_volume, "seek": _pb_seek,
})


# ── pandora_search ──────────────────────────────────────────────────────────

SEARCH_TYPE_LABEL = {
    "SF": "station", "ST": "station", "TR": "track", "AL": "album", "PL": "playlist",
    "PC": "podcast", "PE": "episode", "AR": "artist", "ART": "artist",
}

SEARCH_KIND_BY_TYPENAME = {
    "StationFactory": "station", "Station": "station", "Track": "track", "Album": "album",
    "Playlist": "playlist", "Podcast": "podcast", "PodcastEpisode": "episode", "Artist": "artist",
}


def _kind_of(item: dict) -> str:
    prefix = str(item.get("id") or "").split(":", 1)[0].upper()
    return (SEARCH_TYPE_LABEL.get(prefix)
            or SEARCH_KIND_BY_TYPENAME.get(str(item.get("__typename") or ""))
            or "item")


def _handle_pandora_search(args: dict, **kw) -> str:
    action = "search"
    try:
        query = str(_required(args.get("query"), "query is required.")).strip()
        types = args.get("types") or args.get("type")
        if isinstance(types, str):
            types = [t.strip() for t in types.replace(" ", ",").split(",") if t.strip()]
        client = PandoraClient()
        payload = client.search(query, types=[str(t).upper() for t in types] if types else None,
                                limit=int(args.get("limit") or 10))
        results = []
        for item in _items(payload, "search"):
            compact = _compact(item)
            compact["kind"] = _kind_of(item)
            results.append(compact)
        return _ok(action, query=query, count=len(results), results=results,
                   hint="Play a station with pandora_playback action='play' and the id.")
    except Exception as exc:
        return _pandora_error(exc)


# ── pandora_stations ────────────────────────────────────────────────────────

def _station_name(station: dict) -> str:
    return str(station.get("name") or "")


def _st_list(args: dict, action: str) -> str:
    client = PandoraClient()
    payload = client.collection(types=["ST"], limit=int(args.get("limit") or 50))
    stations = [{"id": i.get("id"), "name": _station_name(i)} for i in _items(payload, "collection")]
    return _ok(action, count=len(stations), stations=stations)


def _find_station(client: PandoraClient, target: str) -> dict:
    payload = client.collection(types=["ST"], limit=50)
    wanted = target.strip().lower()
    stations = _items(payload, "collection")
    for station in stations:
        if str(station.get("id") or "").lower() == wanted:
            return station
    for station in stations:
        if _station_name(station).lower() == wanted:
            return station
    for station in stations:
        if wanted in _station_name(station).lower():
            return station
    return {}


def _st_play(args: dict, action: str) -> str:
    client = PandoraClient()
    target = str(_required(args.get("station") or args.get("name") or args.get("source_id"),
                           "station (name) or source_id is required."))
    if ":" in target:
        return _ok(action, **player.play_source(normalize_source_id(target)))
    station = _find_station(client, target)
    if not station:
        raise PandoraError(f"No station matching {target!r}. Use pandora_stations action='list'.")
    return _ok(action, **player.play_source(str(station["id"])))


def _st_recent(args: dict, action: str) -> str:
    client = PandoraClient()
    payload = client.recent_favorites(limit=int(args.get("limit") or 10))
    items = [{"id": i.get("id"), "name": _station_name(i), "type": i.get("__typename")}
             for i in _items(payload, "recentFavorites")]
    return _ok(action, items=items)


def _st_special(args: dict, action: str, needles: tuple[str, ...]) -> str:
    client = PandoraClient()
    payload = client.collection(types=["ST"], limit=50)
    for station in _items(payload, "collection"):
        name = _station_name(station).lower()
        if any(needle in name for needle in needles):
            return _ok(action, **player.play_source(str(station["id"])))
    raise PandoraError(f"No {'/'.join(needles)} station in the collection yet.")


def _st_create(args: dict, action: str) -> str:
    client = PandoraClient()
    seeds = args.get("seed_ids") or args.get("seed_id")
    if isinstance(seeds, str):
        seeds = [seeds]
    seeds = [str(s) for s in (seeds or [])]
    if not seeds:
        raise PandoraError("seed_id or seed_ids is required (e.g. 'AR:269367' from pandora_search).")
    data = client.create_station_from_seeds(seeds, station_name=args.get("name"))
    node = data.get("createStationFromSeeds") or {}
    station = node.get("station") or {}
    return _ok(action, status=node.get("status"), station_id=station.get("id"),
               station=station.get("name"),
               hint="The station is now in your collection; start it with pandora_stations action='play'.")


def _st_seed(args: dict, action: str) -> str:
    client = PandoraClient()
    station = str(_required(args.get("station") or args.get("station_id") or args.get("name"),
                            "station (name or id) is required."))
    seed = str(_required(args.get("seed_id") or args.get("seed"), "seed_id is required."))
    if ":" not in station:
        found = _find_station(client, station)
        if not found:
            raise PandoraError(f"No station matching {station!r}. Use pandora_stations action='list'.")
        station = str(found["id"])
    data = client.add_station_seed(station, seed) if action == "add_seed" \
        else client.remove_station_seed(station, seed)
    node = (data.get("addStationSeed") or data.get("removeStationSeed") or {})
    return _ok(action, status=node.get("status"), station_id=station, seed_id=seed)


def _st_rename(args: dict, action: str) -> str:
    client = PandoraClient()
    station = str(_required(args.get("station") or args.get("station_id") or args.get("name"),
                            "station (name or id) is required."))
    name = str(_required(args.get("new_name") or args.get("rename_to"),
                         "new_name is required (the new station name)."))
    if ":" not in station:
        found = _find_station(client, station)
        if not found:
            raise PandoraError(f"No station matching {station!r}. Use pandora_stations action='list'.")
        station = str(found["id"])
    data = client.set_station_details(station, name=name,
                                      description=str(args.get("description") or ""))
    node = data.get("setStationDetails") or {}
    return _ok(action, status=node.get("status"), station_id=station, name=name)


handle_pandora_stations = _dispatcher("pandora_stations", "list", {
    "list": _st_list,
    "play": _st_play,
    "recent": _st_recent,
    "shuffle": lambda a, act: _st_special(a, act, ("shuffle",)),
    "thumbprint": lambda a, act: _st_special(a, act, ("thumbprint",)),
    "create": _st_create,
    "add_seed": _st_seed,
    "remove_seed": _st_seed,
    "rename": _st_rename,
})


# ── pandora_feedback ────────────────────────────────────────────────────────

def _current_feedback_context() -> tuple[str, str, str, int]:
    """(target_id, source_id, track_token, elapsed) for the currently playing item."""
    state = player.state()
    if not state.get("source"):
        raise PandoraError("Nothing is playing. Start a station first, then give feedback.")
    item = player.item or {}
    if str(item.get("__typename") or "").endswith("AdItem"):
        raise PandoraError("Feedback is not available during an ad.")
    track = item.get("track") or {}
    target_id = str(track.get("id") or "")
    track_token = str(item.get("trackToken") or "")
    if not target_id or not track_token:
        raise PandoraError(
            "Pandora did not return a track id for the current song, so feedback can't be sent. "
            "Try action='now_playing' to refresh, then retry.")
    elapsed = max(0, int(float(state.get("position_seconds") or 0)))
    return target_id, str(state.get("source")), track_token, elapsed


def _fb_love(args: dict, action: str) -> str:
    target_id, source_id, token, elapsed = _current_feedback_context()
    data = PandoraClient().set_feedback(target_id=target_id, source_id=source_id, value="UP",
                                        track_token=token, elapsed=elapsed)
    node = ((data.get("feedback") or {}).get("setFeedback") or {})
    player.item["feedbackValue"] = "THUMB_UP"
    return _ok(action, status=node.get("status"), **player.state())


def _fb_ban(args: dict, action: str) -> str:
    target_id, source_id, token, elapsed = _current_feedback_context()
    data = PandoraClient().set_feedback(target_id=target_id, source_id=source_id, value="DOWN",
                                        track_token=token, elapsed=elapsed)
    node = ((data.get("feedback") or {}).get("setFeedback") or {})
    # A thumbs-down also advances the station; play whatever Pandora hands back next.
    player.item["feedbackValue"] = "THUMB_DOWN"
    nxt = node.get("current") or {}
    if nxt:
        with player._lock:
            player._set_item(nxt, source_id=source_id, source_type=player.source_type,
                             index=int(nxt.get("index") or player.index + 1))
            # Route through the active output (local mpv OR the Cast device);
            # _ensure_mpv() here would spawn mpv even when casting.
            player._ensure_output()
            player._play_item_audio()
    return _ok(action, status=node.get("status"), **player.state())


def _fb_undo(args: dict, action: str) -> str:
    target_id, source_id, token, elapsed = _current_feedback_context()
    data = PandoraClient().remove_feedback(target_id=target_id, source_id=source_id,
                                           track_token=token, elapsed=elapsed)
    node = ((data.get("feedback") or {}).get("removeFeedback") or {})
    player.item["feedbackValue"] = None
    return _ok(action, status=node.get("status"), **player.state())


handle_pandora_feedback = _dispatcher("pandora_feedback", "love", {
    "love": _fb_love, "ban": _fb_ban, "undo": _fb_undo,
})


# ── pandora_collection ──────────────────────────────────────────────────────

def _handle_pandora_collection(args: dict, **kw) -> str:
    action = str(args.get("action") or "list").strip().lower()
    try:
        client = PandoraClient()
        if action == "recent_favorites":
            payload = client.recent_favorites(limit=int(args.get("limit") or 10))
            items = [_compact(i) for i in _items(payload, "recentFavorites")]
            return _ok(action, items=items)
        if action in ("favorite", "unfavorite"):
            return _col_favorite(client, args, action)
        if action != "list":
            return tool_error(f"Unknown pandora_collection action: {action}")
        types = args.get("types") or ["ST"]
        if isinstance(types, str):
            types = [types]
        payload = client.collection(types=[str(t).upper() for t in types],
                                    limit=int(args.get("limit") or 50),
                                    offset=int(args.get("offset") or 0))
        items = [_compact(i) for i in _items(payload, "collection")]
        return _ok(action, types=types, count=len(items), items=items)
    except Exception as exc:
        return _pandora_error(exc)


# ── pandora_collection: favorites (collect / uncollect) ────────────────────
# Live-verified on Free tier: collect() accepts SF/AL/AR/TR/PC/PE/PL ids but
# NOT ST ids (stations can be uncollected, not re-collected by id on Free).
# Surface Pandora's own restriction message when it occurs.

def _col_favorite(client: PandoraClient, args: dict, action: str) -> str:
    target = str(_required(args.get("id") or args.get("name"),
                           "id (e.g. 'SF:18245:802') or name is required."))
    if ":" not in target:
        payload = client.search(target, limit=10)
        items = _items(payload, "search")
        if not items:
            raise PandoraError(f"No search results for {target!r}; pass an explicit id instead.")
        target = str(items[0].get("id"))
    data = client.collect([target]) if action == "favorite" else client.uncollect([target])
    node = data.get("collect") or data.get("uncollect") or {}
    base = "Favorite added." if action == "favorite" else "Favorite removed."
    message = node.get("message") or base
    return _ok(action, status=node.get("status"), id=target, message=message)


# ── pandora_playlists ───────────────────────────────────────────────────────
# Playlists are a Premium feature. On Free tier the mutations fail server-side
# with a generic error; we detect that via listenerTemplate's collectionTypes
# and replace it with a clear tier message.

def _tier_has_playlists(client: PandoraClient) -> bool:
    try:
        template = (client.listener_template().get("listenerTemplate") or {})
        return "PL" in [str(t).upper() for t in (template.get("collectionTypes") or [])]
    except Exception:
        return True  # unknown tier -> don't pre-empt the real error


def _playlist_guard(client: PandoraClient, exc: Exception) -> None:
    if isinstance(exc, Exception) and not _tier_has_playlists(client):
        raise PandoraError(
            "Playlists are a Premium feature (not included in Plus or Free); "
            "this account does not have playlist access.") from exc


def _resolve_playlist_id(client: PandoraClient, target: str) -> str:
    if ":" in target:
        return target
    payload = client.collection(types=["PL"], limit=50)
    for item in _items(payload, "collection"):
        if str(item.get("id") or "") == target:
            return str(item["id"])
        if str(item.get("name") or "").lower() == target.lower():
            return str(item["id"])
    for item in _items(payload, "collection"):
        if target.lower() in str(item.get("name") or "").lower():
            return str(item["id"])
    raise PandoraError(f"No playlist matching {target!r}. Use pandora_playlists action='list'.")


def _pl_ids(args: dict) -> list[str]:
    raw = args.get("item_ids") or args.get("track_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x) for x in raw]


def _pl_list(args: dict, action: str) -> str:
    client = PandoraClient()
    payload = client.collection(types=["PL"], limit=int(args.get("limit") or 50))
    items = [_compact(i) for i in _items(payload, "collection")]
    if not items:
        return _ok(action, count=0, items=[],
                   note="No playlists found. Playlists are a Premium feature (not included in Plus).")
    return _ok(action, count=len(items), items=items)


def _pl_create(args: dict, action: str) -> str:
    client = PandoraClient()
    name = str(_required(args.get("name"), "name is required."))
    ids = _pl_ids(args)
    try:
        data = client.create_playlist(name, description=args.get("description"),
                                      item_ids=ids or None)
    except Exception as exc:
        _playlist_guard(client, exc)
        raise
    node = data.get("createPlaylist") or {}
    playlist = node.get("playlist") or {}
    return _ok(action, status=node.get("status"), playlist_id=playlist.get("id"),
               name=playlist.get("name"), tracks=playlist.get("totalTracks"))


def _pl_add(args: dict, action: str) -> str:
    client = PandoraClient()
    target = str(_required(args.get("playlist") or args.get("playlist_id") or args.get("name"),
                           "playlist (name or id) is required."))
    ids = _pl_ids(args)
    if not ids:
        raise PandoraError("item_ids is required (e.g. ['TR:123'] from pandora_search).")
    pid = _resolve_playlist_id(client, target)
    try:
        data = client.add_to_playlist(pid, ids, index=args.get("index"))
    except Exception as exc:
        _playlist_guard(client, exc)
        raise
    node = data.get("addToPlaylist") or {}
    playlist = node.get("playlist") or {}
    return _ok(action, status=node.get("status"), playlist_id=playlist.get("id") or pid,
               name=playlist.get("name"), tracks=playlist.get("totalTracks"))


def _pl_update(args: dict, action: str) -> str:
    client = PandoraClient()
    target = str(_required(args.get("playlist") or args.get("playlist_id") or args.get("name"),
                           "playlist (name or id) is required."))
    pid = _resolve_playlist_id(client, target)
    name = args.get("new_name") or args.get("rename_to")
    ids = args.get("item_ids")
    if ids:
        ids = [str(x) for x in (ids if isinstance(ids, list) else [ids])]
    if action == "set_items":
        if not ids:
            raise PandoraError("item_ids is required: the FULL new item list "
                               "(replacement, not append).")
    elif not name:
        raise PandoraError("new_name is required for action='rename'.")
    try:
        data = client.update_playlist(pid, name=name, item_ids=ids)
    except Exception as exc:
        _playlist_guard(client, exc)
        raise
    node = data.get("updatePlaylist") or {}
    playlist = node.get("playlist") or {}
    return _ok(action, status=node.get("status"), playlist_id=playlist.get("id") or pid,
               name=playlist.get("name"), tracks=playlist.get("totalTracks"))


handle_pandora_playlists = _dispatcher("pandora_playlists", "list", {
    "list": _pl_list,
    "create": _pl_create,
    "add": _pl_add,
    "rename": _pl_update,
    "set_items": _pl_update,
})


# ── pandora_devices (Phase 3: Chromecast / Google Nest casting) ──────────────

def _devices_list(args: dict, action: str) -> str:
    from .pandora_cast import discover_devices, _normalize
    devices = discover_devices(timeout=10.0)
    out = []
    for d in devices:
        item = dict(d)
        item["is_output"] = bool(d["name"]) and (
            d["name"] == player.output or _normalize(d["name"]) == _normalize(player.output))
        out.append(item)
    return _ok(action, output=player.output, devices=out,
               hint="Switch with action='set' + device name (or 'local'), then play as usual via pandora_playback.")


def _devices_set(args: dict, action: str) -> str:
    device = args.get("device") or args.get("name")
    device = str(_required(device, "device is required for action='set' (e.g. 'Living Room Speaker', or 'local' for this PC)."))
    return _ok(action, **player.set_output(device))


def _devices_status(args: dict, action: str) -> str:
    from .pandora_cast import get_cast_output
    state = player.state()
    result = {
        "output": player.output,
        "playing": state.get("playing"),
        "paused": state.get("paused"),
        "track": state.get("track"),
        "artist": state.get("artist"),
        "position_seconds": state.get("position_seconds"),
    }
    if player.output != "local":
        cast = get_cast_output()
        result["cast"] = {
            "name": cast.name, "model": cast.model, "type": cast.device_type,
            "host": cast.host, "port": cast.port, "connected": cast.connected,
            "media": cast.status() if cast.connected else {},
        }
    return _ok(action, **result)


handle_pandora_devices = _dispatcher("pandora_devices", "list", {
    "list": _devices_list, "set": _devices_set, "status": _devices_status,
})


# ── schemas ─────────────────────────────────────────────────────────────────

def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema = {"name": name, "description": description,
              "parameters": {"type": "object", "properties": properties}}
    if required:
        schema["parameters"]["required"] = required
    return schema


def _enum(*values: str, description: str = "") -> dict:
    return {"type": "string", "enum": list(values), **({"description": description} if description else {})}


_STR = {"type": "string"}
_INT = {"type": "integer"}


PANDORA_PLAYBACK_SCHEMA = _schema(
    "pandora_playback",
    "Control Pandora playback on this machine (local mpv player): play a station/source, "
    "show now-playing, pause/resume, skip, restart the current track (previous), replay, volume, seek, stop. "
    "Tier note: on-demand (TR:/AL:/PL:) sources require Premium — on Free/Plus, play a "
    "station (ST:/SF:) instead, or create one via pandora_stations (see the pandora-usage skill).",
    {
        "action": _enum("play", "now_playing", "pause", "resume", "next", "previous", "replay",
                        "stop", "set_volume", "seek"),
        "source_id": {"type": "string",
                      "description": "Pandora source id (e.g. ST:0:123, SF:16722:1360). For action='play'."},
        "volume": {**_INT, "description": "0-100, for action='set_volume'"},
        "position_seconds": {"type": "number", "description": "Seek target, for action='seek'"},
    })

PANDORA_SEARCH_SCHEMA = _schema(
    "pandora_search",
    "Search Pandora for stations, tracks, albums, playlists, podcasts or artists. "
    "Returns ids to play with pandora_playback action='play'. Tier note: on Free/Plus "
    "accounts only ST/SF station ids are playable (TR on-demand needs Premium) — for a "
    "song/artist request, prefer a station, or create an artist station via pandora_stations.",
    {
        "query": {**_STR, "description": "Free-text search, e.g. 'Adele' or 'lo-fi beats'"},
        "types": {"type": "array", "items": {"type": "string", "enum": sorted(SEARCH_TYPE_LABEL)},
                  "description": "Restrict types (default: all). SF/ST = stations, TR = track, "
                                 "AL = album, PL = playlist, PC = podcast, ART = artist"},
        "limit": {**_INT, "description": "Max results (default 10, max 50)"},
    }, ["query"])

PANDORA_STATIONS_SCHEMA = _schema(
    "pandora_stations",
    "Work with the listener's Pandora stations: list them, play one by name or id, show recent, "
    "start the Shuffle/Thumbprint station, create a station from seeds, add/remove station seeds, "
    "or rename a station. Seed ids come from pandora_search (e.g. 'AR:269367'). "
    "This is the fallback for on-demand requests on Free/Plus accounts: 'play me Adele' → "
    "list (existing artist station?) → play, else create from the artist's AR: seed → play.",
    {
        "action": _enum("list", "play", "recent", "shuffle", "thumbprint",
                        "create", "add_seed", "remove_seed", "rename"),
        "station": {**_STR, "description": "Station name or id (play/add_seed/remove_seed/rename)"},
        "source_id": {**_STR, "description": "Exact station id for action='play'"},
        "seed_id": {**_STR, "description": "Seed id for create/add_seed/remove_seed, e.g. 'AR:269367'"},
        "seed_ids": {"type": "array", "items": _STR, "description": "Multiple seed ids for action='create'"},
        "name": {**_STR, "description": "New station name for action='create' (optional) or 'rename'"},
        "new_name": {**_STR, "description": "New station name for action='rename'"},
        "limit": {**_INT, "description": "Max stations/items to return"},
    })

PANDORA_FEEDBACK_SCHEMA = _schema(
    "pandora_feedback",
    "Give thumbs up (love), thumbs down (ban) or remove feedback on the currently playing Pandora track.",
    {"action": _enum("love", "ban", "undo")})

PANDORA_COLLECTION_SCHEMA = _schema(
    "pandora_collection",
    "List the listener's Pandora collection (stations, playlists, albums) or recent favorites, "
    "or add/remove favorites. 'favorite'/'unfavorite' take an id (e.g. SF:/ST:/TR:/AR:) or a name. "
    "On the free tier, stations can be removed but not re-added by ST id (use the SF id).",
    {
        "action": _enum("list", "recent_favorites", "favorite", "unfavorite"),
        "types": {"type": "array", "items": {"type": "string"},
                  "description": "Collection types for action='list' (default: ['ST'])"},
        "id": {**_STR, "description": "Entity id for favorite/unfavorite, e.g. 'SF:18245:802'"},
        "name": {**_STR, "description": "Entity name for favorite/unfavorite (resolved via search)"},
        "limit": _INT,
        "offset": _INT,
    })

PANDORA_PLAYLISTS_SCHEMA = _schema(
    "pandora_playlists",
    "Manage Pandora playlists (Premium feature; free accounts get a clear tier message): "
    "list, create, add items, rename, or replace the full item list.",
    {
        "action": _enum("list", "create", "add", "rename", "set_items"),
        "name": {**_STR, "description": "Playlist name (create) or lookup by name (add/rename/set_items)"},
        "playlist": {**_STR, "description": "Playlist name or id for add/rename/set_items"},
        "playlist_id": {**_STR, "description": "Exact playlist id, e.g. 'PL:123'"},
        "item_ids": {"type": "array", "items": _STR,
                     "description": "Item ids, e.g. ['TR:123'] from pandora_search. For set_items this is the FULL new list (replacement)."},
        "description": {**_STR, "description": "Playlist description (create)"},
        "new_name": {**_STR, "description": "New name for action='rename'"},
        "index": _INT,
        "limit": _INT,
    })


PANDORA_DEVICES_SCHEMA = _schema(
    "pandora_devices",
    "Phase 3: cast Pandora to Google Cast / Nest devices on the local network (Nest Audio, "
    "Nest Hub, or a Cast group — use the name shown in the Pandora/Google Home app, e.g. "
    "'Living Room Speaker', 'Kitchen Display', 'Speakers'). List discovered devices, switch "
    "the playback output to a device (or 'local' for this PC), or show current output status. "
    "After action='set', play/pause/skip/etc. work as usual via pandora_playback.",
    {
        "action": _enum("list", "set", "status"),
        "device": {**_STR, "description": "Device name for action='set' (e.g. 'Living Room Speaker') or 'local' for this PC."},
    })


TOOL_DEFS: list[tuple[dict, Callable]] = [
    (PANDORA_PLAYBACK_SCHEMA, handle_pandora_playback),
    (PANDORA_SEARCH_SCHEMA, _handle_pandora_search),
    (PANDORA_STATIONS_SCHEMA, handle_pandora_stations),
    (PANDORA_FEEDBACK_SCHEMA, handle_pandora_feedback),
    (PANDORA_COLLECTION_SCHEMA, _handle_pandora_collection),
    (PANDORA_PLAYLISTS_SCHEMA, handle_pandora_playlists),
    (PANDORA_DEVICES_SCHEMA, handle_pandora_devices),
]


def register_tools(ctx) -> None:
    for schema, handler in TOOL_DEFS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=_check_pandora, emoji="🎵")
