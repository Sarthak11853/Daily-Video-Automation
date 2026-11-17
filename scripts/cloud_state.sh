#!/usr/bin/env bash
# Persist the SQLite database between GitHub Actions runs, AES-256 encrypted with DVA_STATE_KEY,
# as a single-commit orphan branch "dva-state" (old states are overwritten, so the repo stays small).
#   cloud_state.sh pull   # before the run
#   cloud_state.sh push   # after the run
set -euo pipefail
DB="data/dva.sqlite3"
BRANCH="dva-state"
FILE="state.db.enc"
: "${DVA_STATE_KEY:?DVA_STATE_KEY secret is missing (see docs/CLOUD.md)}"
mkdir -p data
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

case "${1:-}" in
  pull)
    if git fetch -q --depth=1 origin "$BRANCH" 2>/dev/null; then
      git show "FETCH_HEAD:$FILE" > "$TMP"
      openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$TMP" -out "$DB" -pass env:DVA_STATE_KEY
      echo "state restored ($(wc -c < "$DB") bytes)"
    else
      echo "no saved state yet: starting fresh"
    fi
    ;;
  push)
    [ -f "$DB" ] || { echo "no database to save"; exit 0; }
    python - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
PY
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$DB" -out "$TMP" -pass env:DVA_STATE_KEY
    export GIT_AUTHOR_NAME="dva-bot" GIT_AUTHOR_EMAIL="dva-bot@users.noreply.github.com"
    export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
    blob=$(git hash-object -w "$TMP")
    tree=$(printf '100644 blob %s\t%s\n' "$blob" "$FILE" | git mktree)
    commit=$(git commit-tree "$tree" -m "state $(date -u +%FT%TZ)")
    for i in 1 2 3 4; do
      git push -q -f origin "$commit:refs/heads/$BRANCH" && { echo "state saved"; exit 0; }
      sleep $((i * 5))
    done
    echo "could not save state" >&2; exit 1
    ;;
  *) echo "usage: $0 pull|push" >&2; exit 2 ;;
esac
