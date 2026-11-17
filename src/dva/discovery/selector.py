"""Choose today's topic: filter, de-duplicate against history, rank with the
LLM (one batched call) and blend in past performance per category."""
from __future__ import annotations

import json
import logging
import random

from ..core.db import Database
from ..core.models import Candidate
from ..core.text import jaccard, keywords, normalize, overlap_coefficient
from ..llm.base import LLMChain

log = logging.getLogger(__name__)

RANK_SYSTEM = ("You are the research editor of a family-friendly Indian YouTube Shorts / Instagram Reels channel "
               "that explains surprising, verifiable facts in simple words. You pick topics that can be backed by "
               "encyclopaedic or scientific sources. You never pick politics, tragedies, crime, celebrities, "
               "medical advice, or anything that is rumour or opinion.")

RANK_PROMPT = """Here are today's candidate topics (from Google Trends India, science RSS feeds and Wikipedia):

{items}

For EACH candidate return an object with:
- "i": its index
- "category": one of {categories}
- "fact_worthy": 0-10 (10 = a mind-blowing, verifiable, evergreen-interesting fact story; 0 = news/politics/gossip/sports)
- "sensitive": true if it involves death, violence, politics, religion conflict, health advice or a living private person
- "angle": one sentence: the single most surprising verifiable fact angle (English)
- "wiki_query": the best English Wikipedia article title to research it

Return JSON: {{"items": [ ... ]}}"""


def is_duplicate(title: str, history_rows, threshold: float) -> bool:
    kw = keywords(title)
    norm = normalize(title)
    for row in history_rows:
        if row["normalized_title"] == norm:
            return True
        hk = (row["keywords"] or "").split()
        if jaccard(kw, hk) >= threshold or (len(kw) >= 2 and overlap_coefficient(kw, hk) >= 0.85):
            return True
        if row["wiki_title"] and normalize(row["wiki_title"]) == norm:
            return True
    return False


def category_weights(db: Database, categories: list[str]) -> dict[str, float]:
    """Bayesian-smoothed engagement per category, normalised around 1.0."""
    rows = db.query("""
        SELECT v.category AS category, AVG(m.score) AS avg_score, COUNT(*) AS n FROM videos v
        JOIN publications p ON p.video_id = v.id
        JOIN (SELECT publication_id,
                     MAX(COALESCE(views,0)) AS views,
                     (MAX(COALESCE(likes,0)) + 3*MAX(COALESCE(shares,0)) + 2*MAX(COALESCE(saves,0))
                       + MAX(COALESCE(comments,0))) * 1.0 / MAX(MAX(COALESCE(views,0)),1)
                       + COALESCE(MAX(avg_view_pct),0)/100.0 AS score
              FROM metrics GROUP BY publication_id) m ON m.publication_id = p.id
        WHERE v.category IS NOT NULL GROUP BY v.category""")
    stats = {r["category"]: (float(r["avg_score"] or 0), int(r["n"])) for r in rows}
    if not stats:
        return {c: 1.0 for c in categories}
    global_mean = sum(s * n for s, n in stats.values()) / max(1, sum(n for _, n in stats.values()))
    prior = 3  # pseudo-observations
    weights = {}
    for c in categories:
        s, n = stats.get(c, (global_mean, 0))
        smoothed = (s * n + global_mean * prior) / (n + prior)
        weights[c] = smoothed / global_mean if global_mean > 0 else 1.0
    return weights


class TopicSelector:
    def __init__(self, cfg, db: Database, llm: LLMChain | None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.categories = cfg.get("topics.categories", [])

    def prefilter(self, cands: list[Candidate]) -> list[Candidate]:
        blocked = [b.lower() for b in self.cfg.get("topics.blocked_keywords", [])]
        history = self.db.recent_topics(int(self.cfg.get("topics.avoid_days", 365)))
        thr = float(self.cfg.get("topics.similarity_threshold", 0.6))
        out, seen = [], []
        for c in cands:
            text = f"{c.title} {c.summary}".lower()
            if any(b in text for b in blocked):
                continue
            if is_duplicate(c.title, history, thr) or (c.wiki_title and is_duplicate(c.wiki_title, history, thr)):
                continue
            kw = keywords(c.title)
            if any(jaccard(kw, s) >= thr for s in seen):
                continue
            seen.append(kw)
            out.append(c)
        return out

    def rank(self, cands: list[Candidate]) -> list[Candidate]:
        cands = cands[: int(self.cfg.get("topics.max_candidates_for_llm", 25))]
        judged: dict[int, dict] = {}
        if self.llm and self.llm.available_providers():
            items = "\n".join(f"{i}. [{c.source}] {c.title} — {c.summary[:200]}" for i, c in enumerate(cands))
            try:
                res = self.llm.json(RANK_PROMPT.format(items=items, categories=json.dumps(self.categories)),
                                    system=RANK_SYSTEM, judge=True, temperature=0.2)
                for it in res.get("items", []):
                    if isinstance(it, dict) and isinstance(it.get("i"), int):
                        judged[it["i"]] = it
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM ranking failed, using heuristic ranking: %s", exc)
        weights = category_weights(self.db, self.categories)
        explore = random.random() < float(self.cfg.get("topics.exploration_rate", 0.2))
        ranked = []
        for i, c in enumerate(cands):
            j = judged.get(i)
            if j:
                if j.get("sensitive"):
                    continue
                c.category = j.get("category") if j.get("category") in self.categories else (c.category or "science")
                c.angle = j.get("angle", "")
                c.search_query = j.get("wiki_query") or c.wiki_title or c.title
                worth = float(j.get("fact_worthy", 0)) / 10
            else:
                c.category = c.category or "science"
                c.search_query = c.wiki_title or c.title
                worth = 0.55 if c.source in ("evergreen",) or c.source.startswith("rss") else 0.35
            if worth < 0.5:
                continue
            perf = 1.0 if explore else weights.get(c.category, 1.0)
            c.score = round(worth * 0.6 + c.trend_score * 0.25 + 0.15 * min(perf, 2.0) / 2.0, 4) * perf ** 0.5
            ranked.append(c)
        ranked.sort(key=lambda c: c.score, reverse=True)
        return ranked

    def select(self, cands: list[Candidate]) -> list[Candidate]:
        """Return candidates best-first (the pipeline tries them until research verifies one)."""
        filtered = self.prefilter(cands)
        log.info("selector: %d candidates after filtering (from %d)", len(filtered), len(cands))
        # trends first so they get LLM judgement; evergreen guarantees a fallback
        filtered.sort(key=lambda c: (c.source == "evergreen", -c.trend_score))
        return self.rank(filtered)
