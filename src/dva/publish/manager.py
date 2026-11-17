"""Publishing orchestration with duplicate-upload prevention.

Each (video, platform) has exactly one `publications` row (UNIQUE). State is
written *before* every remote side-effect, so a crash can never cause a blind
second upload: an interrupted YouTube upload is resumed via its stored
session URI, an Instagram container is re-polled, and anything ambiguous is
marked `unknown` and escalated to you instead of retried.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core.db import Database, utcnow
from ..core.errors import ConfigError, DuplicateUpload, PublishError
from .instagram import Instagram
from .youtube import YouTube

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, cfg, db: Database, youtube: YouTube | None = None, instagram: Instagram | None = None):
        self.cfg = cfg
        self.db = db
        self.yt = youtube or YouTube(cfg)
        self.ig = instagram or Instagram(cfg, db)

    def publish(self, video_id: int) -> dict[str, dict]:
        v = self.db.get_video(video_id)
        if not v:
            raise PublishError(f"video {video_id} not found")
        if v["status"] not in ("approved", "publishing", "published"):
            raise PublishError(f"video {video_id} is '{v['status']}', not approved")
        self.db.update_video(video_id, status="publishing")
        meta = json.loads(v["metadata_json"])
        path = Path(v["video_path"])
        results: dict[str, dict] = {}
        for platform in self.cfg.get("publish.enabled_platforms", ["youtube", "instagram"]):
            try:
                results[platform] = self._one(platform, video_id, v["video_sha256"], path, meta)
            except (ConfigError, DuplicateUpload, PublishError) as exc:
                log.error("%s publish failed: %s", platform, exc)
                results[platform] = {"status": "failed", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("%s publish crashed", platform)
                results[platform] = {"status": "failed", "error": str(exc)}
        ok = all(r.get("status") == "published" for r in results.values()) and results
        self.db.update_video(video_id, status="published" if ok else "approved")
        return results

    def _one(self, platform: str, video_id: int, sha: str, path: Path, meta: dict) -> dict:
        pub = self.db.claim_publication(video_id, platform)
        if pub["status"] == "published":
            return {"status": "published", "url": pub["url"], "id": pub["external_id"], "note": "already published"}
        if pub["status"] == "unknown":
            raise DuplicateUpload(f"{platform}: previous attempt ended in an unknown state; check the channel "
                                  f"manually, then run `dva mark-published` or `dva reset-publication`")
        others = self.db.one("SELECT p.id FROM publications p JOIN videos v ON v.id=p.video_id "
                             "WHERE v.video_sha256=? AND p.platform=? AND p.status='published' AND p.video_id<>?",
                             (sha, platform, video_id))
        if others:
            raise DuplicateUpload(f"{platform}: identical video file already published")
        limit = int(self.cfg.get(f"publish.{platform}.daily_upload_limit", 3))
        if self.db.published_count_today(platform) >= limit:
            raise PublishError(f"{platform}: daily upload limit ({limit}) reached")
        self.db.update_publication(pub["id"], attempts=pub["attempts"] + 1, error=None)
        if platform == "youtube":
            return self._youtube(pub, path, meta["youtube"])
        if platform == "instagram":
            return self._instagram(pub, path, meta["instagram"])
        raise PublishError(f"unknown platform {platform}")

    def _youtube(self, pub, path: Path, meta: dict) -> dict:
        if not self.yt.configured():
            raise ConfigError("YouTube not configured (secrets/youtube_client_secret.json + `dva auth-youtube`)")
        if pub["status"] == "uploading" and not pub["upload_session"]:
            self.db.update_publication(pub["id"], status="unknown")
            raise DuplicateUpload("youtube: crashed before the upload session was stored")
        finder = getattr(self.yt, "find_recent", None)
        if finder and not pub["upload_session"]:
            try:
                existing = finder(meta["title"])
            except Exception as exc:  # noqa: BLE001 — the check is best-effort
                log.warning("YouTube duplicate check failed: %s", exc)
                existing = None
            if existing:
                url = f"https://www.youtube.com/shorts/{existing}"
                self.db.update_publication(pub["id"], status="published", external_id=existing, url=url,
                                           published_at=utcnow())
                return {"status": "published", "id": existing, "url": url, "note": "already on the channel"}
        self.db.update_publication(pub["id"], status="uploading")
        try:
            res = self.yt.upload(path, meta, session=pub["upload_session"],
                                 on_session=lambda s: self.db.update_publication(pub["id"], upload_session=s))
        except PublishError as exc:
            if "expired" in str(exc):
                self.db.update_publication(pub["id"], status="unknown", error=str(exc))
            else:
                self.db.update_publication(pub["id"], status="failed", error=str(exc), upload_session=None)
            raise
        vid = res["id"]
        url = f"https://www.youtube.com/shorts/{vid}"
        self.db.update_publication(pub["id"], status="published", external_id=vid, url=url, published_at=utcnow())
        return {"status": "published", "id": vid, "url": url,
                "privacy": res.get("status", {}).get("privacyStatus")}

    def _instagram(self, pub, path: Path, meta: dict) -> dict:
        if not self.ig.configured():
            raise ConfigError("Instagram not configured (INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID)")
        self.ig.refresh_token_if_needed()
        left = self.ig.publishing_quota_left()
        if left is not None and left <= 0:
            raise PublishError("instagram: API publishing limit reached for the last 24h")
        container = pub["upload_session"]
        if pub["status"] == "publishing_container":
            self.db.update_publication(pub["id"], status="unknown")
            raise DuplicateUpload("instagram: crashed during media_publish; verify on the profile")
        finder = getattr(self.ig, "find_recent", None)
        if finder and pub["status"] in ("failed", "pending"):
            try:
                existing = finder(meta["caption"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Instagram duplicate check failed: %s", exc)
                existing = None
            if existing:
                self.db.update_publication(pub["id"], status="published", external_id=existing["id"],
                                           url=existing.get("url"), published_at=utcnow())
                return {"status": "published", **existing, "note": "already on the profile"}
        if not container or pub["status"] in ("failed", "pending"):
            container, uri = self.ig.create_container(meta["caption"])
            self.db.update_publication(pub["id"], status="uploading", upload_session=container)
            self.ig.upload_binary(uri, path)
            self.db.update_publication(pub["id"], status="processing")
        try:
            self.ig.wait_ready(container)
        except PublishError as exc:
            self.db.update_publication(pub["id"], status="failed", error=str(exc), upload_session=None)
            raise
        self.db.update_publication(pub["id"], status="publishing_container")
        res = self.ig.publish_container(container)
        self.db.update_publication(pub["id"], status="published", external_id=res["id"], url=res.get("url"),
                                   published_at=utcnow())
        return {"status": "published", **res}
