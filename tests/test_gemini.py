"""Gemini resilience: model fallback on 'high demand' / quota, model discovery, daily retry schedule."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeTelegram
from dva.cloud import daily_due
from dva.core.errors import ProviderUnavailable, QuotaExceeded
from dva.core.http import HttpClient
from dva.llm.gemini import GeminiProvider, pick_models
from dva.pipeline import Pipeline


class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data or {}
        self.text = json.dumps(self._data)
        self.headers = {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, behaviour, listing=None):
        self.behaviour = behaviour          # model -> status code (200 = answers)
        self.listing = listing or []
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        model = url.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        code = self.behaviour.get(model, 404)
        if code == 200:
            return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps({"ok": model})}]}}]})
        return Resp(code, {"error": {"code": code, "message": "This model is currently experiencing high demand"}})

    def get(self, url, **kw):
        return Resp(200, {"models": self.listing})


def provider(behaviour, listing=None, **cfg):
    http = HttpClient()
    http.session = FakeSession(behaviour, listing)
    base = {"model": "gemini-flash-latest", "judge_model": "gemini-flash-lite-latest",
            "fallback_models": ["gemini-2.5-flash"], "daily_request_limit": 0}
    return GeminiProvider({**base, **cfg}, "key", http=http, sleep=lambda s: None), http.session


LISTING = [
    {"name": "models/gemini-3-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
]


def test_pick_models_orders_stable_newest_first_and_skips_non_text():
    assert pick_models(LISTING) == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash",
                                    "gemini-3-flash-preview", "gemma-3-27b-it"]


def test_falls_back_when_latest_model_is_overloaded():
    p, sess = provider({"gemini-flash-latest": 503, "gemini-2.5-flash": 200})
    assert json.loads(p.generate("hi"))["ok"] == "gemini-2.5-flash"
    assert sess.calls == ["gemini-flash-latest", "gemini-flash-latest", "gemini-2.5-flash"]
    sess.calls.clear()
    p.generate("again")                                   # the busy model is now tried last
    assert sess.calls == ["gemini-2.5-flash"]


def test_quota_exhausted_skips_to_next_model_immediately():
    p, sess = provider({"gemini-flash-latest": 429, "gemini-2.5-flash": 200})
    p.generate("hi")
    assert sess.calls == ["gemini-flash-latest", "gemini-2.5-flash"]


def test_discovered_models_used_when_configured_ones_fail():
    p, sess = provider({"gemini-flash-latest": 503, "gemini-2.5-flash": 503, "gemini-flash-lite-latest": 503,
                        "gemini-2.5-flash-lite": 200}, LISTING)
    assert json.loads(p.generate("hi"))["ok"] == "gemini-2.5-flash-lite"


def test_gemma_last_resort_gets_system_in_prompt():
    p, sess = provider({"gemma-3-27b-it": 200}, [LISTING[-1]], fallback_models=[])
    assert json.loads(p.generate("hi", system="be nice"))["ok"] == "gemma-3-27b-it"


def test_errors_when_everything_fails():
    p, _ = provider({"gemini-flash-latest": 503, "gemini-2.5-flash": 503, "gemini-flash-lite-latest": 503})
    with pytest.raises(ProviderUnavailable):
        p.generate("hi")
    p, _ = provider({"gemini-flash-latest": 429, "gemini-2.5-flash": 429, "gemini-flash-lite-latest": 429})
    with pytest.raises(QuotaExceeded):
        p.generate("hi")


def test_daily_retries_are_spaced_and_messages_are_calm(cfg, db):
    cfg.data["channel"]["daily_time"] = "00:00"
    tg = FakeTelegram()
    pipe = Pipeline(cfg, db, telegram=tg)
    day = pipe.today()
    assert daily_due(pipe)
    for i in range(3):
        rid = db.start_run(day, "daily")
        db.update_run(rid, status="failed")
        pipe.notify_failure(rid, "research", ProviderUnavailable("gemini: busy"), "busy")
    texts = [m[1] for m in tg.sent]
    assert len(texts) == 1 and "delayed" in texts[0]          # one calm note, silent retries
    assert not daily_due(pipe)                                # just failed: wait before the next try
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds")
    db.execute("UPDATE runs SET started_at=?", (old,))
    assert daily_due(pipe)
    cfg.data["cloud"]["daily_attempts"] = 4
    rid = db.start_run(day, "daily")
    pipe.notify_failure(rid, "research", ProviderUnavailable("gemini: busy"), "busy")
    assert "after 4 tries" in tg.sent[-1][1]                  # final alert
    assert not daily_due(pipe)
