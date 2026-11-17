"""Instagram Graph API (Content Publishing) — Reels via *resumable* upload,
so the video does not need to be hosted on a public URL.

Requires a Professional (Business/Creator) account and a long-lived token
with instagram_business_basic + instagram_business_content_publish
(+ instagram_business_manage_insights for metrics). See docs/SETUP.md.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..core.errors import ConfigError, PublishError
from ..core.http import HttpClient

log = logging.getLogger(__name__)
INSIGHT_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
                   "ig_reels_avg_watch_time"]


class Instagram:
    def __init__(self, cfg, db=None, http: HttpClient | None = None):
        self.cfg = cfg
        self.i = cfg.section("publish.instagram")
        self.db = db
        self.http = http or HttpClient(timeout=120)
        host = "graph.instagram.com" if self.i.get("api", "instagram_login") == "instagram_login" else "graph.facebook.com"
        self.version = self.i.get("api_version", "v23.0")
        self.base = f"https://{host}/{self.version}"
        self.host = host

    def token(self) -> str:
        tok = (self.db.kv_get("instagram_token") if self.db else None) or {}
        value = tok.get("access_token") or self.cfg.secret(self.i.get("token_env", "INSTAGRAM_ACCESS_TOKEN"))
        if not value:
            raise ConfigError("INSTAGRAM_ACCESS_TOKEN not set")
        return value

    def user_id(self) -> str:
        uid = self.cfg.secret(self.i.get("user_id_env", "INSTAGRAM_USER_ID"))
        if not uid:
            raise ConfigError("INSTAGRAM_USER_ID not set")
        return uid

    def configured(self) -> bool:
        try:
            self.token()
            self.user_id()
            return True
        except ConfigError:
            return False

    def refresh_token_if_needed(self, min_days_left: int = 10) -> None:
        """Long-lived Instagram-Login tokens last 60 days and can be refreshed (free)."""
        if self.host != "graph.instagram.com" or not self.db:
            return
        tok = self.db.kv_get("instagram_token") or {}
        if tok.get("expires_at", 0) - time.time() > min_days_left * 86400:
            return
        resp = self.http.get("https://graph.instagram.com/refresh_access_token",
                             params={"grant_type": "ig_refresh_token", "access_token": self.token()}, ok_status=())
        data = resp.json()
        if "access_token" in data:
            self.db.kv_set("instagram_token", {"access_token": data["access_token"],
                                               "expires_at": time.time() + int(data.get("expires_in", 5184000))})
            log.info("Instagram token refreshed")
        else:
            log.warning("Instagram token refresh failed: %s", data.get("error", {}).get("message"))

    def publishing_quota_left(self) -> int | None:
        try:
            resp = self.http.get(f"{self.base}/{self.user_id()}/content_publishing_limit",
                                 params={"fields": "quota_usage,config", "access_token": self.token()}, ok_status=())
            d = (resp.json().get("data") or [{}])[0]
            return int(d.get("config", {}).get("quota_total", 100)) - int(d.get("quota_usage", 0))
        except Exception:  # noqa: BLE001
            return None

    def create_container(self, caption: str) -> tuple[str, str]:
        resp = self.http.post_once(f"{self.base}/{self.user_id()}/media", ok_status=(), data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true" if self.i.get("share_to_feed", True) else "false", "access_token": self.token()})
        data = resp.json()
        if "id" not in data:
            raise PublishError(f"Instagram container failed: {data.get('error', data)}")
        uri = data.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{data['id']}"
        return data["id"], uri

    def upload_binary(self, uri: str, video: Path) -> None:
        size = video.stat().st_size
        with open(video, "rb") as fh:
            resp = self.http.post(uri, data=fh.read(), ok_status=(), headers={
                "Authorization": f"OAuth {self.token()}", "offset": "0", "file_size": str(size)})
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or not data.get("success", True):
            raise PublishError(f"Instagram binary upload failed HTTP {resp.status_code}: {resp.text[:300]}")

    def wait_ready(self, container_id: str) -> None:
        for _ in range(int(self.i.get("status_poll_max", 60))):
            resp = self.http.get(f"{self.base}/{container_id}", params={"fields": "status_code,status",
                                                                        "access_token": self.token()}, ok_status=())
            st = resp.json()
            code = st.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {code}: {st.get('status')}")
            time.sleep(float(self.i.get("status_poll_seconds", 10)))
        raise PublishError("Instagram processing timed out")

    def publish_container(self, container_id: str) -> dict:
        resp = self.http.post_once(f"{self.base}/{self.user_id()}/media_publish", ok_status=(),
                                   data={"creation_id": container_id, "access_token": self.token()})
        data = resp.json()
        if "id" not in data:
            raise PublishError(f"Instagram publish failed: {data.get('error', data)}")
        link = self.http.get(f"{self.base}/{data['id']}", ok_status=(),
                             params={"fields": "permalink", "access_token": self.token()}).json()
        return {"id": data["id"], "url": link.get("permalink")}

    def find_recent(self, caption: str, limit: int = 15) -> dict | None:
        """Safety net against duplicates: a recent post with exactly this caption."""
        resp = self.http.get(f"{self.base}/{self.user_id()}/media", ok_status=(), params={
            "fields": "id,caption,permalink,timestamp", "limit": limit, "access_token": self.token()})
        for m in resp.json().get("data", []) or []:
            if (m.get("caption") or "").strip() == caption.strip():
                return {"id": m["id"], "url": m.get("permalink")}
        return None

    def insights(self, media_id: str) -> dict:
        out: dict = {}
        for metric in INSIGHT_METRICS:  # one by one: unsupported metrics must not break the rest
            resp = self.http.get(f"{self.base}/{media_id}/insights", ok_status=(),
                                 params={"metric": metric, "access_token": self.token()})
            for d in resp.json().get("data", []) or []:
                vals = d.get("values") or [{}]
                out[d["name"]] = vals[0].get("value", d.get("total_value", {}).get("value"))
        return out
