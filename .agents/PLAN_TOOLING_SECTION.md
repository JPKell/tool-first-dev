# Tool-First Plan Section Template

Copy the section below (`## Tool-First Development & MCP Workflow`) into your new project's initial `IMPLEMENTATION_PLAN.md` (or kickoff prompt) so every coding agent — Antigravity, Claude Code, and Codex — integrates the `project-dev` MCP tools directly into planning, implementation, and verification from Milestone 0 onward.

---

<!-- COPY FROM HERE INTO YOUR PROJECT'S IMPLEMENTATION_PLAN.md -->

## Tool-First Development & MCP Workflow

This repository uses the shared **`project-dev`** MCP server (`.agents/plugins/dev-tools/`, exposed in Antigravity as `dev-tools_project-dev` and in Claude Code / Codex as `project-dev`) backed by a live SQLite knowledge graph (Tree-sitter AST + import-resolved call graph + optional OpenWeightGym summaries/embeddings, with an offline BM25 fallback) and lifecycle hooks.

### 1. Required MCP Tools (Use First — Before Broad File Reads or Shell Commands)

All `project-dev` tools are configured for eager loading. Use them as your primary navigation and verification interface:

| Phase | MCP Tool | When to Call It | Replaces |
|---|---|---|---|
| **Orient** | `repo_map(role?, scope?, depth?, limit?, include_summaries?)` | Start of every session or milestone. On large repos call `depth=1` first for a per-directory overview, then narrow with `scope`. | `find`, `ls -R`, reading 10+ files blindly |
| **Read Less** | `outline(path)` / `get_source(symbol, path?)` | Before reading any source file: `outline` shows symbols, signatures, and line ranges; `get_source` returns just the body you need. | Reading whole files |
| **Text Search** | `search_text(pattern, scope?, regex?, file_glob?)` | Finding string literals, config keys, env vars, routes, or error messages (things that are not symbols). | Shell `grep` / `rg` |
| **Rules** | `get_conventions(topic)` | Before creating or editing code in any role (`router`, `service`, `repository`, etc.) or named pattern. | Reading `.agents/RULES.md` / `.agents/config.json` manually |
| **New Modules** | `find_similar(query, role?, limit?)` → `scaffold_module(path, role, purpose, patterns?)` | Before creating any new file: find the closest existing module, then scaffold the file and its linked test stub from `.agents/templates/` with a valid header. | Guessing layout; hand-writing headers |
| **Edits / Refactors** | `lookup_symbol(symbol, path?, include_impact?, limit?)` | Before modifying any existing class, function, method, or interface — returns the definition plus import-resolved callers, internal callees, importers, and covering tests. | `grep` / `rg` for references |
| **Testing** | `affected_tests(paths?, run?)` / `run_tests(scope?)` | While iterating, `affected_tests(run=true)` runs only tests covering changed files; `run_tests()` for the full suite. Compact failure-only JSON. | Shell `pytest` (blocked by `PreToolUse` hook) |
| **Lint & Contracts** | `lint_and_headers(scope?)` | After edits, to validate Ruff rules, required module headers, and header drift in one call. | Shell `ruff` (blocked by `PreToolUse` hook) |
| **Graph Sync** | `index_status()` / `reindex(full?, sync_remote?)` | At milestone completion (or after bulk multi-file scaffolding when `hook:stop` deferred remote summaries). | Manual index checks |
| **Adoption Audit** | `tool_metrics(limit?)` | End of each milestone to verify MCP adoption, payload sizes, estimated tokens saved, and missed MCP opportunities. | Guessing whether tooling works |

> **Rule of thumb:** Discover with MCP first (`repo_map` → `outline` / `lookup_symbol` / `get_source` / `search_text` / `find_similar` / `get_conventions`), then `read_file` only the exact line ranges you need to edit. Never run raw shell `pytest` or `ruff`, and never hand-edit `.agents/state/`.

---

### 2. Milestone 0: Project Tooling Bootstrap (Do Before Writing Feature Code)

Before starting Milestone 1, customize the scaffolded template for this repository:

1. **Customize `.agents/config.json`**:
   - Set `project_name`, `source_dirs`, `header_required_dirs`, `extensions`, `roles`, `patterns`, and `embeddings.suite_application` / `summaries.suite_application`.
2. **Customize `.agents/RULES.md` & `AGENTS.md`**:
   - Define this project's Role Vocabulary, Named Patterns, and architectural boundaries (keep `CLAUDE.md` pointing to `@AGENTS.md`).
3. **Initialize the toolkit & initial index**:
   - Run `.agents/plugins/dev-tools/setup.sh` (creates the isolated toolkit venv and runs initial `--full` index).
   - Verify the project `.venv` (or dev container `APP_VENV`) has all `requirements.txt` dependencies installed so `run_tests` runs cleanly.
4. **Verify MCP connectivity once**:
   - Call `index_status` and `lint_and_headers` to confirm `fresh: true` and `ok: true`.

---

### 3. How to Integrate Tools Into Every Plan Milestone

Structure every milestone in this plan with the following concrete artifacts and execution loop:

#### A. Plan-Time Module Table (Include in Each Milestone Spec)
For every file a milestone creates or modifies, declare its contract upfront in the plan so module headers and tests align from the start:

| File | Action | `Role` | `Patterns` | `Tests` | Key symbols |
|---|---|---|---|---|---|
| `app/services/example.py` | create (`scaffold_module`) | `service` | `service-layer, structured-errors` | `tests/test_example.py` | `ExampleService` |

#### B. Per-Task Execution Loop (Follow in Order)
1. **Orient**: Call `repo_map(scope=...)` at session start; call `get_conventions(<role or pattern>)` for the area you are touching.
2. **Impact / Precedent Check**:
   - Creating a new module → call `find_similar("<purpose>", role="<role>")`, then `scaffold_module(path, role, purpose, patterns)` to create the file and its test stub.
   - Changing an existing symbol → `outline("<file>")`, then `lookup_symbol("<symbol>")` for callers, callees, importers, and covering tests, then `get_source("<symbol>")` for the exact body.
3. **Tests First (TDD)**: Write or update the linked test file (`Tests:`) first, then run `run_tests(scope="<test_file>")` to confirm the expected failure.
4. **Implement with Valid Headers**: Keep the structured header (`Purpose`, `Role`, `Patterns`, `Tests`, `Invariants`; `Exposes`/`Depends on` are derived when omitted) and concise docstrings on public functions/classes.
5. **Verify**:
   - Run `affected_tests(run=true)` until green, then `run_tests()` for the full suite.
   - Run `lint_and_headers()` until `ok: true` with zero header or Ruff issues.
6. **Sync & Audit (End of Milestone)**:
   - Run `index_status()`. If `stale_summaries` or `stale_embeddings` are non-empty (e.g., after a large multi-file scaffolding turn where `hook:stop` capped remote summaries), call `reindex(full=False, sync_remote=True)`.
   - Run `tool_metrics()` at milestone sign-off to verify high MCP usage and zero unexpected guardrail blocks.

#### C. Milestone Definition of Done
A milestone is **not complete** until all four checks pass:
- [ ] `run_tests()` returns `"ok": true` with zero failures/errors.
- [ ] `lint_and_headers()` returns `"ok": true` with zero header or lint issues.
- [ ] `index_status()` returns `"fresh": true` with `invalid_headers: []`.
- [ ] Every new/changed file has a valid module header matching `.agents/RULES.md` and is reflected in `repo_map()`.

<!-- END COPY -->
