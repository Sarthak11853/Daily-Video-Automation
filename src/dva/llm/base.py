"""LLM provider interface + JSON helpers + quota-aware provider chain."""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

from ..core.db import Database
from ..core.errors import ProviderUnavailable, QuotaExceeded, TransientError

log = logging.getLogger(__name__)


class LLMProvider(ABC):
    name = "base"

    def __init__(self, cfg: dict, db: Database | None = None, tz: str = "Asia/Kolkata"):
        self.cfg = cfg
        self.db = db
        self.tz = tz
        self._last_call = 0.0

    # -- quota ----------------------------------------------------------
    def _day(self) -> str:
        return datetime.now(ZoneInfo(self.tz)).date().isoformat()

    def check_quota(self) -> None:
        limit = int(self.cfg.get("daily_request_limit", 0) or 0)
        if self.db and limit:
            used = self.db.quota_used(f"llm:{self.name}", self._day())
            if used >= limit:
                raise QuotaExceeded(f"{self.name}: daily request limit {limit} reached")

    def record_call(self) -> None:
        if self.db:
            self.db.quota_add(f"llm:{self.name}", self._day())

    def throttle(self) -> None:
        gap = float(self.cfg.get("min_seconds_between_calls", 0) or 0)
        wait = self._last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # -- api --------------------------------------------------------------
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def _generate(self, prompt: str, system: str, json_mode: bool, judge: bool, temperature: float) -> str: ...

    def generate(self, prompt: str, system: str = "", json_mode: bool = True, judge: bool = False,
                 temperature: float = 0.7) -> str:
        self.check_quota()
        self.throttle()
        self.record_call()
        return self._generate(prompt, system, json_mode, judge, temperature)


def parse_json(text: str):
    """Parse JSON from a model reply, tolerating ```json fences and trailing prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Model did not return valid JSON: {text[:300]}")


class LLMChain:
    """Tries providers in order; falls back on quota/unavailability/transient errors."""

    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers

    def available_providers(self) -> list[LLMProvider]:
        return [p for p in self.providers if p.available()]

    def json(self, prompt: str, system: str = "", judge: bool = False, temperature: float = 0.7,
             attempts: int = 2):
        errors = []
        for p in self.providers:
            if not p.available():
                errors.append(f"{p.name}: unavailable")
                continue
            for i in range(attempts):
                try:
                    raw = p.generate(prompt, system=system, json_mode=True, judge=judge, temperature=temperature)
                    return parse_json(raw)
                except QuotaExceeded as exc:
                    errors.append(str(exc))
                    break
                except (TransientError, ProviderUnavailable) as exc:
                    errors.append(f"{p.name}: {exc}")
                    break
                except ValueError as exc:  # bad JSON — ask again once
                    errors.append(f"{p.name}: {exc}")
                    log.warning("Invalid JSON from %s (attempt %d)", p.name, i + 1)
        raise ProviderUnavailable("No LLM provider could answer: " + " | ".join(errors))
