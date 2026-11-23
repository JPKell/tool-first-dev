# AGENTS.md

## Project
{{PROJECT_NAME}} — {{PROJECT_DESCRIPTION}}
- Source directories: `{{SOURCE_DIRS}}`
- `.agents/` — Tool-First Development rules, lint config, role templates, knowledge-graph indexer, lifecycle hooks, and project MCP server.

## Commands
- Setup dev tools: `.agents/plugins/dev-tools/setup.sh`
- Test (compact, failures only): use MCP `run_tests`, or `affected_tests(run=true)` for just the tests covering your changes
- Lint & header check: use MCP `lint_and_headers`
- Re-index knowledge graph: use MCP `reindex` (also runs automatically after edits and every turn via hooks)

## Workflow
1. Call `repo_map` (use `depth=1` first on a large repo) to orient yourself before broad file reads or shell `find`/`ls`.
2. Call `get_conventions` for the area, role, or pattern you are touching.
3. New module → `find_similar` for the closest existing structure, then `scaffold_module` to create it with a valid header and linked test stub.
4. Existing code → `outline` the file, `lookup_symbol` for callers/callees/tests, and `get_source` for the exact body you will change. Only `Read` whole files when you truly need them.
5. Write or update tests first.
6. Write code with a valid module header (see Formats and `.agents/RULES.md`).
7. Run `affected_tests(run=true)` while iterating, then `run_tests` and `lint_and_headers` until clean.
8. Commit when ready; hooks (and the optional git post-commit hook) keep the knowledge graph fresh.

## Tools (MCP server `project-dev` / `dev-tools_project-dev` — use before reading files or running shell commands)
- Orienting on modules/architecture -> `repo_map` (offline; `role`, `scope`, `depth`, `limit`, `include_summaries`).
- Seeing what is in a file -> `outline` (symbols, signatures, line ranges; no bodies).
- Reading one function/class -> `get_source` (exact current body; `path` to disambiguate).
- Editing a function, class, or import -> `lookup_symbol` for definition, import-resolved callers, callees, importers, and tests.
- Finding literals, config keys, routes, env vars, error messages -> `search_text` (grouped grep replacement).
- New file or feature -> `find_similar` (embeddings, or offline BM25) then `scaffold_module`.
- Unfamiliar area or pattern -> `get_conventions` (returns valid topics when nothing matches).
- Running tests -> `run_tests` / `affected_tests`, not shell `pytest`.
- Checking style, headers, and header drift -> `lint_and_headers`, not raw shell linters.
- Checking index freshness -> `index_status` (and `reindex` if stale).
- Reviewing tool effectiveness and estimated tokens saved -> `tool_metrics`.
- Fall back to broad file reads or shell grep only when the MCP tools lack what you need.

## Formats
- Rules and named patterns: `.agents/RULES.md`
- Role starter templates (used by `scaffold_module`): `.agents/templates/`
- Plan integration guide: `.agents/PLAN_TOOLING_SECTION.md`
- Allowed role tags: `page | component | service | repository | router | schema | util | test`
- Required module header (top docstring for Python, leading `/** ... */` block for JS):
  ```text
  Purpose: One-sentence summary of what this module does.
  Role: service
  Patterns: service-layer, structured-errors
  Tests: tests/test_example.py
  Invariants: Key constraints that must always hold (or none).
  ```
  `Exposes` and `Depends on` are derived from the code when omitted. If you do write them, `lint_and_headers` reports drift (an exposed name that is not defined, or a `Tests:` path that does not exist).

## Boundaries
- **Always**: write tests first, include a valid module header, keep functions documented (purpose, params, return, side effects), run `run_tests` and `lint_and_headers`.
- **Ask first**: new third-party dependencies, database schema changes, edits to shared core interfaces, changes outside the current task's scope.
- **Never**: hand-edit `.agents/state/` index or logs (including via shell redirects), hard-code secrets or credentials, delete or skip tests to make a run pass, rewrite unrelated modules.

## Definition of done
- `run_tests` passes with zero failures.
- `lint_and_headers` passes with zero errors.
- `index_status` reports a fresh index with no invalid headers.
- Concise summary of changes and any open questions.
