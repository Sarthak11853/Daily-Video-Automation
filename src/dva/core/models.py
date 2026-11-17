"""Plain dataclasses passed between pipeline stages (JSON-serialisable)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Candidate:
    title: str
    source: str                      # google_trends | rss:<name> | wikipedia_featured | evergreen
    url: str | None = None
    summary: str = ""
    category: str | None = None
    trend_score: float = 0.0         # 0..1 popularity signal
    wiki_title: str | None = None
    published: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # filled by the selector
    score: float = 0.0
    angle: str = ""
    search_query: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Source:
    id: str                          # S1, S2 ...
    url: str
    title: str
    domain: str
    tier: int                        # 1 = primary/scientific, 2 = reputable secondary
    text: str = ""

    def to_dict(self, with_text: bool = False) -> dict:
        d = asdict(self)
        if not with_text:
            d.pop("text")
        return d


@dataclass
class Claim:
    id: str                          # C1, C2 ...
    text: str                        # claim in plain English
    evidence: list[dict[str, str]] = field(default_factory=list)  # [{source_id, quote}]
    verified: bool = False
    verification_notes: str = ""
    independent_sources: int = 0
    is_core: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FactSheet:
    topic: str
    category: str
    angle: str
    sources: list[Source]
    claims: list[Claim]
    demo: bool = False

    @property
    def verified_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.verified]

    def to_dict(self) -> dict:
        return {"topic": self.topic, "category": self.category, "angle": self.angle, "demo": self.demo,
                "sources": [s.to_dict() for s in self.sources], "claims": [c.to_dict() for c in self.claims]}

    @classmethod
    def from_dict(cls, d: dict) -> "FactSheet":
        return cls(topic=d["topic"], category=d.get("category", "science"), angle=d.get("angle", ""),
                   sources=[Source(**{k: v for k, v in s.items()}) for s in d.get("sources", [])],
                   claims=[Claim(**c) for c in d.get("claims", [])], demo=d.get("demo", False))
