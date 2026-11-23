"""
Purpose: Contract tests for call-graph accuracy, navigation tools, header derivation, and hardening.
Role: test
Patterns: contract-first-test
Tests: none
Invariants: Deterministic; uses temp repos and stub clients only (no network).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

from src.config import load_config
from src.graph_store import GraphStore
from src.headers import parse_and_validate_header
from src.hook_handler import handle_post_tool, handle_pre_tool
from src.indexer import get_index_status, reindex_project
from src.navigation import affected_tests, get_source, outline, search_text
from src.observability import log_mcp_call, summarize_metrics
from src.parser import parse_file
from src.scaffolding import scaffold_module
from src.similarity import search_similar
from src.test_runner import run_compact_tests, run_lint_and_headers
from tests.test_toolkit import _make_sample_repo

TOOLKIT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
LEAN_FIELDS = ["Purpose", "Role", "Patterns", "Tests", "Invariants"]


def _lean_repo(tmp_path: Path) -> Path:
    """Sample repo using the new default required fields plus nested/colliding modules."""
    repo = _make_sample_repo(tmp_path)
    cfg_path = repo / ".agents" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["required_header_fields"] = LEAN_FIELDS
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    shutil.copytree(REPO_ROOT / ".agents" / "templates", repo / ".agents" / "templates")

    (repo / "app" / "server.py").write_text(
        '"""\n'
        "Purpose: Expose request handlers built from closures.\n"
        "Role: service\n"
        "Patterns: none\n"
        "Tests: tests/test_math.py\n"
        "Invariants: none\n"
        '"""\n\n'
        "import os\n\n"
        "from app.math_util import add_numbers\n\n\n"
        "def handler(x: int) -> int:\n"
        "    def _exec(y: int) -> int:\n"
        "        return add_numbers(x, y)\n\n"
        "    os.getcwd()\n"
        "    os.getcwd()\n"
        "    return _exec(1)\n\n\n"
        'if __name__ == "__main__":\n\n'
        "    def cli() -> int:\n"
        "        return handler(2)\n\n"
        "    cli()\n",
        encoding="utf-8",
    )
    (repo / "app" / "other.py").write_text(
        '"""\n'
        "Purpose: Unrelated module that happens to define add_numbers too.\n"
        "Role: util\n"
        "Patterns: none\n"
        "Tests: none\n"
        "Invariants: none\n"
        '"""\n\n\n'
        "def add_numbers(a: str, b: str) -> str:\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "app" / "uses_other.py").write_text(
        '"""\n'
        "Purpose: Concatenate labels.\n"
        "Role: util\n"
        "Patterns: none\n"
        "Tests: none\n"
        "Invariants: none\n"
        '"""\n\n'
        "from app.other import add_numbers\n\n\n"
        "def label() -> str:\n"
        "    return add_numbers('a', 'b')\n",
        encoding="utf-8",
    )
    return repo


def _indexed(tmp_path: Path):
    repo = _lean_repo(tmp_path)
    cfg = load_config(repo)
    reindex_project(config=cfg, full=True, sync_remote=False, trigger="test")
    return repo, cfg, GraphStore(cfg.db_path)


# --- 1. Call graph accuracy -------------------------------------------------


def test_nested_functions_are_indexed_and_their_calls_attributed(tmp_path: Path) -> None:
    _, _, store = _indexed(tmp_path)
    parsed = parse_file(Path("app/server.py"), (tmp_path / "app/server.py").read_text())
    qnames = {s.qualified_name: s.kind for s in parsed.symbols}
    assert qnames["handler._exec"] == "function"
    assert "cli" in qnames  # defined inside `if __name__ == "__main__":`

    info = store.lookup_symbol("add_numbers", path="app/math_util.py")
    callers = {(c["source_file"], c["source_symbol"]) for c in info["callers"]}
    assert ("app/server.py", "handler._exec") in callers
    assert ("app/calc_service.py", "BillingService.total") in callers

    # A function's callees include calls made inside its closures
    handler = store.lookup_symbol("handler")
    assert any(c["target"] == "add_numbers" for c in handler["callees"])
    assert any(c["source_symbol"] == "cli" for c in handler["callers"])


def test_callers_are_resolved_through_imports_not_bare_names(tmp_path: Path) -> None:
    _, _, store = _indexed(tmp_path)

    math_only = store.lookup_symbol("add_numbers", path="app/math_util.py")
    assert [d["file_path"] for d in math_only["definitions"]] == ["app/math_util.py"]
    assert all(c["source_file"] != "app/uses_other.py" for c in math_only["callers"])

    both = store.lookup_symbol("add_numbers")
    assert len(both["definitions"]) == 2
    label_caller = next(c for c in both["callers"] if c["source_file"] == "app/uses_other.py")
    assert label_caller["resolves_to"] == "app/other.py"

    other_importers = store.lookup_symbol("add_numbers", path="app/other.py")["importers"]
    assert [i["source_file"] for i in other_importers] == ["app/uses_other.py"]


def test_callees_are_deduped_and_split_internal_external(tmp_path: Path) -> None:
    _, _, store = _indexed(tmp_path)
    info = store.lookup_symbol("handler")
    targets = [c["target"] for c in info["callees"]]
    assert len(targets) == len(set(targets))
    assert "os.getcwd" not in targets
    assert info["external_callees"] == ["os.getcwd"]
    internal = next(c for c in info["callees"] if c["target"] == "add_numbers")
    assert internal["defined_in"] == "app/math_util.py"


def test_lookup_symbol_respects_limit(tmp_path: Path) -> None:
    _, _, store = _indexed(tmp_path)
    info = store.lookup_symbol("add_numbers", limit=1)
    assert len(info["callers"]) == 1
    assert info["truncated"]["callers"] >= 1


# --- 2. Navigation tools ----------------------------------------------------


def test_outline_returns_signatures_without_bodies(tmp_path: Path) -> None:
    repo, cfg, _ = _indexed(tmp_path)
    out = outline(cfg, "app/calc_service.py")
    names = {s["name"]: s for s in out["symbols"]}
    assert names["BillingService.total"]["sig"].startswith("def total(self, x: int, y: int)")
    assert "-" in names["BillingService"]["lines"]
    assert out["role"] == "service"
    assert "return add_numbers" not in json.dumps(out)
    assert out["_baseline_bytes"] == (repo / "app/calc_service.py").stat().st_size


def test_get_source_is_live_and_handles_ambiguity(tmp_path: Path) -> None:
    repo, cfg, _ = _indexed(tmp_path)
    src = get_source(cfg, "BillingService.total")
    match = src["matches"][0]
    assert "return add_numbers(x, y)" in match["code"]
    assert match["path"] == "app/calc_service.py"

    # Edited on disk but not reindexed: get_source must reflect the live file
    p = repo / "app/calc_service.py"
    p.write_text(p.read_text().replace("return add_numbers(x, y)", "return add_numbers(y, x)"))
    assert "add_numbers(y, x)" in get_source(cfg, "BillingService.total")["matches"][0]["code"]

    amb = get_source(cfg, "add_numbers")
    assert {m["path"] for m in amb["matches"]} == {"app/math_util.py", "app/other.py"}
    scoped = get_source(cfg, "add_numbers", path="app/other.py")
    assert [m["path"] for m in scoped["matches"]] == ["app/other.py"]


def test_search_text_groups_by_file_and_truncates(tmp_path: Path) -> None:
    _, cfg, _ = _indexed(tmp_path)
    res = search_text(cfg, "Totals are non-negative")
    assert list(res["matches"]) == ["app/calc_service.py"]
    assert res["matches"]["app/calc_service.py"][0][0] == 8

    rx = search_text(cfg, r"def \w+_numbers", regex=True)
    assert set(rx["matches"]) == {"app/math_util.py", "app/other.py"}

    capped = search_text(cfg, "Purpose", limit=2)
    assert capped["total_matches"] == 2 and capped["truncated"] is True
    assert all(not p.startswith(".agents") for p in search_text(cfg, "service-layer")["matches"])


def test_affected_tests_follows_headers_and_reverse_imports(tmp_path: Path) -> None:
    _, cfg, _ = _indexed(tmp_path)
    res = affected_tests(cfg, paths=["app/math_util.py"])
    assert res["tests"] == ["tests/test_math.py"]
    assert res["reasons"]["tests/test_math.py"]

    assert affected_tests(cfg, paths=["tests/test_math.py"])["tests"] == ["tests/test_math.py"]
    assert affected_tests(cfg, paths=["app/widget.js"])["tests"] == []

    from src.config import is_test_path

    assert is_test_path("tests/test_math.py") and is_test_path("app/widget.test.js")
    assert not is_test_path("app/test_runner.py")

    ran = affected_tests(cfg, paths=["app/math_util.py"], run=True)
    assert ran["run"]["ok"] is True and ran["run"]["passed"] == 1


def test_scaffold_module_creates_valid_module_and_test(tmp_path: Path) -> None:
    repo, cfg, store = _indexed(tmp_path)
    res = scaffold_module(
        cfg,
        path="app/order_service.py",
        role="service",
        purpose="Manage customer orders.",
        patterns="service-layer, structured-errors",
    )
    assert res["created"] == ["app/order_service.py", "tests/test_order_service.py"]
    assert res["header_valid"] is True
    body = (repo / "app/order_service.py").read_text()
    assert "Role: service" in body and "Tests: tests/test_order_service.py" in body
    assert run_lint_and_headers(config=cfg)["ok"] is True
    assert any(m["path"] == "app/order_service.py" for m in store.repo_map()["modules"])
    assert run_compact_tests(scope="tests/test_order_service.py", config=cfg)["ok"] is True

    for kwargs, message in (
        ({"path": "app/order_service.py", "role": "service"}, "already exists"),
        ({"path": "app/z.py", "role": "wizard"}, "Unknown role"),
    ):
        try:
            scaffold_module(cfg, purpose="x", **kwargs)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"expected ValueError({message!r})")


# --- 3. Headers: derive + drift ----------------------------------------------


def test_header_derives_exposes_and_depends_on(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    cfg = load_config(repo)
    src = (repo / "app/server.py").read_text()
    rel = Path("app/server.py")
    h = parse_and_validate_header(rel, src, cfg, parsed=parse_file(rel, src))
    assert h.is_valid, h.errors
    assert h.exposes == ["handler"]
    assert "app.math_util" in h.depends_on and "os" in h.depends_on
    assert set(h.derived_fields) == {"Exposes", "Depends on"}


def test_header_drift_is_reported(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    cfg = load_config(repo)
    src = (
        '"""\nPurpose: Drifted.\nRole: util\nPatterns: none\n'
        "Exposes: real_fn, GhostClass\nTests: tests/test_missing.py\nInvariants: none\n"
        '"""\n\n\ndef real_fn() -> None:\n    pass\n'
    )
    rel = Path("app/drift.py")
    h = parse_and_validate_header(rel, src, cfg, parsed=parse_file(rel, src))
    joined = " ".join(h.errors)
    assert "GhostClass" in joined and "real_fn" not in joined
    assert "tests/test_missing.py" in joined


# --- 4. Local similarity fallback & freshness without remote --------------------


def test_find_similar_bm25_fallback_without_owg(tmp_path: Path) -> None:
    _, _, store = _indexed(tmp_path)
    res = search_similar(
        query="billing calculations",
        files=store.get_all_files(),
        vectors={},
        owg_client=None,
        symbols_by_file=store.get_all_symbols_by_file(),
    )
    assert res["method"] == "bm25"
    assert res["matches"][0]["path"] == "app/calc_service.py"


def test_index_status_fresh_when_remote_not_configured(tmp_path: Path, monkeypatch: Any) -> None:
    for var in ("OWG_HOST", "API_IP", "API_HOST", "OWG_API_KEY", "API_KEY"):
        monkeypatch.delenv(var, raising=False)
    _, cfg, _ = _indexed(tmp_path)
    status = get_index_status(config=cfg)
    assert status["fresh"] is True
    assert status["remote_enabled"] is False


# --- 5. Compact payloads ----------------------------------------------------------


def test_mcp_payloads_are_compact_and_bounded(tmp_path: Path, monkeypatch: Any) -> None:
    repo, _, _ = _indexed(tmp_path)
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    from src import mcp_server

    raw = mcp_server.repo_map()
    assert "\n" not in raw
    assert "_baseline_bytes" not in raw

    by_dir = json.loads(mcp_server.repo_map(depth=1))
    assert by_dir["directories"]["app"]["modules"] >= 5
    assert "modules" not in by_dir

    capped = json.loads(mcp_server.repo_map(limit=2))
    assert len(capped["modules"]) == 2 and capped["truncated"] > 0

    miss = mcp_server.get_conventions("no-such-topic")
    assert len(miss) < 1000
    miss_obj = json.loads(miss)
    assert miss_obj["matched"] is False and "service-layer" in miss_obj["available_topics"]

    hit = json.loads(mcp_server.get_conventions("service"))
    assert hit["matched"] is True
    assert hit["starter_template"].endswith("service.py.tmpl")

    log = [json.loads(ln) for ln in (repo / ".agents/state/tool_usage.jsonl").read_text().splitlines()]
    assert any(r.get("baseline_bytes") for r in log if r["tool"] == "repo_map")


# --- 6. Hook hardening -------------------------------------------------------------


def _bash(repo: Path, cmd: str, cwd: Path | None = None) -> dict[str, Any]:
    return handle_pre_tool(
        {
            "cwd": str(cwd or repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }
    )


def _denied(res: dict[str, Any]) -> bool:
    return res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_pre_tool_closes_bypasses_and_guards_shell_writes(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    toolkit = repo / ".agents/plugins/dev-tools"
    toolkit.mkdir(parents=True)

    assert _denied(_bash(repo, "pytest tests # .agents/plugins/dev-tools"))
    assert not _denied(_bash(repo, "cd .agents/plugins/dev-tools && pytest -q"))
    assert not _denied(_bash(repo, ".venv/bin/python -m pytest .agents/plugins/dev-tools/tests"))
    assert not _denied(_bash(repo, "pytest -q", cwd=toolkit))

    assert _denied(_bash(repo, "echo x > .agents/state/knowledge_graph.db"))
    assert _denied(_bash(repo, "rm -rf .agents/state"))
    assert _denied(_bash(repo, "sed -i 's/a/b/' .env"))
    assert not _denied(_bash(repo, "cat .agents/state/tool_usage.jsonl"))
    assert not _denied(_bash(repo, "grep foo .agents/state/tool_usage.jsonl > /tmp/out.txt"))


def test_post_tool_reindexes_after_shell_edits(tmp_path: Path) -> None:
    repo, cfg, store = _indexed(tmp_path)
    p = repo / "app/math_util.py"
    p.write_text(p.read_text() + "\n\ndef sub_numbers(a: int, b: int) -> int:\n    return a - b\n")
    handle_post_tool(
        {
            "cwd": str(repo),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat >> app/math_util.py <<'EOF'\n...\nEOF"},
        }
    )
    assert store.lookup_symbol("sub_numbers")["definitions"]


# --- 7. Remote circuit breaker ------------------------------------------------------


class _DownClient:
    """Stub whose every call fails like an unreachable OpenWeightGym host."""

    def __init__(self) -> None:
        self.calls = 0

    def _fail(self) -> None:
        import httpx  # local import keeps the module's import block layout-independent for Ruff

        self.calls += 1
        raise httpx.ConnectError("connection refused")

    def resolve_summary_model(self) -> str:
        self._fail()
        return ""

    def embed_texts(self, texts: list[str], trace: str = "") -> tuple[str, list[list[float]]]:
        self._fail()
        return "", []


def test_remote_sync_backs_off_after_transport_failure(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    cfg = load_config(repo)
    down = _DownClient()
    first = reindex_project(config=cfg, full=True, sync_remote=True, owg_client=down, respect_backoff=True)  # type: ignore[arg-type]
    assert first["remote_errors"]
    assert GraphStore(cfg.db_path).get_meta().get("remote_backoff_until")

    calls_before = down.calls
    second = reindex_project(config=cfg, sync_remote=True, owg_client=down, respect_backoff=True)  # type: ignore[arg-type]
    assert down.calls == calls_before
    assert "backoff" in second["remote_skipped"]


# --- 8. Metrics: estimated tokens saved ------------------------------------------------


def test_metrics_report_estimated_token_savings(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    cfg = load_config(repo)
    log_mcp_call(cfg.usage_log_path, "outline", {"path": "x"}, 400, 1.0, baseline_bytes=4000)
    log_mcp_call(cfg.usage_log_path, "run_tests", {}, 200, 1.0)
    m = summarize_metrics(cfg.usage_log_path)
    assert m["est_tokens_returned"] == 150
    assert m["est_tokens_saved"] == 900
    assert m["by_tool"]["outline"]["est_tokens_saved"] == 900


# --- 9. Test runner: multiple scopes + timeout -------------------------------------------


def test_run_tests_multiple_scopes_and_timeout(tmp_path: Path) -> None:
    repo = _lean_repo(tmp_path)
    (repo / "tests" / "test_two.py").write_text("def test_two():\n    assert True\n")
    cfg = load_config(repo)
    res = run_compact_tests(scope="tests/test_math.py tests/test_two.py", config=cfg)
    assert res["ok"] is True and res["passed"] == 2

    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(5)\n"
    )
    fast_cfg = dataclasses.replace(cfg, tests={"timeout_s": 1})
    slow = run_compact_tests(scope="tests/test_slow.py", config=fast_cfg)
    assert slow["ok"] is False and slow["status"] == "timeout"


# --- 10. Dogfooding: the toolkit follows its own rules -------------------------------------


def test_toolkit_source_passes_its_own_header_and_lint_rules(tmp_path: Path) -> None:
    template_cfg = json.loads((REPO_ROOT / ".agents/config.json").read_text())
    cfg = dataclasses.replace(
        load_config(tmp_path),
        project_root=TOOLKIT_DIR,
        ruff_config_path=REPO_ROOT / ".agents/ruff.toml",
        source_dirs=("src", "tests"),
        header_required_dirs=("src",),
        roles=tuple(template_cfg["roles"]),
        patterns=tuple(template_cfg["patterns"]),
        required_header_fields=tuple(template_cfg["required_header_fields"]),
    )
    res = run_lint_and_headers(config=cfg)
    assert res["header_issues"] == [], res["header_issues"]
    assert res["ruff_issues"] == [], res["ruff_issues"]
