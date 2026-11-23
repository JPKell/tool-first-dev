#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
export PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="${DEV_TOOLS_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  # Toolkit not set up yet (run setup.sh). Fail open silently so every tool call
  # doesn't surface a hook error; guardrails/telemetry simply stay inactive.
  cat >/dev/null
  echo '{}'
  exit 0
fi
exec "$PYTHON_BIN" -m src.hook_handler "$@"
