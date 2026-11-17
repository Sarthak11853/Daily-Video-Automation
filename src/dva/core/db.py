"""SQLite storage: topics, runs, videos, publications, metrics, quotas, kv."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    category TEXT,
    source TEXT,
    source_url TEXT,
    wiki_title TEXT,
    status TEXT NOT NULL DEFAULT 'used',   -- used | rejected | blacklisted
    created_at TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_topics_norm ON topics(normalized_title);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'daily',     -- daily | regenerate | demo | manual
    status TEXT NOT NULL,                   -- running | awaiting_approval | package | failed | done
    stage TEXT,
    topic_id INTEGER,
    output_dir TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    topic_id INTEGER,
    title TEXT,
    category TEXT,
    status TEXT NOT NULL,     -- awaiting_footage | processing_footage | awaiting_approval | approved | rejected |
                              -- regenerating | publishing | published | skipped | superseded
    video_path TEXT,
    video_sha256 TEXT UNIQUE,
    duration REAL,
    script_json TEXT,
    facts_json TEXT,
    metadata_json TEXT,
    qc_json TEXT,
    telegram_message_ids TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    reminded_at TEXT
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,     -- pending | uploading | processing | published | failed | unknown
    external_id TEXT,
    url TEXT,
    upload_session TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(video_id, platform)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER, reach INTEGER,
    avg_view_pct REAL, avg_watch_seconds REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS quota (
    service TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(service, day)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (idempotent)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")}
        for name, decl in (("prompts_json", "TEXT"), ("footage_path", "TEXT"), ("mode", "TEXT DEFAULT 'full'"),
                           ("user_caption", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {decl}")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, json.dumps(value), utcnow()))

    # --------------------------------------------------------------- quota
    def quota_used(self, service: str, day: str) -> int:
        row = self.one("SELECT used FROM quota WHERE service=? AND day=?", (service, day))
        return int(row["used"]) if row else 0

    def quota_add(self, service: str, day: str, amount: int = 1) -> int:
        self.execute("INSERT INTO quota(service,day,used) VALUES(?,?,?) "
                     "ON CONFLICT(service,day) DO UPDATE SET used=used+excluded.used", (service, day, amount))
        return self.quota_used(service, day)

    # -------------------------------------------------------------- topics
    def add_topic(self, title: str, normalized: str, keywords: list[str], category: str | None,
                  source: str | None, source_url: str | None, wiki_title: str | None,
                  status: str = "used", notes: str | None = None) -> int:
        return self.execute(
            "INSERT INTO topics(title,normalized_title,keywords,category,source,source_url,wiki_title,status,created_at,notes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (title, normalized, " ".join(keywords), category, source, source_url, wiki_title, status, utcnow(), notes))

    def recent_topics(self, days: int) -> list[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.query("SELECT * FROM topics WHERE created_at >= ? OR status='blacklisted'", (since,))

    # --------------------------------------------------------------- runs
    def start_run(self, run_date: str, kind: str) -> int:
        return self.execute("INSERT INTO runs(run_date,kind,status,stage,started_at) VALUES(?,?,?,?,?)",
                            (run_date, kind, "running", "start", utcnow()))

    def update_run(self, run_id: int, **fields: Any) -> None:
        if fields.get("status") in ("awaiting_approval", "awaiting_footage", "package", "failed", "done"):
            fields.setdefault("finished_at", utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    # -------------------------------------------------------------- videos
    def add_video(self, **fields: Any) -> int:
        fields.setdefault("created_at", utcnow())
        cols = ",".join(fields)
        qs = ",".join("?" for _ in fields)
        return self.execute(f"INSERT INTO videos({cols}) VALUES({qs})", tuple(fields.values()))

    def update_video(self, video_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), video_id))

    def get_video(self, video_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM videos WHERE id=?", (video_id,))

    def transition_video(self, video_id: int, from_states: tuple[str, ...], to_state: str) -> bool:
        """Atomic compare-and-set on video status (prevents double approval/publish)."""
        qs = ",".join("?" for _ in from_states)
        with self.tx() as c:
            cur = c.execute(f"UPDATE videos SET status=?, decided_at=? WHERE id=? AND status IN ({qs})",
                            (to_state, utcnow(), video_id, *from_states))
            return cur.rowcount == 1

    # -------------------------------------------------------- publications
    def get_publication(self, video_id: int, platform: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM publications WHERE video_id=? AND platform=?", (video_id, platform))

    def claim_publication(self, video_id: int, platform: str) -> sqlite3.Row:
        """Create or fetch the single publication row for (video, platform)."""
        self.execute("INSERT OR IGNORE INTO publications(video_id,platform,status,created_at) VALUES(?,?,?,?)",
                     (video_id, platform, "pending", utcnow()))
        row = self.get_publication(video_id, platform)
        assert row is not None
        return row

    def update_publication(self, pub_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE publications SET {cols} WHERE id=?", (*fields.values(), pub_id))

    def published_count_today(self, platform: str) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.one("SELECT COUNT(*) n FROM publications WHERE platform=? AND status='published' "
                       "AND substr(published_at,1,10)=?", (platform, day))
        return int(row["n"]) if row else 0

    def sha_exists_published(self, sha: str) -> bool:
        row = self.one("SELECT v.id FROM videos v JOIN publications p ON p.video_id=v.id "
                       "WHERE v.video_sha256=? AND p.status IN ('published','uploading','processing')", (sha,))
        return row is not None
