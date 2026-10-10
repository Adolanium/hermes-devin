"""Plugin settings: loading, defaults and validation.

Everything lives under ``plugins.entries.devin.settings`` in config.yaml, which
is the namespace Hermes hands each plugin (``ctx.get_config``). The permission
mode Devin runs with is read from here and only from here: no tool argument can
select or widen it.

Validation is strict on purpose. A typo in a safety-relevant key (an unknown
permission mode, ``allow_dangerous: "yes please"``) must stop runs with a clear
message rather than silently fall back to something else.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

PLUGIN_ID = "devin"

BACKENDS = ("acp", "print")

# Plugin-facing permission modes. ``plan`` is read-only planning and only
# exists over ACP (it is a Devin agent mode, not a ``--permission-mode``).
PERMISSION_MODES = ("plan", "normal", "accept-edits", "smart", "dangerous", "autonomous")
PERMISSION_ALIASES = {"auto": "normal", "yolo": "dangerous", "bypass": "dangerous"}

# What the plugin does when Devin asks for permission in the middle of an ACP run.
PERMISSION_REQUEST_POLICIES = ("ask", "deny")

APPROVAL_SCOPES = ("run", "workspace")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Settings that must not be run with. The message is shown to the user."""


@dataclass(frozen=True)
class DevinConfig:
    cli_path: str = ""  # empty: find ``devin`` on PATH
    backend: str = "acp"
    model: str = ""
    permission_mode: str = "normal"
    allow_dangerous: bool = False
    sandbox: bool = False
    trust_workspace: bool = False
    permission_requests: str = "ask"
    approval_scope: str = "run"
    default_cwd: str = ""
    allowed_roots: tuple[str, ...] = ()
    default_background: bool = False
    timeout_seconds: int = 1800
    max_timeout_seconds: int = 7200
    background_timeout_seconds: int = 3600
    max_concurrent_runs: int = 2
    max_output_chars: int = 200_000
    max_result_chars: int = 12_000
    max_progress_events: int = 500
    max_prompt_chars: int = 100_000
    history_limit: int = 50
    notify_on_complete: bool = True
    preflight_auth_check: bool = True
    cancel_grace_seconds: float = 5.0
    persist_runs: bool = True
    extra_env: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_dangerous(self) -> bool:
        return self.permission_mode == "dangerous"


# --- coercion ---------------------------------------------------------------


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def _int(raw: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {number}")
    return number


def _float(raw: Mapping[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a number, got {value!r}") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {number}")
    return number


def _str(raw: Mapping[str, Any], key: str, default: str) -> str:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {value!r}")
    return value.strip()


def _choice(raw: Mapping[str, Any], key: str, default: str, allowed: tuple[str, ...],
            aliases: Mapping[str, str] | None = None) -> str:
    value = _str(raw, key, default).lower()
    value = (aliases or {}).get(value, value)
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}; got {raw.get(key)!r}")
    return value


def _str_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{key} must be a list of paths")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _env_map(raw: Mapping[str, Any], key: str) -> dict[str, str]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a mapping of NAME: value")
    out: dict[str, str] = {}
    for name, item in value.items():
        name = str(name)
        if not name or "=" in name or name.upper() == "DEVIN_PERMISSION_MODE":
            raise ConfigError(f"{key}: {name!r} cannot be set here")
        out[name] = str(item)
    return out


# --- parse + validate ---------------------------------------------------------


def parse(raw: Mapping[str, Any] | None, *, platform: str | None = None) -> DevinConfig:
    """Build a validated config from the raw settings mapping.

    Raises :class:`ConfigError` with a message meant for a human.
    """
    raw = dict(raw or {})
    platform = platform or sys.platform
    backend = _choice(raw, "backend", "acp", BACKENDS)
    mode = _choice(raw, "permission_mode", "normal", PERMISSION_MODES, PERMISSION_ALIASES)
    cfg = DevinConfig(
        cli_path=_str(raw, "cli_path", ""),
        backend=backend,
        model=_str(raw, "model", ""),
        permission_mode=mode,
        allow_dangerous=_bool(raw, "allow_dangerous", False),
        sandbox=_bool(raw, "sandbox", False),
        trust_workspace=_bool(raw, "trust_workspace", False),
        permission_requests=_choice(raw, "permission_requests", "ask", PERMISSION_REQUEST_POLICIES),
        approval_scope=_choice(raw, "approval_scope", "run", APPROVAL_SCOPES),
        default_cwd=_str(raw, "default_cwd", ""),
        allowed_roots=_str_list(raw, "allowed_roots"),
        default_background=_bool(raw, "default_background", False),
        timeout_seconds=_int(raw, "timeout_seconds", 1800, 10, 86_400),
        max_timeout_seconds=_int(raw, "max_timeout_seconds", 7200, 10, 86_400),
        background_timeout_seconds=_int(raw, "background_timeout_seconds", 3600, 10, 86_400),
        max_concurrent_runs=_int(raw, "max_concurrent_runs", 2, 1, 16),
        max_output_chars=_int(raw, "max_output_chars", 200_000, 1_000, 5_000_000),
        max_result_chars=_int(raw, "max_result_chars", 12_000, 500, 200_000),
        max_progress_events=_int(raw, "max_progress_events", 500, 10, 10_000),
        max_prompt_chars=_int(raw, "max_prompt_chars", 100_000, 100, 1_000_000),
        history_limit=_int(raw, "history_limit", 50, 1, 1_000),
        notify_on_complete=_bool(raw, "notify_on_complete", True),
        preflight_auth_check=_bool(raw, "preflight_auth_check", True),
        cancel_grace_seconds=_float(raw, "cancel_grace_seconds", 5.0, 0.0, 120.0),
        persist_runs=_bool(raw, "persist_runs", True),
        extra_env=_env_map(raw, "extra_env"),
    )
    validate(cfg, platform=platform)
    return cfg


def validate(cfg: DevinConfig, *, platform: str | None = None) -> None:
    platform = platform or sys.platform
    if cfg.permission_mode == "dangerous" and not cfg.allow_dangerous:
        raise ConfigError(
            "permission_mode 'dangerous' auto-approves every action Devin takes, including "
            "destructive shell commands. It is refused unless allow_dangerous: true is also set "
            "in plugins.entries.devin.settings."
        )
    if cfg.permission_mode == "autonomous":
        if not cfg.sandbox:
            raise ConfigError("permission_mode 'autonomous' requires sandbox: true (Devin enforces it with --sandbox)")
        if platform.startswith("win"):
            raise ConfigError(
                "permission_mode 'autonomous' needs Devin's OS sandbox, which native Windows does "
                "not support. Run Hermes and Devin inside WSL 2 to use it."
            )
    if cfg.sandbox and platform.startswith("win"):
        raise ConfigError("sandbox: true is not supported by Devin on native Windows (use WSL 2)")
    if cfg.permission_mode == "plan" and cfg.backend != "acp":
        raise ConfigError("permission_mode 'plan' (read-only) is only available with backend: acp")
    if cfg.timeout_seconds > cfg.max_timeout_seconds:
        raise ConfigError("timeout_seconds cannot exceed max_timeout_seconds")
    if cfg.background_timeout_seconds > cfg.max_timeout_seconds:
        raise ConfigError("background_timeout_seconds cannot exceed max_timeout_seconds")
    if cfg.max_result_chars > cfg.max_output_chars:
        raise ConfigError("max_result_chars cannot exceed max_output_chars")


# --- loading -----------------------------------------------------------------


def _hermes_settings() -> dict[str, Any]:
    """Read ``plugins.entries.devin.settings`` (legacy ``config`` subtree as a fallback)."""
    try:
        from hermes_cli.config import load_config_readonly
    except Exception:
        return {}
    try:
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID)
    if not isinstance(entry, Mapping):
        return {}
    settings = entry.get("settings")
    if isinstance(settings, Mapping):
        return dict(settings)
    legacy = entry.get("config")
    return dict(legacy) if isinstance(legacy, Mapping) else {}


class ConfigSource:
    """Resolves the effective config on demand.

    No caching: settings are read once per tool call, which keeps edits in
    config.yaml (or the Desktop settings form) effective on the next call.
    Tests inject a ``reader``.
    """

    def __init__(self, reader: Callable[[], Mapping[str, Any]] | None = None, platform: str | None = None):
        self._reader = reader or _hermes_settings
        self._platform = platform

    def load(self) -> DevinConfig:
        return parse(self._reader(), platform=self._platform)

    def try_load(self) -> tuple[DevinConfig | None, str | None]:
        try:
            return self.load(), None
        except ConfigError as exc:
            return None, str(exc)


def with_overrides(cfg: DevinConfig, **changes: Any) -> DevinConfig:
    """Return a copy with ``changes`` applied, re-validated. Used by tests."""
    updated = replace(cfg, **changes)
    validate(updated)
    return updated
