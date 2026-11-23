"""
Purpose: Stdio MCP server exposing Tool-First Development tools backed by the knowledge graph.
Role: router
Patterns: thin-router, structured-errors
Tests: tests/test_toolkit.py, tests/test_navigation.py
Invariants: Tools return compact JSON; errors are returned as {"ok": false, "error"}, never raised.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import ToolkitConfig, load_config
from .graph_store import GraphStore
from .indexer import get_index_status, reindex_project
from .navigation import affected_tests as _affected_tests
from .navigation import get_source as _get_source
from .navigation import outline as _outline
from .navigation import search_text as _search_text
from .observability import log_mcp_call, summarize_metrics
from .owg_client import OWGClient, load_owg_settings
from .scaffolding import scaffold_module as _scaffold_module
from .similarity import search_similar
from .test_runner import run_compact_tests, run_lint_and_headers

SERVER_INSTRUCTIONS = (
    "Tool-First Development MCP server (project-dev) backed by the project SQLite knowledge graph. "
    "Use these tools BEFORE whole-file reads or shell grep/find/pytest/ruff: "
    "repo_map (offline module map; depth=1 for a directory overview), "
    "outline (a file's symbols + signatures + line ranges, no bodies), "
    "get_source (exact current source of one function/class instead of reading the whole file), "
    "lookup_symbol (definition, import-resolved callers/callees/importers, covering tests), "
    "search_text (grep replacement for literals, config keys, routes, error messages), "
    "find_similar (closest existing modules; dense embeddings or offline BM25), "
    "get_conventions (rules + examples/templates for a role or pattern), "
    "scaffold_module (create a module + linked test from a role template with a valid header), "
    "run_tests / affected_tests (compact failure-only test runs; affected_tests runs only tests "
    "covering changed files), lint_and_headers (Ruff + header/drift checks), "
    "index_status / reindex (graph freshness), and tool_metrics (adoption + est. tokens saved)."
)
RULES_FALLBACK_TOPICS = 40

mcp = MCPServer("project-dev", instructions=SERVER_INSTRUCTIONS)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _source_bytes(cfg: ToolkitConfig, paths: list[str]) -> int:
    total = 0
    for p in set(paths):
        try:
            total += (cfg.project_root / p).stat().st_size
        except OSError:
            continue
    return total


def _run_observed(
    tool_name: str,
    args: dict[str, Any],
    fn: Callable[[ToolkitConfig], Any],
) -> str:
    """Run a tool, return compact JSON, and log payload size vs. the read it replaced.

    A result dict may carry `_baseline_bytes` (bytes the agent would otherwise have read);
    it is stripped from the payload and logged for tool_metrics' token-savings estimate.
    """
    cfg = load_config()
    t0 = time.monotonic()
    try:
        result = fn(cfg)
        baseline = result.pop("_baseline_bytes", None) if isinstance(result, dict) else None
        payload = _dumps(result)
        log_mcp_call(
            cfg.usage_log_path,
            tool=tool_name,
            args=args,
            result_bytes=len(payload.encode("utf-8")),
            duration_ms=(time.monotonic() - t0) * 1000.0,
            ok=True,
            baseline_bytes=baseline,
        )
        return payload
    except Exception as exc:
        err_payload = _dumps({"ok": False, "error": str(exc)})
        log_mcp_call(
            cfg.usage_log_path,
            tool=tool_name,
            args=args,
            result_bytes=len(err_payload.encode("utf-8")),
            duration_ms=(time.monotonic() - t0) * 1000.0,
            ok=False,
            error=str(exc),
        )
        return err_payload


def _rules_topics(rules_text: str, cfg: ToolkitConfig) -> list[str]:
    headings = [h.strip().strip("`") for h in re.findall(r"^#{2,3}\s+(.+)$", rules_text, re.MULTILINE)]
    topics = list(dict.fromkeys([*cfg.roles, *cfg.patterns, *headings]))
    return topics[:RULES_FALLBACK_TOPICS]


def _extract_rules_sections(rules_text: str, topic: str) -> list[str]:
    """Return matching sections from RULES.md for a role, pattern, or area (may be empty)."""
    needle = topic.strip().lower()
    if not needle:
        return []
    sections = re.split(r"(?=^#{2,3}\s+)", rules_text, flags=re.MULTILINE)
    return [sec.strip() for sec in sections if needle in sec.lower()][:5]


@mcp.tool()
def repo_map(
    role: str | None = None,
    scope: str | None = None,
    include_summaries: bool = False,
    depth: int | None = None,
    limit: int = 200,
) -> str:
    """Offline architectural map of indexed modules (path, role, purpose, exports, tests).

    Use depth=1 first on large repos for a per-directory overview; narrow with scope/role.
    """

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        store = GraphStore(cfg.db_path)
        res = store.repo_map(
            role=role, scope=scope, include_summaries=include_summaries, depth=depth, limit=limit
        )
        listed = [m["path"] for m in res.get("modules", [])] or [
            f["path"] for f in store.get_all_files()
            if not scope or f["path"].startswith(scope.strip().lstrip("./"))
        ]
        res["_baseline_bytes"] = _source_bytes(cfg, listed)
        return res

    return _run_observed(
        "repo_map",
        {"role": role, "scope": scope, "include_summaries": include_summaries, "depth": depth},
        _exec,
    )


@mcp.tool()
def outline(path: str) -> str:
    """A file's header, symbols, signatures, and line ranges without bodies (read this before Read)."""
    return _run_observed("outline", {"path": path}, lambda cfg: _outline(cfg, path))


@mcp.tool()
def get_source(symbol: str, path: str | None = None, context: int = 0) -> str:
    """Exact current source of a function/class/method (e.g. `BillingService.total`) instead of the whole file."""
    return _run_observed(
        "get_source",
        {"symbol": symbol, "path": path, "context": context},
        lambda cfg: _get_source(cfg, symbol, path=path, context=context),
    )


@mcp.tool()
def search_text(
    pattern: str,
    scope: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    file_glob: str | None = None,
    limit: int = 50,
) -> str:
    """Grep replacement for literals/config keys/routes/error messages; matches grouped by file."""
    return _run_observed(
        "search_text",
        {"pattern": pattern, "scope": scope, "regex": regex, "file_glob": file_glob},
        lambda cfg: _search_text(
            cfg,
            pattern,
            scope=scope,
            regex=regex,
            case_sensitive=case_sensitive,
            file_glob=file_glob,
            limit=limit,
        ),
    )


@mcp.tool()
def find_similar(query: str, role: str | None = None, limit: int = 5) -> str:
    """Closest existing modules to a description or file path (dense embeddings, else offline BM25)."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        store = GraphStore(cfg.db_path)
        files = store.get_all_files()
        vectors = store.get_all_vectors() if cfg.remote_enabled("embeddings") else {}
        client = None
        if vectors:
            settings = load_owg_settings(
                cfg.project_root,
                suite_application=cfg.embeddings.get("suite_application", "tool-first-dev"),
            )
            client = OWGClient(settings, timeout=15.0) if settings.is_configured else None
        res = search_similar(
            query=query,
            files=files,
            vectors=vectors,
            owg_client=client,
            role=role,
            limit=limit,
            symbols_by_file=store.get_all_symbols_by_file(),
        )
        return {"query": query, "role_filter": role, "indexed_file_count": len(files), **res}

    return _run_observed("find_similar", {"query": query, "role": role, "limit": limit}, _exec)


@mcp.tool()
def get_conventions(topic: str) -> str:
    """RULES.md sections for a role tag, pattern name, or area, plus example files or a starter template."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        rules_text = cfg.rules_path.read_text(encoding="utf-8") if cfg.rules_path.exists() else ""
        sections = _extract_rules_sections(rules_text, topic)
        examples = GraphStore(cfg.db_path).find_examples(topic, limit=5)
        out: dict[str, Any] = {"topic": topic, "matched": bool(sections or examples)}
        if not out["matched"]:
            out["available_topics"] = _rules_topics(rules_text, cfg)
            out["hint"] = "Call get_conventions with one of available_topics."
            return out
        out["rules_sections"] = sections
        out["example_files"] = examples
        role = topic.strip().lower()
        for ext in cfg.extensions:
            tmpl = cfg.templates_dir / f"{role}{ext}.tmpl"
            if tmpl.is_file():
                out["starter_template"] = tmpl.relative_to(cfg.project_root).as_posix()
                break
        if not examples and "starter_template" in out:
            out["hint"] = f"No project examples yet; scaffold_module(role='{role}') renders the starter template."
        return out

    return _run_observed("get_conventions", {"topic": topic}, _exec)


@mcp.tool()
def lookup_symbol(
    symbol: str,
    include_impact: bool = True,
    path: str | None = None,
    limit: int = 25,
) -> str:
    """Definition(s) plus import-resolved callers, callees, importers, and covering tests.

    Pass `path` to disambiguate same-named symbols; lists are capped at `limit`.
    """

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        res = GraphStore(cfg.db_path).lookup_symbol(
            symbol=symbol, include_impact=include_impact, path=path, limit=limit
        )
        touched = [d["file_path"] for d in res["definitions"]] + [
            c["source_file"] for c in res.get("callers", [])
        ]
        res["_baseline_bytes"] = _source_bytes(cfg, touched)
        return res

    return _run_observed(
        "lookup_symbol",
        {"symbol": symbol, "include_impact": include_impact, "path": path},
        _exec,
    )


@mcp.tool()
def scaffold_module(
    path: str,
    role: str,
    purpose: str,
    patterns: str = "none",
    invariants: str = "none",
    with_test: bool = True,
) -> str:
    """Create a new module and its linked test stub from the role template, with a valid header."""
    return _run_observed(
        "scaffold_module",
        {"path": path, "role": role, "patterns": patterns},
        lambda cfg: _scaffold_module(
            cfg,
            path=path,
            role=role,
            purpose=purpose,
            patterns=patterns,
            invariants=invariants,
            with_test=with_test,
        ),
    )


@mcp.tool()
def run_tests(scope: str | None = None) -> str:
    """Run tests (space-separated paths/node ids) and return compact results: counts + failures only."""
    return _run_observed(
        "run_tests", {"scope": scope}, lambda cfg: run_compact_tests(scope=scope, config=cfg)
    )


@mcp.tool()
def affected_tests(paths: list[str] | None = None, run: bool = False) -> str:
    """Tests covering changed files (default: git diff HEAD + untracked); run=True runs only those."""
    return _run_observed(
        "affected_tests",
        {"paths": paths, "run": run},
        lambda cfg: _affected_tests(cfg, paths=paths, run=run),
    )


@mcp.tool()
def lint_and_headers(scope: str | None = None) -> str:
    """Validate module headers (roles, patterns, fields, drift vs. code) and run Ruff lint checks."""
    return _run_observed(
        "lint_and_headers", {"scope": scope}, lambda cfg: run_lint_and_headers(scope=scope, config=cfg)
    )


@mcp.tool()
def index_status() -> str:
    """Knowledge-graph freshness, stale summaries/embeddings (when remote is enabled), invalid headers."""
    return _run_observed("index_status", {}, lambda cfg: get_index_status(config=cfg))


@mcp.tool()
def reindex(full: bool = False, sync_remote: bool = True) -> str:
    """Incrementally (or fully) rebuild the knowledge graph; remote summaries/embeddings when configured."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        res = reindex_project(config=cfg, full=full, sync_remote=sync_remote, trigger="mcp:reindex")
        # Keep the payload small: counts instead of full file lists
        for key in ("updated_files", "summarized_files", "embedded_files", "deleted_files"):
            items = res.get(key) or []
            if len(items) > 20:
                res[key] = {"count": len(items), "first": items[:20]}
        return res

    return _run_observed("reindex", {"full": full, "sync_remote": sync_remote}, _exec)


@mcp.tool()
def tool_metrics(limit: int = 10) -> str:
    """MCP usage stats, payload sizes, estimated tokens saved, fallbacks, and non-MCP tool usage."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        return summarize_metrics(
            cfg.usage_log_path,
            recent_limit=limit,
            non_mcp_log_path=cfg.non_mcp_log_path,
        )

    return _run_observed("tool_metrics", {"limit": limit}, _exec)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
