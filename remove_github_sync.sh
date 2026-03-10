#!/bin/bash
#
# Remove GitHub Repos Sync Cron Job
#

set -e

echo "Removing GitHub sync cron job..."

# Get current crontab
TEMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TEMP_CRON" || true

# Remove our cron job
if grep -q "sync_github_repos.py" "$TEMP_CRON" 2>/dev/null; then
    grep -v "sync_github_repos.py" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
    grep -v "GitHub Repos Sync" "$TEMP_CRON.new" > "$TEMP_CRON" || true
    crontab "$TEMP_CRON"
    echo "✓ Cron job removed"
else
    echo "No GitHub sync cron job found"
fi

# Cleanup
rm -f "$TEMP_CRON" "${TEMP_CRON}.new"
