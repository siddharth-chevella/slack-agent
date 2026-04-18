#!/bin/bash
#
# Setup GitHub Repos Sync Cron Job
#
# Installs a cron job to automatically sync tracked GitHub repositories.
#
# Usage:
#   ./scripts/setup_github_sync.sh [daily|hourly|weekly]
#
# Defaults to daily sync at 3:00 AM if no argument provided.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC_SCRIPT="$PROJECT_ROOT/scripts/sync_repos.py"
LOG_DIR="$PROJECT_ROOT/logs"
REPO_LOGS_DIR="$LOG_DIR/repo_logs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=============================================="
echo "GitHub Repos Sync - Cron Setup"
echo "=============================================="

mkdir -p "$LOG_DIR" "$REPO_LOGS_DIR"

FREQUENCY="${1:-daily}"

case "$FREQUENCY" in
    hourly)
        CRON_SCHEDULE="0 * * * *"
        echo "Schedule: Hourly (at minute 0)"
        ;;
    daily)
        CRON_SCHEDULE="0 3 * * *"
        echo "Schedule: Daily at 3:00 AM"
        ;;
    weekly)
        CRON_SCHEDULE="0 3 * * 0"
        echo "Schedule: Weekly on Sunday at 3:00 AM"
        ;;
    *)
        echo -e "${RED}Error: Invalid frequency. Use: hourly, daily, or weekly${NC}"
        exit 1
        ;;
esac

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 not found${NC}"
    exit 1
fi

PYTHON_PATH="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_PATH" ]; then
  PYTHON_PATH=$(which python3)
fi
# sync_repos.py writes to logs/repo_logs/YYYY-MM-DD.log (IST); no shell redirect needed
CRON_CMD="$CRON_SCHEDULE cd $PROJECT_ROOT && $PYTHON_PATH $SYNC_SCRIPT"

echo ""
echo "Project root: $PROJECT_ROOT"
echo "Sync script:  $SYNC_SCRIPT"
echo "Repo logs:    $REPO_LOGS_DIR/<YYYY-MM-DD>.log (IST)"
echo "Python:       $PYTHON_PATH"
echo ""

TEMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TEMP_CRON" || true

if grep -q "sync_repos.py" "$TEMP_CRON" 2>/dev/null; then
    echo -e "${YELLOW}Warning: GitHub sync cron job already exists — replacing...${NC}"
    grep -v "sync_repos.py" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
    mv "${TEMP_CRON}.new" "$TEMP_CRON"
fi

echo "" >> "$TEMP_CRON"
echo "# GitHub Repos Sync - Added by setup_github_sync.sh on $(date)" >> "$TEMP_CRON"
echo "$CRON_CMD" >> "$TEMP_CRON"

crontab "$TEMP_CRON"
rm -f "$TEMP_CRON"

echo -e "${GREEN}✓ Cron job installed successfully!${NC}"
echo ""
echo "Verify:      crontab -l"
echo "Remove:      ./scripts/remove_github_sync.sh"
echo "Manual sync: python3 scripts/sync_repos.py"
echo "View logs:   ls -la $REPO_LOGS_DIR && tail -f $REPO_LOGS_DIR/*.log"
echo "=============================================="
