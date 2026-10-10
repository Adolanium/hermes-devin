# Known limitations

- **Concurrent runs are refused, not queued.** When `max_concurrent_runs` is reached, a new run is refused, and the model is told to wait or cancel one.
- **Background runs cannot ask you anything.** Devin's in-run permission prompts are denied in background runs. Use `accept-edits` or a wider mode if background runs need to edit files on their own.
- **Background notices need a way in.** Outside the classic CLI, completion notices need `allow_gateway_injection: true` on the plugin entry. Without it, results are still available through `devin_run_result` and `/devin status`.
- **Plan mode needs ACP**, and a Devin build that advertises a read-only session mode. If none is offered, the run is refused rather than run with more rights.
- **Sandboxed modes are not on native Windows.** `sandbox: true` and `autonomous` are rejected there, matching the Devin CLI. Use WSL 2.
- **Matching approvals.** Hermes does not pass a tool-call id to tool handlers, so the approval ticket is matched on session and arguments. Two identical calls in the same session share one ticket slot.
- **The first instant on Windows.** A process Devin spawned before it joined its Job Object would escape the job. See [Process trees](process-tree.md#limits).
- **POSIX and a killed Hermes.** If Hermes is killed outright on macOS or Linux, a background run's process group is not signalled. The run is marked failed the next time the plugin starts.
- **Login output.** `devin auth status` output is read for clear yes/no answers. Anything the plugin does not recognise never blocks a run; Devin then reports a missing login itself.
- **No Desktop panel yet.** The data model, events and read-only API are ready. The panel UI is not part of this release.
- **Tested against a fake CLI.** The tests use a stand-in for the Devin CLI, built from Devin's documented flags and the ACP specification. The real CLI was not run during development, so behaviour differences in a given Devin version could show up first in real use.
