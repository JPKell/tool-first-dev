"""Observability logger and metrics summarizer for MCP calls and fallback reads/greps."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def append_event(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _now().isoformat(), **event}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_mcp_call(
    log_path: Path,
    tool: str,
    args: dict[str, Any],
    result_bytes: int,
    duration_ms: float,
    ok: bool = True,
    error: str | None = None,
) -> None:
    append_event(
        log_path,
        {
            "event": "mcp_call",
            "tool": tool,
            "args": args,
            "result_bytes": result_bytes,
            "duration_ms": round(duration_ms, 2),
            "ok": ok,
            "error": error,
        },
    )


def _last_mcp_call(log_path: Path) -> dict[str, Any] | None:
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-50:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "mcp_call":
            return rec
    return None


def recent_mcp_call(
    log_path: Path,
    max_age_seconds: float = 120.0,
) -> tuple[str | None, float | None]:
    """Return (tool_name, seconds_since) if an MCP call occurred within max_age_seconds."""
    prev = _last_mcp_call(log_path)
    if prev and "ts" in prev:
        try:
            prev_ts = datetime.fromisoformat(prev["ts"])
            delta = (_now() - prev_ts).total_seconds()
            if 0 <= delta <= max_age_seconds:
                return prev.get("tool"), round(delta, 2)
        except ValueError:
            pass
    return None, None


def log_agent_fallback(
    log_path: Path,
    tool_name: str,
    target: str,
) -> None:
    """Record read_file or shell grep/find calls and correlate with recent MCP calls."""
    followed_mcp, seconds_since = recent_mcp_call(log_path)

    append_event(
        log_path,
        {
            "event": "agent_fallback",
            "tool": tool_name,
            "target": target[:240],
            "followed_mcp_tool": followed_mcp,
            "seconds_since_mcp": seconds_since,
        },
    )


def log_non_mcp_tool(
    non_mcp_log_path: Path,
    mcp_log_path: Path,
    *,
    agent: str,
    hook: str,
    tool_name: str,
    target: str,
    args_keys: list[str] | None = None,
    conversation_id: str | None = None,
    model_name: str | None = None,
    step_idx: int | None = None,
    error: str | None = None,
    is_indexed_source: bool = False,
    suggested_mcp_tool: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    """Record every non-MCP tool invocation (or blocked attempt) for MCP-adoption auditing."""
    followed_mcp, seconds_since = recent_mcp_call(mcp_log_path)

    append_event(
        non_mcp_log_path,
        {
            "event": "non_mcp_tool",
            "agent": agent,
            "hook": hook,
            "tool": tool_name,
            "target": target[:300],
            "args_keys": args_keys or [],
            "conversation_id": conversation_id,
            "model": model_name,
            "step_idx": step_idx,
            "error": error[:240] if error else None,
            "is_indexed_source": is_indexed_source,
            "suggested_mcp_tool": suggested_mcp_tool,
            "blocked_reason": blocked_reason[:240] if blocked_reason else None,
            "followed_mcp_tool": followed_mcp,
            "seconds_since_mcp": seconds_since,
        },
    )


def summarize_non_mcp_usage(
    non_mcp_log_path: Path,
    recent_limit: int = 15,
) -> dict[str, Any]:
    """Summarize all logged non-MCP tool calls, guardrail blocks, and missed MCP opportunities."""
    if not non_mcp_log_path.exists():
        return {
            "log_path": str(non_mcp_log_path),
            "total_non_mcp_calls": 0,
            "blocked_attempts": 0,
            "mcp_opportunity_count": 0,
            "by_tool": {},
            "by_suggested_mcp": {},
            "recent": [],
        }

    records: list[dict[str, Any]] = []
    for line in non_mcp_log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_tool: Counter[str] = Counter()
    by_suggested: Counter[str] = Counter()
    blocked_attempts = 0
    opportunity_count = 0

    for rec in records:
        tool = str(rec.get("tool") or "unknown")
        by_tool[tool] += 1
        if rec.get("hook") == "pre_tool_blocked":
            blocked_attempts += 1
        suggested = rec.get("suggested_mcp_tool")
        if suggested:
            opportunity_count += 1
            by_suggested[str(suggested)] += 1

    return {
        "log_path": str(non_mcp_log_path),
        "total_non_mcp_calls": len(records),
        "blocked_attempts": blocked_attempts,
        "mcp_opportunity_count": opportunity_count,
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_suggested_mcp": dict(sorted(by_suggested.items(), key=lambda kv: (-kv[1], kv[0]))),
        "recent": records[-recent_limit:],
    }


def summarize_metrics(
    log_path: Path,
    recent_limit: int = 15,
    non_mcp_log_path: Path | None = None,
) -> dict[str, Any]:
    """Summarize MCP call counts, average payload sizes, fallback frequency, and non-MCP usage."""
    non_mcp = (
        summarize_non_mcp_usage(non_mcp_log_path, recent_limit=recent_limit)
        if non_mcp_log_path is not None
        else None
    )
    if not log_path.exists():
        out: dict[str, Any] = {
            "total_mcp_calls": 0,
            "total_fallbacks": 0,
            "by_tool": {},
            "fallbacks_after_mcp": {},
            "recent": [],
        }
        if non_mcp is not None:
            out["non_mcp_summary"] = non_mcp
        return out

    records: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_tool: dict[str, dict[str, Any]] = {}
    fallbacks_after: Counter[str] = Counter()
    total_mcp = 0
    total_fallbacks = 0

    for rec in records:
        ev = rec.get("event")
        if ev == "mcp_call":
            total_mcp += 1
            t = rec.get("tool", "unknown")
            bucket = by_tool.setdefault(t, {"calls": 0, "errors": 0, "total_bytes": 0, "total_ms": 0.0})
            bucket["calls"] += 1
            if not rec.get("ok", True):
                bucket["errors"] += 1
            bucket["total_bytes"] += int(rec.get("result_bytes", 0))
            bucket["total_ms"] += float(rec.get("duration_ms", 0.0))
        elif ev == "agent_fallback":
            total_fallbacks += 1
            if rec.get("followed_mcp_tool"):
                fallbacks_after[rec["followed_mcp_tool"]] += 1

    compact_by_tool = {
        tool: {
            "calls": b["calls"],
            "errors": b["errors"],
            "avg_bytes": round(b["total_bytes"] / b["calls"], 1) if b["calls"] else 0,
            "avg_ms": round(b["total_ms"] / b["calls"], 1) if b["calls"] else 0,
        }
        for tool, b in sorted(by_tool.items())
    }

    result: dict[str, Any] = {
        "total_mcp_calls": total_mcp,
        "total_fallbacks": total_fallbacks,
        "by_tool": compact_by_tool,
        "fallbacks_after_mcp": dict(fallbacks_after),
        "recent": records[-recent_limit:],
    }
    if non_mcp is not None:
        total_all = total_mcp + non_mcp["total_non_mcp_calls"]
        non_mcp["mcp_share_pct"] = round((total_mcp / total_all) * 100.0, 1) if total_all else 0.0
        result["non_mcp_summary"] = non_mcp
    return result
