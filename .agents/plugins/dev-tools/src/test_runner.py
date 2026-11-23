"""Compact failure-only test runner and header/ruff lint runner."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import ToolkitConfig, load_config
from .headers import parse_and_validate_header

SUMMARY_RE = re.compile(
    r"(?:(?P<failed>\d+)\s+failed)?"
    r"(?:[,\s]*(?P<passed>\d+)\s+passed)?"
    r"(?:[,\s]*(?P<skipped>\d+)\s+skipped)?"
    r"(?:[,\s]*(?P<errors>\d+)\s+errors?)?"
)
FAIL_HEADER_RE = re.compile(r"^_{3,}\s+(?P<test>.+?)\s+_{3,}$")
LOC_RE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):\s*(?P<msg>.*)$")


def _select_python(config: ToolkitConfig) -> str:
    """Prefer container APP_VENV or project .venv python when present; otherwise toolkit python."""
    import os

    app_venv = os.environ.get("APP_VENV")
    if app_venv:
        app_py = Path(app_venv) / "bin" / "python"
        if app_py.exists():
            # Only use APP_VENV if pytest is installed there; otherwise fall back to toolkit python
            probe = subprocess.run(
                [str(app_py), "-c", "import pytest"],
                capture_output=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return str(app_py)
    project_py = config.project_root / ".venv" / "bin" / "python"
    if project_py.exists():
        return str(project_py)
    return sys.executable


def _select_ruff(config: ToolkitConfig) -> str:
    toolkit_ruff = Path(sys.executable).parent / "ruff"
    if toolkit_ruff.exists():
        return str(toolkit_ruff)
    project_ruff = config.project_root / ".venv" / "bin" / "ruff"
    if project_ruff.exists():
        return str(project_ruff)
    return "ruff"


def _parse_pytest_failures(output: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        m = FAIL_HEADER_RE.match(lines[i].strip())
        if m:
            test_name = m.group("test").strip()
            block: list[str] = []
            i += 1
            while i < len(lines) and not FAIL_HEADER_RE.match(lines[i].strip()) and not lines[i].startswith("===="):
                block.append(lines[i])
                i += 1
            file_loc = ""
            line_no: int | None = None
            err_lines: list[str] = []
            for bl in block:
                lm = LOC_RE.match(bl.strip())
                if lm:
                    file_loc = lm.group("file")
                    line_no = int(lm.group("line"))
                if bl.startswith("E   "):
                    err_lines.append(bl[4:].strip())
            if not err_lines and block:
                err_lines = [b.strip() for b in block[-3:] if b.strip()]
            failures.append(
                {
                    "test": test_name,
                    "file": file_loc,
                    "line": line_no,
                    "error": " | ".join(err_lines)[:400],
                }
            )
            continue
        i += 1
    return failures


def run_compact_tests(
    scope: str | None = None,
    config: ToolkitConfig | None = None,
) -> dict[str, Any]:
    """Run pytest and return compact machine-readable results (failures only)."""
    cfg = config or load_config()
    target = scope.strip() if scope else "tests"
    target_path = cfg.project_root / target.split("::")[0]
    if not target_path.exists():
        return {
            "ok": True,
            "status": "no_tests_yet",
            "scope": target,
            "python": _select_python(cfg),
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "duration_s": 0.0,
            "failures": [],
            "note": f"Test path '{target}' does not exist yet.",
        }

    py = _select_python(cfg)
    cmd = [py, "-m", "pytest", target, "-q", "--tb=short", "--no-header"]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=cfg.project_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    duration = round(time.monotonic() - t0, 3)
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

    passed = failed = skipped = errors = 0
    for line in reversed(combined.splitlines()):
        if any(k in line for k in ("passed", "failed", "error", "skipped", "no tests ran")):
            for num, label in re.findall(r"(\d+)\s+(passed|failed|skipped|errors?)", line):
                n = int(num)
                if label == "passed":
                    passed = n
                elif label == "failed":
                    failed = n
                elif label == "skipped":
                    skipped = n
                elif label.startswith("error"):
                    errors = n
            break

    failures = _parse_pytest_failures(combined) if proc.returncode not in (0, 5) else []
    if proc.returncode not in (0, 5) and not failures:
        tail = [ln.strip() for ln in combined.splitlines() if ln.strip()][-6:]
        failures.append({"test": target, "file": "", "line": None, "error": " | ".join(tail)[:400]})

    return {
        "ok": proc.returncode in (0, 5),
        "exit_code": proc.returncode,
        "scope": target,
        "python": py,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "duration_s": duration,
        "failures": failures,
    }


def run_lint_and_headers(
    scope: str | None = None,
    config: ToolkitConfig | None = None,
) -> dict[str, Any]:
    """Validate module headers against RULES.md/config.json and run Ruff lint checks."""
    cfg = config or load_config()
    files = cfg.discover_source_files(scope=scope)

    header_issues: list[dict[str, Any]] = []
    py_files: list[str] = []
    for rel in files:
        full = cfg.project_root / rel
        source = full.read_text(encoding="utf-8", errors="ignore")
        header = parse_and_validate_header(rel, source, cfg)
        if not header.is_valid:
            header_issues.append({"file": rel.as_posix(), "errors": header.errors})
        if rel.suffix == ".py":
            py_files.append(rel.as_posix())

    ruff_issues: list[dict[str, Any]] = []
    if py_files:
        ruff_bin = _select_ruff(cfg)
        cmd = [ruff_bin, "check", "--output-format", "json"]
        if cfg.ruff_config_path.exists():
            cmd.extend(["--config", str(cfg.ruff_config_path)])
        cmd.extend(py_files)
        proc = subprocess.run(
            cmd,
            cwd=cfg.project_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.stdout.strip():
            try:
                raw_items = json.loads(proc.stdout)
                for item in raw_items:
                    fname = item.get("filename", "")
                    with contextlib.suppress(ValueError):
                        fname = Path(fname).resolve().relative_to(cfg.project_root).as_posix()
                    loc = item.get("location") or {}
                    ruff_issues.append(
                        {
                            "file": fname,
                            "line": loc.get("row"),
                            "code": item.get("code"),
                            "message": item.get("message"),
                        }
                    )
            except json.JSONDecodeError:
                ruff_issues.append({"file": "", "line": None, "code": "RUFF", "message": proc.stdout[:300]})

    return {
        "ok": not header_issues and not ruff_issues,
        "checked_files": len(files),
        "header_issues": header_issues,
        "ruff_issues": ruff_issues,
    }
