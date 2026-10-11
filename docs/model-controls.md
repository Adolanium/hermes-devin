# Local model controls

In Hermes Desktop, open **Devin → New Devin task**, set the local folder, then choose
**Choose a model or Fusion pairing**. This needs the agent plugin enabled and `backend: acp`.
The standalone Devin CLI must be installed and signed in. Devin Desktop is not required.

The picker gets its choices from a temporary ACP session in that folder. Discovery sends
no task prompt, denies permission requests and closes its process tree afterwards. The
model catalog adds family labels and pricing where available; it does not add choices
that the ACP session did not advertise.

## Choose and review

- Search by model family, lead or sidekick. Fusion choices appear with their advertised
  pairing names, grouped alongside the other model families.
- Thinking, speed or other model configuration menus appear only when the session
  supplies them. Changing the model reloads these menus because their values can change.
- The server confirms every selection. A thinking change can also change the model ID;
  the confirmed ID, rather than the previous one, becomes the launch selection.
- Review the local folder, permission mode and catalog pricing. Missing pricing means
  unknown, not free. Catalog visibility does not guarantee plan access, and prices are
  not a quote for the task.

**Ask Hermes** refreshes the selection and opens a chat with a single-use launch reference.
Hermes forwards that reference in `delegate_to_devin`; it cannot replace it with a raw
model, permission or sandbox argument. Your normal Hermes approval still comes before
the task. The approval shows the selected model, conditional controls and pricing note.

After approval, the new ACP run applies and verifies the saved values before sending its
prompt, then reapplies the configured permission mode. A rejected selection starts no
task. If an active session changes an approved model setting, the plugin cancels the run
rather than authorize a fallback. The run card shows requested versus confirmed settings;
finished-run follow-ups restore the confirmed selection instead of new defaults.

## Defaults and recovery

Approved choices become the Local defaults for this Hermes profile when `persist_runs`
is enabled. They are stored in `plugin-data/devin/session-preferences.json`, separately
from run snapshots and Cloud defaults. Merely browsing or denying a task does
not save a default. This preference does not change the CLI's global defaults or apply to
ordinary chat delegation without a launch reference.

Selections expire after 15 minutes and are bound to the folder and plugin settings. If
either changes, refresh before starting. Unavailable saved values are refused, not
silently replaced. **Reload available models** explicitly reloads the current CLI
choices so you can select again. **Use plugin defaults** explicitly leaves the picker
and uses the existing `model` setting or CLI default.

An older CLI without ACP model controls can still use the plugin defaults. The print
backend uses the `model` plugin setting and cannot use this picker. Permission modes
remain independent: a model selection cannot make an unsupported ACP permission mode
work or widen it to another mode. See [Configuration](configuration.md) and
[Approval and permissions](approval-and-permissions.md).

This page describes Local execution only. Cloud execution is not enabled by a Local
model or Fusion selection. Use the separate target selector described in [Cloud sessions](cloud-sessions.md).
