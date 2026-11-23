#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
echo "Setting up Tool-First Dev environment in $SCRIPT_DIR/.venv ..."
uv venv --python 3.12 "$SCRIPT_DIR/.venv"
uv pip install --python "$SCRIPT_DIR/.venv/bin/python" -r "$SCRIPT_DIR/pyproject.toml"
chmod +x "$SCRIPT_DIR/run_mcp.sh" "$SCRIPT_DIR/run_hook.sh" "$SCRIPT_DIR/install_git_hook.sh"
echo "Done. Running initial knowledge-graph index..."
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
PROJECT_ROOT="$REPO_ROOT" PYTHONPATH="$SCRIPT_DIR" "$SCRIPT_DIR/.venv/bin/python" -m src.indexer --full
