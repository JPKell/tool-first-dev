#!/usr/bin/env python3
"""
Purpose: A/B benchmark the same coding task with and without the project-dev MCP tools and hooks.
Role: router
Patterns: none
Tests: none
Invariants: Each run uses a throwaway detached git worktree; the source repo's working tree is never modified.

Usage:
    python3 .agents/plugins/dev-tools/bench/compare.py --repo ~/repos/my-project \\
        --task "Add a discount field to BillingService.total and cover it with a test" \\
        --runs 3 --verify "python -m pytest -q"

Each run calls `claude -p <task> --output-format json` in a fresh worktree:
  * tools    — project settings (hooks) + the worktree's .mcp.json (project-dev server)
  * baseline — user settings only and no MCP servers (--strict-mcp-config with an empty config)
and reports turns, cost, tokens, duration, and (optionally) whether --verify passed afterwards.
Real runs spend API credits; use --dry-run to print the commands first.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TOOLKIT_REL = Path(".agents/plugins/dev-tools")
EMPTY_MCP = json.dumps({"mcpServers": {}})


def claude_command(variant: str, task: str, worktree: Path, args: argparse.Namespace) -> list[str]:
    """Build the headless claude invocation for one variant."""
    cmd = ["claude", "-p", task, "--output-format", "json", "--permission-mode", args.permission_mode]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_budget_usd:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]
    if variant == "tools":
        cmd += ["--strict-mcp-config", "--mcp-config", str(worktree / ".mcp.json")]
    else:
        cmd += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP, "--setting-sources", "user"]
    return cmd


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def run_once(variant: str, repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Run one variant in a throwaway worktree and return parsed metrics."""
    toolkit_python = repo / TOOLKIT_REL / ".venv" / "bin" / "python"
    with tempfile.TemporaryDirectory(prefix=f"tfd-bench-{variant}-") as tmp:
        worktree = Path(tmp) / "wt"
        cmd = claude_command(variant, args.task, worktree, args)
        if args.dry_run:
            return {"variant": variant, "command": cmd}
        _run(["git", "worktree", "add", "--detach", str(worktree), args.ref], cwd=repo).check_returncode()
        try:
            env = dict(os.environ)
            if variant == "tools":
                env["DEV_TOOLS_PYTHON"] = str(toolkit_python)  # worktrees lack the gitignored venv
                env["PROJECT_ROOT"] = str(worktree)
                _run(
                    [str(toolkit_python), "-m", "src.indexer", "--full", "--local-only"],
                    cwd=worktree / TOOLKIT_REL,
                    env={**env, "PYTHONPATH": str(worktree / TOOLKIT_REL)},
                )
            proc = _run(cmd, cwd=worktree, env=env)
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data = {"is_error": True, "result": (proc.stdout or proc.stderr)[-500:]}
            usage = data.get("usage") or {}
            out: dict[str, Any] = {
                "variant": variant,
                "ok": not data.get("is_error", False),
                "turns": data.get("num_turns"),
                "cost_usd": data.get("total_cost_usd"),
                "duration_s": round((data.get("duration_ms") or 0) / 1000, 1),
                "input_tokens": usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
            if args.verify:
                verify = _run(["sh", "-c", args.verify], cwd=worktree, env=env, timeout=900)
                out["verified"] = verify.returncode == 0
            return out
        finally:
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo)


def summarize(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Median of each numeric metric per variant, plus success/verification counts."""
    summary: dict[str, dict[str, Any]] = {}
    for variant in ("tools", "baseline"):
        rows = [r for r in results if r["variant"] == variant and "command" not in r]
        if not rows:
            continue
        agg: dict[str, Any] = {"runs": len(rows), "ok": sum(1 for r in rows if r.get("ok"))}
        if any("verified" in r for r in rows):
            agg["verified"] = sum(1 for r in rows if r.get("verified"))
        for key in ("turns", "cost_usd", "duration_s", "input_tokens", "output_tokens"):
            vals = [r[key] for r in rows if isinstance(r.get(key), int | float)]
            if vals:
                agg[f"median_{key}"] = statistics.median(vals)
        summary[variant] = agg
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True, help="Git repo with the tool-first template applied.")
    ap.add_argument("--task", required=True, help="Task prompt given identically to both variants.")
    ap.add_argument("--runs", type=int, default=1, help="Runs per variant (results are medians).")
    ap.add_argument("--ref", default="HEAD", help="Commit to start each worktree from.")
    ap.add_argument("--verify", help="Shell command run in the worktree afterwards (exit 0 = success).")
    ap.add_argument("--model", help="Model passed to claude --model.")
    ap.add_argument("--max-budget-usd", type=float, help="Per-run spend cap passed to claude.")
    ap.add_argument(
        "--permission-mode",
        default="acceptEdits",
        help="claude --permission-mode for both variants (runs execute in throwaway worktrees).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the commands without running them.")
    args = ap.parse_args()

    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        sys.exit(f"{repo} is not a git repository root.")
    if not args.dry_run and not (repo / TOOLKIT_REL / ".venv" / "bin" / "python").exists():
        sys.exit(f"Run {TOOLKIT_REL}/setup.sh in {repo} first (the tools variant needs the toolkit venv).")

    results: list[dict[str, Any]] = []
    for i in range(args.runs):
        for variant in ("tools", "baseline") if i % 2 == 0 else ("baseline", "tools"):
            res = run_once(variant, repo, args)
            results.append(res)
            print(json.dumps(res), file=sys.stderr)
    print(json.dumps({"task": args.task, "summary": summarize(results), "runs": results}, indent=2))


if __name__ == "__main__":
    main()
