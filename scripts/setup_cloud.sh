#!/usr/bin/env bash
# ONE-TIME cloud setup, run on any computer (your Mac). After this the Mac can stay off:
# everything runs on GitHub Actions and you only use Telegram (+ Google Drive) on your phone.
#
# Before running: fill in .env (GEMINI_API_KEY, TELEGRAM_*, INSTAGRAM_*) and put the Google OAuth client
# at secrets/youtube_client_secret.json (docs/CLOUD.md, steps 1-5).
set -euo pipefail
cd "$(dirname "$0")/.."
say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

[ -f .env ] || { echo "Missing .env: copy .env.example to .env and fill it in first."; exit 1; }
[ -f secrets/youtube_client_secret.json ] || { echo "Missing secrets/youtube_client_secret.json (docs/CLOUD.md step 4)."; exit 1; }
command -v brew >/dev/null || { echo "Homebrew is required: https://brew.sh"; exit 1; }
command -v gh >/dev/null || brew install gh
if [ ! -d .venv ]; then
  say "Creating Python environment"
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
  "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
  .venv/bin/pip install -q -e .
fi

say "Logging in to GitHub (choose GitHub.com → HTTPS → login with a web browser)"
gh auth status >/dev/null 2>&1 || gh auth login -h github.com -p https -w
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "Repository: $REPO"

say "Google login (YouTube upload + stats + read your Drive inbox folder)"
if [ ! -f secrets/youtube_token.json ] || [ "${1:-}" = "--reauth" ]; then
  .venv/bin/python -m dva auth-youtube
fi

if [ ! -s secrets/state_key.txt ]; then
  umask 077; openssl rand -base64 32 | tr -d '\n' > secrets/state_key.txt
  echo "Created secrets/state_key.txt (encrypts the cloud database; keep a copy somewhere safe)"
fi

DRIVE_ID="$(grep -E '^DVA_DRIVE_FOLDER_ID=' .env | cut -d= -f2- || true)"
if [ -z "$DRIVE_ID" ]; then
  echo
  echo "Optional: Google Drive folder for videos bigger than 20 MB."
  echo "Create a folder named 'DVA Inbox' in Google Drive, open it, and copy the last part of the link"
  echo "(https://drive.google.com/drive/folders/THIS_PART). Press Enter to skip."
  read -r -p "Drive folder id: " DRIVE_ID
  [ -n "$DRIVE_ID" ] && echo "DVA_DRIVE_FOLDER_ID=$DRIVE_ID" >> .env
fi

say "Uploading secrets to GitHub (encrypted; never visible in the repo)"
while IFS='=' read -r key value; do
  case "$key" in ''|\#*) continue ;; esac
  value="${value%\"}"; value="${value#\"}"
  [ -n "$value" ] || continue
  printf '%s' "$value" | gh secret set "$key" -R "$REPO"
  echo "  set $key"
done < .env
gh secret set YOUTUBE_CLIENT_SECRET_JSON -R "$REPO" < secrets/youtube_client_secret.json && echo "  set YOUTUBE_CLIENT_SECRET_JSON"
gh secret set YOUTUBE_TOKEN_JSON -R "$REPO" < secrets/youtube_token.json && echo "  set YOUTUBE_TOKEN_JSON"
gh secret set DVA_STATE_KEY -R "$REPO" < secrets/state_key.txt && echo "  set DVA_STATE_KEY"

say "Stopping any local background services (only one bot may read Telegram)"
for f in com.dva.bot com.dva.daily com.dva.metrics com.dva.n8n; do
  launchctl unload ~/Library/LaunchAgents/$f.plist 2>/dev/null && echo "  stopped $f" || true
done

say "Starting the first cloud run"
gh workflow enable dva.yml -R "$REPO" >/dev/null 2>&1 || true
if gh workflow run dva.yml -R "$REPO" -f command=poll; then
  echo "Started. Watch it: gh run watch -R $REPO   (or the Actions tab on github.com)"
else
  echo "Could not start it: the workflow must be on the default branch (merge the PR into main first)."
fi
echo
echo "Done. From now on: Telegram on your phone is all you need. Send /status to your bot to check."
