# Installation

[Documentation](../README.md)

## Prerequisites

- [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started/installation),
  with a configured model provider. Use Hermes Desktop if you want the visual controls.
- The standalone [Devin CLI](https://cli.devin.ai), signed in to an account with access
  to the target and models you want to use.

Hermes and Devin each need their own authentication. Devin Desktop is optional.
The plugin uses the existing CLI login for Local and Cloud, not a separate Cloud API key.

## Install the Devin CLI

macOS and Linux:

```sh
curl -fsSL https://cli.devin.ai/install.sh | bash
devin auth login
```

Windows PowerShell:

```powershell
irm https://static.devin.ai/cli/setup.ps1 | iex
devin auth login
```

Restart your terminal if the installer changed PATH. Check that the CLI is available:

```sh
devin --version
devin auth status
```

## Install the community plugin

Use the Hermes installation and profile where you want the plugin:

```sh
hermes plugins install hermes-devin
hermes plugins enable devin
```

`hermes-devin` is the catalog entry, which installs the reviewed commit. The plugin
key Hermes uses after installation is `devin`. Run `hermes plugins update devin`
to move to the next reviewed commit.

To track the repository instead of the catalog, install `Adolanium/hermes-devin`.
That install does not follow catalog updates. A linked development checkout and a Git
installation cannot occupy the same plugin directory. See
[local development](../../CONTRIBUTING.md#use-a-local-checkout).

Restart Hermes. For Desktop, turn on the Desktop half under
**Capabilities → Plugins → Devin**.

## Configure your first Local run

Settings live under `plugins.entries.devin.settings` in the active profile's
`config.yaml`. Desktop exposes the same settings as a form.

Replace the example root with your own project directory:

```yaml
plugins:
  enabled: [devin]
  entries:
    devin:
      allow_gateway_injection: true
      settings:
        permission_mode: accept-edits
        allowed_roots: ["/path/to/projects"]
        max_concurrent_runs: 2
        timeout_seconds: 1800
        background_timeout_seconds: 3600
```

On native Windows, use your Windows project path instead, for example
`allowed_roots: ['C:\Projects']`.

`accept-edits` lets Devin edit files without a separate question for every edit.
Its other permission requests come through Hermes where supported.
Read [Approval and permissions](../reference/approval-and-permissions.md) before
choosing a wider mode.

The default `normal` mode asks before every action. Current Devin builds do not
advertise an equivalent ACP session mode, so the plugin refuses Local ACP runs
with that setting. Choose a supported mode, or keep `normal` with `backend: print`,
where Devin rejects actions that require confirmation.

`allow_gateway_injection: true` lets background results enter TUI, Desktop and gateway
conversations. Without it, results remain available on run cards and through the
status and result tools.

## Check the connection

In a Hermes conversation:

```text
/devin doctor
```

Then follow the [usage guide](usage.md). If a launch is refused, check
[known limitations](../reference/limitations.md) and the [configuration reference](../reference/configuration.md).

For Cloud, use the separate target selector and confirm Cloud-managed permissions.
Local permission modes and folder restrictions do not govern remote actions.
Read the [Cloud guide](../guides/cloud-sessions.md) before launching remote work.
