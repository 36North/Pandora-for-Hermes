"""Thin Pandora GraphQL client used by the Pandora tools.

Endpoint: ``{base}/api/v1/graphql/graphql`` — the same service the pandora.com
web player talks to. Auth headers: ``X-AuthToken`` (listener session token from
``pandora_auth.login``) and ``X-CsrfToken`` (the ``csrftoken`` cookie captured
at login).

Values are inlined into the query document (enums as bare tokens, strings as
JSON literals) instead of passed as GraphQL variables: the schema's variable
types are not documented, inline literals are what the published examples use,
and it keeps the client dependency-free.

Pandora occasionally rejects a projection field that a session cannot read
(e.g. ``id`` on a Track for some accounts). ``_run`` retries once with the
offending field stripped from the OPTIONAL set, so a slightly-wrong projection
degrades instead of failing the whole call.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

import httpx

from . import pandora_auth
from .pandora_auth import PandoraAuthError, PandoraAuthRequiredError

logger = logging.getLogger(__name__)

GRAPHQL_PATH = "/api/v1/graphql/graphql"
CLIENT_VERSION = "1.0"
USER_AGENT = pandora_auth.USER_AGENT
DEFAULT_TIMEOUT = 30.0

# Entity/id prefixes accepted for playback sources.
SOURCE_TYPES = {"ST", "SF", "TR", "AL", "PL", "PC", "PE", "AR", "AU"}

# Live-schema SearchableTypes enum (verified 2026-11-06 by introspection):
# AL, AR, PC, PE, PL, ST, SF, TR. "ART" is accepted as an input alias for AR.
SEARCH_TYPES = {
    "SF": "StationFactory", "ST": "Station", "TR": "Track", "AL": "Album",
    "PL": "Playlist", "PC": "Podcast", "PE": "PodcastEpisode", "AR": "Artist",
}
TYPE_ALIASES = {"ART": "AR"}

# IPlaybackItem = TrackItem | AudioAdItem. Spread ONLY onto `current`/`item`
# fields (typed IPlaybackItem) — never onto a mutation's response object.
ITEM_SELECTION = """
      __typename
      ... on TrackItem {
        index
        trackToken
        audioUrl
        interactions
        feedbackValue
        track {
          id
          name
          duration
          art { url(size: WIDTH_130) }
          artist { name }
        }
      }
      ... on AudioAdItem {
        index
        audioUrl
        interactions
      }
"""

# Response shape shared by every playback/feedback mutation
# (Started/Ended/Skip/Pause/Previous/Replay/SetFeedback...):
# status + current (IPlaybackItem) + source (IPlaybackSource).
RESPONSE_SELECTION = (
    "      status\n"
    "      current {{\n        {ITEM_SELECTION}\n      }}\n"
    "      source {{ id type }}\n").format(ITEM_SELECTION=ITEM_SELECTION)


class PandoraError(RuntimeError):
    """Base Pandora tool error."""


class PandoraAPIError(PandoraError):
    def __init__(self, message: str, *, code: str = "", status_code: Optional[int] = None,
                 errors: Optional[list] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.errors = errors or []


_CODE_RE = re.compile(r"\(([A-Z][A-Z0-9_]+)\)")


def _error_code(payload: dict) -> str:
    errors = payload.get("errors") or []
    for item in errors:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "")
        match = _CODE_RE.search(message)
        if match:
            return match.group(1)
        ext = item.get("extensions")
        if isinstance(ext, dict) and ext.get("code"):
            return str(ext["code"])
    return ""


def _error_message(payload: dict) -> str:
    errors = payload.get("errors") or []
    messages = [str(e.get("message")) for e in errors if isinstance(e, dict) and e.get("message")]
    return " | ".join(messages[:3]) or "Pandora API error"


def _lit(value: Any) -> str:
    """GraphQL literal for a Python value (enum names pass through as-is)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_lit(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _enum(value: Any) -> str:
    """GraphQL enum token, validated to avoid malformed/injected query text."""
    token = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", token):
        raise PandoraError(f"Invalid enum value: {value!r}")
    return token


def _enums(values) -> str:
    """GraphQL enum list literal with bare tokens: ``[SF, ST, TR]``.

    The search/collection ``types`` args are enum lists; quoted strings
    (``"SF"``) are rejected by the schema (Expected type SearchableTypes!).
    """
    return "[" + ", ".join(_enum(v) for v in values) + "]"


def _strip_selection(query: str, field: str) -> str:
    """Remove one projection entry from *query*, touching nothing else.

    Optional entries are emitted one per line (a bare field, or a whole
    ``... on Type { name }`` fragment), so removal is line-anchored and takes
    the shallowest match. A plain :meth:`str.replace` would also rewrite the
    field name inside longer identifiers — stripping ``id`` used to turn
    ``deviceUuid`` into ``deviceUu`` and corrupt the whole query.

    Returns *query* unchanged when the entry cannot be located; the caller
    treats that as "give up and surface the server error" instead of burning
    another request on an identical document.
    """
    want = field.strip()
    lines = query.splitlines(keepends=True)
    candidates: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == want or stripped.startswith(want + " {"):
            candidates.append((len(line) - len(line.lstrip()), index))
    if not candidates:
        return query
    _, start = min(candidates)
    # A block selection (``current {``) removes its whole balanced body; a
    # leaf line (``id``, ``... on Playlist { name }``) removes one line.
    depth = lines[start].count("{") - lines[start].count("}")
    end = start + 1
    while depth > 0 and end < len(lines):
        depth += lines[end].count("{") - lines[end].count("}")
        end += 1
    return "".join(lines[:start] + lines[end:])


def normalize_source_id(raw: str) -> str:
    """Accept ``ST:0:123``-style ids (and the bare station forms) unchanged."""
    value = str(raw or "").strip()
    if not value:
        raise PandoraError("A Pandora source id is required.")
    if ":" not in value:
        raise PandoraError(
            f"Expected a Pandora id like 'ST:0:123' or 'SF:16722:1360', got {value!r}. "
            "Use pandora_search first to find the id.")
    prefix = value.split(":", 1)[0].upper()
    if prefix not in SOURCE_TYPES:
        raise PandoraError(f"Unsupported Pandora source type {prefix!r} (id {value!r}).")
    return value


class PandoraClient:
    def __init__(self, *, state: Optional[dict[str, Any]] = None) -> None:
        self._state = state or pandora_auth.resolve_credentials()
        self.base_url = (self._state.get("base_url") or pandora_auth.base_url()).rstrip("/")
        self.device_uuid = str(self._state.get("device_uuid") or pandora_auth.device_uuid())
        self._client = self._new_http_client(self._state)

    # ── transport ──────────────────────────────────────────────────────────

    def _new_http_client(self, state: dict[str, Any]) -> httpx.Client:
        client = httpx.Client(http2=False, timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        for name, value in (state.get("cookies") or {}).items():
            try:
                client.cookies.set(name, value, domain=".pandora.com")
            except Exception:
                pass
        return client

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
            "apollographql-client-name": "web",
            "apollographql-client-version": CLIENT_VERSION,
        }
        token = str(self._state.get("auth_token") or "")
        csrf = str(self._state.get("csrf_token") or "")
        if token:
            headers["X-AuthToken"] = token
        if csrf:
            headers["X-CsrfToken"] = csrf
        return headers

    def _refresh_auth(self) -> None:
        self._state = pandora_auth.relogin()
        self._client.close()
        self._client = self._new_http_client(self._state)

    def _persist_cookies(self) -> None:
        """Write rotated ``Set-Cookie`` values back to the auth state.

        PerimeterX (``_pxhd``) and the session cookies rotate mid-session. The
        state on disk is only written at login, so a restart used to replay the
        login-time values and look like a rollback. Compare the live jar first:
        this runs after every request, and an auth.json write is only worth it
        when something actually changed.
        """
        try:
            jar = {c.name: c.value for c in self._client.cookies.jar}
        except Exception:
            return
        if not jar:
            return
        current = dict(self._state.get("cookies") or {})
        if jar == current:
            return
        self._state["cookies"] = jar
        csrf = jar.get("csrftoken")
        if csrf and csrf != self._state.get("csrf_token"):
            self._state["csrf_token"] = csrf
        token = jar.get("at")
        if token and token != self._state.get("auth_token"):
            self._state["auth_token"] = token
        try:
            pandora_auth.save_state(self._state)
            logger.debug("Pandora cookies rotated; auth state saved")
        except Exception as exc:  # never fail a request over bookkeeping
            logger.debug("Could not persist rotated Pandora cookies: %s", exc)

    def _run(self, query: str, *, optional: Iterable[str] = (), _retried: bool = False) -> dict[str, Any]:
        optional = list(optional)
        while True:
            try:
                response = self._client.post(
                    f"{self.base_url}{GRAPHQL_PATH}", headers=self._headers,
                    content=json.dumps({"query": query, "variables": {}, "operationName": None}))
            except httpx.HTTPError as exc:
                raise PandoraError(f"Could not reach Pandora: {type(exc).__name__}: {exc}") from exc

            self._persist_cookies()

            if response.status_code == 401:
                if _retried:
                    raise PandoraAuthRequiredError(
                        "Pandora rejected the session. Run `hermes pandora login` again.",
                        code="pandora_session_rejected", relogin_required=True)
                try:
                    self._refresh_auth()
                except PandoraAuthError as exc:
                    raise PandoraAuthRequiredError(str(exc), code=getattr(exc, "code", "pandora_auth")) from exc
                return self._run(query, optional=optional, _retried=True)

            try:
                payload = response.json()
            except Exception:
                raise PandoraAPIError(
                    f"Pandora returned a non-JSON response (HTTP {response.status_code}).",
                    status_code=response.status_code) from None

            if not isinstance(payload, dict):
                raise PandoraAPIError("Pandora returned an unexpected response.", status_code=response.status_code)

            if payload.get("errors"):
                code = _error_code(payload)
                message = _error_message(payload)
                # Strip an optional projection field the server rejected and retry
                # once per field. Only a field we can actually locate may be
                # retried — otherwise the same document is re-sent for nothing.
                if optional and not _retried:
                    retriable = False
                    for field in list(optional):
                        bare = field.split("(")[0].strip()
                        if re.search(rf"Cannot query field \"?{re.escape(bare)}\"?", message, re.I) or (
                                bare in message and "field" in message.lower()):
                            cleaned = _strip_selection(query, field)
                            if cleaned == query:
                                break
                            optional = [f for f in optional if f != field]
                            query = cleaned
                            retriable = True
                            logger.info("Pandora rejected field %s; retrying without it", bare)
                            break
                    if retriable:
                        continue
                raise PandoraAPIError(message, code=code, status_code=response.status_code,
                                      errors=payload.get("errors"))

            if response.status_code >= 400:
                raise PandoraAPIError(_error_message(payload), status_code=response.status_code)
            return payload.get("data") or {}

    def graphql(self, query: str) -> dict[str, Any]:
        return self._run(query)

    # ── queries ────────────────────────────────────────────────────────────

    def listener_template(self) -> dict[str, Any]:
        query = """
        query {
          listenerTemplate(template: DEFAULT) {
            collectionTypes
            searchTypes
          }
        }
        """
        return self._run(query)

    def search(self, text: str, *, types: Optional[list[str]] = None, limit: int = 10) -> dict[str, Any]:
        raw = [t.upper() for t in (types or ["SF", "ST", "TR", "AL", "PL", "PC", "PE", "AR"])]
        raw = [TYPE_ALIASES.get(t, t) for t in raw]
        selected = [t for t in raw if t in SEARCH_TYPES]
        if not selected:
            raise PandoraError("No supported search types requested.")
        limit = max(1, min(50, int(limit)))
        query = f"""
        query {{
          search(types: {_enums(selected)}, query: {_lit(text)}, pagination: {{ limit: {limit} }}) {{
            items {{
              __typename
              id
              ... on Station {{ name }}
              ... on StationFactory {{ name }}
              ... on Track {{ name duration art {{ url }} artist {{ name }} album {{ name }} }}
              ... on Album {{ name }}
              ... on Playlist {{ name }}
              ... on Artist {{ name art {{ url }} }}
            }}
          }}
        }}
        """
        return self._run(query)

    def collection(self, *, types: Optional[list[str]] = None, limit: int = 50,
                   offset: int = 0) -> dict[str, Any]:
        selected = [t.upper() for t in (types or ["ST"])]
        limit = max(1, min(50, int(limit)))  # Pandora rejects pagination limits over 50
        query = f"""
        query {{
          collection(types: {_enums(selected)}, pagination: {{ limit: {limit}, offset: {int(offset)} }}) {{
            items {{
              __typename
              id
              ... on Station {{ name }}
              ... on Playlist {{ name }}
              ... on Album {{ name }}
            }}
          }}
        }}
        """
        return self._run(query, optional=["__typename", "... on Playlist { name }", "... on Album { name }"])

    def recent_favorites(self, *, limit: int = 10) -> dict[str, Any]:
        query = f"""
        query {{
          recentFavorites(pagination: {{ limit: {max(1, min(20, int(limit)))} }}) {{
            items {{ __typename id ... on Station {{ name }} ... on Track {{ name }} ... on Album {{ name }} }}
          }}
        }}
        """
        return self._run(query)

    def entities(self, ids: list[str]) -> dict[str, Any]:
        query = f"""
        query {{
          entities(ids: {_lit(ids)}) {{
            __typename
            ... on StationFactory {{ id name }}
            ... on Station {{ id name }}
            ... on Track {{ id name }}
            ... on Album {{ id name }}
            ... on Artist {{ id name }}
          }}
        }}
        """
        return self._run(query)

    # ── playback ───────────────────────────────────────────────────────────

    def playback_set_source(self, source_id: str) -> dict[str, Any]:
        source = normalize_source_id(source_id)
        # setSource returns IPlaybackSource (id/type/current) — NOT a playback
        # item, so the TrackItem/AudioAdItem fragments can't be spread here.
        # The first item is reachable via the source's `current` field.
        query = f"""
        mutation {{
          playback {{
            setSource(deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(source)}) {{
              id
              type
              current {{
                {ITEM_SELECTION}
              }}
            }}
          }}
        }}
        """
        return self._run(query, optional=["id", "feedbackValue", "current"])

    def playback_current(self, source_id: Optional[str] = None) -> dict[str, Any]:
        extra = f", sourceId: {_lit(normalize_source_id(source_id))}" if source_id else ""
        query = f"""
        query {{
          playback {{
            current(deviceUuid: {_lit(self.device_uuid)}{extra}) {{
              {ITEM_SELECTION}
            }}
          }}
        }}
        """
        return self._run(query, optional=["feedbackValue"])

    def playback_peek(self, source_id: str) -> dict[str, Any]:
        query = f"""
        query {{
          playback {{
            peek(deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}) {{
              {ITEM_SELECTION}
            }}
          }}
        }}
        """
        return self._run(query, optional=["feedbackValue"])

    def _playback_mutation(self, name: str, args: str) -> dict[str, Any]:
        query = f"""
        mutation {{
          playback {{
            {name}({args}) {{
              {RESPONSE_SELECTION}
            }}
          }}
        }}
        """
        return self._run(query, optional=["feedbackValue"])

    def playback_skip(self, source_id: str, index: int, elapsed: int = 0, *,
                      check_only: bool = False) -> dict[str, Any]:
        return self._playback_mutation(
            "skip",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}, checkOnly: {_lit(check_only)}")

    def playback_pause(self, source_id: str, index: int, elapsed: int = 0) -> dict[str, Any]:
        return self._playback_mutation(
            "pause",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}")

    def playback_started(self, source_id: str, index: int, elapsed: int = 0) -> dict[str, Any]:
        return self._playback_mutation(
            "setStarted",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}")

    def playback_progress(self, source_id: str, index: int, elapsed: int) -> dict[str, Any]:
        return self._playback_mutation(
            "setProgress",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}")

    def playback_ended(self, source_id: str, index: int, elapsed: int,
                       reason: str = "NORMAL") -> dict[str, Any]:
        return self._playback_mutation(
            "setEnded",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}, reason: {_enum(reason)}")

    def playback_previous(self, source_id: str, index: int, elapsed: int = 0) -> dict[str, Any]:
        return self._playback_mutation(
            "previous",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}")

    def playback_replay(self, source_id: str, index: int, track_token: str,
                        elapsed: int = 0) -> dict[str, Any]:
        return self._playback_mutation(
            "replay",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"index: {int(index)}, elapsedTime: {int(elapsed)}, trackToken: {_lit(track_token)}")

    def playback_shuffle(self, source_id: str, enabled: bool) -> dict[str, Any]:
        return self._playback_mutation(
            "shuffle",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"enabled: {_lit(bool(enabled))}")

    def playback_repeat(self, source_id: str, mode: str) -> dict[str, Any]:
        return self._playback_mutation(
            "repeat",
            f"deviceUuid: {_lit(self.device_uuid)}, sourceId: {_lit(normalize_source_id(source_id))}, "
            f"repeatMode: {_enum(mode)}")

    # ── feedback ───────────────────────────────────────────────────────────

    def set_feedback(self, *, target_id: str, source_id: str, value: str, track_token: str,
                     elapsed: int = 0) -> dict[str, Any]:
        query = f"""
        mutation {{
          feedback {{
            setFeedback(
              targetId: {_lit(target_id)}
              sourceContextId: {_lit(normalize_source_id(source_id))}
              value: {_enum(value)}
              trackToken: {_lit(track_token)}
              deviceUuid: {_lit(self.device_uuid)}
              elapsedTime: {int(elapsed)}
            ) {{
              {RESPONSE_SELECTION}
            }}
          }}
        }}
        """
        return self._run(query, optional=["feedbackValue"])

    def remove_feedback(self, *, target_id: str, source_id: str, track_token: str,
                        elapsed: int = 0) -> dict[str, Any]:
        query = f"""
        mutation {{
          feedback {{
            removeFeedback(
              targetId: {_lit(target_id)}
              sourceContextId: {_lit(normalize_source_id(source_id))}
              trackToken: {_lit(track_token)}
              deviceUuid: {_lit(self.device_uuid)}
              elapsedTime: {int(elapsed)}
            ) {{ status }}
          }}
        }}
        """
        return self._run(query)

    def feedbacks(self, *, value: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        value_filter = f", values: [{_enum(value)}]" if value else ""
        query = f"""
        query {{
          feedbacks(pagination: {{ limit: {max(1, min(50, int(limit)))} }},
                    filter: {{ targetTypes: [TR], sourceContextTypes: [ST]{value_filter} }}) {{
            items {{
              __typename
              ... on Track {{ id name }}
            }}
          }}
        }}
        """
        return self._run(query)

    # ── editing: stations, playlists, collection ───────────────────────────
    # Live-verified mutation surface (introspected 2026-11-06):
    # createStationFromSeeds(seedIds!, stationName) -> {status, station}
    # addStationSeed(stationId!, seedId!) / removeStationSeed(...) -> {status}
    # setStationDetails(stationId!, name!, description!, enableArtistMessages)
    #     -> {status, station}
    # createPlaylist(name!, description, itemIds) /
    # updatePlaylist(pandoraId, name, isPrivate, itemIds) /
    # addToPlaylist(playlistId!, ids!, index) -> {status, playlist}
    # collect(ids!) / uncollect(ids!) -> {status, message}
    # Playlists are Premium-tier: on Free these return Pandora's own
    # restriction errors, which the tools surface verbatim.

    def create_station_from_seeds(self, seed_ids, *, station_name: Optional[str] = None,
                                  view_mode: Optional[str] = None,
                                  page_view: Optional[str] = None) -> dict[str, Any]:
        if not seed_ids:
            raise PandoraError("At least one seed id is required (e.g. 'AR:269367').")
        extra = ""
        if station_name:
            extra += f", stationName: {_lit(station_name)}"
        if view_mode:
            extra += f", viewMode: {_lit(view_mode)}"
        if page_view:
            extra += f", pageView: {_lit(page_view)}"
        query = f"""
        mutation {{
          createStationFromSeeds(seedIds: {_lit(list(seed_ids))}{extra}) {{
            status
            station {{ id name description }}
          }}
        }}
        """
        return self._run(query, optional=["station"])

    def add_station_seed(self, station_id: str, seed_id: str) -> dict[str, Any]:
        query = f"""
        mutation {{
          addStationSeed(stationId: {_lit(station_id)}, seedId: {_lit(seed_id)}) {{ status }}
        }}
        """
        return self._run(query)

    def remove_station_seed(self, station_id: str, seed_id: str) -> dict[str, Any]:
        query = f"""
        mutation {{
          removeStationSeed(stationId: {_lit(station_id)}, seedId: {_lit(seed_id)}) {{ status }}
        }}
        """
        return self._run(query)

    def set_station_details(self, station_id: str, *, name: str, description: str = "",
                            enable_artist_messages: Optional[bool] = None) -> dict[str, Any]:
        extra = ""
        if enable_artist_messages is not None:
            extra = f", enableArtistMessages: {_lit(enable_artist_messages)}"
        query = f"""
        mutation {{
          setStationDetails(
            stationId: {_lit(station_id)}
            name: {_lit(name)}
            description: {_lit(description)}{extra}
          ) {{
            status
            station {{ id name }}
          }}
        }}
        """
        return self._run(query, optional=["station"])

    def create_playlist(self, name: str, *, description: Optional[str] = None,
                        item_ids=None, is_private: Optional[bool] = None) -> dict[str, Any]:
        extra = ""
        if description:
            extra += f", description: {_lit(description)}"
        if is_private is not None:
            extra += f", isPrivate: {_lit(is_private)}"
        if item_ids:
            extra += f", itemIds: {_lit(list(item_ids))}"
        query = f"""
        mutation {{
          createPlaylist(name: {_lit(name)}{extra}) {{
            status
            playlist {{ id name isPrivate totalTracks }}
          }}
        }}
        """
        return self._run(query, optional=["playlist"])

    def update_playlist(self, playlist_id: str, *, name: Optional[str] = None,
                        is_private: Optional[bool] = None,
                        item_ids: Optional[list] = None) -> dict[str, Any]:
        args = [f"pandoraId: {_lit(playlist_id)}"]
        if name is not None:
            args.append(f"name: {_lit(name)}")
        if is_private is not None:
            args.append(f"isPrivate: {_lit(is_private)}")
        if item_ids is not None:
            args.append(f"itemIds: {_lit(list(item_ids))}")
        query = f"""
        mutation {{
          updatePlaylist({", ".join(args)}) {{
            status
            playlist {{ id name isPrivate totalTracks }}
          }}
        }}
        """
        return self._run(query, optional=["playlist"])

    def add_to_playlist(self, playlist_id: str, item_ids, *, index: Optional[int] = None) -> dict[str, Any]:
        if not item_ids:
            raise PandoraError("At least one item id is required (e.g. 'TR:123').")
        extra = f", index: {int(index)}" if index is not None else ""
        query = f"""
        mutation {{
          addToPlaylist(playlistId: {_lit(playlist_id)}, ids: {_lit(list(item_ids))}{extra}) {{
            status
            playlist {{ id name totalTracks }}
          }}
        }}
        """
        return self._run(query, optional=["playlist"])

    def collect(self, ids) -> dict[str, Any]:
        if not ids:
            raise PandoraError("At least one id is required to collect.")
        query = f"""
        mutation {{
          collect(ids: {_lit(list(ids))}) {{ status message }}
        }}
        """
        return self._run(query)

    def uncollect(self, ids) -> dict[str, Any]:
        if not ids:
            raise PandoraError("At least one id is required to uncollect.")
        query = f"""
        mutation {{
          uncollect(ids: {_lit(list(ids))}) {{ status message }}
        }}
        """
        return self._run(query)
