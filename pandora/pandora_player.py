"""Local playback for the Pandora plugin — one long-lived mpv process per host.

mpv is driven over its JSON IPC socket (a Windows named pipe). The player owns
the queue position for our Pandora "device": when a track ends it reports
``setEnded`` to Pandora and plays the next item the API hands back, which is
what makes skip limits, station personalization and free-tier audio ads behave
the way they do in the official clients.

If mpv is missing the player raises ``PlayerUnavailable`` with install
instructions; everything else in the plugin (search, stations, feedback) keeps
working without it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .pandora_winjob import KillOnCloseJob

logger = logging.getLogger(__name__)

IPC_PIPE_NAME = "hermes_pandora"
DEFAULT_VOLUME = 70
PROGRESS_INTERVAL_SECONDS = 60
# A wedged mpv (or a stale handle on the pipe) must never hang a caller silently.
WRITE_DEADLINE_SECONDS = 5.0


class PlayerError(RuntimeError):
    """Playback failed."""


class PlayerUnavailable(PlayerError):
    """mpv is not installed / could not be started."""


# ── mpv discovery ───────────────────────────────────────────────────────────

def _mpv_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("PANDORA_MPV_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    found = shutil.which("mpv")
    if found:
        candidates.append(Path(found))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "mpv" / "mpv.exe")
        candidates.append(Path(local) / "Microsoft" / "WinGet" / "Packages" / "shinchiro.mpv" / "mpv.exe")
    candidates.append(Path(__file__).resolve().parent / "bin" / "mpv.exe")
    try:
        from .pandora_mpv import mpv_path as _downloaded_mpv
        downloaded = _downloaded_mpv()
        if downloaded is not None:
            candidates.insert(0, downloaded)
    except Exception:
        pass
    if sys.platform != "win32":
        candidates.append(Path("/usr/bin/mpv"))
        candidates.append(Path("/usr/local/bin/mpv"))
    return candidates


def find_mpv() -> Optional[str]:
    for candidate in _mpv_candidates():
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _ipc_path() -> str:
    # Per-process name. Windows named pipes are per-name singletons, so a
    # handle left behind by a killed process keeps the old name wedged: the
    # next run can open it but never gets a reply. A fresh name per process
    # sidesteps that class of hang entirely.
    name = f"{IPC_PIPE_NAME}_{os.getpid()}"
    if sys.platform == "win32":
        return rf"\\.\pipe\{name}"
    import tempfile
    return str(Path(tempfile.gettempdir()) / f"{name}.sock")


def _set_pipe_nonblocking(fd: int) -> None:
    """Windows named pipes must be non-blocking: a blocking read holds the handle.

    Some interpreter builds (notably the repo's old Python 3.11 venv shim) have
    no ``os.set_blocking`` at all. Silently continuing there puts the reader on a
    blocking pipe and wedges the next write, so refuse up front instead.
    """
    set_blocking = getattr(os, "set_blocking", None)
    if set_blocking is None:
        raise PlayerUnavailable(
            "This Python build has no os.set_blocking(); the mpv IPC pipe cannot be used "
            "safely. Run the plugin under Hermes' bundled runtime Python (3.12+).")
    set_blocking(fd, False)


# ── player ──────────────────────────────────────────────────────────────────

class PandoraPlayer:
    """Singleton playback session (module-level instance ``player``)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._fd: Optional[int] = None
        self._reader: Optional[threading.Thread] = None
        self._progress_thread: Optional[threading.Thread] = None
        self._stop_progress = threading.Event()
        self._client = None

        self.source_id: str = ""
        self.source_type: str = ""
        self.index: int = 0
        self.item: dict[str, Any] = {}
        self.kind: str = ""            # "track" | "ad" | ""
        self.track_name: str = ""
        self.artist_name: str = ""
        self.art_url: str = ""
        self.duration: float = 0.0
        self.started_at: float = 0.0
        self.paused: bool = False
        self._suppress_end: bool = False
        self._loading: bool = False
        self._load_at: float = 0.0
        self.volume: int = DEFAULT_VOLUME
        self.last_error: str = ""
        self._end_handling = False
        self._reload_attempted = False
        self._pause_started: float = 0.0
        self._cast_was_active = False  # cast device reached PLAYING/BUFFERING since last load
        self._mpv_path: Optional[str] = None
        # Windows job object: the kernel kills mpv when this process dies, so
        # closing Hermes stops the music even on a hard kill.
        self._job: Optional[KillOnCloseJob] = None
        self._request_id = 0
        self._replies: dict[int, Optional[dict]] = {}
        self._reply_events: dict[int, threading.Event] = {}
        # Phase 3: playback output — "local" (mpv on this host) or a Cast device
        # friendly name (Nest Audio / Nest Hub / Cast group).
        self.output: str = "local"
        self._cast = None  # pandora_cast.CastOutput instance in cast mode

    # ── process / IPC ──────────────────────────────────────────────────────

    def _ensure_mpv(self) -> None:
        with self._lock:
            self._ensure_mpv_locked()

    def _ensure_mpv_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._fd is not None:
            return
        self._mpv_path = self._mpv_path or find_mpv()
        if not self._mpv_path:
            try:
                from .pandora_mpv import ensure_mpv
                self._mpv_path = str(ensure_mpv(log=logger.info))
            except Exception as exc:
                raise PlayerUnavailable(
                    "mpv is required for local playback but is not installed. "
                    "Run `hermes pandora setup` to download a verified portable "
                    f"build, or set PANDORA_MPV_PATH to an mpv executable. ({exc})")
        pipe = _ipc_path()
        args = [
            self._mpv_path,
            "--idle=yes", "--no-video", "--no-terminal", "--really-quiet",
            "--force-window=no", "--audio-display=no",
            f"--input-ipc-server={pipe}",
            f"--volume={self.volume}",
            "--gapless-audio=no", "--cache=yes", "--cache-secs=30",
            "--user-agent=Mozilla/5.0",
        ]
        logger.info("Starting mpv: %s", " ".join(args))
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        if self._job is None:
            self._job = KillOnCloseJob()
        if self._proc is not None and self._job.active:
            self._job.assign(self._proc.pid)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                fd = os.open(pipe, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except OSError:
                if self._proc.poll() is not None:
                    raise PlayerUnavailable(f"mpv exited immediately (code {self._proc.returncode}).")
                time.sleep(0.05)
                continue
            # Windows named pipes are synchronous handles: a blocking read would
            # block our writes from the same handle. Non-blocking mode is the
            # difference between this working and the whole process hanging.
            self._fd = fd
            try:
                _set_pipe_nonblocking(fd)
            except PlayerUnavailable:
                self._ipc_stalled("this Python build has no os.set_blocking")
                raise
            except OSError:
                pass
            break
        if self._fd is None:
            raise PlayerUnavailable("Could not connect to the mpv IPC socket.")
        self._reader = threading.Thread(target=self._read_loop, name="pandora-mpv-reader", daemon=True)
        self._reader.start()
        self._send(["observe_property", 1, "pause"])
        self._send(["observe_property", 2, "time-pos"])
        self._send(["observe_property", 3, "duration"])
        self._send(["observe_property", 4, "media-title"])

    def _ipc_stalled(self, reason: str) -> None:
        """Drop a wedged mpv so the next playback attempt starts a fresh one."""
        with self._lock:
            logger.warning("Restarting mpv: %s", reason)
            fd, self._fd = self._fd, None
            proc, self._proc = self._proc, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    logger.debug("could not terminate the wedged mpv", exc_info=True)

    def _write_bytes(self, payload: bytes) -> None:
        """Write one IPC message with a hard deadline.

        A wedged mpv (or a stale handle on the pipe) used to spin here forever
        with no log line, hanging the caller silently — which is what forced a
        separate fallback player. On timeout the player is torn down so the
        next call spawns a fresh mpv, and the caller gets a real error.
        """
        fd = self._fd
        if fd is None:
            return
        view = memoryview(payload)
        deadline = time.monotonic() + WRITE_DEADLINE_SECONDS
        while view:
            try:
                written = os.write(fd, view)
            except (BlockingIOError, InterruptedError):
                if time.monotonic() >= deadline:
                    self._ipc_stalled("the mpv IPC pipe stopped accepting writes")
                    raise PlayerUnavailable(
                        "The mpv player stopped responding. It was restarted; retry playback.")
                time.sleep(0.01)
                continue
            except OSError as exc:
                logger.debug("mpv IPC write failed", exc_info=True)
                self._ipc_stalled(f"the mpv IPC pipe failed ({exc})")
                raise PlayerUnavailable(f"Lost the mpv control pipe ({exc}).") from exc
            if written <= 0:
                self._ipc_stalled("the mpv IPC pipe returned a zero-byte write")
                raise PlayerUnavailable(
                    "The mpv player stopped responding. It was restarted; retry playback.")
            view = view[written:]

    def _send(self, command: list[Any]) -> None:
        self._write_bytes((json.dumps({"command": command}) + "\n").encode("utf-8"))

    def _request(self, command: list[Any], *, timeout: float = 5.0) -> Optional[dict]:
        """Send a command and wait for mpv's reply (used for property reads)."""
        if self._fd is None:
            return None
        self._request_id += 1
        request_id = self._request_id
        event = threading.Event()
        self._replies[request_id] = None
        self._reply_events[request_id] = event
        self._write_bytes((json.dumps({"command": command, "request_id": request_id}) + "\n").encode("utf-8"))
        if not event.wait(timeout):
            # Don't leak the pending-reply entries when mpv never answers.
            self._replies.pop(request_id, None)
            self._reply_events.pop(request_id, None)
            return None
        return self._replies.pop(request_id, None)

    def _get_property(self, name: str, default: Any = None) -> Any:
        reply = self._request(["get_property", name])
        if isinstance(reply, dict) and reply.get("error") == "success":
            return reply.get("data")
        return default

    @staticmethod
    def _decode_line(line: bytes) -> Optional[dict]:
        try:
            payload = json.loads(line.decode("utf-8", "ignore"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _read_loop(self) -> None:
        fd = self._fd
        if fd is None:
            return
        buffer = b""
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                time.sleep(0.02)
                if self._proc is not None and self._proc.poll() is not None:
                    logger.debug("mpv exited; reader stopping")
                    break
                continue
            except OSError:
                break
            if not chunk:
                # Non-blocking pipe: an empty read means "nothing yet" (there is
                # no reliable EOF signal), so wait and check the process instead.
                time.sleep(0.02)
                if self._proc is not None and self._proc.poll() is not None:
                    break
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                payload = self._decode_line(line)
                if payload is not None:
                    self._handle_event(payload)
        logger.debug("mpv reader stopped")

    def _handle_event(self, payload: dict) -> None:
        request_id = payload.get("request_id")
        if isinstance(request_id, int):
            self._replies[request_id] = payload
            event = self._reply_events.pop(request_id, None)
            if event is not None:
                event.set()
            return
        if payload.get("event") == "file-loaded":
            self._loading = False
        if payload.get("event") == "property-change":
            name, value = payload.get("name"), payload.get("data")
            if name == "pause" and isinstance(value, bool):
                self.paused = value
            elif name == "duration" and isinstance(value, (int, float)) and value > 0:
                self.duration = float(value)
            elif name == "media-title" and isinstance(value, str):
                logger.debug("mpv media-title: %s", value)
        elif payload.get("event") == "end-file":
            reason = str(payload.get("reason") or "")
            logger.info("mpv end-file reason=%s", reason)
            switching = (self._suppress_end or self._loading
                         or (self._load_at and time.time() - self._load_at < 2.0))
            if switching and reason in {"eof", "stop"}:
                # The end-file of the track we just replaced (station change or
                # output switch) is not a finished track — treating it as one
                # advanced Pandora again, which is the skip loop.
                self._suppress_end = False
                self._loading = False
                logger.debug("ignoring mpv end-file from a replace")
            elif reason in {"eof", "stop"}:
                self._handle_track_end()
            elif reason == "error":
                self._handle_stream_error()

    # ── session ────────────────────────────────────────────────────────────

    @property
    def client(self):
        if self._client is None:
            from .pandora_client import PandoraClient
            self._client = PandoraClient()
        return self._client

    def _set_item(self, item: dict[str, Any], *, source_id: str, source_type: str, index: int) -> None:
        self.item = dict(item or {})
        self.source_id = source_id
        self.source_type = source_type
        self.index = int(index or 0)
        typename = str(self.item.get("__typename") or "")
        self.kind = "ad" if "Ad" in typename else "track"
        track = self.item.get("track") or {}
        self.track_name = str(track.get("name") or "")
        artist = track.get("artist") or {}
        self.artist_name = str(artist.get("name") or "") if isinstance(artist, dict) else ""
        art = track.get("art") or {}
        self.art_url = str(art.get("url") or "") if isinstance(art, dict) else ""
        self.duration = float(track.get("duration") or 0) or self.duration
        self.started_at = time.time()
        self._reload_attempted = False

    def play_source(self, source_id: str) -> dict[str, Any]:
        """Start (or switch to) a Pandora source on this device."""
        with self._lock:
            data = self.client.playback_set_source(source_id)
            node = ((data.get("playback") or {}).get("setSource") or {})
            current = node.get("current") or {}
            if not current:
                raise PlayerError("Pandora returned no playable item for that source.")
            self._set_item(current, source_id=str(node.get("id") or source_id),
                           source_type=str(node.get("type") or ""), index=int(current.get("index") or 0))
            self._ensure_output()
            self._play_item_audio()
            try:
                self.client.playback_started(self.source_id, self.index, 0)
            except Exception:
                logger.debug("setStarted failed", exc_info=True)
            self._start_progress()
            return self.state()

    # ── output (local mpv vs Cast device) ─────────────────────────────────

    def _in_cast(self) -> bool:
        return self.output != "local"

    def _ensure_output(self) -> None:
        if self._in_cast():
            from .pandora_cast import get_cast_output
            cast = get_cast_output()
            if not cast.connected:
                cast.connect(self.output, timeout=15.0)
            self._cast = cast
            try:
                cast.on_status(self)  # player implements new_media_status()
            except Exception:
                logger.debug("cast status listener unavailable", exc_info=True)
        else:
            self._ensure_mpv()

    def new_media_status(self, status: Any) -> None:
        """Cast media-status push (pychromecast status listener callback)."""
        state = str(getattr(status, "player_state", "") or "")
        try:
            if state in ("PLAYING", "BUFFERING"):
                self._cast_was_active = True
            if state == "IDLE" and self.kind and not self.paused:
                if not self._cast_was_active:
                    # IDLE before the device ever started this item (pre-buffer or a
                    # load that never took) — not a track end; ignore it.
                    logger.debug("cast IDLE before playback started — ignoring")
                    return
                logger.info("cast device went idle -> treating as track end")
                self._cast_was_active = False
                self._handle_track_end()
            elif state == "PAUSED" and not self.paused:
                self.paused = True
                self._pause_started = time.time()
                elapsed = max(0, int(self._cast_position() or 0))
                try:
                    self.client.playback_pause(self.source_id, self.index, elapsed)
                except Exception:
                    logger.debug("cast pause report failed", exc_info=True)
            elif state == "PLAYING" and self.paused:
                self.paused = False
                if self._pause_started:
                    self.started_at += time.time() - self._pause_started
                    self._pause_started = 0.0
                elapsed = max(0, int(self._cast_position() or 0))
                try:
                    self.client.playback_started(self.source_id, self.index, elapsed)
                except Exception:
                    logger.debug("cast resume report failed", exc_info=True)
            if self._cast is not None:
                st = self._cast.status()
                cdur = st.get("play_duration") or 0
                if cdur and cdur > 0:
                    self.duration = float(cdur)
        except Exception as exc:
            logger.warning("cast status handling failed: %s", exc)
            self.last_error = str(exc)

    def _cast_position(self) -> float:
        try:
            if self._cast is not None and self._cast.connected:
                st = self._cast.status()
                return float(st.get("current_time") or 0.0)
        except Exception:
            pass
        return 0.0

    def _stop_local_output(self) -> None:
        """Stop mpv without reporting the stop as a finished track."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._suppress_end = True
        try:
            self._send(["stop"])
        except Exception:
            self._suppress_end = False
            logger.debug("mpv stop failed", exc_info=True)

    def set_output(self, name: str, *, device_list: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Switch the playback output: 'local' or a Cast device name."""
        name = str(name or "local").strip() or "local"
        with self._lock:
            if name != "local":
                # Exactly one output may play: a second live stream echoes and
                # both ends report end-of-track, which skips Pandora twice.
                self._stop_local_output()
                from .pandora_cast import get_cast_output
                cast = get_cast_output()
                cast.connect(name, timeout=15.0, device_list=device_list)
                self.output = cast.name or name
            else:
                if self._cast is not None:
                    try:
                        self._cast.stop()
                    except Exception:
                        logger.debug("cast stop failed", exc_info=True)
                from .pandora_cast import reset_cast_output
                reset_cast_output()
                self._cast = None
                self.output = "local"
            # If something is playing, move the current item to the new output.
            if self.kind:
                try:
                    self._ensure_output()
                    self._play_item_audio()
                except Exception as exc:
                    logger.warning("could not move playback to %r: %s", name, exc)
                    self.last_error = str(exc)
            return self.state()

    def _play_item_audio(self) -> None:
        url = str(self.item.get("audioUrl") or "")
        if not url:
            self._refresh_audio_url()
            url = str(self.item.get("audioUrl") or "")
        if not url:
            raise PlayerError("Pandora returned no audio URL for the current item.")
        if self._in_cast():
            self._stop_local_output()
            self._suppress_end = False
            self._ensure_output()
            self._cast_was_active = False  # new load has not started yet
            self._cast.load(url, title=self.track_name, artist=self.artist_name,
                            duration=self.duration, volume=self.volume / 100.0,
                            art_url=self.art_url)
        else:
            self._suppress_end = False
            self._loading = True
            self._load_at = time.time()
            self._send(["loadfile", url, "replace"])
            self._send(["set_property", "pause", False])

    def _refresh_audio_url(self) -> None:
        """Audio URLs are short-lived; ask the API for a fresh one."""
        try:
            data = self.client.playback_current(self.source_id)
            current = ((data.get("playback") or {}).get("current") or {})
            if current.get("audioUrl"):
                self.item["audioUrl"] = current["audioUrl"]
        except Exception as exc:
            logger.info("Could not refresh the audio URL: %s", exc)

    def _handle_track_end(self) -> None:
        if self._end_handling:
            return
        if not self.source_id:
            # end-file from an explicit stop()/shutdown(): there is no source to
            # report against. Without this guard it logs a bogus failure and
            # fires a setEnded with an empty id.
            return
        self._end_handling = True
        try:
            elapsed = max(0, int(time.time() - self.started_at))
            if self.paused:
                return
            data = self.client.playback_ended(self.source_id, self.index, elapsed, "NORMAL")
            node = ((data.get("playback") or {}).get("setEnded") or {})
            nxt = node.get("current") or {}
            if not nxt:
                data = self.client.playback_current(self.source_id)
                nxt = ((data.get("playback") or {}).get("current") or {})
            if not nxt:
                self.last_error = "Pandora returned no next item."
                return
            if nxt.get("__typename") == "EndedResponse":
                # The source is exhausted (or the ad cycle closed) — stop cleanly.
                self.stop()
                return
            self._set_item(nxt, source_id=self.source_id, source_type=self.source_type,
                           index=int(nxt.get("index") or self.index + 1))
            self._play_item_audio()
        except Exception as exc:
            logger.warning("track-end handling failed: %s", exc)
            self.last_error = str(exc)
        finally:
            self._end_handling = False

    def _handle_stream_error(self) -> None:
        """A stream died (expired URL / network). Retry once with a fresh URL."""
        if self._reload_attempted:
            self.last_error = "Playback stopped: the stream failed twice."
            return
        self._reload_attempted = True
        with self._lock:
            self._refresh_audio_url()
            url = str(self.item.get("audioUrl") or "")
            if url:
                self._send(["loadfile", url, "replace"])
                self._send(["set_property", "pause", False])

    def _start_progress(self) -> None:
        self._stop_progress.set()
        self._stop_progress = threading.Event()
        stop = self._stop_progress

        def beat() -> None:
            while not stop.wait(PROGRESS_INTERVAL_SECONDS):
                try:
                    if self._in_cast():
                        elapsed = int(self._cast_position() or 0)
                    else:
                        elapsed = int(time.time() - self.started_at)
                    if not self.paused and elapsed > 5:
                        self.client.playback_progress(self.source_id, self.index, elapsed)
                except Exception:
                    logger.debug("progress beat failed", exc_info=True)

        self._progress_thread = threading.Thread(target=beat, name="pandora-progress", daemon=True)
        self._progress_thread.start()

    # ── controls ───────────────────────────────────────────────────────────

    def pause(self) -> dict[str, Any]:
        if self._in_cast():
            self._ensure_output()
            self._cast.pause()
            self.paused = True
            self._pause_started = time.time()
            elapsed = max(0, int(self._cast_position() or 0))
        else:
            self._send(["set_property", "pause", True])
            self.paused = True
            self._pause_started = time.time()
            elapsed = max(0, int(time.time() - self.started_at))
        try:
            self.client.playback_pause(self.source_id, self.index, elapsed)
        except Exception:
            logger.debug("pause report failed", exc_info=True)
        return self.state()

    def resume(self) -> dict[str, Any]:
        if self._in_cast():
            self._ensure_output()
            self._cast.play()
            self.paused = False
            self._pause_started = 0.0
            elapsed = max(0, int(self._cast_position() or 0))
        else:
            self._send(["set_property", "pause", False])
            self.paused = False
            # Keep elapsed-time reporting continuous: exclude the paused interval.
            if self._pause_started:
                self.started_at += time.time() - self._pause_started
                self._pause_started = 0.0
            elapsed = max(0, int(time.time() - self.started_at))
        try:
            self.client.playback_started(self.source_id, self.index, elapsed)
        except Exception:
            logger.debug("resume report failed", exc_info=True)
        return self.state()

    def stop(self) -> dict[str, Any]:
        self._stop_progress.set()
        if self._in_cast():
            try:
                if self._cast is not None and self._cast.connected:
                    self._cast.stop()
            except Exception:
                logger.debug("cast stop failed", exc_info=True)
        else:
            self._send(["stop"])
        self.item = {}
        self.kind = ""
        self.source_id = ""
        self.track_name = ""
        self.artist_name = ""
        return self.state()

    def next(self) -> dict[str, Any]:
        with self._lock:
            elapsed = max(0, int(time.time() - self.started_at))
            data = self.client.playback_skip(self.source_id, self.index, elapsed)
            node = ((data.get("playback") or {}).get("skip") or {})
            nxt = node.get("current") or {}
            if not nxt:
                data = self.client.playback_current(self.source_id)
                nxt = ((data.get("playback") or {}).get("current") or {})
            if not nxt:
                raise PlayerError("Pandora did not return a next track (skip may be unavailable).")
            if nxt.get("__typename") == "EndedResponse":
                raise PlayerError("Playback has ended (no more items in this source).")
            self._set_item(nxt, source_id=self.source_id, source_type=self.source_type,
                           index=int(nxt.get("index") or self.index + 1))
            self._ensure_output()
            self._play_item_audio()
            return self.state()

    def previous(self) -> dict[str, Any]:
        """Restart the current track.

        Pandora radio has no "previous track" operation: the API answers
        NOT_SUPPORTED for the previous mutation on station sources, and the
        station's queue is not navigable backwards. What every Pandora client
        does instead is start the song over, so that is what this exposes —
        reported as a replay, with the active output rewound to 0. With
        nothing loaded, start the station instead.
        """
        with self._lock:
            if not self.item:
                raise PlayerError("Nothing is playing — start a station first.")
            try:
                self._ensure_output()
            except Exception:
                logger.debug("previous: ensure output failed", exc_info=True)
            try:
                return self.replay()
            except Exception:
                logger.debug("previous: replay unavailable; rewinding locally", exc_info=True)
                if self.kind:
                    try:
                        self.seek(0.0)
                    except Exception:
                        logger.debug("previous: seek(0) failed", exc_info=True)
                return self.state()

    def replay(self) -> dict[str, Any]:
        token = str(self.item.get("trackToken") or "")
        if not token:
            raise PlayerError("No track to replay.")
        self.client.playback_replay(self.source_id, self.index, token, max(0, int(time.time() - self.started_at)))
        # "Replay" means play this song from the top again: restart the media
        # position on the active output (absolute 0 on local, relative rewind
        # on Cast). Without this the device just keeps playing from where it
        # was, which reads as a no-op (observed live on the Nest Hub 2026-11-06).
        if self.kind:
            try:
                self.seek(0.0)
            except Exception:
                logger.debug("replay: seek(0) failed", exc_info=True)
        return self.resume()

    def set_volume(self, level: int) -> dict[str, Any]:
        self.volume = max(0, min(100, int(level)))
        if self._in_cast():
            try:
                if self._cast is not None and self._cast.connected:
                    self._cast.set_volume(self.volume / 100.0)
            except Exception:
                logger.debug("cast set_volume failed", exc_info=True)
        else:
            self._send(["set_property", "volume", self.volume])
        return self.state()

    def seek(self, seconds: float) -> dict[str, Any]:
        if self._in_cast():
            self._ensure_output()
            # pychromecast's seek() takes an ABSOLUTE position in seconds — it
            # sends `currentTime: position` — so pass the target, not a delta.
            self._cast.seek(float(seconds))
        else:
            self._send(["seek", float(seconds), "absolute"])
            self.started_at = time.time() - float(seconds)
        return self.state()

    def _feedback_name(self) -> str:
        """'up' / 'down' for the current item, '' when it has no thumb."""
        raw = str(self.item.get("feedbackValue") or "").upper()
        if "THUMB_UP" in raw or raw == "UP":
            return "up"
        if "THUMB_DOWN" in raw or raw == "DOWN":
            return "down"
        return ""

    def state(self) -> dict[str, Any]:
        position = 0.0
        playing = bool(self.kind) and not self.paused
        if self._in_cast() and self.kind:
            cst = self._cast.status() if (self._cast is not None and self._cast.connected) else {}
            position = float(cst.get("current_time") or 0.0)
            cdur = cst.get("play_duration") or 0
            if cdur > 0:
                self.duration = float(cdur)
            ps = str(cst.get("player_state") or "")
            if ps:
                playing = ps == "PLAYING"
                if ps == "PLAYING":
                    self.paused = False
        else:
            if self.started_at and self.kind:
                position = max(0.0, min(time.time() - self.started_at, self.duration or 1e9))
            live_position = self._get_property("time-pos") if self.kind and self._fd is not None else None
            if isinstance(live_position, (int, float)) and live_position >= 0:
                position = float(live_position)
            live_duration = self._get_property("duration") if self.kind and self._fd is not None else None
            if isinstance(live_duration, (int, float)) and live_duration > 0:
                self.duration = float(live_duration)
        return {
            "playing": playing,
            "paused": self.paused,
            "kind": self.kind,
            "track": self.track_name,
            "artist": self.artist_name,
            "art_url": self.art_url,
            "source": self.source_id,
            "source_type": self.source_type,
            "index": self.index,
            "duration_seconds": round(self.duration or 0),
            "position_seconds": round(position),
            "volume": self.volume,
            "output": self.output,
            "feedback": self._feedback_name(),
            "device_uuid": getattr(self.client, "device_uuid", "") if self._client else "",
            "last_error": self.last_error,
        }

    def shutdown(self) -> None:
        self._stop_progress.set()
        try:
            from .pandora_cast import reset_cast_output
            reset_cast_output()
        except Exception:
            pass
        self._cast = None
        try:
            if self._fd is not None:
                self._send(["quit"])
        except Exception:
            pass
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        self._proc = None
        if self._job is not None:
            # Closing the job also kills any straggler still inside it.
            self._job.close()
            self._job = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


player = PandoraPlayer()
