/**
 * Devin for Hermes Desktop: the desktop half of the `devin` agent plugin.
 *
 * - A live run card the model drops into the chat with `::devin-run{id="dv-…"}`
 *   (the delegate_to_devin result hands it the line in Desktop sessions).
 * - A Devin page (sidebar row, ⌘K command) with active runs and history.
 * - A status-bar chip while runs are active.
 * - A pet line and an OS notification when a run finishes.
 *
 * Data: `ctx.rest('/runs…')` reaches dashboard/plugin_api.py; live updates
 * are the `plugin.devin.run.*` gateway events the agent half broadcasts.
 *
 * Plain ESM, loaded uncompiled: UI is jsx() calls, and only
 * @hermes/plugin-sdk, react and react/jsx-runtime may be imported. Styling
 * uses the app's theme variables inline, because Tailwind classes that the
 * app itself does not use are not in its stylesheet.
 */

import {
  atom,
  Button,
  Codicon,
  GlyphSpinner,
  host,
  MessageTextContent,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Textarea,
  Tip,
  TRANSCRIPT_DIRECTIVE_AREA,
  useQuery,
  useTheme,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'devin'
const PAGE = '/devin'
const RUN_ID = /^dv-[0-9a-f]{6,32}$/
const ACTIVE = new Set(['created', 'starting', 'running', 'cancelling'])
const EVENT_TYPES = ['run.created', 'run.state', 'run.progress', 'run.permission', 'run.usage', 'run.finished']

const STATE = {
  created: { label: 'Queued', color: 'var(--ui-text-tertiary)' },
  starting: { label: 'Starting', color: 'var(--ui-cyan)' },
  running: { label: 'Working', color: 'var(--ui-accent)' },
  cancelling: { label: 'Stopping', color: 'var(--ui-yellow)' },
  succeeded: { label: 'Done', color: 'var(--ui-green)' },
  failed: { label: 'Failed', color: 'var(--ui-red)' },
  cancelled: { label: 'Stopped', color: 'var(--ui-yellow)' },
  timed_out: { label: 'Timed out', color: 'var(--ui-red)' }
}

const KIND_ICON = {
  edit: 'edit',
  delete: 'trash',
  move: 'arrow-right',
  read: 'eye',
  search: 'search',
  execute: 'terminal',
  think: 'lightbulb',
  fetch: 'globe',
  other: 'tools'
}

// --- shared state ------------------------------------------------------------------

/** id -> run summary, fed by REST and by the live events. */
const $runs = atom({})
/** id -> recent live events (newest last), so cards move before the next fetch. */
const $live = atom({})
/** The REST backend answered (false: the Python half is off or not mounted). */
const $backend = atom('unknown')
const ownerKey = () => JSON.stringify([host.state.connectionId?.get() || '', host.state.profile?.get() || 'default'])
const $owner = atom(ownerKey())

let rest = null
let ownerRevision = 0
let profileScope = null

const isActive = run => Boolean(run) && ACTIVE.has(run.state)

function mergeRuns(rows) {
  const next = { ...$runs.get() }

  for (const row of rows) {
    if (row && RUN_ID.test(row.id || '')) {
      const prev = next[row.id]
      // Keep whichever copy is newer: the REST snapshot can lag the event stream.
      if (!prev || (row.updated_at || 0) >= (prev.updated_at || 0)) {
        next[row.id] = { ...prev, ...row }
      }
    }
  }

  $runs.set(next)
}

function pushLive(runId, event) {
  if (!event) {
    return
  }

  const all = $live.get()
  const list = (all[runId] || []).filter(e => e.seq !== event.seq)
  list.push(event)
  $live.set({ ...all, [runId]: list.slice(-40) })
}

async function refreshRuns() {
  if (!rest) {
    return
  }

  const revision = ownerRevision
  try {
    const body = await rest('/runs?limit=40')
    if (revision !== ownerRevision) return
    const nextScope = body.profile_scope || ''
    if (profileScope !== null && profileScope !== nextScope) {
      $runs.set({})
      $live.set({})
    }
    profileScope = nextScope
    mergeRuns(body.runs || [])
    $backend.set('ok')
  } catch {
    if (revision === ownerRevision) $backend.set('off')
  }
}

// --- small helpers ---------------------------------------------------------------

const baseName = path => String(path || '').split(/[\\/]/).filter(Boolean).pop() || String(path || '')

function shortPath(path, cwd) {
  const p = String(path || '')
  const root = String(cwd || '')

  if (root && p.toLowerCase().startsWith(root.toLowerCase())) {
    return p.slice(root.length).replace(/^[\\/]+/, '') || baseName(p)
  }

  return baseName(p)
}

function ago(ts, now) {
  if (!ts) {
    return ''
  }

  const s = Math.max(0, Math.round(now / 1000 - ts))

  if (s < 60) {
    return 'just now'
  }

  if (s < 3600) {
    return `${Math.floor(s / 60)} min ago`
  }

  if (s < 86400) {
    return `${Math.floor(s / 3600)} h ago`
  }

  return `${Math.floor(s / 86400)} d ago`
}

function elapsed(run, now) {
  const start = run.started_at || run.created_at
  const end = run.finished_at || now / 1000

  if (!start) {
    return ''
  }

  const s = Math.max(0, Math.round(end - start))

  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

function tokens(usage) {
  const n = usage && usage.tokens && usage.tokens.totalTokens

  if (!n) {
    return ''
  }

  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k tokens` : `${n} tokens`
}

function useNow(active) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) {
      return undefined
    }

    const timer = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(timer)
  }, [active])

  return now
}

const S = {
  card: {
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 60%, transparent)',
    borderRadius: 10,
    background: 'var(--ui-widget-surface-background, transparent)',
    padding: '12px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
    fontSize: 13,
    color: 'var(--ui-text-primary)',
    maxWidth: 720,
    position: 'relative',
    overflow: 'hidden'
  },
  row: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 },
  muted: { color: 'var(--ui-text-tertiary)' },
  faint: { color: 'var(--ui-text-quaternary)' },
  label: {
    fontSize: 10.5,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    color: 'var(--ui-text-quaternary)',
    fontWeight: 600
  },
  mono: { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: 12 },
  pill: color => ({
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '1px 8px',
    borderRadius: 999,
    fontSize: 11.5,
    fontWeight: 600,
    color,
    background: `color-mix(in srgb, ${color} 14%, transparent)`,
    whiteSpace: 'nowrap'
  }),
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    padding: '2px 8px',
    borderRadius: 6,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 50%, transparent)',
    fontSize: 12,
    cursor: 'pointer',
    maxWidth: 260,
    background: 'transparent',
    color: 'var(--ui-text-secondary)'
  }
}

// Keyframes for the card's live touches; injected once, removed on unload.
const CSS = `
@keyframes devin-flash { 0% { background: color-mix(in srgb, var(--ui-green) 35%, transparent); color: var(--ui-text-primary); } 100% { background: transparent; } }
@keyframes devin-in { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
@keyframes devin-sweep { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.devin-flash { animation: devin-flash 1.6s ease-out; }
.devin-in { animation: devin-in 0.25s ease-out; }
.devin-sweep { position: absolute; left: 0; right: 0; top: 0; height: 2px; overflow: hidden; }
.devin-sweep::after { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, var(--ui-accent), var(--ui-cyan), transparent); animation: devin-sweep 1.4s linear infinite; }
@keyframes devin-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.devin-pulse { animation: devin-pulse 2.2s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) {
  .devin-pulse, .devin-sweep::after, .devin-flash, .devin-in { animation: none; }
}
.devin-row:hover { background: var(--ui-row-hover-background); }
`

// --- Devin × Hermes ----------------------------------------------------------------

const DEVIN_MASK = `url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="68 47 291 333"><path d="M70 159.333V91.3471C70 88.3592 71.594 85.5983 74.1816 84.1044L133.043 50.1205C135.631 48.6265 138.819 48.6265 141.407 50.1205L200.269 84.1044C202.856 85.5983 204.45 88.3592 204.45 91.3471V126.068C204.708 137.606 210.806 148.734 221.531 154.926C232.256 161.117 244.942 160.834 255.063 155.289L285.132 137.929C287.719 136.435 290.907 136.435 293.495 137.929L352.357 171.913C354.944 173.406 356.538 176.167 356.538 179.155V247.123C356.538 250.111 354.944 252.872 352.357 254.366L293.495 288.35C290.907 289.844 287.719 289.844 285.132 288.35L255.306 271.13C245.146 265.456 232.344 265.117 221.534 271.358C210.809 277.55 204.711 288.678 204.453 300.215V334.926C204.453 337.914 202.859 340.675 200.271 342.169L141.41 376.153C138.822 377.647 135.634 377.647 133.046 376.153L74.1845 342.169C71.5969 340.675 70.0028 337.914 70.0028 334.926V266.959C70.0029 263.971 71.5969 261.21 74.1845 259.716L133.046 225.732C135.634 224.238 138.822 224.238 141.41 225.732L171.547 243.132C181.656 248.638 194.306 248.906 205.005 242.729C215.815 236.488 221.922 225.231 222.088 213.595C221.83 202.057 215.732 189.737 205.008 183.545C194.283 177.353 181.597 177.636 171.476 183.181L141.269 200.72C138.67 202.229 135.461 202.228 132.864 200.716L74.1576 166.562C71.5835 165.065 70 162.311 70 159.333Z"/></svg>`)}")`

/** Devin's trioexagon mark in the theme's text colour. */
function DevinMark({ size = 20 }) {
  return jsx('span', {
    'aria-label': 'Devin',
    role: 'img',
    style: {
      display: 'inline-block',
      flex: 'none',
      width: size,
      height: size,
      background: 'var(--ui-text-primary)',
      WebkitMaskImage: DEVIN_MASK,
      maskImage: DEVIN_MASK,
      WebkitMaskRepeat: 'no-repeat',
      maskRepeat: 'no-repeat',
      WebkitMaskPosition: 'center',
      maskPosition: 'center',
      WebkitMaskSize: 'contain',
      maskSize: 'contain'
    }
  })
}

/** The Hermes badge the app itself ships (light and dark variants). */
function HermesMark({ size = 20 }) {
  const theme = useTheme()
  const [failed, setFailed] = useState(false)
  const dark = !theme || theme.renderedMode !== 'light'

  if (failed) {
    return jsx(Codicon, { name: 'sparkle', style: { fontSize: Math.round(size * 0.85), color: 'var(--ui-text-primary)' } })
  }

  return jsx('img', {
    alt: 'Hermes',
    src: dark ? 'nous-girl-dark.png' : 'nous-girl.png',
    width: size,
    height: size,
    draggable: false,
    onError: () => setFailed(true),
    style: { width: size, height: size, flex: 'none', objectFit: 'contain' }
  })
}

/** The two app icons side by side on matching tiles: Devin, then Hermes. */
function PairBadge({ size = 28, live = false }) {
  const tile = {
    display: 'grid',
    placeItems: 'center',
    width: size,
    height: size,
    flex: 'none',
    overflow: 'hidden',
    borderRadius: Math.round(size * 0.27),
    background: 'color-mix(in srgb, var(--ui-text-primary) 7%, var(--ui-editor-surface-background, transparent))',
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 55%, transparent)'
  }

  return jsxs('span', {
    'aria-label': 'Devin × Hermes',
    role: 'img',
    style: { position: 'relative', display: 'inline-flex', alignItems: 'center', gap: Math.max(4, Math.round(size * 0.15)), flex: 'none' },
    children: [
      jsx('span', { style: tile, children: jsx(DevinMark, { size: Math.round(size * 0.56) }) }),
      jsx('span', { style: tile, children: jsx(HermesMark, { size: Math.round(size * 0.86) }) }),
      live
        ? jsx('span', {
            className: 'devin-pulse',
            style: {
              position: 'absolute',
              right: -2,
              top: -2,
              width: Math.max(6, Math.round(size * 0.22)),
              height: Math.max(6, Math.round(size * 0.22)),
              borderRadius: 999,
              background: 'var(--ui-green)',
              boxShadow: '0 0 0 2px var(--ui-editor-surface-background, var(--ui-surface-background, transparent))'
            }
          })
        : null
    ]
  })
}

/** The page header: the pair, the name, one line of context, and what Devin is doing now. */
function BrandHeader({ live, active }) {
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' },
    children: [
      jsx(PairBadge, { size: 40, live }),
      jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: '1 1 260px' },
        children: [
          jsxs('h1', {
            style: { margin: 0, fontSize: 18, fontWeight: 650, letterSpacing: '-0.01em', color: 'var(--ui-text-primary)' },
            children: ['Devin ', jsx('span', { style: { color: 'var(--ui-text-quaternary)', fontWeight: 400 }, children: '×' }), ' Hermes']
          }),
          jsx('span', {
            style: { fontSize: 13, color: 'var(--ui-text-tertiary)' },
            children: 'Hermes plans the task and asks you first. Devin runs it locally or in the cloud.'
          })
        ]
      }),
      jsxs('span', {
        style: S.pill(live ? 'var(--ui-green)' : 'var(--ui-text-tertiary)'),
        children: [
          jsx('span', { style: { width: 6, height: 6, borderRadius: 999, background: live ? 'var(--ui-green)' : 'var(--ui-text-quaternary)' } }),
          live ? `${active} working` : 'Idle'
        ]
      })
    ]
  })
}

// --- the run card ---------------------------------------------------------------

function StateBadge({ state }) {
  const s = STATE[state] || { label: state || 'Unknown', color: 'var(--ui-text-tertiary)' }

  return jsxs('span', {
    style: S.pill(s.color),
    children: [
      ACTIVE.has(state)
        ? jsx(GlyphSpinner, { ariaLabel: s.label })
        : jsx('span', { style: { width: 6, height: 6, borderRadius: 999, background: s.color } }),
      s.label
    ]
  })
}

function Plan({ plan }) {
  if (!plan || plan.length === 0) {
    return null
  }

  const done = plan.filter(e => e.status === 'completed').length

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 4 },
    children: [
      jsx('div', { style: S.label, children: `Plan · ${done}/${plan.length}` }),
      ...plan.slice(0, 8).map((entry, i) => {
        const status = entry.status
        const icon = status === 'completed' ? 'pass-filled' : status === 'in_progress' ? 'circle-large-outline' : 'circle-large'
        const color = status === 'completed' ? 'var(--ui-green)' : status === 'in_progress' ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)'

        return jsxs(
          'div',
          {
            className: 'devin-in',
            style: {
              ...S.row,
              color: status === 'completed' ? 'var(--ui-text-tertiary)' : 'var(--ui-text-primary)',
              textDecoration: status === 'completed' ? 'line-through' : 'none',
              textDecorationColor: 'var(--ui-text-quaternary)'
            },
            children: [
              jsx(Codicon, { name: icon, style: { color, fontSize: 13 }, spinning: false }),
              jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: entry.content })
            ]
          },
          i
        )
      })
    ]
  })
}

function Files({ files, cwd, os }) {
  const seen = useRef(null)
  const fresh = new Set()

  if (seen.current === null) {
    seen.current = new Set(files)
  } else {
    for (const f of files) {
      if (!seen.current.has(f)) {
        fresh.add(f)
      }
    }
  }

  useEffect(() => {
    for (const f of files) {
      seen.current.add(f)
    }
  })

  if (!files || files.length === 0) {
    return null
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 5 },
    children: [
      jsx('div', { style: S.label, children: `Files changed · ${files.length}` }),
      jsx('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 5 },
        children: files.slice(0, 24).map(f =>
          jsx(
            Tip,
            {
              label: os ? `${f} — show in folder` : f,
              children: jsxs(os ? 'button' : 'span', {
                type: os ? 'button' : undefined,
                className: fresh.has(f) ? 'devin-flash' : undefined,
                style: S.chip,
                onClick: os ? () => os.revealPath(f) : undefined,
                children: [
                  jsx(Codicon, { name: 'diff-modified', style: { color: 'var(--ui-diff-add-foreground, var(--ui-green))' } }),
                  jsx('span', {
                    style: { ...S.mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                    children: shortPath(f, cwd)
                  })
                ]
              })
            },
            f
          )
        )
      })
    ]
  })
}

function eventLine(e) {
  const data = e.data || {}

  switch (e.kind) {
    case 'tool_call':
      return { icon: KIND_ICON[data.tool_kind] || 'tools', text: e.text, tone: 'var(--ui-text-secondary)' }
    case 'tool_update':
      return data.status === 'failed'
        ? { icon: 'error', text: `${e.text} failed`, tone: 'var(--ui-red)' }
        : null
    case 'permission': {
      const denied = String(e.text || '').startsWith('denied')

      return { icon: denied ? 'shield' : 'unlock', text: e.text, tone: denied ? 'var(--ui-yellow)' : 'var(--ui-green)' }
    }
    case 'permission_request':
      return { icon: 'question', text: `asks: ${e.text}`, tone: 'var(--ui-yellow)' }
    case 'status':
      return { icon: 'info', text: e.text, tone: 'var(--ui-text-tertiary)' }
    case 'mode':
      return e.text ? { icon: 'settings-gear', text: `mode: ${e.text}`, tone: 'var(--ui-text-tertiary)' } : null
    default:
      return null
  }
}

function Activity({ events }) {
  const lines = []

  for (const e of events || []) {
    const line = eventLine(e)

    if (line && line.text) {
      lines.push({ ...line, seq: e.seq })
    }
  }

  const shown = lines.slice(-5)

  if (shown.length === 0) {
    return null
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 3 },
    children: [
      jsx('div', { style: S.label, children: 'Activity' }),
      ...shown.map((line, i) =>
        jsxs(
          'div',
          {
            className: 'devin-in',
            style: { ...S.row, color: line.tone, opacity: 0.55 + (0.45 * (i + 1)) / shown.length },
            children: [
              jsx(Codicon, { name: line.icon, style: { fontSize: 12 } }),
              jsx('span', {
                style: { ...S.mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                children: line.text
              })
            ]
          },
          line.seq
        )
      )
    ]
  })
}

function Permissions({ permissions }) {
  if (!permissions || permissions.length === 0) {
    return null
  }

  const allowed = permissions.filter(p => p.decision === 'allowed').length
  const denied = permissions.length - allowed

  return jsxs('div', {
    style: { ...S.row, flexWrap: 'wrap' },
    children: [
      jsx('span', { style: S.label, children: 'Permissions' }),
      allowed ? jsx('span', { style: S.pill('var(--ui-green)'), children: `${allowed} allowed` }) : null,
      denied ? jsx('span', { style: S.pill('var(--ui-yellow)'), children: `${denied} denied` }) : null
    ]
  })
}

// --- live changes and terminal ---------------------------------------------------

/** Edit snapshots grouped by file, including history retained in older run events. */
function liveViews(data, events) {
  const edits = new Map()
  const files = new Map()
  const commands = new Map()
  const recorded = data.events || []
  const watermark = Math.max(0, ...recorded.map(e => e.seq || 0))
  const putEdit = (d, seq, snapshot = false) => {
    if (!d.path || typeof d.patch !== 'string') return
    const key = JSON.stringify([d.path, d.edit_id || d.tool_call_id || 'legacy'])
    const previous = edits.get(key)
    if (snapshot || !previous || seq > previous.seq) edits.set(key, { ...d, seq })
  }

  for (const e of recorded) {
    if (e.kind === 'diff') putEdit(e.data || {}, e.seq || 0)
  }

  for (const d of data.diffs || []) {
    putEdit(d, watermark, true)
  }

  for (const c of data.commands || []) {
    commands.set(c.tool_call_id, c)
  }

  for (const e of events) {
    const d = e.data || {}

    if (e.kind === 'diff' && d.path) {
      putEdit(d, e.seq || 0)
    } else if (e.kind === 'terminal' && d.tool_call_id) {
      commands.set(d.tool_call_id, { ...commands.get(d.tool_call_id), ...d, seq: e.seq })
    }
  }

  for (const d of [...edits.values()].sort((a, b) => a.seq - b.seq).slice(-30)) {
    const previous = files.get(d.path)
    files.delete(d.path)
    files.set(d.path, {
      ...d,
      edits: [...(previous?.edits || []), d],
      added: (previous?.added || 0) + (d.added || 0),
      removed: (previous?.removed || 0) + (d.removed || 0)
    })
  }

  return { diffs: [...files.values()], commands: [...commands.values()], diffsTruncated: !!data.diffs_dropped || edits.size > 30 }
}

const LINE = {
  add: {
    background: 'var(--ui-diff-add-background, color-mix(in srgb, var(--ui-green) 14%, transparent))',
    color: 'var(--ui-diff-add-foreground, var(--ui-green))'
  },
  remove: {
    background: 'var(--ui-diff-remove-background, color-mix(in srgb, var(--ui-red) 14%, transparent))',
    color: 'var(--ui-diff-remove-foreground, var(--ui-red))'
  },
  hunk: { color: 'var(--ui-text-quaternary)' },
  same: { color: 'var(--ui-text-tertiary)' }
}

const box = {
  ...S.mono,
  fontSize: 11.5,
  lineHeight: '17px',
  borderRadius: 8,
  border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)',
  background: 'color-mix(in srgb, var(--ui-text-primary) 3%, transparent)',
  overflow: 'auto',
  maxHeight: 260
}

function DiffView({ diff, full = false }) {
  if (diff.edits) {
    return jsx('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 6 },
      children: diff.edits.map((edit, i) => jsxs('div', {
        children: [
          diff.edits.length > 1 ? jsx('div', { style: { ...S.faint, padding: '3px 0' }, children: `Edit ${i + 1} · +${edit.added || 0} −${edit.removed || 0}` }) : null,
          jsx(DiffView, { diff: edit, full })
        ]
      }, edit.edit_id || edit.tool_call_id || i))
    })
  }
  const lines = String(diff.patch || '').split('\n')

  return jsxs('div', {
    className: 'devin-in',
    style: { ...box, padding: '6px 0', ...(full ? { maxHeight: 'none' } : {}) },
    children: [
      ...lines.map((line, i) => {
        const tone = line.startsWith('@@')
          ? LINE.hunk
          : line.startsWith('+')
            ? LINE.add
            : line.startsWith('-')
              ? LINE.remove
              : LINE.same

        return jsx('div', { style: { ...tone, padding: '0 10px', whiteSpace: 'pre' }, children: line || ' ' }, i)
      }),
      diff.truncated ? jsx('div', { style: { ...LINE.hunk, padding: '2px 10px' }, children: '… diff truncated' }) : null
    ]
  })
}

function Changes({ diffs, cwd, os, live, truncated }) {
  const [picked, setPicked] = useState(null)

  if (!diffs.length) {
    return null
  }

  // Follow Devin's latest edit until the user picks a file.
  const latest = diffs[diffs.length - 1]
  const current = diffs.find(d => d.path === picked) || latest
  const added = diffs.reduce((n, d) => n + (d.added || 0), 0)
  const removed = diffs.reduce((n, d) => n + (d.removed || 0), 0)

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 6 },
    children: [
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between' },
        children: [
          jsxs('span', {
            style: { ...S.row, gap: 6 },
            children: [
              jsx('span', { style: S.label, children: `Changes · ${diffs.length} file${diffs.length === 1 ? '' : 's'}` }),
              jsx('span', { style: { ...S.mono, color: 'var(--ui-green)' }, children: `+${added}` }),
              jsx('span', { style: { ...S.mono, color: 'var(--ui-red)' }, children: `−${removed}` })
            ]
          }),
          live && !picked ? jsx('span', { style: { ...S.faint, fontSize: 11 }, children: 'following Devin' }) : null
        ]
      }),
      jsx('div', {
        style: { ...S.faint, fontSize: 11 },
        children: truncated ? 'Showing the latest 30 recorded edits. Totals are not a net diff.' : 'Edit totals, not a net diff.'
      }),
      jsx('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 5 },
        children: diffs
          .slice(-12)
          .reverse()
          .map(d =>
            jsxs(
              'button',
              {
                type: 'button',
                className: d === latest && live ? 'devin-flash' : undefined,
                onClick: () => setPicked(d.path === picked ? null : d.path),
                style: {
                  ...S.chip,
                  borderColor: d.path === current.path ? 'var(--ui-accent)' : S.chip.border,
                  color: d.path === current.path ? 'var(--ui-text-primary)' : S.chip.color
                },
                children: [
                  jsx(Codicon, { name: d.new_file ? 'diff-added' : 'diff-modified', style: { color: d.new_file ? 'var(--ui-green)' : 'var(--ui-yellow)' } }),
                  jsx('span', {
                    style: { ...S.mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                    children: shortPath(d.path, cwd)
                  }),
                  jsx('span', { style: { ...S.mono, fontSize: 11, color: 'var(--ui-green)' }, children: `+${d.added || 0}` }),
                  jsx('span', { style: { ...S.mono, fontSize: 11, color: 'var(--ui-red)' }, children: `−${d.removed || 0}` })
                ]
              },
              d.path
            )
          )
      }),
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between', fontSize: 11.5 },
        children: [
          jsx('span', { style: { ...S.mono, ...S.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: current.path }),
          os
            ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => os.revealPath(current.path), children: 'Show in folder' })
            : null
        ]
      }),
      jsx(DiffView, { diff: current }, `${current.path}:${current.seq || 0}:${(current.patch || '').length}`)
    ]
  })
}

function CommandView({ command }) {
  const running = command.status !== 'completed' && command.status !== 'failed' && command.exit_code == null
  const ok = command.exit_code === 0
  const lines = String(command.output || '').split('\n')
  const tail = lines.slice(-14).join('\n')

  return jsxs('div', {
    style: { ...box, padding: 0 },
    children: [
      jsxs('div', {
        style: {
          ...S.row,
          justifyContent: 'space-between',
          padding: '5px 10px',
          borderBottom: command.output ? '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 35%, transparent)' : 'none'
        },
        children: [
          jsxs('span', {
            style: { ...S.row, minWidth: 0, color: 'var(--ui-text-primary)' },
            children: [
              jsx('span', { style: { color: 'var(--ui-accent)' }, children: '$' }),
              jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: command.command })
            ]
          }),
          running
            ? jsx(GlyphSpinner, { ariaLabel: 'running' })
            : command.exit_code == null
              ? jsx('span', { style: S.faint, children: command.status })
              : jsx('span', { style: S.pill(ok ? 'var(--ui-green)' : 'var(--ui-red)'), children: `exit ${command.exit_code}` })
        ]
      }),
      command.output
        ? jsx('div', {
            style: { padding: '6px 10px', whiteSpace: 'pre-wrap', color: 'var(--ui-text-secondary)' },
            children: lines.length > 14 ? `…\n${tail}` : tail
          })
        : null
    ]
  })
}

function Terminal({ commands }) {
  if (!commands.length) {
    return null
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 6 },
    children: [
      jsx('div', { style: S.label, children: 'Terminal' }),
      ...commands.slice(-2).map(c => jsx(CommandView, { command: c }, c.tool_call_id))
    ]
  })
}

// --- Devin's requests waiting for an answer ----------------------------------------

function PendingRequest({ runId, ask }) {
  const [busy, setBusy] = useState(false)
  const now = useNow(ask.via === 'card')
  const left = ask.expires_at ? Math.max(0, Math.round(ask.expires_at - now / 1000)) : null

  const answer = async allow => {
    setBusy(true)

    try {
      await rest(`/runs/${runId}/permissions/${ask.ask_id}`, { method: 'POST', body: { allow } })
    } catch (error) {
      host.notify({ kind: 'error', message: (error && error.message) || 'Could not answer Devin' })
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'devin-in',
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
      padding: '10px 12px',
      borderRadius: 9,
      border: '1px solid color-mix(in srgb, var(--ui-yellow) 45%, transparent)',
      background: 'color-mix(in srgb, var(--ui-yellow) 8%, transparent)'
    },
    children: [
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between' },
        children: [
          jsxs('span', {
            style: { ...S.row, color: 'var(--ui-yellow)', fontWeight: 600 },
            children: [jsx(Codicon, { name: 'shield' }), 'Devin asks to']
          }),
          jsx('span', { style: { ...S.pill('var(--ui-text-tertiary)') }, children: ask.kind })
        ]
      }),
      jsx('div', { style: { ...S.mono, color: 'var(--ui-text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }, children: ask.title }),
      ask.via === 'card'
        ? jsxs('div', {
            style: { ...S.row, justifyContent: 'space-between' },
            children: [
              jsx('span', {
                style: { ...S.faint, fontSize: 11.5 },
                children: left === null ? '' : `Denied automatically in ${left}s if nobody answers`
              }),
              jsxs('div', {
                style: S.row,
                children: [
                  jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: () => answer(false), children: 'Deny' }),
                  jsx(Button, { size: 'xs', variant: 'default', disabled: busy, onClick: () => answer(true), children: 'Allow' })
                ]
              })
            ]
          })
        : jsx('span', { style: { ...S.faint, fontSize: 11.5 }, children: "Answer it in Hermes' approval prompt." })
    ]
  })
}

function PendingRequests({ runId, pending }) {
  if (!pending || pending.length === 0) {
    return null
  }

  return jsx('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 6 },
    children: pending.map(ask => jsx(PendingRequest, { runId, ask }, ask.ask_id))
  })
}

// --- follow-ups ------------------------------------------------------------------

function FollowUpBox({ runId, active }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const send = async () => {
    const prompt = text.trim()

    if (!prompt || busy || active) {
      return
    }

    setBusy(true)

    try {
      const body = await rest(`/runs/${runId}/follow-up`, { method: 'POST', body: { prompt } })

      if (body && body.run) {
        mergeRuns([body.run])
      }

      setText('')
    } catch (error) {
      host.notify({ kind: 'error', message: (error && error.message) || 'Could not send the follow-up' })
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 6 },
    children: [
      jsx(Textarea, {
        value: text,
        rows: 2,
        disabled: busy || active,
        placeholder: active ? 'You can follow up when Devin finishes…' : 'Tell Devin what to do next…',
        onChange: event => setText(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            void send()
          }
        },
        style: { fontSize: 13, resize: 'vertical', minHeight: 54 }
      }),
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between' },
        children: [
          jsx('span', {
            style: { ...S.faint, fontSize: 11.5 },
            children: 'Continues the same Devin session, so Devin remembers this run. Enter sends, Shift+Enter adds a line.'
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'default',
            disabled: busy || active || !text.trim(),
            onClick: send,
            children: busy ? 'Sending…' : 'Send to Devin'
          })
        ]
      })
    ]
  })
}

function FollowUps({ children, ctx, wide }) {
  if (!children.length) {
    return null
  }

  return jsx('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 10, paddingLeft: 12, borderLeft: '2px solid color-mix(in srgb, var(--ui-accent) 40%, transparent)' },
    children: children.map(child => jsx(RunCard, { runId: child.id, ctx, wide: true }, child.id))
  })
}

function useRunDetail(runId, active) {
  const owner = useValue($owner)
  return useQuery({
    queryKey: [ID, 'run', runId, owner, active ? 'live' : 'output'],
    enabled: Boolean(rest) && RUN_ID.test(runId || ''),
    queryFn: () => rest(`/runs/${runId}${active ? '' : '?output=true'}`),
    refetchInterval: active ? 2500 : false,
    retry: 1
  })
}

function RunCard({ runId, ctx, compact = false, wide = false }) {
  const summaries = useValue($runs)
  const liveAll = useValue($live)
  const backend = useValue($backend)
  const summary = summaries[runId]
  const active = isActive(summary) || (!summary && backend !== 'off')
  const detail = useRunDetail(runId, active)
  const now = useNow(isActive(summary))
  const [stopping, setStopping] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)

  const data = detail.data || {}
  // The event summary is the freshest; the REST detail adds plan, events, output.
  const run = { ...data, ...(summary || {}) }

  if (!run.id) {
    if (detail.isError || backend === 'off') {
      return jsx('div', {
        style: { ...S.card, ...S.muted },
        children: `Devin run ${runId} — details are not available here (is the devin plugin enabled in this Hermes?).`
      })
    }

    return jsxs('div', {
      style: { ...S.card, ...S.row },
      children: [jsx(GlyphSpinner, {}), jsx('span', { style: S.muted, children: `Loading Devin run ${runId}…` })]
    })
  }

  const live = isActive(run)
  const events = (() => {
    const bySeq = new Map()

    for (const e of data.events || []) {
      bySeq.set(e.seq, e)
    }

    for (const e of liveAll[runId] || []) {
      bySeq.set(e.seq, e)
    }

    return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
  })()

  const files = run.files_changed || []
  const views = liveViews(data, events)
  const followUps = Object.values(summaries)
    .filter(r => r.parent_run_id === runId)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0))
  const cloud = run.execution_target === 'cloud'
  const resumable = run.backend === 'acp' && Boolean(data.devin_session_id)
    && (!cloud || run.remote_state === 'idle' || run.remote_state === 'cancelled')
  const cloudUrl = /^https:\/\/app\.devin\.ai\/sessions\/[a-f0-9]{32}$/.test(run.cloud_url || '') ? run.cloud_url : ''
  const reconnect = async () => {
    setReconnecting(true)
    try {
      await rest(`/runs/${runId}/reconnect`, { method: 'POST' })
      host.notify({ kind: 'info', message: 'Cloud session is reachable. Remote activity is not confirmed; check the Cloud session before continuing.' })
    } catch (error) {
      host.notify({ kind: 'error', message: error?.message || 'Could not reconnect to the Cloud session.' })
    } finally { setReconnecting(false) }
  }
  const stop = async () => {
    setStopping(true)

    try {
      await rest(`/runs/${runId}/cancel`, { method: 'POST' })
      host.notify({ kind: 'info', message: `Stopping Devin run ${runId}` })
    } catch (error) {
      host.notify({ kind: 'error', message: (error && error.message) || `Could not stop ${runId}` })
      setStopping(false)
    }
  }

  return jsxs('div', {
    style: wide ? { ...S.card, maxWidth: 'none' } : S.card,
    children: [
      live ? jsx('div', { className: 'devin-sweep' }) : null,
      // header
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between' },
        children: [
          jsxs('div', {
            style: { ...S.row, flex: 1 },
            children: [
              jsx(PairBadge, { size: 24, live }),
              jsxs('div', {
                style: { display: 'flex', flexDirection: 'column', minWidth: 0 },
                children: [
                  jsx('span', {
                    style: { fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                    children: run.title || 'Devin run'
                  }),
                  jsxs('span', {
                    style: { ...S.faint, fontSize: 11.5, ...S.row, gap: 6 },
                    children: [
                      jsx('span', { style: S.mono, children: runId }),
                      '·',
                      jsx('span', { children: cloud ? 'Cloud' : baseName(run.cwd) }),
                      '·',
                      jsx('span', { children: cloud ? 'Cloud-managed permissions' : run.permission_mode }),
                      run.background ? '· background' : null
                    ]
                  })
                ]
              })
            ]
          }),
          jsxs('div', {
            style: { ...S.row, flex: 'none' },
            children: [
              jsx('span', { style: { ...S.muted, ...S.mono }, children: elapsed(run, now) }),
              jsx(StateBadge, { state: run.state })
            ]
          })
        ]
      }),
      live && run.last_activity && compact
        ? jsx('div', { style: { ...S.muted, ...S.mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: run.last_activity })
        : null,
      !compact && (run.model_label || run.model || Object.keys(run.session_config || {}).length) ? jsxs('div', {
        style: { ...S.muted, fontSize: 12, display: 'flex', flexDirection: 'column', gap: 4, overflowWrap: 'anywhere' },
        children: [
          jsx('span', { children: `${run.model_confirmed ? 'Confirmed' : 'Requested'} model: ${run.model_label || run.model || 'CLI default'}` }),
          jsx('span', { children: Object.entries(run.session_config || {}).filter(([key]) => key !== 'model').map(([key, value]) => `${key}: ${value}`).join(' · ') }),
          jsx('span', { children: run.model_cost_summary ? `Catalog pricing: ${run.model_cost_summary}` : 'Pricing not supplied; usage may incur charges.' })
        ]
      }) : null,
      compact ? null : jsx(Plan, { plan: data.plan }),
      compact ? null : jsx(Changes, { diffs: views.diffs, cwd: cloud ? '' : run.cwd, os: !cloud && ctx && ctx.os, live, truncated: views.diffsTruncated }),
      views.diffs.length && !compact ? null : jsx(Files, { files, cwd: cloud ? '' : run.cwd, os: !cloud && ctx && ctx.os }),
      compact ? null : jsx(Terminal, { commands: views.commands }),
      compact || !live ? null : jsx(Activity, { events }),
      live ? jsx(PendingRequests, { runId, pending: run.pending_permissions }) : null,
      compact ? null : jsx(Permissions, { permissions: data.permissions }),
      run.error
        ? jsxs('div', {
            style: {
              ...S.row,
              alignItems: 'flex-start',
              color: 'var(--ui-red)',
              background: 'color-mix(in srgb, var(--ui-red) 8%, transparent)',
              borderRadius: 7,
              padding: '7px 9px'
            },
            children: [
              jsx(Codicon, { name: 'error', style: { marginTop: 2 } }),
              jsxs('span', { children: [jsx('b', { children: run.error.code }), ' — ', run.error.message] })
            ]
          })
        : null,
      !compact && !live && data.output
        ? jsx('div', {
            style: {
              borderLeft: '2px solid var(--ui-stroke-secondary)',
              paddingLeft: 11,
              maxHeight: 260,
              overflow: 'auto',
              color: 'var(--ui-text-secondary)'
            },
            children: jsx(MessageTextContent, { text: String(data.output).trim().slice(0, 4000), media: false })
          })
        : null,
      // footer
      jsxs('div', {
        style: { ...S.row, justifyContent: 'space-between' },
        children: [
          jsxs('span', {
            style: { ...S.faint, fontSize: 11.5, ...S.row, gap: 10 },
            children: [
              run.tool_calls ? jsx('span', { children: `${run.tool_calls} tool calls` }) : null,
              tokens(run.usage) ? jsx('span', { children: tokens(run.usage) }) : null
            ]
          }),
          jsxs('div', {
            style: S.row,
            children: [
              compact
                ? null
                : jsx(Button, {
                    size: 'xs',
                    variant: 'ghost',
                    onClick: () => host.navigate(PAGE),
                    children: 'All runs'
                  }),
              !compact && views.diffs.length
                ? jsx(Button, {
                    size: 'xs',
                    variant: 'outline',
                    onClick: () => openReview(runId, run.title, ctx),
                    children: `Review changes (${views.diffs.length})`
                  })
                : null,
              live
                ? jsx(Button, {
                    size: 'xs',
                    variant: 'outline',
                    disabled: stopping || run.state === 'cancelling',
                    onClick: stop,
                    children: stopping || run.state === 'cancelling' ? 'Stopping…' : 'Stop'
                  })
                : null
            ]
          })
        ]
      }),
      cloud ? jsxs('div', {
        style: { ...S.row, flexWrap: 'wrap', gap: 10, fontSize: 12, ...S.muted },
        children: [jsx('span', { role: 'status', children: run.remote_state === 'unknown'
          ? 'Remote activity unknown; Cloud work may still be running.'
          : run.remote_state === 'cancelled' ? 'Remote cancellation confirmed.'
          : run.remote_state === 'idle' ? 'Cloud turn finished; the session remains available.'
          : run.remote_state === 'cancel_requested' ? 'Waiting for Cloud cancellation confirmation…' : 'Cloud connection active.' }),
          cloudUrl ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => ctx.os.openExternal(cloudUrl), children: 'Open Cloud session' }) : null,
          !live && data.devin_session_id ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: reconnecting,
            onClick: reconnect, children: reconnecting ? 'Reconnecting…' : 'Reconnect' }) : null]
      }) : null,
      compact ? null : jsx(FollowUps, { children: followUps, ctx }),
      !compact && resumable && followUps.length === 0 ? jsx(FollowUpBox, { runId, active: live }) : null
    ]
  })
}

// --- review tab and new tasks ----------------------------------------------------

function ReviewPane({ runId, ctx }) {
  const summaries = useValue($runs)
  const liveAll = useValue($live)
  const summary = summaries[runId] || {}
  const detail = useRunDetail(runId, isActive(summary))
  const data = detail.data || {}
  const run = { ...data, ...summary }
  const { diffs, diffsTruncated } = liveViews(data, liveAll[runId] || [])
  const added = diffs.reduce((n, d) => n + (d.added || 0), 0)
  const removed = diffs.reduce((n, d) => n + (d.removed || 0), 0)

  return jsx('div', {
    style: { height: '100%', overflow: 'auto' },
    children: jsxs('div', {
      style: { maxWidth: 1100, margin: '0 auto', padding: '22px 24px 48px', display: 'flex', flexDirection: 'column', gap: 18 },
      children: [
        jsxs('header', {
          style: { display: 'flex', flexDirection: 'column', gap: 6 },
          children: [
            jsxs('div', {
              style: { ...S.row, gap: 10 },
              children: [
                jsx(Codicon, { name: 'diff', style: { fontSize: 18, color: 'var(--ui-accent)' } }),
                jsx('h1', { style: { fontSize: 18, fontWeight: 650, margin: 0 }, children: run.title || 'Devin run' }),
                jsx(StateBadge, { state: run.state })
              ]
            }),
            jsxs('div', {
              style: { ...S.row, ...S.muted, gap: 10, fontSize: 12.5 },
              children: [
                jsx('span', { style: S.mono, children: runId }),
                jsx('span', { children: run.cwd }),
                jsx('span', { children: `${diffs.length} file${diffs.length === 1 ? '' : 's'}` }),
                jsx('span', { style: { ...S.mono, color: 'var(--ui-green)' }, children: `+${added}` }),
                jsx('span', { style: { ...S.mono, color: 'var(--ui-red)' }, children: `−${removed}` })
              ]
            })
          ]
        }),
        jsx('div', { style: S.faint, children: diffsTruncated ? 'Showing the latest 30 recorded edits. Totals are not a net diff.' : 'Edit totals, not a net diff.' }),
        diffs.length
          ? jsx('div', {
              style: { display: 'flex', flexDirection: 'column', gap: 16 },
              children: diffs
                .slice()
                .reverse()
                .map(d =>
                  jsxs(
                    'section',
                    {
                      style: { display: 'flex', flexDirection: 'column', gap: 6 },
                      children: [
                        jsxs('div', {
                          style: { ...S.row, justifyContent: 'space-between' },
                          children: [
                            jsxs('span', {
                              style: { ...S.row, minWidth: 0 },
                              children: [
                                jsx(Codicon, {
                                  name: d.new_file ? 'diff-added' : 'diff-modified',
                                  style: { color: d.new_file ? 'var(--ui-green)' : 'var(--ui-yellow)' }
                                }),
                                jsx('span', {
                                  style: { ...S.mono, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                                  children: shortPath(d.path, run.cwd)
                                }),
                                d.new_file ? jsx('span', { style: S.pill('var(--ui-green)'), children: 'new' }) : null,
                                jsx('span', { style: { ...S.mono, fontSize: 11.5, color: 'var(--ui-green)' }, children: `+${d.added || 0}` }),
                                jsx('span', { style: { ...S.mono, fontSize: 11.5, color: 'var(--ui-red)' }, children: `−${d.removed || 0}` })
                              ]
                            }),
                            ctx && ctx.os && run.execution_target !== 'cloud'
                              ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => ctx.os.revealPath(d.path), children: 'Show in folder' })
                              : null
                          ]
                        }),
                        jsx(DiffView, { diff: d, full: true })
                      ]
                    },
                    d.path
                  )
                )
            })
          : jsx('div', { style: S.faint, children: detail.isLoading ? 'Loading changes…' : 'Devin has not changed any files in this run.' })
      ]
    })
  })
}

function openReview(runId, title, ctx) {
  if (typeof host.openWorkspace !== 'function') {
    host.navigate(PAGE)

    return
  }

  host.openWorkspace(`devin-review-${runId}`, {
    title: `Changes · ${String(title || runId).slice(0, 40)}`,
    render: () => jsx(ReviewPane, { runId, ctx })
  })
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

/** Open a fresh chat and send a request, retrying while the new composer mounts. */
async function askHermes(text) {
  if (!host.composer || typeof host.newChat !== 'function') {
    return false
  }

  host.newChat()

  for (let attempt = 0; attempt < 20; attempt += 1) {
    await sleep(150)

    if (host.composer.submit('new', text) || host.composer.submit(null, text)) {
      return true
    }
  }

  return host.composer.setDraft ? host.composer.setDraft(null, text) : false
}

const MODEL_INPUT = {
  minWidth: 0, width: '100%', minHeight: 32, padding: '5px 8px', borderRadius: 7,
  border: '1px solid var(--ui-stroke-secondary)', background: 'var(--ui-widget-surface-background)',
  color: 'var(--ui-text-primary)', font: 'inherit', outlineColor: 'var(--ui-accent)'
}

function groupedModels(choices, query = '') {
  const groups = new Map()
  const needle = query.trim().toLocaleLowerCase()
  for (const choice of choices || []) {
    const family = choice.family || choice.group || (choice.name.startsWith('Fusion') ? 'Fusion' : 'Models')
    if (needle && !`${family} ${choice.label || choice.name} ${choice.description || ''}`.toLocaleLowerCase().includes(needle)) {
      continue
    }
    if (!groups.has(family)) groups.set(family, [])
    groups.get(family).push(choice)
  }
  return [...groups.entries()]
}

function taskRequest(what, where, background, selection) {
  const cloud = selection?.execution_target === 'cloud'
  const task = `Have Devin${cloud ? ' Cloud' : ''} ${what.replace(/^have devin\s+/i, '')}${where && !cloud ? ` in ${where}` : ''}.${background ? ' Run it in the background and tell me when it is done.' : ''}`
  return selection
    ? `${task}\nUse the saved launch_id ${JSON.stringify(selection.launch_id)} exactly in delegate_to_devin. Keep it out of the task prompt. ${cloud ? 'The saved settings target Devin Cloud; do not send local files, folders or MCP servers.' : `The saved settings belong to ${JSON.stringify(selection.cwd)};`} Do not substitute model, target or permission arguments.`
    : task
}

function ModelFields({ data, loading, onChange }) {
  const [search, setSearch] = useState('')
  const cloud = data.execution_target === 'cloud'
  const modelId = cloud ? 'devin_version' : 'model'
  const model = (data.config_options || []).find(option => option.id === modelId)
  const picked = model && model.options.find(choice => choice.value === model.currentValue)
  const groups = model ? groupedModels(model.options, search) : []
  const visible = groups.some(([, choices]) => choices.some(choice => choice.value === model.currentValue))
  if (model && picked && !visible) groups.unshift(['Selected', [picked]])

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 10 },
    children: [
      model && !cloud ? jsxs('label', {
        style: { display: 'flex', flexDirection: 'column', gap: 5, fontSize: 12 },
        children: ['Search models and Fusion pairings', jsx('input', {
          type: 'search', value: search, placeholder: 'Search by family, lead or sidekick',
          onChange: event => setSearch(event.target.value), style: MODEL_INPUT
        })]
      }) : null,
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 10 },
        children: (data.config_options || []).map(option => jsxs('label', {
          style: { flex: option.id === modelId || option.id === 'repos' ? '2 1 300px' : '1 1 140px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 5, fontSize: 12 },
          children: [option.name, jsx('select', {
            value: option.currentValue, disabled: loading, style: MODEL_INPUT,
            onChange: event => onChange(option, event.target.value),
            children: option.id === modelId && !cloud
              ? groups.map(([family, choices]) => jsx('optgroup', { label: family, children: choices.map(choice => jsx('option', {
                value: choice.value, children: choice.label || choice.name
              }, choice.value)) }, family))
              : option.options.map(choice => jsx('option', { value: choice.value, children: choice.name }, choice.value))
          })]
        }, option.id))
      }),
      search && groups.every(([family]) => family === 'Selected')
        ? jsx('span', { role: 'status', style: { ...S.muted, fontSize: 12 }, children: 'No matching models. Clear the search to see the available choices.' }) : null,
      jsxs('div', {
        style: { fontSize: 12, ...S.muted, display: 'flex', flexDirection: 'column', gap: 4, overflowWrap: 'anywhere' },
        children: [
          jsx('span', { children: cloud ? 'Cloud · Remote repositories · Cloud-managed permissions' : `Local · ${data.cwd} · ${data.permission_mode} permissions` }),
          cloud ? jsx('span', { children: 'Your local folder, uncommitted changes and local MCP servers are not uploaded.' }) : null,
          jsx('span', { children: picked && picked.cost_summary ? `Catalog pricing: ${picked.cost_summary}` : 'Pricing not supplied; usage may incur charges.' }),
          jsx('span', { children: 'Availability is checked again at launch. Catalog prices are not a usage quote. These choices affect Devin, not the Hermes chat model.' })
        ]
      })
    ]
  })
}

function NewTask() {
  const [task, setTask] = useState('')
  const [folder, setFolder] = useState('')
  const [background, setBackground] = useState(true)
  const [busy, setBusy] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [choices, setChoices] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [target, setTarget] = useState('local')
  const [cloudAck, setCloudAck] = useState(false)
  const serial = useRef(0)

  useEffect(() => () => { serial.current += 1 }, [])

  const discover = async (values, selectedTarget = target, consent = cloudAck) => {
    const request = ++serial.current
    setLoading(true)
    setError('')
    try {
      const body = { cwd: selectedTarget === 'cloud' ? '' : folder.trim(), target: selectedTarget, cloud_permissions_ack: consent }
      if (values !== undefined) body.values = values
      const data = await rest('/session-options', { method: 'POST', body })
      if (request !== serial.current) return null
      if (!data || !data.launch_id || !(data.config_options || []).length) {
        throw new Error('This CLI did not return model controls. Update Devin CLI or use plugin defaults explicitly.')
      }
      setChoices(data)
      return data
    } catch (err) {
      if (request === serial.current) {
        setChoices(null)
        setError(err.message || 'Could not load models. Check CLI login and folder access, then retry.')
      }
      return null
    } finally {
      if (request === serial.current) setLoading(false)
    }
  }

  const changeOption = (option, value) => {
    // Re-read conditional thinking/speed controls after the model changes.
    const values = option.id === 'model' || option.id === 'org_id' ? { [option.id]: value } : { ...choices.values, [option.id]: value }
    void discover(values)
  }

  const start = async () => {
    const what = task.trim()

    if (!what || busy || loading || (advanced && !choices) || (target === 'cloud' && !cloudAck)) {
      return
    }

    const where = folder.trim()
    setBusy(true)
    const selection = advanced ? await discover(choices.values) : null
    if (advanced && !selection) {
      setBusy(false)
      return
    }
    const text = taskRequest(what, where, background, selection)
    const sent = await askHermes(text)
    setBusy(false)

    if (sent) {
      setTask('')
    } else {
      host.notify({ kind: 'error', message: 'Could not open a chat for the request. Start a chat and ask Hermes there.' })
    }
  }

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
      padding: '14px 16px',
      borderRadius: 10,
      border: '1px solid color-mix(in srgb, var(--ui-accent) 35%, transparent)',
      background: 'color-mix(in srgb, var(--ui-accent) 5%, transparent)'
    },
    children: [
      jsxs('div', {
        style: { ...S.row, gap: 8 },
        children: [jsx(Codicon, { name: 'sparkle', style: { color: 'var(--ui-accent)' } }), jsx('span', { style: { fontWeight: 600 }, children: 'New Devin task' })]
      }),
      jsxs('label', {
        style: { ...S.row, flexWrap: 'wrap', gap: 10, fontSize: 12.5 },
        children: ['Run on', jsx('select', {
          'aria-label': 'Execution target', value: target, disabled: busy,
          style: { ...MODEL_INPUT, width: 180 },
          onChange: event => {
            const value = event.target.value
            serial.current += 1
            setTarget(value); setCloudAck(false); setChoices(null); setLoading(false); setError('')
            setAdvanced(value === 'cloud')
            if (value === 'cloud') void discover(undefined, value, false)
          },
          children: [jsx('option', { value: 'local', children: 'Local computer' }, 'local'), jsx('option', { value: 'cloud', children: 'Devin Cloud' }, 'cloud')]
        })]
      }),
      jsx(Textarea, {
        value: task,
        rows: 2,
        placeholder: 'What should Devin do? For example: add type hints to src/utils.py and run the tests',
        onChange: event => setTask(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
            event.preventDefault()
            void start()
          }
        },
        style: { fontSize: 13, resize: 'vertical', minHeight: 54 }
      }),
      jsxs('div', {
        style: { ...S.row, flexWrap: 'wrap', gap: 10 },
        children: [
          target === 'local' ? jsx('input', {
            value: folder,
            'aria-label': 'Local folder',
            disabled: busy,
            onChange: event => {
              serial.current += 1
              setFolder(event.target.value)
              setChoices(null)
              setLoading(false)
              setError('')
            },
            placeholder: 'Folder (optional), e.g. E:\\Dev\\myapp',
            style: {
              flex: '1 1 260px',
              minWidth: 0,
              height: 28,
              padding: '0 9px',
              borderRadius: 7,
              border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 55%, transparent)',
              background: 'transparent',
              color: 'var(--ui-text-primary)',
              font: 'inherit',
              fontSize: 12.5
            }
          }) : null,
          jsxs('label', {
            style: { ...S.row, gap: 6, ...S.muted, fontSize: 12.5, cursor: 'pointer' },
            children: [
              jsx('input', { type: 'checkbox', checked: background, onChange: event => setBackground(event.target.checked) }),
              'In the background'
            ]
          }),
          jsx(Button, { size: 'sm', variant: 'default', disabled: busy || loading || !task.trim() || (advanced && !choices) || (target === 'cloud' && !cloudAck), onClick: start, children: busy ? 'Preparing task…' : 'Ask Hermes' })
        ]
      }),
      jsxs('div', {
        style: { borderTop: '1px solid var(--ui-stroke-secondary)', paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 10 },
        children: [
          jsxs('div', {
            style: { ...S.row, flexWrap: 'wrap' },
            children: [
              jsx(Button, {
                size: 'sm', variant: 'ghost', disabled: busy || loading, 'aria-expanded': advanced,
                onClick: () => { setAdvanced(true); void discover() },
                children: target === 'cloud' ? 'Refresh Cloud choices' : advanced ? 'Refresh model choices' : 'Choose a model or Fusion pairing'
              }),
              advanced && target === 'local' ? jsx(Button, {
                size: 'sm', variant: 'ghost', disabled: busy,
                onClick: () => { serial.current += 1; setAdvanced(false); setLoading(false); setChoices(null); setError('') },
                children: 'Use plugin defaults'
              }) : target === 'local' ? jsx('span', { style: { ...S.muted, fontSize: 12 }, children: 'Local · Plugin defaults' }) : null,
              loading ? jsxs('span', { role: 'status', style: { ...S.row, ...S.muted, fontSize: 12 }, children: [jsx(GlyphSpinner, {}), 'Checking Devin CLI…'] }) : null
            ]
          }),
          error ? jsxs('div', {
            role: 'alert', style: { fontSize: 12, color: 'var(--ui-red)', overflowWrap: 'anywhere' },
            children: [jsx('p', { style: { margin: '0 0 6px' }, children: error }), jsx(Button, {
              size: 'sm', variant: 'ghost', disabled: loading || busy, onClick: () => { void discover({}) }, children: 'Reload available models'
            })]
          }) : null,
          advanced && choices ? jsx(ModelFields, { data: choices, loading: loading || busy, onChange: changeOption }) : null,
          target === 'cloud' ? jsxs('label', {
            style: { ...S.row, alignItems: 'flex-start', gap: 8, fontSize: 12.5, lineHeight: 1.5 },
            children: [jsx('input', { type: 'checkbox', checked: cloudAck, disabled: busy,
              onChange: event => setCloudAck(event.target.checked), style: { marginTop: 3 } }),
            'I understand that Devin Cloud manages permissions and may incur charges. Hermes Local approvals and sandbox settings do not govern remote actions.']
          }) : null,
          advanced && !choices && !error && !loading ? jsx('span', {
            role: 'status', style: { ...S.muted, fontSize: 12 }, children: 'The folder changed. Refresh model choices before starting.'
          }) : null
        ]
      }),
      jsx('span', {
        style: { ...S.faint, fontSize: 11.5 },
        children: 'Opens a new chat with this request. Hermes writes the brief for Devin, and the run waits for your approval as usual.'
      })
    ]
  })
}

// --- the page --------------------------------------------------------------------

function Stat({ label, value, tone }) {
  return jsxs('div', {
    style: {
      flex: '1 1 0',
      minWidth: 110,
      padding: '10px 12px',
      borderRadius: 9,
      border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)',
      display: 'flex',
      flexDirection: 'column',
      gap: 2
    },
    children: [
      jsx('span', { style: { fontSize: 20, fontWeight: 650, color: tone || 'var(--ui-text-primary)' }, children: value }),
      jsx('span', { style: S.label, children: label })
    ]
  })
}

function HistoryRow({ run, now, onOpen }) {
  const state = STATE[run.state] || { label: run.state, color: 'var(--ui-text-quaternary)' }
  const changed = run.files_changed || []
  const detail = run.error
    ? `${run.error.code}: ${run.error.message}`
    : changed.length
      ? changed.slice(0, 4).map(f => shortPath(f, run.cwd)).join(', ') + (changed.length > 4 ? ` +${changed.length - 4}` : '')
      : run.last_activity || ''

  return jsxs('button', {
    type: 'button',
    className: 'devin-row',
    onClick: onOpen,
    style: {
      display: 'grid',
      gridTemplateColumns: '14px 1fr auto',
      columnGap: 10,
      rowGap: 2,
      alignItems: 'center',
      width: '100%',
      padding: '9px 10px',
      borderRadius: 8,
      border: 'none',
      background: 'transparent',
      color: 'var(--ui-text-primary)',
      cursor: 'pointer',
      textAlign: 'left',
      font: 'inherit'
    },
    children: [
      jsx('span', { style: { width: 8, height: 8, borderRadius: 999, background: state.color, justifySelf: 'center' } }),
      jsx('span', {
        style: { fontWeight: 550, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
        children: run.title
      }),
      jsxs('span', {
        style: { ...S.faint, ...S.row, fontSize: 12, justifySelf: 'end' },
        children: [
          jsx('span', { children: baseName(run.cwd) }),
          jsx('span', { style: S.mono, children: elapsed(run, now) }),
          jsx('span', { style: { color: state.color }, children: state.label }),
          jsx('span', { children: ago(run.finished_at || run.created_at, now) })
        ]
      }),
      jsx('span', {}),
      detail
        ? jsx('span', {
            style: {
              gridColumn: '2 / 4',
              fontSize: 12,
              color: run.error ? 'color-mix(in srgb, var(--ui-red) 80%, var(--ui-text-tertiary))' : 'var(--ui-text-tertiary)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              ...(run.error ? {} : S.mono)
            },
            children: detail
          })
        : null
    ]
  })
}

function DevinPage({ ctx }) {
  const owner = useValue($owner)
  const runs = useValue($runs)
  const backend = useValue($backend)
  const [open, setOpen] = useState(null)
  const now = useNow(true)

  useEffect(() => {
    setOpen(null)
    void refreshRuns()
  }, [owner])

  const all = Object.values(runs).sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
  const active = all.filter(isActive)
  const ids = new Set(all.map(r => r.id))
  const finished = all.filter(r => !isActive(r) && !(r.parent_run_id && ids.has(r.parent_run_id)))
  const succeeded = finished.filter(r => r.state === 'succeeded').length
  const filesTouched = new Set(all.flatMap(r => r.files_changed || [])).size

  const section = (title, children, extra) =>
    jsxs('section', {
      style: { display: 'flex', flexDirection: 'column', gap: 10 },
      children: [
        jsxs('div', {
          style: { ...S.row, justifyContent: 'space-between' },
          children: [jsx('div', { style: S.label, children: title }), extra || null]
        }),
        children
      ]
    })

  return jsx('div', {
    style: { height: '100%', overflow: 'auto' },
    children: jsxs('div', {
      style: { maxWidth: 1040, margin: '0 auto', padding: '28px 28px 56px', display: 'flex', flexDirection: 'column', gap: 26 },
      children: [
        jsxs('header', {
          style: { display: 'flex', flexDirection: 'column', gap: 14 },
          children: [
            jsx(BrandHeader, { live: active.length > 0, active: active.length }),
            all.length
              ? jsxs('div', {
                  style: { display: 'flex', flexWrap: 'wrap', gap: 10 },
                  children: [
                    jsx(Stat, { label: 'Working now', value: active.length, tone: active.length ? 'var(--ui-accent)' : undefined }),
                    jsx(Stat, { label: 'Finished', value: finished.length }),
                    jsx(Stat, {
                      label: 'Succeeded',
                      value: finished.length ? `${Math.round((100 * succeeded) / finished.length)}%` : '–',
                      tone: 'var(--ui-green)'
                    }),
                    jsx(Stat, { label: 'Files changed', value: filesTouched })
                  ]
                })
              : null
          ]
        }),
        jsx(NewTask, {}, owner),
        backend === 'off'
          ? jsx('div', {
              style: { ...S.card, ...S.muted },
              children:
                'The Devin backend is not reachable. Make sure the devin plugin is enabled in Hermes (hermes plugins enable devin) and restart the Hermes backend.'
            })
          : null,
        section(
          'Active',
          active.length
            ? jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 12 },
                children: active.map(r => jsx(RunCard, { runId: r.id, ctx, wide: true }, r.id))
              })
            : jsxs('div', {
                style: {
                  border: '1px dashed color-mix(in srgb, var(--ui-stroke-secondary) 60%, transparent)',
                  borderRadius: 10,
                  padding: '18px 16px',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4
                },
                children: [
                  jsx('span', { style: { color: 'var(--ui-text-secondary)' }, children: 'Nothing running right now.' }),
                  jsx('span', {
                    style: S.faint,
                    children:
                      'Ask Hermes in any chat, for example: "Have Devin add type hints to src/utils.py in my project and run the tests."'
                  })
                ]
              })
        ),
        section(
          'History',
          finished.length
            ? jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 2 },
                children: finished.slice(0, 50).map(r =>
                  open === r.id
                    ? jsx('div', { style: { padding: '6px 0' }, children: jsx(RunCard, { runId: r.id, ctx, wide: true }) }, r.id)
                    : jsx(HistoryRow, { run: r, now, onOpen: () => setOpen(r.id) }, r.id)
                )
              })
            : jsx('div', { style: S.faint, children: 'No finished runs yet.' }),
          open ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setOpen(null), children: 'Collapse' }) : null
        )
      ]
    })
  })
}

// --- status bar ------------------------------------------------------------------

function DevinChip() {
  const runs = useValue($runs)
  const active = Object.values(runs).filter(isActive)

  if (active.length === 0) {
    return null
  }

  const latest = active.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))[0]
  const waiting = active.flatMap(r => (r.pending_permissions || []).filter(p => p.via === 'card'))

  return jsx(Tip, {
    label: waiting.length
      ? `Devin asks to: ${waiting[0].title}`
      : latest.last_activity
        ? `Devin: ${latest.last_activity}`
        : `Devin: ${latest.title}`,
    children: jsxs('button', {
      type: 'button',
      onClick: () => host.navigate(PAGE),
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        height: '100%',
        padding: '0 7px',
        border: 'none',
        background: 'transparent',
        color: waiting.length ? 'var(--ui-yellow)' : 'var(--ui-accent)',
        fontSize: 11,
        cursor: 'pointer'
      },
      children: waiting.length
        ? [jsx(Codicon, { name: 'shield', style: { fontSize: 12 } }), jsx('span', { children: 'Devin needs you' })]
        : [
            jsx(DevinMark, { size: 12 }),
            jsx(GlyphSpinner, { ariaLabel: 'Devin is working' }),
            jsx('span', { children: active.length === 1 ? 'Devin working' : `Devin · ${active.length} running` })
          ]
    })
  })
}

// For tests/desktop_smoke.mjs; Hermes Desktop only reads the default export.
export const __testing = { ReviewPane, askHermes, ModelFields, groupedModels, taskRequest, liveViews }

// --- plugin ----------------------------------------------------------------------

export default {
  id: ID,
  name: 'Devin',
  description: 'Live Devin run cards in chat, a Devin runs page and a status-bar indicator.',
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.appendChild(style)
    ctx.onDispose(() => style.remove())

    const announced = new Set()
    const lastLine = new Map()
    const resetOwner = () => {
      const next = ownerKey()
      if (next === $owner.get()) return
      ownerRevision += 1
      profileScope = null
      $runs.set({})
      $live.set({})
      $backend.set('unknown')
      announced.clear()
      lastLine.clear()
      $owner.set(next)
      void refreshRuns()
    }
    for (const store of [host.state.profile, host.state.connectionId]) {
      if (store?.listen) ctx.onDispose(store.listen(resetOwner))
    }
    resetOwner()
    ctx.onDispose(() => {
      ownerRevision += 1
      rest = null
      profileScope = null
      $runs.set({})
      $live.set({})
    })

    // Short progress lines on the pet while Devin edits files or runs commands, at most one
    // every 12 seconds per run.
    const narrate = payload => {
      const event = payload.event

      if (!event || (event.kind !== 'diff' && event.kind !== 'terminal')) {
        return
      }

      const now = Date.now()

      if (now - (lastLine.get(payload.run_id) || 0) < 12000) {
        return
      }

      const data = event.data || {}
      const line =
        event.kind === 'diff'
          ? `Devin is ${data.new_file ? 'creating' : 'editing'} ${baseName(data.path)}`
          : data.status === 'running' && data.command
            ? `Devin is running ${String(data.command).slice(0, 80)}`
            : ''

      if (line) {
        lastLine.set(payload.run_id, now)
        ctx.pet?.say(line, { id: `devin-progress-${payload.run_id}`, tone: 'info', ttlMs: 5000 })
      }
    }

    const onRunEvent = frame => {
      const payload = frame && frame.payload

      if (!payload || !payload.run || !RUN_ID.test(payload.run_id || '')) {
        return
      }
      // Shared gateways broadcast across profiles. Older events have no scope;
      // only update known runs, discovering new ones through the scoped REST API.
      if (payload.profile_scope ? payload.profile_scope !== profileScope : !$runs.get()[payload.run_id]) {
        void refreshRuns()
        return
      }

      mergeRuns([payload.run])
      pushLive(payload.run_id, payload.event)
      narrate(payload)

      if (payload.type === 'run.finished' || payload.type === 'run.state') {
        void queryClient.invalidateQueries({ queryKey: [ID, 'run', payload.run_id] })
      }

      for (const ask of payload.run.pending_permissions || []) {
        if (ask.via === 'card' && !announced.has(ask.ask_id)) {
          announced.add(ask.ask_id)
          const line = `Devin asks to: ${ask.title}`
          ctx.pet?.say(line.slice(0, 120), { id: `devin-ask-${ask.ask_id}`, tone: 'wait', ttlMs: 15000 })
          ctx.os?.notify({ title: 'Devin needs your OK', body: `${payload.run.title}: ${ask.title}`, activate: PAGE })
        }
      }

      if (payload.type === 'run.finished' && !announced.has(payload.run_id)) {
        announced.add(payload.run_id)

        const run = payload.run
        const ok = run.state === 'succeeded'
        const files = (run.files_changed || []).length
        const line = ok
          ? `Devin finished "${run.title}"${files ? ` · ${files} file${files === 1 ? '' : 's'} changed` : ''}`
          : `Devin run ${(STATE[run.state] || {}).label || run.state}: ${run.title}`

        ctx.pet?.say(line.slice(0, 120), { id: `devin-${run.id}`, tone: ok ? 'info' : 'error', ttlMs: 9000 })
        ctx.os?.notify({ title: ok ? 'Devin finished' : 'Devin run ended', body: line, activate: PAGE })
      }
    }

    for (const type of EVENT_TYPES) {
      ctx.onEvent(`plugin.${ID}.${type}`, onRunEvent)
    }

    // Initial load, then a slow poll as the fallback for missed events.
    void refreshRuns()
    ctx.setInterval(() => {
      const anyActive = Object.values($runs.get()).some(isActive)

      if (anyActive || Math.random() < 0.2) {
        void refreshRuns()
      }
    }, 5000)

    ctx.registerMany([
      {
        id: 'run-card',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'devin-run',
          render: ({ attrs }) => {
            const runId = String((attrs && attrs.id) || '')

            return RUN_ID.test(runId) ? jsx('div', { style: { margin: '6px 0' }, children: jsx(RunCard, { runId, ctx }) }) : null
          }
        }
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PAGE },
        render: () => jsx(DevinPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: { path: PAGE, label: 'Devin', codicon: 'hubot' }
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 70,
        render: () => jsx(DevinChip, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'devin.open',
          label: 'Devin: Show runs',
          keywords: ['devin', 'runs', 'delegate', 'coding agent'],
          run: () => host.navigate(PAGE)
        }
      }
    ])
  }
}
