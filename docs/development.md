# Development and testing

The plugin is plain Python 3.11+ with no dependencies beyond Hermes itself.

## Layout

| File | What it holds |
| --- | --- |
| `__init__.py` | `register(ctx)`: wires tools, hooks, the slash command and shutdown |
| `plugin.yaml` | Manifest and `config_schema` for Desktop |
| `config.py` | Settings: defaults, parsing and validation |
| `core.py` | Tool handlers, argument checks and the `pre_tool_call` / `post_approval_response` hooks |
| `approval.py` | Approval prompt text, rule keys and single-use tickets |
| `runner.py` | Run manager: preflight, workers, concurrency, timeouts, notices |
| `backends.py` | ACP and print backends |
| `jsonrpc.py` | JSON-RPC 2.0 over stdio |
| `process.py` | Process tree: Job Object, `taskkill`, process groups |
| `models.py` | Run record, states, errors, capped output |
| `events.py` | Event envelope and fan-out |
| `store.py` | Snapshot persistence, pruning, orphans |
| `schemas.py` | Tool schemas |
| `slash.py` | `/devin` command |
| `dashboard/plugin_api.py` | Runs API for the Desktop half; `manifest.json` mounts it |
| `desktop/plugin.js` | Desktop half: live run card, Devin page, status-bar chip |

## Running the tests

The tests never run the real Devin CLI. `tests/fake_devin.py` stands in for it: it speaks the same ACP and print interfaces, and its behaviour is chosen by the `FAKE_DEVIN_SCENARIO` environment variable. Scenarios cover success, slow and hanging agents, a child process tree, crashes, missing login, large output, refusals, permission prompts, failures and untrusted folders.

Run pytest from `tests/`:

```bash
cd tests
python -m pytest
```

On Windows, use the Hermes venv's Python:

```powershell
cd tests
E:\Dev\Hermes\hermes-agent\.venv\Scripts\python.exe -m pytest
```

Pytest's scratch files go to `.pytest_tmp/` inside the plugin folder.

What the suite covers:

- **Approval:** the gate, tickets, rule keys and denial paths
- **Runs:** foreground runs for every error code, output caps and argument checks
- **Background:** background runs, notices, the concurrency limit and yielding
- **Permissions:** permission modes and in-run permission requests
- **Stopping:** interrupts, cancels, timeouts and real process-tree kills, plus mocked Windows kill paths
- **Parts:** settings, the run model, events, the store, the slash command and the dashboard API

`tests/test_hermes_contract.py` checks the Hermes APIs the plugin relies on, in a subprocess. It runs when Hermes is importable from the Python in use, or when `HERMES_AGENT_DIR` points to a Hermes checkout. Otherwise it is skipped. On a Hermes venv on Windows the whole suite runs, with one POSIX-only test skipped.

## Lint

```bash
ruff check .
ruff format --check .
```

`ruff.toml` holds the settings.
