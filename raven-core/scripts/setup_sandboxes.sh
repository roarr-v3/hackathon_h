#!/usr/bin/env bash
# Idempotent Raven sandbox setup (macOS host, Apple Silicon).
#   ./scripts/setup_sandboxes.sh            # linux (+ kicks off macOS in background)
#   ./scripts/setup_sandboxes.sh linux      # only linux
#   ./scripts/setup_sandboxes.sh macos      # only macOS (foreground; ~23 GB first pull)
#   ./scripts/setup_sandboxes.sh windows    # check the remote Windows worker
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts
WHAT="${1:-all}"

# 1. Cua CLI (optional, handy for `cua sb ls` / debugging) and the python SDK (via uv)
if ! command -v cua >/dev/null 2>&1; then
  echo "== installing cua CLI"
  curl -fsSL https://cua.ai/install.sh | sh -s -- --cli-only || echo "cua CLI install failed (SDK still works)"
  export PATH="$HOME/.local/bin:$HOME/.cua/bin:$PATH"
fi
uv sync

# 2. Docker runtime for the linux container
if [[ "$WHAT" == all || "$WHAT" == linux ]]; then
  if ! docker info >/dev/null 2>&1; then
    echo "== starting colima"; colima start --cpu 4 --memory 8
  fi
  docker pull ghcr.io/trycua/linux:24.04 >/dev/null 2>&1 || true
  echo "== linux sandbox"
  uv run python scripts/sandbox_setup.py linux
  uv run python scripts/sandbox_setup.py shot linux || true
fi

# 3. macOS Lume VM (long download) — background unless asked explicitly
if [[ "$WHAT" == macos ]]; then
  command -v cua >/dev/null && cua runtime setup lume || true
  uv run python scripts/sandbox_setup.py macos
elif [[ "$WHAT" == all ]]; then
  echo "== macOS VM setup started in background -> artifacts/macos-setup.log"
  ( (command -v cua >/dev/null && cua runtime setup lume || true); uv run python scripts/sandbox_setup.py macos ) \
    > artifacts/macos-setup.log 2>&1 &
fi

# 4. Windows (remote cua-spacesd over Tailscale; see workers/windows/README.md)
if [[ "$WHAT" == windows ]]; then
  uv run python scripts/sandbox_setup.py windows
fi
echo "== done ($WHAT)"
