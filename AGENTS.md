# AGENTS.md

## Project
{{PROJECT_NAME}} — {{PROJECT_DESCRIPTION}}
- Source directories: `{{SOURCE_DIRS}}`
- `.agents/` — Tool-First Development rules, lint config, knowledge-graph indexer, lifecycle hooks, and project MCP server.

## Commands
- Setup dev tools: `.agents/plugins/dev-tools/setup.sh`
- Test (compact, failures only): use MCP `run_tests`
- Lint & header check: use MCP `lint_and_headers`
- Re-index knowledge graph: use MCP `reindex` (also runs automatically after every turn via hook)

## Workflow
1. Call `repo_map` to orient yourself on the codebase before broad file reads or shell `find`/`ls`.
2. Call `get_conventions` for the area, role, or pattern you are touching.
3. Call `find_similar` before creating any new module and match the closest existing structure.
4. Call `lookup_symbol` before changing an existing function, class, or interface.
5. Write or update tests first.
6. Write code with a valid module header (see Formats and `.agents/RULES.md`).
7. Run `run_tests` and `lint_and_headers` until clean.
8. Commit when ready; the post-turn hook (and optional git post-commit hook) keeps the knowledge graph fresh.

## Tools (MCP server `project-dev` / `dev-tools_project-dev` — use before reading files or running shell commands)
- Orienting on modules/architecture -> `repo_map` (offline SQLite overview; optional `role`, `scope`, `include_summaries`).
- New file or feature -> `find_similar` to find matching modules and copy conventions.
- Unfamiliar area or pattern -> `get_conventions`.
- Editing a function, class, or import -> `lookup_symbol` for definition, callers, callees, and tests.
- Running tests -> `run_tests`, not shell `pytest`.
- Checking style and headers -> `lint_and_headers`, not raw shell linters.
- Checking index freshness -> `index_status` (and `reindex` if stale).
- Reviewing tool effectiveness -> `tool_metrics`.
- Fall back to broad file reads or shell grep only when the MCP tools lack what you need.

## Formats
- Rules and named patterns: `.agents/RULES.md`
- Plan integration guide: `.agents/PLAN_TOOLING_SECTION.md`
- Allowed role tags: `page | component | service | repository | router | schema | util | test`
- Required module header (top docstring for Python, leading `/** ... */` block for JS):
  ```text
  Purpose: One-sentence summary of what this module does.
  Role: service
  Patterns: service-layer, structured-errors
  Depends on: repository, client
  Exposes: ExampleService, run_workflow
  Tests: tests/test_example.py
  Invariants: Key constraints that must always hold (or none).
  ```

## Boundaries
- **Always**: write tests first, include a valid module header, keep functions documented (purpose, params, return, side effects), run `run_tests` and `lint_and_headers`.
- **Ask first**: new third-party dependencies, database schema changes, edits to shared core interfaces, changes outside the current task's scope.
- **Never**: hand-edit `.agents/state/` index or logs, hard-code secrets or credentials, delete or skip tests to make a run pass, rewrite unrelated modules.

## Definition of done
- `run_tests` passes with zero failures.
- `lint_and_headers` passes with zero errors.
- `index_status` reports a fresh index with no invalid headers.
- Concise summary of changes and any open questions.
