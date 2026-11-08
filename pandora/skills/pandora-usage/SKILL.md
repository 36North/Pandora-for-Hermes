---
name: pandora-usage
description: "Use when asked to play, search, skip, or control music on the user's Pandora account via the pandora tools."
metadata:
  tags: "pandora, music, playback"
---

# Pandora Playback Usage

Instruction set for any AI operating this plugin. Follow the **playback policy** — it prevents the most common failure (asking an account for on-demand songs its tier doesn't have) and the worst UX (skip-scanning loops).

## Tools

| Tool | What it does |
|---|---|
| `pandora_playback` | `play` (source id), `now_playing`, `pause`, `resume`, `next`, `previous`, `replay`, `stop`, `set_volume`, `seek` |
| `pandora_search` | Search stations (SF/ST), tracks (TR), artists (AR), albums (AL), playlists (PL), podcasts (PC) → ids |
| `pandora_stations` | `list`, `play` (by name or id), `recent`, `shuffle`, `thumbprint`, `create` (from seed ids), `add_seed`/`remove_seed`, `rename` |
| `pandora_feedback` | `love` / `ban` / `undo` on the currently playing track |
| `pandora_collection` | List the collection by type, `recent_favorites`, `favorite`/`unfavorite` |
| `pandora_devices` | Route output to a Cast/Nest device or `local` (mpv on this machine); `list` / `set` / `status` |

Source ids: stations are `ST:…` (own) or `SF:…` (public factory); tracks `TR:…`; artist seeds `AR:…`.

## Playback policy (follow in order)

1. **On-demand is Premium-only.** Playing a specific song/track (`TR:`), album, or playlist requires a Premium account. Before playing on-demand, check the tier:
   - `pandora_search` for the requested item — on Free/Plus accounts the search simply returns no `TR` results for on-demand play (the account's `listenerTemplate` has no `TR` in `searchTypes` and no `PL` in `collectionTypes`), and `pandora_playback play` with a `TR:` id returns `FORBIDDEN`. That response is **correct for the tier**, not a bug, session lag, or auth problem — do not chase re-logins.
2. **On-demand not available → fall back to an artist station, in this order:**
   a. **Existing station first:** `pandora_stations action=list` — if the listener already has a station for that artist (or close, e.g. "Adele"), play it: `pandora_stations action=play station="<name>"`.
   b. **Otherwise create one:** `pandora_search query="<artist>" types=["ART"]` → take the `AR:<id>` seed → `pandora_stations action=create seed_ids=["AR:<id>"] name="<Artist> Radio"` → `pandora_stations action=play station_id="<new ST id>"`.
   c. **Tell the user what happened:** "Your plan (Free/Plus) doesn't allow on-demand songs, so I started <Artist> Radio instead." Never silently substitute.
3. **Never auto-skip in a loop.** Station queues deliberately mix in similar artists — the first item after starting an artist station may not be by that artist. If the current track isn't what the user asked for: leave the station playing and **ask** whether they'd like you to skip forward. Free/Plus skip quota is limited and user-controlled by design.
4. **Not signed in:** the tools return a shaped sign-in error. Direct the user to run `hermes pandora login` in the in-app terminal (email + password prompt; a sign-up link is printed). Never ask for or accept a password in chat.
5. **Output routing:** default is local mpv on this machine. If the user names a speaker ("on the kitchen display"), use `pandora_devices action=set device="<name>"` first; device matching is case/whitespace-insensitive.

## Quick flows

- "Put on some Adele" / "play me a song by X" → policy step 2 (search → existing station → create → play → report).
- "Play my Shuffle station" → `pandora_stations action=shuffle` (or `play` by name).
- "What's playing?" → `pandora_playback action=now_playing`.
- "Skip" / "I don't like this" → `pandora_playback action=next` / `pandora_feedback action=ban`.
- "Louder" / "pause for a minute" → `pandora_playback action=set_volume` / `pause`.

## Verification

A `play` call succeeding means the source is queued and audio is handed to mpv — confirm with `now_playing` (track name + position) before telling the user what's playing. If `now_playing` shows a different track than expected, the queue may still be filling (stations pre-buffer); report what's actually playing.
