"""Prompt mode: prompts + voiceover go out, your footage comes back, gets finished and published."""
import json
import os
import subprocess
from pathlib import Path

from conftest import FakeDiscovery, FakeFetcher, FakePublisher, FakeProvider, FakeTelegram

from dva.approval.bot import ApprovalBot
from dva.footage.inbox import scan_inbox
from dva.footage.processor import FootageProcessor
from dva.llm.base import LLMChain
from dva.pipeline import Pipeline


def make_clip(path: Path, seconds: float, size="640x360", audio=False) -> Path:
    args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


def _pipe(cfg, db, tg=None):
    return Pipeline(cfg, db, llm=LLMChain([FakeProvider()]), telegram=tg or FakeTelegram(),
                    discovery=FakeDiscovery(), fetcher=FakeFetcher(), publisher=FakePublisher(db))


def _run_prompt(cfg, db, tg=None):
    pipe = _pipe(cfg, db, tg)
    res = pipe.run(mode="prompt")
    assert res["status"] == "awaiting_footage", res
    return pipe, res


def test_prompt_package_sent(cfg, db):
    tg = FakeTelegram()
    pipe, res = _run_prompt(cfg, db, tg)
    out = Path(res["prompts"]).parent
    pkg = json.loads((out / "prompts.json").read_text())
    script = json.loads((out / "script.json").read_text())
    assert pkg["source"] == "llm" and len(pkg["scenes"]) == len(script["scenes"])
    for s in pkg["scenes"]:
        assert all(c["seconds"] <= cfg.get("prompt_mode.target_clip_seconds") + 0.01 for c in s["clips"])
        assert all(cfg.get("prompt_mode.style_bible").split()[0] in c["prompt"] for c in s["clips"])
    for f in ("PROMPTS.md", "voiceover.wav", "voiceover_with_music.wav", "subtitles.srt", "timeline.json"):
        assert (out / f).exists(), f
    html = [m for m in tg.sent if m[0] == "msg" and m[3] == "HTML"]
    assert any("<pre>" in m[1] for m in html)
    assert all(len(m[1]) <= 4096 for m in tg.sent if m[0] == "msg")
    docs = [m[1] for m in tg.sent if m[0] == "doc"]
    assert any(d.endswith("PROMPTS.md") for d in docs) and any(d.endswith("voiceover_with_music.wav") for d in docs)
    last = [m for m in tg.sent if m[0] == "msg"][-1]
    assert last[2]["inline_keyboard"][0][0]["callback_data"] == f"regen:{res['video_id']}"
    assert db.get_video(res["video_id"])["status"] == "awaiting_footage"
    # same day again -> skipped
    assert pipe.run(mode="prompt")["status"] == "skipped"


def test_silent_footage_gets_voice_and_is_published(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    clip = make_clip(tmp_path / "mine.mp4", 12)
    msg = FootageProcessor(pipe).process(res["video_id"], [clip])
    assert "Publishing" in msg, msg
    v = db.get_video(res["video_id"])
    assert v["status"] == "published" and pipe._publisher.calls == 1
    qc = json.loads(v["qc_json"])
    assert qc["post_process"] == "voice_and_subtitles" and qc["passed"]
    tl = json.loads((Path(res["prompts"]).parent / "timeline.json").read_text())
    assert abs(qc["duration"] - tl["total"]) < 0.5          # video looped/stretched to the voiceover
    assert any("reframed" in n for n in qc["notes"])          # 16:9 -> 9:16
    # a second submission for the same video is refused
    assert "not waiting" in FootageProcessor(pipe).process(res["video_id"], [clip])


def test_footage_with_sound_is_kept(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    clip = make_clip(tmp_path / "voiced.mp4", 6, size="360x640", audio=True)
    FootageProcessor(pipe).process(res["video_id"], [clip])
    qc = json.loads(db.get_video(res["video_id"])["qc_json"])
    assert qc["post_process"] == "none" and abs(qc["duration"] - 6) < 0.3


def test_bad_footage_returns_to_waiting(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    msg = FootageProcessor(pipe).process(res["video_id"], [bad])
    assert "Couldn't use" in msg
    assert db.get_video(res["video_id"])["status"] == "awaiting_footage"


def test_inbox_folder_of_clips(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    inbox = Path(cfg.get("prompt_mode.inbox_dir"))
    folder = inbox / f"clips_{res['video_id']}"
    folder.mkdir(parents=True)
    make_clip(folder / "01.mp4", 5)
    make_clip(folder / "02.mp4", 5)
    msgs = scan_inbox(pipe, settle_file=0, settle_dir=0)
    assert len(msgs) == 1 and "Publishing" in msgs[0], msgs
    assert not folder.exists() and any((inbox / "processed").iterdir())
    assert db.get_video(res["video_id"])["status"] == "published"
    # nothing waiting -> next file goes to unmatched
    make_clip(inbox / "extra.mp4", 2)
    old = os.path.getmtime(inbox / "extra.mp4") - 100
    os.utime(inbox / "extra.mp4", (old, old))
    assert "no video is waiting" in scan_inbox(pipe, settle_file=0)[0]


def test_telegram_upload_and_skip(cfg, db, tmp_path):
    tg = FakeTelegram()
    pipe, res = _run_prompt(cfg, db, tg)
    bot = ApprovalBot(pipe)
    bot.jobs = type("Sync", (), {"submit": lambda self, fn: fn()})()
    clip = make_clip(tmp_path / "tg.mp4", 8)
    bot.handle_message({"message_id": 500, "chat": {"id": 42}, "caption": f"#{res['video_id']}",
                        "video": {"file_id": str(clip), "file_size": clip.stat().st_size, "file_name": "tg.mp4"}})
    assert db.get_video(res["video_id"])["status"] == "published"
    assert any("Publishing" in m[1] for m in tg.sent if m[0] == "msg")

    # next topic: too-big upload is redirected to the inbox, then Skip drops the topic
    pipe2, res2 = Pipeline(cfg, db, llm=LLMChain([FakeProvider()]), telegram=tg, discovery=FakeDiscovery(),
                           fetcher=FakeFetcher(), publisher=FakePublisher(db)), None
    db.execute("DELETE FROM topics")
    db.execute("DELETE FROM kv WHERE key='tts_wps'")  # the fake script has a fixed length
    res2 = pipe2.run(mode="prompt", force=True)
    bot.handle_message({"message_id": 501, "chat": {"id": 42},
                        "video": {"file_id": "x", "file_size": 60_000_000, "file_name": "big.mp4"}})
    assert "20 MB" in tg.sent[-1][1]
    bot.handle_callback({"id": "9", "data": f"reject:{res2['video_id']}", "message": {"chat": {"id": 42}, "message_id": 1}})
    assert db.get_video(res2["video_id"])["status"] == "rejected"


def test_new_prompts_button(cfg, db):
    pipe, res = _run_prompt(cfg, db)
    db.execute("DELETE FROM kv WHERE key='tts_wps'")  # the fake script has a fixed length
    msg = pipe.decide(res["video_id"], "regen")
    assert "awaiting_footage" in msg, msg
    assert db.get_video(res["video_id"])["status"] == "superseded"
    new = db.one("SELECT id, status FROM videos WHERE id<>?", (res["video_id"],))
    assert new["status"] == "awaiting_footage"
