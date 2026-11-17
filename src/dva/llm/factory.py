from __future__ import annotations

from ..core.config import Config
from ..core.db import Database
from .base import LLMChain, LLMProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider


def build_llm(cfg: Config, db: Database) -> LLMChain:
    tz = cfg.get("channel.timezone", "Asia/Kolkata")
    providers: list[LLMProvider] = []
    for name in cfg.get("llm.provider_order", ["gemini", "ollama"]):
        section = cfg.section(f"llm.{name}")
        if name == "gemini":
            providers.append(GeminiProvider(section, cfg.secret(section.get("api_key_env", "GEMINI_API_KEY")), db, tz))
        elif name == "ollama":
            providers.append(OllamaProvider(section, db, tz))
    return LLMChain(providers)
