# Daily AI Animated Facts — ₹0/month Shorts & Reels factory

Every morning at **9:00 AM IST** this system finds a trending or evergreen *interesting fact*,
verifies it against trusted sources, writes a **Hinglish** script, renders a **~60-second vertical
3D cartoon video** in Blender with a recurring mascot host, voices it with a local neural TTS,
adds music, SFX and animated subtitles, runs automated QC, and sends it to you on **Telegram**
with **Approve / Regenerate / Reject** buttons. On approval it publishes to **YouTube Shorts**
and **Instagram Reels** through their official APIs and later tracks views, likes and shares
to steer future topics and hooks.

Everything runs **locally on your Mac** with free and open-source tools. No paid APIs, no hosting.

```
 n8n / launchd (09:00 IST)
        │
        ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Discover   │──▶│ Research &   │──▶│ Hinglish     │──▶│ Piper TTS    │
 │ Trends, RSS,│   │ verify facts │   │ script (6-10 │   │ per scene    │
 │ Wikipedia   │   │ (quotes must │   │ scenes) +    │   │ (Devanagari) │
 │ + history   │   │  match text) │   │ fact audit   │   └──────┬───────┘
 └─────────────┘   └──────────────┘   └──────────────┘          ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ Telegram    │◀──│ QC: facts,   │◀──│ FFmpeg edit: │◀──│ Blender 3D   │
 │ approve /   │   │ sync, length,│   │ transitions, │   │ scenes from  │
 │ regen/reject│   │ res, black,  │   │ karaoke subs,│   │ procedural   │
 └──────┬──────┘   │ silence, size│   │ music + SFX  │   │ asset library│
        ▼          └──────────────┘   └──────────────┘   └──────────────┘
 YouTube Shorts + Instagram Reels ──▶ metrics every 6 h ──▶ topic & hook weights
```

## What's in the box

| Stage | Implementation | Free because |
|---|---|---|
| Topic discovery | Google Trends India RSS, NASA/ScienceDaily/BBC/ESA/… RSS, Wikipedia most-read & on-this-day, curated evergreen pool (`src/dva/discovery`) | public feeds |
| De-duplication | SQLite history (365 days), keyword similarity, blacklist on reject | local |
| Research & verification | Wikipedia API + its citations on trusted domains + the trend's own article; LLM must quote sources **verbatim**; quotes are string-matched, numbers cross-checked, core fact needs 2 independent domains; judge LLM checks entailment (`src/dva/research`) | Gemini free tier / local Ollama |
| Script | Gemini writes hook (≤3 s) → story → reveal → ending + CTA, subtitles in Roman Hinglish and voice text in Devanagari; validator + second-pass fact audit, auto-rewrite (`src/dva/script`) | Gemini free tier |
| 3D animation | Blender scene builder: 12 environments, 41 procedural props, the mascot **Gyaanu** (consistent channel identity), 9 camera moves, 3D keyword text, ambient particles; EEVEE on Mac GPU, Cycles fallback (`src/dva/render/blender`) | Blender (GPL) |
| Voice | Piper neural TTS, Hindi voices (fallbacks: macOS *Lekha*, espeak-ng) | open source, offline |
| Music & SFX | Generated procedurally per video with numpy — zero copyright risk (`src/dva/audio`) | local |
| Edit | FFmpeg: xfade transitions, word-by-word highlighted subtitles (ASS), progress bar, watermark, sidechain ducking, loudness normalised to −14 LUFS | FFmpeg |
| QC | resolution, codec, fps, duration, file size, decode integrity, black frames, long silences, per-scene voice presence, subtitle/voice sync, subtitle overlap/coverage, facts verified | local |
| Approval | Telegram bot (long polling: no public server) — video + full script + facts + sources + captions | Telegram Bot API |
| Publishing | YouTube Data API v3 resumable upload; Instagram Graph API resumable Reels upload (no public hosting needed) | official free APIs |
| Analytics | YouTube Data + Analytics API (views, likes, comments, shares, retention), Instagram insights → category weights + "what worked" hooks fed into the next script | official free APIs |
| Fallbacks | Ollama (local LLM) when Gemini quota is exhausted; Pillow 2.5D renderer when Blender fails; **production package (zip)** sent to Telegram when any automatic stage fails | local |

## Quick start (Mac)

```bash
git clone <this repo> ~/Daily-Video-Automation && cd ~/Daily-Video-Automation
./scripts/setup_mac.sh                 # ffmpeg, Blender, venv, Piper Hindi voice, OFL fonts, .env
.venv/bin/python -m dva demo --fast    # keyless demo video in ~2-5 min -> output/<date>_run1/final.mp4
.venv/bin/python -m dva demo           # full-quality animated demo (EEVEE on Apple GPU)
```

Then add credentials (all free) — step by step in **[docs/SETUP.md](docs/SETUP.md)**:

1. `GEMINI_API_KEY` (Google AI Studio)
2. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather)
3. YouTube OAuth client → `dva auth-youtube`
4. `INSTAGRAM_ACCESS_TOKEN` + `INSTAGRAM_USER_ID`

```bash
.venv/bin/python -m dva doctor          # shows what's ready / missing
.venv/bin/python -m dva run --force     # one real end-to-end run now
./scripts/setup_mac.sh --services       # bot + n8n (or launchd) schedule at 09:00
```

## Commands

| Command | What it does |
|---|---|
| `dva run [--force] [--json]` | the daily pipeline (idempotent per day; `--json` for n8n) |
| `dva demo [--fast] [--fallback] [--tts espeak]` | render the built-in octopus demo without API keys |
| `dva bot` | Telegram listener (Approve / Regenerate / Reject, `/status`, `/new`, `/metrics`, `/pending`) |
| `dva approve|reject|regen <id>` | same as the Telegram buttons |
| `dva publish <id>` | retry publishing an approved video (never re-uploads a published platform) |
| `dva mark-published <id> <platform> <url>` / `dva reset-publication <id> <platform>` | resolve an ambiguous upload by hand |
| `dva metrics` / `dva report` | collect and print performance |
| `dva auth-youtube` | one-time YouTube OAuth consent |
| `dva doctor` | health check: tools, voices, fonts, credentials, today's LLM quota use |
| `dva cleanup --days 14` | delete heavy intermediates of old runs |

## Reliability & safety

- **Never invents facts:** a claim survives only if the LLM's evidence quote is found in the fetched source text, all its numbers appear in that evidence, and a separate judge call agrees. The script may only cite verified claim IDs, every number it speaks must belong to a cited claim, and a final audit flags any unsupported assertion. If too little is verified, the topic is dropped and the next candidate is tried.
- **No unlicensed content:** all 3D assets, music and SFX are generated procedurally by this code. Fonts are SIL-OFL. Optional music library mode only reads files you put in `assets/music/` (use CC0 tracks).
- **Duplicate-upload prevention:** one `publications` row per (video, platform) (UNIQUE), state is written before each remote call, YouTube uploads resume via the stored session URI, identical files (SHA-256) can't be registered twice, approvals are atomic compare-and-set (double-taps do nothing), and any ambiguous crash state becomes `unknown` and is escalated to you instead of retried.
- **Quotas are finite:** each LLM provider has a self-imposed daily request cap and minimum spacing (`llm.*.daily_request_limit`, `min_seconds_between_calls`), tracked in SQLite. A run uses about 5 LLM calls. Per-platform daily upload caps are enforced too.
- **Retries** with exponential backoff and `Retry-After` for network/5xx/429 errors. **Secrets** live in `.env` (chmod 600) or the macOS Keychain (`KEY=keychain:<service>`) and are redacted from logs.
- **Modular:** each stage sits behind a small interface (`LLMProvider`, `TTSEngine`, renderer with `render_all`, `Publisher`). Add a provider by adding a class and listing it in `config/settings.yaml`.

## Project layout

```
config/settings.yaml      all tunables (channel, schedule, quotas, style, QC thresholds)
src/dva/pipeline.py       orchestration, failure → production package
src/dva/discovery/        trend/RSS/Wikipedia sources, selector (dedup + LLM rank + performance weights)
src/dva/research/         source fetching + fact verification
src/dva/script/           Hinglish script writer, validator, fact audit
src/dva/tts/              Piper / macOS say / espeak engines
src/dva/render/           Blender runner, scene_builder.py (runs inside Blender), Pillow fallback, vocab
src/dva/audio/            procedural music + SFX
src/dva/editor/           timeline, subtitles, FFmpeg compose, production package
src/dva/qc/               automated quality checks
src/dva/approval/         Telegram client + bot
src/dva/publish/          metadata, YouTube, Instagram, duplicate-safe publisher
src/dva/analytics/        metrics collection + learnings
n8n/                      importable n8n workflow (09:00 IST, metrics every 6 h, weekly cleanup)
launchd/                  macOS services (bot, n8n, or n8n-free daily job)
tests/                    offline test-suite incl. a full end-to-end run with fakes
```

## Known limits (honest list)

- **Render time:** a full-quality 60 s video is ~1,400 frames. On Apple Silicon with EEVEE expect roughly 10-40 min; set `render.blender.mode: stills` (one rendered frame per scene + camera motion) for ~2 min renders, or lower `resolution_percentage`.
- **YouTube:** uploads from an unverified Google Cloud project are locked to **private** until the project passes Google's (free) API compliance audit. The bot warns you when this happens.
- **Instagram:** requires a Professional (Business/Creator) account; Meta's app/permission setup is the most fiddly step — see docs/SETUP.md.
- **Hindi TTS quality:** Piper's Hindi voices are decent but not studio-grade, and English words are spoken via their Devanagari spelling. You can swap the voice in `tts.piper.voice`.
- **Free tiers change:** Google can change Gemini free-tier limits at any time; when it's exhausted the run falls back to Ollama (if installed) or stops with a notification — it never switches to a paid tier.
