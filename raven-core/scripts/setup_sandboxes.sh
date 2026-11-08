#!/usr/bin/env bash
# Read-only by default. Preparation/creation is always an explicit command.
#   ./scripts/setup_sandboxes.sh
#   ./scripts/setup_sandboxes.sh linux
#   ./scripts/setup_sandboxes.sh linux --create
# Native macOS/DeviceHub are the defaults; no background VM creation or downloads.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" == all ]]; then
  shift
  set -- inspect "$@"
fi
exec uv run python scripts/sandbox_setup.py "$@"
