# Pandora for Hermes

![Pandora in the Hermes status bar](assets/hero.png)

Pandora (pandora.com) inside Hermes: play your stations on this PC or on a Google
Cast / Nest device, see the current artist and song in the desktop status bar,
and give the agent real playback tools — play, skip, restart, thumbs, volume,
search, station creation, and the full station list.

It talks to Pandora through the same web GraphQL API the pandora.com player uses,
with **your own account**. There is no API key, no OAuth app to register, and no
third-party server in the path — your session stays on your machine.

> This plugin is unofficial and community-maintained. It is not affiliated with,
> endorsed by, or supported by Pandora. Because it uses Pandora's private web
> API, a change on Pandora's side can break it; issues and fixes are welcome.

**Works on Free, Plus and Premium.** Free/Plus get stations, podcasts, search,
thumbs and skips. On-demand songs, albums and playlists are Premium-only and
fail with a clear message rather than a mystery error.

---

## Install

```bash
hermes plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable
```

That is the `owner/repo#subdir` form while the plugin is distributed from its own
repository. Once it is listed in the Hermes plugin catalog the same install is:

```bash
hermes plugins install pandora
```

Plugins are installed **per profile**. Agent tools and the dashboard backend only
exist in a profile that has the plugin enabled:

```bash
hermes -p <profile> plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable
```

The **account, credentials, mpv binary and player** are shared across profiles —
you sign in once (see [Where things live](#where-things-live)). Only the code and
the enable flag are per profile, which is how Hermes scopes plugin execution.

Windows is the supported platform today: local playback drives mpv over a named
pipe and needs `os.set_blocking` (Python 3.12+, which Hermes' bundled runtime
satisfies). Casting needs `pychromecast` 14.x, declared in `pyproject.toml`, so
the installer offers it.

---

## Prerequisites

- A Pandora account (free signup is fine).
- Hermes Agent **0.21.6 or newer**.
- **mpv** for local playback. It is **not bundled** — a bundled executable trips
  the installer's binary scan and GitHub's per-file limit. Get it one of three
  ways:
  - `hermes pandora setup` — downloads a checksum-verified portable build into
    the plugin's data directory (recommended);
  - let the plugin fetch it automatically the first time playback needs it;
  - point `PANDORA_MPV_PATH` at an mpv you already installed (or install
    `shinchiro.mpv` with winget and leave the path unset — it is found on PATH).
- For casting: a Cast device on the **same LAN** — Nest Audio, Nest Hub, a Cast
  group, or anything the Google Home app can see.

---

## Setup

### 1. Install the plugin

```bash
hermes plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable
```

### 2. Get mpv

```bash
hermes pandora setup
```

Prints the download size, verifies the SHA-256, and installs to
`<launch home>/plugin-data/pandora/bin`. Skip it if you like — the first playback
will download it in the background instead, unless `PANDORA_MPV_AUTO_DOWNLOAD=0`.

### 3. Sign in

```bash
hermes pandora login
```

Email + password against Pandora's web login endpoint. It prints a "No account?
Sign up" link but **never opens a browser on a plain login**; use `--signup` if
you want it opened. The session is stored in `auth.json` under `providers.pandora`
and refreshed automatically; the email/password (used for automatic re-login when
the token expires) are kept in the launch profile's `.env`.

### 4. Enable the toolset (only if your profile predates the install)

```bash
hermes tools enable pandora
```

Current Hermes enables the plugin's toolset as part of install/enable. On an older
profile whose saved toolset list has no `pandora`, this is the one-time fix.

### 5. Restart the two surfaces

- **Hermes Desktop** — copies `desktop/plugin.js` into `desktop-plugins/pandora/`
  at startup. Restart the app once after installing.
- `hermes gateway restart` — mounts the chip's backend route
  (`/api/plugins/pandora/*`) at gateway startup.

---

## Commands

| Command | What it does |
|---|---|
| `hermes pandora status` | Login state, account, device id, player state, output |
| `hermes pandora login` | Email + password sign-in (`--email`, `--no-browser`, `--signup`) |
| `hermes pandora logout` | Remove the session (`--keep-credentials` keeps email/password) |
| `hermes pandora setup` | Download/verify the portable mpv (`--force`, `--url`) |
| `hermes pandora capture` | PerimeterX fallback: listen for a session from your signed-in browser (`--port`, `--wait`, `--no-browser`) |
| `hermes pandora import-session` | Store a session token copied from devtools (`--token`, `--csrf`, `--username`) |

---

## The desktop status bar

The chip is injected by the desktop plugin half and polls the backend every 5 s.

- **Idle / playing:** `Artist — Track` (just `Pandora` when nothing is playing).
  The dot turns live while playing.
- **Left-click** opens the menu:
  - play / pause, restart the current track, skip
  - thumbs up / down / undo, with the active thumb filled
  - volume
  - **Stations** — your recent stations first, then the full station collection;
    the list scrolls. Click one to start it.
  - **Search** — type an artist or song (2+ characters) to get results and start
    a station from any of them.
  - **Output** — `This PC` plus every Cast device found; the one in use is bold.
    Switching output keeps the current song and its position (see below).

The menu no longer shows which plan the account is on; it only shows a capability
note when a Premium-only row is unavailable.

---

## Casting

- Devices are found over mDNS. One live `CastBrowser` is started on first use and
  closed after ten minutes without a listing, so an idle Hermes holds no
  multicast socket.
- Connecting is direct: the device's `host`/`port`/`uuid` is already known, so no
  discovery scan runs on the dial (measured ~30 ms). A discovery fallback remains
  for the rare case the direct dial fails.
- Exactly one output plays at a time. Switching devices stops the previous
  device's media first; switching back to `This PC` stops the device and resumes
  locally at the position the device reported.
- Playback uses the **Default Media Receiver**: artwork, title and artist show on
  the device, and pause/play work there. Skip, thumbs and volume are driven by
  the plugin. A custom receiver (Pandora-branded UI, on-device transport) is
  possible but not shipped; it needs a registered Cast receiver app and an
  HTTPS-hosted page.

---

## Tool reference

The agent gets seven tools. You talk naturally ("play some Vance Joy", "what's
playing", "thumbs down this", "cast to the kitchen display") and it picks the
action.

**You rarely need ids.** `pandora_stations action=play` accepts a station *name*
(exact match first, then substring) and searches the whole account, not just the
first page. "Play Adele" becomes: look for an existing station → play it; if
there is none, create one from the artist's search seed and play that. Ids from
`pandora_search` are only needed when you want a specific source.


### `pandora_playback`

| Action | Purpose | Premium? |
|---|---|---|
| `play` | Start a source (`source_id`, e.g. `ST:0:123`, `SF:16722:1360`); for a name, use `pandora_stations play` | station: no |
| `now_playing` | Current track, artist, position, output | no |
| `pause` / `resume` | Pause/resume the active output | no |
| `next` | Skip (Pandora's skip limits apply) | no |
| `previous` | Restart the current track (Pandora radio has no back-navigation) | no |
| `replay` | Play the current track from 0:00 | no |
| `set_volume` | `volume` 0–100 | no |
| `seek` | `position_seconds` | no |
| `stop` | Stop playback and clear the output | no |

On-demand sources (`TR:`/`AL:`/`PL:`) require Premium; on Free/Plus, play a
station instead or create one with `pandora_stations`.

### `pandora_search`

`query` (required), `types` (`SF`/`ST` stations, `TR` track, `AL` album, `PL`
playlist, `PC` podcast, `ART` artist), `limit` (default 10, max 50). Returns ids
usable with `pandora_playback play` and seeds for station creation.

### `pandora_stations`

| Action | Purpose |
|---|---|
| `list` | Every station on the account (paginated, up to 200) |
| `recent` | Pandora's recency list |
| `play` | Play by `station` name/id or `source_id` |
| `create` | New station from `seed_id`/`seed_ids` (from search), optional `name` |
| `add_seed` / `remove_seed` | Change a station's seeds |
| `rename` | Rename (`new_name`) |
| `shuffle` / `thumbprint` | Start Pandora's Shuffle / Thumbprint stations |

This is the fallback for on-demand requests on Free/Plus: "play me Adele" →
`list` (existing artist station?) → `play`, else `create` from the artist's
`AR:` seed → `play`.

### `pandora_feedback`

`love` (thumbs up), `ban` (thumbs down), `undo` — on the current track.

### `pandora_collection`

`list` (types, default `["ST"]`), `recent_favorites`, `favorite`, `unfavorite`
(by `id` or `name`).

### `pandora_playlists` (Premium)

`list`, `create` (`name`, optional `description`), `add` (`item_ids`),
`rename`, `set_items` (replaces the full list). Free accounts get a clear tier
message.

### `pandora_devices`

`list` (Cast devices), `set` (`device` name, or `local`), `status` (current
output). After `set`, all `pandora_playback` actions apply to that output.

---

## Free / Plus vs Premium

| Works on Free and Plus | Premium only |
|---|---|
| Stations: play, skip, thumbs, create, rename, seeds | On-demand tracks/albums/playlists (`TR:`/`AL:`/`PL:`) |
| Search (all types; playable ids are stations) | `pandora_playlists` (all actions) |
| Podcasts, collection, recent favorites | |
| Local and Cast playback, volume, seek, restart | |

The plugin never hides a control the account cannot use: the backend reports
capabilities and the menu disables what needs Premium.

---

## Scheduling: Pandora + cron

Pandora tools are ordinary Hermes tools, so cron can drive playback:

```bash
hermes cron add \
  --name "morning" \
  "0 7 * * 1-5" \
  "Cast Pandora to the kitchen display and play my 'Morning' station. Volume 40."
```

The cron session needs the `pandora` toolset enabled, and the Cast device must be
reachable when it fires. Crons run with `skip_memory=True`.

---

## Configuration

| Variable | Purpose |
|---|---|
| `PANDORA_EMAIL` / `PANDORA_PASSWORD` | Saved by `pandora login`; used for automatic re-login. Password is stored in the launch profile's `.env` (plaintext — see Security) |
| `PANDORA_API_BASE_URL` | Override the Pandora web API base (default `https://www.pandora.com`) |
| `PANDORA_MPV_PATH` | Use this mpv; wins over the downloaded build |
| `PANDORA_MPV_URL` | Portable mpv zip for `pandora setup` / first play (default: this repo's `mpv` release asset) |
| `PANDORA_MPV_SHA256` | Expected zip hash; a sibling `.sha256` is used when unset |
| `PANDORA_MPV_AUTO_DOWNLOAD` | `0` disables the first-play download (use `pandora setup`) |

---

## Where things live

| Path | Contents |
|---|---|
| `<launch home>/auth.json` → `providers.pandora` | session token, device id, account |
| `<launch home>/.env` | `PANDORA_EMAIL`, `PANDORA_PASSWORD` |
| `<launch home>/plugin-data/pandora/bin/` | downloaded mpv + its licenses |
| `<launch home>/plugin-data/pandora/diag.log` | cast/audio diagnostics (JSON lines, rotated) |
| `<launch home>/desktop-plugins/pandora/plugin.js` | the status-bar half, copied by the app |
| `<launch home>/plugins/pandora/` | the installed plugin (and `dashboard/` backend) |

The account is pinned to the launch home on purpose: every profile and bot shares
one Pandora session and device id, so there is no per-profile login.

---

## Troubleshooting

**`hermes pandora` is not a command** — the plugin is installed but disabled.
`hermes plugins enable pandora` (and `hermes tools enable pandora`).

**`404 {"detail":"Plugin not found"}` in the chip** — the app is running a
profile whose `plugins.enabled` does not include `pandora`. Hermes executes a
user plugin's backend only in a profile that enabled it. Install/enable it in the
profile the app runs (`hermes -p <profile> plugins install … --enable`), or set
`plugins.isolation: host` if you deliberately run several profiles.

**`503 The Pandora agent plugin is not loaded in this process`** — the dashboard
backend runs in the gateway, which v0.23+ isolates from plugin code. The plugin
self-loads its package for this case; if you still see it, restart the gateway.

**Login is blocked (PerimeterX / 403)** — Pandora occasionally refuses scripted
logins. Run `hermes pandora capture`, open pandora.com signed in, and paste the
printed snippet into the browser console; the session is handed to the plugin.
`hermes pandora import-session --token …` does the same by hand.

**"mpv is required for local playback"** — run `hermes pandora setup`, or set
`PANDORA_MPV_PATH`. A failed download prints the URL and expected hash; override
with `PANDORA_MPV_URL` / `PANDORA_MPV_SHA256`.

**No Cast devices listed** — the devices must be on the same LAN and reachable
over mDNS; make sure the speaker is awake and on the same Wi-Fi/VLAN. The list is
live: a device added after the browser started appears within a couple of seconds.

**Cast playback has no skip/thumbs on the device** — that is the Default Media
Receiver. The plugin controls playback from the chip; a custom receiver would add
on-device transport but is not shipped.

**Brief gap between tracks** — mpv runs with `--gapless-audio=yes`. A small gap
can remain because the next track's URL is only known after Pandora accepts the
end of the previous one.

**File logs stop on Windows** — Hermes' `concurrent-log-handler` needs `pywin32`;
without it the gateway's file logging can go quiet. The plugin writes
`plugin-data/pandora/diag.log` independently, and that is the file to check for
`dial` / `local_audio` / `cast_state_before` events.

---

## Security and privacy

- Credentials live only on your machine: session token in `auth.json`, email and
  password in the launch profile's `.env` (plaintext, as saved by
  `hermes pandora login`). Treat that `.env` as a secret; `hermes pandora logout`
  removes the session and, unless `--keep-credentials`, the saved password.
- The plugin talks to pandora.com directly. No telemetry, no analytics, no
  third-party service.
- mpv is GPLv2+ and downloaded from this repository's `mpv` release; its license
  and third-party notices ship beside the binary and in `pandora/bin/`.
- Pandora is a trademark of Pandora Media, LLC. This project is unaffiliated.
