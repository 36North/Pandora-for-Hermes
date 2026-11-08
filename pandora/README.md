# Pandora for Hermes Agent

Pandora music control from chat: local playback on this machine, search,
stations, thumbs up/down, and collections — using your own Pandora account
through the same GraphQL API the pandora.com web player uses.

Works on **Free, Plus and Premium**. Free/Plus get stations (and skip limits);
on-demand songs, albums and playlists require Premium and fail with a clear
message below that.

## Install

```bash
hermes plugins install <path-or-url>      # or drop this directory in ~/.hermes/plugins/pandora
hermes pandora login
```

**Requirements:** Python 3.12+ — local playback drives mpv over a Windows named pipe
and needs `os.set_blocking`, which Windows only gained in Python 3.12 (Hermes' bundled
runtime is 3.14). Casting needs `pychromecast` 14.x. Both are declared in
`pyproject.toml`, so `hermes plugins install` offers to install the dependency; the
plugin still works without it for search, stations and local playback.

Current Hermes enables the plugin's toolset as part of `plugins install`/`enable`
and it is on by default on platforms without an explicit toolset list. If your
profile was set up on an older Hermes (where `plugins enable` didn't touch
toolsets) or a platform's saved toolset list lacks `pandora`, run the one-time:

```bash
hermes tools enable pandora
```

`hermes pandora login` asks for your Pandora email and password (stored in the
**launch profile's** `.env`; the session token in its `auth.json`). The login is
account-wide on purpose: every profile and bot shares the same Pandora session
and device id, so you sign in once — see `pandora_auth._account_home()`.

Hermes discovers agent plugins **per profile** (`$HERMES_HOME/plugins`). This
repo's `sync.sh` installs the plugin into the launch profile and every profile
under `profiles/`; a profile only loads it when `plugins.enabled` lists
`pandora` (`hermes plugins enable pandora` with that profile's `HERMES_HOME`).
The desktop status-bar half is app-level — one copy, every profile.

Local playback needs **mpv**, which is not bundled with the plugin:

- **Recommended:** after installing, run `hermes pandora setup`. It downloads a
  portable Windows build into `~/.hermes/plugin-data/pandora/bin`, verifies the
  archive SHA-256, extracts it, and prints the `mpv --version` it found.
- **Skipped that step?** The plugin downloads the same build automatically the
  first time playback needs it, so pressing play on a fresh install still works
  (one-time ~47 MB download). Set `PANDORA_MPV_AUTO_DOWNLOAD=0` to turn the
  automatic fetch off.
- **Already have mpv?** Set `PANDORA_MPV_PATH` (or put mpv on `PATH`) and the
  downloader is never used.

`PANDORA_MPV_URL` overrides the download source; `PANDORA_MPV_SHA256` pins the
archive hash (a sibling `<url>.sha256` is used when present).

## Commands

| Command | What it does |
|---|---|
| `hermes pandora login` | Sign in (email + password); `--signup` opens the sign-up page |
| `hermes pandora capture` | Browser fallback when Pandora blocks the headless login: listens on 127.0.0.1 and stores the session your signed-in browser sends |
| `hermes pandora import-session` | Store a session token copied from the browser's devtools |
| `hermes pandora status` | Login + player status |
| `hermes pandora logout` | Clear the session (and saved credentials unless `--keep-credentials`) |

## Tools

| Tool | Actions |
|---|---|
| `pandora_playback` | `play` (source id), `now_playing`, `pause`, `resume`, `next`, `previous`, `replay`, `stop`, `set_volume`, `seek` |
| `pandora_search` | search stations/tracks/albums/playlists/podcasts/artists → ids |
| `pandora_stations` | `list`, `play` (by name or id), `recent`, `shuffle`, `thumbprint` |
| `pandora_feedback` | `love`, `ban`, `undo` on the current track |
| `pandora_collection` | `list` by type, `recent_favorites`, `add`/`remove` (station seeds, favorites) |
| `pandora_devices` | `list` / `set` / `status` — playback output: local (mpv) or a Cast device (Nest/Chromecast) |

Example prompts:

- "Play my Shuffle station"
- "Put on some Adele"
- "What's playing?" / "Skip this" / "Thumbs down this song"
- "Turn it up to 80" / "Pause for a minute"

## Desktop status bar

The plugin ships a desktop half: a status-bar chip (bottom right) showing
**Artist — Song**. Left-click it for the Pandora menu — play/pause, skip,
thumbs up/down, volume, stations, output (this PC or a Cast device) and
playlists. The menu is plan-aware: on Free/Plus the on-demand and playlist
entries explain why they are unavailable; Premium unlocks them.

Enable it once in **Settings → Plugins (Capabilities) → Pandora** — the desktop
half is opt-in and stays off until you flip the toggle. It talks to
`/api/plugins/pandora/*`, served by `dashboard/plugin_api.py`. The backend
follows whichever profile's player is actually running, and the login is
account-wide, so music started from any bot shows up in the chip.

Pandora's GraphQL surface exposes which content types a plan allows
(`listenerTemplate.searchTypes` / `collectionTypes`) but no separate
Free-vs-Plus flag; Free and Plus have the same on-demand capabilities, so the
menu labels that tier **Free or Plus** and reserves the premium entries for the
plan that actually differs.

## Playback policy (what your agent does)

The plugin ships a `pandora-usage` skill (in `skills/`) with the full instruction
set; the tool descriptions carry short tier reminders. Summary:

1. **On-demand songs, albums and playlists are Premium-only.** On Free/Plus
   accounts the tools degrade gracefully (search returns no on-demand items;
   `TR:` playback is `FORBIDDEN` — a correct tier response, not an error to chase).
2. **Song/artist request on a below-Premium account:** the agent falls back to an
   artist radio — existing station first (`pandora_stations` list/play), otherwise
   it creates one from the artist's `AR:` seed and plays it, then tells you so.
3. **No auto-skip loops.** If the station's current item isn't by the requested
   artist, the agent leaves it playing and asks before skipping (Free/Plus skip
   quota is limited).

## How it works

- **Auth:** `POST /api/v1/listener/emailPassword` (the web login), then
  `X-AuthToken` + `X-CsrfToken` headers on `POST /api/v1/graphql/graphql`.
  Sessions are re-established automatically from the saved credentials on 401.
- **Device:** playback is scoped to a stable `deviceUuid` per Hermes install,
  like any Pandora client. The API hands back a short-lived `audioUrl`; the
  plugin plays it locally with mpv and reports `setStarted` / `setProgress` /
  `setEnded`, which is what drives station personalization, skip limits and ads.
- **Ads:** on the Free tier the API queues audio ads; they play (that's what
  keeps the account ad-supported and the plugin honest). Track metadata and
  feedback are skipped while an ad is playing.

## Bundled binaries

`bin/mpv.exe` (with `mpv.com` and `d3dcompiler_43.dll`) is a portable mpv build used
for local playback. mpv is GPLv2+ — the third-party notice and the license text are in
`bin/NOTICE-third-party.txt` and `bin/LICENSE-GPL-2.0.txt`, including the corresponding
source links. To use your own mpv instead, install it and set `PANDORA_MPV_PATH`.

## Known limitations

- Local playback is Windows-first: mpv is driven over a Windows named pipe (a Unix
  socket elsewhere). Casting to Chromecast, Nest and Google Cast groups works through
  `pandora_devices` and needs `pychromecast`.
- `previous` / `replay` / `repeat` / `shuffle` are tier- and source-dependent;
  Pandora returns the restriction as a readable error.
- Third-party clients are a gray area in Pandora's terms; this uses your own
  account and the same endpoints as the web player, with no scraping and no
  sharing of credentials.

## Development

```bash
python -m pytest tests -q
```

Run this from the source tree (`pandora-plugin/`), not from an installed copy — the
installer ships the plugin code only. Tests mock the HTTP layer; nothing hits Pandora.
`PLAN.md` (one level up) has the API research notes and the phase plan.
