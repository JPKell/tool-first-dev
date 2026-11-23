# Tool-First Development Template (`tool-first-dev`)

A reusable, project-agnostic **Tool-First Development** starter kit for **Google Antigravity (`agy`)**, **Claude Code (`claude`)**, and **OpenAI Codex (`codex`)**.

It bundles:
- **Shared `project-dev` MCP server** (`.agents/plugins/dev-tools/`) backed by a local SQLite knowledge graph (Tree-sitter AST + call graph + OpenWeightGym summaries & dense embeddings).
- **9 eager-loaded MCP tools**: `repo_map`, `get_conventions`, `find_similar`, `lookup_symbol`, `run_tests`, `lint_and_headers`, `index_status`, `reindex`, and `tool_metrics`.
- **Multi-agent lifecycle hooks**: `PreToolUse` guardrails (blocks hand-editing `.agents/state/` or `.env`, blocks raw shell `pytest`/`ruff`), `PostToolUse` local AST sync + adoption telemetry, and `Stop` hook incremental OWG sync.
- **Root agent docs & configs**: `AGENTS.md`, `CLAUDE.md` (`@AGENTS.md`), `.mcp.json`, `.claude/settings.json`, `.codex/config.toml`, `.codex/hooks.json`, `.agents/RULES.md`, `.agents/PLAN_TOOLING_SECTION.md`, and an optional multi-agent `DevDockerfile`.

---

## Quick Start (Interactive Deployment)

Just run `./deploy.sh` — it walks you through every question with smart defaults so you never have to remember flags:

```bash
cd ~/repos/tool-first-dev
./deploy.sh
```

The wizard will ask for:
1. Target project path (existing or new folder; shortcuts under `~/repos/` work too)
2. Project name & one-line description (written into `AGENTS.md` and `.agents/config.json`)
3. Source directories to index (auto-detects `src/` vs `app/`), header-required directories, and file extensions
4. Whether to include `DevDockerfile` / `.dockerignore` (plus app port & optional Playwright)
5. Whether to update `.gitignore` with `.agents/state/` and plugin `.venv` ignores
6. Optional `.env` setup for OpenWeightGym (copy from an existing repo's `.env` or `.env.example`)
7. Whether to run `.agents/plugins/dev-tools/setup.sh` immediately, install the git `post-commit` hook, and trust the project in `~/.codex/config.toml`

---

## Using It in Your Project's Initial Plan

Copy the section in `.agents/PLAN_TOOLING_SECTION.md` directly into your new project's `IMPLEMENTATION_PLAN.md` (or kickoff prompt). It tells the agent which MCP tool to use in each phase, how to declare `Role`/`Patterns`/`Exposes`/`Tests` upfront per milestone, and enforces a Tool-First Definition of Done.

---

## Per-Agent First-Run Checklist

After `./deploy.sh` finishes:
- **Antigravity (`agy`)**: Open the target repo and run `/mcp` — all 9 `dev-tools_project-dev` tools load under `Eager`.
- **Claude Code (`claude`)**: Launch `claude` once in the target repo and approve `.mcp.json`'s `project-dev` server (`.claude/settings.json` pre-configures `ENABLE_TOOL_SEARCH=false` and `mcp__project-dev__*` permissions).
- **Codex (`codex`)**: If you answered `y` to trusting the repo in `~/.codex/config.toml`, Codex automatically loads `.codex/config.toml` and `.codex/hooks.json`. Run `codex mcp list` in the target repo to verify.

---

## Testing This Template

```bash
cd ~/repos/tool-first-dev/.agents/plugins/dev-tools
./setup.sh  # or use an existing venv with dev-tools dependencies
PYTHONPATH=. .venv/bin/pytest -q tests/test_toolkit.py
```
