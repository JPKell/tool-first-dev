"""
Purpose: Context-saving navigation (file outlines, exact symbol source, grouped text search, affected tests).
Role: service
Patterns: service-layer
Tests: tests/test_navigation.py
Invariants: Reads live files (never a stale index) for code; every result is size-bounded.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ToolkitConfig, is_test_path
from .graph_store import GraphStore
from .headers import parse_and_validate_header
from .parser import ExtractedSymbol, parse_file
from .test_runner import run_compact_tests

TEXT_EXTS = frozenset(
    {
        ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".cfg", ".ini", ".html", ".css",
        ".sql", ".sh", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".env.example",
    }
)
MAX_FILE_BYTES = 1_000_000
LINE_CHARS = 160
DOC_CHARS = 120
REVERSE_IMPORT_DEPTH = 3


def _rel(cfg: ToolkitConfig, path: str) -> Path:
    """Resolve a user-supplied path to a project-relative Path inside the project root."""
    p = Path(path.strip())
    full = (p if p.is_absolute() else cfg.project_root / p).resolve()
    try:
        return full.relative_to(cfg.project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path '{path}' is outside the project root.") from exc


def _read(cfg: ToolkitConfig, rel: Path) -> str:
    full = cfg.project_root / rel
    if not full.is_file():
        raise ValueError(f"File '{rel.as_posix()}' does not exist.")
    return full.read_text(encoding="utf-8", errors="ignore")


def _first_sentence(doc: str) -> str:
    sentence = re.split(r"(?<=[.!?])\s", doc.strip(), maxsplit=1)[0]
    return sentence[:DOC_CHARS]


def outline(cfg: ToolkitConfig, path: str) -> dict[str, Any]:
    """Return a file's header fields and symbol signatures with line ranges, without bodies.

    Args:
        cfg: Toolkit config.
        path: Project-relative (or absolute in-project) file path.
    Returns:
        {path, lines, role?, purpose?, symbols: [{name, kind, lines, sig, doc?}], imports, _baseline_bytes}.
    Side effects: Reads the file from disk (live, even if the index is stale).
    """
    rel = _rel(cfg, path)
    source = _read(cfg, rel)
    parsed = parse_file(rel, source)
    if parsed.language == "unknown":
        return {
            "path": rel.as_posix(),
            "lines": len(source.splitlines()),
            "note": "No parser for this file type; use search_text or a ranged Read.",
            "_baseline_bytes": len(source.encode("utf-8")),
        }
    header = parse_and_validate_header(rel, source, cfg, parsed=parsed)
    symbols = []
    for s in parsed.symbols:
        entry: dict[str, Any] = {
            "name": s.qualified_name,
            "kind": s.kind,
            "lines": f"{s.start_line}-{s.end_line}",
            "sig": s.signature,
        }
        if s.docstring:
            entry["doc"] = _first_sentence(s.docstring)
        symbols.append(entry)
    out: dict[str, Any] = {
        "path": rel.as_posix(),
        "lines": parsed.line_count,
        "role": header.role,
        "purpose": header.purpose,
        "symbols": symbols,
        "imports": parsed.import_modules,
        "_baseline_bytes": len(source.encode("utf-8")),
    }
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def _symbol_matches(sym: ExtractedSymbol, symbol: str) -> bool:
    return sym.qualified_name == symbol or sym.name == symbol or sym.qualified_name.endswith(f".{symbol}")


def get_source(
    cfg: ToolkitConfig,
    symbol: str,
    path: str | None = None,
    context: int = 0,
    max_lines: int = 200,
    max_matches: int = 3,
) -> dict[str, Any]:
    """Return the exact current source of a function/class/method (not the whole file).

    Args:
        symbol: Bare or qualified name (`total`, `BillingService.total`).
        path: Optional file to search (otherwise files known to the index, then all source files).
        context: Extra lines before/after the definition.
        max_lines: Truncate each body after this many lines.
        max_matches: Return code for at most this many definitions; the rest are listed.
    Returns:
        {symbol, matches: [{path, name, kind, start_line, end_line, code, truncated?}], more?}.
    """
    if path:
        candidate_files = [_rel(cfg, path).as_posix()]
    else:
        defs = GraphStore(cfg.db_path).lookup_symbol(symbol, include_impact=False)["definitions"]
        candidate_files = sorted({d["file_path"] for d in defs})
        if not candidate_files:
            candidate_files = [p.as_posix() for p in cfg.discover_source_files()]

    matches: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    baseline = 0
    for rel_str in candidate_files:
        rel = Path(rel_str)
        try:
            source = _read(cfg, rel)
        except ValueError:
            continue
        lines = source.splitlines()
        for sym in parse_file(rel, source).symbols:
            if not _symbol_matches(sym, symbol):
                continue
            if "." in symbol and not sym.qualified_name.endswith(symbol):
                continue
            if len(matches) >= max_matches:
                others.append({"path": rel_str, "name": sym.qualified_name, "lines": f"{sym.start_line}-{sym.end_line}"})
                continue
            start = max(1, sym.start_line - context)
            end = min(len(lines), sym.end_line + context)
            body = lines[start - 1 : end]
            entry: dict[str, Any] = {
                "path": rel_str,
                "name": sym.qualified_name,
                "kind": sym.kind,
                "start_line": start,
                "end_line": end,
                "code": "\n".join(body[:max_lines]),
            }
            if len(body) > max_lines:
                entry["truncated"] = f"{len(body) - max_lines} more lines; use a ranged Read"
            matches.append(entry)
            baseline += len(source.encode("utf-8"))
    out: dict[str, Any] = {"symbol": symbol, "matches": matches, "_baseline_bytes": baseline}
    if others:
        out["more"] = others
    if not matches:
        out["note"] = "No definition found; try lookup_symbol or search_text."
    return out


def _iter_search_files(cfg: ToolkitConfig, scope: str | None, file_glob: str | None) -> list[Path]:
    root = cfg.project_root / _rel(cfg, scope) if scope else cfg.project_root
    if root.is_file():
        return [root.relative_to(cfg.project_root)]
    found: list[Path] = []
    allowed = set(cfg.extensions) | TEXT_EXTS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in cfg.ignore_dirs and not d.startswith(".venv")
        )
        for fname in sorted(filenames):
            full = Path(dirpath) / fname
            if file_glob:
                if not fnmatch.fnmatch(fname, file_glob):
                    continue
            elif full.suffix not in allowed and fname not in TEXT_EXTS:
                continue
            found.append(full.relative_to(cfg.project_root))
    return found


def search_text(
    cfg: ToolkitConfig,
    pattern: str,
    scope: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    file_glob: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Grep replacement: find literal/regex text and return matches grouped by file.

    Finds what lookup_symbol cannot: string literals, config keys, env var names, routes,
    error messages. Skips ignore_dirs (including .agents), binaries, and files > 1 MB.
    Returns {pattern, matches: {path: [[line, text], ...]}, total_matches, truncated, files_searched}.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    matches: dict[str, list[list[Any]]] = {}
    total = 0
    truncated = False
    searched = 0
    baseline = 0
    for rel in _iter_search_files(cfg, scope, file_glob):
        full = cfg.project_root / rel
        try:
            if full.stat().st_size > MAX_FILE_BYTES:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        searched += 1
        hit_in_file = False
        for i, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), start=1):
            m = rx.search(line)
            if not m:
                continue
            if total >= limit:
                truncated = True
                break
            text = line.strip()
            if len(text) > LINE_CHARS:
                lo = max(0, m.start() - LINE_CHARS // 3)
                text = ("…" if lo else "") + line[lo : lo + LINE_CHARS].strip() + "…"
            matches.setdefault(rel.as_posix(), []).append([i, text])
            total += 1
            hit_in_file = True
        if hit_in_file:
            baseline += len(data)
        if truncated:
            break
    return {
        "pattern": pattern,
        "matches": matches,
        "total_matches": total,
        "truncated": truncated,
        "files_searched": searched,
        "_baseline_bytes": baseline,
    }


def _git_changed_paths(cfg: ToolkitConfig) -> list[str]:
    """Paths changed vs HEAD plus untracked files (empty if not a git repo)."""
    paths: set[str] = set()
    for cmd in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(cmd, cwd=cfg.project_root, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        paths.update(ln.strip() for ln in proc.stdout.splitlines() if ln.strip())
    return sorted(paths)


def affected_tests(
    cfg: ToolkitConfig,
    paths: list[str] | None = None,
    run: bool = False,
) -> dict[str, Any]:
    """Find the tests covering changed files (and optionally run only those).

    Coverage = changed test files + header `Tests:` links + tests importing the changed module,
    followed through up to REVERSE_IMPORT_DEPTH levels of reverse imports.
    Args:
        paths: Project-relative changed files; defaults to `git diff HEAD` + untracked files.
        run: When True, run the selected tests via run_tests and include the compact result.
    Returns:
        {changed, tests, reasons: {test: [why]}, run?}.
    """
    store = GraphStore(cfg.db_path)
    changed = [p.strip().lstrip("./") for p in (paths if paths is not None else _git_changed_paths(cfg))]
    changed = [p for p in changed if p]
    reasons: dict[str, list[str]] = {}

    def _add(test: str, why: str) -> None:
        test_file = test.split("::", 1)[0]
        if not (cfg.project_root / test_file).exists():
            return
        bucket = reasons.setdefault(test_file, [])
        if why not in bucket and len(bucket) < 5:
            bucket.append(why)

    for path in changed:
        if is_test_path(path):
            _add(path, "changed")
            continue
        frontier = [path]
        seen = {path}
        for depth in range(REVERSE_IMPORT_DEPTH + 1):
            nxt: list[str] = []
            for mod in frontier:
                via = "" if mod == path else f" via {mod}"
                for t in store.tested_by(mod):
                    _add(t, f"Tests: header of {mod}" + ("" if mod == path else f" (imports {path})"))
                if depth == REVERSE_IMPORT_DEPTH:
                    continue
                for importer in store.files_importing(mod):
                    if is_test_path(importer):
                        _add(importer, f"imports {path}{via}")
                    elif importer not in seen:
                        seen.add(importer)
                        nxt.append(importer)
            frontier = nxt

    tests = sorted(reasons)
    out: dict[str, Any] = {"changed": changed, "tests": tests, "reasons": reasons}
    if run:
        out["run"] = (
            run_compact_tests(scope=" ".join(tests), config=cfg)
            if tests
            else {"ok": True, "status": "no_affected_tests"}
        )
    return out
