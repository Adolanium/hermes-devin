<div align="center">

  <a href="https://github.com/NousResearch/hermes-agent">
    <img src="https://github.com/user-attachments/assets/ac2f5702-c842-4b2e-9340-737481fa0ece" width="96" height="96" alt="Nous Research Hermes mark" />
  </a>

  # Hermes Devin

  **Hand the coding task to Devin. Keep your hand on the switch.**

  Hermes Devin lets Hermes delegate a coding task to the Devin CLI on your machine.
  You approve every run, you choose how much Devin may do on its own, and you can
  stop it at any moment. Every process it started stops with it.

  <sub>POWERED BY <a href="https://github.com/NousResearch/hermes-agent">HERMES AGENT</a> &nbsp;·&nbsp; COMMUNITY PLUGIN &nbsp;·&nbsp; VERSION 0.1.0</sub>

  <br /><br />

  [See what it does](#a-second-pair-of-hands-in-the-chat) &nbsp;·&nbsp; [Install it](#make-it-yours) &nbsp;·&nbsp; [Read the safety model](#you-stay-in-charge)

</div>

## Powered by Hermes

Hermes Devin is a community-built agent plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It adds tools to Hermes' own `delegation` toolset, asks through Hermes' own approval prompt, and posts results back into the same conversation. It works the same in the CLI, the TUI, Hermes Desktop and the messaging gateway.

Devin runs locally through the [Devin CLI](https://cli.devin.ai) you already installed and logged in to. The plugin does not hold an API key and does not call Devin's cloud API.

## A second pair of hands in the chat

Ask Hermes to give a task to Devin and keep talking. Hermes writes the brief, you approve it, and Devin works in the folder you name.

| | |
| --- | --- |
| **Delegate**<br />Hermes hands Devin a complete brief: goal, files, constraints, how to verify. You see the folder, the permission mode, the timeout and the task before anything starts. | **Watch**<br />Progress streams live: Devin's messages, the files it edits, its plan, token use and every permission decision. `/devin status` shows it at a glance. |
| **Background it**<br />Long tasks return a run id right away. The result lands in the chat when Devin finishes. A concurrency limit keeps two or three runs from turning into twenty. | **Stop it**<br />Interrupt, cancel, a timeout, or quitting Hermes ends the run. Devin is asked to stop first, then its whole process tree is closed. You get a report of how it went. |

An illustrative exchange:

```text
You     › Have Devin add type hints to src/utils.py in E:\Dev\myapp and run the tests.
Hermes  › [approval] Start a Devin run in E:\Dev\myapp; permission mode 'accept-edits' ... Allow?
Devin   › edits src/utils.py · asks to run pytest → you approve · 41 passed
Hermes  › Devin finished: 1 file changed, tests pass. Here is the summary ...
```

## Watch it work in Hermes Desktop

In Hermes Desktop, Devin shows up in the conversation itself. When Hermes hands off a task, its reply carries a live card for the run:

- Devin's plan, ticking off step by step
- each file lighting up as Devin edits it (click one to show it in its folder)
- the code Devin writes, as live diffs, and the output of the commands it runs
- Devin's requests, with **Allow** and **Deny** buttons for background runs
- recent tool calls and every permission decision
- elapsed time, token use, the final answer or the error
- a **Stop** button that ends the run and everything it started

A **Devin** page in the sidebar lists active runs and history, a status-bar chip shows while Devin is working, and you get a notification when a run finishes, even if you've switched to another app.

The Desktop half ships in the same folder as the plugin and starts switched off. Turn it on under **Capabilities → Plugins → Devin**.

## You stay in charge

- **Every run needs your yes.** Each run passes through Hermes' approval prompt. If nobody is there to answer, the run does not start.
- **Permission mode is your setting, not the model's.** `plan`, `normal`, `accept-edits`, `smart`, `dangerous` or `autonomous` come only from the plugin settings. `dangerous` also needs an explicit `allow_dangerous: true`. Hermes cannot pass a mode, a model or a sandbox flag per call.
- **Devin's own questions reach you.** In a foreground run, Devin's "may I run this command?" prompts become Hermes approval prompts. A background run started from Hermes Desktop asks on its run card, with Allow and Deny buttons, and denies the request if nobody answers in time. Other background runs deny them, because nobody is waiting to answer.
- **Fenced folders.** `allowed_roots` keeps Devin inside the directories you list.
- **Nothing left behind.** On Windows the run lives in a Job Object, so even a crashed Hermes takes Devin's helpers with it. On macOS and Linux it gets its own process group.
- **Output is data.** Devin's reply is quoted and labelled as another agent's output when it comes back into the chat.

The full model is in [Approval and permissions](docs/approval-and-permissions.md).

## Make it yours

### Install

You need Hermes Agent and the Devin CLI, installed and logged in:

```powershell
# Windows (PowerShell)
irm https://static.devin.ai/cli/setup.ps1 | iex
devin auth login
```

```sh
# macOS / Linux
curl -fsSL https://cli.devin.ai/install.sh | bash
devin auth login
```

Then install and enable the plugin. Its key in Hermes is `devin`:

```sh
hermes plugins install Adolanium/hermes-devin
hermes plugins enable devin
```

Installing from GitHub uses your git credentials, so you need read access to the repository while it is private.

Working from a local checkout instead? Link it into your Hermes home's `plugins/` folder as `devin`, then enable it. Use one method or the other: `hermes plugins install` refuses to replace a linked plugin.

```powershell
$h = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }
New-Item -ItemType Junction -Path "$h\plugins\devin" -Target "C:\path\to\hermes-devin"
hermes plugins enable devin
```

Restart Hermes and check the setup from an interactive session:

```text
/devin doctor
```

The tools join the `delegation` toolset, which Hermes enables by default. Subagents never get it, so only the main agent can start Devin.

### Quick config

Settings live under `plugins.entries.devin.settings` in `config.yaml`. Hermes Desktop shows the same fields as a form. These are the ones most people touch:

```yaml
plugins:
  enabled: [devin]
  entries:
    devin:
      allow_gateway_injection: true    # background results in TUI, Desktop and gateway chats
      settings:
        permission_mode: accept-edits  # plan | normal | accept-edits | smart | dangerous | autonomous
        allowed_roots: ["E:\\Dev"]     # Devin may only work inside these folders
        max_concurrent_runs: 2
        timeout_seconds: 1800          # foreground runs
        background_timeout_seconds: 3600
```

Set `permission_mode` before your first run. The default, `normal`, never lets Devin act without asking. Current Devin builds offer no session mode like that over ACP, so the plugin refuses those runs rather than let Devin do more than you allowed. Choose `accept-edits` to let Devin edit files on its own while its other requests come to you, or keep `normal` with `backend: print`, where Devin rejects anything that needs confirmation. See [Known limitations](docs/limitations.md).

Invalid settings block runs with a message that names the problem. Every option is in the [configuration reference](docs/configuration.md).

### Use it

Ask in plain words. Hermes picks the tool.

> Have Devin fix the failing test in tests/test_api.py in E:\Dev\myapp. Wait for it.

> In the background, ask Devin to migrate the test suite in E:\Dev\myapp from unittest to pytest. Tell me when it's done.

> What is Devin doing right now?

> Stop the Devin run.

Or drive runs yourself:

```text
/devin list                 recent runs
/devin status <run_id>      state, plan and latest events
/devin cancel <run_id|all>  stop a run and everything it started
/devin doctor               settings, CLI and login check
```

| Tool | What Hermes uses it for |
| --- | --- |
| `delegate_to_devin` | Start a run: `prompt`, plus optional `cwd`, `background`, `timeout_seconds`, `title` |
| `devin_list_runs` | Recent runs, active or finished |
| `devin_run_status` | State, plan, recent events and output tail of one run |
| `devin_run_result` | Output, files changed, permission decisions, usage and errors |
| `devin_cancel_run` | Stop a run and every process it started |

## Built on what Devin already speaks

Hermes Devin talks to `devin acp`, the same Agent Client Protocol Devin uses for editor integrations. That gives typed progress events, a real cancel, and permission requests the plugin can answer itself. A plain `devin -p` backend is there as a fallback. [How it works](docs/how-it-works.md) explains the choice.

There is no fork, upstream patch, extra service or Python dependency. Run history is kept as small JSON files in Hermes' plugin data folder, and every change is published as a live event. See [Run data and events](docs/run-data-and-events.md).

Want Devin's hosted tools in Hermes too? The [Devin MCP server](docs/devin-mcp.md) works alongside this plugin.

## Documentation

- [How it works](docs/how-it-works.md): run lifecycle, ACP vs print backend
- [Configuration reference](docs/configuration.md): every setting, its default and its limits
- [Approval and permissions](docs/approval-and-permissions.md): the approval gate and how the permission mode is enforced
- [Process trees on Windows and POSIX](docs/process-tree.md): how runs are stopped and cleaned up
- [Run data and events](docs/run-data-and-events.md): the run model, stored snapshots and live events for Desktop
- [Devin MCP server](docs/devin-mcp.md): optional `mcp_servers` setup
- [Development and testing](docs/development.md)
- [Known limitations](docs/limitations.md)

## License

[MIT](LICENSE).

<br />

<div align="center">
  <strong>Hermes Devin</strong><br />
  <sub>Delegate the work. Keep the say.</sub>
</div>

<br />

> **Community project**
>
> Hermes Devin is an independent community plugin. It is not affiliated with, endorsed by, sponsored by, or officially associated with [Nous Research](https://github.com/NousResearch), the [Hermes Agent project](https://github.com/NousResearch/hermes-agent), or [Cognition](https://cognition.ai). Hermes, Hermes Agent, Nous Research, Devin and Cognition are names and marks belonging to their respective owners.
