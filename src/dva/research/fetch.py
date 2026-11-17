"""Fetch source texts: Wikipedia (API), its cited references on trusted
domains, and the original article of a trend/RSS item."""
from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

from ..core.http import HttpClient
from ..core.models import Source

log = logging.getLogger(__name__)
WIKI_API = "https://en.wikipedia.org/w/api.php"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg", "figure"}
    KEEP = {"p", "li", "h1", "h2", "h3", "blockquote", "td"}

    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.in_keep = 0
        self.parts: list[str] = []
        self.buf: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.KEEP:
            self.in_keep += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.KEEP and self.in_keep:
            self.in_keep -= 1
            if not self.in_keep:
                text = " ".join("".join(self.buf).split())
                if len(text) > 40:
                    self.parts.append(text)
                self.buf = []
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.in_keep and not self.skip_depth:
            self.buf.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML
        pass
    return p.title.strip(), "\n".join(p.parts)


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def trust_tier(url: str, trusted: dict) -> int | None:
    host = domain_of(url)
    for tier_name, tier in (("tier1", 1), ("tier2", 2)):
        for d in trusted.get(tier_name, []):
            if host == d or host.endswith("." + d):
                return tier
    return None


class SourceFetcher:
    def __init__(self, cfg, http: HttpClient | None = None):
        self.cfg = cfg
        self.trusted = cfg.section("research.trusted_domains")
        self.max_chars = int(cfg.get("research.max_source_chars", 12000))
        self.http = http or HttpClient(cfg.get("research.user_agent", "DailyFactsBot/0.1"),
                                       timeout=cfg.get("research.request_timeout", 20))

    def wiki_search(self, query: str) -> str | None:
        r = self.http.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": query,
                                            "srlimit": 1, "format": "json"}).json()
        hits = r.get("query", {}).get("search", [])
        return hits[0]["title"] if hits else None

    def wikipedia(self, title: str) -> tuple[Source | None, list[str]]:
        r = self.http.get(WIKI_API, params={"action": "query", "prop": "extracts|info|extlinks", "explaintext": 1,
                                            "inprop": "url", "ellimit": 200, "redirects": 1, "titles": title,
                                            "format": "json"}).json()
        pages = r.get("query", {}).get("pages", {})
        for _, pg in pages.items():
            if "missing" in pg or not pg.get("extract"):
                continue
            text = pg["extract"]
            # drop reference sections
            for marker in ("\n== References ==", "\n== See also ==", "\n== External links ==", "\n== Notes =="):
                text = text.split(marker)[0]
            links = [l.get("*") or l.get("url", "") for l in pg.get("extlinks", [])]
            src = Source(id="", url=pg.get("fullurl", f"https://en.wikipedia.org/wiki/{quote(title)}"),
                         title=pg.get("title", title), domain="wikipedia.org", tier=2, text=text[: self.max_chars])
            return src, links
        return None, []

    def page(self, url: str) -> Source | None:
        tier = trust_tier(url, self.trusted)
        if tier is None:
            return None
        try:
            resp = self.http.get(url, headers={"Accept": "text/html"})
        except Exception as exc:  # noqa: BLE001
            log.info("source fetch failed %s: %s", url, exc)
            return None
        if "html" not in resp.headers.get("Content-Type", "html"):
            return None
        title, text = html_to_text(resp.text)
        if len(text) < 600:
            return None
        return Source(id="", url=url, title=title[:200] or url, domain=domain_of(url), tier=tier,
                      text=text[: self.max_chars])

    def gather(self, query: str, article_url: str | None = None, wiki_title: str | None = None,
               max_sources: int = 4) -> list[Source]:
        sources: list[Source] = []
        if article_url:
            s = self.page(article_url)
            if s:
                sources.append(s)
        title = wiki_title or self.wiki_search(query)
        refs: list[str] = []
        if title:
            w, refs = self.wikipedia(title)
            if w:
                sources.append(w)
        # follow Wikipedia citations that live on trusted, non-Wikipedia domains (independent sources)
        seen_domains = {s.domain for s in sources}
        refs = sorted(set(refs), key=lambda u: trust_tier(u, self.trusted) or 9)
        for url in refs:
            if len(sources) >= max_sources:
                break
            if not url.startswith("http") or trust_tier(url, self.trusted) is None:
                continue
            if domain_of(url) in seen_domains or url.lower().endswith(".pdf"):
                continue
            s = self.page(url)
            if s:
                sources.append(s)
                seen_domains.add(s.domain)
        for i, s in enumerate(sources, 1):
            s.id = f"S{i}"
        return sources
