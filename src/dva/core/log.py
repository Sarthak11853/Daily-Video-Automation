"""Logging setup: console + rotating file in data/logs."""
from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(key=)[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(access_token=)[A-Za-z0-9_\-.|]{10,}"),
    re.compile(r"(bot)\d{6,}:[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(Bearer )[A-Za-z0-9_\-.]{10,}"),
]


class RedactingFilter(logging.Filter):
    """Strip API keys/tokens from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = msg
        for pat in _SECRET_PATTERNS:
            red = pat.sub(r"\1***", red)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1***", text)
    return text


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_dva_configured", False):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactingFilter())
    root.addHandler(console)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "dva.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)
    for noisy in ("urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._dva_configured = True  # type: ignore[attr-defined]
