"""YouTube Data API v3: OAuth (installed-app loopback flow), resumable upload,
statistics. Implemented with plain `requests` (no Google SDK needed).

Quota (free, 10,000 units/day by default): upload ≈ 1,600 units, stats = 1 unit.
NOTE: videos uploaded by an *unverified* API project are locked to private
until the project passes Google's (free) API audit — see docs/SETUP.md.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from ..core.errors import ConfigError, PublishError, TransientError
from ..core.http import HttpClient

log = logging.getLogger(__name__)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]


def _write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class YouTube:
    def __init__(self, cfg, http: HttpClient | None = None):
        self.cfg = cfg
        y = cfg.section("publish.youtube")
        self.y = y
        self.secret_file = cfg.path("publish.youtube.client_secret_file", "secrets/youtube_client_secret.json")
        self.token_file = cfg.path("publish.youtube.token_file", "secrets/youtube_token.json")
        self.http = http or HttpClient(timeout=120)

    # ------------------------------------------------------------ oauth
    def _client(self) -> dict:
        if not self.secret_file.exists():
            raise ConfigError(f"YouTube client secret not found at {self.secret_file}")
        data = json.loads(self.secret_file.read_text())
        return data.get("installed") or data.get("web") or data

    def configured(self) -> bool:
        return self.secret_file.exists() and self.token_file.exists()

    def authorize(self, port: int = 8765) -> None:
        """One-time interactive consent. Opens the browser; listens on localhost."""
        client = self._client()
        state = secrets.token_urlsafe(16)
        redirect = f"http://127.0.0.1:{port}/"
        params = {"client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
                  "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state}
        url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
        result: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"YouTube authorised. You can close this tab.")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
        th = threading.Thread(target=srv.handle_request, daemon=True)
        th.start()
        print(f"Open this URL to authorise YouTube uploads:\n{url}\n")
        webbrowser.open(url)
        th.join(timeout=300)
        srv.server_close()
        if result.get("state") != state or "code" not in result:
            raise ConfigError(f"OAuth failed: {result.get('error', 'no code received')}")
        tok = requests.post(TOKEN_URL, data={"code": result["code"], "client_id": client["client_id"],
                                             "client_secret": client["client_secret"], "redirect_uri": redirect,
                                             "grant_type": "authorization_code"}, timeout=30).json()
        if "refresh_token" not in tok:
            raise ConfigError(f"No refresh token returned: {tok}")
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        _write_secret(self.token_file, tok)
        print(f"Saved token to {self.token_file}")

    def access_token(self) -> str:
        if not self.token_file.exists():
            raise ConfigError("YouTube not authorised. Run: dva auth-youtube")
        tok = json.loads(self.token_file.read_text())
        if tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        client = self._client()
        resp = self.http.post(TOKEN_URL, data={"client_id": client["client_id"], "client_secret": client["client_secret"],
                                               "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"},
                              ok_status=())
        new = resp.json()
        if "access_token" not in new:
            raise ConfigError(f"YouTube token refresh failed (re-run dva auth-youtube): {new.get('error')}")
        tok.update(access_token=new["access_token"], expires_at=time.time() + new.get("expires_in", 3600) - 60)
        _write_secret(self.token_file, tok)
        return tok["access_token"]

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}

    # ----------------------------------------------------------- upload
    def start_session(self, meta: dict, size: int) -> str:
        body = {
            "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta.get("tags", []),
                        "categoryId": str(self.y.get("category_id", "27")), "defaultLanguage": "hi",
                        "defaultAudioLanguage": "hi"},
            "status": {"privacyStatus": self.y.get("privacy_status", "public"),
                       "selfDeclaredMadeForKids": bool(self.y.get("made_for_kids", False)),
                       "containsSyntheticMedia": bool(self.y.get("contains_synthetic_media", False))},
        }
        resp = self.http.post_once(f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                                   headers={**self._h(), "Content-Type": "application/json; charset=UTF-8",
                                            "X-Upload-Content-Type": "video/mp4",
                                            "X-Upload-Content-Length": str(size)},
                                   data=json.dumps(body), ok_status=(200,))
        return resp.headers["Location"]

    def _uploaded_bytes(self, session: str, size: int) -> int | dict:
        """Query a resumable session. Returns bytes received, or the final video resource."""
        resp = self.http.session.put(session, headers={**self._h(), "Content-Range": f"bytes */{size}"}, timeout=60)
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 308:
            rng = resp.headers.get("Range")
            return int(rng.split("-")[1]) + 1 if rng else 0
        if resp.status_code == 404:
            raise PublishError("upload session expired")
        raise TransientError(f"session status HTTP {resp.status_code}")

    def upload(self, video: Path, meta: dict, session: str | None = None, on_session=None,
               chunk: int = 8 * 1024 * 1024) -> dict:
        size = video.stat().st_size
        if not session:
            session = self.start_session(meta, size)
            if on_session:
                on_session(session)
        offset = 0
        for attempt in range(8):
            try:
                st = self._uploaded_bytes(session, size)
                if isinstance(st, dict):
                    return st
                offset = st
                with open(video, "rb") as fh:
                    while offset < size:
                        fh.seek(offset)
                        data = fh.read(chunk)
                        end = offset + len(data) - 1
                        resp = self.http.session.put(session, data=data, timeout=300, headers={
                            **self._h(), "Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"})
                        if resp.status_code in (200, 201):
                            return resp.json()
                        if resp.status_code == 308:
                            rng = resp.headers.get("Range")
                            offset = int(rng.split("-")[1]) + 1 if rng else 0
                            continue
                        if resp.status_code in (500, 502, 503, 504):
                            raise TransientError(f"HTTP {resp.status_code}")
                        raise PublishError(f"YouTube upload failed HTTP {resp.status_code}: {resp.text[:400]}")
            except (TransientError, requests.ConnectionError, requests.Timeout) as exc:
                wait = min(60, 2 ** attempt)
                log.warning("YouTube upload interrupted (%s); resuming in %ss", exc, wait)
                time.sleep(wait)
        raise PublishError("YouTube upload did not complete after retries")

    # ---------------------------------------------------------- metrics
    def stats(self, video_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            resp = self.http.get(f"{API}/videos", params={"part": "statistics", "id": ",".join(video_ids[i:i + 50])},
                                 headers=self._h())
            for it in resp.json().get("items", []):
                s = it.get("statistics", {})
                out[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                 "comments": int(s.get("commentCount", 0))}
        return out

    def analytics(self, video_id: str, start: str, end: str) -> dict:
        """Shares + retention via YouTube Analytics API (optional scope)."""
        resp = self.http.get(ANALYTICS, headers=self._h(), ok_status=(), params={
            "ids": "channel==MINE", "startDate": start, "endDate": end, "filters": f"video=={video_id}",
            "metrics": "views,likes,shares,comments,averageViewDuration,averageViewPercentage"})
        if resp.status_code != 200:
            return {}
        data = resp.json()
        rows = data.get("rows") or []
        if not rows:
            return {}
        cols = [h["name"] for h in data.get("columnHeaders", [])]
        return dict(zip(cols, rows[0]))
