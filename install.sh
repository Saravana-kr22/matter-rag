#!/bin/bash
# Matter RAG Pipeline — Setup Script
#
# Installs all dependencies needed to run the pipeline on macOS or Linux.
# Run from the project root: ./install.sh
#
# What it installs:
#   - Python virtual environment + pip dependencies
#   - Asciidoctor (for adoc processing)
#   - Docker Desktop check (required for --pr-url mode)
#   - HuggingFace embedding model (downloaded on first pipeline run)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---------------------------------------------------------------------------
# Detect OS
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *)      error "Unsupported OS: $OS"; exit 1 ;;
esac
info "Detected platform: $PLATFORM"

# ---------------------------------------------------------------------------
# Check Python
# ---------------------------------------------------------------------------
PYTHON=""
for cmd in python3.11 python3.12 python3.13 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.11+ is required but not found."
    if [ "$PLATFORM" = "macos" ]; then
        echo "  Install with: brew install python@3.11"
    else
        echo "  Install with: sudo apt install python3.11 python3.11-venv"
    fi
    exit 1
fi
info "Using Python: $PYTHON ($($PYTHON --version))"

# ---------------------------------------------------------------------------
# Check/Install Asciidoctor
# ---------------------------------------------------------------------------
if command -v asciidoctor &>/dev/null; then
    info "Asciidoctor found: $(asciidoctor --version | head -1)"
else
    warn "Asciidoctor not found — installing..."
    if [ "$PLATFORM" = "macos" ]; then
        if command -v brew &>/dev/null; then
            brew install asciidoctor
        else
            error "Homebrew not found. Install asciidoctor manually: gem install asciidoctor"
            exit 1
        fi
    else
        if command -v apt-get &>/dev/null; then
            sudo apt-get update && sudo apt-get install -y asciidoctor
        elif command -v gem &>/dev/null; then
            sudo gem install asciidoctor
        else
            error "Cannot install asciidoctor. Install manually: gem install asciidoctor"
            exit 1
        fi
    fi
    info "Asciidoctor installed."
fi

# ---------------------------------------------------------------------------
# Check Docker (optional — needed for --pr-url mode)
# ---------------------------------------------------------------------------
if command -v docker &>/dev/null; then
    if docker info &>/dev/null 2>&1; then
        info "Docker is running."
    else
        warn "Docker is installed but not running. Start Docker Desktop for --pr-url mode."
    fi
else
    warn "Docker not found. Required only for --pr-url mode (auto-generating diff HTML from spec PRs)."
    if [ "$PLATFORM" = "macos" ]; then
        echo "  Install from: https://www.docker.com/products/docker-desktop/"
    else
        echo "  Install with: sudo apt-get install docker.io"
    fi
fi

# ---------------------------------------------------------------------------
# Check Claude CLI (optional — needed for claude_subprocess provider)
# ---------------------------------------------------------------------------
if command -v claude &>/dev/null; then
    info "Claude CLI found: $(claude --version 2>&1 | head -1)"
else
    warn "Claude CLI not found. Required for the default LLM provider (claude_subprocess)."
    echo "  Install from: https://docs.anthropic.com/claude-code/getting-started"
    echo "  Or switch to claude_cli provider (requires ANTHROPIC_API_KEY)."
fi

# ---------------------------------------------------------------------------
# Create virtual environment
# ---------------------------------------------------------------------------
VENV_DIR=".venv"
if [ -d "$VENV_DIR" ]; then
    info "Virtual environment already exists: $VENV_DIR"
else
    info "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
    info "Virtual environment created: $VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"
info "Activated virtual environment."

# ---------------------------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------------------------
info "Installing Python dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
info "Python dependencies installed."

# ---------------------------------------------------------------------------
# Create data directories
# ---------------------------------------------------------------------------
info "Creating data directories..."
mkdir -p data/data_model
mkdir -p data/test_plans
mkdir -p data/matter_spec
mkdir -p data/input_doc
mkdir -p data/knowledge_graph
mkdir -p data/faiss_index
mkdir -p data/cache
mkdir -p reports
mkdir -p logs
info "Data directories ready."

# ---------------------------------------------------------------------------
# Create .env template if not exists
# ---------------------------------------------------------------------------
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# Matter RAG Pipeline — Environment Variables
# Uncomment and set the variables you need.

# GitHub token (required for --pr-url mode and private spec repos)
# GITHUB_TOKEN=ghp_your_token_here

# Anthropic API key (only for claude_cli provider)
# ANTHROPIC_API_KEY=sk-ant-...

# Google Gemini API key (only for gemini provider)
# GEMINI_API_KEY=AIza...
EOF
    info "Created .env template. Edit it with your credentials."
else
    info ".env file already exists."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "  Virtual environment: $VENV_DIR (activate with: source $VENV_DIR/bin/activate)"
echo ""
echo "  Next steps:"
echo "    1. Edit .env with your credentials (GITHUB_TOKEN, etc.)"
echo "    2. Place data files in data/ (see README.md for details)"
echo "    3. Run the pipeline:"
echo ""
echo "       # From a spec PR (requires Docker + GITHUB_TOKEN):"
echo "       source .env"
echo "       python scripts/run_ghpr_analysis.py \\"
echo "         --build-test-plan-vectors --build-knowledge-graph \\"
echo "         --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \\"
echo "         --spec-repo /path/to/connectedhomeip-spec"
echo ""
echo "       # From a local diff HTML:"
echo "       python scripts/run_ghpr_analysis.py \\"
echo "         --build-test-plan-vectors --build-knowledge-graph \\"
echo "         --input-doc data/input_doc/appclusters_diff.html"
echo ""
echo "============================================================"
