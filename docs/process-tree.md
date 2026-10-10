# Process trees on Windows and POSIX

Devin starts shells, test runners, language servers and MCP servers. Killing only the `devin` process would leave all of them running, so each run gets a container for its whole tree.

## Starting a run

**Windows**

- Devin starts with `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.
- It is then assigned to a Job Object created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Every process Devin starts afterwards joins the job.
- If Hermes itself exits or crashes, the job handle closes and Windows kills the tree.
- If a job cannot be created or assigned, the run continues with the `taskkill` fallback below. The log says so.

**macOS and Linux**

- Devin starts in a new session (`start_new_session`), so it leads its own process group.

## Stopping a run

Cancel, timeout, interrupt, unloading the plugin and Hermes shutdown all use the same sequence:

1. **Polite stop (ACP).** Send `session/cancel` and give Devin up to half of `cancel_grace_seconds` to end its turn. Then close its stdin and wait up to another half for the process to exit. A well-behaved agent exits on its own here. The print backend has no cancel message, so it goes straight to step 2.
2. **Terminate (POSIX only).** `SIGTERM` to the process group, then wait again. Windows skips this step. A windowless child has no console to receive control events, and a misdirected `CTRL_BREAK_EVENT` could land on Hermes itself.
3. **Kill the tree.** This runs even if the main process already exited, because its children may still be alive.
   - Windows: `TerminateJobObject`. Without a job, `taskkill /PID <pid> /T /F`, then a direct kill if needed.
   - POSIX: `SIGKILL` to the process group.

After a normal exit, any process still left in the job or group is reaped the same way.

## The kill report

Stopped runs include a `kill_report`:

```json
{
  "method": "job_object",
  "graceful": true,
  "exit_code": 0,
  "steps": ["soft_stop", "terminate_job"]
}
```

- `method` is `job_object`, `taskkill` or `process_group`.
- `graceful` is true when Devin exited before any hard kill.
- `steps` lists what was done, including failures, such as `terminate_job_failed:...` or `taskkill_rc:128`.

## Limits

- **The first instant on Windows.** The child is assigned to its job right after it starts. A process it spawned in that first instant would not be in the job. Devin starts its helpers well after startup, so in practice the job holds the tree.
- **The taskkill fallback** follows parent and child links. If the job is unavailable and Devin has already exited, its orphaned children can no longer be found that way.
- **Background runs and Hermes crashes.** On POSIX, if Hermes is killed outright (no `atexit`), the process group is not signalled and Devin may keep running. Windows does not have this gap, because of the job.
