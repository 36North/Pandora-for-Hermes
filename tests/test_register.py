"""Tests for plugin registration: tools, CLI command, and the bundled usage skill."""

from __future__ import annotations

import pandora as plugin


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.cli = []
        self.skills = []

    def register_tool(self, **kw):
        self.tools.append(kw["name"])

    def register_cli_command(self, **kw):
        self.cli.append(kw["name"])

    def register_skill(self, name, path, *a, **kw):
        self.skills.append((name, str(path)))


def test_register_exposes_tools_cli_and_bundled_skill():
    ctx = FakeCtx()
    plugin.register(ctx)

    assert len(ctx.tools) == 7
    assert ctx.cli == ["pandora"]
    # Native plugins must register skills explicitly; leaving the skill only on
    # disk means the agent never sees the playback policy.
    assert [name for name, _ in ctx.skills] == ["pandora-usage"]
    assert ctx.skills[0][1].replace("\\", "/").endswith("skills/pandora-usage/SKILL.md")


def test_register_tolerates_ctx_without_register_skill():
    class OlderCtx(FakeCtx):
        register_skill = None

    ctx = OlderCtx()
    plugin.register(ctx)  # must not raise on Hermes builds predating register_skill
    assert len(ctx.tools) == 7
    assert ctx.skills == []
