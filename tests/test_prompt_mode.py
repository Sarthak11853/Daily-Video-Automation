"""Prompt mode: prompts go out; you make video + voiceover + captions; it gets published as you made it."""
import json
import os
import subprocess
from pathlib import Path

from conftest import FakeDiscovery, FakeFetcher, FakePublisher, FakeProvider, FakeTelegram

from dva.approval.bot import ApprovalBot
from dva.footage.inbox import scan_inbox
from dva.footage.processor import FootageProcessor, probe
from dva.llm.base import LLMChain
from dva.pipeline import Pipeline

CAPTION = "Octopus ke 3 dil! 🐙\nNature is wild.\n#octopus #facts"


def make_clip(path: Path, seconds: float, size="720x1280", audio=True, rate=30) -> Path:
    args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:a", "aac"]
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


def _sync(bot):
    bot.jobs = type("Sync", (), {"submit": lambda self, fn: fn()})()
    return bot


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
    assert (out / "PROMPTS.md").exists()
    # you do the voiceover: no TTS files are produced by default
    assert not (out / "voiceover.wav").exists() and not (out / "subtitles.srt").exists()
    texts = [m[1] for m in tg.sent if m[0] == "msg"]
    assert any("<pre>" in t for t in texts)
    assert any("VERIFIED FACTS" in t for t in texts) and any("VOICEOVER SCRIPT (easy English" in t for t in texts)
    music = next(t for t in texts if "Background music" in t)
    assert "playful orchestral" in music and "110 BPM" in music and "playful pizzicato" in music
    assert all("Background music (if your tool makes audio): playful orchestral" in c["prompt"]
               for s in pkg["scenes"] for c in s["clips"])
    assert pkg["scenes"][0]["music_cue"] == "gentle build"
    auto = [t for t in texts if "posted for #" in t][-1]
    assert "YouTube tags: tardigrade facts" in auto
    assert "## Background music" in (out / "PROMPTS.md").read_text()
    assert all(len(t) <= 4096 for t in texts)
    last = [m for m in tg.sent if m[0] == "msg"][-1]
    assert last[2]["inline_keyboard"][0][0]["callback_data"] == f"regen:{res['video_id']}"
    assert pipe.run(mode="prompt")["status"] == "skipped"   # one topic per day


def test_your_video_and_caption_published_untouched(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    clip = make_clip(tmp_path / "mine.mp4", 6)
    msg = FootageProcessor(pipe).process(res["video_id"], [clip], caption=f"#{res['video_id']} " + CAPTION)
    assert "Publishing" in msg, msg
    v = db.get_video(res["video_id"])
    assert v["status"] == "published" and pipe._publisher.calls == 1
    qc = json.loads(v["qc_json"])
    assert qc["notes"] == ["uploaded exactly as you made it"] and qc["post_process"] == "none"
    a, b = probe(clip), probe(Path(v["video_path"]))
    assert (a["width"], a["height"], a["vcodec"]) == (b["width"], b["height"], b["vcodec"])   # not re-encoded
    meta = json.loads(v["metadata_json"])
    assert meta["caption_source"] == "yours"
    assert meta["youtube"]["title"] == "Octopus ke 3 dil! 🐙"
    assert meta["instagram"]["caption"] == CAPTION                    # exactly your text (minus the #id tag)
    assert meta["youtube"]["tags"] == ["octopus", "facts"]
    assert "not waiting" in FootageProcessor(pipe).process(res["video_id"], [clip])


def test_video_without_caption_gets_automatic_caption(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    msg = FootageProcessor(pipe).process(res["video_id"], [make_clip(tmp_path / "auto.mp4", 6)])
    v = db.get_video(res["video_id"])
    meta = json.loads(v["metadata_json"])
    assert v["status"] == "published" and "automatic caption" in msg
    assert meta["caption_source"] == "automatic" and meta["youtube"]["tags"][0] == "tardigrade facts"
    assert "Tardigrades are tiny but tough" in meta["instagram"]["caption"]


def test_yours_only_waits_then_text_publishes(cfg, db, tmp_path):
    cfg.data["prompt_mode"]["caption_source"] = "yours_only"
    tg = FakeTelegram()
    pipe, res = _run_prompt(cfg, db, tg)
    bot = _sync(ApprovalBot(pipe))
    clip = make_clip(tmp_path / "tg.mp4", 6)
    bot.handle_message({"message_id": 500, "chat": {"id": 42},
                        "video": {"file_id": str(clip), "file_size": clip.stat().st_size, "file_name": "tg.mp4"}})
    assert db.get_video(res["video_id"])["status"] == "awaiting_caption"
    ask = [m for m in tg.sent if m[0] == "msg" and "send the caption" in m[1]][-1]
    assert ask[2]["inline_keyboard"][0][0]["callback_data"] == f"autocap:{res['video_id']}"
    bot.handle_message({"message_id": 501, "chat": {"id": 42}, "text": CAPTION})
    v = db.get_video(res["video_id"])
    assert v["status"] == "published" and json.loads(v["metadata_json"])["instagram"]["caption"] == CAPTION


def test_suggested_caption_button(cfg, db, tmp_path):
    cfg.data["prompt_mode"]["caption_source"] = "yours_only"
    pipe, res = _run_prompt(cfg, db)
    FootageProcessor(pipe).process(res["video_id"], [make_clip(tmp_path / "a.mp4", 6)])
    bot = _sync(ApprovalBot(pipe))
    bot.handle_callback({"id": "1", "data": f"autocap:{res['video_id']}", "message": {"chat": {"id": 42}, "message_id": 3}})
    v = db.get_video(res["video_id"])
    assert v["status"] == "published" and json.loads(v["metadata_json"])["caption_source"] == "automatic"


def test_caption_sent_before_video(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    bot = _sync(ApprovalBot(pipe))
    bot.handle_message({"message_id": 7, "chat": {"id": 42}, "text": CAPTION})
    assert db.get_video(res["video_id"])["user_caption"] == CAPTION
    FootageProcessor(pipe).process(res["video_id"], [make_clip(tmp_path / "b.mp4", 6)])
    assert db.get_video(res["video_id"])["status"] == "published"


def test_silent_video_refused_by_default(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    msg = FootageProcessor(pipe).process(res["video_id"], [make_clip(tmp_path / "s.mp4", 6, audio=False)], CAPTION)
    assert "no sound" in msg
    assert db.get_video(res["video_id"])["status"] == "awaiting_footage"


def test_non_compliant_file_is_reencoded_not_rejected(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    clip = make_clip(tmp_path / "wide.mp4", 6, size="640x360", rate=15)
    FootageProcessor(pipe).process(res["video_id"], [clip], caption=CAPTION)
    v = db.get_video(res["video_id"])
    qc = json.loads(v["qc_json"])
    assert v["status"] == "published" and any("reframed" in n for n in qc["notes"])
    assert (probe(Path(v["video_path"]))["width"], probe(Path(v["video_path"]))["height"]) == (360, 640)


def test_bad_footage_returns_to_waiting(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    assert "Couldn't use" in FootageProcessor(pipe).process(res["video_id"], [bad], CAPTION)
    assert db.get_video(res["video_id"])["status"] == "awaiting_footage"


def test_inbox_video_with_caption_file(cfg, db, tmp_path):
    pipe, res = _run_prompt(cfg, db)
    inbox = Path(cfg.get("prompt_mode.inbox_dir"))
    inbox.mkdir(parents=True, exist_ok=True)
    make_clip(inbox / f"{res['video_id']}.mp4", 6)
    (inbox / f"{res['video_id']}.txt").write_text(CAPTION, encoding="utf-8")
    msgs = scan_inbox(pipe, settle_file=0, settle_dir=0)
    assert len(msgs) == 1 and "Publishing" in msgs[0], msgs
    v = db.get_video(res["video_id"])
    assert v["status"] == "published" and v["user_caption"] == CAPTION
    assert not (inbox / f"{res['video_id']}.txt").exists()
    make_clip(inbox / "extra.mp4", 2)
    old = os.path.getmtime(inbox / "extra.mp4") - 100
    os.utime(inbox / "extra.mp4", (old, old))
    assert "no video is waiting" in scan_inbox(pipe, settle_file=0)[0]


def test_optional_voiceover_mode_still_works(cfg, db, tmp_path):
    cfg.data["prompt_mode"].update(generate_voiceover=True, post_process="auto", caption_source="generated")
    pipe, res = _run_prompt(cfg, db)
    out = Path(res["prompts"]).parent
    assert (out / "voiceover_with_music.wav").exists() and (out / "subtitles.srt").exists()
    FootageProcessor(pipe).process(res["video_id"], [make_clip(tmp_path / "silent.mp4", 12, audio=False)])
    qc = json.loads(db.get_video(res["video_id"])["qc_json"])
    assert db.get_video(res["video_id"])["status"] == "published" and qc["post_process"] == "voice_and_subtitles"


def test_big_upload_redirect_skip_and_new_prompts(cfg, db, tmp_path):
    tg = FakeTelegram()
    pipe, res = _run_prompt(cfg, db, tg)
    bot = _sync(ApprovalBot(pipe))
    bot.handle_message({"message_id": 501, "chat": {"id": 42},
                        "video": {"file_id": "x", "file_size": 60_000_000, "file_name": "big.mp4"}})
    assert "20 MB" in tg.sent[-1][1]
    db.execute("DELETE FROM kv WHERE key='tts_wps'")
    assert "awaiting_footage" in pipe.decide(res["video_id"], "regen")
    assert db.get_video(res["video_id"])["status"] == "superseded"
    new = db.one("SELECT id FROM videos WHERE status='awaiting_footage'")
    bot.handle_callback({"id": "9", "data": f"reject:{new['id']}", "message": {"chat": {"id": 42}, "message_id": 1}})
    assert db.get_video(new["id"])["status"] == "rejected"
