# Contributing

Keep changes focused and preserve Hermes' approval and profile boundaries.
Include a regression test for behavior changes and update the affected documentation.

## Repository layout

- The root Python modules and `plugin.yaml` are the agent plugin.
  Keep the entry points where the Hermes loader expects them.
- `desktop/` contains the Desktop renderer and `dashboard/` its API.
- `tests/` contains the fake CLI and regression checks.
- `docs/getting-started/` contains installation and usage.
- `docs/guides/` contains Local, Cloud and optional MCP guides.
- `docs/reference/` contains settings, permissions and limitations.
- `docs/development/` contains architecture, run data, process cleanup and testing.
- `assets/` contains README branding and screenshots, not runtime dependencies.

## Use a local checkout

Use one plugin installation method. `hermes plugins install` refuses to replace a
linked checkout. Older Hermes Desktop builds may skip linked plugins; update
Desktop or install from Git instead.

Resolve the active profile's home through Hermes rather than assuming a default folder.
Run these commands in the Hermes Python environment for that profile.

macOS and Linux:

```sh
hermes_home="$(python -c 'from hermes_constants import get_hermes_home; print(get_hermes_home())')"
mkdir -p "$hermes_home/plugins"
ln -s "/path/to/hermes-devin" "$hermes_home/plugins/devin"
hermes plugins enable devin
```

Windows PowerShell:

```powershell
$hermesHome = python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"
New-Item -ItemType Directory -Force -Path "$hermesHome\plugins"
New-Item -ItemType Junction -Path "$hermesHome\plugins\devin" -Target "C:\path\to\hermes-devin"
hermes plugins enable devin
```

Restart Hermes after changing plugin code. Enable the Desktop half separately under
**Capabilities → Plugins → Devin**.

## Local checks

The Python suite uses a fake Devin CLI and does not start paid Local or Cloud work.
From the repository root:

```sh
cd tests
python -m pytest
cd ..
ruff check .
ruff format --check .
node --check desktop/plugin.js
node tests/desktop_smoke.mjs /path/to/hermes-agent/node_modules
```

The isolated test environment needs `pytest`, `ruff` and `psutil==7.2.2`.
Hermes provides `psutil` at runtime. Desktop smoke needs Hermes' installed React
and React DOM. For host contract checks, set `HERMES_AGENT_DIR` to the Hermes checkout;
for Desktop checks, set `HERMES_DESKTOP_NODE_MODULES` to its `node_modules` directory.

See [development and testing](docs/development/testing.md) for coverage and platform details.
Never include credentials, private run history or generated caches in a change.
See [TRADEMARKS.md](TRADEMARKS.md) before changing branding assets.
