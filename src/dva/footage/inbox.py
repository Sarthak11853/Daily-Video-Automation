"""Inbox folder intake: drop a finished video (or a folder of clips) into the
inbox and it's picked up automatically. Works with any synced folder (iCloud
Drive, Google Drive desktop, AirDrop to Downloads → move, etc.).

Matching: a number in the file/folder name that equals a pending video id
(e.g. `12.mp4`, `video-12/`) wins; otherwise the newest pending video is used."""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from .processor import VIDEO_EXT, FootageProcessor

log = logging.getLogger(__name__)
RESERVED = {"processed", "failed", "unmatched", ".working"}
PARTIAL = {".part", ".crdownload", ".download", ".icloud", ".tmp"}


def _files(item: Path) -> list[Path]:
    if item.is_file():
        return [item]
    return sorted(p for p in item.rglob("*") if p.is_file() and not p.name.startswith("."))


def ready_items(inbox: Path, settle_file: float = 20, settle_dir: float = 60) -> list[Path]:
    """Items whose files have stopped changing (still-copying files are left alone)."""
    if not inbox.is_dir():
        return []
    now = time.time()
    out = []
    for item in sorted(inbox.iterdir()):
        if item.name.startswith(".") or item.name in RESERVED:
            continue
        files = _files(item)
        if not files or any(f.suffix.lower() in PARTIAL for f in files):
            continue
        if item.is_file() and item.suffix.lower() not in VIDEO_EXT:
            continue
        if item.is_dir() and not any(f.suffix.lower() in VIDEO_EXT for f in files):
            continue
        newest = max(f.stat().st_mtime for f in files)
        if now - newest >= (settle_file if item.is_file() else settle_dir):
            out.append(item)
    return out


def _move(item: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{datetime.now():%Y%m%d-%H%M%S}_{item.name}"
    shutil.move(str(item), dest)
    return dest


def scan_inbox(pipeline, settle_file: float = 20, settle_dir: float = 60) -> list[str]:
    inbox = pipeline.cfg.path("prompt_mode.inbox_dir", "inbox").expanduser()
    inbox.mkdir(parents=True, exist_ok=True)
    proc = FootageProcessor(pipeline)
    messages = []
    for item in ready_items(inbox, settle_file, settle_dir):
        target = proc.resolve(item.name)
        if target is None:
            dest = _move(item, inbox / "unmatched")
            messages.append(f"📥 Got {item.name}, but no video is waiting for footage. Moved to {dest}.")
            continue
        working = _move(item, inbox / ".working")
        log.info("inbox: %s -> video #%s", item.name, target["id"])
        msg = proc.process(target["id"], _files(working))
        ok = pipeline.db.get_video(target["id"])["status"] != "awaiting_footage"
        _move(working, inbox / ("processed" if ok else "failed"))
        messages.append(f"📥 {item.name} → #{target['id']}\n{msg}")
    return messages
