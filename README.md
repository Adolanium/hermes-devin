# hermes-devin

A Hermes Agent plugin that lets Hermes hand coding tasks to the local
[Devin CLI](https://cli.devin.ai).

- **Your approval comes first.** Every run goes through Hermes' approval prompt
  (CLI, gateway buttons or Desktop dialog). If nobody is there to answer, the run
  does not start.
- **The permission mode is yours to set.** Devin's permission mode comes only
  from the plugin settings. The model cannot pass or change it, and `dangerous`
  needs an explicit `allow_dangerous: true`.
- **You see live progress.** Devin is driven over ACP (Agent Client Protocol),
  so Hermes gets structured events: messages, tool calls, the plan, usage and
  permission prompts.
- **Background runs.** Long tasks return a run id right away. The result is
  posted back to the conversation when Devin finishes, and a concurrency limit
  applies.
- **Clean stops.** An interrupt, a timeout, a cancel, unloading the plugin or
  quitting Hermes kills Devin's whole process tree. On Windows this uses a Job
  Object, falling back to `taskkill /T /F`. On POSIX it signals the process
  group. You are told how the stop went.

## Requirements

- Hermes Agent with the plugin API v1 (`api_version: 1`).
- The Devin CLI, installed and logged in:
  - Windows (PowerShell): `irm https://static.devin.ai/cli/setup.ps1 | iex`
  - macOS / Linux: `curl -fsSL https://cli.devin.ai/install.sh | bash`
  - Then run `devin auth login` once in a terminal. The plugin never logs in or
    out for you.

There are no extra Python dependencies.

## Install

Put the plugin directory under your Hermes home's `plugins/` folder. The
default Hermes home is `%LOCALAPPDATA%\hermes` on Windows and `~/.hermes`
elsewhere, or whatever `HERMES_HOME` points to.

Windows, from a checkout like `E:\Dev\Hermes\plugins\hermes-devin`, using a
junction so edits take effect without copying:

```powershell
$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }
New-Item -ItemType Junction -Path "$hermesHome\plugins\devin" -Target "E:\Dev\Hermes\plugins\hermes-devin"
```

macOS / Linux:

```bash
ln -s /path/to/hermes-devin ~/.hermes/plugins/devin
```

Then enable it. Plugins are opt-in, and the plugin key is `devin`:

```bash
hermes plugins enable devin
```

Or edit `config.yaml` by hand:

```yaml
plugins:
  enabled: [devin]
```

The tools join Hermes' existing `delegation` toolset, which is on by default.
If you trimmed your toolsets, add `delegation` back. Subagents never get this
toolset, so only the main agent can start Devin.

Restart Hermes, then check the setup with:

```
/devin doctor
```

## Configuration

All settings live under `plugins.entries.devin.settings` in `config.yaml`.
Hermes Desktop shows the same fields as a form, from `config_schema` in
`plugin.yaml`.

Settings are validated whenever they are read, so you never need a restart for
them. Invalid settings block every run with a message naming the problem; they
never fall back to something more permissive. The only exception is
`persist_runs`, which is read once at startup.

```yaml
plugins:
  enabled: [devin]
  entries:
    devin:
      # Needed only for background completion notices in the TUI, Desktop
      # or messaging gateway. The classic CLI does not need it.
      allow_gateway_injection: true
      settings:
        backend: acp                 # acp (default) | print
        permission_mode: normal      # plan | normal | accept-edits | smart | dangerous | autonomous
        allow_dangerous: false       # required for permission_mode: dangerous
        permission_requests: ask     # ask: relay Devin's prompts to you (foreground only) | deny
        approval_scope: run          # run: "always allow" covers the identical request only
                                     # workspace: covers any prompt in that directory and mode
        cli_path: ""                 # empty: find `devin` on PATH
        model: ""                    # empty: Devin's default model
        default_cwd: ""              # empty: Hermes' working directory
        allowed_roots: []            # if set, Devin may only run inside these directories
        trust_workspace: false       # print backend: pass --respect-workspace-trust false
        sandbox: false               # Devin's --sandbox (not available on native Windows)
        default_background: false
        timeout_seconds: 1800        # foreground default
        background_timeout_seconds: 3600
        max_timeout_seconds: 7200    # cap for any timeout_seconds the model asks for
        max_concurrent_runs: 2       # 1-16; extra runs are refused, not queued
        max_output_chars: 200000     # stored output per run (head and tail kept)
        max_result_chars: 12000      # output returned to the model by default
        max_progress_events: 500
        max_prompt_chars: 100000
        history_limit: 50            # finished runs kept in memory and on disk
        notify_on_complete: true     # post background results to the conversation
        preflight_auth_check: true   # run `devin auth status` before a run (cached for 5 min)
        cancel_grace_seconds: 5
        persist_runs: true           # keep run history under <hermes home>/plugin-data/devin/
        extra_env: {}                # extra environment variables for Devin
```

### Permission modes

| Setting        | What Devin may do without asking                       | Prompts from Devin             |
|----------------|--------------------------------------------------------|--------------------------------|
| `plan`         | read and search only (Devin's plan mode, ACP only)     | writes and commands denied     |
| `normal`       | nothing                                                | relayed to you, or denied      |
| `accept-edits` | file edits                                             | commands relayed, or denied    |
| `smart`        | file edits; Devin's own safety model handles the rest  | the rest relayed, or denied    |
| `dangerous`    | everything, including destructive commands             | none                           |
| `autonomous`   | sandboxed; needs `sandbox: true`, not on native Windows| -                              |

The mode is applied in several places at once:

- `DEVIN_PERMISSION_MODE` in Devin's environment, overwriting any inherited value
- `--permission-mode` on the command line (print backend)
- the matching ACP session mode, if Devin advertises one
- the plugin's own answer to each ACP permission request

So even if one of those is ignored, Devin cannot do more than the configured
mode allows. Tool arguments such as `permission_mode`, `mode`, `model`,
`sandbox` or `allow_dangerous` are rejected outright.

The Hermes approval prompt always names the mode. The remembered-approval key
includes the mode and backend, so approving a `normal` run never pre-approves a
`dangerous` one.

### Optional: the Devin MCP server

Separately from this plugin, Hermes can talk to Devin's hosted MCP server
(Devin sessions, wiki and repository Q&A) through its own MCP client. This
needs a Devin API key starting with `cog_`; legacy `apk_` keys do not work.
Keep the key in your environment or `.env`, not in `config.yaml`:

```yaml
mcp_servers:
  devin:
    url: https://mcp.devin.ai/mcp
    headers:
      Authorization: "Bearer ${DEVIN_API_KEY}"
      # X-Org-Id: "${DEVIN_ORG_ID}"   # only for enterprise keys and personal access tokens
```

This plugin does not need it, and does not use it.

## Tools

| Tool                | Purpose                                                                        |
|---------------------|--------------------------------------------------------------------------------|
| `delegate_to_devin` | Start a run: `prompt` (required), `cwd`, `background`, `timeout_seconds`, `title` |
| `devin_list_runs`   | Recent runs, filtered by `state`: `active`, `finished` or `all`                |
| `devin_run_status`  | State, plan, recent events and output tail of one run                         |
| `devin_run_result`  | Full (capped) output, files changed, permission decisions, usage, errors     |
| `devin_cancel_run`  | Stop a run and every process it started                                      |

The `/devin` slash command does the same things without going through the model:

```
/devin list
/devin status <run_id>
/devin cancel <run_id|all>
/devin doctor
```

## Examples

Ask Hermes:

> Have Devin add type hints to `src/utils.py` in E:\Dev\myapp and run the
> tests. Wait for it.

Hermes calls `delegate_to_devin` and you get an approval prompt like this:

```
Start a Devin run in E:\Dev\myapp; permission mode 'normal' (Devin asks before
edits and commands); foreground, timeout 1800s, backend acp. Task: ...
```

While Devin works, its own permission prompts ("Run `pytest`") show up as
further approval prompts. The tool result lists Devin's answer, the files it
touched and any errors.

For something long:

> In the background, ask Devin to migrate the test suite in E:\Dev\myapp from
> unittest to pytest. Tell me when it's done.

The call returns a run id right away. When Devin finishes, a message starting
with `[Devin background run dv-... succeeded ...]` lands in the conversation,
with Devin's output quoted as data. In background runs nobody is around to
answer Devin's prompts, so they are denied. Pick `accept-edits` (or wider)
if background runs need to edit files on their own.

If you send a message while a foreground run is working, Hermes asks the tool
to yield. The run then carries on in the background and reports back the same
way.

## How it works

1. **Approval.** The plugin's `pre_tool_call` hook answers every
   `delegate_to_devin` call with an `approve` directive. Hermes then shows its
   normal approval prompt, which fails closed when nobody can answer. The hook
   first blocks calls that could not run anyway: invalid settings, a missing
   CLI, a bad `cwd`, or the concurrency limit. That way you are never asked to
   approve something that will fail.

   The hook leaves a single-use ticket bound to the session and the exact
   arguments, and the tool handler consumes it. If the handler is ever reached
   without a ticket (some other code path that skipped hooks), it asks the
   same approval gate itself before starting anything.
2. **Preflight.** It resolves the `devin` binary and runs `devin auth status`.
   This produces clear errors for "CLI not found" (with install commands) and
   "not logged in" (run `devin auth login`).
3. **Run.** `devin acp` is started in a new process group (Windows: plus a Job
   Object), then the plugin speaks JSON-RPC over stdio: `initialize`,
   `session/new`, `session/set_mode`, `session/prompt`.

   `session/update` notifications become run events. `session/request_permission`
   is answered from the permission mode, or relayed to you (foreground only).
4. **Stop.** On cancel, timeout or interrupt, the plugin sends
   `session/cancel`, waits `cancel_grace_seconds`, then kills the tree: Job
   Object termination, else `taskkill /T /F`, or the POSIX process group. The
   result includes a `kill_report` (method, graceful or not, steps). After a
   normal exit, leftover child processes are reaped the same way.
5. **Results.** Output is capped, keeping the head and tail. Background results
   are posted with `ctx.inject_message`, and every run is saved as JSON for
   history.

### Why ACP instead of `devin -p`

`devin -p` prints text and exits. That leaves no structured progress, no way to
answer Devin's permission prompts, and no clean cancel short of killing the
process.

ACP is the protocol Devin uses for editor integrations. It gives typed events
(messages, tool calls with kinds and file locations, plan, usage), a
protocol-level `session/cancel`, and a permission callback the plugin answers
itself. That callback is what lets the configured mode be enforced on the
Hermes side too. The `print` backend stays available as a fallback
(`backend: print`).

## Run data and events (for a Desktop panel)

Each run is a plain JSON snapshot (`schema_version: 1`) with these fields:

- id, title, state, cwd, permission mode, backend
- timestamps
- approval record
- plan, tool calls, permission decisions
- usage, error, kill report
- capped event list and capped output

The run states are `created`, `starting`, `running`, `cancelling`,
`succeeded`, `failed`, `cancelled` and `timed_out`.

- Snapshots are written to `<hermes home>/plugin-data/devin/runs/<run id>.json`.
- Live events go out as `ctx.emit` (`devin:run.created`, `devin:run.state`,
  `devin:run.progress`, `devin:run.permission`, `devin:run.usage`,
  `devin:run.finished`). They also go to the Desktop event bus as
  `plugin.devin.run.*` (listen with `host.onEvent`).
- Each event carries `{schema_version, type, run_id, at, run: <summary>, seq, event}`.
- `dashboard/plugin_api.py` is a ready read-only backend (`GET /runs`,
  `GET /runs/{run_id}`). Hermes mounts it once a `dashboard/manifest.json` with
  `"api": "plugin_api.py"` is added together with a panel UI. Until then it is
  not loaded.

## Security notes

- The prompt Devin receives is written by the model. Read the approval
  prompt before saying yes, especially with wide permission modes.
- Devin's output is another agent's text. Completion notices quote it inside
  `<<<DEVIN_OUTPUT ... DEVIN_OUTPUT>>>` and label it as data, not instructions.
  Starting another run still needs your approval.
- `allowed_roots` limits where Devin may run. Without it, any existing
  directory the model names can be used, and the approval prompt shows which
  one.
- When the plugin is unloaded or Hermes exits, active runs are stopped. On
  Windows, the Job Object also kills Devin's tree if Hermes crashes.

## Development

Tests use a fake Devin CLI (`tests/fake_devin.py`). The real one is never run.

```bash
cd tests
python -m pytest
```

On Windows, use the Hermes venv's Python, for example
`E:\Dev\Hermes\hermes-agent\.venv\Scripts\python.exe -m pytest`. Pytest's
scratch files go to `.pytest_tmp/` inside the plugin folder.
`tests/test_hermes_contract.py` checks the Hermes APIs the plugin relies on.
It runs when Hermes is importable next to that Python (or `HERMES_AGENT_DIR` is
set), and is skipped otherwise.

## License

MIT
