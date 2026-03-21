#!/bin/bash
#
# Slack Agent - Startup Script
#
# 1. Checks dependencies (Python, git, ripgrep)
# 2. Clones configured GitHub repositories
# 3. Optionally sets up background cron sync
# 4. Starts the interactive CLI
#
# Usage:
#   ./scripts/start.sh
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}            Slack Community Agent - Startup            ${CYAN}║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

print_status()  { echo -e "${BLUE}➜${NC} $1"; }
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error()   { echo -e "${RED}✗${NC} $1"; }

# ── Dependencies ──────────────────────────────────────────────────────────────
print_status "Checking dependencies..."

if ! command -v python3 &> /dev/null; then
    print_error "python3 not found. Please install Python 3.11+"
    exit 1
fi
print_success "Python: $(python3 --version 2>&1 | awk '{print $2}')"

if ! command -v git &> /dev/null; then
    print_error "git not found. Please install git."
    exit 1
fi
print_success "Git: $(git --version | awk '{print $3}')"

if ! command -v rg &> /dev/null; then
    print_warning "ripgrep (rg) not found — codebase search will be unavailable. Install: brew install ripgrep"
else
    print_success "Ripgrep: $(rg --version | head -1)"
fi

# ── Environment ───────────────────────────────────────────────────────────────
print_status "Checking configuration..."

if [ ! -f ".env" ]; then
    print_warning ".env not found — copying from .env.example..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_success ".env created. Fill in your API keys before continuing."
    else
        print_error ".env.example not found. Create .env with required settings."
        exit 1
    fi
else
    print_success ".env found"
fi

if [ ! -f "config/repos.yaml" ]; then
    print_warning "config/repos.yaml not found — no repos will be cloned."
else
    print_success "config/repos.yaml found"
fi

# ── Clone repos ───────────────────────────────────────────────────────────────
echo ""
print_status "Setting up repositories..."

python3 - << 'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from agent.github_repo_tracker import GitHubRepoTracker

tracker = GitHubRepoTracker()

if not tracker.repos:
    print("  No repositories configured in config/repos.yaml")
    sys.exit(0)

cloned = 0
for name, repo in tracker.repos.items():
    if not repo.enabled:
        continue
    path = tracker.get_repo_path(name)
    if path:
        print(f"  ✓ {name}: Already cloned")
    else:
        result = tracker.clone_repo(name)
        if result["success"]:
            print(f"  ✓ {name}: Cloned")
            cloned += 1
        else:
            print(f"  ✗ {name}: {result['message']}")

if cloned > 0:
    print(f"\n  Cloned {cloned} repository(s)")
PYEOF

# ── Background sync ───────────────────────────────────────────────────────────
echo ""
print_status "Checking background sync..."

if crontab -l 2>/dev/null | grep -q "sync_repos.py"; then
    print_success "Background sync already configured"
else
    echo -e "  ${YELLOW}💡 Set up automatic repo sync? (daily at 3 AM)${NC}"
    read -p "  [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        "$SCRIPT_DIR/setup_github_sync.sh" daily
        print_success "Background sync configured"
    else
        print_warning "Skipping background sync setup"
    fi
fi

# ── Start CLI ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}              Starting Interactive CLI                 ${GREEN}║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

if command -v uv &> /dev/null; then
    uv run python3 agent/cli_chat.py
else
    python3 agent/cli_chat.py
fi
