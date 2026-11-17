"""Topic candidate sources: Google Trends RSS, science/news RSS feeds,
Wikipedia featured feed (most-read / on-this-day) and a curated evergreen pool.
Every source fails soft: one broken feed never stops the run."""
from __future__ import annotations

import json
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import date
from html import unescape
from pathlib import Path

from ..core.http import HttpClient
from ..core.models import Candidate

log = logging.getLogger(__name__)

NS = {"ht": "https://trends.google.com/trending/rss", "atom": "http://www.w3.org/2005/Atom",
      "media": "http://search.yahoo.com/mrss/"}

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", text or ""))).strip()


def _traffic_to_score(traffic: str) -> float:
    m = re.match(r"([\d,]+)\s*([KkMm]?)", traffic or "")
    if not m:
        return 0.3
    n = float(m.group(1).replace(",", ""))
    n *= {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)
    # 1K -> ~0.3, 100K -> ~0.7, 1M+ -> 1.0
    import math
    return max(0.1, min(1.0, (math.log10(max(n, 10)) - 2) / 4))


def parse_rss(xml_text: str, source: str, category: str | None = None, limit: int = 30) -> list[Candidate]:
    """Parse RSS 2.0 or Atom into candidates."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as exc:
        log.warning("Bad feed XML from %s: %s", source, exc)
        return []
    out: list[Candidate] = []
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    for it in items[:limit]:
        title = strip_html(it.findtext("title") or it.findtext("atom:title", default="", namespaces=NS))
        if not title:
            continue
        link = it.findtext("link") or ""
        if not link:
            el = it.find("atom:link", NS)
            link = el.get("href", "") if el is not None else ""
        summary = strip_html(it.findtext("description") or it.findtext("atom:summary", default="", namespaces=NS)
                             or it.findtext("atom:content", default="", namespaces=NS))
        pub = it.findtext("pubDate") or it.findtext("atom:updated", default="", namespaces=NS)
        cand = Candidate(title=title, source=source, url=link.strip() or None, summary=summary[:600],
                         category=category, published=pub, trend_score=0.4)
        traffic = it.findtext("ht:approx_traffic", default="", namespaces=NS)
        if traffic:
            cand.trend_score = _traffic_to_score(traffic)
            news = []
            for n in it.findall("ht:news_item", NS):
                news.append({"title": strip_html(n.findtext("ht:news_item_title", default="", namespaces=NS)),
                             "url": n.findtext("ht:news_item_url", default="", namespaces=NS),
                             "source": n.findtext("ht:news_item_source", default="", namespaces=NS)})
            cand.extra["news"] = news
            if news and not cand.url:
                cand.url = news[0]["url"]
            if news and not cand.summary:
                cand.summary = " | ".join(n["title"] for n in news[:3])
        out.append(cand)
    return out


class Discovery:
    def __init__(self, cfg, http: HttpClient | None = None):
        self.cfg = cfg
        self.http = http or HttpClient(cfg.get("research.user_agent", "DailyFactsBot/0.1"),
                                       timeout=cfg.get("research.request_timeout", 20))

    def google_trends(self) -> list[Candidate]:
        tcfg = self.cfg.section("topics.google_trends")
        if not tcfg.get("enabled", True):
            return []
        resp = self.http.get(tcfg.get("url", "https://trends.google.com/trending/rss?geo=IN"))
        return parse_rss(resp.text, "google_trends")

    def rss(self) -> list[Candidate]:
        out: list[Candidate] = []
        for feed in self.cfg.get("topics.rss_feeds", []) or []:
            try:
                resp = self.http.get(feed["url"])
                out += parse_rss(resp.text, f"rss:{feed['name']}", feed.get("category"), limit=15)
            except Exception as exc:  # noqa: BLE001 — feeds must fail soft
                log.warning("RSS %s failed: %s", feed.get("name"), exc)
        return out

    def wikipedia_featured(self, day: date | None = None) -> list[Candidate]:
        if not self.cfg.get("topics.wikipedia_featured.enabled", True):
            return []
        day = day or date.today()
        url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{day:%Y/%m/%d}"
        data = self.http.get(url).json()
        out: list[Candidate] = []
        for art in (data.get("mostread", {}) or {}).get("articles", [])[:25]:
            title = art.get("normalizedtitle") or art.get("title", "").replace("_", " ")
            out.append(Candidate(title=title, source="wikipedia_mostread", wiki_title=art.get("title"),
                                 url=art.get("content_urls", {}).get("desktop", {}).get("page"),
                                 summary=(art.get("extract") or "")[:600],
                                 trend_score=min(1.0, (art.get("views", 0) or 0) / 500000)))
        for ev in (data.get("onthisday") or [])[:10]:
            pages = ev.get("pages") or []
            if not pages:
                continue
            pg = pages[0]
            out.append(Candidate(title=f"{ev.get('year')}: {ev.get('text', '')[:120]}", source="wikipedia_onthisday",
                                 wiki_title=pg.get("title"), category="history",
                                 url=pg.get("content_urls", {}).get("desktop", {}).get("page"),
                                 summary=(pg.get("extract") or "")[:600], trend_score=0.35))
        return out

    def evergreen(self, k: int = 12, seed: int | None = None) -> list[Candidate]:
        if not self.cfg.get("topics.evergreen_seed_enabled", True):
            return []
        pool = json.loads((Path(__file__).parent / "evergreen.json").read_text(encoding="utf-8"))
        rng = random.Random(seed)
        items = [(cat, t) for cat, titles in pool.items() for t in titles]
        rng.shuffle(items)
        return [Candidate(title=t, source="evergreen", wiki_title=t, category=cat, trend_score=0.25,
                          url=f"https://en.wikipedia.org/wiki/{t.replace(' ', '_')}") for cat, t in items[:k]]

    def collect(self) -> list[Candidate]:
        cands: list[Candidate] = []
        for name, fn in (("google_trends", self.google_trends), ("rss", self.rss),
                         ("wikipedia_featured", self.wikipedia_featured)):
            try:
                got = fn()
                log.info("discovery: %s -> %d candidates", name, len(got))
                cands += got
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery source %s failed: %s", name, exc)
        cands += self.evergreen(seed=date.today().toordinal())
        return cands
