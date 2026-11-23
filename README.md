# Tool-First Development Template (`tool-first-dev`)

A reusable, project-agnostic **Tool-First Development** starter kit for **Google Antigravity (`agy`)**, **Claude Code (`claude`)**, and **OpenAI Codex (`codex`)**.

It bundles:
- **Shared `project-dev` MCP server** (`.agents/plugins/dev-tools/`) backed by a local SQLite knowledge graph (Tree-sitter AST + import-resolved call graph, with optional OpenWeightGym summaries & dense embeddings and an offline BM25 fallback).
- **14 eager-loaded MCP tools**, all returning compact, size-bounded JSON:

  | Need | Tool |
  |---|---|
  | Orient | `repo_map` (`depth=1` for a directory overview), `get_conventions` |
  | Read less | `outline` (signatures + line ranges, no bodies), `get_source` (one symbol's exact body) |
  | Find | `lookup_symbol` (import-resolved callers/callees/importers/tests), `search_text` (grep replacement), `find_similar` |
  | Create | `scaffold_module` (module + linked test from `.agents/templates/`, valid header) |
  | Verify | `run_tests`, `affected_tests` (only tests covering changed files), `lint_and_headers` (Ruff + header drift) |
  | Maintain | `index_status`, `reindex`, `tool_metrics` (adoption + estimated tokens saved) |
- **Multi-agent lifecycle hooks**: `PreToolUse` guardrails (blocks editing `.agents/state/` or `.env` — including via shell redirects/`sed -i`/`rm` — and raw shell `pytest`/`ruff`), `PostToolUse` local AST sync after Write/Edit *and* shell edits + adoption telemetry, and a `Stop` hook OWG sync with a 10-minute circuit breaker after connection failures. Hooks fail open silently until `setup.sh` has created the toolkit venv.
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
- **Antigravity (`agy`)**: Open the target repo and run `/mcp` — all 14 `dev-tools_project-dev` tools load under `Eager`.
- **Claude Code (`claude`)**: Launch `claude` once in the target repo and approve `.mcp.json`'s `project-dev` server (`.claude/settings.json` pre-configures `ENABLE_TOOL_SEARCH=false` and `mcp__project-dev__*` permissions).
- **Codex (`codex`)**: If you answered `y` to trusting the repo in `~/.codex/config.toml`, Codex automatically loads `.codex/config.toml` and `.codex/hooks.json`. Run `codex mcp list` in the target repo to verify.

---

## Testing This Template

```bash
cd ~/repos/tool-first-dev/.agents/plugins/dev-tools
./setup.sh  # or use an existing venv with dev-tools dependencies
PYTHONPATH=. .venv/bin/pytest -q tests/
```

`tests/test_navigation.py` includes a dogfooding test: the toolkit's own `src/` must pass its header, drift, and Ruff rules.

---

## Measuring Whether Tool-First Helps

- **In-session:** `tool_metrics` reports `est_tokens_returned` and `est_tokens_saved` (bytes of files the agent would otherwise have read, minus the tool payload, ÷ 4). Treat these as rough estimates.
- **A/B benchmark:** `.agents/plugins/dev-tools/bench/compare.py` runs the same task prompt headlessly with `claude -p` in two throwaway git worktrees, once with the `project-dev` server + hooks and once without, and compares turns, cost, and token usage from the JSON output:

  ```bash
  python3 .agents/plugins/dev-tools/bench/compare.py --repo ~/repos/my-project \
      --task "Add a discount field to BillingService.total and cover it with a test" --runs 3
  ```
