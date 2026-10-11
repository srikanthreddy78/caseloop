#!/usr/bin/env bash
# Start the API from any working directory. Usage: scripts/devserver.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run uvicorn app.main:app --port "${1:-8000}"
