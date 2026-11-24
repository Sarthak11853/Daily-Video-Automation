"""Fact extraction + verification.

1. The LLM extracts atomic claims and MUST attach verbatim quotes from the
   fetched sources.
2. Deterministic checks: every quote must really occur in its source text,
   every number in a claim must appear in its evidence, and the core claim
   needs enough independent trusted sources.
3. A second (judge) LLM call checks that each quote actually entails its claim.
Anything that fails is dropped; if too little survives, the topic is rejected.
"""
from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher

from ..core.errors import VerificationFailed
from ..core.models import Candidate, Claim, FactSheet, Source
from ..core.text import numbers, squash
from ..llm.base import LLMChain

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = ("You are a meticulous fact-checker. You only state facts that are explicitly written in the "
                  "provided sources. You copy evidence quotes character-for-character. You never use outside "
                  "knowledge, never round or convert numbers, never speculate.")

EXTRACT_PROMPT = """Topic: {topic}
Suggested angle: {angle}

SOURCES:
{sources}

Task: extract 6-10 short, surprising, kid-friendly facts about this topic that would make a great 60-second
"did you know" video. Each fact must be directly supported by the sources above.

Return JSON:
{{
 "category": one of {categories},
 "angle": "the single most surprising storyline (one sentence)",
 "claims": [
   {{"id": "C1", "text": "atomic factual claim in plain English",
     "is_core": true/false   (exactly one claim is the core 'wow' fact),
     "evidence": [{{"source_id": "S1", "quote": "exact sentence copied from that source"}}]}}
 ]
}}
Rules: quotes must be EXACT substrings of the source text (max 300 characters each). Add evidence from every
source that supports the claim. Do not include claims that are disputed, hedged ("may", "might") or opinion."""

JUDGE_SYSTEM = "You are a strict fact-checking judge. Answer only from the quotes given."

JUDGE_PROMPT = """For each claim decide whether the evidence quotes FULLY support it (no extra details,
no different numbers, no exaggeration).

{items}

Return JSON: {{"results": [{{"id": "C1", "verdict": "supported" | "partial" | "unsupported", "reason": "..."}}]}}"""


def quote_in_source(quote: str, source_text: str, min_ratio: float = 0.88) -> bool:
    q, t = squash(quote), squash(source_text)
    if len(q) < 15:
        return False
    if q in t:
        return True
    m = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return m.size >= min_ratio * len(q)


class FactVerifier:
    def __init__(self, cfg, llm: LLMChain):
        self.cfg = cfg
        self.llm = llm
        self.min_claims = int(cfg.get("research.min_verified_claims", 5))
        self.min_sources = int(cfg.get("research.min_independent_sources", 2))
        self.allow_tier1 = bool(cfg.get("research.allow_single_tier1_source", True))

    def extract(self, cand: Candidate, sources: list[Source]) -> dict:
        cats = json.dumps(self.cfg.get("topics.categories", []))

        def build(max_chars: int | None) -> str:
            # Sources are trimmed (keeping the start of each page) for AI services with smaller free limits.
            # Quotes are still checked against the FULL page text afterwards, so trimming never weakens checks.
            empty = EXTRACT_PROMPT.format(topic=cand.title, angle=cand.angle or "-", sources="", categories=cats)
            per = None
            if max_chars:
                room = max_chars - len(empty) - len(EXTRACT_SYSTEM) - 200 * len(sources)
                per = max(800, room // max(1, len(sources)))
            blob = "\n\n".join(f"[{s.id}] {s.title} ({s.domain})\n{s.text[:per] if per else s.text}" for s in sources)
            return EXTRACT_PROMPT.format(topic=cand.title, angle=cand.angle or "-", sources=blob, categories=cats)

        return self.llm.json(build, system=EXTRACT_SYSTEM, temperature=0.2)

    def check_claim(self, claim: Claim, by_id: dict[str, Source]) -> None:
        valid = []
        for ev in claim.evidence:
            src = by_id.get(str(ev.get("source_id", "")).strip())
            quote = str(ev.get("quote", ""))
            if src and quote_in_source(quote, src.text):
                valid.append({"source_id": src.id, "quote": quote})
        claim.evidence = valid
        if not valid:
            claim.verification_notes = "no verbatim evidence found in sources"
            return
        nums = numbers(claim.text)
        ev_nums = set().union(*(numbers(e["quote"]) for e in valid))
        if not nums <= ev_nums:
            claim.verification_notes = f"numbers {sorted(nums - ev_nums)} not in evidence"
            return
        domains = {by_id[e["source_id"]].domain for e in valid}
        tiers = {by_id[e["source_id"]].tier for e in valid}
        claim.independent_sources = len(domains)
        needed = self.min_sources if claim.is_core else 1
        if claim.independent_sources >= needed or (self.allow_tier1 and 1 in tiers):
            claim.verified = True
            claim.verification_notes = f"{len(valid)} quote(s), {len(domains)} domain(s)"
        else:
            claim.verification_notes = f"only {len(domains)} independent source(s); {needed} required"

    def judge(self, claims: list[Claim]) -> None:
        todo = [c for c in claims if c.verified]
        if not todo:
            return
        items = "\n".join(f"{c.id}: CLAIM: {c.text}\n    QUOTES: " + " || ".join(e["quote"] for e in c.evidence)
                          for c in todo)
        res = self.llm.json(JUDGE_PROMPT.format(items=items), system=JUDGE_SYSTEM, judge=True, temperature=0.0)
        verdicts = {r.get("id"): r for r in res.get("results", []) if isinstance(r, dict)}
        for c in todo:
            v = verdicts.get(c.id)
            if not v or v.get("verdict") != "supported":
                c.verified = False
                c.verification_notes += f"; judge: {(v or {}).get('verdict', 'missing')} {(v or {}).get('reason', '')}"

    def build(self, cand: Candidate, sources: list[Source]) -> FactSheet:
        if not sources:
            raise VerificationFailed(f"No trusted sources found for '{cand.title}'")
        data = self.extract(cand, sources)
        by_id = {s.id: s for s in sources}
        claims = []
        for i, c in enumerate(data.get("claims", []), 1):
            if not isinstance(c, dict) or not c.get("text"):
                continue
            claims.append(Claim(id=str(c.get("id") or f"C{i}"), text=str(c["text"]).strip(),
                                evidence=[e for e in c.get("evidence", []) if isinstance(e, dict)],
                                is_core=bool(c.get("is_core"))))
        if claims and not any(c.is_core for c in claims):
            claims[0].is_core = True
        for c in claims:
            self.check_claim(c, by_id)
        self.judge(claims)
        sheet = FactSheet(topic=cand.title, category=data.get("category") or cand.category or "science",
                          angle=data.get("angle") or cand.angle, sources=sources, claims=claims)
        ok = sheet.verified_claims
        core_ok = any(c.is_core and c.verified for c in claims)
        log.info("verification: %d/%d claims verified (core verified=%s)", len(ok), len(claims), core_ok)
        if len(ok) < self.min_claims or not core_ok:
            raise VerificationFailed(
                f"'{cand.title}': only {len(ok)} verified claims (need {self.min_claims}), core verified={core_ok}")
        return sheet
