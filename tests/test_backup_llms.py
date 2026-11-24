"""Backup AI services (Groq, GitHub Models) and size-aware prompts."""
import json

import pytest

from conftest import FakeFetcher, FakeProvider
from dva.core.errors import ProviderUnavailable
from dva.core.http import HttpClient
from dva.core.models import Candidate
from dva.llm.base import LLMChain
from dva.llm.openai_compat import OpenAICompatProvider
from dva.research.verifier import FactVerifier


class Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._data = code, data or {}
        self.text = text or json.dumps(self._data)
        self.headers = {}

    def json(self):
        return self._data


class Session:
    def __init__(self, behaviour):
        self.behaviour, self.calls, self.headers = behaviour, [], {}

    def request(self, method, url, json=None, headers=None, **kw):
        model = json["model"]
        self.calls.append((model, "response_format" in json))
        code = self.behaviour.get(model, 404)
        if code == "no-json-mode":
            if "response_format" in json:
                return Resp(400, text='{"error": "response_format not supported"}')
            code = 200
        if code == 200:
            return Resp(200, {"choices": [{"message": {"content": '{"ok": "%s"}' % model}}]})
        return Resp(code, {"error": "x"})


def backup(behaviour, **cfg):
    http = HttpClient()
    http.session = Session(behaviour)
    base = {"base_url": "https://api.example/v1", "models": ["big", "small"], "daily_request_limit": 0}
    return OpenAICompatProvider("groq", {**base, **cfg}, "key", http=http, sleep=lambda s: None), http.session


def test_backup_model_fallback_and_json_mode_retry():
    p, s = backup({"big": 429, "small": "no-json-mode"})
    assert json.loads(p.generate("give json"))["ok"] == "small"
    assert s.calls == [("big", True), ("small", True), ("small", False)]


def test_backup_not_set_up_without_key():
    p, _ = backup({})
    p.api_key = None
    assert not p.available()


def test_chain_uses_backup_when_gemini_fails():
    class Down(FakeProvider):
        name = "gemini"

        def _generate(self, *a, **k):
            raise ProviderUnavailable("gemini: all models busy")
    p, _ = backup({"big": 200})
    chain = LLMChain([Down(), p])
    assert chain.json("hello")["ok"] == "big"


def test_long_prompts_are_trimmed_for_small_providers_but_quotes_still_checked(cfg):
    seen = []

    class Small(FakeProvider):
        name = "small"

        def _generate(self, prompt, system, json_mode, judge, temperature):
            seen.append(len(prompt) + len(system))
            return super()._generate(prompt, system, json_mode, judge, temperature)

    small = Small()
    small.cfg["max_input_chars"] = 3000
    sources = FakeFetcher().gather("x")
    sources[0].text = sources[0].text + " filler sentence." * 2000      # a very long page
    sheet = FactVerifier(cfg, LLMChain([small])).build(Candidate(title="Tardigrade", source="evergreen"), sources)
    assert seen[0] <= 3000                                               # the research prompt fit the limit
    assert {c.id for c in sheet.verified_claims} == {"C1", "C2", "C3", "C4", "C5"}


def test_prompt_too_long_skips_provider():
    class Tiny(FakeProvider):
        name = "tiny"
    t = Tiny()
    t.cfg["max_input_chars"] = 10
    with pytest.raises(ProviderUnavailable, match="too long"):
        LLMChain([t]).json("x" * 100)
