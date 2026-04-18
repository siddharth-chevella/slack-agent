#!/usr/bin/env python3
"""
GitHub Repositories Sync Script

Designed to run as a cron job for background synchronization.
Runs ``git pull`` in each tracked clone (see GitHubRepoTracker.sync_repo).

Logs go to LOG_DIR/repo_logs/YYYY-MM-DD.log (IST) via Python; optional stdout for cron.

Usage:
  python scripts/sync_repos.py [--verbose]

Cron example (daily at 3 AM):
  0 3 * * * cd /path/to/slack-agent && .venv/bin/python scripts/sync_repos.py
"""

import logging
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv(project_root / ".env")

from agent.repo_sync_log import RepoSyncDailyFileHandler
from agent.github_repo_tracker import GitHubRepoTracker

log_dir = os.getenv("LOG_DIR", "logs")

_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_root = logging.getLogger()
_root.handlers.clear()
_root.setLevel(logging.INFO)

_repo_h = RepoSyncDailyFileHandler(log_dir)
_repo_h.setFormatter(_fmt)
_root.addHandler(_repo_h)

_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_sh)

log = logging.getLogger(__name__)


def main() -> int:
    """Sync all tracked GitHub repositories."""
    verbose = "--verbose" in sys.argv

    log.info("=" * 60)
    log.info("Starting GitHub repositories sync")
    log.info("=" * 60)

    tracker = GitHubRepoTracker()

    if not tracker.repos:
        log.warning("No repositories configured in config/repos.yaml")
        log.info("Add repos to config/repos.yaml and run:")
        log.info("  python agent/github_repo_tracker.py add <name> <url>")
        return 0

    log.info("Found %d configured repositories", len(tracker.repos))

    result = tracker.sync_all()

    log.info("-" * 60)
    log.info("Sync completed: %s/%s successful", result["synced"], result["total"])

    if result["errors"] > 0:
        log.error("Errors: %s", result["errors"])

    if verbose:
        for name, repo_result in result.get("results", {}).items():
            status = "✓" if repo_result["success"] else "✗"
            log.info("  %s %s: %s", status, name, repo_result["message"])

    log.info("=" * 60)

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
