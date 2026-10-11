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
    log({"mode": "print", "argv": argv, "prompt": prompt,
         "env_mode": os.environ.get("DEVIN_PERMISSION_MODE")})
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
            print("warning: rejected a tool call that requires confirmation. Running in non-interactive mode. "
                  "Use --permission-mode dangerous to auto-approve all tools.", file=sys.stderr, flush=True)
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
        self.session = "sess-1"
        self.cancelled = threading.Event()
        self.responses: dict[int, dict] = {}
        self.response_ready = threading.Condition()
        self.next_id = 1000

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
        if SCENARIO == "crash":
            os._exit(3)
        if SCENARIO == "refusal":
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "refusal"}})
            return
        if SCENARIO == "two_messages":
            text_chunk(sid, "I'll look first.")
        update(sid, {"sessionUpdate": "plan", "entries": [
            {"content": "read code", "status": "completed", "priority": "high"},
            {"content": "fix bug", "status": "in_progress", "priority": "high"}]})
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Edit app.py", "kind": "edit",
                     "status": "pending", "locations": [{"path": "app.py"}]})
        if SCENARIO == "permission":
            answer = self.request("session/request_permission", {
                "sessionId": sid,
                "toolCall": {"toolCallId": "t2", "title": "Run `rm -rf build`", "kind": "execute"},
                "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "always", "name": "Always", "kind": "allow_always"},
                            {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
            outcome = (answer.get("result") or {}).get("outcome") or {}
            text_chunk(sid, f"permission outcome: {outcome.get('outcome')}:{outcome.get('optionId')}\n")
            answer2 = self.request("session/request_permission", {
                "sessionId": sid,
                "toolCall": {"toolCallId": "t3", "title": "Edit main.py", "kind": "edit"},
                "options": [{"optionId": "y", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "n", "name": "Reject", "kind": "reject_once"}]})
            outcome2 = (answer2.get("result") or {}).get("outcome") or {}
            text_chunk(sid, f"edit outcome: {outcome2.get('optionId')}\n")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        if SCENARIO == "diff_edit":
            # Real Devin names edited files only in diff content, never in locations.
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t4", "title": "Wrote util.py", "kind": "edit",
                         "content": [{"type": "diff", "path": "util.py", "newText": "x = 1"}]})
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t4", "status": "completed",
                         "content": [{"type": "diff", "path": "util.py", "newText": "x = 1"},
                                     {"type": "diff", "path": "README.md", "newText": "y"}]})
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
        update(sid, {"sessionUpdate": "usage_update", "used": 1200, "size": 200000,
                     "cost": {"amount": 0.42, "currency": "USD"}})
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "stopReason": "end_turn", "usage": {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200}}})

    def serve(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            log({"mode": "acp", "msg": msg, "env_mode": os.environ.get("DEVIN_PERMISSION_MODE"),
                 "argv": sys.argv[1:]})
            method = msg.get("method")
            rid = msg.get("id")
            if method is None:
                with self.response_ready:
                    self.responses[rid] = msg
                    self.response_ready.notify_all()
                continue
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": 1, "agentCapabilities": {}, "authMethods": [],
                    "agentInfo": {"name": "fake-devin", "version": "0.0.1"}}})
            elif method == "session/new":
                if SCENARIO == "auth":
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
                    continue
                if SCENARIO == "devin_modes":
                    # What Devin CLI 3000.x advertises: no mode that asks before edits.
                    modes = {"currentModeId": "accept-edits", "availableModes": [
                        {"id": "accept-edits", "name": "Code"}, {"id": "smart", "name": "Smart"},
                        {"id": "ask", "name": "Ask"}, {"id": "plan", "name": "Plan"},
                        {"id": "bypass", "name": "Bypass Permissions"}, {"id": "autonomous-x", "name": "Auto Pilot"}]}
                else:
                    modes = {"currentModeId": "normal", "availableModes": [
                        {"id": "normal", "name": "Normal"}, {"id": "accept-edits", "name": "Accept Edits"},
                        {"id": "plan", "name": "Plan"}, {"id": "bypass", "name": "Bypass"}]}
                send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": self.session, "modes": modes}})
            elif method == "session/set_mode":
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "session/prompt":
                threading.Thread(target=self.handle_prompt, args=(rid, msg.get("params") or {}),
                                 daemon=True).start()
            elif method == "session/cancel":
                self.cancelled.set()
            elif rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
        if SCENARIO == "hang":
            time.sleep(300)  # a stuck agent: ignores cancel and the closed connection
        return 0


def main() -> int:
    argv = sys.argv[1:]
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
