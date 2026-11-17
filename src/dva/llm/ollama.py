"""Local Ollama provider — free, offline fallback that runs on the Mac."""
from __future__ import annotations

import requests

from ..core.errors import ProviderUnavailable
from ..core.http import HttpClient
from .base import LLMProvider


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, cfg: dict, db=None, tz: str = "Asia/Kolkata", http: HttpClient | None = None):
        super().__init__(cfg, db, tz)
        self.base = cfg.get("base_url", "http://localhost:11434").rstrip("/")
        self.http = http or HttpClient(timeout=600)
        self._ok: bool | None = None

    def available(self) -> bool:
        if self._ok is None:
            try:
                self._ok = self.http.session.get(self.base + "/api/tags", timeout=3).status_code == 200
            except requests.RequestException:
                self._ok = False
        return self._ok

    def _generate(self, prompt: str, system: str, json_mode: bool, judge: bool, temperature: float) -> str:
        model = self.cfg.get("judge_model" if judge else "model", "qwen2.5:7b")
        body = {"model": model, "prompt": prompt, "system": system, "stream": False,
                "options": {"temperature": temperature, "num_ctx": 16384}}
        if json_mode:
            body["format"] = "json"
        try:
            resp = self.http.post(self.base + "/api/generate", json=body)
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"ollama: {exc}") from exc
        return resp.json().get("response", "")
