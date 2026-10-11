from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import PLUGIN_DIR, load_yaml, sub

config = sub("config")


def test_defaults_are_safe():
    cfg = config.parse({})
    assert cfg.permission_mode == "normal"
    assert cfg.allow_dangerous is False
    assert cfg.backend == "acp"
    assert cfg.max_concurrent_runs == 2
    assert cfg.permission_requests == "ask"


@pytest.mark.parametrize("mode", ["dangerous", "yolo", "bypass", "DANGEROUS"])
def test_dangerous_requires_explicit_opt_in(mode):
    with pytest.raises(config.ConfigError, match="allow_dangerous"):
        config.parse({"permission_mode": mode})


def test_dangerous_with_opt_in():
    cfg = config.parse({"permission_mode": "yolo", "allow_dangerous": True})
    assert cfg.permission_mode == "dangerous"
    assert cfg.is_dangerous


def test_allow_dangerous_must_be_a_real_boolean():
    with pytest.raises(config.ConfigError):
        config.parse({"permission_mode": "dangerous", "allow_dangerous": "please"})


def test_unknown_mode_rejected():
    with pytest.raises(config.ConfigError, match="permission_mode"):
        config.parse({"permission_mode": "godmode"})


def test_alias_auto_is_normal():
    assert config.parse({"permission_mode": "auto"}).permission_mode == "normal"


def test_autonomous_needs_sandbox_and_not_windows():
    with pytest.raises(config.ConfigError, match="sandbox"):
        config.parse({"permission_mode": "autonomous"}, platform="linux")
    assert config.parse({"permission_mode": "autonomous", "sandbox": True}, platform="linux").sandbox
    with pytest.raises(config.ConfigError, match="Windows"):
        config.parse({"permission_mode": "autonomous", "sandbox": True}, platform="win32")


def test_sandbox_refused_on_windows():
    with pytest.raises(config.ConfigError, match="Windows"):
        config.parse({"sandbox": True}, platform="win32")


def test_plan_requires_acp():
    assert config.parse({"permission_mode": "plan"}).permission_mode == "plan"
    with pytest.raises(config.ConfigError, match="acp"):
        config.parse({"permission_mode": "plan", "backend": "print"})


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"timeout_seconds": "soon"}, "timeout_seconds"),
        ({"timeout_seconds": 5}, "between"),
        ({"max_concurrent_runs": 0}, "between"),
        ({"max_concurrent_runs": True}, "whole number"),
        ({"backend": "cloud"}, "backend"),
        ({"timeout_seconds": 9000}, "max_timeout_seconds"),
        ({"max_result_chars": 300_000, "max_output_chars": 200_000}, "max_result_chars"),
        ({"extra_env": {"DEVIN_PERMISSION_MODE": "dangerous"}}, "cannot be set"),
        ({"extra_env": ["A=1"]}, "mapping"),
        ({"cancel_grace_seconds": -1}, "between"),
        ({"allowed_roots": 5}, "list"),
    ],
)
def test_invalid_values(settings, message):
    with pytest.raises(config.ConfigError, match=message):
        config.parse(settings)


def test_string_booleans_accepted():
    assert config.parse({"default_background": "yes"}).default_background is True


def test_config_source_reports_errors():
    src = config.ConfigSource(reader=lambda: {"permission_mode": "dangerous"})
    cfg, err = src.try_load()
    assert cfg is None and "allow_dangerous" in err


def test_manifest_schema_matches_config():
    manifest = load_yaml(PLUGIN_DIR / "plugin.yaml")
    schema = manifest["config_schema"]
    fields = {f.name: f for f in dataclasses.fields(config.DevinConfig)}
    assert set(schema) == set(fields)
    defaults = config.DevinConfig()
    for key, spec in schema.items():
        if "default" in spec:
            expected = getattr(defaults, key)
            if isinstance(expected, tuple):
                expected = list(expected)
            if key == "extra_env":
                expected = dict(expected)
            assert spec["default"] == expected, key
    assert set(schema["permission_mode"]["choices"]) == set(config.PERMISSION_MODES)
    assert set(schema["backend"]["choices"]) == set(config.BACKENDS)


def test_manifest_declares_registered_tools():
    manifest = load_yaml(PLUGIN_DIR / "plugin.yaml")
    assert set(manifest["provides_tools"]) == set(sub("schemas").ALL_TOOLS)
    assert Path(PLUGIN_DIR / "__init__.py").exists()
