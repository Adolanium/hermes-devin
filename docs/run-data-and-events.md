# Run data and events

The run model is designed so a Hermes Desktop panel can be added later without changing how runs work. Everything a panel needs is already written to disk and published live.

## The run record

Each run is one `Run` record (`models.py`). Its JSON snapshot carries `schema_version: 1` and these fields:

| Group | Fields |
| --- | --- |
| Identity | `id` (`dv-` plus hex), `title`, `background`, `cwd`, `permission_mode`, `backend`, `model`, `session_id` (Hermes), `devin_session_id`, `pid` |
| Timing | `created_at`, `started_at`, `finished_at`, `updated_at`, `duration_seconds`, `timeout_seconds` |
| Work | `prompt`, `plan`, `tool_calls` (count), `permission_requests` (count), `usage`, `last_activity` |
| Decisions | `approval` (how the run was approved: via, rule key, choice, times), `permissions` (each in-run request with its decision and reason) |
| Outcome | `state`, `stop_reason`, `exit_code`, `error` (`code` and `message`), `cancel_reason`, `kill_report` |
| Logs | `events` (capped, oldest dropped and counted in `events_dropped`), `output`, `output_chars`, `output_truncated` |

The summary used in lists and event envelopes is a subset: id, title, state, background, cwd, mode, backend, timestamps, duration, counts of tool calls and permission requests, last activity, usage and error.

## States

```text
created → starting → running → succeeded
                             ↘ failed
                             ↘ timed_out
         (any active state) → cancelling → cancelled | timed_out | failed
```

`created`, `starting`, `running` and `cancelling` are active. `succeeded`, `failed`, `cancelled` and `timed_out` are final. Transitions outside this graph raise an error, so a run can never jump back from a final state.

## Progress events

Each run keeps a list of progress events. Each has `seq`, `at`, `kind`, an optional `text` and a `data` object with fields for that kind:

| Kind | What it is |
| --- | --- |
| `status` | Lifecycle notes from the plugin (started, stopping, reaped children) |
| `message` | A chunk of Devin's reply |
| `thought` | A chunk of Devin's reasoning, when it shares one |
| `tool_call` | A tool call Devin starts: title, `tool_kind`, file locations |
| `tool_update` | A change to a running tool call, such as its status |
| `plan` | Devin's plan, with entries and how many are done |
| `mode` | The ACP session mode chosen or changed |
| `permission` | An in-run permission request and its decision |
| `usage` | Token use reported by Devin |
| `stderr` | Stray output from Devin that was not a protocol message |

## Stored snapshots

- **Location:** `<hermes home>/plugin-data/devin/runs/<run id>.json`, through Hermes' `plugin_data_dir`.
- **Atomic writes:** each file is written to a temporary name and renamed, so a reader never sees half a file. While a run is active, writes are throttled.
- **Final means final:** once a final snapshot is written, an older write still in flight cannot overwrite it.
- **Pruning:** finished runs beyond `history_limit` are pruned.
- **Orphans:** each snapshot records the Hermes process that owns it. At startup, a snapshot still marked active is checked. If its owner is gone, the run is marked failed with a note that Hermes stopped while it was running. A live owner, such as a second Hermes window, is left alone.
- `persist_runs: false` keeps history in memory only.

## Live events

Every change produces one event with a stable envelope:

```json
{
  "schema_version": 1,
  "type": "run.progress",
  "run_id": "dv-3f9a1c2b7d",
  "seq": 7,
  "at": 1760000000.0,
  "run": { "id": "dv-3f9a1c2b7d", "state": "running", "...": "summary" },
  "event": { "seq": 7, "at": 1760000000.0, "kind": "tool_call", "text": "Edit src/utils.py", "data": { "...": "..." } }
}
```

Types: `run.created`, `run.state`, `run.progress`, `run.permission`, `run.usage`, `run.finished`.

Each event goes to three places:

- **In-process listeners**, used by the tests and the slash command.
- **The Hermes plugin event bus** via `ctx.emit`, as `devin:<type>`, for other plugins.
- **The Desktop and TUI gateway stream** via `broadcast_plugin_event`, as `plugin.devin.<type>`. A Desktop plugin subscribes with `host.onEvent('plugin.devin.run.progress', ...)`.

Delivery is best effort and never fails a run. Message and thought chunks are coalesced per run (at most one every half second), so a fast stream does not flood clients. The full text is still kept on the run.

## The dashboard backend

`dashboard/plugin_api.py` is a read-only FastAPI router over the stored snapshots:

- `GET /runs?state=active|finished|all&limit=50`: run summaries, newest first
- `GET /runs/{run_id}?output=true`: one full snapshot. Output is left out unless asked for.

It reads files only, so it works from the dashboard process without reaching into the agent process. Run ids are validated before any file is touched.

Hermes mounts it at `/api/plugins/devin/` once a `dashboard/manifest.json` with `"api": "plugin_api.py"` exists. That manifest is meant to ship together with a panel UI. Until then the module is not loaded.
