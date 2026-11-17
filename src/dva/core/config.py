"""Configuration loading: YAML settings + `.env` secrets + DVA__ env overrides."""
from __future__ import annotations

import copy
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    if not path.exists():
        return
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            log.warning("%s is readable by other users; run: chmod 600 %s", path, path)
    except OSError:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _coerce(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _apply_env_overrides(cfg: dict) -> None:
    for key, value in os.environ.items():
        if not key.startswith("DVA__"):
            continue
        parts = [p.lower() for p in key[5:].split("__") if p]
        node = cfg
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        if parts:
            node[parts[-1]] = _coerce(value)


class Config:
    """Thin wrapper around a nested dict with dotted access and secret lookup."""

    def __init__(self, data: dict, root: Path = PROJECT_ROOT):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None, root: Path | None = None,
             overrides: dict | None = None) -> "Config":
        root = Path(root) if root else PROJECT_ROOT
        _load_dotenv(root / ".env")
        cfg_path = Path(path) if path else (root / "config" / "settings.yaml")
        if not cfg_path.exists():
            cfg_path = DEFAULT_CONFIG
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        _apply_env_overrides(data)
        if overrides:
            deep_merge(data, overrides)
        return cls(data, root)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict:
        val = self.get(dotted, {})
        return copy.deepcopy(val) if isinstance(val, dict) else {}

    def path(self, dotted: str, default: str = "") -> Path:
        p = Path(os.path.expanduser(str(self.get(dotted, default))))
        return p if p.is_absolute() else (self.root / p)

    def secret(self, env_name: str | None) -> str | None:
        """Read a secret from env (.env) or, on macOS, from the Keychain.

        A value of the form `keychain:<service>` is resolved with
        `security find-generic-password -s <service> -w`.
        """
        if not env_name:
            return None
        value = os.environ.get(env_name, "").strip()
        if value.startswith("keychain:"):
            service = value.split(":", 1)[1]
            try:
                out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                     capture_output=True, text=True, timeout=10, check=True)
                return out.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                log.error("Could not read %s from macOS Keychain", service)
                return None
        return value or None

    @property
    def data_dir(self) -> Path:
        p = self.path("paths.data_dir", "data")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def output_dir(self) -> Path:
        p = self.path("paths.output_dir", "output")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def secrets_dir(self) -> Path:
        p = self.path("paths.secrets_dir", "secrets")
        p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
        return p


def deep_merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base
