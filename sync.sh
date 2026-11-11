#!/usr/bin/env bash
# Sync the dev tree into every Hermes profile's plugins directory.
#
# Hermes discovers agent plugins per profile (`$HERMES_HOME/plugins`), so the
# code has to exist in each profile that should be able to load it. This keeps
# one source of truth (this repo) and copies it everywhere. Credentials are
# per-profile: each profile resolves PANDORA_EMAIL/PANDORA_PASSWORD from its
# own secret scope (.env, secret source, or the managed env layer for
# all-profiles use); session state lives in each profile's auth.json and
# self-heals via automatic re-login.
#
# A profile still only *loads* the plugin when `plugins.enabled` lists
# `pandora` — activation stays an explicit choice.
set -euo pipefail

SRC="$HOME/pandora-plugin/pandora"
ROOT="$HOME/AppData/Local/hermes"

sync_one() {
  local dst="$1"
  mkdir -p "$dst/bin"
  cp -f "$SRC"/*.py "$SRC"/plugin.yaml "$SRC"/README.md "$dst"/
  # Ship the license with every installed copy (MIT requires the notice).
  [ -f "$SRC/LICENSE" ] && cp -f "$SRC/LICENSE" "$dst"/
  if [ -f "$SRC/pyproject.toml" ]; then cp -f "$SRC/pyproject.toml" "$dst"/; fi
  # mpv is not bundled: `hermes pandora setup` downloads it into plugin-data.
  for f in "$SRC"/bin/*.txt; do
    [ -f "$f" ] || continue
    cp -f "$f" "$dst"/bin/
  done
  for dir in dashboard desktop; do
    if [ -d "$SRC/$dir" ]; then rm -rf "$dst/$dir"; cp -rf "$SRC/$dir" "$dst/$dir"; fi
  done
  rm -rf "$dst/dashboard/__pycache__"
  rm -rf "$dst/skills"
  cp -rf "$SRC/skills" "$dst/skills"
  echo "synced $SRC -> $dst"
}

sync_one "$ROOT/plugins/pandora"
for profile in "$ROOT"/profiles/*/; do
  [ -d "$profile" ] || continue
  sync_one "${profile}plugins/pandora"
done
