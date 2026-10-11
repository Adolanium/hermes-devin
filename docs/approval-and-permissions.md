# Approval and permissions

Two separate questions decide what Devin can do:

1. **May this run start at all?** That is Hermes' approval prompt, asked for every run.
2. **What may Devin do on its own once it runs?** That is the permission mode, set by you in the plugin settings.

## The approval gate

Every `delegate_to_devin` call passes through the plugin's `pre_tool_call` hook.

- **Blocked before asking.** Calls that cannot run are blocked with a JSON error, and you are not asked anything:
  - invalid settings or arguments
  - an attempt to set the permission mode, model or sandbox
  - a missing or disallowed folder
  - no Devin CLI
  - the concurrency limit already reached
- **Otherwise Hermes asks.** The hook returns `{"action": "approve", ...}`, and Hermes runs its own human approval gate. That is the same gate used for dangerous shell commands: a CLI prompt, gateway buttons or a Desktop dialog.
- **No answer means no.** If nobody can answer (an unattended context), or the prompt times out, the call is refused.

The prompt states:

- the working directory
- the permission mode, with a one-line description of what it allows
- foreground or background
- the timeout and the backend
- the task. Long prompts are shown up to 1,500 characters, followed by the total length, so nothing hides past the cut unnoticed.

### Remembered approvals

Hermes lets you answer "once", "for this session" or "always". The remembered rule key is built so that approving a safe run never approves a riskier one:

- The key always includes the permission mode, the backend and the folder.
- With `approval_scope: run` (the default), it also includes the prompt. Only the identical request is pre-approved.
- With `approval_scope: workspace`, any prompt in that folder, with that mode and backend, is pre-approved.

### Tickets

When the hook asks for approval, it also leaves a single-use ticket. The ticket is bound to the session and the exact arguments, and it expires after 15 minutes. The tool handler consumes it.

- **A denial cancels it.** A denial reported through `post_approval_response` removes the ticket.
- **No ticket, ask again.** If the handler runs without a ticket (some code path that skipped hooks), it asks the same approval gate itself. If that gate is unavailable or fails, the run is refused.

Hermes does not pass a tool-call id to tool handlers, so the ticket is matched on session plus arguments. Two identical calls in the same session share one ticket slot.

## Permission modes

| Mode | Devin acts alone on | Devin's prompts | Notes |
| --- | --- | --- | --- |
| `plan` | reading, searching, thinking, fetching | everything else denied | Devin's read-only plan mode. ACP only; refused if Devin offers no read-only mode. |
| `normal` | nothing | relayed to you, or denied | The default. Refused over ACP when Devin offers no matching mode, as Devin CLI 3000.x does; use `backend: print`. |
| `accept-edits` | file edits, deletes and moves | the rest relayed, or denied | |
| `smart` | file edits; Devin's safety model judges the rest | the rest relayed, or denied | |
| `dangerous` | everything, including destructive commands | none | Needs `allow_dangerous: true`. The approval prompt says so in capitals. |
| `autonomous` | everything inside Devin's OS sandbox | none | Needs `sandbox: true`. Not on native Windows. |

### Where the mode comes from

Only from `permission_mode` in the plugin settings.

- **Not from the model.** The tool schema has no mode parameter. Arguments such as `permission_mode`, `mode`, `allow_dangerous`, `dangerous`, `yolo`, `bypass`, `model`, `sandbox`, `trust_workspace` and `cli_path` are rejected outright.
- **Not from the environment.** An inherited `DEVIN_PERMISSION_MODE` is overwritten, and `extra_env` may not set it.

### How the mode is enforced

The mode is applied in four places at once. If one is ignored, the others still hold:

1. `DEVIN_PERMISSION_MODE` in Devin's environment.
2. `--permission-mode` on the command line (print backend).
3. The matching ACP session mode, chosen with `session/set_mode`. If Devin advertises modes but none matches, the run is refused, because staying in Devin's current mode could allow more.
4. The plugin's own answer to each `session/request_permission`.

## Devin's own permission prompts

Over ACP, Devin asks the client before actions its mode does not cover. The plugin answers in this order:

1. **The mode decides when it can.** Examples: `plan` denies an edit, `accept-edits` allows one, `dangerous` allows everything.
2. **`permission_requests: deny`** denies whatever is left.
3. **Foreground runs** relay it to you as a Hermes approval prompt. The text is "Devin run dv-… asks to: Run `pytest` (kind: execute) in E:\Dev\myapp". The remembered rule key includes the mode, the kind of action, the folder and the action itself, so "always" covers only that exact request. In Hermes Desktop the run card shows the request too, and points to the prompt.
4. **Background runs started from Hermes Desktop** (`card_approvals`, on by default) show the request on the run card with **Allow** and **Deny** buttons, and Desktop sends a notification. A request nobody answers within `card_approval_timeout_seconds` (5 minutes by default) is denied. A foreground run moved to the background counts too.
5. **Other background runs** deny whatever is left, because nobody is waiting.

Desktop identity is read from Hermes' session context, including `HERMES_SESSION_SOURCE`.
This applies to initial delegated runs as well as card follow-ups; an unknown or non-Desktop
session does not gain permission-card routing.

Every decision is recorded on the run, with its reason, and shown by `devin_run_result` and `/devin status`.

## Output coming back

Devin's output is another agent's text. Background completion notices quote it between `<<<DEVIN_OUTPUT` and `DEVIN_OUTPUT>>>`, label it as data rather than instructions, and neutralise any copy of those markers inside the output. Starting another Devin run from that text still needs your approval.
