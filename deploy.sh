#!/usr/bin/env bash
set -euo pipefail

TEMPLATE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPOS_BASE="${HOME}/repos"

# --- Pretty helpers ---
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

prompt() {
  local question="$1"
  local default="${2:-}"
  local reply=""
  if [ -n "$default" ]; then
    printf '\n%s [%s]: ' "$question" "$default" >&2
  else
    printf '\n%s: ' "$question" >&2
  fi
  IFS= read -r reply || true
  if [ -z "$reply" ]; then
    printf '%s' "$default"
  else
    printf '%s' "$reply"
  fi
}

prompt_yn() {
  local question="$1"
  local default="${2:-y}" # y or n
  local hint="Y/n"
  [ "$default" = "n" ] && hint="y/N"
  local reply=""
  printf '\n%s [%s]: ' "$question" "$hint" >&2
  IFS= read -r reply || true
  reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
  if [ -z "$reply" ]; then
    [ "$default" = "y" ]
    return
  fi
  case "$reply" in
    y|yes) return 0 ;;
    *)     return 1 ;;
  esac
}

expand_path() {
  local raw="$1"
  # Expand leading ~
  case "$raw" in
    "~")   raw="$HOME" ;;
    "~/"*) raw="$HOME/${raw#~/}" ;;
  esac
  # If user typed just a folder name (no slash), place it under ~/repos/
  if [[ "$raw" != */* ]] && [[ "$raw" != "." ]] && [[ "$raw" != ".." ]]; then
    raw="${REPOS_BASE}/${raw}"
  fi
  python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$raw"
}

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
}

bold "=== Tool-First Development Deployment Wizard ==="
info "Template source: $TEMPLATE_DIR"

# Show existing folders in ~/repos as quick reference
if [ -d "$REPOS_BASE" ]; then
  printf '\nExisting folders in %s:\n' "$REPOS_BASE"
  for d in "$REPOS_BASE"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [ "$name" = "$(basename "$TEMPLATE_DIR")" ] && continue
    printf '  - %s\n' "$name"
  done
fi

# 1. Target directory
TARGET_RAW=""
while [ -z "$TARGET_RAW" ]; do
  TARGET_RAW="$(prompt "Target project folder (name in ~/repos or full path)" "")"
done
TARGET_DIR="$(expand_path "$TARGET_RAW")"

if [ "$TARGET_DIR" = "$TEMPLATE_DIR" ]; then
  echo "Error: Target directory cannot be the template directory itself." >&2
  exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
  if prompt_yn "Directory '$TARGET_DIR' does not exist. Create it now?" "y"; then
    mkdir -p "$TARGET_DIR"
    ok "Created $TARGET_DIR"
  else
    echo "Aborted."
    exit 1
  fi
fi

# 2. Project metadata
DEFAULT_NAME="$(basename "$TARGET_DIR")"
PROJECT_NAME="$(prompt "Project name" "$DEFAULT_NAME")"
PROJECT_SLUG="$(slugify "$PROJECT_NAME")"
[ -z "$PROJECT_SLUG" ] && PROJECT_SLUG="project"

PROJECT_DESC="$(prompt "One-line project description (for AGENTS.md)" "Software project using Tool-First Development.")"

# Smart default for source directories
DEFAULT_SRC_DIRS="app, tests"
DEFAULT_HDR_DIRS="app"
if [ -d "$TARGET_DIR/src" ] && [ ! -d "$TARGET_DIR/app" ]; then
  DEFAULT_SRC_DIRS="src, tests"
  DEFAULT_HDR_DIRS="src"
fi

SOURCE_DIRS_CSV="$(prompt "Source directories to index (comma-separated)" "$DEFAULT_SRC_DIRS")"
HEADER_DIRS_CSV="$(prompt "Directories requiring module headers (comma-separated)" "$DEFAULT_HDR_DIRS")"
EXTENSIONS_CSV="$(prompt "File extensions to index (comma-separated)" ".py, .js")"
SUITE_APP="$(prompt "OpenWeightGym suite application tag" "${PROJECT_SLUG}-dev-tools")"

# 3. Overwrite policy for existing files
OVERWRITE_ROOT="false"
EXISTING_ROOT=()
for f in AGENTS.md CLAUDE.md .mcp.json .env.example DevDockerfile .dockerignore .agents/RULES.md .agents/config.json .agents/ruff.toml; do
  [ -e "$TARGET_DIR/$f" ] && EXISTING_ROOT+=("$f")
done
if [ "${#EXISTING_ROOT[@]}" -gt 0 ]; then
  warn "Target already has: ${EXISTING_ROOT[*]}"
  if prompt_yn "Overwrite existing root/config files? (plugin code in .agents/plugins/dev-tools is always updated)" "n"; then
    OVERWRITE_ROOT="true"
  fi
else
  OVERWRITE_ROOT="true"
fi

# 4. DevDockerfile
INCLUDE_DOCKER="false"
APP_PORT="8000"
INSTALL_PLAYWRIGHT="false"
if prompt_yn "Include multi-agent DevDockerfile and .dockerignore?" "y"; then
  INCLUDE_DOCKER="true"
  APP_PORT="$(prompt "App port to expose in DevDockerfile" "8000")"
  if prompt_yn "Include Playwright + Chromium in DevDockerfile?" "n"; then
    INSTALL_PLAYWRIGHT="true"
  fi
fi

# 5. .gitignore
UPDATE_GITIGNORE="false"
if prompt_yn "Ensure .agents/state/ and plugin .venv are in target .gitignore?" "y"; then
  UPDATE_GITIGNORE="true"
fi

# 6. .env / OpenWeightGym settings
ENV_MODE="skip"
ENV_COPY_FROM=""
if [ ! -f "$TARGET_DIR/.env" ]; then
  DEFAULT_ENV_SRC=""
  for candidate in "$REPOS_BASE"/*/.env; do
    [ -f "$candidate" ] || continue
    if grep -qE '^(OWG_API_KEY|API_KEY)=' "$candidate" 2>/dev/null; then
      DEFAULT_ENV_SRC="$candidate"
      break
    fi
  done
  if [ -n "$DEFAULT_ENV_SRC" ] && prompt_yn "Target has no .env. Copy OpenWeightGym vars from $DEFAULT_ENV_SRC?" "y"; then
    ENV_MODE="copy"
    ENV_COPY_FROM="$DEFAULT_ENV_SRC"
  elif prompt_yn "Create a starter .env from .env.example to fill in later?" "n"; then
    ENV_MODE="example"
  fi
fi

# 7. Post-deploy actions
RUN_SETUP="false"
if prompt_yn "Run .agents/plugins/dev-tools/setup.sh now (create toolkit venv + initial index)?" "y"; then
  RUN_SETUP="true"
fi

INSTALL_GIT_HOOK="false"
if [ -d "$TARGET_DIR/.git" ]; then
  if prompt_yn "Install git post-commit hook for automatic re-indexing?" "y"; then
    INSTALL_GIT_HOOK="true"
  fi
fi

TRUST_CODEX="false"
if [ -f "$HOME/.codex/config.toml" ] || command -v codex >/dev/null 2>&1; then
  if prompt_yn "Mark '$TARGET_DIR' as trusted in ~/.codex/config.toml (enables Codex MCP + hooks)?" "y"; then
    TRUST_CODEX="true"
  fi
fi

# --- Summary & Confirmation ---
printf '\n'
bold "=== Deployment Summary ==="
info "Target:              $TARGET_DIR"
info "Project:             $PROJECT_NAME ($PROJECT_SLUG)"
info "Source dirs:         $SOURCE_DIRS_CSV"
info "Header-req dirs:     $HEADER_DIRS_CSV"
info "Extensions:          $EXTENSIONS_CSV"
info "OWG suite app:       $SUITE_APP"
info "Overwrite existing:  $OVERWRITE_ROOT"
info "DevDockerfile:       $INCLUDE_DOCKER (port=$APP_PORT, playwright=$INSTALL_PLAYWRIGHT)"
info "Update .gitignore:   $UPDATE_GITIGNORE"
info ".env action:         $ENV_MODE"
info "Run setup.sh:        $RUN_SETUP"
info "Git post-commit:     $INSTALL_GIT_HOOK"
info "Trust in Codex:      $TRUST_CODEX"

if ! prompt_yn "Proceed with deployment?" "y"; then
  echo "Aborted."
  exit 0
fi

# --- Perform Deployment ---
printf '\n'
bold "Deploying files..."

python3 - "$TEMPLATE_DIR" "$TARGET_DIR" "$PROJECT_NAME" "$PROJECT_SLUG" "$PROJECT_DESC" \
  "$SOURCE_DIRS_CSV" "$HEADER_DIRS_CSV" "$EXTENSIONS_CSV" "$SUITE_APP" \
  "$OVERWRITE_ROOT" "$INCLUDE_DOCKER" "$APP_PORT" "$INSTALL_PLAYWRIGHT" <<'PYEOF'
import json
import shutil
import sys
from pathlib import Path

(
    template_dir,
    target_dir,
    project_name,
    project_slug,
    project_desc,
    source_dirs_csv,
    header_dirs_csv,
    extensions_csv,
    suite_app,
    overwrite_root_s,
    include_docker_s,
    app_port,
    install_playwright,
) = sys.argv[1:]

src = Path(template_dir)
dst = Path(target_dir)
overwrite_root = overwrite_root_s == "true"
include_docker = include_docker_s == "true"

def parse_csv(val: str) -> list[str]:
    return [x.strip() for x in val.split(",") if x.strip()]

source_dirs = parse_csv(source_dirs_csv)
header_dirs = parse_csv(header_dirs_csv)
extensions = [e if e.startswith(".") else f".{e}" for e in parse_csv(extensions_csv)]

ignore = shutil.ignore_patterns(".venv", "state", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")

# 1. Always sync plugin toolkit + .claude + .codex + .agents/PLAN_TOOLING_SECTION.md
shutil.copytree(src / ".agents/plugins/dev-tools", dst / ".agents/plugins/dev-tools", ignore=ignore, dirs_exist_ok=True)
shutil.copytree(src / ".claude", dst / ".claude", ignore=ignore, dirs_exist_ok=True)
shutil.copytree(src / ".codex", dst / ".codex", ignore=ignore, dirs_exist_ok=True)
(dst / ".agents").mkdir(parents=True, exist_ok=True)
shutil.copy2(src / ".agents/PLAN_TOOLING_SECTION.md", dst / ".agents/PLAN_TOOLING_SECTION.md")

def copy_or_write(rel: str, content: str | None = None) -> str:
    out = dst / rel
    if out.exists() and not overwrite_root:
        return f"skipped (exists): {rel}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        shutil.copy2(src / rel, out)
    else:
        out.write_text(content, encoding="utf-8")
    return f"wrote: {rel}"

# 2. Customize AGENTS.md
agents_md = (src / "AGENTS.md").read_text(encoding="utf-8")
agents_md = (
    agents_md.replace("{{PROJECT_NAME}}", project_name)
    .replace("{{PROJECT_DESCRIPTION}}", project_desc)
    .replace("{{SOURCE_DIRS}}", ", ".join(source_dirs))
)
print(" ", copy_or_write("AGENTS.md", agents_md))
print(" ", copy_or_write("CLAUDE.md"))
print(" ", copy_or_write(".mcp.json"))
print(" ", copy_or_write(".env.example"))
print(" ", copy_or_write(".agents/RULES.md"))
# Role starter templates for scaffold_module (project-owned: never overwritten unless requested)
for tmpl in sorted((src / ".agents/templates").glob("*")):
    if tmpl.is_file():
        print(" ", copy_or_write(f".agents/templates/{tmpl.name}"))

# 3. Customize .agents/config.json
cfg = json.loads((src / ".agents/config.json").read_text(encoding="utf-8"))
cfg["project_name"] = project_name
cfg["source_dirs"] = source_dirs
cfg["header_required_dirs"] = header_dirs
cfg["extensions"] = extensions
cfg["embeddings"]["suite_application"] = suite_app
cfg["summaries"]["suite_application"] = suite_app
print(" ", copy_or_write(".agents/config.json", json.dumps(cfg, indent=2) + "\n"))

# 4. Customize .agents/ruff.toml known-first-party
first_party = [d.split("/")[0] for d in header_dirs if d and d != "tests"] or ["app"]
ruff_txt = (src / ".agents/ruff.toml").read_text(encoding="utf-8")
ruff_txt = ruff_txt.replace('known-first-party = ["app"]', f"known-first-party = {json.dumps(first_party)}")
print(" ", copy_or_write(".agents/ruff.toml", ruff_txt))

# 5. Optional DevDockerfile & .dockerignore
if include_docker:
    docker_txt = (src / "DevDockerfile").read_text(encoding="utf-8")
    docker_txt = (
        docker_txt.replace("{{IMAGE_NAME}}", f"{project_slug}-dev")
        .replace("{{APP_PORT}}", app_port)
        .replace("{{INSTALL_PLAYWRIGHT}}", install_playwright)
    )
    print(" ", copy_or_write("DevDockerfile", docker_txt))
    print(" ", copy_or_write(".dockerignore"))

# Ensure scripts are executable
for script in ("setup.sh", "run_mcp.sh", "run_hook.sh", "install_git_hook.sh", "scaffold.py"):
    sp = dst / ".agents/plugins/dev-tools" / script
    if sp.exists():
        sp.chmod(sp.stat().st_mode | 0o111)
PYEOF
ok "Copied and customized Tool-First files into $TARGET_DIR"

# Update .gitignore idempotently
if [ "$UPDATE_GITIGNORE" = "true" ]; then
  GITIGNORE="$TARGET_DIR/.gitignore"
  touch "$GITIGNORE"
  ADDED_GI=0
  for entry in ".agents/state/" ".agents/plugins/*/.venv/" ".env"; do
    if ! grep -Fqx "$entry" "$GITIGNORE" 2>/dev/null; then
      [ "$ADDED_GI" -eq 0 ] && printf '\n# Tool-First Dev local state & secrets\n' >> "$GITIGNORE"
      printf '%s\n' "$entry" >> "$GITIGNORE"
      ADDED_GI=$((ADDED_GI + 1))
    fi
  done
  ok "Updated $GITIGNORE ($ADDED_GI entries added)"
fi

# Optional .env setup (only OWG / API vars if copying from another repo)
if [ "$ENV_MODE" = "copy" ] && [ -f "$ENV_COPY_FROM" ]; then
  python3 - "$ENV_COPY_FROM" "$TARGET_DIR/.env" <<'PYEOF'
import sys
from pathlib import Path

src_lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore").splitlines()
keep_prefixes = ("OWG_", "API_IP=", "API_PORT=", "API_KEY=", "API_HOST=", "API_PROTOCOL=")
kept = [
    ln for ln in src_lines
    if ln.strip() and not ln.lstrip().startswith("#") and ln.lstrip().startswith(keep_prefixes)
]
header = "# OpenWeightGym settings copied for Tool-First Dev indexer\n"
Path(sys.argv[2]).write_text(header + "\n".join(kept) + "\n", encoding="utf-8")
PYEOF
  chmod 600 "$TARGET_DIR/.env"
  ok "Copied OpenWeightGym settings to $TARGET_DIR/.env (mode 600)"
elif [ "$ENV_MODE" = "example" ]; then
  cp "$TARGET_DIR/.env.example" "$TARGET_DIR/.env"
  chmod 600 "$TARGET_DIR/.env"
  ok "Created starter $TARGET_DIR/.env from .env.example"
fi

# Run setup.sh
if [ "$RUN_SETUP" = "true" ]; then
  bold "Running setup.sh..."
  "$TARGET_DIR/.agents/plugins/dev-tools/setup.sh"
  ok "Toolkit venv and initial knowledge graph ready"
fi

# Install git post-commit hook
if [ "$INSTALL_GIT_HOOK" = "true" ]; then
  "$TARGET_DIR/.agents/plugins/dev-tools/install_git_hook.sh"
  ok "Installed git post-commit hook"
fi

# Trust project in ~/.codex/config.toml
if [ "$TRUST_CODEX" = "true" ]; then
  mkdir -p "$HOME/.codex"
  CODEX_CFG="$HOME/.codex/config.toml"
  touch "$CODEX_CFG"
  if ! grep -Fq "[projects.\"$TARGET_DIR\"]" "$CODEX_CFG" 2>/dev/null; then
    printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$TARGET_DIR" >> "$CODEX_CFG"
    ok "Added trusted project entry to $CODEX_CFG"
  else
    ok "$TARGET_DIR is already listed in $CODEX_CFG"
  fi
fi

printf '\n'
bold "=== Done! Next Steps ==="
info "1. Paste the section from $TARGET_DIR/.agents/PLAN_TOOLING_SECTION.md into your initial plan."
info "2. Review $TARGET_DIR/.agents/RULES.md to add any project-specific patterns or boundaries."
info "3. Per-agent first run:"
info "   - Antigravity: open $TARGET_DIR and check /mcp (all 14 project-dev tools load eagerly)."
info "   - Claude Code: run 'claude' in $TARGET_DIR once to approve .mcp.json's project-dev server."
info "   - Codex CLI:   run 'codex mcp list' in $TARGET_DIR to confirm project-dev is active."
