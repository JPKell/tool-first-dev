"""End-to-end tests for the Tool-First Development toolkit (headers, parser, OWG graph, hooks, scaffold)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.config import load_config
from src.graph_store import GraphStore
from src.headers import parse_and_validate_header
from src.hook_handler import handle_post_tool, handle_pre_tool
from src.indexer import get_index_status, reindex_project
from src.observability import log_agent_fallback, log_mcp_call, summarize_metrics
from src.owg_client import load_owg_settings
from src.similarity import search_similar
from src.test_runner import run_compact_tests, run_lint_and_headers


class FakeOWGClient:
    """Deterministic stub mimicking OpenWeightGym /v1/embeddings and /v1/chat/completions."""

    def __init__(self) -> None:
        self.summary_calls: list[str] = []
        self.embed_calls: list[list[str]] = []

    def summarize_module(
        self,
        rel_path: str,
        role: str,
        patterns: list[str],
        source: str,
        max_source_chars: int = 8000,
        trace: str = "kg-summary",
    ) -> tuple[str, str]:
        self.summary_calls.append(rel_path)
        return "fake-chat-small", f"Summary for {rel_path} ({role})."

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [
            1.0 if "billing" in low or "total" in low else 0.1,
            1.0 if "math" in low or "arithmetic" in low else 0.1,
            1.0 if "widget" in low or "render" in low else 0.1,
            1.0 if "test" in low else 0.1,
        ]

    def embed_texts(
        self, texts: list[str], trace: str = "kg-embeddings"
    ) -> tuple[str, list[list[float]]]:
        self.embed_calls.append(list(texts))
        return "fake-embed", [self._vec(t) for t in texts]


def _make_sample_repo(tmp_path: Path) -> Path:
    agents = tmp_path / ".agents"
    agents.mkdir(parents=True)
    (agents / "config.json").write_text(
        json.dumps(
            {
                "project_name": "demo",
                "source_dirs": ["app", "tests"],
                "header_required_dirs": ["app"],
                "ignore_dirs": [".git", ".agents", "__pycache__"],
                "extensions": [".py", ".js"],
                "roles": ["service", "util", "test"],
                "patterns": ["service-layer", "structured-errors", "none"],
                "required_header_fields": [
                    "Purpose",
                    "Role",
                    "Patterns",
                    "Depends on",
                    "Exposes",
                    "Tests",
                    "Invariants",
                ],
                "embeddings": {"provider": "openweightgym"},
                "summaries": {"provider": "openweightgym"},
            }
        ),
        encoding="utf-8",
    )
    (agents / "RULES.md").write_text(
        "## service-layer\nPut domain logic in service classes.\n",
        encoding="utf-8",
    )
    (agents / "ruff.toml").write_text('target-version = "py312"\n', encoding="utf-8")

    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "app" / "math_util.py").write_text(
        '"""\n'
        "Purpose: Provide pure arithmetic helpers.\n"
        "Role: util\n"
        "Patterns: none\n"
        "Depends on: none\n"
        "Exposes: add_numbers\n"
        "Tests: tests/test_math.py\n"
        "Invariants: Pure stateless functions.\n"
        '"""\n\n'
        "def add_numbers(a: int, b: int) -> int:\n"
        '    """Add two integers."""\n'
        "    return a + b\n",
        encoding="utf-8",
    )

    (tmp_path / "app" / "calc_service.py").write_text(
        '"""\n'
        "Purpose: Orchestrate billing calculations using math helpers.\n"
        "Role: service\n"
        "Patterns: service-layer, structured-errors\n"
        "Depends on: app.math_util\n"
        "Exposes: BillingService\n"
        "Tests: tests/test_math.py\n"
        "Invariants: Totals are non-negative integers.\n"
        '"""\n\n'
        "from app.math_util import add_numbers\n\n\n"
        "class BillingService:\n"
        '    """Compute totals."""\n\n'
        "    def total(self, x: int, y: int) -> int:\n"
        "        return add_numbers(x, y)\n",
        encoding="utf-8",
    )

    (tmp_path / "app" / "widget.js").write_text(
        "/**\n"
        " * Purpose: Render a simple total widget.\n"
        " * Role: util\n"
        " * Patterns: none\n"
        " * Depends on: none\n"
        " * Exposes: formatTotal, renderBadge\n"
        " * Tests: none\n"
        " * Invariants: Always returns a string.\n"
        " */\n\n"
        "export function formatTotal(n) {\n"
        '  return String(n ?? "")\n'
        '    .replaceAll("&", "&amp;")\n'
        "    .trim();\n"
        "}\n\n"
        "export const renderBadge = (n) => {\n"
        "  return formatTotal(n);\n"
        "};\n",
        encoding="utf-8",
    )

    (tmp_path / "app" / "page.js").write_text(
        "/**\n"
        " * Purpose: Mount the widget on the page.\n"
        " * Role: util\n"
        " * Patterns: none\n"
        " * Depends on: app/widget.js\n"
        " * Exposes: mountWidget\n"
        " * Tests: none\n"
        " * Invariants: Safe to call once.\n"
        " */\n\n"
        'import { formatTotal, renderBadge } from "./widget.js";\n\n'
        "export function mountWidget(n) {\n"
        "  return renderBadge(n) + formatTotal(n);\n"
        "}\n\n"
        "mountWidget(1);\n",
        encoding="utf-8",
    )

    (tmp_path / "tests" / "test_math.py").write_text(
        "from app.calc_service import BillingService\n\n\n"
        "def test_total():\n"
        "    assert BillingService().total(2, 3) == 5\n",
        encoding="utf-8",
    )
    return tmp_path


def test_dotenv_aliases_for_owg_settings(tmp_path: Path, monkeypatch: Any) -> None:
    for var in ("OWG_HOST", "API_IP", "OWG_PORT", "API_PORT", "OWG_API_KEY", "API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "API_IP=10.0.0.5\nAPI_PORT=8442\nAPI_KEY=secret-test\n",
        encoding="utf-8",
    )
    settings = load_owg_settings(tmp_path)
    assert settings.is_configured is True
    assert settings.base_url == "http://10.0.0.5:8442/v1"
    assert settings.api_key == "secret-test"


def test_header_validation_catches_invalid_role_and_pattern(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)
    bad_src = (
        '"""\n'
        "Purpose: Broken header.\n"
        "Role: wizard\n"
        "Patterns: unknown-pattern\n"
        "Depends on: none\n"
        "Exposes: foo\n"
        "Tests: none\n"
        '"""\n'
    )
    h = parse_and_validate_header(Path("app/bad.py"), bad_src, cfg)
    assert not h.is_valid
    joined = " ".join(h.errors)
    assert "Invariants" in joined
    assert "Unknown Role 'wizard'" in joined
    assert "Unknown Pattern 'unknown-pattern'" in joined


def test_indexer_owg_summaries_embeddings_and_incremental_skip(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)
    fake = FakeOWGClient()

    res = reindex_project(config=cfg, full=True, sync_remote=True, trigger="test", owg_client=fake)  # type: ignore[arg-type]
    assert len(res["updated_files"]) == 5
    assert len(res["summarized_files"]) == 5
    assert len(res["embedded_files"]) == 5
    assert res["invalid_headers"] == []
    assert res["remote_errors"] == []

    # Second incremental run must make ZERO additional OpenWeightGym API calls
    fake.summary_calls.clear()
    fake.embed_calls.clear()
    inc = reindex_project(config=cfg, full=False, sync_remote=True, trigger="test-inc", owg_client=fake)  # type: ignore[arg-type]
    assert inc["updated_files"] == []
    assert inc["skipped_unchanged"] == 5
    assert fake.summary_calls == []
    assert fake.embed_calls == []

    status = get_index_status(config=cfg)
    assert status["fresh"] is True

    store = GraphStore(cfg.db_path)
    lookup = store.lookup_symbol("add_numbers")
    assert len(lookup["definitions"]) == 1
    assert lookup["definitions"][0]["file_path"] == "app/math_util.py"
    assert any(c["source_symbol"] == "BillingService.total" for c in lookup["callers"])
    assert "tests/test_math.py" in lookup["covering_tests"]

    sim = search_similar(
        query="billing calculations total",
        files=store.get_all_files(),
        vectors=store.get_all_vectors(),
        owg_client=fake,  # type: ignore[arg-type]
        role="service",
    )
    assert sim["matches"]
    assert sim["matches"][0]["path"] == "app/calc_service.py"


def test_pre_tool_guardrails_antigravity_and_claude(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)

    # Antigravity: block editing .agents/state/
    res1 = handle_pre_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "write_file", "args": {"Path": str(repo / ".agents/state/knowledge_graph.db")}},
        }
    )
    assert res1["decision"] == "deny"

    # Antigravity: block editing .env
    res2 = handle_pre_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "edit_file", "args": {"Path": str(repo / ".env")}},
        }
    )
    assert res2["decision"] == "deny"

    # Antigravity: block raw pytest, allow git status
    res3 = handle_pre_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
        }
    )
    assert res3["decision"] == "deny"

    res4 = handle_pre_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
        }
    )
    assert res4["decision"] == "allow"

    # Claude Code format: block raw ruff check
    res5 = handle_pre_tool(
        {
            "cwd": str(repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ruff check app/"},
        }
    )
    assert res5["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_lint_and_compact_test_runner(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)

    lint = run_lint_and_headers(config=cfg)
    assert lint["ok"] is True
    assert lint["header_issues"] == []
    assert lint["ruff_issues"] == []

    tests_ok = run_compact_tests(config=cfg)
    assert tests_ok["ok"] is True
    assert tests_ok["passed"] == 1
    assert tests_ok["failures"] == []
    assert tests_ok["python"]

    (repo / "tests" / "test_math.py").write_text(
        "def test_broken():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    tests_fail = run_compact_tests(config=cfg)
    assert tests_fail["ok"] is False
    assert tests_fail["failed"] == 1
    assert len(tests_fail["failures"]) == 1
    assert "test_broken" in tests_fail["failures"][0]["test"]
    assert tests_fail["python"]


def test_observability_and_stop_hook(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)

    log_mcp_call(cfg.usage_log_path, "find_similar", {"query": "billing"}, 256, 12.5, ok=True)
    log_agent_fallback(cfg.usage_log_path, "read_file", "app/calc_service.py")
    metrics = summarize_metrics(cfg.usage_log_path)
    assert metrics["total_mcp_calls"] == 1
    assert metrics["total_fallbacks"] == 1
    assert metrics["fallbacks_after_mcp"]["find_similar"] == 1

    toolkit_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "src.hook_handler", "stop"],
        cwd=toolkit_dir,
        input=json.dumps({"workspacePaths": [str(repo)], "terminationReason": "NO_TOOL_CALL"}),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == {"decision": "allow"}
    # AST/header graph must still be populated even if OWG is unconfigured in temp repo
    assert GraphStore(cfg.db_path).stats()["files"] == 5


def test_non_mcp_tool_usage_logging_and_metrics(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)

    # 1. Unguided non-MCP read of an indexed source file (before any MCP call) -> flagged as missed opportunity
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "conversationId": "conv-1",
            "modelName": "gemini",
            "stepIdx": 3,
            "toolCall": {"name": "read_file", "args": {"Path": str(repo / "app/calc_service.py")}},
        }
    )

    # Seed an MCP discovery call; subsequent read_file on indexed source is normal read-before-edit (not missed!)
    log_mcp_call(cfg.usage_log_path, "lookup_symbol", {"symbol": "BillingService"}, 512, 5.0, ok=True)
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "conversationId": "conv-1",
            "modelName": "gemini",
            "stepIdx": 4,
            "toolCall": {"name": "read_file", "args": {"Path": str(repo / "app/math_util.py")}},
        }
    )

    # 2. Non-MCP read of RULES.md -> suggests get_conventions
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "read_file", "args": {"Path": str(repo / ".agents/RULES.md")}},
        }
    )

    # 3. Non-MCP shell grep -> suggests lookup_symbol / find_similar; git status -> no suggestion
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "rg BillingService app/"}},
        }
    )
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
        }
    )

    # 4. Non-MCP search_web -> logged even though not in the old 4-tool matcher
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "search_web", "args": {"query": "fastapi lifespan"}},
        }
    )

    # 5. MCP tools must NOT be logged in non_mcp_tool_usage.jsonl
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "call_mcp_tool", "args": {"ToolName": "run_tests"}},
        }
    )
    handle_post_tool(
        {
            "cwd": str(repo),
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__project-dev__run_tests",
            "tool_input": {},
        }
    )

    # 6. Blocked PreToolUse attempt (raw pytest) IS logged as pre_tool_blocked
    res_block = handle_pre_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
        }
    )
    assert res_block["decision"] == "deny"

    lines = cfg.non_mcp_log_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines]
    assert len(records) == 7
    assert all(r["tool"] not in {"call_mcp_tool", "mcp__project-dev__run_tests"} for r in records)

    # Record 0: unguided read -> flagged
    assert records[0]["tool"] == "read_file"
    assert records[0]["is_indexed_source"] is True
    assert records[0]["suggested_mcp_tool"] == "repo_map / lookup_symbol / find_similar"
    assert records[0]["conversation_id"] == "conv-1"
    assert records[0]["followed_mcp_tool"] is None

    # Record 1: guided read right after lookup_symbol -> NOT flagged as missed opportunity
    assert records[1]["tool"] == "read_file"
    assert records[1]["is_indexed_source"] is True
    assert records[1]["followed_mcp_tool"] == "lookup_symbol"
    assert records[1]["suggested_mcp_tool"] is None

    assert records[2]["suggested_mcp_tool"] == "get_conventions"
    assert records[3]["suggested_mcp_tool"] == "lookup_symbol / find_similar"
    assert records[4]["suggested_mcp_tool"] is None
    assert records[5]["tool"] == "search_web"
    assert records[6]["hook"] == "pre_tool_blocked"
    assert records[6]["suggested_mcp_tool"] == "run_tests"
    assert records[6]["blocked_reason"]

    metrics = summarize_metrics(
        cfg.usage_log_path,
        recent_limit=10,
        non_mcp_log_path=cfg.non_mcp_log_path,
    )
    non_mcp = metrics["non_mcp_summary"]
    assert non_mcp["total_non_mcp_calls"] == 7
    assert non_mcp["blocked_attempts"] == 1
    assert non_mcp["mcp_opportunity_count"] == 4
    assert non_mcp["by_tool"]["run_command"] == 3
    assert non_mcp["by_tool"]["read_file"] == 3
    assert non_mcp["by_tool"]["search_web"] == 1
    assert non_mcp["by_suggested_mcp"]["run_tests"] == 1
    assert non_mcp["by_suggested_mcp"]["get_conventions"] == 1
    assert non_mcp["mcp_share_pct"] == 12.5


def test_call_graph_signatures_class_callees_and_js_fidelity(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)
    reindex_project(config=cfg, full=True, sync_remote=False, trigger="test-cg")

    store = GraphStore(cfg.db_path)

    # 1. Full typed Python signature must not be truncated at the first parameter colon
    add_info = store.lookup_symbol("add_numbers")
    assert add_info["definitions"][0]["signature"] == "def add_numbers(a: int, b: int) -> int"

    # 2. Class lookup must include callees from its methods
    billing_info = store.lookup_symbol("BillingService")
    assert any(
        c["source_symbol"] == "BillingService.total" and c["target"] == "add_numbers"
        for c in billing_info["callees"]
    )

    # 3. JS arrow functions, named imports, module-level calls, and clean callees (no multiline chain noise)
    fmt_info = store.lookup_symbol("formatTotal")
    assert any(c["source_symbol"] == "renderBadge" for c in fmt_info["callers"])
    assert any(c["source_symbol"] == "mountWidget" for c in fmt_info["callers"])
    assert any(i["source_file"] == "app/page.js" for i in fmt_info["importers"])
    assert all("\n" not in c["target"] for c in fmt_info["callees"])
    assert all(c["target"] not in {"String", "trim"} for c in fmt_info["callees"])

    badge_info = store.lookup_symbol("renderBadge")
    assert len(badge_info["definitions"]) == 1
    assert badge_info["definitions"][0]["file_path"] == "app/widget.js"
    assert any(i["source_file"] == "app/page.js" for i in badge_info["importers"])

    mount_info = store.lookup_symbol("mountWidget")
    assert any(c["source_symbol"] == "<module>" for c in mount_info["callers"])


def test_hook_guardrails_venv_paths_and_mcp_schema_reads(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)

    # Block path-prefixed pytest and ruff binaries (.venv/bin/pytest, .venv/bin/python -m pytest, .venv/bin/ruff)
    for cmd in (
        ".venv/bin/pytest -q",
        ".venv/bin/python -m pytest tests/",
        "/opt/app-venv/bin/python3 -m pytest -q",
        ".venv/bin/ruff check app/",
    ):
        res = handle_pre_tool(
            {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
            }
        )
        assert res["decision"] == "deny", f"Expected deny for: {cmd}"

    # Reading Antigravity MCP schema JSON must NOT be logged as fallback or non-MCP tool usage
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {
                "name": "read_file",
                "args": {"Path": "/home/user/.gemini/antigravity-cli/mcp/dev-tools_project-dev/run_tests.json"},
            },
        }
    )
    # Eager Antigravity MCP tool calls (mcp_dev-tools_project-dev_*) must NOT be logged as non-MCP
    handle_post_tool(
        {
            "workspacePaths": [str(repo)],
            "toolCall": {"name": "mcp_dev-tools_project-dev_run_tests", "args": {}},
        }
    )
    assert not cfg.usage_log_path.exists()
    # Only the 4 blocked attempts should be in non_mcp_log_path (neither schema read nor eager MCP call)
    records = [json.loads(ln) for ln in cfg.non_mcp_log_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 4
    assert all(r["hook"] == "pre_tool_blocked" for r in records)


def test_mcp_server_instructions_and_agent_configs() -> None:
    import tomllib

    from src.mcp_server import mcp

    assert mcp.instructions and "lookup_symbol" in mcp.instructions and "run_tests" in mcp.instructions

    repo_root = Path(__file__).resolve().parents[4]

    # Antigravity plugin mcp_config.json marks tools eager
    agy_mcp = json.loads(
        (repo_root / ".agents/plugins/dev-tools/mcp_config.json").read_text(encoding="utf-8")
    )
    server_cfg = agy_mcp["mcpServers"]["project-dev"]
    for tool_name in (
        "repo_map",
        "lookup_symbol",
        "find_similar",
        "get_conventions",
        "run_tests",
        "lint_and_headers",
        "index_status",
    ):
        assert server_cfg["tools"][tool_name]["eager"] is True

    # Claude Code settings pre-approve project-dev and disable deferred tool search
    claude_settings = json.loads((repo_root / ".claude/settings.json").read_text(encoding="utf-8"))
    assert "project-dev" in claude_settings.get("enabledMcpjsonServers", [])
    assert claude_settings.get("env", {}).get("ENABLE_TOOL_SEARCH") == "false"
    assert "mcp__project-dev__*" in claude_settings.get("permissions", {}).get("allow", [])

    # Codex config.toml & hooks.json
    codex_cfg = tomllib.loads((repo_root / ".codex/config.toml").read_text(encoding="utf-8"))
    codex_srv = codex_cfg["mcp_servers"]["project-dev"]
    assert codex_srv["enabled"] is True
    assert codex_srv["tool_timeout_sec"] >= 120

    codex_hooks = json.loads((repo_root / ".codex/hooks.json").read_text(encoding="utf-8"))
    assert "PreToolUse" in codex_hooks["hooks"]
    assert "PostToolUse" in codex_hooks["hooks"]
    assert "Stop" in codex_hooks["hooks"]

    # Plan tooling template exists in .agents/ and references all core MCP tools
    plan_doc = (repo_root / ".agents/PLAN_TOOLING_SECTION.md").read_text(encoding="utf-8")
    for tok in (
        "repo_map",
        "get_conventions",
        "find_similar",
        "lookup_symbol",
        "run_tests",
        "lint_and_headers",
        "index_status",
        "tool_metrics",
    ):
        assert tok in plan_doc


def test_repo_map_and_bounded_concurrent_summarization(tmp_path: Path) -> None:
    repo = _make_sample_repo(tmp_path)
    cfg = load_config(repo)
    fake = FakeOWGClient()

    # Cap remote summaries (like hook:stop does) so large turns don't exceed hook timeouts
    res_capped = reindex_project(
        config=cfg,
        full=True,
        sync_remote=True,
        trigger="hook:stop",
        owg_client=fake,  # type: ignore[arg-type]
        max_remote_summaries=2,
    )
    assert len(res_capped["summarized_files"]) == 2
    assert res_capped["deferred_summaries"] == 3

    # Follow-up uncapped reindex finishes the remaining 3 summaries concurrently
    fake.summary_calls.clear()
    res_rest = reindex_project(
        config=cfg,
        full=False,
        sync_remote=True,
        trigger="mcp:reindex",
        owg_client=fake,  # type: ignore[arg-type]
    )
    assert len(res_rest["summarized_files"]) == 3
    assert res_rest["deferred_summaries"] == 0
    assert get_index_status(config=cfg)["fresh"] is True

    store = GraphStore(cfg.db_path)
    full_map = store.repo_map()
    assert full_map["total_modules"] == 5
    assert full_map["counts_by_role"]["util"] == 3
    assert full_map["counts_by_role"]["service"] == 1
    assert all("summary" not in m for m in full_map["modules"])

    svc_map = store.repo_map(role="service", include_summaries=True)
    assert svc_map["total_modules"] == 1
    assert svc_map["modules"][0]["path"] == "app/calc_service.py"
    assert "Summary for app/calc_service.py" in svc_map["modules"][0]["summary"]

    scoped_map = store.repo_map(scope="app/widget.js")
    assert scoped_map["total_modules"] == 1
    assert scoped_map["modules"][0]["path"] == "app/widget.js"
