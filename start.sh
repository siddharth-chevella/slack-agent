#!/bin/bash
#
# OLake Deep Research Agent - Startup Script
#
# This script:
#   1. Checks dependencies (Python, git, ripgrep, ast-grep)
#   2. Clones configured GitHub repositories
#   3. Sets up background sync (cron)
#   4. Starts the interactive CLI
#
# Usage:
#   ./start.sh
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}       OLake Deep Research Agent - Startup             ${CYAN}║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Function to print status
print_status() {
    echo -e "${BLUE}➜${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check Python
print_status "Checking dependencies..."

if ! command -v python3 &> /dev/null; then
    print_error "python3 not found. Please install Python 3.10+"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
print_success "Python: $PYTHON_VERSION"

# Check git
if ! command -v git &> /dev/null; then
    print_error "git not found. Please install git."
    exit 1
fi
GIT_VERSION=$(git --version | awk '{print $3}')
print_success "Git: $GIT_VERSION"

# Check ripgrep
if ! command -v rg &> /dev/null; then
    print_warning "ripgrep (rg) not found. Install for codebase search: brew install ripgrep"
else
    RG_VERSION=$(rg --version | head -1)
    print_success "Ripgrep: $RG_VERSION"
fi

# Check ast-grep
if ! command -v ast-grep &> /dev/null; then
    print_warning "ast-grep not found. Install for AST search: brew install ast-grep"
else
    AST_VERSION=$(ast-grep --version)
    print_success "ast-grep: $AST_VERSION"
fi

# Check Rich library
print_status "Checking Python dependencies..."
if ! python3 -c "import rich" 2>/dev/null; then
    print_warning "Installing Rich library..."
    python3 -m pip install rich --quiet
fi
print_success "Rich library installed"

# Check if .env exists
if [ ! -f ".env" ]; then
    print_warning ".env not found. Copying from .env.example..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_success ".env created. Update with your API keys."
    else
        print_error ".env.example not found. Create .env with required settings."
    fi
fi

# Check github_repos.yaml
if [ ! -f "github_repos.yaml" ]; then
    print_warning "github_repos.yaml not found. Creating default..."
    cat > github_repos.yaml << 'EOF'
# GitHub Repository Tracker Configuration
repositories:
  olake:
    url: https://github.com/datazip-inc/olake.git
    branch: main
    enabled: true
    sync_frequency: daily
EOF
    print_success "github_repos.yaml created"
fi

# Clone repositories
echo ""
print_status "Setting up repositories..."

python3 << 'PYTHON_SCRIPT'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from agent.github_repo_tracker import GitHubRepoTracker

tracker = GitHubRepoTracker()

if not tracker.repos:
    print("  No repositories configured in github_repos.yaml")
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
PYTHON_SCRIPT

# Setup cron sync
echo ""
print_status "Checking background sync..."

if crontab -l 2>/dev/null | grep -q "sync_github_repos.py"; then
    print_success "Background sync already configured"
else
    echo -e "  ${YELLOW}💡 Setup automatic repo sync? (daily at 3 AM)${NC}"
    read -p "  [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ./setup_github_sync.sh daily
        print_success "Background sync configured"
    else
        print_warning "Skipping background sync setup"
    fi
fi

# Start CLI
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}              Starting Interactive CLI                 ${GREEN}║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Use uv run for proper dependency management
if command -v uv &> /dev/null; then
    uv run python3 agent/cli_chat.py
else
    python3 agent/cli_chat.py
fi
