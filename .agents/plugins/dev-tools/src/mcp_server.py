"""Stdio MCP server exposing Tool-First Development tools backed by the knowledge graph."""

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
from .observability import log_mcp_call, summarize_metrics
from .owg_client import OWGClient, load_owg_settings
from .similarity import search_similar
from .test_runner import run_compact_tests, run_lint_and_headers

SERVER_INSTRUCTIONS = (
    "Tool-First Development MCP server (project-dev) backed by the project SQLite knowledge graph. "
    "Use these tools BEFORE broad file reads or shell grep/pytest/ruff: "
    "repo_map (fast offline architectural map of indexed modules, roles, purposes, and exports), "
    "get_conventions (rules/patterns/examples for a role or area), "
    "find_similar (semantic search for closest existing modules), "
    "lookup_symbol (definitions, callers, callees, importers, and covering tests), "
    "run_tests (compact failure-only pytest runner — use instead of shell pytest), "
    "lint_and_headers (Ruff + module header validation — use instead of shell ruff), "
    "index_status / reindex (knowledge-graph freshness), and tool_metrics (adoption stats)."
)

mcp = MCPServer("project-dev", instructions=SERVER_INSTRUCTIONS)


def _run_observed(
    tool_name: str,
    args: dict[str, Any],
    fn: Callable[[ToolkitConfig], Any],
) -> str:
    cfg = load_config()
    t0 = time.monotonic()
    try:
        result = fn(cfg)
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        log_mcp_call(
            cfg.usage_log_path,
            tool=tool_name,
            args=args,
            result_bytes=len(payload.encode("utf-8")),
            duration_ms=(time.monotonic() - t0) * 1000.0,
            ok=True,
        )
        return payload
    except Exception as exc:
        err_payload = json.dumps({"ok": False, "error": str(exc)}, indent=2)
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


def _extract_rules_sections(rules_text: str, topic: str) -> list[str]:
    """Return matching sections from RULES.md for a role, pattern, or area."""
    needle = topic.strip().lower()
    if not needle:
        return [rules_text[:3000]]

    sections = re.split(r"(?=^#{2,3}\s+)", rules_text, flags=re.MULTILINE)
    matched: list[str] = []
    for sec in sections:
        if needle in sec.lower():
            matched.append(sec.strip())
    if not matched:
        return [rules_text[:2500]]
    return matched[:5]


@mcp.tool()
def repo_map(
    role: str | None = None,
    scope: str | None = None,
    include_summaries: bool = False,
) -> str:
    """Return a fast offline architectural map of indexed modules (path, role, purpose, exports, tests)."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        store = GraphStore(cfg.db_path)
        return store.repo_map(role=role, scope=scope, include_summaries=include_summaries)

    return _run_observed(
        "repo_map",
        {"role": role, "scope": scope, "include_summaries": include_summaries},
        _exec,
    )


@mcp.tool()
def find_similar(query: str, role: str | None = None, limit: int = 5) -> str:
    """Find the closest existing modules using OpenWeightGym embeddings, with optional role filter."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        store = GraphStore(cfg.db_path)
        files = store.get_all_files()
        vectors = store.get_all_vectors()
        client = OWGClient(
            load_owg_settings(
                cfg.project_root,
                suite_application=cfg.embeddings.get("suite_application", "tool-first-dev"),
            )
        )
        res = search_similar(
            query=query,
            files=files,
            vectors=vectors,
            owg_client=client,
            role=role,
            limit=limit,
        )
        return {
            "query": query,
            "role_filter": role,
            "indexed_file_count": len(files),
            **res,
        }

    return _run_observed("find_similar", {"query": query, "role": role, "limit": limit}, _exec)


@mcp.tool()
def get_conventions(topic: str) -> str:
    """Return RULES.md conventions for an area, role tag, or pattern name plus matching example files."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        rules_text = (
            cfg.rules_path.read_text(encoding="utf-8") if cfg.rules_path.exists() else ""
        )
        sections = _extract_rules_sections(rules_text, topic)
        store = GraphStore(cfg.db_path)
        examples = store.find_examples(topic, limit=5)
        return {
            "topic": topic,
            "allowed_roles": list(cfg.roles),
            "allowed_patterns": list(cfg.patterns),
            "rules_sections": sections,
            "example_files": examples,
        }

    return _run_observed("get_conventions", {"topic": topic}, _exec)


@mcp.tool()
def lookup_symbol(symbol: str, include_impact: bool = True) -> str:
    """Return symbol definitions, callers, callees, importers, and covering tests from the knowledge graph."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        store = GraphStore(cfg.db_path)
        return store.lookup_symbol(symbol=symbol, include_impact=include_impact)

    return _run_observed(
        "lookup_symbol", {"symbol": symbol, "include_impact": include_impact}, _exec
    )


@mcp.tool()
def run_tests(scope: str | None = None) -> str:
    """Run pytest and return compact machine-readable results (summary counts and failures only)."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        return run_compact_tests(scope=scope, config=cfg)

    return _run_observed("run_tests", {"scope": scope}, _exec)


@mcp.tool()
def lint_and_headers(scope: str | None = None) -> str:
    """Validate required module headers (roles/patterns/fields) and run Ruff lint checks."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        return run_lint_and_headers(scope=scope, config=cfg)

    return _run_observed("lint_and_headers", {"scope": scope}, _exec)


@mcp.tool()
def index_status() -> str:
    """Report knowledge-graph freshness, stale summaries/embeddings, and invalid module headers."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        return get_index_status(config=cfg)

    return _run_observed("index_status", {}, _exec)


@mcp.tool()
def reindex(full: bool = False, sync_remote: bool = True) -> str:
    """Incrementally (or fully) rebuild the SQLite knowledge graph, OWG summaries, and OWG embeddings."""

    def _exec(cfg: ToolkitConfig) -> dict[str, Any]:
        return reindex_project(
            config=cfg,
            full=full,
            sync_remote=sync_remote,
            trigger="mcp:reindex",
        )

    return _run_observed("reindex", {"full": full, "sync_remote": sync_remote}, _exec)


@mcp.tool()
def tool_metrics(limit: int = 15) -> str:
    """Return MCP tool usage stats, average payload sizes, fallback counts, and non-MCP tool usage summary."""

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
