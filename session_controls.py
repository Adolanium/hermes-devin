"""Server-confirmed model settings and single-use Desktop launch selections."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any

from .config import DevinConfig
from .jsonrpc import METHOD_NOT_FOUND, JsonRpcPeer, PeerClosed, RpcError

MODEL_CATEGORIES = {"model", "model_config", "thought_level"}
FORBIDDEN_IDS = {
    "mode",
    "permission_mode",
    "approval_mode",
    "permission_requests",
    "sandbox",
    "trust_workspace",
    "target",
    "execution_target",
    "cli_path",
    "api_key",
    "auth_token",
    "network_policy",
    "local_tools_approval_mode",
}
SELECTION_TTL = 15 * 60
MAX_SELECTIONS = 64
logger = logging.getLogger(__name__)


class ControlsError(ValueError):
    def __init__(self, message: str, *, code: str = "selection_invalid"):
        super().__init__(message)
        self.code = code
        self.message = message


def config_fingerprint(cfg: DevinConfig) -> str:
    blob = json.dumps(asdict(cfg), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int = 512) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _choices(raw: Any, *, group: str = "", depth: int = 0) -> list[dict]:
    if not isinstance(raw, list) or depth > 2:
        return []
    choices = []
    for item in raw[:2000]:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("options"), list):
            choices.extend(_choices(item["options"], group=_text(item.get("name")), depth=depth + 1))
        elif isinstance(item.get("value"), str) and len(item["value"]) <= 512:
            choice = {"value": item["value"], "name": _text(item.get("name")) or item["value"]}
            if item.get("description"):
                choice["description"] = _text(item["description"], 2000)
            if group:
                choice["group"] = group
            choices.append(choice)
    return choices[:2000]


def model_options(raw: Any) -> list[dict]:
    """Drop permission controls, unsupported types, and arbitrary agent metadata."""
    if not isinstance(raw, list):
        return []
    options = []
    seen = set()
    for item in raw[:32]:
        if not isinstance(item, dict):
            continue
        ident, category = _text(item.get("id")), item.get("category")
        if not ident or ident in seen or ident in FORBIDDEN_IDS:
            continue
        if category not in MODEL_CATEGORIES or item.get("type") != "select":
            continue
        current = item.get("currentValue")
        if not isinstance(current, str) or len(current) > 512:
            continue
        choices = _choices(item.get("options"))
        if current and not any(choice["value"] == current for choice in choices):
            choices.insert(0, {"value": current, "name": current})
        if not choices:
            continue
        seen.add(ident)
        options.append(
            {
                "id": ident,
                "name": _text(item.get("name")) or ident,
                "category": category,
                "type": "select",
                "currentValue": current,
                "options": choices,
            }
        )
    return options


def option_values(options: list[dict]) -> dict[str, str]:
    return {option["id"]: option["currentValue"] for option in options}


def apply_options(
    peer: JsonRpcPeer, session_id: str, raw: Any, values: dict[str, str], *, timeout: Any = lambda: 30.0
) -> list[dict]:
    options = model_options(raw)
    if not isinstance(values, dict) or len(values) > 16:
        raise ControlsError("Model settings must be an object with at most 16 selections.")
    # A model change can replace its thinking/speed options. Validate each
    # subsequent choice against the new response, not the initial catalog.
    ordered = sorted(values, key=lambda key: (key != "model", key))
    for ident in ordered:
        value = values[ident]
        option = next((item for item in options if item["id"] == ident), None)
        if not isinstance(value, str) or option is None:
            raise ControlsError(f"Model control '{ident}' is not available in this session.")
        if not any(choice["value"] == value for choice in option["options"]):
            raise ControlsError(f"The selected value for '{option['name']}' is no longer available. Choose again.")
        if option["currentValue"] == value:
            continue
        result = peer.request(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": ident,
                "value": value,
            },
            timeout=timeout(),
        )
        options = model_options(result.get("configOptions") if isinstance(result, dict) else None)
        confirmed = next((item for item in options if item["id"] == ident), None)
        if confirmed is None or confirmed["currentValue"] != value:
            raise ControlsError(f"Devin did not confirm the selected '{option['name']}'. No task was sent.")
    return options


@dataclass(frozen=True)
class LaunchConfig(DevinConfig):
    session_options: tuple[tuple[str, str], ...] = ()
    model_label: str = ""
    model_cost_summary: str = ""

    @classmethod
    def from_config(cls, cfg: DevinConfig, **selection: Any) -> LaunchConfig:
        base = {item.name: getattr(cfg, item.name) for item in fields(DevinConfig)}
        base.update(selection)
        return cls(**base)


@dataclass(frozen=True)
class LaunchSelection:
    config: LaunchConfig
    base_fingerprint: str
    cwd: str
    expires_at: float


class SessionControls:
    def __init__(self, manager: Any, *, clock: Any = time.monotonic):
        self.manager = manager
        self.clock = clock
        self._lock = threading.Lock()
        self._selections: dict[str, LaunchSelection] = {}
        self._probes = threading.BoundedSemaphore(2)
        self._closed = False
        self._trees: set[Any] = set()

    def issue(self, base: DevinConfig, cwd: str, options: list[dict]) -> str:
        values = option_values(options)
        if "model" not in values or not values["model"]:
            raise ControlsError("Devin did not advertise a selectable model. Use plugin defaults explicitly.")
        model = next(option for option in options if option["id"] == "model")
        choice = next((item for item in model["options"] if item["value"] == values["model"]), {})
        selected = LaunchConfig.from_config(
            base,
            model=values["model"],
            session_options=tuple(values.items()),
            model_label=choice.get("label") or choice.get("name") or values["model"],
            model_cost_summary=choice.get("cost_summary") or "",
        )
        nonce = secrets.token_hex(24)
        now = self.clock()
        with self._lock:
            if self._closed:
                raise ControlsError("The plugin stopped. Reopen it before choosing a model.")
            self._selections = {key: value for key, value in self._selections.items() if value.expires_at > now}
            while len(self._selections) >= MAX_SELECTIONS:
                self._selections.pop(next(iter(self._selections)))
            self._selections[nonce] = LaunchSelection(selected, config_fingerprint(base), cwd, now + SELECTION_TTL)
        return nonce

    def resolve(self, nonce: Any, base: DevinConfig, *, consume: bool = False) -> LaunchSelection:
        if not isinstance(nonce, str) or len(nonce) != 48:
            raise ControlsError("Invalid saved launch selection. Choose the model again in Desktop.")
        with self._lock:
            selected = self._selections.get(nonce)
            if selected is None or selected.expires_at <= self.clock():
                self._selections.pop(nonce, None)
                raise ControlsError("The launch selection expired or was used. Choose the model again in Desktop.")
            if selected.base_fingerprint != config_fingerprint(base):
                raise ControlsError("Plugin settings changed after selection. Refresh the model controls.")
            if consume:
                self._selections.pop(nonce)
            return selected

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._selections.clear()
            trees = list(self._trees)
        for tree in trees:
            tree.stop(grace=0.5)

    def _spawn(self, argv: list[str], **kwargs: Any) -> Any:
        with self._lock:
            if self._closed:
                raise ControlsError("The plugin stopped. Reopen it before choosing a model.")
            tree = self.manager._spawn(argv, **kwargs)
            self._trees.add(tree)
            return tree

    def defaults(self) -> dict[str, str]:
        root = self.manager.store.root
        if root is None:
            return {}
        try:
            path = root.parent / "session-preferences.json"
            if path.stat().st_size > 16_000:
                return {}
            values = json.loads(path.read_text(encoding="utf-8")).get("local", {})
            if (
                isinstance(values, dict)
                and len(values) <= 16
                and all(
                    isinstance(key, str) and isinstance(value, str) and len(value) <= 512
                    for key, value in values.items()
                )
            ):
                return values
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def remember(self, cfg: DevinConfig) -> None:
        root = self.manager.store.root
        values = dict(getattr(cfg, "session_options", ()))
        if root is None or not values:
            return
        tmp = ""
        try:
            with self._lock:
                path = root.parent / "session-preferences.json"
                preferences = {}
                if path.is_file() and path.stat().st_size <= 16_000:
                    try:
                        preferences = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
                if not isinstance(preferences, dict):
                    preferences = {}
                preferences["local"] = values
                root.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".session-preferences.", dir=root.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(preferences, stream)
                os.replace(tmp, path)
        except OSError:
            logger.warning("Could not save model defaults")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def discover(self, cfg: DevinConfig, cwd: str, values: dict[str, str]) -> dict:
        if cfg.backend != "acp":
            raise ControlsError(
                "Model controls need backend: acp. The print backend still uses plugin settings.",
                code="backend_unsupported",
            )
        if not self._probes.acquire(blocking=False):
            raise ControlsError("Model discovery is busy. Try again shortly.", code="discovery_busy")
        try:
            argv = self.manager.preflight(cfg)
            options = self._probe(cfg, cwd, argv, values)
            catalog = self._catalog(cfg, cwd, argv)
            for option in options:
                if option["category"] != "model":
                    continue
                for choice in option["options"]:
                    choice.update(catalog.get(choice["value"], {}))
            return {
                "ok": True,
                "cwd": cwd,
                "permission_mode": cfg.permission_mode,
                "config_options": options,
                "values": option_values(options),
                "launch_id": self.issue(cfg, cwd, options),
            }
        finally:
            self._probes.release()

    @staticmethod
    def _deny_request(method: str, params: dict) -> dict:
        if method == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        raise RpcError(METHOD_NOT_FOUND, "Discovery does not run tools")

    def _probe(self, cfg: DevinConfig, cwd: str, argv: list[str], values: dict[str, str]) -> list[dict]:
        from .backends import CLIENT_INFO, _StderrTail

        args = list(argv)
        if cfg.sandbox:
            args.append("--sandbox")
        args.append("acp")
        if cfg.model:
            args += ["--model", cfg.model]
        tree = self._spawn(args, cwd=cwd, env=self.manager._child_env(cfg), stdin=subprocess.PIPE)
        stderr = _StderrTail(tree.proc.stderr)
        peer = JsonRpcPeer(
            tree.proc.stdout,
            tree.proc.stdin,
            on_notification=lambda method, params: None,
            on_request=self._deny_request,
        )
        peer.start()
        deadline = self.clock() + 45

        def budget() -> float:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Model discovery timed out")
            return min(30, remaining)

        try:
            peer.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=budget(),
            )
            session = peer.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=budget())
            if not isinstance(session, dict) or not session.get("sessionId"):
                raise ControlsError("Devin returned no session. Check the CLI login and workspace trust.")
            return apply_options(peer, str(session["sessionId"]), session.get("configOptions"), values, timeout=budget)
        except (RpcError, PeerClosed, TimeoutError) as exc:
            raise ControlsError(
                "Could not load model controls. Check Devin CLI authentication, workspace trust, "
                "and ACP support, then retry.",
                code="discovery_failed",
            ) from exc
        finally:
            self._cleanup(tree)
            peer.join(2)
            stderr.join(1)

    def _cleanup(self, tree: Any) -> None:
        if tree.poll() is None:
            try:
                if tree.proc.stdin:
                    tree.proc.stdin.close()
            except (OSError, ValueError):
                pass
            if tree.wait(0.5) is None:
                tree.stop(grace=0.5)
        tree.reap_descendants()
        tree.close_streams()
        with self._lock:
            self._trees.discard(tree)

    def _catalog(self, cfg: DevinConfig, cwd: str, argv: list[str]) -> dict[str, dict]:
        tree = None
        try:
            tree = self._spawn(argv + ["models", "list", "--format", "json"], cwd=cwd, env=self.manager._child_env(cfg))
            stdout, _stderr = tree.proc.communicate(timeout=15)
            if tree.proc.returncode or len(stdout) > 8 * 1024 * 1024:
                return {}
            raw = json.loads(stdout)
            result = {}
            for family in raw.get("families", [])[:200] if isinstance(raw, dict) else []:
                if not isinstance(family, dict):
                    continue
                for variant in family.get("variants", [])[:2000]:
                    if not isinstance(variant, dict) or not isinstance(variant.get("model_uid"), str):
                        continue
                    meta = {"family": _text(family.get("family_label"))}
                    for key in ("label", "description", "cost_summary", "cost_tier"):
                        if isinstance(variant.get(key), str):
                            meta[key] = _text(variant[key], 2000)
                    result[variant["model_uid"]] = meta
            return result
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            return {}
        finally:
            if tree is not None:
                self._cleanup(tree)
