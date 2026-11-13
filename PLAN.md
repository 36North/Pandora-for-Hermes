# Pandora Plugin for Hermes Agent — Project Plan

Owner: Daniel Bow (danie). Built by Hermes Agent.
Status: Phases 1–3 complete.
Phase 1: login → search → station → real audio (verified 2026-11-06).
Phase 2: station/playlist/collection editing + tier gating (live-verified, Free-tier
collect asymmetry documented). Phase 3: cast to Nest/Chromecast — live-verified on
all three of the user's devices (Nest Audio "Living Room speaker", Nest Hub "Kitchen
display", Cast group "Speakers"): real audio heard, PLAYING state, live-advancing
position, auto-advance on track end, clean stops. 52 tests passing. Synced to both
the default and `rob` profiles. Remaining open items: on-demand/Premium verification
(needs a Premium account), playlist-edit mutations (Premium-gated), token lifetime
under extended real use.

## 1. Goal

A Hermes plugin that lets the agent control the user's Pandora (pandora.com) music:
playback, search, stations, feedback (love/ban), and collections — via Pandora's
official GraphQL API, using the user's own Pandora account (email+password login).

User requirements (from chat):
- Playback: option B — plugin plays audio on this PC (local player, mpv).
  Option D (later phase): cast/stream to a speaker (Nest/Chromecast) like
  Pandora on a Google Nest Hub.
- Must work on ANY subscription tier (user is on Free; Premium unlocks on-demand).
- All capabilities: now-playing + play/pause/next, search & play, love/ban,
  on-demand (when tier allows).
- First use: `hermes pandora login` prompts for sign-in; a "No account? Sign up"
  line with a hyperlink below the prompt, which opens the browser to Pandora's
  sign-up page.

## 2. API research findings (verified 2026-11-05)

- Official GraphQL API, endpoint (partner): `https://ce.pandora.com/api/v1/graphql/graphql`
  with `Authorization: Bearer <token>`. The web player uses the same
  `v1/graphql/graphql` path on `https://www.pandora.com/api/` with `X-AuthToken`
  (session) + `X-CsrfToken` (cookie) headers. Same service family, same schema.
- Legacy JSON v5 API (`api.pandora.com/json/5/...`) is DEAD (host no longer
  resolves) — pydora/Pandora.js-era clients are obsolete. Do not use.
- Official partner program (developer.pandora.com) is approval-gated; we do NOT
  need it: the plugin authenticates as a normal listener via the web login flow.
- Login (scriptable, verified): `POST https://www.pandora.com/api/v1/listener/emailPassword`
  with JSON `{"username": <email>, "password": <pw>, "redirectPath": "/"}`.
  Validates email format ("not a valid email"), then credentials. Response sets
  the session (csrftoken cookie) and returns the auth token used as X-AuthToken.
- GraphQL surface (all documented at developer.pandora.com, captured locally):
  - `search(types: [SF,ST,TR,AL,PL,PC,PE,ART], query, pagination{limit,offset}) { items { id ...on T } }`
  - `playback.setSource(deviceUuid, sourceId, forceNonExplicit?)` → {id,type,current}
    (current: TrackItem{trackToken,audioUrl,interactions,feedbackValue,track{...}} or AudioAdItem)
  - `playback.current(deviceUuid, sourceId?)` → current item; also re-issues expired audioUrls
  - `playback.peek(deviceUuid, sourceId)` → next item (preloading)
  - `playback.pause/setStarted/setProgress(deviceUuid, sourceId, index, elapsedTime)` → status
  - `playback.setEnded(deviceUuid, sourceId, index, elapsedTime, reason: NORMAL|SKIPPED|...)` → status + next current
  - `playback.skip(deviceUuid, sourceId, index, elapsedTime, checkOnly?, targetIndex?)` → status + next
  - `playback.previous(...)` (Premium on-demand), `playback.replay(..., trackToken)` (Plus+),
    `playback.repeat(deviceUuid, sourceId, repeatMode: REPEAT_ALL|REPEAT_ONE|OFF)` (album sources)
  - `playback.shuffle(deviceUuid, sourceId, enabled)`
  - `feedback.setFeedback(targetId: "TR:..", sourceContextId: "ST:..", value: UP|DOWN, trackToken, deviceUuid, elapsedTime)` → status + current
  - `feedback.removeFeedback(...)` → status
  - `feedbacks(filter: {targetTypes: [TR], sourceContextTypes: [ST], values: [UP|DOWN]}, pagination)` → items
  - `collection(types: [ST,PC,PL,AL,TR...], pagination)` → items (stations, playlists, ...)
  - `recentFavorites(pagination)` → items (up to 20)
  - `listenerTemplate` → allowed search/collection types per tier (use to gate on-demand)
  - `entities(ids: [...])` → metadata lookup
  - `podcast(id, episodes(pagination), episodeGroups)`
- Playback semantics: playback is scoped to a DEVICE UUID we choose (stable per
  install). Free tier: stations + podcasts only, skip-limited, audio ads in
  queue (AudioAdItem). Audio URLs are short-lived (1h stations, 5min on-demand)
  → re-fetch via `playback.current` when mpv gets a 4xx/EOF.
- Error model: `errors[].message` ends with a `(CODE)` token (e.g.
  `(SOURCE_NOT_SET)`, `(NOT_SUPPORTED)`, `(BAD_USER_INPUT)`); match on code.

## 3. Architecture

```
hermes pandora login   ──> emailPassword API ──> auth token + csrftoken cookie
                                      │ saved to auth.json (providers.pandora)
                                      ▼
pandora_client.py (httpx GraphQL, X-AuthToken/X-CsrfToken, 401 → re-login)
        │
pandora_tools.py ──> pandora_playback / search / stations / feedback / collection
        │
pandora_player.py (mpv via IPC pipe; tracks queue position; reports
                   setStarted/setProgress/setEnded to API; refreshes expired URLs)
```

- Local player: mpv (portable, downloaded into plugin's bin/), driven over its
  IPC (Windows named pipe `//./pipe/hermes_pandora`). Handles: play URL,
  pause/resume, seek, volume, track-end detection (watch-property change),
  audio ads (played by default on Free — keep the stream ad-supported as the
  account expects).
- Device identity: stable UUID derived from Hermes install_id, persisted in
  auth state.
- Tier gating: `listenerTemplate` query tells us allowed types; on-demand
  (TR/AL/PL) actions on Free return a clean "requires Premium" message
  (same UX as the Spotify plugin's tier handling).

## 4. Tools (toolset `pandora`, opt-in)

1. `pandora_playback` — actions:
   - `play` (source_id or auto: last source) — sets source on our device, starts mpv
   - `now_playing` — track name/artist/album/art, station, position, state
   - `pause` / `resume` / `stop`
   - `next` (skip; respects skip limits, reports remaining skips on error)
   - `previous` / `replay` / `set_shuffle` / `set_repeat` / `set_volume`
2. `pandora_search` — query, types (auto by default), limit → results list with ids
   (SF stations, ST, TR, AL, PL, PC, ART)
3. `pandora_stations` — `list` (collection ST), `play` (by station name or id),
   `recent` (recentFavorites), `thumbprint`/`shuffle` station shortcuts,
   `create` (station from seed ids), `add_seed`/`remove_seed`, `rename`
4. `pandora_feedback` — `love` / `ban` / `undo` (on current track or trackToken)
5. `pandora_collection` — `list` by type, `recent_favorites`,
   `favorite`/`unfavorite` (by id or name; Free tier can remove ST but only
   re-collect via SF factory ids)
6. `pandora_playlists` — `list`/`create`/`add`/`rename`/`set_items` (Premium
   feature; free accounts get a clear tier message via listenerTemplate gating)

All tools return a shaped "not signed in — run `hermes pandora login`" error
when auth is missing (Spotify pattern).

## 5. CLI (`hermes pandora ...`, via ctx.register_cli_command)

- `hermes pandora login` — prompts email + password (getpass). Prints below the
  prompt: "No account? Sign up: https://www.pandora.com/sign-up" (URL also
  printed as a hyperlink line; when a GUI is available the sign-up page is
  opened in the default browser). Stores credentials + session token + device
  UUID in auth.json (providers.pandora), like Spotify's PKCE state.
- `hermes pandora status` — signed in as X, tier, device id, last login,
  player state.
- `hermes pandora logout` — clears credentials + token (calls API logout best-effort).
- Re-auth: token expires / 401 → auto re-login from stored credentials;
  if that fails, tools/CLI say "run `hermes pandora login` again".

## 6. Files

```
pandora-plugin/
  PLAN.md                 (this file)
  pandora/
    __init__.py           register(ctx): tools + CLI command
    plugin.yaml           manifest (standalone, toolset pandora)
    pandora_auth.py       login/status/logout, token store, re-auth
    pandora_client.py     httpx GraphQL client + error mapping
    pandora_player.py     mpv lifecycle + IPC + track-end watcher
    pandora_tools.py      tool handlers + schemas
    pandora_cli.py        `hermes pandora` subcommands
    bin/                  mpv.exe (downloaded once)
  tests/
    test_client.py, test_auth.py, test_tools.py (mocked HTTP)
```

Install: copy `pandora/` into `~\AppData\Local\hermes\plugins\pandora`,
`hermes plugins install/enable` + `hermes tools enable pandora`.

## 7. Phases

- Phase 1 (this build): plugin skeleton, auth CLI with sign-up link, GraphQL
  client, local mpv player, tools 1–5 (core actions), tests.
- Phase 2: progress heartbeat (setProgress), expired-URL auto-refresh polish,
  on-demand (TR/AL/PL) when Premium, shuffle/repeat on albums, playlist edits.
  DONE 2026-11-06: playlist/station editing + tier gating (live-verified, see §9
  item 5). Remaining: on-demand verification (needs Premium), shuffle/repeat on
  album sources.
- Phase 3 (option D): speaker output — cast stream URL to Chromecast/Nest
  (python chromecast) or pipe to a Sonos via SoCo; `pandora_devices` tool.

## 8. Risks / open items

- RESOLVED: sign-up URL is https://www.pandora.com/account/register (the SPA's
  "Sign Up" nav link); /sign-up and /signup 404. Sign-in: /account/sign-in.
- RESOLVED: mpv 0.42 portable (shinchiro build) shipped in `pandora/bin/`
  (Windows x86-64); verified playing an https audio stream locally, with
  track-end detection reporting `setEnded` correctly.
- RESOLVED: Windows named pipes are synchronous handles — a blocking read locks
  out writes on the same handle (this hung the first build). The player opens the
  pipe with `os.open` + `os.set_blocking(fd, False)` and polls non-blocking.
- OPEN: live session not yet validated — needs `hermes pandora login` with the
  user's credentials. Biggest unknowns: the exact login response shape (handled
  leniently by `_find_token`), whether the web-session GraphQL accepts all the
  documented partner projections (mitigated by the optional-field retry), and
  the web session token's lifetime (mitigated by credential re-login).
- Tier-gated actions (previous/replay/repeat/shuffle; on-demand sources) return
  Pandora's own restriction errors, surfaced verbatim.
- Using the web API with a custom device is the same behavior as the web player
  (which is a custom client); the account is the user's own. ToS note in README.
- CORS-free direct HTTP is fine for the plugin; no browser is involved at runtime.

## 9. Session state (resume here)

Source tree: `C:\Users\danie\pandora-plugin` (`pandora/` package, `tests/`, `PLAN.md`).

Live installs (synced via `bash scripts/dev-sync.sh`, which copies
into BOTH):
- `C:\Users\danie\AppData\Local\hermes\plugins\pandora` (default profile)
- `C:\Users\danie\AppData\Local\hermes\profiles\rob\plugins\pandora` (rob profile)

IMPORTANT profile fact (discovered 2026-11-06): the desktop agent session runs with
`HERMES_HOME=C:\Users\danie\AppData\Local\hermes\profiles\rob` — plugin discovery,
`hermes pandora` CLI, `hermes tools enable`, auth.json/.env are ALL per-profile.
The plugin was originally installed only in the default profile, invisible to the
agent. Fixed: plugin copied into the rob profile's plugins dir, `hermes plugins
enable pandora` + `hermes tools enable pandora` run in the rob profile (both ✓;
`hermes plugins validate` passes — warnings only: bundled mpv binaries, and
catalog-rule-9 notes for the `hermes_cli.auth` private-import seam, same convention
as the Spotify plugin). Credentials/session state therefore live in
`profiles\rob\.env` + `profiles\rob\auth.json` for the agent's tools. If the user
logs in from a plain terminal (default profile) instead, mirror the `PANDORA_*`
lines into the rob `.env` afterwards.

Tests (2026-11-06): `cd ~/pandora-plugin && PYTHONPATH="<path-to-hermes-agent>" python3 -m pytest tests -q`
→ **41 passed** (31 before Phase 2 editing; 10 new tool tests). Notes: use python3
(3.13, WindowsApps) + hermes-agent SOURCE on PYTHONPATH — the hermes venv python
lacks pytest; `ruamel.yaml` was pip-installed to python3.13's user site for the
hermes_cli.auth import chain. `python` (hermes venv) has httpx but not pytest.

Rewrite completed 2026-11-06 (auth is current with Pandora's v1/auth API):
- `pandora_auth.py`: login via `POST /api/v1/auth/anonymousLogin` (gets
  authToken/csrftoken over plain HTTP) → `POST /api/v1/auth/login` (JSON
  {email,password,csrfToken}). PerimeterX detection: 403 `1215` → raise with
  guidance to use `hermes pandora capture` (form-POST fallback + browser capture).
  Placeholder `***` password is rejected with a clear message instead of a real
  call. New: `login_url()`, `import_session(token, csrf, username)`, `login()`
  takes `password=None` → CLI getpass path. `relogin()` uses stored credentials.
- `pandora_cli.py`: subcommands `login` (getpass, sign-up link printed),
  `capture` (loopback server 127.0.0.1:18642 with a pre-bind check so a stale
  server is detected and reported), `import-session`, `status`, `logout`.
- `tests/test_auth.py`: rewritten to match (FakeClient fixture with a
  ClientFactory callable; covers anonymousLogin→login, PerimeterX 403,
  placeholder password, import_session, login_url, request shapes).

Pending (next actions, in order):
1. ~~USER ACTION: run `hermes pandora login`~~ DONE 2026-11-06 — user logged in via
   the in-app terminal (rob profile); session stored in profiles/rob auth.json.
2. ~~Headless validation~~ DONE 2026-11-06 — all PASS via the plugin's own client
   methods (scratch `pandora_validate.py`): listenerTemplate, search("Adele"),
   collection(ST), playback_set_source, playback_current returned a real streaming
   audioUrl. Live schema fixes applied to `pandora_client.py`:
   - `listenerTemplate` requires `template: DEFAULT` (only enum value).
   - search/collection `types` args are ENUM lists → bare tokens `[SF, ST, ...]`,
     not quoted strings (new `_enums()` helper).
   - Artist search type is `AR` in the live schema (not `ART`; `ART` kept as alias).
   - `setSource` returns `IPlaybackSource` (id/type/current) — item fragments can't
     be spread on it; first item is projected via `current { TRACK_SELECTION }`.
   - Account tier: FREE (search/collection = PC,PE,SF,ST / PC,PE,ST).
   `hermes pandora status` now reports clean signed-in state, no warnings.
3. ~~Play actual audio through the plugin player~~ DONE 2026-11-06 — `pandora_listen2.py`
   drove `PandoraPlayer.play_source("SF:17231:21727")` (Adele station factory):
   resolved to ST:0:98645986368314920, played "Make You Feel My Love" (Adele,
   212s), mpv `position_seconds` advanced 0→30 in real time, clean stop+shutdown.
   **Phase 1 complete end-to-end: login → search → station → real audio.**
   Remaining: Phase 2 (on-demand for Premium, playlist edits, progress polish)
   and Phase 3 (cast to Nest/Chromecast — option D). Note: pandora tools are
   enabled for the agent but only load in a NEW session (gateway tool registry
   is fixed at session start); until then, drive playback via
   `pandora.pandora_player.PandoraPlayer` directly.
4. Watch token lifetime in real use (open item): on 401 → relogin from stored
   credentials; if credentials also fail, surface the capture flow
   (`hermes pandora capture`).
5. Phase 2 open items — DONE 2026-11-06 (all live-verified on the Free account):
   - Token lifetime: re-login from stored credentials verified live in the rob
     profile (listenerTemplate succeeds after relogin); extended-use watching
     continues as item 4.
   - Progress heartbeat: `pandora_player.py` now adjusts `started_at` on resume
     so paused time is excluded from setProgress/setEnded reports.
   - Station editing (new client methods + `pandora_stations` actions
     create/add_seed/remove_seed/rename): full cycle verified LIVE —
     createStationFromSeeds([AR:269367]) → ST:0:117990793947406761,
     addStationSeed(AL:145677) OK, removeStationSeed OK, setStationDetails
     rename OK, uncollect(delete) OK. Test station deleted; account net-zero.
   - `pandora_collection` favorite/unfavorite: collect()/uncollect() verified.
     DISCOVERED Free-tier asymmetry: uncollect accepts ST ids but collect only
     accepts SF/AL/AR/TR/PC/PE/PL (server error: "Only the following types can
     be collected: ..."). Incident during test: "60s French Pop Radio"
     (ST:0:3421411669827148090) was uncollected and could not be re-collected
     by ST id — restored via its factory collect(SF:18245:802), verified back in
     the collection (50 stations). The tool docs tell the model to prefer SF ids.
   - `pandora_playlists` (new tool, list/create/add/rename/set_items):
     createPlaylist/addToPlaylist on Free fail server-side with a generic
     "An error occurred." (no useful code). Tool layer detects the missing PL in
     listenerTemplate.collectionTypes and returns a clear
     "Playlists require a Premium Pandora account" message instead.
     Premium-path behavior (real playlist CRUD) remains unverified on this
     account.
   - Tests: 31 → 41 (10 new tool tests: station editing, favorites, playlist
     tier gating + premium-path fakes). `PYTHONPATH=.../hermes-agent python3 -m
     pytest tests -q` → 41 passed. Synced to both profile installs.
6. Phase 3 (next): cast to Nest/Chromecast. Needs: device discovery on the
   user's network (which Cast devices exist), chromecast pip package choice
   (castv2/pychromecast), URL-handoff design (short-lived Pandora URLs vs
   Cast's 24h limit; re-cast on expiry), and a `pandora_devices` tool
   (list/switch output). Also verify on-demand (TR) playback behavior on Free
   when the user next plays a specific song.
7. Phase 3 DONE 2026-11-06: cast to Nest/Chromecast, live-verified on all three
   of the user's devices.
   - Stack: pychromecast 14.1.1 (runtime venv), `pandora/pandora_cast.py`
     (discovery + case/whitespace-insensitive name matching + media control +
     status), `pandora_devices` tool (list/set/status), player `output` state
     routing controls through the cast path.
   - Verified LIVE (real audio heard by user on each): Nest Audio "Living Room
     speaker" (192.168.0.173), Nest Hub "Kitchen display", Cast group "Speakers"
     (group, 192.168.0.232:32071). Each: connected, PLAYING, position advancing
     linearly, auto-advance on track end (media_session_id steps one-per-track,
     no runaway), clean stop.
   - Runaway auto-advance fix: the pre-load IDLE the device pushes before a new
     media load "starts" was being treated as a track end, so each end re-loaded
     before the device finished buffering — a self-sustaining spin (player raced
     to index 121+ while the device stayed IDLE). Fixed with a `_cast_was_active`
     flag: IDLE is treated as track end ONLY after the device has reported
     PLAYING/BUFFERING since the last load.
   - pychromecast v14 quirks (all handled in `pandora_cast.py`):
     * `wait()` returns None → poll `socket_client.is_connected` on a deadline.
     * `Chromecast.close()` renamed `disconnect()` → `_cc_close()` helper.
     * `is_connected` moved to `socket_client` → `_cc_connected()` helper.
     * browser stores zeroconf as `.zc` (not `.zeroconf`).
     * `CastInfo` requires `cast_type` and `manufacturer` (pass None).
     * media controller: `play_media(...)` (not `load()`), and
       `media_controller.status` is NOT refreshed on each push — pushed statuses
       only reach registered listeners. Status listener protocol is OBJECT-based:
       v14 calls `listener.new_media_status(status)`, so the capture must be an
       object (registering a bare function → AttributeError).
     * Devices push MediaStatus only on state TRANSITIONS (no periodic
       heartbeats) and v14's MediaStatus omits `play_duration`/`metadata` on many
       pushes. So live `current_time` is computed by local extrapolation: baseline
       on each push, advance the clock while PLAYING, clamp to the loaded
       duration, re-sync on the next push. Volume read-back is unreliable on
       Nest Audio, so `status()` falls back to the last volume we commanded.
   - Tests: 41 → 52 (cast discovery/matching/controls + player cast routing +
     pre-load-IDLE guard + object-listener). Synced to both profile installs.
8. Bundled usage instructions shipped 2026-11-06 (so any installer's AI gets
    them, not just danie's personal skill):
    - NEW `pandora/skills/pandora-usage/SKILL.md` — a plugin-bundled skill
      (registered as `pandora:pandora-usage` for every installer) carrying the
      playback policy: on-demand (TR/AL/PL) is Premium-only; below Premium, a
      song/artist request falls back to an existing artist station (list/play)
      else create from the artist's `AR:` seed (search ART → create → play) and
      tell the user; NEVER auto-skip in a loop — leave the station playing and
      ask before skipping (Free/Plus skip quota); sign-in via `hermes pandora
      login` only; output routing via `pandora_devices`.
    - Short tier reminders added to the `pandora_playback`, `pandora_search` and
      `pandora_stations` tool descriptions (visible at tool-selection time).
    - `plugin.yaml` provides_tools: added `pandora_playlists` (fixes the doctor
      WARN "registration adds tool 'pandora_playlists' not listed in
      provides_tools").
    - `scripts/dev-sync.sh` now also copies `README.md` and `skills/` to both live installs;
      README gained a "Playback policy" section.
    - PITFALL (cost ~20 min): plugin-bundled skill frontmatter validation
      (`_valid_skill_frontmatter` / diagnostics in `hermes_cli/agent_plugins.py`)
      requires `metadata` to be a FLAT string→string map — nested
      `metadata.hermes.tags: [...]` produces `metadata must map string keys to
      string values` and the skill is dropped from registration. On-disk profile
      skills accept rich metadata; plugin skills do not. Use `metadata: {tags:
      "a, b, c"}`.
    - Verify skill discovery from a scratch process: import
      `hermes_cli.agent_plugins._discover_skills(Path(plugin_root), diags)` —
      both live installs report `pandora-usage` with no diagnostics.
    - Tests: 53 passing (Plus/replay pass). Synced to both profile installs.
9. Session notes 2026-11-06 (live playback debugging, also in the rob profile
    skill `pandora-plugin` → "Player / IPC debugging"):
    - `_write_bytes` in `pandora_player.py` retries `BlockingIOError` forever
      (no deadline) — a wedged mpv IPC pipe hangs a driver silently (no log
      after mpv launch). Diagnose: open the pipe yourself, `get_property
      idle-active` (true = loadfile never arrived).
    - The named pipe is SINGLE-CLIENT and per-name: stale client handles from
      killed processes wedge the shared `hermes_pandora` name — scratch drivers
      must use a fresh pipe name (monkeypatch
      `pandora_player.IPC_PIPE_NAME`; read at call time) and kill BOTH the
      driver python and its mpv on restart.
    - A last-resort minimal player works around a wedged plugin player: bundled
      `bin/mpv.exe` + `--input-ipc-server=<fresh pipe>`, deadline-capped IPC
      writes, reader thread, `loadfile <audioUrl>` per item, advance via
      `playback_ended` + `playback_current`, command-file protocol
      (NEXT/PAUSE/RESUME/STOP) — proven live (Adele Radio played this way).
    - Scratch-process gotchas: run ad-hoc pipe scripts with `python3` (3.13) —
      the hermes venv `python.exe` is a degraded shim missing `os.set_blocking`;
      a bare scratch process that may re-login must first call
      `set_secret_scope(build_profile_secret_scope(<profile home>))`.
    - Selection-set GraphQL validation errors (`Cannot query field "X" on type
      "YResponse"`, seen on `skip`/`setEnded`) are schema-projection bugs —
      introspect first; a clean scratch client can behave differently from the
      long-lived tool process (stale in-process state). OPEN: `SkipResponse`
      projection fix not yet applied to `pandora_client.py` (see scratch probes
      in the rob profile cache).
10. Release hygiene + chip diagnostics 2026-11-12 (pre-publication pass):
    - No profile coupling anywhere in the package: greps for a profile name,
      `C:\Users`, `AppData` and `/profiles` path building return nothing under
      `pandora/**`; home resolution stays on `get_hermes_home()`,
      `get_default_hermes_root()` and `get_provider_auth_state()` (whose store
      already falls back from the profile to the global root). The only
      personal strings left in the tree were documentation.
    - README (repo root and the shipped `pandora/README.md`) 404 explanation
      now says "when the app runs a named profile" instead of one person's
      profile name; PLAN.md's test command uses
      `PYTHONPATH="<path-to-hermes-agent>"`.
    - `sync.sh` → `scripts/dev-sync.sh`: DEV-HELPER header (not for end users),
      repo root derived from `${BASH_SOURCE[0]}`, `SRC`/`ROOT` from
      `PANDORA_DEV_TREE` / `HERMES_ROOT` with Windows-then-`~/.hermes`
      autodetect (exit 2 with a message when neither exists), skips
      dot/tombstone profile dirs, and warns before overwriting a target that
      carries `.hermes-origin.json` / `.hermes-catalog.json` provenance.
      `bash -n` clean; guards exercised with a bogus `PANDORA_DEV_TREE`
      (exit 2, nothing written) and with `HERMES_ROOT` overridden.
    - Desktop half: the 404 banner now names the profile the request was
      scoped to and prints the exact fix. `host.state.profile` ("profile the
      live gateway is routed to" — the scope `ctx.rest` carries) feeds
      `notInstalledMessage(profile)`, kept pure between
      `BEGIN/END not-installed-message` markers so it tests without the app.
      List rows reworded to `Not available in profile "<name>"`. Empty
      profile → no `-p` flag (asserted, not assumed). Verified with a node
      harness over 5 profile cases plus an ESM `node --check`; the renderer
      loads the file with no load failure in `logs/desktop.log`.
    - PITFALL (the release trap): a user plugin's dashboard API is mounted per
      profile while the desktop chip is app-level, so
      `hermes plugins install` without `-p` lands in the launch (default) home
      and the chip answers 404 in the profile the app actually runs. Store
      installs target the active profile and are unaffected; CLI users must
      pass `-p <their profile>`. Proven route state after a correct install:
      `GET /api/plugins/pandora/state` → 200 with `logged_in: true`, `volume:
      70`, real stations — verified by mounting `dashboard/plugin_api.py`
      under a throwaway dashboard with `HERMES_DASHBOARD_SESSION_TOKEN`
      (gateway tickets are purpose-bound and not mintable from the CLI).
    - PITFALL: `hermes plugins validate <dir>` is NOT read-only in this
      install — it ran a source-update dependency pass, stopped the running
      desktop app to free the build output and rebuilt it. Don't run it
      mid-session; a rebuild restarts the app under you.
11. Kill-on-close across app exit 2026-11-12:
    - The full uninstall exposed the gap: mpv's kill-on-close job is owned by
      the GATEWAY process, which outlives the Desktop window, so closing the
      app left an idle `mpv.exe` alive and a full wipe stranded on its locked
      files. Closing the app must kill the player; shipped:
      a) Desktop half: `register()` listens for `pagehide`/`beforeunload` and
         fires a fire-and-forget `POST /shutdown` (never awaited — the
         renderer dies right after; Chromium's network service still hands
         the request to the gateway, a separate process).
      b) Backend: new `POST /api/plugins/pandora/shutdown` calls
         `player.shutdown()` — `stop()` alone only silences mpv and leaves
         the process running — and invalidates the state cache. The 503 for a
         process without the plugin propagates unchanged; a teardown error
         is reported as `{"ok": false}` instead of raised.
      c) Wipe script: stops the plugin's own mpv (matched by exe path under
         `plugin-data\pandora\bin`) before deleting, so it can never strand
         the three locked files again.
    - Tests: 4 new in `tests/test_dashboard_shutdown.py` (teardown called,
      cache invalidated, 503 propagated, failure reported).
    - Full wipe run 2026-11-12 while the desktop app was open: the mpv kill
      worked (stopped pid 11712, `plugin-data\pandora` + `desktop-plugins`
      deleted), but the running app re-provisioned within minutes (desktop
      half re-copied at +4 s from the still-locked plugin tree, mpv build
      re-downloaded at +2 min by the still-running backend), and the
      leftover root-home gateway + its plugin host locked
      `plugins\pandora`. Added a pre-flight warning when Hermes.exe is
      still running; deterministic finish = close app → `hermes gateway stop`
      → re-run.
