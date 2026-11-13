# Pandora for Hermes

![Pandora in the Hermes status bar](../assets/hero.jpg)

Your music. Your desktop. Your agents. All connected.

Pandora for Hermes brings your Pandora experience directly into Hermes Desktop. Fire up a favorite station, discover something new, and keep the music flowing while you work.

Everything is right where you need it. See the current song and artist in your status bar, then click the chip to control playback, skip tracks, rate songs, adjust volume, switch stations, browse recent stations, or change audio outputs. Search for an artist or song and instantly spin up a new station. If you have a Premium account, enjoy on demand playback and access to your playlists. You can even cast your music to compatible media devices on your network.

But this plugin goes beyond desktop playback. Its seven built-in agent tools let Hermes interact with Pandora directly through the CLI, making it just as useful for autonomous agents and headless environments as it is for everyday desktop listening.

Supports Pandora Free, Plus and Premium.

Powered by mpv for local playback. The plugin can automatically download a verified portable build, or you can install it yourself using `hermes pandora setup`.

> This plugin is unofficial and community-maintained. It is not affiliated with,
> endorsed by, or supported by Pandora. It talks to Pandora through the same web
> API the pandora.com player uses, with your own account — no API key, no OAuth
> app, no third-party server — and Pandora's private API can change at any time,
> which is why a Pandora-side change can break it. Issues and fixes are welcome.

> **Terms of Use.** This plugin uses automated access to pandora.com that may
> violate Pandora's Terms of Use; use at your own account risk.

MIT licensed — see [LICENSE](LICENSE) and [Legal](#legal).

---

## Setup

Plugins install **per profile**: the agent tools and the chip's backend exist only in a profile where the plugin is enabled.

> **Specify the profile explicitly.** Add `-p <profile>` to every command
> below — `<profile>` is the one your desktop app runs (shown in the app, and
> in the chip's "backend not installed in profile '<profile>'" message if you
> ever see it). On first launch the app auto-detects a profile and sticks
> with it, so a bare install — which lands in the *default* profile — often
> ends up in the wrong place. Running the default profile? The `-p` flag is
> optional there.

### 1. Store

Install **Pandora** from **Capabilities → Plugins** in Hermes Desktop. The app installs the plugin into the profile it is running and adds the status-bar chip immediately.

```bash
hermes -p <profile> pandora setup   # optional: pre-fetch the SHA-256-verified mpv build (shared across profiles)
hermes -p <profile> pandora login   # sign in to Pandora with your email + password
```

### 2. CLI only

```bash
hermes -p <profile> plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable
hermes -p <profile> gateway restart   # if the installer says the gateway needs it
hermes -p <profile> pandora setup
hermes -p <profile> pandora login
hermes -p <profile> tools enable pandora
```

Install the plugin (with its `pychromecast` dependency), fetch mpv, sign in,
then enable the toolset once. Restart the gateway only if the installer says
so — until it happens the plugin's tools stay unloaded, and the seven agent
tools then work from the terminal and with the agent, no desktop app required
(see [CLI-only use](#cli-only-use-no-desktop-app)).

### 3. Full Plugin

```bash
hermes -p <profile> plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable
hermes -p <profile> gateway restart   # if the installer says the gateway needs it
hermes -p <profile> pandora setup
hermes -p <profile> pandora login
hermes -p <profile> tools enable pandora
```

The same five commands, run for the profile the desktop app runs (see the
note above). Then restart Hermes Desktop (which reloads that profile's
gateway) or **Rescan** in Capabilities → Plugins, and enable the **Pandora**
toggle in that panel — the chip appears in the status bar.

### 4. Uninstall

```bash
hermes -p <profile> pandora logout      # delete the session and saved credentials
hermes -p <profile> plugins remove pandora   # or the trash icon in Capabilities → Plugins
```

That keeps your login and the mpv build, so a reinstall starts signed in. For a complete wipe — credentials, mpv build, desktop half, and the plugin itself from every profile — close Hermes Desktop and run the bundled `scripts\full-wipe-pandora-plugin.ps1` from the installed plugin folder **while the plugin is still installed**: the script performs the uninstall itself, so run it instead of the two commands above (if you already uninstalled, get the script from this repository). Details: [Uninstall](#uninstall).

---

## Prerequisites

- A Pandora account (free signup is fine).
- Hermes Agent **0.21.6 or newer**.
- **mpv** for local playback. It is **not bundled** — a bundled executable trips
  the installer's binary scan. Get it one of three ways:
  - `hermes pandora setup` — downloads the **official** mpv build from
    `mpv-player/mpv` (release `v0.42.0`, Windows x86_64), verifies the SHA-256
    pinned in the code, and installs it into the plugin's data directory
    (recommended);
  - let the plugin fetch it automatically the first time playback needs it;
  - point `PANDORA_MPV_PATH` at an mpv you already installed (or install
    `shinchiro.mpv` with winget and leave the path unset — it is found on PATH).
- For casting: a Cast device on the **same LAN** — Nest Audio, Nest Hub, a Cast
  group, or anything the Google Home app can see.

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
  - **Closing the app stops the music** — on exit the chip fires the backend's
    `/shutdown`, which kills the mpv child process (and its kill-on-close job).
    The job is owned by the gateway process, which can outlive the window;
    without this hook, closing the app would leave a player running with
    nothing to drive it.

The menu no longer shows which plan the account is on; it only shows a capability
note when a Premium-only row is unavailable.

---

## CLI-only use (no desktop app)

Everything functional works from a terminal: the agent tools, the `hermes pandora`
commands, local playback and Cast. The status-bar chip is the only desktop-only
piece, and the plugin does not need it.

Install it per [CLI only, above](#2-cli-only). Then talk to the agent (`hermes`),
or query state directly:

```bash
hermes pandora status
```

What you ask for is what runs — "play Vance Joy", "what's playing", "skip this",
"thumbs down", "cast to the living room speaker". The seven tools and their
arguments are in [Tool reference](#tool-reference).

Two behaviours to know before relying on it:

- **Local playback is tied to the Hermes process.** mpv runs as a child in a
  kill-on-close job object, so quitting or killing the CLI stops the audio — a
  deliberate guard against orphaned players. For playback that outlives a
  terminal session, keep a bare `hermes gateway` running headless: the desktop
  app keeps playback alive only while it is open, because its exit hook stops
  it (see [The desktop status bar](#the-desktop-status-bar)). A Cast session is
  different: the device fetches and plays the stream itself, so it can keep
  playing after Hermes exits, with nothing left to control it.
- **Audio plays on the machine running Hermes**, not the machine you type on.
  Over SSH that means the sound comes out of the remote host.

Windows remains the supported platform for local playback (mpv over a named pipe).

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

| Action | Purpose |
|---|---|
| `play` | Start a source (`source_id`, e.g. `ST:0:123`, `SF:16722:1360`); for a name, use `pandora_stations play` |
| `now_playing` | Current track, artist, position, output |
| `pause` / `resume` | Pause/resume the active output |
| `next` | Skip (Pandora's skip limits apply) |
| `previous` | Restart the current track (Pandora radio has no back-navigation) |
| `replay` | Play the current track from 0:00 |
| `set_volume` | `volume` 0–100 |
| `seek` | `position_seconds` |
| `stop` | Stop playback and clear the output |

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

## Uninstall

**Desktop:** Capabilities → Plugins → pick the profile you installed into → the
**trash** icon next to **Pandora for Hermes** → confirm. That removes the
plugin, its install record and the status-bar half. The on/off switch beside it
only disables the plugin — the files stay.

**CLI:** `hermes plugins remove pandora` — one home at a time (the profile
selector in that same panel shows which profile has it).

Uninstalling deliberately keeps your saved login and the downloaded mpv build,
so reinstalling starts where you left off. To remove **everything** — the saved
session (`providers.pandora` in `auth.json`), the `PANDORA_*` values in `.env`
and the managed env layer, `plugin-data\pandora` (mpv, ~120 MB) and the desktop
half — close Hermes Desktop and run the full-wipe script that ships with the
plugin **while the plugin is still installed** (it uninstalls the plugin from
every profile itself — run it instead of the steps above, not after them).
From a plain PowerShell window, pointing at the installed plugin folder
(`%LOCALAPPDATA%\hermes\plugins\pandora\scripts\` for the default profile, or
`profiles\<name>\plugins\pandora\scripts\` for a named one):

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\plugins\pandora\scripts\full-wipe-pandora-plugin.ps1"
```

It asks for confirmation, backs up every file it edits (`.bak-<timestamp>`), and
then uninstalls the plugin from every profile on the machine. It also stops the
plugin's own mpv first (closing the Desktop window does not always stop the
gateway, whose kill-on-close job keeps mpv alive) — if `PANDORA_MPV_PATH`
points at a different mpv, stop that player first. Anything still locked (an
open log) is listed; close Hermes Desktop and re-run (it is idempotent) to
finish.

## Configuration

| Variable | Purpose |
|---|---|
| `PANDORA_EMAIL` / `PANDORA_PASSWORD` | Saved by `pandora login` into the **active profile's** secret scope (`.env`, or your external secret source). Put them in the managed env layer instead and every profile shares one login |
| `PANDORA_API_BASE_URL` | Override the Pandora web API base (default `https://www.pandora.com`) |
| `PANDORA_MPV_PATH` | Use this mpv; wins over the downloaded build |
| `PANDORA_MPV_URL` | Portable mpv zip for `pandora setup` / first play (default: the official `mpv-player/mpv` v0.42.0 Windows build, SHA-256 pinned in code) |
| `PANDORA_MPV_SHA256` | Expected zip hash; a sibling `.sha256` is used when unset |
| `PANDORA_MPV_AUTO_DOWNLOAD` | `0` disables the first-play download (use `pandora setup`) |

---

## Where things live

| Path | Contents |
|---|---|
| `<active profile>/auth.json` → `providers.pandora` | session token, device id, account — per profile (Spotify convention) |
| `<active profile>/.env` | `PANDORA_EMAIL` + `PANDORA_PASSWORD` (written by `pandora login`) |
| `$HERMES_MANAGED_DIR/.env` | **optional** — the same two values here serve every profile, so one login covers all of them |
| `<launch home>/plugin-data/pandora/bin/` | downloaded mpv + its licenses (shared across profiles) |
| `<launch home>/plugin-data/pandora/diag.log` | cast/audio diagnostics (JSON lines, rotated) |
| `<launch home>/desktop-plugins/pandora/plugin.js` | the status-bar half, copied by the app |
| `<launch home>/plugins/pandora/` | the installed plugin (and `dashboard/` backend) |
| `<profile>/plugins/pandora/scripts/full-wipe-pandora-plugin.ps1` | full-uninstall script, ships with the plugin |

Credentials resolve per profile through Hermes's secret scope (`.env`, any
external secret source, and the managed env layer). A profile without a saved
session re-logs in automatically on first use when the credentials are
resolvable — so with the managed env set once, there is no per-profile login and
no shared session file. Each profile gets its own Pandora "device" id.

---

## Troubleshooting

**`hermes pandora` is not a command** — the plugin is installed but disabled.
`hermes plugins enable pandora` (and `hermes tools enable pandora`).

**The `pandora` toolset is missing from an older profile** — current Hermes adds
the toolset during install/enable. On a profile created before the plugin existed,
run `hermes tools enable pandora` once.

**`404 {"detail":"Plugin not found"}` in the chip** — the app is running a
profile whose `plugins.enabled` does not include `pandora`. Hermes executes a
user plugin's backend only in a profile that enabled it. Install/enable it in the
profile the app runs (`hermes -p <profile> plugins install … --enable`), or set
`plugins.isolation: host` if you deliberately run several profiles.

**The Plugins page says Pandora is installed, but the chip 404s** — that page
reflects the package (and the app-level desktop half), not whether the active
profile's backend loaded it. Check
`profiles/<profile>/plugins/pandora/plugin.yaml` and the profile's
`plugins.enabled`; a missing package or enable flag is the cause.

**The chip does not appear after installing** — two things can be missing.
The app copies `desktop/plugin.js` into `desktop-plugins/pandora/` at startup or
on **Rescan** (a store install does the copy itself) — but the desktop half is
an opt-in toggle, so the chip shows only once the **Pandora** entry is enabled
in **Capabilities → Plugins** for the profile the app is running. Flip that
switch and the chip appears live; no further restart needed.

**`503 The Pandora agent plugin is not loaded in this process`** — the dashboard
backend runs in the gateway, which v0.23+ isolates from plugin code. The plugin
self-loads its package for this case; if you still see it, restart the gateway.

**Login is blocked (PerimeterX / 403)** — Pandora occasionally refuses scripted
logins. Run `hermes pandora capture`, open pandora.com signed in, and paste the
printed snippet into the browser console; the session is handed to the plugin.
`hermes pandora import-session --token <token>` does the same by hand.

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

- Credentials live only on your machine and flow only through Hermes's secret
  scope: the session token in the profile's `auth.json`, and
  `PANDORA_EMAIL`/`PANDORA_PASSWORD` in the profile's `.env` (or an external
  secret source you configure — the plugin reads it via `get_secret`, exactly
  like every other Hermes plugin credential). The plugin keeps no credential
  store of its own and never touches another profile's files. `hermes pandora
  status` reports `Creds: env` (a `(from <source>)` suffix names an external
  secret source). `hermes pandora logout` removes the session and the stored
  password. For at-rest encryption, use an external secret source (e.g. a
  Bitwarden-backed source); the value is then never written to a file at all.
- The plugin talks to pandora.com directly. No telemetry, no analytics, no
  third-party service.
- mpv is GPLv2+ and downloaded from the official `mpv-player/mpv` release; its
  license and third-party notices ship beside the binary and in `pandora/bin/`.
- PANDORA and related marks belong to Pandora Media, LLC. This project is
  unaffiliated.

---

## Legal

**License.** MIT — see [LICENSE](LICENSE). Use, modify, redistribute and sell it;
keep the copyright and permission notice with copies of the software. If you plan
to sell or relicense the project, note what MIT already permits: anyone who
receives the code may also use it commercially. A sale transfers the copyright
(and the right to relicense future versions); it does not withdraw MIT rights
already granted to anyone else.

**Trademarks.** PANDORA and related marks are trademarks or registered trademarks
of Pandora Media, LLC. All other trademarks are the property of their respective
owners.

**Not affiliated.** This is an independent client and is not affiliated with,
endorsed by, sponsored by, or supported by Pandora. The name and the word
"Pandora" are used only to say what the software interoperates with (nominative
use); no Pandora logo, artwork or audio is bundled or redistributed. Audio is
streamed from Pandora directly by your own player and is not stored by the
plugin.

**How it talks to Pandora.** The plugin signs in with your own account and calls
the same private web GraphQL endpoint that pandora.com's player uses. There is no
API key and no official partner access. Pandora's Terms of Use may not permit
third-party clients, so using this plugin is at your own risk — including the risk
that an account is rate-limited or actioned. The plugin does not bypass DRM or
plan limits: on-demand playback works only when your account's plan allows it,
and the ad items Pandora serves on ad-supported accounts are played, not stripped.

**"Fair use".** Fair use is a defence in US copyright law, not a permission a
notice can grant. What this project actually relies on is narrower and concrete:
it does not copy or redistribute Pandora's copyrighted content, it uses the name
only to identify compatibility, and it runs with the user's own credentials on
their own machine. That describes the design; it is not legal advice.

**mpv.** Local playback uses mpv, which is GPLv2+ and is redistributed as a
separate portable archive from this repository's `mpv` release. Its license and
third-party notices ship beside the binary and in `pandora/bin/`. The MIT license
above covers this plugin, not mpv; mpv's source is available from its upstream
project.

**No warranty.** The software is provided "as is", without warranty of any kind,
as set out in the MIT license.
