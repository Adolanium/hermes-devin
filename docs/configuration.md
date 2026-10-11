# Configuration reference

All settings live under `plugins.entries.devin.settings` in Hermes' `config.yaml`. Older setups that use a `config` key in the same place are still read. Hermes Desktop renders the same fields from `config_schema` in `plugin.yaml`.

Values are validated on every read. A bad value refuses runs with a message naming the setting. Booleans also accept `"true"`/`"false"`, `"yes"`/`"no"`, `"on"`/`"off"` and `"1"`/`"0"`.

## Full example

```yaml
plugins:
  enabled: [devin]
  entries:
    devin:
      allow_gateway_injection: true
      settings:
        backend: acp
        permission_mode: normal
        allow_dangerous: false
        permission_requests: ask
        approval_scope: run
        cli_path: ""
        model: ""
        default_cwd: ""
        allowed_roots: []
        trust_workspace: false
        sandbox: false
        default_background: false
        timeout_seconds: 1800
        background_timeout_seconds: 3600
        max_timeout_seconds: 7200
        max_concurrent_runs: 2
        max_output_chars: 200000
        max_result_chars: 12000
        max_progress_events: 500
        max_prompt_chars: 100000
        history_limit: 50
        notify_on_complete: true
        preflight_auth_check: true
        cancel_grace_seconds: 5
        persist_runs: true
        extra_env: {}
```

`allow_gateway_injection` is a Hermes setting for the plugin entry, not a plugin setting. It lets background completion notices reach TUI, Desktop and gateway conversations. The classic CLI does not need it.

## Behaviour and safety

| Setting | Default | Values | What it does |
| --- | --- | --- | --- |
| `backend` | `acp` | `acp`, `print` | How Devin is driven. See [How it works](how-it-works.md#why-acp-instead-of-devin--p). |
| `permission_mode` | `normal` | `plan`, `normal`, `accept-edits`, `smart`, `dangerous`, `autonomous` | How much Devin may do without asking. Aliases: `auto` → `normal`; `yolo` and `bypass` → `dangerous`. |
| `allow_dangerous` | `false` | bool | Required for `permission_mode: dangerous`. Without it the setting is rejected. |
| `permission_requests` | `ask` | `ask`, `deny` | What happens to Devin's in-run permission prompts that the mode does not settle. `ask` relays them to you in foreground runs. Background runs deny them, unless the run card can answer (see `card_approvals`). |
| `card_approvals` | `true` | | Background runs started from Hermes Desktop show Devin's requests on their run card, with Allow and Deny buttons, instead of denying them. |
| `card_approval_timeout_seconds` | `300` | 30 to 3600 | How long a request waits on the run card before it is denied. |
| `approval_scope` | `run` | `run`, `workspace` | What a remembered "always/session" approval covers: only the identical request, or any prompt in the same folder and mode. |
| `allowed_roots` | `[]` | list of paths, or one path | When set, runs may only start inside these directories. |
| `default_cwd` | `""` | path | Folder used when the call gives no `cwd`. Empty: Hermes' terminal working directory, then the process directory. A relative `cwd` is resolved against this. |
| `sandbox` | `false` | bool | Devin's `--sandbox`. Required by `autonomous`. Not supported on native Windows and rejected there. |
| `trust_workspace` | `false` | bool | Print backend only: passes `--respect-workspace-trust false`. |

`plan` needs the ACP backend. `autonomous` needs `sandbox: true` and is rejected on native Windows; Devin's docs point to WSL 2 for it.

## Devin CLI

| Setting | Default | What it does |
| --- | --- | --- |
| `cli_path` | `""` | Path to the `devin` executable. Empty: look it up on PATH. |
| `model` | `""` | Devin model id. Empty: Devin's default. |
| `preflight_auth_check` | `true` | Run `devin auth status` before a run. Success is cached for five minutes. |
| `extra_env` | `{}` | Extra environment variables for Devin. Cannot set `DEVIN_PERMISSION_MODE`; the plugin always sets that from `permission_mode`. |

## Time and size limits

| Setting | Default | Range | What it does |
| --- | --- | --- | --- |
| `timeout_seconds` | `1800` | 10 to 86400 | Default timeout for foreground runs. |
| `background_timeout_seconds` | `3600` | 10 to 86400 | Default timeout for background runs. |
| `max_timeout_seconds` | `7200` | 10 to 86400 | Cap for any `timeout_seconds` the model asks for. Both defaults above must be at or below it. |
| `cancel_grace_seconds` | `5` | 0 to 120 | How long Devin gets to stop on its own before its tree is killed. |
| `max_concurrent_runs` | `2` | 1 to 16 | Active runs allowed at once. Extra runs are refused, not queued. |
| `max_prompt_chars` | `100000` | 100 to 1000000 | Longest prompt accepted. |
| `max_output_chars` | `200000` | 1000 to 5000000 | Output stored per run. Beyond it, the first 20% and the latest output are kept, with a marker in between. |
| `max_result_chars` | `12000` | 500 to 200000, and ≤ `max_output_chars` | Output returned to the model by default. `devin_run_result` can ask for more, up to `max_output_chars`. |
| `max_progress_events` | `500` | 10 to 10000 | Progress events kept per run. Older events are dropped and counted. |

## History and notices

| Setting | Default | What it does |
| --- | --- | --- |
| `default_background` | `false` | Run in the background when the call does not say. |
| `notify_on_complete` | `true` | Post background results into the conversation. |
| `history_limit` | `50` | Finished runs kept in memory and on disk (1 to 1000). |
| `persist_runs` | `true` | Save run snapshots under `<hermes home>/plugin-data/devin/runs/`. Read at startup only. |
