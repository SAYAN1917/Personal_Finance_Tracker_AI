#!/usr/bin/env bash
# Nightly backup: dump the DB, compress, send to your Telegram Saved Messages.
# Usage: TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy ./scripts/backup.sh
# Schedule with cron: 0 3 * * * /workspace/service/scripts/backup.sh
# Restore (sqlite3 .backup produces a binary file, NOT a SQL dump):
#   gunzip -c backup.sql.gz > /tmp/finance.backup
#   sqlite3 finance.db ".restore '/tmp/finance.backup'"

set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${DATABASE_URL:-sqlite:///$SERVICE_DIR/finance.db}"

# Translate DATABASE_URL into a plain sqlite path
if [[ "$DB_PATH" == sqlite:///* ]]; then
    DB_FILE="${DB_PATH#sqlite:///}"
else
    echo "Only sqlite backups supported by this script." >&2
    exit 1
fi

if [[ ! -f "$DB_FILE" ]]; then
    echo "No database at $DB_FILE - nothing to back up." >&2
    exit 1
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required." >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
STAMP="$(date +%Y%m%d-%H%M)"
DUMP="$TMP_DIR/finance-$STAMP.sql"
GZIP="$TMP_DIR/finance-$STAMP.sql.gz"

# Note: not enabling WAL or live-mode here; the app uses SQLite so a plain
# .dump is safe enough for a nightly snapshot. For Postgres this becomes
# `pg_dump finance | gzip`.
sqlite3 "$DB_FILE" ".backup '$DUMP'"
gzip -c "$DUMP" > "$GZIP"
SIZE="$(du -h "$GZIP" | cut -f1)"

# Upload to Telegram Saved Messages (private to you).
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendDocument" \
    -F "chat_id=$CHAT_ID" \
    -F "document=@$GZIP" \
    -F "caption=Finance backup $STAMP ($SIZE)" > /dev/null

echo "Backup sent: $GZIP ($SIZE)"
rm -rf "$TMP_DIR"
