#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
HOOK_PATH="$REPO_ROOT/.git/hooks/post-commit"
cat > "$HOOK_PATH" <<'EOF'
#!/usr/bin/env sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
TOOL_DIR="$REPO_ROOT/.agents/plugins/dev-tools"
PYTHON_BIN="${DEV_TOOLS_PYTHON:-$TOOL_DIR/.venv/bin/python}"
if [ -x "$PYTHON_BIN" ]; then
  PROJECT_ROOT="$REPO_ROOT" PYTHONPATH="$TOOL_DIR" \
    "$PYTHON_BIN" -m src.indexer >/dev/null 2>&1 || true
fi
EOF
chmod +x "$HOOK_PATH"
echo "Installed git post-commit re-index hook at $HOOK_PATH"
