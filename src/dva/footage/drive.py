"""Google Drive inbox (cloud mode): upload your finished video from the phone's
Drive app into one folder; the cloud job downloads and publishes it. No size
limit (Telegram bots can only download 20 MB).

Uses the same Google login as YouTube (scope: drive.readonly). Files are never
modified or deleted; processed file ids are remembered in the database.
Caption: send it on Telegram, or put a .txt with the same name next to the video."""
from __future__ import annotations

import logging
from pathlib import Path

from ..core.http import HttpClient
from .processor import VIDEO_EXT, FootageProcessor

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/drive/v3/files"


class DriveInbox:
    def __init__(self, cfg, db, youtube=None, http: HttpClient | None = None):
        from ..publish.youtube import YouTube
        self.cfg = cfg
        self.db = db
        self.yt = youtube or YouTube(cfg)
        self.http = http or HttpClient(timeout=120)
        self.folder = cfg.secret(cfg.get("cloud.drive_folder_id_env", "DVA_DRIVE_FOLDER_ID"))

    def configured(self) -> bool:
        return bool(self.folder) and self.yt.configured()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.yt.access_token()}"}

    def _done(self) -> list[str]:
        return self.db.kv_get("drive_done", []) or []

    def mark_done(self, file_id: str) -> None:
        done = self._done()
        done.append(file_id)
        self.db.kv_set("drive_done", done[-500:])

    def list_files(self) -> list[dict]:
        resp = self.http.get(API, headers=self._h(), ok_status=(), params={
            "q": f"'{self.folder}' in parents and trashed=false",
            "fields": "files(id,name,size,mimeType,createdTime)", "orderBy": "createdTime", "pageSize": 100})
        if resp.status_code == 403:
            log.warning("Drive access denied — re-run `dva auth-youtube` (adds Drive read access) and update the "
                        "YOUTUBE_TOKEN_JSON secret")
            return []
        if resp.status_code != 200:
            log.warning("Drive listing failed HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("files", [])

    def new_videos(self) -> list[dict]:
        if not self.configured():
            return []
        done = set(self._done())
        return [f for f in self.list_files() if f["id"] not in done and
                (f.get("mimeType", "").startswith("video/") or Path(f["name"]).suffix.lower() in VIDEO_EXT)]

    def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.session.get(f"{API}/{file_id}", params={"alt": "media"}, headers=self._h(),
                                   stream=True, timeout=600) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
        return dest

    def caption_for(self, video: dict, files: list[dict]) -> str | None:
        stem = Path(video["name"]).stem
        for f in files:
            if Path(f["name"]).stem == stem and Path(f["name"]).suffix.lower() == ".txt":
                r = self.http.session.get(f"{API}/{f['id']}", params={"alt": "media"}, headers=self._h(), timeout=60)
                if r.ok:
                    return r.content.decode("utf-8", "replace").strip() or None
        return None

    def scan(self, pipeline) -> list[str]:
        vids = self.new_videos()
        if not vids:
            return []
        files = self.list_files()
        proc = FootageProcessor(pipeline)
        out = []
        for f in vids:
            self.mark_done(f["id"])  # never retry the same upload forever; a fixed video = a new upload
            target = proc.resolve(f["name"])
            if target is None:
                out.append(f"📥 Drive: got {f['name']}, but no video is waiting for footage.")
                continue
            dest = pipeline.cfg.data_dir / "drive_in" / f"{f['id']}{Path(f['name']).suffix or '.mp4'}"
            try:
                self.download(f["id"], dest)
                msg = proc.process(target["id"], [dest], caption=self.caption_for(f, files))
            finally:
                dest.unlink(missing_ok=True)
            out.append(f"📥 Drive: {f['name']} → #{target['id']}\n{msg}")
        return out
