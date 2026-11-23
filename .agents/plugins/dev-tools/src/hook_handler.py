"""Multi-agent lifecycle hook handler (Antigravity + Claude Code) for guardrails, sync, and telemetry."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .config import ToolkitConfig, load_config
from .indexer import reindex_project
from .observability import log_agent_fallback, log_non_mcp_tool, recent_mcp_call

RAW_PYTEST_RE = re.compile(
    r"(^|[;&|\s])(?:[\w./-]+/)?(?:python3?(?:\.\d+)?\s+-m\s+)?(?:[\w./-]+/)?pytest(\s|$)"
)
RAW_RUFF_RE = re.compile(r"(^|[;&|\s])(?:[\w./-]+/)?ruff\s+(check|format)(\s|$)")
SEARCH_CMD_RE = re.compile(r"(^|[;&|\s])(grep|rg|ripgrep|find|fd|fdfind|ag|ack)(\s|$)")
PROTECTED_FILENAMES = {".env", ".env.local", ".env.production", "secrets.json"}
MCP_TOOL_NAMES = {"call_mcp_tool", "list_resources", "read_resource"}
MCP_DISCOVERY_TOOLS = frozenset({"repo_map", "lookup_symbol", "find_similar", "get_conventions"})
STOP_HOOK_MAX_SUMMARIES = 6
MCP_SCHEMA_MARKERS = (
    "/.gemini/antigravity-cli/mcp/",
    "/.gemini/antigravity/mcp/",
)


def _read_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _is_claude_payload(payload: dict[str, Any]) -> bool:
    return "tool_name" in payload or "hook_event_name" in payload


def _agent_name(payload: dict[str, Any]) -> str:
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "codex"
    return "claude" if _is_claude_payload(payload) else "antigravity"


def _is_mcp_tool(tool_name: str) -> bool:
    low = tool_name.strip().lower()
    return (
        low in MCP_TOOL_NAMES
        or low.startswith("mcp__")
        or low.startswith("mcp_")
        or low.startswith("dev-tools_")
        or low.startswith("project-dev")
    )


def _is_mcp_schema_read(tool_name: str, path_str: str) -> bool:
    if tool_name not in {"read_file", "Read"} or not path_str:
        return False
    norm = path_str.replace("\\", "/")
    return any(marker in norm for marker in MCP_SCHEMA_MARKERS)


def _resolve_root_from_payload(payload: dict[str, Any]) -> Path | None:
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces:
        return Path(workspaces[0])
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return None


def _extract_tool_info(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "toolCall" in payload and isinstance(payload["toolCall"], dict):
        tc = payload["toolCall"]
        return str(tc.get("name", "")), dict(tc.get("args") or {})
    return str(payload.get("tool_name", "")), dict(payload.get("tool_input") or {})


def _extract_path_arg(args: dict[str, Any]) -> str:
    for key in ("Path", "file_path", "path"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_cmd_arg(args: dict[str, Any]) -> str:
    for key in ("CommandLine", "command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_target_summary(tool_name: str, args: dict[str, Any]) -> str:
    path_val = _extract_path_arg(args)
    if path_val:
        return path_val
    cmd_val = _extract_cmd_arg(args)
    if cmd_val:
        return cmd_val
    for key in ("pattern", "query", "url", "Prompt", "Role", "Action", "ToolName"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if args:
        safe_keys = sorted(str(k) for k in args)
        return f"args({', '.join(safe_keys)})"
    return tool_name


def _resolve_relative_path(cfg: ToolkitConfig, path_str: str) -> Path | None:
    if not path_str:
        return None
    try:
        p = Path(path_str)
        resolved = p.resolve() if p.is_absolute() else (cfg.project_root / p).resolve()
        return resolved.relative_to(cfg.project_root)
    except (ValueError, OSError):
        return None


def _is_indexed_source_path(cfg: ToolkitConfig, rel: Path | None) -> bool:
    if rel is None:
        return False
    posix = rel.as_posix()
    return rel.suffix in cfg.extensions and any(
        posix == d or posix.startswith(f"{d}/") for d in cfg.source_dirs
    )


def _suggest_mcp_tool(
    cfg: ToolkitConfig,
    tool_name: str,
    args: dict[str, Any],
    rel_path: Path | None,
    *,
    followed_mcp_tool: str | None = None,
) -> str | None:
    if tool_name in {"run_command", "Bash"}:
        cmd = _extract_cmd_arg(args)
        if RAW_PYTEST_RE.search(cmd):
            return "run_tests"
        if RAW_RUFF_RE.search(cmd):
            return "lint_and_headers"
        if SEARCH_CMD_RE.search(cmd):
            return "lookup_symbol / find_similar"
        return None

    if tool_name in {"Grep", "Glob", "find_by_name", "grep_search"}:
        return "lookup_symbol / find_similar"

    if tool_name in {"read_file", "Read"} and rel_path is not None:
        posix = rel_path.as_posix()
        if posix in {".agents/RULES.md", ".agents/config.json"}:
            return "get_conventions"
        if _is_indexed_source_path(cfg, rel_path):
            # Reading a source file right after an MCP discovery call is normal read-before-edit
            if followed_mcp_tool in MCP_DISCOVERY_TOOLS:
                return None
            return "repo_map / lookup_symbol / find_similar"
    return None


def _record_non_mcp(
    cfg: ToolkitConfig,
    payload: dict[str, Any],
    *,
    hook: str,
    tool_name: str,
    args: dict[str, Any],
    blocked_reason: str | None = None,
) -> None:
    if not tool_name or _is_mcp_tool(tool_name):
        return
    path_str = _extract_path_arg(args)
    if _is_mcp_schema_read(tool_name, path_str):
        return
    rel = _resolve_relative_path(cfg, path_str)
    if (
        rel is not None
        and (rel.as_posix() == ".agents/state" or rel.as_posix().startswith(".agents/state/"))
        and hook != "pre_tool_blocked"
    ):
        # Still record blocked attempts to edit .agents/state/, skip routine reads of logs
        return

    target = _extract_target_summary(tool_name, args)
    conv_id = payload.get("conversationId") or payload.get("session_id")
    model_name = payload.get("modelName")
    step_idx = payload.get("stepIdx") if isinstance(payload.get("stepIdx"), int) else None
    err_raw = payload.get("error")
    err_str = str(err_raw) if err_raw else None

    followed_mcp, _ = recent_mcp_call(cfg.usage_log_path)
    log_non_mcp_tool(
        cfg.non_mcp_log_path,
        cfg.usage_log_path,
        agent=_agent_name(payload),
        hook=hook,
        tool_name=tool_name,
        target=target,
        args_keys=sorted(str(k) for k in args),
        conversation_id=str(conv_id) if conv_id else None,
        model_name=str(model_name) if model_name else None,
        step_idx=step_idx,
        error=err_str,
        is_indexed_source=_is_indexed_source_path(cfg, rel),
        suggested_mcp_tool=_suggest_mcp_tool(
            cfg, tool_name, args, rel, followed_mcp_tool=followed_mcp
        ),
        blocked_reason=blocked_reason,
    )


def _deny_response(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    if _is_claude_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {"decision": "deny", "reason": reason}


def _allow_response(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_claude_payload(payload):
        return {}
    return {"decision": "allow"}


def handle_pre_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce Tool-First Never list: block edits to index/secrets and raw shell pytest/ruff."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        tool_name, args = _extract_tool_info(payload)

        if tool_name in {"write_file", "edit_file", "Write", "Edit", "MultiEdit"}:
            path_str = _extract_path_arg(args)
            if path_str:
                target = Path(path_str)
                resolved = target.resolve() if target.is_absolute() else (cfg.project_root / target).resolve()
                try:
                    rel = resolved.relative_to(cfg.project_root).as_posix()
                except ValueError:
                    rel = path_str

                if rel == ".agents/state" or rel.startswith(".agents/state/"):
                    reason = (
                        "Blocked by Tool-First guardrail: never hand-edit .agents/state/ index or logs. "
                        "Use MCP tools (reindex, index_status, tool_metrics) instead."
                    )
                    _record_non_mcp(
                        cfg, payload, hook="pre_tool_blocked", tool_name=tool_name, args=args, blocked_reason=reason
                    )
                    return _deny_response(payload, reason)
                if Path(rel).name in PROTECTED_FILENAMES:
                    reason = (
                        f"Blocked by Tool-First guardrail: do not edit secret file '{Path(rel).name}'. "
                        "Ask the user to update secrets manually."
                    )
                    _record_non_mcp(
                        cfg, payload, hook="pre_tool_blocked", tool_name=tool_name, args=args, blocked_reason=reason
                    )
                    return _deny_response(payload, reason)

        if tool_name in {"run_command", "Bash"}:
            cmd = _extract_cmd_arg(args)
            # Allow toolkit self-setup and toolkit self-tests inside .agents/plugins/dev-tools
            if ".agents/plugins/dev-tools" in cmd:
                return _allow_response(payload)

            if RAW_PYTEST_RE.search(cmd):
                reason = (
                    "Blocked by Tool-First guardrail: use MCP tool 'run_tests' "
                    "(server: project-dev / dev-tools_project-dev) instead of shell pytest "
                    "for compact, machine-readable failure output."
                )
                _record_non_mcp(
                    cfg, payload, hook="pre_tool_blocked", tool_name=tool_name, args=args, blocked_reason=reason
                )
                return _deny_response(payload, reason)
            if RAW_RUFF_RE.search(cmd):
                reason = (
                    "Blocked by Tool-First guardrail: use MCP tool 'lint_and_headers' "
                    "(server: project-dev / dev-tools_project-dev) instead of shell ruff "
                    "so both Ruff lint and module headers are verified together."
                )
                _record_non_mcp(
                    cfg, payload, hook="pre_tool_blocked", tool_name=tool_name, args=args, blocked_reason=reason
                )
                return _deny_response(payload, reason)
    except Exception as exc:
        print(f"[dev-tools hook:pre_tool] {exc}", file=sys.stderr)
    return _allow_response(payload)


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Incrementally update the knowledge graph (including OpenWeightGym summaries/embeddings) after each turn."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        reindex_project(
            config=cfg,
            full=False,
            sync_remote=True,
            trigger="hook:stop",
            max_remote_summaries=STOP_HOOK_MAX_SUMMARIES,
        )
    except Exception as exc:
        print(f"[dev-tools hook:stop] {exc}", file=sys.stderr)
    if _is_claude_payload(payload):
        return {}
    return {"decision": "allow"}


def handle_post_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Fast local AST/header sync after writes; log all non-MCP tool usage and fallback telemetry."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        tool_name, args = _extract_tool_info(payload)

        # Log every non-MCP tool invocation to .agents/state/non_mcp_tool_usage.jsonl
        _record_non_mcp(cfg, payload, hook="post_tool", tool_name=tool_name, args=args)

        if tool_name in {"write_file", "edit_file", "Write", "Edit", "MultiEdit"}:
            rel = _resolve_relative_path(cfg, _extract_path_arg(args))
            if _is_indexed_source_path(cfg, rel):
                # Fast local-only AST/header refresh mid-turn; remote OWG sync runs on Stop
                reindex_project(
                    config=cfg,
                    full=False,
                    sync_remote=False,
                    trigger=f"hook:{tool_name}",
                )
        elif tool_name in {"read_file", "Read"}:
            target = _extract_path_arg(args)
            if target and ".agents/state" not in target and not _is_mcp_schema_read(tool_name, target):
                log_agent_fallback(cfg.usage_log_path, tool_name, target)
        elif tool_name in {"run_command", "Bash", "Grep"}:
            cmd = _extract_cmd_arg(args) or str(args.get("pattern", ""))
            if tool_name == "Grep" or any(tok in cmd for tok in ("grep ", "rg ", "find ", "pytest", "ruff")):
                log_agent_fallback(cfg.usage_log_path, tool_name, cmd)
    except Exception as exc:
        print(f"[dev-tools hook:post_tool] {exc}", file=sys.stderr)
    return {}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    payload = _read_stdin_payload()
    if mode == "pre_tool":
        out = handle_pre_tool(payload)
    elif mode == "post_tool":
        out = handle_post_tool(payload)
    else:
        out = handle_stop(payload)
    # stdout MUST contain only the valid JSON hook response
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
