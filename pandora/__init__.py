"""hermes-pandora — Pandora music control for Hermes Agent.

Standalone plugin (toolset ``pandora``): local playback through mpv, search,
stations, feedback and collections over Pandora's GraphQL API, plus
``hermes pandora login|status|logout``.

Registration goes only through the public plugin API: ``ctx.register_tool``
(via ``register_tools``) and ``ctx.register_cli_command``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .pandora_tools import register_tools

logger = logging.getLogger(__name__)

__all__ = ["register", "register_tools"]


def _register_skills(ctx) -> None:
    """Expose the bundled usage skill to the agent.

    Native (``plugin.yaml``) plugins must register their skills explicitly —
    only portable ``plugin.json`` packages get ``skills/*`` auto-discovery, so
    a skill that merely sits in ``skills/`` is never loaded. Guarded for
    Hermes builds that predate ``ctx.register_skill``.
    """
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is None:
        return
    skills_dir = Path(__file__).resolve().parent / "skills"
    if not skills_dir.is_dir():
        return
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            register_skill(child.name, skill_md)


def _cli_setup(parser) -> None:  # noqa: ANN001 - argparse subparser
    from .pandora_cli import setup_parser
    setup_parser(parser)


def _cli_handle(args) -> int:
    from .pandora_cli import handle
    return handle(args)


def _prefetch_mpv() -> None:
    """Fetch the portable mpv in the background when it is missing.

    ``hermes pandora setup`` is the documented step; this covers the user who
    skips it and starts playback right away. The download is shared by every
    profile, so it runs once.
    """
    try:
        from . import pandora_mpv
        if pandora_mpv.mpv_path() or not pandora_mpv.auto_download_enabled():
            return
        from .pandora_player import find_mpv
        if find_mpv():
            return
        import threading
        threading.Thread(target=_download_mpv_quietly, name="pandora-mpv-prefetch",
                         daemon=True).start()
    except Exception:
        logger.debug("mpv prefetch skipped", exc_info=True)


def _download_mpv_quietly() -> None:
    try:
        from . import pandora_mpv
        pandora_mpv.download(log=lambda msg: logger.info("mpv setup: %s", msg))
    except Exception as exc:
        logger.warning("mpv auto-download failed: %s (run `hermes pandora setup`)", exc)


def register(ctx) -> None:
    """Entry point called by the Hermes plugin loader."""
    register_tools(ctx)
    _register_skills(ctx)
    _prefetch_mpv()
    ctx.register_cli_command(
        name="pandora",
        help="Pandora: sign in, check status, sign out, fetch mpv",
        setup_fn=_cli_setup,
        handler_fn=_cli_handle,
        description="Pandora music control (login/logout/status/setup + agent tools)",
    )
    logger.info("hermes-pandora registered (toolset: pandora)")
