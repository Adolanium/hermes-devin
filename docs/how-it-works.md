# How it works

This page follows one run from the tool call to the result, then explains why the plugin drives Devin over ACP.

## The life of a run

1. **The model calls `delegate_to_devin`.** Arguments are limited to `prompt`, `cwd`, `background`, `timeout_seconds` and `title`. Anything else is rejected, including any attempt to set the permission mode, model or sandbox.
2. **The `pre_tool_call` hook checks the call.** It validates the settings and arguments, resolves the working directory, finds the Devin CLI and checks the concurrency limit. If the run could never start, the call is blocked with a clear error and you are not asked anything.
3. **Hermes asks you.** Otherwise the hook answers with an `approve` directive. Hermes shows its normal approval prompt (CLI prompt, gateway buttons or Desktop dialog), naming the folder, the permission mode, foreground or background, the timeout, the backend and the task. See [Approval and permissions](approval-and-permissions.md).
4. **Preflight.** The handler runs `devin auth status`. A clear "not logged in" answer stops the run with instructions to run `devin auth login`. A successful check is cached for five minutes. Output the plugin does not recognise never blocks a run.
5. **Devin starts.** `devin acp` is started in the chosen folder, in its own process group, and on Windows inside a Job Object. See [Process trees](process-tree.md).
6. **The session runs.** The plugin speaks JSON-RPC over stdio:
   - `initialize`, announcing no file-system or terminal capabilities, so Devin uses its own tools
   - `session/new` with the working directory, or `session/load` for a follow-up, which continues an earlier run's Devin session. The conversation Devin replays on load is not added to the new run
   - `session/set_mode` when Devin advertises a mode matching the configured permission mode. A mode is matched by exact id or name. If Devin advertises modes but none matches, the run is refused.
   - `session/prompt` with the task
7. **Progress streams in.** `session/update` notifications become run events: message and thought chunks, tool calls with their kind and file locations, plan updates, mode changes and usage. Devin's `session/request_permission` calls are answered by the plugin. See [Approval and permissions](approval-and-permissions.md#devins-own-permission-prompts).
8. **The run ends.** The prompt returns with a stop reason: `end_turn` is success, `cancelled` is a stop, `refusal` and `max_tokens` are reported as such. Errors are mapped to clear codes, listed below.
9. **Results come back.**
   - A foreground call returns the output (capped, head and tail kept), the files changed, permission decisions, usage and any error.
   - A background run posts a short notice into the conversation through `ctx.inject_message`, with Devin's output quoted as data. `devin_run_result` returns the details.

## Foreground, background and yielding

- **Foreground** (the default): the tool call waits. Devin's permission prompts are relayed to you. A Hermes interrupt cancels the run and waits until the process tree is gone.
- **Background** (`background: true`, or `default_background` in settings): the call returns a run id at once. Nobody is waiting, so Devin's permission prompts are denied. Interrupting the current turn does not stop a background run. Cancel it with `devin_cancel_run` or `/devin cancel`.
- **Yield.** If you send a message while a foreground run is working, Hermes asks long tools to yield. The run then becomes a background run and reports back the same way.

Completion notices need a way back into the conversation. The classic CLI always has one. The TUI, Desktop and the messaging gateway also need `plugins.entries.devin.allow_gateway_injection: true`. Without it, results are still in `devin_run_result` and `/devin status`.

## Error codes

| Code | Meaning |
| --- | --- |
| `cli_not_found` | No `devin` on PATH and no valid `cli_path`. The message includes the install commands. |
| `not_logged_in` | `devin auth status` or Devin itself reported no login. |
| `timeout` | The run hit its timeout and was stopped. |
| `exit_nonzero` | Devin exited with an error code. The last part of its stderr is included. |
| `workspace_untrusted` | Devin refused an untrusted folder (print backend). See `trust_workspace`. |
| `refused` | Devin refused the task, or Devin offered no ACP session mode matching the configured permission mode. |
| `permission_denied` | Print backend: Devin rejected actions its permission mode does not allow, because print mode cannot ask, and gave no answer. |
| `protocol_error` | Devin sent something the plugin could not use. |
| `cancelled`, `interrupted` | Stopped by a cancel, or by a Hermes interrupt. |
| `spawn_failed`, `internal_error` | The process could not be started, or the plugin hit an unexpected error. |
| `concurrency_limit`, `config_invalid`, `invalid_arguments`, `cwd_invalid`, `cwd_not_allowed`, `not_approved` | The run was refused before it started. |

## Why ACP instead of `devin -p`

`devin -p` (print mode) takes a prompt, works, prints text and exits. For a delegated run that leaves three gaps:

- **No structure.** Progress is lines of text. There is no reliable way to tell a tool call from a message, see which files were edited, or follow the plan.
- **No answer channel.** When Devin wants permission for something, print mode either blocks or applies the mode's default. Hermes cannot answer.
- **No polite stop.** The only way to stop it is to kill the process.

`devin acp` is the Agent Client Protocol server that Devin exposes for editors. Over ACP the plugin gets:

- typed events: messages, tool calls with kind and file locations, the plan, usage
- a protocol-level `session/cancel`, so Devin can end its turn cleanly before anything is killed
- `session/request_permission` callbacks, which the plugin answers from the configured mode or relays to you

That callback is also why the permission mode is enforced on the Hermes side, not only inside Devin.

The print backend (`backend: print`) remains as a fallback. Use it for environments where ACP is unavailable.

- It runs `devin --print --prompt-file <temp file> --permission-mode <mode>`.
- The prompt goes through a temp file, never the command line, and the file is deleted afterwards.
- Each stdout line becomes a message event.
- Plan mode is not available in print mode.

## Settings are read live

Settings are read and validated on every call, so a change in `config.yaml` applies to the next run without a restart. Invalid settings refuse runs; they never fall back to something more permissive. `persist_runs` is the exception: it is read once at startup.
