"""Chromecast / Google Nest casting for the Pandora plugin (Phase 3, option D).

Streams Pandora audio URLs to any Cast device on the local network: Nest
Audio, Nest Hub (audio receiver), or a Google Cast Group. Uses the standard
Cast media receiver, so any Cast-capable speaker works.

Discovery is mDNS-based (pychromecast/zeroconf). Devices are matched by the
friendly name the user sees in the Pandora / Google Home apps (case- and
whitespace-insensitive).
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
import time
import uuid as uuidlib
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import pychromecast
    from pychromecast import Chromecast, STREAM_TYPE_BUFFERED
    _CAST_AVAILABLE = True
    _CAST_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - depends on env
    Chromecast = None
    STREAM_TYPE_BUFFERED = "buffered"
    _CAST_AVAILABLE = False
    _CAST_IMPORT_ERROR = str(exc)


class CastError(RuntimeError):
    """Casting failed."""


class CastUnavailable(CastError):
    """pychromecast is not installed."""


def _cc_connected(cc: Any) -> bool:
    """Connection flag across pychromecast versions (v14 moved it to socket_client)."""
    sc = getattr(cc, "socket_client", None)
    if sc is not None and hasattr(sc, "is_connected"):
        return bool(sc.is_connected)
    return bool(getattr(cc, "is_connected", False))


def _cc_close(cc: Any) -> None:
    for name in ("disconnect", "close"):
        fn = getattr(cc, name, None)
        if callable(fn):
            try:
                fn()
            except TypeError:
                try:
                    fn(block=False)
                except Exception:
                    logger.debug("cast close(%s) failed", name, exc_info=True)
            except Exception:
                logger.debug("cast close(%s) failed", name, exc_info=True)
            return


def _require_cast() -> None:
    if not _CAST_AVAILABLE:
        raise CastUnavailable(
            "Chromecast support needs the 'pychromecast' package in the Hermes "
            "environment (declared in pyproject.toml; reinstalling the plugin "
            f"fetches it). Import error was: {_CAST_IMPORT_ERROR!r}")


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _stop_browser(browser: Any) -> None:
    """Stop a discovery browser and close its Zeroconf instance.

    ``discover_chromecasts`` hands the caller a live browser; dropping the
    reference leaves its multicast listener thread and socket running, so every
    scan would add one. Callers own it and must stop it.
    """
    try:
        stop = getattr(browser, "stop_discovery", None)
        if callable(stop):
            stop()
    except Exception:
        logger.debug("browser stop_discovery failed", exc_info=True)
    zc = getattr(browser, "zc", None) or getattr(browser, "zeroconf", None)
    if zc is not None:
        try:
            zc.close()
        except Exception:
            logger.debug("zeroconf close failed", exc_info=True)


def discover_devices(timeout: float = 10.0) -> list[dict[str, Any]]:
    """List Cast devices on the local network.

    Returns dicts: ``{name, model, type: audio|cast|group, host, port, uuid}``.
    ``type`` comes from the Cast service: ``audio`` = dedicated speaker
    (Nest Audio), ``cast`` = display device (Nest Hub), ``group`` = Cast group.
    """
    _require_cast()
    from pychromecast.discovery import discover_chromecasts
    try:
        devices, browser = discover_chromecasts(timeout=timeout)
    except Exception as exc:
        raise CastError(f"Cast discovery failed: {exc}") from exc
    try:
        out = [_device_dict(d) for d in devices]
        out.sort(key=lambda x: x["name"].lower())
        return out
    finally:
        _stop_browser(browser)


# ── live device browser (persistent, idle-stopped) ──────────────────────────
#
# A one-shot scan holds Zeroconf open for its whole timeout and returns nothing
# until it expires. A CastBrowser instead listens to the mDNS announcements the
# devices already broadcast, so a listing is a dict read. It starts lazily on
# the first listing and is stopped after BROWSER_IDLE_SECONDS without one, so an
# idle Hermes holds no multicast socket at all.

BROWSER_IDLE_SECONDS = 600.0      # drop the listener after 10 min without a listing
BROWSER_SETTLE_SECONDS = 2.0      # first listing: collect the initial mDNS burst
BROWSER_REAP_INTERVAL = 30.0


def _device_dict(d: Any) -> dict[str, Any]:
    return {
        "name": str(d.friendly_name or d.model_name or "Chromecast"),
        "model": str(d.model_name or ""),
        "type": str(getattr(d, "cast_type", "") or ""),
        "host": str(d.host or ""),
        "port": int(d.port or 8009),
        "uuid": str(getattr(d, "uuid", "") or ""),
    }


def _as_uuid(value: Any) -> Any:
    """pychromecast wants a UUID object; the listing carries the string form."""
    raw = str(value or "").strip()
    if raw:
        try:
            return uuidlib.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            pass
    return uuidlib.uuid4()


class _DeviceBrowser:
    """One CastBrowser, started on demand and reaped once it goes unused."""

    def __init__(self, *, idle_seconds: float = BROWSER_IDLE_SECONDS) -> None:
        self._lock = threading.Lock()
        self._idle_seconds = float(idle_seconds)
        self._browser = None
        self._zc = None
        self._last_used = 0.0
        self._reaper: Optional[threading.Thread] = None

    def _start_locked(self) -> None:
        import zeroconf
        from pychromecast.discovery import CastBrowser, SimpleCastListener

        self._zc = zeroconf.Zeroconf()
        try:
            self._browser = CastBrowser(SimpleCastListener(), self._zc)
            self._browser.start_discovery()
        except Exception:
            self._stop_locked()  # never leave a half-started listener behind
            raise
        self._last_used = time.monotonic()
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(target=self._reap, name="pandora-cast-reaper",
                                            daemon=True)
            self._reaper.start()

    def _stop_locked(self) -> None:
        browser, zc = self._browser, self._zc
        self._browser = self._zc = None
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:
                logger.debug("browser stop_discovery failed", exc_info=True)
        if zc is not None:
            try:
                zc.close()
            except Exception:
                logger.debug("zeroconf close failed", exc_info=True)

    def _maybe_stop_idle(self, now: Optional[float] = None) -> bool:
        """Stop the browser once it has gone ``idle_seconds`` without a listing."""
        with self._lock:
            if self._browser is None:
                return False
            if (now if now is not None else time.monotonic()) - self._last_used < self._idle_seconds:
                return False
            logger.info("cast browser idle for %.0fs — closing it", self._idle_seconds)
            self._stop_locked()
            return True

    def _reap(self) -> None:
        while True:
            time.sleep(BROWSER_REAP_INTERVAL)
            if self._browser is None:
                return
            self._maybe_stop_idle()

    def zeroconf(self) -> Any:
        """The live browser's Zeroconf instance, or None when it is not running."""
        with self._lock:
            return self._zc

    def cast_info_for(self, dev: dict[str, Any]) -> Any:
        """The live browser's CastInfo for ``dev`` (matched by uuid, then host)."""
        with self._lock:
            browser = self._browser
            if browser is None:
                return None
            infos = list((getattr(browser, "devices", None) or {}).values())
        wanted_uuid = str(dev.get("uuid") or "")
        if wanted_uuid:
            for info in infos:
                if str(getattr(info, "uuid", "")) == wanted_uuid:
                    return info
        wanted_host = str(dev.get("host") or "")
        if wanted_host:
            for info in infos:
                if str(getattr(info, "host", "")) == wanted_host:
                    return info
        return None

    def snapshot(self, settle: float = BROWSER_SETTLE_SECONDS) -> list[dict[str, Any]]:
        """Devices currently on the LAN; starts the browser when it is not running."""
        with self._lock:
            fresh = self._browser is None
            if fresh:
                self._start_locked()
            self._last_used = time.monotonic()
            browser = self._browser
        if fresh and settle > 0 and browser is not None:
            deadline = time.monotonic() + settle
            while time.monotonic() < deadline:
                if getattr(browser, "devices", None):
                    break
                time.sleep(0.05)
        with self._lock:
            browser = self._browser
            if browser is None:
                return []
            devices = list((getattr(browser, "devices", None) or {}).values())
        out = [_device_dict(d) for d in devices]
        out.sort(key=lambda item: item["name"].lower())
        return out

    def close(self) -> None:
        with self._lock:
            self._stop_locked()


_device_browser = _DeviceBrowser()
atexit.register(_device_browser.close)


def list_devices(settle: float = BROWSER_SETTLE_SECONDS) -> list[dict[str, Any]]:
    """Cast devices on the LAN from the live browser (starts it on demand)."""
    _require_cast()
    try:
        return _device_browser.snapshot(settle=settle)
    except Exception as exc:
        raise CastError(f"Cast discovery failed: {exc}") from exc


def _pick_device(devices: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """Fuzzy-match a device by name (case/whitespace/punctuation-insensitive)."""
    wanted = _normalize(target)
    if not wanted:
        raise CastError("Empty device name.")
    for dev in devices:  # exact normalized
        if _normalize(dev["name"]) == wanted:
            return dev
    for dev in devices:  # substring either way
        if wanted in _normalize(dev["name"]) or _normalize(dev["name"]) in wanted:
            return dev
    names = ", ".join(d["name"] for d in devices) or "(none discovered)"
    raise CastError(f"No Cast device matching {target!r}. Discovered: {names}")


def _content_type_for_url(url: str) -> str:
    """Pandora CDN URLs don't carry a reliable extension — ask the server."""
    ext = str(url.rsplit("?", 1)[0].rsplit(".", 1)[-1]).lower()
    fallback = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
                "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg"}.get(ext, "audio/mpeg")
    try:
        import httpx
        head = httpx.head(url, timeout=5.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        ctype = str(head.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype.startswith("audio/") or ctype.startswith("video/"):
            return ctype
    except Exception:
        pass
    return fallback


class CastOutput:
    """A connected Cast device used as a Pandora playback output.

    Usage::

        out = CastOutput()
        out.connect("Living Room Speaker")     # or "Kitchen Display", "Speakers"
        out.load(url, title="...", artist="...", duration=212.0)
        out.play() / out.pause() / out.stop() / out.set_volume(0.5)
        st = out.status()   # {player_state, current_time, play_duration, ...}
        out.close()
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cc: Optional[Any] = None
        self._media: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._last_status: Optional[Any] = None  # last pushed media status (live position)
        self._last_duration: float = 0.0  # duration of the loaded track (for clamping)
        # Devices only push on state transitions (no playback heartbeats), so the
        # pushed current_time is a snapshot. Extrapolate locally while PLAYING.
        self._pos_base_time: Optional[float] = None
        self._pos_base_current: float = 0.0
        self._commanded_volume: Optional[float] = None  # last volume we set (read-back fallback)
        self.name: str = ""
        self.model: str = ""
        self.device_type: str = ""
        self.host: str = ""
        self.port: int = 0

    # ── connection ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        if self._cc is None:
            return False
        return _cc_connected(self._cc)

    def connect(self, target: str, *, timeout: float = 15.0,
                device_list: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Connect to a device by name (or 'local' is handled by the caller)."""
        _require_cast()
        with self._lock:
            dev = None
            if ":" in target:  # explicit host[:port]
                host, _, port = target.partition(":")
                port = int(port) if port else 8009
                dev = {"name": target, "model": "", "type": "",
                       "host": host, "port": port, "uuid": ""}
            else:
                devices = (device_list or list_devices(settle=1.0)
                           or discover_devices(timeout=5.0))
                if not devices:
                    raise CastError(
                        "No Cast devices found on this network. Is the speaker powered on "
                        "and on the same Wi-Fi network as this PC?")
                dev = _pick_device(devices, target)
            info = self._dial(dev, timeout=timeout)
            self.name, self.model = str(info["name"]), str(info.get("model", ""))
            self.device_type = str(info.get("type", ""))
            self.host = str(info["host"])
            self.port = int(info.get("port", 8009))
            self._media = self._cc.media_controller
            self._last_status = None
            # v14 doesn't refresh media_controller.status on each push — capture the
            # pushed statuses so status() can report live position. v14's listener
            # protocol is object-based (listener.new_media_status(st)), so register
            # `self`.
            try:
                self._media.register_status_listener(self)
            except Exception:
                logger.debug("could not register cast status capture", exc_info=True)
            logger.info("Cast connected: %s (%s %s @ %s:%s)",
                        self.name, self.model, self.device_type, self.host, self.port)
            return {"name": self.name, "model": self.model, "type": self.device_type,
                    "host": self.host, "port": self.port}

    def _dial(self, dev: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Connect straight to the device we already know about.

        mDNS is only how a device is *found*; the Cast protocol is a TCP socket
        on the device's port. The listing already carries host/port/uuid and the
        live browser carries the full CastInfo, so re-running discovery here only
        added its timeout to every cast.
        """
        from pychromecast.models import CastInfo

        started = time.time()
        cast_info = _device_browser.cast_info_for(dev)
        source = "browser" if cast_info is not None else "cached"
        if cast_info is None:
            cast_info = CastInfo(
                services=set(),
                uuid=_as_uuid(dev.get("uuid")),
                model_name=str(dev.get("model") or "Chromecast"),
                friendly_name=str(dev.get("name") or "Chromecast"),
                host=str(dev["host"]),
                port=int(dev.get("port") or 8009),
                cast_type=(str(dev.get("type") or "") or None),
                manufacturer=None,
            )
        try:
            cc = Chromecast(cast_info, timeout=timeout, zconf=_device_browser.zeroconf())
            self._cc = cc
            try:
                cc.wait(timeout=timeout)  # returns None on some versions; raises RequestTimeout on others
            except Exception:
                pass
            # v14's wait() is not a reliable connection flag — poll it.
            deadline = time.time() + max(3.0, float(timeout))
            while time.time() < deadline and not _cc_connected(cc):
                time.sleep(0.2)
            if not _cc_connected(cc):
                raise CastError(f"Could not connect to {dev['name'] or dev['host']} "
                                f"(port {dev.get('port')}). Is it on and reachable?")
        except Exception as exc:
            if self._cc is not None:
                try:
                    _cc_close(self._cc)
                except Exception:
                    pass
                self._cc = None
            logger.warning("direct cast dial failed (%s info): %s; retrying via discovery",
                           source, exc)
            return self._dial_via_discovery(dev, timeout=timeout)
        logger.info("cast dial (%s info): %.2fs", source, time.time() - started)
        from .pandora_diag import write as _diag

        _diag("dial", source=source, seconds=round(time.time() - started, 2),
              device=cast_info.friendly_name, host=cast_info.host)
        return {"name": cast_info.friendly_name or dev.get("name", ""),
                "model": cast_info.model_name or "", "type": getattr(cast_info, "cast_type", ""),
                "host": cast_info.host or str(dev["host"]), "port": int(cast_info.port or 8009)}

    def _dial_via_discovery(self, dev: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Fallback: discover this host, then connect with what the scan returned."""
        from pychromecast.discovery import discover_chromecasts

        started = time.time()
        cast_infos, browser = discover_chromecasts(timeout=timeout, known_hosts=[dev["host"]])
        self._browser = browser
        cast_info = None
        wanted_uuid = dev.get("uuid") or ""
        for ci in cast_infos:
            if wanted_uuid and str(getattr(ci, "uuid", "")) == wanted_uuid:
                cast_info = ci
                break
        if cast_info is None:
            for ci in cast_infos:
                if str(ci.host or "") == str(dev["host"]):
                    cast_info = ci
                    break
        if cast_info is None:
            from pychromecast.models import CastInfo
            cast_info = CastInfo(
                services=set(),
                uuid=_as_uuid(wanted_uuid),
                model_name=str(dev.get("model") or "Chromecast"),
                friendly_name=str(dev.get("name") or "Chromecast"),
                host=str(dev["host"]),
                port=int(dev.get("port") or 8009),
                cast_type=(str(dev.get("type") or "") or None),
                manufacturer=None,
            )
        cc = Chromecast(cast_info, zconf=getattr(browser, "zc", None) or getattr(browser, "zeroconf", None))
        self._cc = cc
        try:
            cc.wait(timeout=timeout)  # returns None on some versions; raises RequestTimeout on others
        except Exception:
            pass
        # v14's wait() is not a reliable connection flag — poll it.
        deadline = time.time() + max(3.0, float(timeout))
        while time.time() < deadline and not _cc_connected(cc):
            time.sleep(0.2)
        if not _cc_connected(cc):
            raise CastError(f"Could not connect to {dev['name'] or dev['host']} "
                            f"(port {dev.get('port')}). Is it on and reachable?")
        logger.info("cast dial (discovery fallback): %.2fs", time.time() - started)
        from .pandora_diag import write as _diag

        _diag("dial", source="discovery", seconds=round(time.time() - started, 2),
              device=cast_info.friendly_name, host=cast_info.host)
        return {"name": cast_info.friendly_name or dev.get("name", ""),
                "model": cast_info.model_name or "", "type": getattr(cast_info, "cast_type", ""),
                "host": cast_info.host or str(dev["host"]), "port": int(cast_info.port or 8009)}

    # ── media control ─────────────────────────────────────────────────────

    def _media_or_raise(self) -> Any:
        if self._media is None or self._cc is None or not self.connected:
            raise CastError("No Cast device connected. Call connect() first.")
        return self._media

    def load(self, url: str, *, title: str = "", artist: str = "",
             duration: float = 0.0, volume: Optional[float] = None,
             art_url: str = "", position: float = 0.0) -> dict[str, Any]:
        media = self._media_or_raise()
        ctype = _content_type_for_url(url)
        label = f"{title} — {artist}".strip(" —") if (title or artist) else (title or None)
        if volume is not None:
            cc = self._cc
            frac = max(0.0, min(1.0, float(volume)))
            self._commanded_volume = frac
            try:
                cc.volume_controller.set_volume(frac)
            except Exception:
                logger.debug("cast set_volume failed", exc_info=True)
        self._last_duration = float(duration or 0.0)
        # metadataType 3 = MusicTrackMediaMetadata. The default receiver shows
        # the artwork when images are present; branding still needs a custom
        # receiver app.
        metadata: dict[str, Any] = {
            "metadataType": 3,
            "title": title or None,
            "artist": artist or None,
            "albumName": "Pandora",
        }
        if art_url:
            metadata["images"] = [{"url": art_url}]
        # pychromecast >= 14: play_media(); older releases: load().
        if hasattr(media, "play_media"):
            media.play_media(url, ctype, title=label, stream_type=STREAM_TYPE_BUFFERED,
                             autoplay=True, metadata=metadata, thumb=art_url or None,
                             current_time=float(position) if position and position > 0.5 else None)
        else:  # pragma: no cover - pychromecast < 14
            metadata = None
            try:
                from pychromecast import MediaMetadata as _MM
                metadata = _MM()
                metadata.title = title or None
                if artist:
                    metadata.artist = artist
            except ImportError:
                metadata = None
            media.load(url, content_type=ctype, stream_type=STREAM_TYPE_BUFFERED,
                       title=title or None, metadata=metadata)
        status = media.status
        return {"player_state": getattr(status, "player_state", ""),
                "current_time": float(getattr(status, "current_time", 0) or 0),
                "content_type": ctype}

    def play(self) -> dict[str, Any]:
        self._media_or_raise().play()
        return self.status()

    def pause(self) -> dict[str, Any]:
        self._media_or_raise().pause()
        return self.status()

    def stop(self) -> dict[str, Any]:
        media = self._media_or_raise()
        try:
            media.stop()
        except Exception:
            logger.debug("cast stop failed", exc_info=True)
        return self.status()

    def seek(self, seconds: float) -> dict[str, Any]:
        self._media_or_raise().seek(float(seconds))
        return self.status()

    def set_volume(self, fraction: float) -> dict[str, Any]:
        cc = self._cc
        if cc is None:
            raise CastError("No Cast device connected.")
        frac = max(0.0, min(1.0, float(fraction)))
        self._commanded_volume = frac
        cc.volume_controller.set_volume(frac)
        return self.status()

    def status(self) -> dict[str, Any]:
        if self._media is None or self._cc is None or not self.connected:
            return {"connected": False, "player_state": "", "current_time": 0.0,
                    "play_duration": 0.0, "track": "", "volume": None}
        st = self._last_status if self._last_status is not None else self._media.status
        if st is None:
            return {"connected": True, "player_state": "", "current_time": 0.0,
                    "play_duration": 0.0, "track": "", "volume": None}
        # Volume: prefer the receiver status (v14's volume_controller.volume can
        # report 0.0 even when the device volume is set); fall back to the
        # volume controller.
        vol = None
        rs = getattr(self._cc, "status", None)
        try:
            rv = getattr(rs, "volume", None)
            if rv is not None:
                vol = float(rv)
        except Exception:
            pass
        if vol is None:
            try:
                vol = float(self._cc.volume_controller.volume)
            except Exception:
                vol = None
        if vol is None:
            vol = self._commanded_volume
        return {
            "connected": True,
            "player_state": str(getattr(st, "player_state", "") or ""),
            "current_time": self._live_current_time,
            "play_duration": float(getattr(st, "play_duration", 0) or 0),
            "track": str((getattr(st, "metadata", None) and getattr(st.metadata, "title", "")) or ""),
            "volume": vol,
        }

    def new_media_status(self, status: Any) -> None:
        """Internal status listener (v14 calls listener.new_media_status(st)):
        keep the latest pushed media status for live position reads in status()."""
        self._last_status = status
        state = str(getattr(status, "player_state", "") or "")
        if state in ("PLAYING", "BUFFERING"):
            # Re-baseline the local position clock on every push so any drift or
            # seek the device reports gets picked up.
            self._pos_base_time = time.time()
            try:
                self._pos_base_current = float(getattr(status, "current_time", 0.0) or 0.0)
            except (TypeError, ValueError):
                self._pos_base_current = 0.0

    @property
    def _live_current_time(self) -> float:
        """current_time from the last push, extrapolated forward while playing."""
        st = self._last_status
        if st is None:
            return 0.0
        state = str(getattr(st, "player_state", "") or "")
        try:
            base = float(getattr(st, "current_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            base = 0.0
        if state == "PLAYING" and self._pos_base_time is not None:
            pos = self._pos_base_current + (time.time() - self._pos_base_time)
            if self._last_duration > 0:
                pos = min(pos, self._last_duration)
            return max(0.0, pos)
        return max(0.0, base)

    def on_status(self, listener: Any) -> None:
        """Subscribe to media-status pushes (listener needs new_media_status(st))."""
        media = self._media_or_raise()
        if hasattr(media, "register_status_listener"):
            media.register_status_listener(listener)
        else:  # pragma: no cover
            raise CastError("This pychromecast version has no status listeners.")

    # ── teardown ──────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            if self._cc is not None:
                _cc_close(self._cc)
                self._cc = None
                self._media = None
                self._last_status = None
            if self._browser is not None:
                _stop_browser(self._browser)
                self._browser = None
            self.name = self.model = self.device_type = self.host = ""
            self.port = 0


_cast_output: Optional[CastOutput] = None
_cast_lock = threading.Lock()


def get_cast_output() -> CastOutput:
    """Process-wide CastOutput (one Cast device at a time)."""
    global _cast_output
    with _cast_lock:
        if _cast_output is None:
            _cast_output = CastOutput()
        return _cast_output


def reset_cast_output() -> None:
    global _cast_output
    with _cast_lock:
        if _cast_output is not None:
            _cast_output.close()
        _cast_output = None
