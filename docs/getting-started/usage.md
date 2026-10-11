# Usage

[Documentation](../README.md)

Complete [installation](installation.md) first.

## Ask Hermes

Give Hermes a concrete task and the full path to your project:

> Have Devin add type hints to src/utils.py in /path/to/project and run the tests.

> In the background, ask Devin to migrate the test suite in /path/to/project from unittest to pytest. Tell me when it finishes.

> What is Devin doing right now?

> Stop the Devin run.

Use your actual folder path, including a Windows path when working on native Windows.
Hermes checks the arguments and asks for launch approval through its normal approval system.
Only the main agent can start Devin. Subagents do not receive the delegation toolset.

## Use Hermes Desktop

Open the **Devin** sidebar page and choose **New Devin task**. Select Local or Cloud,
then use the target's advertised controls:

- Local uses your selected folder, with searchable models and Fusion pairings.
  Thinking and other controls appear when the session supports them.
- Cloud uses the account's available organization, repositories, platform, persona
  and model controls. Confirm Cloud-managed permissions before launch.

The model selection applies to Devin, not Hermes' chat model. See
[Local model controls](../guides/model-controls.md) and [Cloud sessions](../guides/cloud-sessions.md).

### Follow a run

The conversation's live run card shows Devin's plan, file edits, command output,
permission decisions and final answer. Background Local runs started in Desktop
can show **Allow** and **Deny** requests on the card. Outside Desktop, background
permission requests are denied because there is no interactive answer route.

The sidebar lists active runs and history. The status-bar chip shows when Devin
is working or needs an answer. The Desktop pet can report the current action,
and notifications can report completion or a pending request.

### Review the edits

**Review changes** opens recorded edits grouped by file. Separate edits to the
same file remain visible. Repeated progress updates replace their own snapshot.

The view keeps up to 30 recent edit snapshots, with at most 160 lines per patch.
It marks omitted or truncated history. Line totals count edit operations, not a
net Git diff. Local files have a **Show in folder** action; Cloud files do not.
See [run data and events](../development/run-data-and-events.md).

### Stop or continue

**Stop** cancels a Local task and cleans up its tracked processes. Cloud Stop
requests remote cancellation and reports whether Devin confirmed it. Closing the
relay alone does not prove Cloud work stopped.

After a run finishes, **Tell Devin…** sends a follow-up into the same ACP session.
Follow-ups need an available saved session. Cloud runs with unknown remote
activity block follow-ups. Reconnect checks are described in the
[Cloud guide](../guides/cloud-sessions.md#stop-and-reconnect).

The Devin icon is bundled and works offline. Devin Desktop is not required.

## Slash commands

```text
/devin list
/devin status <run_id>
/devin cancel <run_id|all>
/devin doctor
```

`list` shows recent runs. `status` shows state, plan and recent events.
`cancel` stops Local work or requests Cloud cancellation. `doctor` checks
settings, the CLI and sign-in.

## Tools

Hermes chooses these tools from your request:

| Tool | Purpose |
| --- | --- |
| `delegate_to_devin` | Start a run with `prompt`, optional `cwd`, `background`, `timeout_seconds`, `title` and an opaque Desktop `launch_id` |
| `devin_list_runs` | List active and finished runs |
| `devin_run_status` | Read state, plan, recent events and output tail |
| `devin_run_result` | Read output, files changed, permission decisions, usage and errors |
| `devin_cancel_run` | Cancel a run |

The Desktop creates `launch_id` after discovery. It binds the approved target,
workspace and model controls. It is not a model identifier or a setting to invent
in a prompt. Tools cannot widen the configured permission mode or sandbox.
