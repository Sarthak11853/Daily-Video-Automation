import json

from conftest import CLAIMS, SOURCE_TEXT_1, FakeFetcher, FakeProvider, make_script

from dva.core.models import Candidate, Claim, FactSheet
from dva.core.text import keywords, numbers, squash
from dva.discovery.selector import TopicSelector, is_duplicate
from dva.discovery.sources import parse_rss
from dva.editor.subtitles import build_ass, build_srt
from dva.editor.timeline import build_timeline
from dva.llm.base import LLMChain, parse_json
from dva.publish.metadata import build_metadata
from dva.research.verifier import FactVerifier, quote_in_source
from dva.script.writer import ScriptWriter, normalize_script, validate_script
from dva.approval.telegram import split_text


def test_numbers_and_keywords():
    assert numbers("about 0.5 mm, 1,200 km, three hearts, 2.50") == {"0.5", "1200", "3", "2.5"}
    assert "tardigrade" in keywords("The Tardigrades of space")
    assert squash("Hello[1], “World”!") == "helloworld"


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": [1,2]} hope this helps') == {"a": [1, 2]}


def test_rss_google_trends():
    xml = """<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
    <item><title>chandrayaan</title><ht:approx_traffic>200K+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>ISRO update</ht:news_item_title><ht:news_item_url>https://isro.gov.in/x</ht:news_item_url>
    <ht:news_item_source>ISRO</ht:news_item_source></ht:news_item></item></channel></rss>"""
    c = parse_rss(xml, "google_trends")[0]
    assert c.title == "chandrayaan" and c.trend_score > 0.7 and c.url == "https://isro.gov.in/x"


def test_quote_matching():
    assert quote_in_source("Tardigrades have survived exposure to the vacuum of outer space.", SOURCE_TEXT_1)
    assert not quote_in_source("Tardigrades can live for one thousand years in ice.", SOURCE_TEXT_1)


def _sheet(llm):
    cand = Candidate(title="Tardigrade", source="evergreen")
    from conftest import ROOT
    from dva.core.config import Config
    cfg = Config.load(ROOT / "config" / "settings.yaml", root=ROOT, overrides={"research": {"min_verified_claims": 4}})
    return FactVerifier(cfg, llm).build(cand, FakeFetcher().gather("x")), cfg


def test_verifier_rejects_hallucinations(llm):
    sheet, _ = _sheet(llm)
    by = {c.id: c for c in sheet.claims}
    assert by["C1"].verified and by["C1"].independent_sources == 2
    assert not by["C6"].verified and "numbers" in by["C6"].verification_notes
    assert not by["C7"].verified and "verbatim" in by["C7"].verification_notes
    assert {c.id for c in sheet.verified_claims} == {"C1", "C2", "C3", "C4", "C5"}


def test_core_claim_needs_two_independent_sources(llm, cfg):
    sources = FakeFetcher().gather("x")[:1]  # only wikipedia (tier 2)
    v = FactVerifier(cfg, llm)
    c = Claim(id="C1", text="Tardigrades have survived exposure to the vacuum of outer space.", is_core=True,
              evidence=[{"source_id": "S1", "quote": "Tardigrades have survived exposure to the vacuum of outer space."}])
    v.check_claim(c, {s.id: s for s in sources})
    assert not c.verified


def test_script_validation(llm, cfg):
    sheet, _ = _sheet(llm)
    good = normalize_script(make_script(), cfg)
    assert validate_script(good, sheet, cfg) == []
    bad = normalize_script(make_script(bad_number=True), cfg)
    assert any("numbers" in p for p in validate_script(bad, sheet, cfg))
    few = normalize_script(make_script(n_scenes=3), cfg)
    assert any("scenes" in p for p in validate_script(few, sheet, cfg))
    # easy English: no Hindi script, no very long sentences
    s = make_script()
    s["scenes"][0]["narration_display"] = "क्या " + s["scenes"][0]["narration_display"]
    assert any("English only" in p for p in validate_script(normalize_script(s, cfg), sheet, cfg))
    s = make_script()
    s["scenes"][2]["narration_display"] = ("It has eight legs and every one of those legs has little claws that help it "
                                           "hold on to moss and leaves and tiny wet rocks all day long.")
    assert any("too long" in p for p in validate_script(normalize_script(s, cfg), sheet, cfg))
    # Hinglish mode still supported
    cfg.data["channel"]["language"] = "hinglish"
    assert validate_script(normalize_script(make_script(lang="hinglish"), cfg), sheet, cfg) == []
    s = make_script(lang="hinglish")
    s["scenes"][0]["narration_tts"] = "roman only"
    assert any("Devanagari" in p for p in validate_script(normalize_script(s, cfg), sheet, cfg))


def test_script_vocab_coercion(cfg):
    s = make_script()
    s["scenes"][0].update(environment="Outer Space", props=["Planets", "unicorn"], camera="zoom", sfx="laser")
    n = normalize_script(s, cfg)["scenes"][0]
    assert n["environment"] == "space" and n["props"][0] == "planet" and n["sfx"] == "whoosh"


def test_script_writer_retries_on_audit_failure(cfg):
    prov = FakeProvider(audit_fail_first=True)
    llm = LLMChain([prov])
    sheet, _ = _sheet(llm)
    script = ScriptWriter(cfg, llm).write(sheet)
    assert script["attempts"] == 2 and prov.calls.count("script") == 2


def test_duplicate_detection(db):
    db.add_topic("Octopus has three hearts", "octopus has three hearts", keywords("Octopus has three hearts"),
                 "animals", "x", None, "Octopus")
    hist = db.recent_topics(365)
    assert is_duplicate("Octopus", hist, 0.6)
    assert is_duplicate("Why does an octopus have three hearts?", hist, 0.6)
    assert not is_duplicate("Neutron star", hist, 0.6)


def test_selector_filters_blocked_and_ranks(cfg, db, llm):
    from conftest import FakeDiscovery
    ranked = TopicSelector(cfg, db, llm).select(FakeDiscovery().collect())
    titles = [c.title for c in ranked]
    assert "Cricket score live IPL" not in titles
    assert titles[0] == "Tardigrade"


def test_timeline_and_subtitles(cfg):
    scenes = normalize_script(make_script(), cfg)["scenes"]
    voice = [{"id": s["id"], "duration": 3.0 + i * 0.2} for i, s in enumerate(scenes)]
    tl = build_timeline(scenes, voice, 0.35)
    assert tl.scenes[0].start == 0
    for a, b in zip(tl.scenes, tl.scenes[1:]):
        assert abs(b.start - (a.start + a.clip_len - 0.35)) < 1e-6
        assert b.voice_start > a.voice_start + a.voice_dur  # voices never overlap
    for s in tl.scenes:
        assert abs(s.words[-1]["end"] - (s.voice_start + s.voice_dur)) < 0.01
    ass, cues = build_ass(tl, cfg)
    assert "Dialogue:" in ass and "\\c&H" in ass
    assert all(cues[i]["start"] >= cues[i - 1]["end"] - 1e-6 for i in range(1, len(cues)))
    assert "-->" in build_srt(cues)


def test_metadata_limits(llm, cfg):
    sheet, _ = _sheet(llm)
    s = normalize_script(make_script(), cfg)
    s["youtube_title"] = "X" * 150
    m = build_metadata(s, sheet, cfg)
    assert len(m["youtube"]["title"]) <= 100
    assert len(m["instagram"]["caption"]) <= 2200
    assert sum(len(t) + 1 for t in m["youtube"]["tags"]) <= 500
    assert "esa.int" in m["instagram"]["caption"] and "Sources" in m["youtube"]["description"]
    assert m["youtube"]["tags"][:2] == ["tardigrade facts", "water bear"]       # AI-chosen tags come first
    assert "animals in space" in m["youtube"]["tags"] and m["youtube"]["language"] == "en"
    assert m["caption_source"] == "automatic"


def test_split_text():
    parts = split_text("line\n" * 2000, 4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 3


def test_quota_enforced(db):
    from dva.core.errors import QuotaExceeded
    p = FakeProvider()
    p.cfg = {"daily_request_limit": 2}
    p.db = db
    p.generate("Write a ~ x")
    p.generate("Write a ~ x")
    try:
        p.generate("Write a ~ x")
        raise AssertionError("expected quota error")
    except QuotaExceeded:
        pass


def test_factsheet_roundtrip(llm):
    sheet, _ = _sheet(llm)
    again = FactSheet.from_dict(json.loads(json.dumps(sheet.to_dict())))
    assert [c.id for c in again.verified_claims] == [c.id for c in sheet.verified_claims]
    assert CLAIMS["claims"][0]["id"] == "C1"
