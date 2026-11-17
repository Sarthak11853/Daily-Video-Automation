"""Full pipeline, offline: fake LLM + sources, tone TTS, fallback renderer,
real FFmpeg editing and QC, fake Telegram + publisher."""
import json
from pathlib import Path

from conftest import FakeDiscovery, FakeFetcher, FakePublisher, FakeProvider, FakeTelegram

from dva.approval.bot import ApprovalBot
from dva.llm.base import LLMChain
from dva.pipeline import Pipeline


def _pipe(cfg, db, tg=None, script=None):
    return Pipeline(cfg, db, llm=LLMChain([FakeProvider(script)]), telegram=tg or FakeTelegram(),
                    discovery=FakeDiscovery(), fetcher=FakeFetcher(), publisher=FakePublisher(db))


def test_daily_run_to_publish(cfg, db):
    tg = FakeTelegram()
    pipe = _pipe(cfg, db, tg)
    res = pipe.run()
    assert res["status"] == "awaiting_approval", res
    out = Path(res["video"]).parent
    for f in ("final.mp4", "script.json", "facts.json", "metadata.json", "qc_report.json", "subtitles.srt",
              "thumbnail.jpg"):
        assert (out / f).exists(), f
    qc = json.loads((out / "qc_report.json").read_text())
    assert qc["passed"], qc
    kinds = [s[0] for s in tg.sent]
    assert "video" in kinds and "msg" in kinds
    video_msg = next(s for s in tg.sent if s[0] == "video")
    assert video_msg[3]["inline_keyboard"][0][0]["callback_data"] == f"approve:{res['video_id']}"

    # same day again -> skipped (no duplicate daily video)
    assert pipe.run()["status"] == "skipped"

    # topic recorded -> not selected again
    assert db.one("SELECT COUNT(*) n FROM topics")["n"] == 1

    # approve via the bot callback; second click is ignored
    bot = ApprovalBot(pipe)
    bot.jobs = type("Sync", (), {"submit": lambda self, fn: fn()})()
    cq = {"id": "1", "data": f"approve:{res['video_id']}", "message": {"chat": {"id": 42}, "message_id": 99}}
    bot.handle_callback(cq)
    bot.handle_callback(cq)
    assert pipe._publisher.calls == 1
    assert db.get_video(res["video_id"])["status"] == "published"

    # unauthorised chat cannot approve
    cq2 = {"id": "2", "data": f"approve:{res['video_id']}", "message": {"chat": {"id": 7}, "message_id": 1}}
    bot.handle_callback(cq2)
    assert ("answer", "Not authorised") in tg.sent


def test_failure_produces_package(cfg, db):
    cfg.data["video"]["max_duration"] = 5  # force QC/duration failure after script stage
    tg = FakeTelegram()
    pipe = _pipe(cfg, db, tg)
    res = pipe.run()
    assert res["status"] == "package"
    assert pipe.llm.providers[0].calls.count("script") >= 2  # automatic rewrite for length was attempted
    assert db.kv_get("tts_wps") > 0
    assert Path(res["package"]).exists()
    assert any(s[0] == "doc" for s in tg.sent)


def test_reject_blacklists_topic(cfg, db):
    pipe = _pipe(cfg, db)
    res = pipe.run()
    msg = pipe.decide(res["video_id"], "reject")
    assert "rejected" in msg
    assert db.one("SELECT status FROM topics")["status"] == "blacklisted"
