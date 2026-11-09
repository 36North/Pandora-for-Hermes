"""Windows DPAPI storage for the Pandora password.

The password has to be readable unattended — the plugin re-logs in when the
session token expires — and Windows DPAPI gives that without a key file or a
keychain entry: ``CryptProtectData`` encrypts against the current user,
``CryptUnprotectData`` decrypts only for that user on that machine. A copied
file is useless anywhere else, which is the correct failure: you log in again.

Non-Windows has no DPAPI (and local playback is Windows-only anyway), so
``available()`` is false there and the caller falls back to the environment.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UI_FORBIDDEN = 0x1
_IS_WINDOWS = os.name == "nt"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    """True when DPAPI can be used in this process."""
    if not _IS_WINDOWS:
        return False
    try:
        return hasattr(ctypes.windll, "crypt32")
    except Exception:
        return False


def path() -> Path:
    """Beside the downloaded mpv; shared by every profile on this install."""
    from .pandora_mpv import bin_dir

    return bin_dir().parent / "credentials.bin"


def _blob_in(data: bytes) -> tuple[_Blob, Any]:
    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _take(blob: _Blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(blob.pbData)
        except Exception:
            pass


def protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user."""
    blob_in, _buf = _blob_in(data)
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "hermes-pandora", None, None, None,
        _UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return _take(blob_out)


def unprotect(data: bytes) -> bytes:
    """Decrypt a blob made by :func:`protect` for this user."""
    blob_in, _buf = _blob_in(data)
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    return _take(blob_out)


def save(email: str, password: str) -> bool:
    """Encrypt and store the credentials; False when DPAPI is unavailable."""
    if not password or not available():
        return False
    payload = json.dumps({"email": email or "", "password": password}).encode("utf-8")
    blob = protect(payload)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return True


def load() -> Optional[dict[str, Any]]:
    """The stored credentials, or None when there is nothing readable."""
    if not available():
        return None
    target = path()
    if not target.is_file():
        return None
    try:
        data = json.loads(unprotect(target.read_bytes()).decode("utf-8"))
    except Exception:
        logger.debug("stored credentials could not be decrypted", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def clear() -> bool:
    try:
        path().unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("stored credentials could not be removed", exc_info=True)
        return False
