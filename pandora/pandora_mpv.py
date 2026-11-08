"""Fetch the mpv binary used for local playback.

The plugin no longer ships mpv inside the package (a 120 MB executable in a
plugin is both a store-scan block and a slow clone). Instead:

* ``hermes pandora setup`` downloads a portable build into
  ``<launch home>/plugin-data/pandora/bin`` and verifies it, and
* the player calls :func:`ensure_mpv` the first time it needs mpv, so ignoring
  the setup step still works — it just downloads at that moment.

``PANDORA_MPV_PATH`` still wins when set (a system mpv is never downloaded
over). ``PANDORA_MPV_URL`` points at the portable zip; ``PANDORA_MPV_SHA256``
verifies it. ``PANDORA_MPV_AUTO_DOWNLOAD=0`` disables the automatic fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

# Released under the repo's `mpv` tag. Resolved through the GitHub API by tag +
# asset name, so a re-uploaded asset (new id) keeps working.
DEFAULT_MPV_URL = "github-release://36North/Pandora-for-Hermes/mpv/mpv-portable.zip"

GITHUB_RELEASE_SCHEME = "github-release://"

MPV_BINARIES = ("mpv.exe", "mpv.com", "d3dcompiler_43.dll")
MPV_DOCS = ("LICENSE-GPL-2.0.txt", "NOTICE-third-party.txt")


class MpvSetupError(RuntimeError):
    pass


def _launch_home() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def bin_dir() -> Path:
    """Where the downloaded portable build lives (shared by every profile)."""
    return _launch_home() / "plugin-data" / "pandora" / "bin"


def mpv_path() -> Optional[Path]:
    """The downloaded mpv, if present."""
    exe = bin_dir() / "mpv.exe"
    return exe if exe.is_file() else None


def download_url() -> str:
    return str(os.environ.get("PANDORA_MPV_URL", "") or DEFAULT_MPV_URL).strip()


def auto_download_enabled() -> bool:
    return str(os.environ.get("PANDORA_MPV_AUTO_DOWNLOAD", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _github_get(url: str, *, binary: bool = False) -> bytes:
    accept = "application/octet-stream" if binary else "application/vnd.github+json"
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "hermes-pandora"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _resolve_github_release(spec: str) -> tuple[str, str]:
    """``owner/repo/tag/asset`` → (asset API URL, checksum API URL or '')."""
    parts = spec[len(GITHUB_RELEASE_SCHEME):].strip("/").split("/", 3)
    if len(parts) != 4 or not all(parts):
        raise MpvSetupError(
            f"{GITHUB_RELEASE_SCHEME}<owner>/<repo>/<tag>/<asset> expected, got {spec!r}")
    owner, repo, tag, asset = parts
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        data = json.loads(_github_get(api).decode("utf-8"))
    except Exception as exc:
        raise MpvSetupError(f"Could not read release {tag} of {owner}/{repo}: {exc}") from exc
    assets = {str(a.get("name")): a.get("id") for a in data.get("assets") or []}
    if asset not in assets:
        raise MpvSetupError(f"Release {tag} of {owner}/{repo} has no asset {asset!r}")
    base = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{assets[asset]}"
    sha_name = asset + ".sha256"
    sha_url = (f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{assets[sha_name]}"
               if sha_name in assets else "")
    return base, sha_url


def _remote_size(url: str) -> str:
    if url.startswith("file://"):
        try:
            from urllib.request import url2pathname
            from urllib.parse import urlparse
            path = Path(url2pathname(urlparse(url).path.lstrip("/")))
            return f"{path.stat().st_size / (1024 * 1024):.1f} MB"
        except Exception:
            return "unknown size"
    try:
        request = urllib.request.Request(
            url, method="HEAD",
            headers={"Accept": "application/octet-stream", "User-Agent": "hermes-pandora"})
        with urllib.request.urlopen(request, timeout=20) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > 0:
                return f"{int(length) / (1024 * 1024):.1f} MB"
    except Exception:
        pass
    return "unknown size"


def _expected_sha(url: str, sha_url: str = "") -> str:
    env = str(os.environ.get("PANDORA_MPV_SHA256", "") or "").strip().lower()
    if env:
        return env
    try:
        if sha_url:
            text = _github_get(sha_url, binary=True).decode("utf-8", "replace").strip().split()
        else:
            with urllib.request.urlopen(url + ".sha256", timeout=20) as response:
                text = response.read().decode("utf-8", "replace").strip().split()
        if text and len(text[0]) == 64:
            return text[0].lower()
    except Exception:
        pass
    return ""


def download(*, url: Optional[str] = None, force: bool = False,
             log: Callable[[str], None] = print) -> Path:
    """Download + extract the portable build; return the mpv.exe path."""
    url = (url or download_url()).strip()
    if not url:
        raise MpvSetupError(
            "No mpv download URL is configured. Set PANDORA_MPV_URL to the portable "
            "zip (or set PANDORA_MPV_PATH to an installed mpv).")
    sha_url = ""
    if url.startswith(GITHUB_RELEASE_SCHEME):
        url, sha_url = _resolve_github_release(url)
    target = bin_dir()
    if mpv_path() and not force:
        log(f"mpv already installed: {mpv_path()}")
        return mpv_path()  # type: ignore[return-value]

    target.mkdir(parents=True, exist_ok=True)
    log(f"Downloading mpv (~{_remote_size(url)}) from {url}")
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="pandora-mpv-")
    os.close(fd)  # mkstemp leaves the descriptor open, which locks the file on Windows
    tmp_zip = Path(tmp_name)
    try:
        request = urllib.request.Request(
            url, headers={"Accept": "application/octet-stream",
                          "User-Agent": "hermes-pandora"})
        with urllib.request.urlopen(request, timeout=60) as response, tmp_zip.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last = -1
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    percent = int(done * 100 / total)
                    if percent >= last + 10:
                        last = percent
                        log(f"  {percent}% ({done / (1024 * 1024):.1f} MB)")
            if not total:
                log(f"  {done / (1024 * 1024):.1f} MB")

        expected = _expected_sha(url, sha_url)
        actual = _sha256(tmp_zip)
        if expected and actual != expected:
            raise MpvSetupError(
                f"Checksum mismatch for {url}\n  expected {expected}\n  got      {actual}")
        if expected:
            log(f"SHA-256 verified: {actual}")
        else:
            log(f"SHA-256 (no published checksum to compare): {actual}")

        with zipfile.ZipFile(tmp_zip) as archive:
            names = {Path(n).name for n in archive.namelist()}
            missing = [name for name in MPV_BINARIES if name not in names]
            if missing:
                raise MpvSetupError(f"Archive is missing {', '.join(missing)}")
            for info in archive.infolist():
                name = Path(info.filename).name
                if not name or not (name in MPV_BINARIES or name in MPV_DOCS):
                    continue
                with archive.open(info) as src, (target / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    finally:
        tmp_zip.unlink(missing_ok=True)

    exe = target / "mpv.exe"
    if sys.platform != "win32":
        exe.chmod(0o755)
    if not exe.is_file():
        raise MpvSetupError("mpv.exe was not extracted")
    log(f"Installed mpv: {exe}")
    return exe


def ensure_mpv(*, log: Callable[[str], None] = print) -> Path:
    """Return the portable mpv, downloading it if needed and allowed."""
    found = mpv_path()
    if found:
        return found
    if not auto_download_enabled():
        raise MpvSetupError(
            "mpv is not installed. Run `hermes pandora setup` (or set PANDORA_MPV_PATH).")
    return download(log=log)
