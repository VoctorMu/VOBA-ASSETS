"""Configuration loading. Values in config.yaml override the defaults in config.example.yaml."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = yaml.safe_load(EXAMPLE.read_text())
    if path is None and (ROOT / "config.yaml").exists():
        path = ROOT / "config.yaml"
    if path is not None:
        cfg = _merge(cfg, yaml.safe_load(Path(path).read_text()) or {})
    if cfg["execution"] not in ("paper", "mt5"):
        raise ValueError("execution must be 'paper' or 'mt5'")
    if cfg["data_source"] not in ("synthetic", "mt5"):
        raise ValueError("data_source must be 'synthetic' or 'mt5'")
    if cfg["execution"] == "mt5" and cfg["data_source"] != "mt5":
        raise ValueError("execution: mt5 requires data_source: mt5")
    host = cfg["dashboard"]["host"]
    if host not in ("127.0.0.1", "localhost") and not cfg["dashboard"]["token"]:
        raise ValueError("Set dashboard.token before exposing the dashboard on " + host)
    return cfg
