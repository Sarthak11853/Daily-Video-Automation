"""Any OpenAI-compatible chat API: used for the free backups Groq and GitHub Models.

- Groq (https://console.groq.com): free API key, no card; Llama / GPT-OSS models.
- GitHub Models (https://models.github.ai): free with a GitHub token; inside GitHub Actions the workflow's own
  GITHUB_TOKEN works (permission `models: read`), so it needs no signup at all.

Like the Gemini provider, each request walks a list of models and moves on when one is busy, out of free quota
or rejects the request size.
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


class OpenAICompatProvider(LLMProvider):
    def __init__(self, name: str, cfg: dict, api_key: str | None, db=None, tz: str = "Asia/Kolkata",
                 http: HttpClient | None = None, sleep=time.sleep):
        super().__init__(cfg, db, tz)
        self.name = name
        self.api_key = api_key
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.http = http or HttpClient(timeout=180)
        self.sleep = sleep
        self._bad: dict[str, int] = {}

    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.cfg.get("models"))

    def candidates(self, judge: bool) -> list[str]:
        models = list(self.cfg.get("judge_models") or []) if judge else []
        models += [m for m in self.cfg.get("models") or [] if m not in models]
        return sorted(models, key=lambda m: self._bad.get(m, 0))

    def _post(self, model: str, prompt: str, system: str, json_mode: bool, temperature: float):
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict = {"model": model, "messages": msgs, "temperature": temperature,
                      "max_tokens": int(self.cfg.get("max_output_tokens", 4000))}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"
        resp = self.http.request("POST", url, ok_status=(), json=body, headers=headers)
        if resp.status_code == 400 and json_mode and "response_format" in resp.text:
            body.pop("response_format")          # some models don't support JSON mode: ask in plain text
            msgs[-1]["content"] = prompt + "\n\nReply with valid JSON only, no other text."
            resp = self.http.request("POST", url, ok_status=(), json=body, headers=headers)
        return resp

    def _generate(self, prompt: str, system: str, json_mode: bool, judge: bool, temperature: float) -> str:
        tries = int(self.cfg.get("tries_per_model", 2))
        wait = float(self.cfg.get("retry_wait_seconds", 5))
        errors: list[str] = []
        quota_only = True
        for model in self.candidates(judge):
            for attempt in range(tries):
                try:
                    resp = self._post(model, prompt, system, json_mode, temperature)
                except TransientError as exc:     # 429 / 5xx
                    m = re.search(r"HTTP (\d+)", str(exc))
                    code = m.group(1) if m else "?"
                    errors.append(f"{model}: HTTP {code}")
                    if code == "429":
                        break                     # rate/daily limit for this model: next model
                    quota_only = False
                    if attempt + 1 < tries:
                        self.sleep(wait * (attempt + 1))
                    continue
                except (requests.ConnectionError, requests.Timeout) as exc:
                    quota_only = False
                    errors.append(f"{model}: {type(exc).__name__}")
                    if attempt + 1 < tries:
                        self.sleep(wait)
                    continue
                if resp.status_code == 401:
                    raise ProviderUnavailable(f"{self.name}: API key rejected (HTTP 401)")
                if resp.status_code == 413:       # request bigger than this model's free limits
                    errors.append(f"{model}: HTTP 413 (too large)")
                    break
                if resp.status_code != 200:       # 400/403/404: model unavailable for this key / request
                    quota_only = False
                    errors.append(f"{model}: HTTP {resp.status_code} {resp.text[:120]}")
                    break
                choices = resp.json().get("choices") or []
                text = (choices[0].get("message", {}).get("content") if choices else "") or ""
                if text.strip():
                    if errors:
                        log.info("%s answered with %s after: %s", self.name, model, "; ".join(errors[-6:]))
                    self._bad.pop(model, None)
                    return text
                quota_only = False
                errors.append(f"{model}: empty answer")
                break
            self._bad[model] = self._bad.get(model, 0) + 1
        summary = "; ".join(errors[-8:]) or "no models configured"
        if quota_only and errors:
            raise QuotaExceeded(f"{self.name}: free limits used up ({summary})")
        raise ProviderUnavailable(f"{self.name}: all models failed ({summary})")
