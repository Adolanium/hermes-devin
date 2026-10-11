# Cloud sessions

Open **Devin → New Devin task** in Hermes Desktop and select **Devin Cloud**.
The agent plugin must be enabled with `backend: acp`, and the standalone Devin CLI
must support `devin acp --cloud` and be signed in. Devin Desktop is not required.

## Choose the target and settings

Cloud choices come from a temporary ACP session, not a hard-coded model or repository
catalog. Discovery sends no `session/prompt`; browsing does not launch a task.
The session can advertise organization, repository, persona, model preset and platform
menus. Only the controls and choices it actually supplies are shown. Changing the
organization reloads its defaults and choices rather than reusing another organization's
repository selection. Fusion and other presets remain subject to account access.
Missing pricing means unknown, not free.

Cloud uses its own VM and repositories. The plugin does not upload the selected Local
folder, local files, uncommitted changes or local MCP servers. It sends ACP a neutral `/`
working directory, advertises no client filesystem or terminal capability and refuses
requests to access those Local resources. A Cloud run's stored `cwd` is only its
Hermes-side launch binding; it is not the Cloud workspace.

Confirm **Cloud-managed permissions** before **Ask Hermes**. This acknowledges remote
permissions and possible charges; Hermes Local permission modes, sandbox and per-tool
approval cards do not govern remote actions. It does not bypass the separate Hermes
approval to start the task. That approval shows the target and confirmed choices.

The launch reference is single-use, expires after 15 minutes and is bound to settings,
target and consent. Hermes cannot replace it with raw target, model or permission
arguments. Before sending a task, the backend verifies every approved configuration
value. It refuses unavailable choices and cancels on an observed unauthorized change
rather than silently accepting another model.

Approved choices are saved separately from Local defaults within the active Hermes
profile when `persist_runs` is enabled. Browsing or denying a launch saves nothing.
Switching targets clears the current picker and Cloud consent. These choices do not
change Hermes' chat model or the CLI's global defaults.

## Stop and reconnect

**Stop**, a timeout or host shutdown sends `session/cancel` before cleaning up the local
relay. Remote cancellation is confirmed only when the original pending prompt returns
`stopReason: cancelled`. A disconnected relay without that response leaves
**Remote activity unknown**, and Cloud work may continue. Open the Cloud session to
check it. Stopping the local relay is not a substitute for remote cancellation.

Completed turns keep the session available; they do not destroy the Cloud VM. A normal
completed or confirmed-cancelled turn can receive a follow-up through ACP `session/load`.
The server must advertise load support, and the approved target, consent and configuration
are preserved. Unknown remote activity blocks another prompt instead of risking duplicate
work.

**Reconnect** checks a recorded session using `session/load`, never sends a prompt or
changes its configuration, then closes the probe relay. It verifies the saved choices
but cannot prove remote work is idle. It does not clear an unknown-activity warning or
automatically enable follow-ups. It is not a persistent connection, session listing or
an arbitrary-session attach feature.

The card exposes a Cloud link only for a validated session ID. Raw ACP `_meta`, signed
upload URLs, creator IDs and account secrets are not copied into run records. There is
no implicit Local-to-Cloud handoff or Cloud-to-Local pickup.

## Validation scope

Automated tests use a fake ACP server for launching, follow-ups, configuration changes,
confirmed cancellation and relay failure, including Cloud model presets advertised with
ACP's `model` category. Cloud presets use `devin_version`; a resumed Cloud session must
not acquire the Local `model` setting.

Real authenticated Desktop tests verified the target-specific menus, consent gating and
profile isolation. A separately approved test launched one Lite Cloud task with no
repository, Agent persona, Ubuntu and a 120-second plugin timeout. Desktop Stop received
the original prompt's cancellation confirmation, and the local relay exited.

Cloud follow-ups and reconnect have not been live-tested. Other Cloud presets and
platforms were discovered, not executed. Billing was not tested; the plugin timeout
is not a billing cap.
