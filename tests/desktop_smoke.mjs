// Render smoke test for desktop/plugin.js.
//
// The Desktop half is plain ESM that only runs inside Hermes Desktop, so a
// typo in a jsx() call is a ReferenceError nobody sees until a run renders.
// This loads the file with a stub `@hermes/plugin-sdk` and the real React,
// registers it, feeds it run events and renders every surface to a string.
//
// Usage: node tests/desktop_smoke.mjs <node_modules dir that has react + react-dom>

import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const modules = process.argv[2]
const require = createRequire(path.join(modules, 'noop.js'))
const reactUrl = pathToFileURL(require.resolve('react')).href
const jsxUrl = pathToFileURL(require.resolve('react/jsx-runtime')).href
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

const failures = []
const check = (name, ok, detail = '') => {
  if (!ok) {
    failures.push(`${name}${detail ? `: ${detail}` : ''}`)
  }
}

// --- fixtures ---------------------------------------------------------------------

const now = Date.now() / 1000
const running = {
  id: 'dv-aaaaaaaaaa', title: 'Add type hints', state: 'running', background: true, cwd: 'C:\\work\\app',
  permission_mode: 'accept-edits', backend: 'acp', created_at: now - 40, started_at: now - 39, updated_at: now,
  tool_calls: 3, files_changed: ['C:\\work\\app\\src\\utils.py'], permission_requests: 1,
  last_activity: 'Edit src/utils.py', usage: { tokens: { totalTokens: 15400 } },
  pending_permissions: [{ ask_id: 'abc123def456', title: 'Run `pytest -q`', kind: 'execute', via: 'card', expires_at: now + 200 }]
}
const failed = {
  id: 'dv-bbbbbbbbbb', title: 'Run the tests', state: 'failed', background: false, cwd: 'C:\\work\\app',
  permission_mode: 'normal', backend: 'print', created_at: now - 600, finished_at: now - 590, updated_at: now - 590,
  tool_calls: 0, files_changed: [], error: { code: 'permission_denied', message: 'Devin stopped without an answer' }
}
const done = {
  id: 'dv-cccccccccc', title: 'Create hello.txt', state: 'succeeded', background: false, cwd: 'C:\\work\\app',
  permission_mode: 'accept-edits', backend: 'acp', created_at: now - 300, finished_at: now - 290, updated_at: now - 290,
  tool_calls: 2, files_changed: ['C:\\work\\app\\hello.txt']
}
const followUp = {
  id: 'dv-dddddddddd', title: 'Follow-up: also add tests', state: 'succeeded', background: true, cwd: 'C:\\work\\app',
  permission_mode: 'accept-edits', backend: 'acp', parent_run_id: 'dv-cccccccccc', created_at: now - 200,
  finished_at: now - 190, updated_at: now - 190, tool_calls: 1, files_changed: []
}
const details = {
  [running.id]: {
    ...running,
    plan: [
      { content: 'Read utils.py', status: 'completed' },
      { content: 'Add hints', status: 'in_progress' },
      { content: 'Run tests', status: 'pending' }
    ],
    permissions: [{ decision: 'allowed', title: 'Run pytest', kind: 'execute' }],
    diffs: [
      {
        path: 'C:\\work\\app\\src\\utils.py',
        patch: '@@ -1,2 +1,2 @@\n-def add(a, b):\n+def add(a: int, b: int) -> int:\n     return a + b',
        added: 1,
        removed: 1,
        new_file: false
      }
    ],
    commands: [{ tool_call_id: 'x1', command: 'pytest -q', output: '3 passed', exit_code: 0, status: 'completed' }],
    events: [
      { seq: 1, kind: 'tool_call', text: 'Read src/utils.py', data: { tool_kind: 'read' } },
      { seq: 2, kind: 'tool_call', text: 'Edit src/utils.py', data: { tool_kind: 'edit' } },
      { seq: 3, kind: 'permission', text: 'allowed: Run pytest', data: {} }
    ]
  },
  [failed.id]: { ...failed, events: [], permissions: [] },
  [done.id]: { ...done, events: [], permissions: [], plan: [], output: 'Created **hello.txt**.', devin_session_id: 'stump-hexagon' },
  [followUp.id]: { ...followUp, events: [], permissions: [], plan: [], output: 'Added tests.', devin_session_id: 'stump-hexagon' }
}

// --- the stub SDK -----------------------------------------------------------------

globalThis.__devinSmoke = { React, details, listeners: {}, contributions: [], notices: [] }

const sdk = `
const { React, details, notices } = globalThis.__devinSmoke
const h = React.createElement
export function atom(value) {
  const subs = new Set()
  return { get: () => value, set: next => { value = next; subs.forEach(fn => fn(value)) }, listen: fn => (subs.add(fn), () => subs.delete(fn)) }
}
export function useValue(store) {
  return React.useSyncExternalStore(store.listen, store.get, store.get)
}
export function useQuery({ queryKey, enabled }) {
  return { data: enabled === false ? undefined : details[queryKey[2]], isError: false }
}
export const queryClient = { invalidateQueries: () => Promise.resolve() }
export const host = {
  notify: n => notices.push(n), navigate: () => {}, onEvent: () => () => {},
  state: {}, request: () => Promise.resolve({}),
  newChat: () => notices.push({ newChat: true }),
  composer: { submit: (address, text) => (notices.push({ submit: [address, text] }), true), setDraft: async () => true },
  openWorkspace: (id, options) => (notices.push({ workspace: id, title: options.title }), () => {})
}
export const Button = ({ children, onClick, disabled }) => h('button', { onClick, disabled }, children)
export const Codicon = ({ name }) => h('i', { 'data-icon': name })
export const GlyphSpinner = () => h('span', null, '*')
export const Tip = ({ children }) => children
export const Textarea = props => h('textarea', { placeholder: props.placeholder, disabled: props.disabled })
export const MessageTextContent = ({ text }) => h('div', { 'data-md': true }, text)
export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebarNav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directive'
`

// --- load the plugin --------------------------------------------------------------

const dir = mkdtempSync(path.join(tmpdir(), 'devin-desktop-smoke-'))
const sdkFile = path.join(dir, 'sdk.mjs')
writeFileSync(sdkFile, sdk)

const source = readFileSync(path.join(here, '..', 'desktop', 'plugin.js'), 'utf8')
const imports = [...source.matchAll(/from\s+'([^']+)'/g)].map(m => m[1])
check('imports stay inside the SDK surface', imports.every(s => ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(s)), imports.join(', '))

const rewritten = source
  .replace(/from\s+'@hermes\/plugin-sdk'/g, `from '${pathToFileURL(sdkFile).href}'`)
  .replace(/from\s+'react\/jsx-runtime'/g, `from '${jsxUrl}'`)
  .replace(/from\s+'react'/g, `from '${reactUrl}'`)
const pluginFile = path.join(dir, 'plugin.mjs')
writeFileSync(pluginFile, rewritten)

globalThis.document = {
  createElement: () => ({ dataset: {}, remove() {}, set textContent(_) {} }),
  head: { appendChild() {} }
}

const pluginModule = await import(pathToFileURL(pluginFile).href)
const plugin = pluginModule.default
check('default export has id devin', plugin && plugin.id === 'devin')

const state = globalThis.__devinSmoke
const restCalls = []
const ctx = {
  source: 'plugin:devin',
  rest: async (p, opts) => {
    restCalls.push([p, opts])
    if (p.startsWith('/runs?')) {
      return { runs: [running, failed, done, followUp], live: true }
    }
    return details[p.split('/')[2]?.split('?')[0]] || {}
  },
  register: c => (state.contributions.push(c), () => {}),
  registerMany: cs => (state.contributions.push(...cs), () => {}),
  onEvent: (type, fn) => ((state.listeners[type] ||= []).push(fn), () => {}),
  onDispose: () => {},
  setInterval: () => () => {},
  setTimeout: () => () => {},
  os: { revealPath: async () => true, notify: n => state.notices.push(n) },
  pet: { say: line => state.notices.push({ pet: line }) },
  storage: { get: (_, f) => f, set() {}, remove() {} }
}

plugin.register(ctx)
await new Promise(r => setTimeout(r, 20))

const byArea = area => state.contributions.filter(c => c.area === area)
check('registers a devin-run directive', byArea('transcript.directive').some(c => c.data && c.data.name === 'devin-run'))
check('registers the /devin page', byArea('routes').some(c => c.data && c.data.path === '/devin'))
check('registers a sidebar row', byArea('sidebarNav').length === 1)
check('registers a status-bar chip', byArea('statusBar.right').length === 1)
check('subscribes to run events', Object.keys(state.listeners).includes('plugin.devin.run.progress'))

// Live event: the running run moves.
for (const fn of state.listeners['plugin.devin.run.progress'] || []) {
  fn({ type: 'plugin.devin.run.progress', payload: { type: 'run.progress', run_id: running.id, run: running, event: { seq: 4, kind: 'tool_call', text: 'Edit src/models.py', data: { tool_kind: 'edit' } } } })
}

const render = (name, node, expect = []) => {
  try {
    const html = renderToStaticMarkup(node)
    for (const text of expect) {
      check(`${name} shows ${JSON.stringify(text)}`, html.includes(text), html.slice(0, 300))
    }
    return html
  } catch (error) {
    check(`${name} renders`, false, error && error.stack ? error.stack.split('\n').slice(0, 3).join(' | ') : String(error))
    return ''
  }
}

const directive = byArea('transcript.directive')[0].data
for (const fn of state.listeners['plugin.devin.run.progress'] || []) {
  fn({ type: 'plugin.devin.run.progress', payload: { type: 'run.progress', run_id: running.id, run: running, event: { seq: 5, kind: 'terminal', text: 'npm test', data: { tool_call_id: 'x2', command: 'npm test', output: 'tick 0\ntick 1', status: 'running' } } } })
}
render('running card', directive.render({ attrs: { id: running.id }, streaming: false }),
  ['Add type hints', 'Working', 'utils.py', 'Read utils.py', 'Stop', 'Edit src/models.py', '15k tokens',
   'Devin asks to', 'Run `pytest -q`', 'Allow', 'Deny', 'Denied automatically in',
   'Changes · 1 file', '+def add(a: int, b: int) -&gt; int:', 'following Devin', 'pytest -q', 'exit 0', 'npm test', 'tick 1'])
const thread = render('finished card', directive.render({ attrs: { id: done.id }, streaming: false }),
  ['Create hello.txt', 'Done', 'hello.txt', 'Follow-up: also add tests', 'Send to Devin', 'Tell Devin what to do next'])
check('the follow-up box sits only on the newest run of a thread', (thread.match(/Send to Devin/g) || []).length === 1)
render('failed card', directive.render({ attrs: { id: failed.id }, streaming: false }), ['permission_denied'])
check('directive ignores a bad id', directive.render({ attrs: { id: '../x' }, streaming: false }) === null)
const page = render('page', byArea('routes')[0].render(), ['Devin', 'Add type hints', 'Run the tests', 'permission_denied', 'Create hello.txt'])
check('follow-ups are not listed as separate history rows', !page.includes('Follow-up: also add tests'))
render('review tab', React.createElement(pluginModule.__testing.ReviewPane, { runId: running.id, ctx }),
  ['Add type hints', 'utils.py', '+def add(a: int, b: int) -&gt; int:', 'Show in folder'])
check('the card offers a review', render('card again', directive.render({ attrs: { id: running.id }, streaming: false })).includes('Review changes (1)'))
check('the page offers a new task', render('page again', byArea('routes')[0].render()).includes('New Devin task'))
check('askHermes opens a chat and sends', await pluginModule.__testing.askHermes('Have Devin fix it.') === true
  && state.notices.some(n => n.newChat) && state.notices.some(n => n.submit && n.submit[1] === 'Have Devin fix it.'))
for (const fn of state.listeners['plugin.devin.run.progress'] || []) {
  fn({ type: 'plugin.devin.run.progress', payload: { type: 'run.progress', run_id: running.id, run: running, event: { seq: 9, kind: 'diff', text: 'Edited models.py', data: { path: 'C:\\work\\app\\models.py', new_file: false } } } })
  fn({ type: 'plugin.devin.run.progress', payload: { type: 'run.progress', run_id: running.id, run: running, event: { seq: 10, kind: 'diff', text: 'Edited a.py', data: { path: 'a.py' } } } })
}
check('the pet narrates edits, throttled', state.notices.filter(n => n.pet && n.pet.startsWith('Devin is ')).length === 1, JSON.stringify(state.notices))
render('status chip', byArea('statusBar.right')[0].render(), ['Devin needs you'])
check('a waiting request alerts once', state.notices.filter(n => n.pet && n.pet.startsWith('Devin asks to')).length === 1, JSON.stringify(state.notices))

// Finishing announces once.
for (const fn of state.listeners['plugin.devin.run.finished'] || []) {
  const payload = { type: 'run.finished', run_id: done.id, run: done }
  fn({ type: 'plugin.devin.run.finished', payload })
  fn({ type: 'plugin.devin.run.finished', payload })
}
check('finish announces once on the pet', state.notices.filter(n => n.pet && n.pet.startsWith('Devin finished')).length === 1, JSON.stringify(state.notices))

if (failures.length) {
  console.error(`desktop smoke: ${failures.length} failure(s)`)
  for (const f of failures) {
    console.error(` - ${f}`)
  }
  process.exit(1)
}

console.log('desktop smoke: ok')
