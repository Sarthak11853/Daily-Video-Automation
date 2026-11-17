"""Shared fixtures: offline fakes for the LLM, discovery, source fetching,
Telegram and platform APIs, so the full pipeline can be tested without
network access or credentials."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dva.core.config import Config  # noqa: E402
from dva.core.db import Database  # noqa: E402
from dva.core.models import Candidate, Source  # noqa: E402
from dva.llm.base import LLMChain, LLMProvider  # noqa: E402

SOURCE_TEXT_1 = (
    "The tardigrade is a micro-animal found almost everywhere on Earth. Tardigrades are about 0.5 mm long when "
    "fully grown. They can survive temperatures as low as -272 degrees Celsius. Tardigrades have survived exposure "
    "to the vacuum of outer space. They can enter a dried-out state called cryptobiosis and revive when water "
    "returns. Tardigrades have eight legs, each ending in claws.")
SOURCE_TEXT_2 = (
    "Water bears, or tardigrades, can survive the vacuum of outer space, as an ESA experiment showed in 2007. "
    "In the dried-out cryptobiosis state, their metabolism drops to less than 0.01 percent of normal.")

CLAIMS = {
    "category": "animals", "angle": "Tiny animals that survive space",
    "claims": [
        {"id": "C1", "text": "Tardigrades have survived exposure to the vacuum of outer space.", "is_core": True,
         "evidence": [{"source_id": "S1", "quote": "Tardigrades have survived exposure to the vacuum of outer space."},
                      {"source_id": "S2", "quote": "Water bears, or tardigrades, can survive the vacuum of outer space"}]},
        {"id": "C2", "text": "Tardigrades are about 0.5 mm long when fully grown.",
         "evidence": [{"source_id": "S1", "quote": "Tardigrades are about 0.5 mm long when fully grown."}]},
        {"id": "C3", "text": "Tardigrades can survive temperatures as low as -272 degrees Celsius.",
         "evidence": [{"source_id": "S1", "quote": "They can survive temperatures as low as -272 degrees Celsius."}]},
        {"id": "C4", "text": "Tardigrades have eight legs.",
         "evidence": [{"source_id": "S1", "quote": "Tardigrades have eight legs, each ending in claws."}]},
        {"id": "C5", "text": "Tardigrades can enter a dried-out state called cryptobiosis and revive when water returns.",
         "evidence": [{"source_id": "S1", "quote": "They can enter a dried-out state called cryptobiosis and revive when water returns."}]},
        {"id": "C6", "text": "Tardigrades are 5 cm long.",  # hallucinated number -> must be rejected
         "evidence": [{"source_id": "S1", "quote": "Tardigrades are about 0.5 mm long when fully grown."}]},
        {"id": "C7", "text": "Tardigrades can live for 1000 years.",  # fabricated quote -> must be rejected
         "evidence": [{"source_id": "S1", "quote": "Tardigrades can live for one thousand years in ice."}]},
    ],
}


def make_script(n_scenes: int = 6, bad_number: bool = False, lang: str = "english") -> dict:
    english = [
        ("Can a tiny animal survive in outer space?", ["C1"]),
        ("Meet the tardigrade. It is only 0.5 mm long.", ["C2"]),
        ("It has eight legs, and every leg has little claws!", ["C4"]),
        ("It can survive cold as low as -272 degrees Celsius.", ["C3"]),
        ("With no water, it dries up and waits. This is called cryptobiosis.", ["C5"]),
        ("And it has even survived the vacuum of space. Follow for more facts!", ["C1"]),
    ]
    hinglish = [
        ("Kya ek animal space mein zinda reh sakta hai?", "क्या एक एनिमल स्पेस में ज़िंदा रह सकता है?", ["C1"]),
        ("Milo tardigrade se, sirf 0.5 mm ka chhota sa jeev.", "मिलो टार्डिग्रेड से, सिर्फ़ ज़ीरो पॉइंट फ़ाइव एमएम का छोटा सा जीव।", ["C2"]),
        ("Iske paas aath pair hote hain, aur har pair pe claws!", "इसके पास आठ पैर होते हैं, और हर पैर पे क्लॉज़!", ["C4"]),
        ("Ye -272 degree Celsius ki thand bhi jhel leta hai.", "ये माइनस दो सौ बहत्तर डिग्री सेल्सियस की ठंड भी झेल लेता है।", ["C3"]),
        ("Pani na ho, toh ye sookh kar cryptobiosis mein chala jaata hai.", "पानी न हो, तो ये सूख कर क्रिप्टोबायोसिस में चला जाता है।", ["C5"]),
        ("Aur space ke vacuum mein bhi ye survive kar chuka hai. Follow karo!", "और स्पेस के वैक्यूम में भी ये सर्वाइव कर चुका है। फ़ॉलो करो!", ["C1"]),
    ]
    scenes = []
    for i in range(n_scenes):
        if lang == "hinglish":
            disp, tts, cids = hinglish[i % 6]
            disp += " Sach mein kamaal hai yaar."
        else:
            disp, cids = english[i % 6]
            disp += " Isn't that amazing?"
            tts = disp
        if bad_number and i == 1:
            disp = disp.replace("0.5", "5")
            tts = tts.replace("0.5", "5") if lang != "hinglish" else tts
        scenes.append({"narration_display": disp, "narration_tts": tts,
                       "claim_ids": cids, "environment": "jungle", "props": ["creature", "planet"],
                       "mascot_action": "explain", "camera": "push_in", "on_screen_text": "WATER BEAR", "sfx": "whoosh",
                       "visual_description": "A tiny cute tardigrade floating among stars."})
    return {"title": "Space survivor", "hook_style": "question", "scenes": scenes,
            "youtube_title": "This tiny animal survived space!", "youtube_description": "Tardigrade facts.",
            "instagram_caption": "Tardigrades are tiny but tough 🐻\nWhich fact surprised you?",
            "hashtags": ["#tardigrade", "science facts"],
            "youtube_tags": ["tardigrade facts", "water bear", "#animals in space"]}


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, script: dict | None = None, audit_fail_first: bool = False):
        super().__init__({"daily_request_limit": 100})
        self.calls: list[str] = []
        self.script = script or make_script()
        self.audit_fail_first = audit_fail_first

    def available(self) -> bool:
        return True

    def _generate(self, prompt, system, json_mode, judge, temperature):
        if "candidate topics" in prompt:
            self.calls.append("rank")
            import re
            lines = re.findall(r"^(\d+)\. \[[^\]]+\] (.*)$", prompt, re.M)
            items = [{"i": int(i), "category": "animals", "fact_worthy": 9 if "Tardigrade" in t else 3,
                      "sensitive": False, "angle": "survives space", "wiki_query": "Tardigrade"} for i, t in lines]
            return json.dumps({"items": items})
        if "extract 6-10" in prompt:
            self.calls.append("extract")
            return json.dumps(CLAIMS)
        if "FULLY support" in prompt:
            self.calls.append("judge")
            ids = [line.split(":")[0].strip() for line in prompt.splitlines() if ": CLAIM:" in line]
            return json.dumps({"results": [{"id": i, "verdict": "supported"} for i in ids]})
        if "List every factual assertion" in prompt:
            self.calls.append("audit")
            if self.audit_fail_first and self.calls.count("audit") == 1:
                return json.dumps({"assertions": [{"scene": 2, "text": "made up", "supported": False}]})
            return json.dumps({"assertions": [{"scene": 1, "text": "x", "supported": True}]})
        if "Return exactly" in prompt and "text-to-video" in system:
            self.calls.append("video_prompts")
            import re
            ids = [int(x) for x in re.findall(r"^(\d+)\. \(", prompt, re.M)]
            return json.dumps({"scenes": [{"id": i, "shot": f"A tiny tardigrade floats in space, shot {i}.",
                                           "camera": "slow dolly in", "transition_hint": "fade",
                                           "music_cue": "gentle build"} for i in ids],
                               "thumbnail_prompt": "tardigrade in space",
                               "music": {"genre": "playful orchestral", "mood": "curious, bouncy", "tempo_bpm": 110,
                                         "instruments": "pizzicato strings, marimba", "energy": "builds to reveal",
                                         "search_keywords": "playful pizzicato", "avoid": "vocals"}})
        if "Write a ~" in prompt:
            self.calls.append("script")
            return json.dumps(self.script)
        raise AssertionError("unexpected prompt")


class FakeDiscovery:
    def collect(self):
        return [Candidate(title="Tardigrade", source="evergreen", wiki_title="Tardigrade", category="animals",
                          trend_score=0.3, url="https://en.wikipedia.org/wiki/Tardigrade"),
                Candidate(title="Cricket score live IPL", source="google_trends", trend_score=1.0),
                Candidate(title="New phone launch", source="google_trends", trend_score=0.9)]


class FakeFetcher:
    def gather(self, query, article_url=None, wiki_title=None, max_sources=4):
        return [Source(id="S1", url="https://en.wikipedia.org/wiki/Tardigrade", title="Tardigrade",
                       domain="wikipedia.org", tier=2, text=SOURCE_TEXT_1),
                Source(id="S2", url="https://www.esa.int/water-bears", title="Water bears", domain="esa.int",
                       tier=1, text=SOURCE_TEXT_2)]


class FakeTelegram:
    chat_id = "42"

    def __init__(self):
        self.sent: list[tuple] = []

    def send_message(self, text, reply_markup=None, chat_id=None, parse_mode=None, reply_to=None):
        self.sent.append(("msg", text, reply_markup, parse_mode))
        return [len(self.sent)]

    def download(self, file_id, dest):
        import shutil
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_id, dest)  # tests pass a local path as the file_id
        return dest

    def send_video(self, path, caption, reply_markup=None, thumb=None, **kw):
        self.sent.append(("video", str(path), caption, reply_markup))
        return 99

    def send_document(self, path, caption="", reply_markup=None):
        self.sent.append(("doc", str(path)))
        return 100

    def answer_callback(self, cid, text):
        self.sent.append(("answer", text))

    def clear_buttons(self, mid):
        pass


class FakePublisher:
    def __init__(self, db):
        self.db = db
        self.calls = 0

    def publish(self, video_id):
        self.calls += 1
        self.db.update_video(video_id, status="published")
        return {"youtube": {"status": "published", "url": "https://youtube.com/shorts/x"}}


@pytest.fixture
def cfg(tmp_path) -> Config:
    overrides = {
        "paths": {"data_dir": str(tmp_path / "data"), "output_dir": str(tmp_path / "out"),
                  "secrets_dir": str(tmp_path / "secrets")},
        "video": {"width": 360, "height": 640, "fps": 15, "min_duration": 10, "max_duration": 120,
                  "max_file_mb": 49},
        "script": {"min_words": 30, "max_words": 200},
        "tts": {"engine_order": ["tone"]},
        "render": {"engine_order": ["fallback"]},
        "subtitles": {"font_size": 32, "margin_v": 140},
        "research": {"min_verified_claims": 4},
        "approval": {"telegram": {"bot_token_env": "NOPE_TOKEN", "chat_id_env": "NOPE_CHAT"}},
        "llm": {"gemini": {"min_seconds_between_calls": 0}},
        "workflow": {"mode": "full"},
        "prompt_mode": {"inbox_dir": str(tmp_path / "inbox"), "min_duration": 3},
    }
    return Config.load(ROOT / "config" / "settings.yaml", root=ROOT, overrides=overrides)


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3")


@pytest.fixture
def llm() -> LLMChain:
    return LLMChain([FakeProvider()])
