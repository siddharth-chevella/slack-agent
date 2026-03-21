#!/bin/bash
#
# Remove GitHub Repos Sync Cron Job
#

set -e

echo "Removing GitHub sync cron job..."

TEMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TEMP_CRON" || true

if grep -q "sync_repos.py" "$TEMP_CRON" 2>/dev/null; then
    grep -v "sync_repos.py" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
    grep -v "GitHub Repos Sync" "${TEMP_CRON}.new" > "$TEMP_CRON" || true
    crontab "$TEMP_CRON"
    echo "✓ Cron job removed"
else
    echo "No GitHub sync cron job found"
fi

rm -f "$TEMP_CRON" "${TEMP_CRON}.new"
