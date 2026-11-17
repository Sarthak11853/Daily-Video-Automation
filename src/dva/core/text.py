"""Text utilities: normalization, keywords, similarity, number extraction."""
from __future__ import annotations

import re
import unicodedata

STOPWORDS = set("""
a an the of in on at to for from by with and or but is are was were be been being this that these those
it its as into about over after before new study finds found says say how why what when who which
scientists researchers reveal reveals show shows could may might will can just than more most
has have had does did doing why where there their they you your our all any some very really ever
hai hain ka ki ke ko se me mein aur ye yeh woh kya kaise kyun
""".split())


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def keywords(text: str, limit: int = 12) -> list[str]:
    words = [w for w in normalize(text).split() if len(w) > 2 and w not in STOPWORDS]
    seen: list[str] = []
    for w in words:
        stem = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if stem not in seen:
            seen.append(stem)
    return seen[:limit]


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def overlap_coefficient(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w])")


_WORD_NUMS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORD_NUMS) + r")\b", re.I)


def numbers(text: str) -> set[str]:
    """Extract numeric tokens, normalised (commas removed, trailing .0 dropped,
    English number words zero..twelve mapped to digits)."""
    out = {_WORD_NUMS[m.lower()] for m in _WORD_RE.findall(text or "")}
    for m in _NUM_RE.findall(text or ""):
        n = m.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def squash(text: str) -> str:
    """Aggressive normalisation used for verbatim-quote matching."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\[\d+\]", "", text)  # wikipedia citation markers
    return re.sub(r"[^a-z0-9]+", "", text)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_latin_display(text: str) -> bool:
    """True if text can be drawn by Blender's text object (no complex scripts)."""
    return all(ord(ch) < 0x0250 or ch in "₹—–…“”‘’" for ch in text)
