"""Google Gemini (AI Studio free tier) via REST — no SDK dependency.

Resilience: free-tier models are sometimes overloaded (HTTP 503 "high demand") or out of quota (429) for hours.
Each request therefore walks a list of models: the configured one, then `fallback_models`, then every other
suitable Flash model your key can use (asked from Google's model list once per run). A model that keeps
failing is moved to the back for the rest of the run, so later calls go straight to one that works.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from ..core.errors import ProviderUnavailable, QuotaExceeded, TransientError
from ..core.http import HttpClient
from .base import LLMProvider

log = logging.getLogger(__name__)
BASE = "https://generativelanguage.googleapis.com/v1beta"
API = BASE + "/models/{model}:generateContent"
SKIP_WORDS = ("image", "tts", "audio", "live", "embed", "vision", "computer", "robotics", "aqa", "learnlm",
              "native", "thinking", "nano", "8b")


def model_rank(name: str) -> tuple:
    """Sort key: stable before preview/experimental, newer version first, full Flash before Lite."""
    n = name.lower()
    stable = not any(w in n for w in ("preview", "exp"))
    m = re.search(r"(\d+(?:\.\d+)?)", n)
    version = float(m.group(1)) if m else 0.0
    return (stable, version, "lite" not in n)


def pick_models(listing: list[dict]) -> list[str]:
    """From Google's ListModels response, the usable text models, best first (Gemma last, as a final resort)."""
    flash, gemma = [], []
    for m in listing:
        name = str(m.get("name", "")).removeprefix("models/")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        low = name.lower()
        if any(w in low for w in SKIP_WORDS):
            continue
        if low.startswith("gemini") and "flash" in low:
            flash.append(name)
        elif low.startswith("gemma") and ("27b" in low or "12b" in low):
            gemma.append(name)
    flash.sort(key=model_rank, reverse=True)
    gemma.sort(key=model_rank, reverse=True)
    return flash + gemma[:1]


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, cfg: dict, api_key: str | None, db=None, tz: str = "Asia/Kolkata",
                 http: HttpClient | None = None, sleep=time.sleep):
        super().__init__(cfg, db, tz)
        self.api_key = api_key
        self.http = http or HttpClient(timeout=120)
        self.sleep = sleep
        self._discovered: list[str] | None = None
        self._bad: dict[str, int] = {}

    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key or ""}

    def discover(self) -> list[str]:
        if self._discovered is None:
            self._discovered = []
            if self.cfg.get("discover_models", True):
                try:
                    resp = self.http.session.get(f"{BASE}/models", params={"pageSize": 200},
                                                 headers=self._headers(), timeout=30)
                    if resp.status_code == 200:
                        self._discovered = pick_models(resp.json().get("models", []))
                        log.info("Gemini models available: %s", ", ".join(self._discovered[:8]))
                except requests.RequestException as exc:
                    log.warning("could not list Gemini models: %s", exc)
        return self._discovered

    def candidates(self, judge: bool) -> list[str]:
        first = self.cfg.get("judge_model" if judge else "model", "gemini-flash-latest")
        other = self.cfg.get("model" if judge else "judge_model")
        order = [first, *(self.cfg.get("fallback_models") or []), other, *self.discover()]
        seen, out = set(), []
        for m in order:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        # models that already failed in this run go to the back
        return sorted(out, key=lambda m: self._bad.get(m, 0))

    def _body(self, model: str, prompt: str, system: str, json_mode: bool, temperature: float) -> dict:
        gemma = model.lower().startswith("gemma")
        text = f"{system}\n\n{prompt}" if (system and gemma) else prompt
        if json_mode and gemma:
            text += "\n\nReply with valid JSON only, no other text."
        body: dict = {"contents": [{"role": "user", "parts": [{"text": text}]}],
                      "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192}}
        if system and not gemma:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode and not gemma:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return body

    def _generate(self, prompt: str, system: str, json_mode: bool, judge: bool, temperature: float) -> str:
        tries = int(self.cfg.get("tries_per_model", 2))
        wait = float(self.cfg.get("retry_wait_seconds", 6))
        errors: list[str] = []
        quota_only = True
        for model in self.candidates(judge):
            for attempt in range(tries):
                try:
                    resp = self.http.request("POST", API.format(model=model), ok_status=(), headers=self._headers(),
                                             json=self._body(model, prompt, system, json_mode, temperature))
                except TransientError as exc:     # 429 / 5xx
                    code = re.search(r"HTTP (\d+)", str(exc))
                    code = code.group(1) if code else "?"
                    if code != "429":
                        quota_only = False
                    errors.append(f"{model}: HTTP {code}")
                    if code == "429":
                        break                     # this model's free quota is used up: next model
                    if attempt + 1 < tries:
                        self.sleep(wait * (attempt + 1))
                    continue
                except (requests.ConnectionError, requests.Timeout) as exc:
                    quota_only = False
                    errors.append(f"{model}: {type(exc).__name__}")
                    if attempt + 1 < tries:
                        self.sleep(wait)
                    continue
                if resp.status_code in (401, 403):
                    raise ProviderUnavailable(f"Gemini auth failed (HTTP {resp.status_code}) — check GEMINI_API_KEY")
                if resp.status_code in (400, 404):
                    quota_only = False
                    errors.append(f"{model}: HTTP {resp.status_code}")
                    break                         # model unknown/retired or rejects this request: next model
                if resp.status_code != 200:
                    quota_only = False
                    errors.append(f"{model}: HTTP {resp.status_code}")
                    break
                data = resp.json()
                cands = data.get("candidates") or []
                parts = (cands[0].get("content", {}) if cands else {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if text:
                    if errors:
                        log.info("Gemini answered with %s after: %s", model, "; ".join(errors[-6:]))
                    self._bad.pop(model, None)
                    return text
                quota_only = False
                errors.append(f"{model}: empty answer ({cands[0].get('finishReason') if cands else 'no candidates'})")
                break
            self._bad[model] = self._bad.get(model, 0) + 1
        summary = "; ".join(errors[-8:]) or "no models to try"
        if quota_only and errors:
            raise QuotaExceeded(f"gemini: every model is out of free quota today ({summary})")
        raise ProviderUnavailable(f"gemini: all models busy or failing ({summary})")
