#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon or Intel). Everything installed here is free.
#   ./scripts/setup_mac.sh            # core install
#   ./scripts/setup_mac.sh --services # also install launchd services (bot + n8n or daily job)
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
VOICE="${DVA_VOICE:-hi_IN-pratham-medium}"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh"; exit 1
fi

say "Installing ffmpeg + Python (Homebrew)"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12

if [ ! -d /Applications/Blender.app ]; then
  say "Installing Blender (free, open source) — ~1 GB"
  brew install --cask blender || echo "Blender install failed; the fallback 2.5D renderer will be used."
fi

say "Creating virtualenv"
PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[tts,dev]"

say "Downloading Piper Hindi voice: $VOICE"
mkdir -p assets/voices
lang="${VOICE%%-*}"; rest="${VOICE#*-}"; name="${rest%%-*}"; quality="${rest#*-}"
base="https://huggingface.co/rhasspy/piper-voices/resolve/main/${lang%%_*}/${lang}/${name}/${quality}/${VOICE}"
for ext in onnx onnx.json; do
  [ -f "assets/voices/${VOICE}.${ext}" ] || curl -fL --retry 3 -o "assets/voices/${VOICE}.${ext}" "${base}.${ext}?download=true"
done

say "Downloading Poppins font (SIL Open Font License)"
mkdir -p assets/fonts
for w in ExtraBold Bold; do
  [ -f "assets/fonts/Poppins-${w}.ttf" ] || curl -fL --retry 3 -o "assets/fonts/Poppins-${w}.ttf" \
    "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-${w}.ttf"
done

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Created .env — fill in GEMINI_API_KEY and TELEGRAM_* (see docs/SETUP.md)"
fi
mkdir -p secrets data/logs && chmod 700 secrets

# n8n workflow with this checkout's real path baked in
sed "s#\$HOME/Daily-Video-Automation#${PROJECT_DIR}#g" n8n/daily_facts_workflow.json > n8n/daily_facts_workflow.local.json

say "Health check"
.venv/bin/python -m dva doctor || true

if [[ "${1:-}" == "--services" ]]; then
  say "Installing launchd services"
  mkdir -p ~/Library/LaunchAgents
  for f in launchd/com.dva.bot.plist launchd/com.dva.metrics.plist; do
    dest=~/Library/LaunchAgents/$(basename "$f")
    sed "s#__PROJECT_DIR__#${PROJECT_DIR}#g" "$f" > "$dest"
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load "$dest"
  done
  if command -v n8n >/dev/null; then
    dest=~/Library/LaunchAgents/com.dva.n8n.plist
    sed "s#__PROJECT_DIR__#${PROJECT_DIR}#g" launchd/com.dva.n8n.plist > "$dest"
    launchctl unload "$dest" 2>/dev/null || true; launchctl load "$dest"
    echo "n8n running at http://localhost:5678 — import n8n/daily_facts_workflow.local.json and activate it."
  else
    dest=~/Library/LaunchAgents/com.dva.daily.plist
    sed "s#__PROJECT_DIR__#${PROJECT_DIR}#g" launchd/com.dva.daily.plist > "$dest"
    launchctl unload "$dest" 2>/dev/null || true; launchctl load "$dest"
    echo "n8n not found: installed the launchd daily job instead (09:00 local time)."
  fi
  echo "Tip: wake the Mac before the run:  sudo pmset repeat wakeorpoweron MTWRFSU 08:50:00"
fi

say "Done. Try a keyless demo video:  .venv/bin/python -m dva demo --fast"
