#!/usr/bin/env bash
# DEV HELPER — not part of the shipped plugin, and not for end users.
#
# Copies this dev tree into the plugins directory of every Hermes home on the
# machine (the default home plus each profile) so one source of truth drives all
# of them. Hermes discovers agent plugins per profile (`$HERMES_HOME/plugins`),
# so the code has to exist in each home that should load it; a home still only
# *loads* it when its `plugins.enabled` lists `pandora`.
#
# WARNING: this OVERWRITES whatever is installed at each target, including copies
# installed from the catalog or a git URL. A release install carries
# `.hermes-origin.json` / `.hermes-catalog.json` provenance; overwriting it
# desyncs `hermes plugins update` (reinstall from the same source afterwards).
# Targets that look like real installs are reported before anything is written.
#
# Usage:
#   bash scripts/dev-sync.sh                 # sync into $HERMES_ROOT (auto-detected)
#   HERMES_ROOT=/path/to/.hermes bash scripts/dev-sync.sh
#   PANDORA_DEV_TREE=/elsewhere/pandora-plugin bash scripts/dev-sync.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${PANDORA_DEV_TREE:-$REPO_ROOT}/pandora"

if [ -n "${HERMES_ROOT:-}" ]; then
  ROOT="$HERMES_ROOT"
elif [ -d "$HOME/AppData/Local/hermes" ]; then
  ROOT="$HOME/AppData/Local/hermes"          # Windows (Hermes default)
elif [ -d "$HOME/.hermes" ]; then
  ROOT="$HOME/.hermes"                       # Linux / macOS
else
  echo "dev-sync: no Hermes home found; set HERMES_ROOT" >&2
  exit 2
fi

[ -f "$SRC/plugin.yaml" ] || { echo "dev-sync: no plugin at $SRC" >&2; exit 2; }

warn_if_release_install() {
  local dst="$1"
  if [ -f "$dst/.hermes-origin.json" ] || [ -f "$dst/.hermes-catalog.json" ]; then
    echo "dev-sync: WARNING $dst looks like a release install (provenance marker);" >&2
    echo "  this sync overwrites it — reinstall from the same source afterwards." >&2
  fi
}

sync_one() {
  local dst="$1"
  warn_if_release_install "$dst"
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
  for dir in dashboard desktop scripts; do
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
  case "$(basename "${profile%/}")" in .*|.deleted) continue ;; esac
  sync_one "${profile}plugins/pandora"
done
