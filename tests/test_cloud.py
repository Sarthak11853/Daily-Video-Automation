"""Cloud mode: stateless polling passes drive the whole day."""
import json
import shutil

from conftest import FakeDiscovery, FakeFetcher, FakePublisher, FakeProvider, FakeTelegram
from test_prompt_mode import CAPTION, make_clip
from test_publish import FakeIG, FakeYT, _video

from dva.cloud import daily_due, poll
from dva.footage.drive import DriveInbox
from dva.llm.base import LLMChain
from dva.pipeline import Pipeline
from dva.publish.manager import Publisher


class QueueTelegram(FakeTelegram):
    """Telegram fake with an update queue that honours getUpdates offsets."""

    def __init__(self):
        super().__init__()
        self.queue: list[dict] = []
        self.next_id = 1

    def push(self, kind, payload):
        self.queue.append({"update_id": self.next_id, kind: payload})
        self.next_id += 1

    def get_updates(self, offset, timeout=0):
        return [u for u in self.queue if offset is None or u["update_id"] >= offset]


def _pipe(cfg, db, tg):
    cfg.data["channel"]["daily_time"] = "00:00"
    cfg.data["workflow"]["mode"] = "prompt"
    return Pipeline(cfg, db, llm=LLMChain([FakeProvider()]), telegram=tg, discovery=FakeDiscovery(),
                    fetcher=FakeFetcher(), publisher=FakePublisher(db))


def test_a_full_day_in_the_cloud(cfg, db, tmp_path):
    tg = QueueTelegram()
    pipe = _pipe(cfg, db, tg)
    assert poll(pipe, check_only=True)["daily"] is True
    poll(pipe)                                            # 9 AM pass: prompts go out
    vid = db.one("SELECT id, status FROM videos")
    assert vid["status"] == "awaiting_footage" and not daily_due(pipe)
    assert poll(pipe, check_only=True)["any"] is False    # nothing to do -> job skips heavy steps

    clip = make_clip(tmp_path / "phone.mp4", 6)
    tg.push("message", {"message_id": 10, "chat": {"id": 42}, "caption": CAPTION,
                        "video": {"file_id": str(clip), "file_size": clip.stat().st_size, "file_name": "p.mp4"}})
    assert poll(pipe, check_only=True)["telegram"] == 1
    poll(pipe)                                            # next pass: your video is published
    assert db.get_video(vid["id"])["status"] == "published"
    assert db.kv_get("telegram_offset") == 2
    assert poll(pipe, check_only=True)["telegram"] == 0   # update is not handled twice


def test_drive_inbox(cfg, db, tmp_path, monkeypatch):
    tg = QueueTelegram()
    pipe = _pipe(cfg, db, tg)
    poll(pipe)
    vid = db.one("SELECT id FROM videos")["id"]
    clip = make_clip(tmp_path / "big.mp4", 6)
    files = [{"id": "f1", "name": f"{vid}.mp4", "mimeType": "video/mp4"},
             {"id": "f2", "name": f"{vid}.txt", "mimeType": "text/plain"}]
    drive = DriveInbox.__new__(DriveInbox)
    drive.cfg, drive.db, drive.folder = cfg, db, "folder"
    monkeypatch.setattr(DriveInbox, "configured", lambda self: True)
    monkeypatch.setattr(DriveInbox, "list_files", lambda self: files)
    monkeypatch.setattr(DriveInbox, "download", lambda self, fid, dest: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                                        shutil.copy2(clip, dest))[1])
    monkeypatch.setattr(DriveInbox, "caption_for", lambda self, f, fl: CAPTION)
    assert [f["id"] for f in drive.new_videos()] == ["f1"]
    msgs = drive.scan(pipe)
    assert "Publishing" in msgs[0]
    v = db.get_video(vid)
    assert v["status"] == "published" and v["user_caption"] == CAPTION
    assert drive.new_videos() == []                       # same Drive file never processed twice


def test_platform_duplicate_safety_net(cfg, db, tmp_path):
    class YTWithTwin(FakeYT):
        def find_recent(self, title, hours=72):
            return "existing123"

    class IGWithTwin(FakeIG):
        def find_recent(self, caption, limit=15):
            return {"id": "ig-old", "url": "https://instagram.com/p/old"}

    vid = _video(db, tmp_path)
    yt, ig = YTWithTwin(), IGWithTwin()
    res = Publisher(cfg, db, yt, ig).publish(vid)
    assert yt.uploads == 0 and ig.published == 0         # lost-state rerun can't double-post
    assert res["youtube"]["id"] == "existing123" and res["instagram"]["id"] == "ig-old"
    assert json.loads(json.dumps(res))["youtube"]["status"] == "published"
