"""Google Gemini (AI Studio free tier) via REST — no SDK dependency."""
from __future__ import annotations

import logging

import requests

from ..core.errors import ProviderUnavailable, QuotaExceeded, TransientError
from ..core.http import HttpClient
from .base import LLMProvider

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, cfg: dict, api_key: str | None, db=None, tz: str = "Asia/Kolkata",
                 http: HttpClient | None = None):
        super().__init__(cfg, db, tz)
        self.api_key = api_key
        self.http = http or HttpClient(timeout=120)

    def available(self) -> bool:
        return bool(self.api_key)

    def _generate(self, prompt: str, system: str, json_mode: bool, judge: bool, temperature: float) -> str:
        model = self.cfg.get("judge_model" if judge else "model", "gemini-flash-latest")
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        try:
            resp = self.http.post(API.format(model=model), json=body,
                                  headers={"x-goog-api-key": self.api_key or ""}, ok_status=())
        except TransientError as exc:
            if "429" in str(exc):
                raise QuotaExceeded(f"gemini rate/quota limit: {exc}") from exc
            raise
        if resp.status_code in (401, 403):
            raise ProviderUnavailable(f"Gemini auth failed (HTTP {resp.status_code}) — check GEMINI_API_KEY")
        if resp.status_code == 404:
            raise ProviderUnavailable(f"Gemini model '{model}' not found — update llm.gemini.model")
        if resp.status_code != 200:
            raise requests.HTTPError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            raise ValueError(f"Gemini returned no candidates: {data.get('promptFeedback')}")
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise ValueError(f"Gemini empty response (finishReason={cands[0].get('finishReason')})")
        return text
