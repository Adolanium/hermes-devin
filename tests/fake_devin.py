"""Stand-in for the Devin CLI used by the tests. Never talks to any network.

Behaviour is chosen with environment variables:

FAKE_DEVIN_SCENARIO  ok | slow | hang | spawn_child | crash | auth | big | refusal |
                     permission | fail | trust | usage
FAKE_DEVIN_AUTH      ok | no | weird        (for ``auth status``)
FAKE_DEVIN_LOG       file that receives one JSON line per received ACP message
FAKE_DEVIN_PIDFILE   where ``spawn_child`` writes the grandchild's pid
FAKE_DEVIN_BIG       number of characters for the ``big`` scenario
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

SCENARIO = os.environ.get("FAKE_DEVIN_SCENARIO", "ok")
LOG = os.environ.get("FAKE_DEVIN_LOG")
_write_lock = threading.Lock()


def log(entry: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def spawn_grandchild() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    pidfile = os.environ.get("FAKE_DEVIN_PIDFILE")
    if pidfile:
        with open(pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))


# --- auth -----------------------------------------------------------------------


def auth_status() -> int:
    mode = os.environ.get("FAKE_DEVIN_AUTH", "ok")
    if mode == "ok":
        print("Logged in as dev@example.com")
        return 0
    if mode == "no":
        print("Not logged in. Run `devin auth login` to sign in.")
        return 1
    print("something unexpected")
    return 2


# --- print mode -------------------------------------------------------------------


def print_mode(argv: list[str]) -> int:
    prompt = ""
    if "--prompt-file" in argv:
        with open(argv[argv.index("--prompt-file") + 1], encoding="utf-8") as fh:
            prompt = fh.read()
    log({"mode": "print", "argv": argv, "prompt": prompt, "env_mode": os.environ.get("DEVIN_PERMISSION_MODE")})
    if SCENARIO == "auth":
        print("Error: not logged in. Run devin auth login.", file=sys.stderr)
        return 1
    if SCENARIO == "trust":
        print("Error: this workspace is not trusted", file=sys.stderr)
        return 1
    if SCENARIO == "fail":
        print("partial work", flush=True)
        print("boom: something broke", file=sys.stderr)
        return 2
    if SCENARIO in ("slow", "hang"):
        print("working...", flush=True)
        time.sleep(300)
        return 0
    if SCENARIO == "spawn_child":
        spawn_grandchild()
        print("spawned", flush=True)
        time.sleep(300)
        return 0
    if SCENARIO == "big":
        size = int(os.environ.get("FAKE_DEVIN_BIG", "50000"))
        sys.stdout.write("x" * size + "\nTHE END\n")
        return 0
    if SCENARIO in ("print_rejected", "print_rejected_partial"):
        # What Devin CLI 3000.x does when its mode would have asked: warn on stderr, exit 0.
        if SCENARIO == "print_rejected_partial":
            print("Read the code; could not run the tests.", flush=True)
        for _ in range(2):
            print(
                "warning: rejected a tool call that requires confirmation. Running in non-interactive mode. "
                "Use --permission-mode dangerous to auto-approve all tools.",
                file=sys.stderr,
                flush=True,
            )
        return 0
    print(f"Done: {prompt[:40]}", flush=True)
    print("All tests pass.", flush=True)
    return 0


# --- ACP -------------------------------------------------------------------------


def send(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def update(session_id: str, upd: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": upd}})


def text_chunk(session_id: str, text: str) -> None:
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


class Agent:
    def __init__(self) -> None:
        self.cloud = "--cloud" in sys.argv
        self.session = "devin-" + "1" * 32 if self.cloud else "sess-1"
        self.cancelled = threading.Event()
        self.responses: dict[int, dict] = {}
        self.response_ready = threading.Condition()
        self.next_id = 1000
        self.config_values = {"model": "base-medium", "thought_level": "medium"}
        self.config_values.update(
            org_id="test-org", repos="", persona_slug="", devin_version="devin-auto", platform="linux"
        )
        self.current_mode = "normal"
        if "--model" in sys.argv:
            self.config_values["model"] = sys.argv[sys.argv.index("--model") + 1]

    def config_options(self) -> list[dict]:
        if self.cloud:
            menus = {
                "org_id": [("test-org", "Test organization")],
                "repos": [("", "No repository"), ("test/app", "app")],
                "persona_slug": [("", "Agent"), ("analyst", "Analyst")],
                "devin_version": [("devin-auto", "Fusion"), ("devin_lite", "Lite")],
                "platform": [("linux", "Ubuntu"), ("macos", "macOS"), ("windows", "Windows")],
            }
            return [
                {
                    "id": key,
                    "name": key,
                    "type": "select",
                    "category": "model" if key == "devin_version" else "other",
                    "currentValue": self.config_values[key],
                    "options": [{"value": value, "name": name} for value, name in choices],
                    "_meta": {"signedUrl": "https://private.invalid/?token=private"},
                }
                for key, choices in menus.items()
            ]
        fusion = self.config_values["model"].startswith("fusion-")
        thinking = ["low", "medium", "high"] if fusion else ["medium", "high"]
        return [
            {
                "id": "mode",
                "name": "Permission mode",
                "category": "mode",
                "type": "select",
                "currentValue": self.current_mode,
                "options": [{"value": "bypass", "name": "Bypass"}],
            },
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": self.config_values["model"],
                "options": [
                    {"value": "base-medium", "name": "Base"},
                    {"value": "fusion-frontier-medium-sidekick-economy-medium", "name": "Fusion (Frontier + Economy)"},
                ],
            },
            {
                "id": "thought_level",
                "name": "Thinking",
                "category": "thought_level",
                "type": "select",
                "currentValue": self.config_values["thought_level"],
                "options": [{"value": value, "name": value.title()} for value in thinking],
            },
        ]

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        rid = self.next_id
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        with self.response_ready:
            while rid not in self.responses:
                self.response_ready.wait(0.1)
            return self.responses.pop(rid)

    def handle_prompt(self, rid: int, params: dict) -> None:
        sid = self.session
        prompt = "".join(b.get("text", "") for b in params.get("prompt", []))
        if SCENARIO == "config_changed":
            self.config_values["devin_version" if self.cloud else "model"] = (
                "devin_lite" if self.cloud else "base-medium"
            )
            update(sid, {"sessionUpdate": "config_option_update", "configOptions": self.config_options()})
            for _ in range(100):
                if self.cancelled.wait(0.05):
                    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                    return
        if SCENARIO == "crash":
            os._exit(3)
        if SCENARIO == "refusal":
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "refusal"}})
            return
        if SCENARIO == "two_messages":
            text_chunk(sid, "I'll look first.")
        update(
            sid,
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "read code", "status": "completed", "priority": "high"},
                    {"content": "fix bug", "status": "in_progress", "priority": "high"},
                ],
            },
        )
        update(
            sid,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Edit app.py",
                "kind": "edit",
                "status": "pending",
                "locations": [{"path": "app.py"}],
            },
        )
        if SCENARIO == "permission":
            answer = self.request(
                "session/request_permission",
                {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "t2", "title": "Run `rm -rf build`", "kind": "execute"},
                    "options": [
                        {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "always", "name": "Always", "kind": "allow_always"},
                        {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            )
            outcome = (answer.get("result") or {}).get("outcome") or {}
            text_chunk(sid, f"permission outcome: {outcome.get('outcome')}:{outcome.get('optionId')}\n")
            answer2 = self.request(
                "session/request_permission",
                {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "t3", "title": "Edit main.py", "kind": "edit"},
                    "options": [
                        {"optionId": "y", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "n", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            )
            outcome2 = (answer2.get("result") or {}).get("outcome") or {}
            text_chunk(sid, f"edit outcome: {outcome2.get('optionId')}\n")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        if SCENARIO in ("repeated_edits", "many_edits"):
            path = os.path.join(os.getcwd(), "index.html")
            count = 35 if SCENARIO == "many_edits" else 3
            for i in range(count):
                item = {"type": "diff", "path": path, "oldText": "", "newText": f"edit-{i}\n"}
                content = [item]
                if i == 0 and SCENARIO == "repeated_edits":
                    content.append({**item, "newText": "second-hunk\n"})
                update(
                    sid, {"sessionUpdate": "tool_call", "toolCallId": f"edit-{i}", "kind": "edit", "content": content}
                )
                if i == count - 1:
                    content = [{**item, "newText": "final-snapshot\n"}]
                for _ in range(2):
                    update(
                        sid,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": f"edit-{i}",
                            "status": "completed",
                            "content": content,
                        },
                    )
        if SCENARIO == "live":
            # Shapes captured from Devin CLI 3000.11.3: an edit with old and new text, a new file
            # (no oldText), and a command whose output arrives as a growing snapshot.
            path = os.path.join(os.getcwd(), "calc.py")
            update(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "e1",
                    "title": "Edit file",
                    "kind": "edit",
                    "content": [
                        {
                            "type": "diff",
                            "path": path,
                            "oldText": "def add(a, b):\n    return a + b\n",
                            "newText": "def add(a: int, b: int) -> int:\n    return a + b\n",
                        }
                    ],
                },
            )
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "e1", "status": "completed"})
            update(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "e2",
                    "title": "Write file",
                    "kind": "edit",
                    "content": [
                        {"type": "diff", "path": os.path.join(os.getcwd(), "notes.md"), "newText": "# Notes\n"}
                    ],
                },
            )
            update(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "x1",
                    "title": "Ran pytest",
                    "kind": "execute",
                    "rawInput": {"command": "pytest -q"},
                },
            )
            for upto in (1, 2, 3):
                update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "x1",
                        "status": "in_progress",
                        "content": [
                            {
                                "type": "content",
                                "content": {"type": "text", "text": "\n".join(f"line {i}" for i in range(upto))},
                            }
                        ],
                    },
                )
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "x1",
                    "status": "in_progress",
                    "_meta": {"terminal_exit": {"exit_code": 1}},
                },
            )
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "x1", "status": "completed"})
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "r1", "title": "Read file", "kind": "read"})
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "r1",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "6 lines"}}],
                },
            )
        if SCENARIO == "diff_edit":
            # Real Devin names edited files only in diff content, never in locations.
            update(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "t4",
                    "title": "Wrote util.py",
                    "kind": "edit",
                    "content": [{"type": "diff", "path": "util.py", "newText": "x = 1"}],
                },
            )
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t4",
                    "status": "completed",
                    "content": [
                        {"type": "diff", "path": "util.py", "newText": "x = 1"},
                        {"type": "diff", "path": "README.md", "newText": "y"},
                    ],
                },
            )
        if SCENARIO in ("slow", "hang", "spawn_child"):
            if SCENARIO == "spawn_child":
                spawn_grandchild()
            text_chunk(sid, "working...")
            deadline = time.time() + 300
            while time.time() < deadline:
                if self.cancelled.is_set() and SCENARIO == "slow":
                    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                    return
                time.sleep(0.05)
            return
        if SCENARIO == "big":
            size = int(os.environ.get("FAKE_DEVIN_BIG", "50000"))
            for _ in range(size // 1000):
                text_chunk(sid, "y" * 1000)
            text_chunk(sid, "\nTHE END")
        else:
            text_chunk(sid, "Fixed the bug in app.py. ")
            text_chunk(sid, f"Task was: {prompt[:30]}")
        update(
            sid,
            {
                "sessionUpdate": "usage_update",
                "used": 1200,
                "size": 200000,
                "cost": {"amount": 0.42, "currency": "USD"},
            },
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200},
                },
            }
        )

    def serve(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            log({"mode": "acp", "msg": msg, "env_mode": os.environ.get("DEVIN_PERMISSION_MODE"), "argv": sys.argv[1:]})
            method = msg.get("method")
            rid = msg.get("id")
            if method is None:
                with self.response_ready:
                    self.responses[rid] = msg
                    self.response_ready.notify_all()
                continue
            if method == "initialize":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {"loadSession": True},
                            "authMethods": [],
                            "agentInfo": {"name": "fake-devin", "version": "0.0.1"},
                        },
                    }
                )
            elif method == "session/new":
                if SCENARIO == "config_discovery_hang":
                    time.sleep(300)
                if SCENARIO == "auth":
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
                    continue
                if SCENARIO == "devin_modes":
                    # What Devin CLI 3000.x advertises: no mode that asks before edits.
                    modes = {
                        "currentModeId": "accept-edits",
                        "availableModes": [
                            {"id": "accept-edits", "name": "Code"},
                            {"id": "smart", "name": "Smart"},
                            {"id": "ask", "name": "Ask"},
                            {"id": "plan", "name": "Plan"},
                            {"id": "bypass", "name": "Bypass Permissions"},
                            {"id": "autonomous-x", "name": "Auto Pilot"},
                        ],
                    }
                else:
                    modes = {
                        "currentModeId": "normal",
                        "availableModes": [
                            {"id": "normal", "name": "Normal"},
                            {"id": "accept-edits", "name": "Accept Edits"},
                            {"id": "plan", "name": "Plan"},
                            {"id": "bypass", "name": "Bypass"},
                        ],
                    }
                result = {"sessionId": self.session, "modes": modes}
                if self.cloud:
                    result.pop("modes")
                    result["_meta"] = {
                        "signedUrl": "https://private.invalid/?token=private",
                        "credentials": "never-save",
                    }
                if SCENARIO.startswith("config") or self.cloud:
                    result["configOptions"] = self.config_options()
                send({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "session/load":
                wanted = str((msg.get("params") or {}).get("sessionId") or "")
                if wanted == "missing-session":
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Session not found"}})
                    continue
                # Like Devin: the old conversation is replayed before the reply.
                self.session = wanted
                update(wanted, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old ask"}})
                text_chunk(wanted, "REPLAYED OLD ANSWER")
                result = {
                    "modes": {
                        "currentModeId": "normal",
                        "availableModes": [
                            {"id": "normal", "name": "Normal"},
                            {"id": "accept-edits", "name": "Accept Edits"},
                            {"id": "plan", "name": "Plan"},
                            {"id": "bypass", "name": "Bypass"},
                        ],
                    }
                }
                if self.cloud:
                    result.pop("modes")
                if SCENARIO.startswith("config") or self.cloud:
                    result["configOptions"] = self.config_options()
                send({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "session/set_config_option":
                params = msg.get("params") or {}
                if SCENARIO == "config_rejected":
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Model unavailable"}})
                    continue
                ident, value = params.get("configId"), params.get("value")
                self.config_values[ident] = value
                if ident == "model":
                    self.config_values["thought_level"] = "medium"
                    self.current_mode = "bypass"
                if SCENARIO == "config_canonical" and ident == "thought_level":
                    self.config_values["model"] = "fusion-frontier-high-sidekick-economy-medium"
                update(self.session, {"sessionUpdate": "config_option_update", "configOptions": self.config_options()})
                send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": self.config_options()}})
            elif method == "session/set_mode":
                self.current_mode = (msg.get("params") or {}).get("modeId")
                if SCENARIO.startswith("config"):
                    update(
                        self.session, {"sessionUpdate": "config_option_update", "configOptions": self.config_options()}
                    )
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "session/prompt":
                threading.Thread(target=self.handle_prompt, args=(rid, msg.get("params") or {}), daemon=True).start()
            elif method == "session/cancel":
                self.cancelled.set()
            elif rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
        if SCENARIO == "hang":
            time.sleep(300)  # a stuck agent: ignores cancel and the closed connection
        return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv[:2] == ["models", "list"]:
        print(
            json.dumps(
                {
                    "families": [
                        {
                            "family_label": "Fusion",
                            "variants": [
                                {
                                    "model_uid": "fusion-frontier-medium-sidekick-economy-medium",
                                    "label": "Fusion (Frontier Medium + Economy Medium)",
                                    "cost_summary": "Catalog price supplied by CLI",
                                }
                            ],
                        }
                    ]
                }
            )
        )
        return 0
    if argv[:2] == ["auth", "status"]:
        return auth_status()
    if "--print" in argv or "-p" in argv:
        return print_mode(argv)
    if "acp" in argv:
        return Agent().serve()
    print("fake devin: unknown invocation", argv, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
