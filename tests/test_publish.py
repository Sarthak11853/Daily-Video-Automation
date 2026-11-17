import json

import pytest

from dva.core.errors import DuplicateUpload
from dva.publish.manager import Publisher


class FakeYT:
    def __init__(self, fail=False):
        self.uploads = 0
        self.fail = fail

    def configured(self):
        return True

    def upload(self, path, meta, session=None, on_session=None):
        if not session and on_session:
            on_session("https://upload/session/1")
        self.uploads += 1
        if self.fail:
            raise RuntimeError("network down")
        return {"id": "abc123", "status": {"privacyStatus": "public"}}


class FakeIG:
    def __init__(self):
        self.published = 0

    def configured(self):
        return True

    def refresh_token_if_needed(self):
        pass

    def publishing_quota_left(self):
        return 50

    def create_container(self, caption):
        return "c1", "https://rupload/c1"

    def upload_binary(self, uri, path):
        pass

    def wait_ready(self, cid):
        pass

    def publish_container(self, cid):
        self.published += 1
        return {"id": "ig1", "url": "https://instagram.com/reel/ig1"}


def _video(db, tmp_path, sha="s1"):
    f = tmp_path / f"{sha}.mp4"
    f.write_bytes(b"x")
    meta = {"youtube": {"title": "t", "description": "d", "tags": []}, "instagram": {"caption": "c"}}
    return db.add_video(run_id=1, title="t", status="approved", video_path=str(f), video_sha256=sha,
                        metadata_json=json.dumps(meta))


def test_publish_once_only(cfg, db, tmp_path):
    vid = _video(db, tmp_path)
    yt, ig = FakeYT(), FakeIG()
    pub = Publisher(cfg, db, yt, ig)
    res = pub.publish(vid)
    assert res["youtube"]["status"] == "published" and res["instagram"]["status"] == "published"
    assert db.get_video(vid)["status"] == "published"
    res2 = pub.publish(vid)  # second call must not upload again
    assert yt.uploads == 1 and ig.published == 1
    assert res2["youtube"]["note"] == "already published"


def test_same_file_not_registered_twice(cfg, db, tmp_path):
    import sqlite3
    v1 = _video(db, tmp_path, "same")
    Publisher(cfg, db, FakeYT(), FakeIG()).publish(v1)
    # the identical file (same SHA-256) can never become a second video row -> never a second upload
    with pytest.raises(sqlite3.IntegrityError):
        _video(db, tmp_path, "same")
    assert db.sha_exists_published("same")


def test_crash_state_is_not_blindly_retried(cfg, db, tmp_path):
    vid = _video(db, tmp_path)
    pubrow = db.claim_publication(vid, "youtube")
    db.update_publication(pubrow["id"], status="uploading")  # crashed before session stored
    yt = FakeYT()
    p = Publisher(cfg, db, yt, FakeIG())
    with pytest.raises(DuplicateUpload):
        p._one("youtube", vid, "s1", tmp_path / "s1.mp4", json.loads(db.get_video(vid)["metadata_json"]))
    assert yt.uploads == 0
    assert db.get_publication(vid, "youtube")["status"] == "unknown"


def test_unapproved_video_cannot_publish(cfg, db, tmp_path):
    vid = _video(db, tmp_path)
    db.update_video(vid, status="awaiting_approval")
    with pytest.raises(Exception):
        Publisher(cfg, db, FakeYT(), FakeIG()).publish(vid)


def test_daily_limit(cfg, db, tmp_path):
    cfg.data["publish"]["youtube"]["daily_upload_limit"] = 1
    cfg.data["publish"]["enabled_platforms"] = ["youtube"]
    Publisher(cfg, db, FakeYT(), FakeIG()).publish(_video(db, tmp_path, "a"))
    res = Publisher(cfg, db, FakeYT(), FakeIG()).publish(_video(db, tmp_path, "b"))
    assert "daily upload limit" in res["youtube"]["error"]
