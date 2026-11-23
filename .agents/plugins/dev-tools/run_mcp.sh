#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
export PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="${DEV_TOOLS_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
exec "$PYTHON_BIN" -m src.mcp_server
