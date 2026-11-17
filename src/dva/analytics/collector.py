"""Collect views/likes/shares/etc. for published videos and derive learnings
that feed back into topic selection (category weights) and script writing
(best-performing hooks)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from ..core.db import Database, utcnow

log = logging.getLogger(__name__)


def collect(cfg, db: Database, youtube=None, instagram=None) -> int:
    hours = sorted(cfg.get("analytics.collect_after_hours", [24, 72, 168]))
    now = datetime.now(timezone.utc)
    pubs = db.query("SELECT * FROM publications WHERE status='published' AND external_id IS NOT NULL")
    n = 0
    for p in pubs:
        published = datetime.fromisoformat(p["published_at"])
        age_h = (now - published).total_seconds() / 3600
        last = db.one("SELECT collected_at FROM metrics WHERE publication_id=? ORDER BY collected_at DESC LIMIT 1",
                      (p["id"],))
        # checkpoints: every configured age (e.g. 24h, 72h, 168h) gets exactly one snapshot
        due = [h for h in hours if age_h >= h]
        if not due:
            continue
        if last:
            last_age = (datetime.fromisoformat(last["collected_at"]) - published).total_seconds() / 3600
            if last_age >= due[-1]:
                continue
        try:
            row = _fetch(cfg, p, youtube, instagram, published.date())
        except Exception as exc:  # noqa: BLE001
            log.warning("metrics for %s/%s failed: %s", p["platform"], p["external_id"], exc)
            continue
        if row:
            db.execute("INSERT INTO metrics(publication_id,collected_at,views,likes,comments,shares,saves,reach,"
                       "avg_view_pct,avg_watch_seconds,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (p["id"], utcnow(), row.get("views"), row.get("likes"), row.get("comments"), row.get("shares"),
                        row.get("saves"), row.get("reach"), row.get("avg_view_pct"), row.get("avg_watch_seconds"),
                        json.dumps(row.get("raw", {}))))
            n += 1
    return n


def _fetch(cfg, p, youtube, instagram, published: date) -> dict | None:
    if p["platform"] == "youtube" and youtube and youtube.configured():
        base = youtube.stats([p["external_id"]]).get(p["external_id"], {})
        extra = {}
        if cfg.get("analytics.youtube_analytics_api", True):
            extra = youtube.analytics(p["external_id"], published.isoformat(),
                                      (date.today() + timedelta(days=1)).isoformat())
        return {**base, "shares": extra.get("shares"), "avg_view_pct": extra.get("averageViewPercentage"),
                "avg_watch_seconds": extra.get("averageViewDuration"), "raw": {**base, **extra}}
    if p["platform"] == "instagram" and instagram and instagram.configured():
        ins = instagram.insights(p["external_id"])
        watch = ins.get("ig_reels_avg_watch_time")
        return {"views": ins.get("views"), "likes": ins.get("likes"), "comments": ins.get("comments"),
                "shares": ins.get("shares"), "saves": ins.get("saved"), "reach": ins.get("reach"),
                "avg_watch_seconds": watch / 1000 if isinstance(watch, (int, float)) else None, "raw": ins}
    return None


def learnings(db: Database, limit: int = 3) -> str:
    """Prompt snippet: what worked best so far (top hooks by engagement)."""
    rows = db.query("""
        SELECT v.title, v.script_json, v.category,
               MAX(COALESCE(m.views,0)) AS views,
               MAX(COALESCE(m.likes,0) + 3*COALESCE(m.shares,0) + 2*COALESCE(m.saves,0)) * 1.0
                   / MAX(MAX(COALESCE(m.views,0)), 1) AS eng,
               MAX(COALESCE(m.avg_view_pct,0)) AS retention
        FROM videos v JOIN publications p ON p.video_id=v.id JOIN metrics m ON m.publication_id=p.id
        GROUP BY v.id HAVING views > 0 ORDER BY (eng + retention/100.0) DESC LIMIT ?""", (limit * 3,))
    if len(rows) < 2:
        return ""
    best = rows[:limit]
    worst = rows[-1]
    lines = ["\nWHAT WORKED ON THIS CHANNEL (learn from it, don't copy it):"]
    for r in best:
        s = json.loads(r["script_json"] or "{}")
        hook = (s.get("scenes") or [{}])[0].get("narration_display", "")
        lines.append(f"- Hook ({s.get('hook_style', '?')}, {r['category']}, {r['views']} views, "
                     f"retention {r['retention'] or 0:.0f}%): \"{hook}\"")
    ws = json.loads(worst["script_json"] or "{}")
    lines.append(f"- Weakest hook so far ({worst['views']} views): \"{(ws.get('scenes') or [{}])[0].get('narration_display', '')}\"")
    return "\n".join(lines) + "\n"


def report(db: Database) -> str:
    rows = db.query("""
        SELECT v.id, v.title, p.platform, p.url, MAX(m.views) views, MAX(m.likes) likes, MAX(m.shares) shares,
               MAX(m.comments) comments, MAX(m.avg_view_pct) ret
        FROM videos v JOIN publications p ON p.video_id=v.id LEFT JOIN metrics m ON m.publication_id=p.id
        WHERE p.status='published' GROUP BY p.id ORDER BY v.id DESC LIMIT 20""")
    if not rows:
        return "No published videos yet."
    return "\n".join(f"#{r['id']} {r['platform'][:2].upper()} {r['title'][:40]} — 👁 {r['views'] or 0} ❤ {r['likes'] or 0} "
                     f"↗ {r['shares'] or 0} 💬 {r['comments'] or 0}" + (f" ⏱ {r['ret']:.0f}%" if r["ret"] else "")
                     for r in rows)
