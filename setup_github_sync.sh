#!/bin/bash
#
# Setup GitHub Repos Sync Cron Job
#
# This script installs a cron job to automatically sync tracked GitHub
# repositories in the background.
#
# Usage:
#   ./setup_cron.sh [daily|hourly|weekly]
#
# Defaults to daily sync at 3:00 AM if no argument provided.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
SYNC_SCRIPT="$PROJECT_ROOT/sync_github_repos.py"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/github_sync.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=============================================="
echo "GitHub Repos Sync - Cron Setup"
echo "=============================================="

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# Get sync frequency
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

# Create cron job command
CRON_CMD="$CRON_SCHEDULE cd $PROJECT_ROOT && python3 $SYNC_SCRIPT >> $LOG_FILE 2>&1"

echo ""
echo "Project root: $PROJECT_ROOT"
echo "Sync script: $SYNC_SCRIPT"
echo "Log file: $LOG_FILE"
echo ""

# Check if Python3 is available
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 not found${NC}"
    exit 1
fi

PYTHON_PATH=$(which python3)
echo "Python: $PYTHON_PATH"

# Update cron command with full python path
CRON_CMD="$CRON_SCHEDULE cd $PROJECT_ROOT && $PYTHON_PATH $SYNC_SCRIPT >> $LOG_FILE 2>&1"

# Check current crontab
echo ""
echo "Checking existing cron jobs..."

# Create temp file for crontab
TEMP_CRON=$(mktemp)

# Get current crontab or create empty
crontab -l 2>/dev/null > "$TEMP_CRON" || true

# Check if our cron job already exists
if grep -q "sync_github_repos.py" "$TEMP_CRON" 2>/dev/null; then
    echo -e "${YELLOW}Warning: GitHub sync cron job already exists${NC}"
    echo "Removing existing job..."
    grep -v "sync_github_repos.py" "$TEMP_CRON" > "${TEMP_CRON}.new" || true
    mv "${TEMP_CRON}.new" "$TEMP_CRON"
fi

# Add new cron job
echo "" >> "$TEMP_CRON"
echo "# GitHub Repos Sync - Added by setup_cron.sh on $(date)" >> "$TEMP_CRON"
echo "$CRON_CMD" >> "$TEMP_CRON"

# Install crontab
echo ""
echo "Installing cron job..."
crontab "$TEMP_CRON"

# Cleanup
rm -f "$TEMP_CRON"

echo ""
echo -e "${GREEN}✓ Cron job installed successfully!${NC}"
echo ""
echo "To verify, run: crontab -l"
echo "To remove, run: ./setup_cron.sh remove"
echo ""
echo "Manual sync: python3 sync_github_repos.py"
echo "View logs:   tail -f $LOG_FILE"
echo ""
echo "=============================================="
