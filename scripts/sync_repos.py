#!/usr/bin/env python3
"""
GitHub Repositories Sync Script

Designed to run as a cron job for background synchronization.
Pulls latest changes for all tracked repositories.

Usage:
  python scripts/sync_repos.py [--verbose]

Cron example (daily at 3 AM):
  0 3 * * * cd /path/to/slack-agent && python scripts/sync_repos.py >> logs/github_sync.log 2>&1
"""

import sys
import logging
from pathlib import Path

# scripts/ is one level below the project root
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.github_repo_tracker import GitHubRepoTracker

# Configure logging
log_dir = project_root / "logs"
log_dir.mkdir(exist_ok=True)

log_file = log_dir / "github_sync.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
    ],
)

log = logging.getLogger(__name__)


def main():
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
    
    log.info(f"Found {len(tracker.repos)} configured repositories")
    
    # Sync all repos
    result = tracker.sync_all()
    
    # Log summary
    log.info("-" * 60)
    log.info(f"Sync completed: {result['synced']}/{result['total']} successful")
    
    if result["errors"] > 0:
        log.error(f"Errors: {result['errors']}")
    
    if verbose:
        for name, repo_result in result.get("results", {}).items():
            status = "✓" if repo_result["success"] else "✗"
            log.info(f"  {status} {name}: {repo_result['message']}")
    
    log.info("=" * 60)
    
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
