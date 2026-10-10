"""Tool schemas: what the model reads."""

from __future__ import annotations

DELEGATE = "delegate_to_devin"
LIST = "devin_list_runs"
STATUS = "devin_run_status"
RESULT = "devin_run_result"
CANCEL = "devin_cancel_run"

ALL_TOOLS = (DELEGATE, LIST, STATUS, RESULT, CANCEL)

_RUN_ID = {"type": "string", "description": "Run id returned by delegate_to_devin (looks like dv-1a2b3c4d5e)."}

SCHEMAS: dict[str, dict] = {
    DELEGATE: {
        "name": DELEGATE,
        "description": (
            "Hand a self-contained coding task to Devin (Cognition's coding agent, via the local Devin CLI). "
            "Devin works in the given directory on its own and reports back. The user must approve every "
            "run before it starts; Devin's permission mode is fixed by the user's plugin settings and cannot "
            "be chosen here. Use background=true for long tasks: the call returns a run_id immediately and "
            "the result is posted to the conversation when Devin finishes. Write the prompt as a complete "
            "brief: goal, relevant files, constraints, how to verify."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The full task for Devin."},
                "cwd": {"type": "string",
                        "description": "Working directory for Devin. Defaults to the current workspace."},
                "background": {"type": "boolean",
                               "description": "Start without waiting; you are notified on completion."},
                "timeout_seconds": {"type": "integer", "minimum": 10,
                                    "description": "Stop Devin after this many seconds (capped by settings)."},
                "title": {"type": "string", "description": "Short label for listings (optional)."},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    LIST: {
        "name": LIST,
        "description": "List recent Devin runs with their state, directory and last activity.",
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "enum": ["active", "finished", "all"],
                          "description": "Filter by state (default all)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max runs (default 10)."},
            },
            "additionalProperties": False,
        },
    },
    STATUS: {
        "name": STATUS,
        "description": "Live status of one Devin run: state, plan progress, recent tool calls and events, usage.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "events": {"type": "integer", "minimum": 0, "maximum": 100,
                           "description": "How many recent progress events to include (default 10)."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    RESULT: {
        "name": RESULT,
        "description": "Full result of a Devin run: final output (capped), error, permissions asked, files touched.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "max_chars": {"type": "integer", "minimum": 500,
                              "description": "Cap on returned output characters (default from settings)."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    CANCEL: {
        "name": CANCEL,
        "description": "Stop a running Devin run, including every process it started.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": _RUN_ID},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
}

EMOJI = {DELEGATE: "🤝", LIST: "📋", STATUS: "📡", RESULT: "📄", CANCEL: "🛑"}
