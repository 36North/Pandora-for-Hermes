// Pandora status-bar plugin (desktop half of the unified package).
//
// Shows "Artist — Song" in the status bar; left-click opens the Pandora menu.
// The menu is plan-aware: the backend reports what the account can do and the
// premium-only sections stay disabled (with the reason) otherwise.
import {
  Button,
  Codicon,
  GlyphSpinner,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'pandora'
const POLL_MS = 5000

const CSS = `
.pandora-bar { display: flex; align-items: center; height: 100%; min-width: 0; }
.pandora-trigger { display: inline-flex; align-items: center; gap: 6px; height: 100%; min-width: 0; max-width: 300px; padding: 0 6px; border: 0; background: transparent; color: var(--ui-text-tertiary); font-size: 0.6875rem; line-height: 1; cursor: pointer; }
.pandora-trigger:hover { background: var(--ui-control-hover-background); color: var(--foreground); }
.pandora-dot { flex: none; width: 6px; height: 6px; border-radius: 9999px; background: var(--ui-text-quaternary); }
.pandora-dot.is-live { background: var(--ui-accent); }
.pandora-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pandora-panel { width: 330px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.pandora-head { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pandora-track { font-size: 0.8125rem; font-weight: 600; color: var(--ui-text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pandora-artist { font-size: 0.6875rem; color: var(--ui-text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pandora-plan { font-size: 0.625rem; color: var(--ui-text-quaternary); }
.pandora-row { display: flex; align-items: center; gap: 4px; }
.pandora-row.spread { justify-content: space-between; }
.pandora-sep { height: 1px; background: var(--ui-stroke-secondary); margin: 2px 0; }
.pandora-label-sm { font-size: 0.625rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ui-text-quaternary); }
.pandora-list { max-height: 180px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
.pandora-search-results { max-height: 240px; }
.pandora-item { display: flex; align-items: center; gap: 6px; width: 100%; padding: 4px 6px; border: 0; border-radius: 4px; background: transparent; color: var(--ui-text-secondary); font-size: 0.75rem; text-align: left; cursor: pointer; }
.pandora-item:hover { background: var(--ui-control-hover-background); color: var(--foreground); }
.pandora-item.is-active { color: var(--foreground); font-weight: 600; }
.pandora-item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pandora-progress { height: 3px; border-radius: 9999px; background: var(--ui-stroke-secondary); overflow: hidden; }
.pandora-progress-fill { height: 100%; background: var(--ui-accent); border-radius: inherit; transition: width 0.5s linear; }
.pandora-error { font-size: 0.6875rem; color: var(--ui-text-secondary); border-left: 2px solid var(--ui-accent); padding-left: 6px; }
.pandora-muted { font-size: 0.6875rem; color: var(--ui-text-quaternary); }
.pandora-vol { min-width: 26px; text-align: center; font-size: 0.6875rem; color: var(--ui-text-secondary); }
.pandora-search { display: flex; align-items: center; gap: 4px; }
.pandora-search input { flex: 1; min-width: 0; height: 24px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 4px; background: transparent; color: var(--ui-text-primary); font-size: 0.75rem; outline: none; }
.pandora-search input:focus { border-color: var(--ui-accent); }
.pandora-kind { flex: none; margin-left: auto; font-size: 0.5625rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ui-text-quaternary); }
.pandora-item-art { flex: none; width: 24px; height: 24px; border-radius: 4px; object-fit: cover; background: var(--ui-control-hover-background); }
.pandora-item-text { display: flex; flex-direction: column; min-width: 0; }
.pandora-item-sub { font-size: 0.625rem; color: var(--ui-text-quaternary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
`

// Hermes serves a user plugin's dashboard API only from the gateway's launch
// profile; a request scoped to a secondary profile (the app's active profile)
// is refused before it reaches the plugin. The plugin itself — and its player
// — live in the launch profile, so retry on the primary connection: the same
// bridge call ctx.rest makes, minus the profile tag.
async function rest(ctx, path, opts = {}) {
  try {
    return await ctx.rest(path, opts)
  } catch (err) {
    const message = String(err && err.message ? err.message : err)
    const bridge = typeof window !== 'undefined' && window.hermesDesktop && window.hermesDesktop.api
    if (!bridge || !/404|not found/i.test(message)) throw err
    console.warn('[pandora] ctx.rest failed; retrying on the primary connection:', message)
    return bridge({
      path: `/api/plugins/${ID}${path}`,
      method: opts.method,
      body: opts.body,
      timeoutMs: opts.timeoutMs
    })
  }
}

// Pure on purpose (no SDK, no React) so it can be exercised without the app:
// the 404 banner has to name the profile the request was actually scoped to,
// because "install or enable Pandora here" is useless without knowing where
// "here" is when the desktop app runs a named profile.
// BEGIN not-installed-message
function notInstalledMessage(profile) {
  const name = String(profile || '').trim()
  const where = name ? `profile "${name}"` : 'this profile'
  const command = name
    ? `hermes -p ${name} plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable`
    : 'hermes plugins install 36North/Pandora-for-Hermes#pandora --yes-deps --enable'
  return `Pandora's backend is not installed in ${where}. The chip is app-wide, but its backend runs only in the profile that has the package — install it here: ${command} — or switch the app to a profile that already has Pandora.`
}
// END not-installed-message

function iconButton(name, label, onClick, disabled) {
  return jsx(Button, {
    type: 'button',
    variant: 'ghost',
    size: 'micro',
    title: label,
    'aria-label': label,
    disabled: !!disabled,
    onClick,
    children: jsx(Codicon, { name, size: 14 })
  })
}

function PandoraChip({ ctx }) {
  const [open, setOpen] = useState(false)
  const { data, error: stateError } = useQuery({
    queryKey: ['pandora', 'state'],
    queryFn: async () => {
      try {
        return await rest(ctx, '/state')
      } catch (err) {
        console.warn('[pandora] GET /state failed:', String(err && err.message ? err.message : err))
        throw err
      }
    },
    refetchInterval: POLL_MS,
    staleTime: 2000,
    retry: 1
  })
  const state = data || {}
  const text = state.track
    ? (state.artist ? `${state.artist} — ${state.track}` : state.track)
    : 'Pandora'
  const live = !!state.playing && !state.paused
  const title = text

  return jsxs(Popover, {
    open,
    onOpenChange: setOpen,
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsx('button', {
          type: 'button',
          className: 'pandora-trigger',
          title,
          'data-slot': 'pandora-chip',
          children: [
            jsx('span', { className: `pandora-dot${live ? ' is-live' : ''}` }),
            jsx('span', { className: 'pandora-label', children: text })
          ]
        })
      }),
      jsx(PopoverContent, {
        side: 'top',
        align: 'end',
        className: 'pandora-panel',
        'aria-label': 'Pandora',
        'data-slot': 'pandora-menu',
        children: jsx(PandoraMenu, { ctx, state, stateError, onClose: () => setOpen(false) })
      })
    ]
  })
}

function TrackProgress({ state }) {
  // The backend reports the position every ~5 s. Advance it locally between
  // polls from the moment it arrived, so the bar moves with the song instead of
  // stepping: no extra requests, one style update every 500 ms while open.
  const playing = !!state.playing && !state.paused
  const duration = Number(state.duration_seconds) || 0
  const reported = Number(state.position_seconds) || 0
  const anchor = useRef({ pos: reported, at: Date.now() })
  const [, setTick] = useState(0)

  useEffect(() => {
    anchor.current = { pos: reported, at: Date.now() }
  }, [reported, state.track, playing])

  useEffect(() => {
    if (!playing || !duration) return undefined
    const id = setInterval(() => setTick(tick => tick + 1), 500)
    return () => clearInterval(id)
  }, [playing, duration, state.track])

  if (!duration) return null
  const position = playing
    ? anchor.current.pos + (Date.now() - anchor.current.at) / 1000
    : reported
  const pct = Math.min(100, Math.max(0, (position / duration) * 100))
  return jsx('div', {
    className: 'pandora-progress',
    role: 'progressbar',
    'aria-valuemin': 0,
    'aria-valuemax': 100,
    'aria-valuenow': Math.round(pct),
    children: jsx('div', { className: 'pandora-progress-fill', style: { width: `${pct}%` } })
  })
}

function PandoraMenu({ ctx, state, stateError, onClose }) {
  const queryClient = useQueryClient()
  // The profile the backend request is scoped to: the 404 banner names it so
  // the install command it suggests is the right one for this app.
  const activeProfile = useValue(host.state.profile) || ''
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showOutput, setShowOutput] = useState(false)
  const [searchText, setSearchText] = useState('')
  const [searchFor, setSearchFor] = useState('')

  const stations = useQuery({
    queryKey: ['pandora', 'stations'],
    queryFn: async () => {
      try {
        return await rest(ctx, '/stations')
      } catch (err) {
        console.warn('[pandora] GET /stations failed:', String(err && err.message ? err.message : err))
        throw err
      }
    },
    staleTime: 60000,
    retry: 1
  })
  const search = useQuery({
    queryKey: ['pandora', 'search', searchFor],
    queryFn: async () => {
      try {
        return await rest(ctx, `/search?q=${encodeURIComponent(searchFor)}&limit=50`)
      } catch (err) {
        console.warn('[pandora] GET /search failed:', String(err && err.message ? err.message : err))
        throw err
      }
    },
    enabled: searchFor.length >= 2,
    staleTime: 30000,
    retry: 1
  })
  const playlists = useQuery({
    queryKey: ['pandora', 'playlists'],
    queryFn: () => rest(ctx, '/playlists'),
    staleTime: 60000,
    retry: 1
  })
  const devices = useQuery({
    queryKey: ['pandora', 'devices'],
    queryFn: () => rest(ctx, '/devices'),
    enabled: showOutput,
    staleTime: 30000,
    retry: 1
  })

  const call = async (path, body) => {
    setBusy(true)
    setError('')
    try {
      const result = await rest(ctx, path, { method: 'POST', body })
      if (result && result.ok === false) setError(result.error || 'Pandora request failed.')
      await queryClient.invalidateQueries({ queryKey: ['pandora', 'state'] })
      return result
    } catch (err) {
      setError(String(err && err.message ? err.message : err))
      return null
    } finally {
      setBusy(false)
    }
  }

  const startStation = async result => {
    setBusy(true)
    setError('')
    try {
      const created = await rest(ctx, '/station', { method: 'POST', body: { seed_id: result.id } })
      if (!created || created.ok === false) {
        setError((created && created.error) || 'Could not start that station.')
        return
      }
      setSearchFor('')
      setSearchText('')
      await queryClient.invalidateQueries({ queryKey: ['pandora', 'stations'] })
      await queryClient.invalidateQueries({ queryKey: ['pandora', 'state'] })
    } catch (err) {
      setError(String(err && err.message ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  const plan = state.plan || {}
  const hasTrack = !!state.track
  const paused = !!state.paused || !state.playing
  const stationItems = (stations.data && stations.data.stations) || []
  const searchResults = (search.data && search.data.results) || []
  const searchError = (search.data && search.data.error)
    || (search.error && String(search.error.message || search.error)) || ''
  const playlistItems = (playlists.data && playlists.data.playlists) || []
  const deviceItems = (devices.data && devices.data.devices) || []
  const volume = Number.isFinite(state.volume) ? state.volume : 0

  const deviceRow = (name, label) =>
    jsx('button', {
      type: 'button',
      className: state.output === name ? 'pandora-item is-active' : 'pandora-item',
      'aria-current': state.output === name ? 'true' : undefined,
      disabled: busy,
      title: label,
      onClick: async () => {
        await call('/output', { device: name })
        onClose()
      },
      children: jsxs('span', {
        className: 'pandora-item-name',
        children: [
          jsx(Codicon, { name: name === 'local' ? 'device-desktop' : 'broadcast', size: 12 }),
          ` ${label}`
        ]
      })
    }, name)

  const errorText = (() => {
    if (!stateError) return ''
    const raw = String(stateError.message || stateError)
    if (/404|not found/i.test(raw)) return notInstalledMessage(activeProfile)
    return `Pandora backend unreachable — ${raw}`
  })()

  const friendlyListError = value => {
    const raw = value ? String(value.message || value) : ''
    if (/Plugin not found|404/i.test(raw)) {
      return activeProfile
        ? `Not available in profile "${activeProfile}" — see the note above.`
        : 'Not enabled in this profile — see the note above.'
    }
    return raw
  }

  return jsxs('div', {
    children: [
      jsx(TrackProgress, { state }),
      errorText
        ? jsx('div', { className: 'pandora-error', children: errorText })
        : null,
      jsxs('div', {
        className: 'pandora-head',
        children: [
          jsx('div', {
            className: 'pandora-track',
            children: state.logged_in === false ? 'Not signed in'
              : (state.track || 'Nothing playing')
          }),
          jsx('div', {
            className: 'pandora-artist',
            children: state.logged_in === false
              ? 'Run `hermes pandora login`'
              : (state.artist || (state.kind === 'ad' ? 'Advertisement' : 'Pandora'))
          }),
          jsx('div', {
            className: 'pandora-plan',
            children: plan.note || ''
          })
        ]
      }),

      jsxs('div', {
        className: 'pandora-row',
        children: [
          iconButton('debug-step-back', 'Restart track', () => call('/action', { action: 'previous' }), busy || !hasTrack),
          iconButton(paused ? 'play' : 'debug-pause',
            paused ? 'Play' : 'Pause',
            () => call('/action', { action: paused ? 'resume' : 'pause' }),
            busy || !hasTrack),
          iconButton('debug-step-over', 'Next', () => call('/action', { action: 'next' }), busy || !hasTrack),
          jsx('span', { style: { flex: 1 } }),
          iconButton(state.feedback === 'up' ? 'thumbsup-filled' : 'thumbsup',
            'Thumbs up', () => call('/feedback', { action: 'love' }), busy || !hasTrack),
          iconButton(state.feedback === 'down' ? 'thumbsdown-filled' : 'thumbsdown',
            'Thumbs down', () => call('/feedback', { action: 'ban' }), busy || !hasTrack),
          iconButton('discard', 'Undo feedback', () => call('/feedback', { action: 'undo' }), busy || !hasTrack)
        ]
      }),

      jsxs('div', {
        className: 'pandora-row spread',
        children: [
          jsx('span', { className: 'pandora-label-sm', children: 'Volume' }),
          jsxs('div', {
            className: 'pandora-row',
            children: [
              iconButton('chevron-left', 'Quieter',
                () => call('/action', { action: 'set_volume', volume: Math.max(0, volume - 10) }),
                busy),
              jsx('span', { className: 'pandora-vol', children: String(volume) }),
              iconButton('chevron-right', 'Louder',
                () => call('/action', { action: 'set_volume', volume: Math.min(100, volume + 10) }),
                busy)
            ]
          })
        ]
      }),

      jsx('div', { className: 'pandora-sep' }),

      jsxs('div', {
        className: 'pandora-search',
        children: [
          jsx('input', {
            type: 'search',
            value: searchText,
            placeholder: 'Search artist or song',
            'aria-label': 'Search Pandora',
            onChange: event => setSearchText(event.target.value),
            onKeyDown: event => {
              if (event.key !== 'Enter') return
              event.preventDefault()
              const value = searchText.trim()
              if (value.length >= 2) setSearchFor(value)
            }
          }),
          jsx(Button, {
            type: 'button',
            variant: 'ghost',
            size: 'micro',
            disabled: busy || searchText.trim().length < 2,
            onClick: () => setSearchFor(searchText.trim()),
            children: 'Go'
          })
        ]
      }),
      searchFor
        ? (search.isLoading
          ? jsx(GlyphSpinner, {})
          : (searchResults.length
            ? jsx('div', {
                className: 'pandora-list pandora-search-results',
                children: searchResults.map(result => jsxs('button', {
                  type: 'button',
                  className: 'pandora-item',
                  disabled: busy,
                  title: `${result.name}${result.id ? ` (${result.id})` : ''} — start a station`,
                  onClick: () => startStation(result),
                  children: [
                    result.art
                      ? jsx('img', { className: 'pandora-item-art', src: result.art, alt: '', loading: 'lazy' })
                      : jsx('span', { className: 'pandora-item-art' }),
                    jsxs('span', {
                      className: 'pandora-item-text',
                      children: [
                        jsx('span', { className: 'pandora-item-name', children: result.name }),
                        (result.artist || result.album)
                          ? jsx('span', {
                              className: 'pandora-item-sub',
                              children: [result.artist, result.album].filter(Boolean).join(' · ')
                            })
                          : null
                      ]
                    }),
                    jsx('span', {
                      className: 'pandora-kind',
                      children: result.kind === 'artist' ? 'Artist' : 'Song'
                    })
                  ]
                }, result.id))
              })
            : jsx('div', { className: 'pandora-muted', children: searchError || `No matches for “${searchFor}”` })))
        : null,

      jsx('div', { className: 'pandora-sep' }),

      jsx('div', { className: 'pandora-label-sm', children: 'Stations' }),
      stations.isLoading
        ? jsx(GlyphSpinner, {})
        : (stationItems.length
          ? jsx('div', {
              className: 'pandora-list',
              children: stationItems.map(station =>
                jsx('button', {
                  type: 'button',
                  className: 'pandora-item',
                  title: station.name,
                  disabled: busy,
                  onClick: async () => {
                    await call('/play-station', { source_id: station.id })
                    onClose()
                  },
                  children: jsx('span', { className: 'pandora-item-name', children: station.name })
                }, station.id))
            })
          : jsx('div', {
              className: 'pandora-muted',
              children: friendlyListError((stations.data && stations.data.error) || stations.error)
                || 'No stations found'
            })),

      jsx('div', { className: 'pandora-sep' }),

      jsx('div', { className: 'pandora-label-sm', children: 'Playlists' }),
      playlists.isLoading
        ? jsx(GlyphSpinner, {})
        : ((playlists.data && playlists.data.premium && playlistItems.length)
          ? jsx('div', {
              className: 'pandora-list',
              children: playlistItems.slice(0, 20).map(playlist =>
                jsx('button', {
                  type: 'button',
                  className: 'pandora-item',
                  title: playlist.name,
                  disabled: busy,
                  onClick: async () => {
                    await call('/play-station', { source_id: playlist.id })
                    onClose()
                  },
                  children: jsx('span', { className: 'pandora-item-name', children: playlist.name })
                }, playlist.id))
            })
          : jsx('div', {
              className: 'pandora-muted',
              children: (playlists.data && playlists.data.note)
                || (plan.playlists ? 'No playlists' : 'Playlists need Pandora Premium')
            })),

      jsxs('div', {
        className: 'pandora-row spread',
        children: [
          jsx('span', { className: 'pandora-label-sm', children: `Output · ${state.output || 'local'}` }),
          jsx(Button, {
            type: 'button',
            variant: 'ghost',
            size: 'micro',
            onClick: () => setShowOutput(value => !value),
            children: showOutput ? 'Hide' : 'Change'
          })
        ]
      }),
      showOutput
        ? (devices.isLoading
          ? jsx(GlyphSpinner, {})
          : jsxs('div', {
              className: 'pandora-list',
              children: [
                deviceRow('local', 'This PC'),
                ...deviceItems.map(device =>
                  deviceRow(device.name, `${device.name}${device.model ? ` · ${device.model}` : ''}`))
              ]
            }))
        : null,
      showOutput && devices.data && devices.data.error
        ? jsx('div', { className: 'pandora-muted', children: devices.data.error })
        : null,

      error ? jsx('div', { className: 'pandora-error', children: error }) : null
    ]
  })
}

export default {
  id: ID,
  name: 'Pandora',
  description: 'What is playing on Pandora, with plan-aware playback controls in the status bar.',
  defaultEnabled: false,
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    style.dataset.plugin = ID
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    // Kill-on-close, renderer half: mpv's kill-on-close job is owned by the
    // gateway process, which outlives this window, so an ordinary close would
    // leave the music playing with no chip to stop it. The renderer is torn
    // down right after this fires, so the request is dispatched and never
    // awaited: Chromium's network service hands it to the browser process, and
    // the gateway — a separate, still-running process — receives it and kills
    // the mpv child. pagehide + beforeunload cover the close paths Chromium
    // uses; the flag turns the double-fire into a single request.
    let exitStopSent = false
    const stopOnExit = () => {
      if (exitStopSent) return
      exitStopSent = true
      try {
        rest(ctx, '/shutdown', { method: 'POST', body: {} }).catch(() => {})
      } catch {
        // The renderer is going away with it — nothing left to do.
      }
    }
    window.addEventListener('pagehide', stopOnExit)
    window.addEventListener('beforeunload', stopOnExit)
    ctx.onDispose(() => {
      window.removeEventListener('pagehide', stopOnExit)
      window.removeEventListener('beforeunload', stopOnExit)
    })
    ctx.register({
      id: 'now-playing',
      area: STATUSBAR_AREAS.right,
      order: 20,
      render: () => jsx(PandoraChip, { ctx })
    })
  }
}
