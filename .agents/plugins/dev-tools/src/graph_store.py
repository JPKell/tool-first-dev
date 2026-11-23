"""
Purpose: SQLite-backed knowledge graph of files, symbols, call/import edges, summaries, and vectors.
Role: repository
Patterns: repository-pattern
Tests: tests/test_toolkit.py, tests/test_navigation.py
Invariants: Parameterized SQL only; callers/importers are resolved through imports, not bare names.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import is_test_path
from .headers import ModuleHeader
from .parser import ParsedFile

JS_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


def normalize_module(spec: str) -> str:
    """Normalize a Python dotted / relative or JS path import spec to a dotted module path."""
    s = spec.strip().strip("'\"")
    while s.startswith(("./", "../")):
        s = s[2:] if s.startswith("./") else s[3:]
    for ext in JS_EXTS + (".py",):
        if s.endswith(ext):
            s = s[: -len(ext)]
            break
    s = s.lstrip(".").replace("/", ".")
    if s.endswith(".index") or s.endswith(".__init__"):
        s = s.rsplit(".", 1)[0]
    return s


def file_module(rel_path: str) -> str:
    """Dotted module path for a project-relative file (app/math_util.py -> app.math_util)."""
    return normalize_module(rel_path)


def module_matches(import_spec: str, rel_path: str) -> bool:
    """True if an import spec plausibly refers to the module stored at rel_path."""
    imp = normalize_module(import_spec)
    if not imp:
        return False
    mod = file_module(rel_path)
    return mod == imp or mod.endswith(f".{imp}")


def _compact(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so payloads only carry information."""
    return {k: v for k, v in entry.items() if v not in ("", [], {}, None)}

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    role TEXT NOT NULL,
    purpose TEXT NOT NULL,
    patterns_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    exposes_json TEXT NOT NULL,
    tests_json TEXT NOT NULL,
    invariants TEXT NOT NULL,
    header_present INTEGER NOT NULL,
    header_valid INTEGER NOT NULL,
    header_errors_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    summary_provider TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL,
    docstring TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    source_symbol TEXT NOT NULL,
    target TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    line_number INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_file);

CREATE TABLE IF NOT EXISTS vectors (
    file_path TEXT PRIMARY KEY REFERENCES files(path) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class GraphStore:
    """Manages reads and writes to the project SQLite knowledge graph."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Lightweight migration for older vectors table lacking model/content_hash
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(vectors)").fetchall()
            }
            if "model" not in cols:
                conn.execute("ALTER TABLE vectors ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            if "content_hash" not in cols:
                conn.execute("ALTER TABLE vectors ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def get_indexed_hashes(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, content_hash FROM files").fetchall()
            return {r["path"]: r["content_hash"] for r in rows}

    def get_file(self, rel_path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE path = ?", (rel_path,)).fetchone()
            return self._decode_file_row(row) if row else None

    @staticmethod
    def _decode_file_row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "path": r["path"],
            "content_hash": r["content_hash"],
            "language": r["language"],
            "role": r["role"],
            "purpose": r["purpose"],
            "patterns": json.loads(r["patterns_json"]),
            "depends_on": json.loads(r["depends_on_json"]),
            "exposes": json.loads(r["exposes_json"]),
            "tests": json.loads(r["tests_json"]),
            "invariants": r["invariants"],
            "header_present": bool(r["header_present"]),
            "header_valid": bool(r["header_valid"]),
            "header_errors": json.loads(r["header_errors_json"]),
            "summary": r["summary"],
            "summary_hash": r["summary_hash"],
            "summary_provider": r["summary_provider"],
            "line_count": r["line_count"],
            "updated_at": r["updated_at"],
        }

    def upsert_file(
        self,
        rel_path: str,
        content_hash: str,
        parsed: ParsedFile,
        header: ModuleHeader,
        summary: str = "",
        summary_hash: str = "",
        summary_provider: str = "",
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
            conn.execute("DELETE FROM edges WHERE source_file = ?", (rel_path,))

            conn.execute(
                """
                INSERT INTO files (
                    path, content_hash, language, role, purpose,
                    patterns_json, depends_on_json, exposes_json, tests_json,
                    invariants, header_present, header_valid, header_errors_json,
                    summary, summary_hash, summary_provider, line_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    language=excluded.language,
                    role=excluded.role,
                    purpose=excluded.purpose,
                    patterns_json=excluded.patterns_json,
                    depends_on_json=excluded.depends_on_json,
                    exposes_json=excluded.exposes_json,
                    tests_json=excluded.tests_json,
                    invariants=excluded.invariants,
                    header_present=excluded.header_present,
                    header_valid=excluded.header_valid,
                    header_errors_json=excluded.header_errors_json,
                    summary=excluded.summary,
                    summary_hash=excluded.summary_hash,
                    summary_provider=excluded.summary_provider,
                    line_count=excluded.line_count,
                    updated_at=excluded.updated_at
                """,
                (
                    rel_path,
                    content_hash,
                    parsed.language,
                    header.role,
                    header.purpose,
                    json.dumps(header.patterns),
                    json.dumps(header.depends_on),
                    json.dumps(header.exposes),
                    json.dumps(header.tests),
                    header.invariants,
                    int(header.present),
                    int(header.is_valid),
                    json.dumps(header.errors),
                    summary,
                    summary_hash,
                    summary_provider,
                    parsed.line_count,
                    now,
                ),
            )

            conn.executemany(
                """
                INSERT INTO symbols (
                    file_path, name, qualified_name, kind,
                    start_line, end_line, signature, docstring
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        rel_path,
                        s.name,
                        s.qualified_name,
                        s.kind,
                        s.start_line,
                        s.end_line,
                        s.signature,
                        s.docstring,
                    )
                    for s in parsed.symbols
                ],
            )

            edge_rows: list[tuple[str, str, str, str, int]] = [
                (rel_path, e.source_symbol, e.target, e.edge_type, e.line_number)
                for e in parsed.edges
            ]
            for test_path in header.tests:
                edge_rows.append((rel_path, "<module>", test_path, "tested_by", 1))
            for pattern in header.patterns:
                edge_rows.append((rel_path, "<module>", pattern, "follows_pattern", 1))
            if header.role:
                edge_rows.append((rel_path, "<module>", header.role, "has_role", 1))

            conn.executemany(
                """
                INSERT INTO edges (source_file, source_symbol, target, edge_type, line_number)
                VALUES (?, ?, ?, ?, ?)
                """,
                edge_rows,
            )

    def update_summary(
        self,
        rel_path: str,
        summary: str,
        summary_hash: str,
        summary_provider: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE files
                SET summary = ?, summary_hash = ?, summary_provider = ?, updated_at = ?
                WHERE path = ?
                """,
                (summary, summary_hash, summary_provider, _now_iso(), rel_path),
            )

    def upsert_vector(
        self,
        rel_path: str,
        provider: str,
        model: str,
        content_hash: str,
        vector: list[float],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vectors(file_path, provider, model, content_hash, vector_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    provider=excluded.provider,
                    model=excluded.model,
                    content_hash=excluded.content_hash,
                    vector_json=excluded.vector_json,
                    updated_at=excluded.updated_at
                """,
                (rel_path, provider, model, content_hash, json.dumps(vector), _now_iso()),
            )

    def delete_files(self, paths: list[str]) -> int:
        if not paths:
            return 0
        with self._connect() as conn:
            cur = conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])
            return cur.rowcount

    def get_all_files(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
            return [self._decode_file_row(r) for r in rows]

    def get_all_symbols_by_file(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, name, qualified_name, kind, start_line, end_line, signature, docstring "
                "FROM symbols ORDER BY file_path, start_line"
            ).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                grouped.setdefault(r["file_path"], []).append(dict(r))
            return grouped

    def get_all_vectors(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, provider, model, content_hash, vector_json FROM vectors"
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for r in rows:
                parsed = json.loads(r["vector_json"])
                if isinstance(parsed, list):
                    out[r["file_path"]] = {
                        "provider": r["provider"],
                        "model": r["model"],
                        "content_hash": r["content_hash"],
                        "vector": parsed,
                    }
            return out

    # ------------------------------------------------------------------ resolution

    def _imports_by_file(self, conn: sqlite3.Connection) -> dict[str, list[str]]:
        rows = conn.execute(
            "SELECT source_file, target FROM edges WHERE edge_type = 'imports'"
        ).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["source_file"], []).append(r["target"])
        return out

    @staticmethod
    def _imports_name_from(imports: list[str], name: str, def_file: str) -> bool:
        """True if an import list brings `name` in from the module defined by def_file."""
        suffix = f".{name}"
        return any(
            imp.endswith(suffix) and module_matches(imp[: -len(suffix)], def_file) for imp in imports
        )

    @staticmethod
    def _imported_module_aliases(imports: list[str], def_file: str) -> set[str]:
        """Names under which def_file's module is reachable in a file (e.g. `math_util`)."""
        aliases: set[str] = set()
        for imp in imports:
            if module_matches(imp, def_file):
                aliases.add(imp)
                aliases.add(normalize_module(imp).rsplit(".", 1)[-1])
        return aliases

    def _caller_match(
        self,
        target: str,
        caller_file: str,
        definition: dict[str, Any],
        imports: list[str],
    ) -> str | None:
        """Classify whether a call edge refers to this definition: same_file, import, or None."""
        name = definition["name"]
        def_file = definition["file_path"]
        qname = definition["qualified_name"]
        owner = qname.rsplit(".", 1)[0] if "." in qname else ""
        prefix = target[: -len(name) - 1] if target.endswith(f".{name}") else None
        if target != name and prefix is None:
            return None

        if caller_file == def_file:
            if target in {name, qname} or prefix in {"self", "cls", owner}:
                return "same_file"
            return None

        if target == name:
            return "import" if self._imports_name_from(imports, name, def_file) else None
        assert prefix is not None
        if prefix in self._imported_module_aliases(imports, def_file):
            return "import"
        if definition["kind"] == "method" and owner:
            top_owner = owner.split(".", 1)[0]
            if prefix == owner or self._imports_name_from(imports, top_owner, def_file):
                # obj.method() where the caller imports the owning class (receiver type unchecked)
                return "import"
        return None

    def _resolve_callee(
        self,
        target: str,
        source_file: str,
        imports: list[str],
        by_name: dict[str, list[dict[str, Any]]],
    ) -> str | None:
        """Return the project file defining a call target, or None for external calls."""
        leaf = target.rsplit(".", 1)[-1]
        candidates = by_name.get(leaf, [])
        if not candidates:
            return None
        files = sorted({c["file_path"] for c in candidates})
        if source_file in files and (target == leaf or target.split(".", 1)[0] in {"self", "cls"}):
            return source_file
        for f in files:
            if self._imports_name_from(imports, leaf, f) or (
                "." in target and target.rsplit(".", 1)[0] in self._imported_module_aliases(imports, f)
            ):
                return f
        if target.split(".", 1)[0] in {"self", "cls"} and source_file in files:
            return source_file
        if len(files) == 1 and target == leaf:
            return files[0]
        return None

    # ------------------------------------------------------------------ queries

    def lookup_symbol(
        self,
        symbol: str,
        include_impact: bool = True,
        path: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Find definitions plus import-resolved callers, callees, importers, and covering tests.

        Args:
            symbol: Bare name (`add_numbers`) or qualified name (`BillingService.total`).
            include_impact: When False, only definitions are returned.
            path: Optional project-relative file to disambiguate same-named symbols.
            limit: Max entries per list; overflow counts are reported under `truncated`.
        """
        short_name = symbol.split(".")[-1]
        with self._connect() as conn:
            defs = conn.execute(
                """
                SELECT file_path, name, qualified_name, kind, start_line, end_line, signature, docstring
                FROM symbols
                WHERE name = ? OR qualified_name = ? OR qualified_name LIKE ?
                ORDER BY file_path, start_line
                """,
                (symbol, symbol, f"%.{symbol}"),
            ).fetchall()
            definitions = [dict(r) for r in defs]
            if "." in symbol:
                exact = [d for d in definitions if d["qualified_name"].endswith(symbol)]
                definitions = exact or definitions
            if path:
                norm = path.strip().lstrip("./")
                definitions = [d for d in definitions if d["file_path"] == norm]

            result: dict[str, Any] = {"symbol": symbol, "definitions": definitions}
            if not include_impact or not definitions:
                return result

            imports_by_file = self._imports_by_file(conn)
            multi = len({d["file_path"] for d in definitions}) > 1

            # Callers: name-filtered in SQL, then resolved through the caller's imports
            call_rows = conn.execute(
                """
                SELECT DISTINCT source_file, source_symbol, target, line_number
                FROM edges
                WHERE edge_type = 'calls' AND (target = ? OR target LIKE ?)
                ORDER BY source_file, line_number
                """,
                (short_name, f"%.{short_name}"),
            ).fetchall()
            callers: list[dict[str, Any]] = []
            unresolved = 0
            for r in call_rows:
                imports = imports_by_file.get(r["source_file"], [])
                hit = None
                for d in definitions:
                    match = self._caller_match(r["target"], r["source_file"], d, imports)
                    if match:
                        hit = (d, match)
                        break
                if hit is None:
                    unresolved += 1
                    continue
                entry = dict(r)
                if multi:
                    entry["resolves_to"] = hit[0]["file_path"]
                callers.append(entry)

            # Callees: calls made inside each definition (and its methods/closures), deduped
            clauses: list[str] = []
            params: list[str] = []
            for d in definitions:
                clauses.append("(source_file = ? AND (source_symbol = ? OR source_symbol LIKE ?))")
                params.extend([d["file_path"], d["qualified_name"], f"{d['qualified_name']}.%"])
            callee_rows = conn.execute(
                f"""
                SELECT source_file, source_symbol, target, MIN(line_number) AS line_number
                FROM edges
                WHERE edge_type = 'calls' AND ({" OR ".join(clauses)})
                GROUP BY source_file, target
                ORDER BY line_number
                """,
                tuple(params),
            ).fetchall()
            by_name: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute("SELECT name, file_path FROM symbols").fetchall():
                by_name.setdefault(r["name"], []).append(dict(r))
            callees: list[dict[str, Any]] = []
            external: list[str] = []
            for r in callee_rows:
                defined_in = self._resolve_callee(
                    r["target"], r["source_file"], imports_by_file.get(r["source_file"], []), by_name
                )
                if defined_in:
                    callees.append(
                        {
                            "target": r["target"],
                            "source_symbol": r["source_symbol"],
                            "line_number": r["line_number"],
                            "defined_in": defined_in,
                        }
                    )
                elif r["target"] not in external:
                    external.append(r["target"])

            # Importers: files whose imports resolve to a definition's module
            importers: list[dict[str, Any]] = []
            for d in definitions:
                imp_rows = conn.execute(
                    """
                    SELECT DISTINCT source_file, target, line_number FROM edges
                    WHERE edge_type = 'imports' AND source_file != ?
                    ORDER BY source_file, line_number
                    """,
                    (d["file_path"],),
                ).fetchall()
                for r in imp_rows:
                    t = r["target"]
                    if (t.endswith(f".{d['name']}") and module_matches(t[: -len(d["name"]) - 1], d["file_path"])):
                        importers.append(dict(r))

            covering: set[str] = set()
            for d in definitions:
                for r in conn.execute(
                    "SELECT target FROM edges WHERE source_file = ? AND edge_type = 'tested_by'",
                    (d["file_path"],),
                ).fetchall():
                    covering.add(r["target"])
            for c in callers:
                if is_test_path(c["source_file"]):
                    covering.add(f"{c['source_file']}::{c['source_symbol']}")
            for i in importers:
                if is_test_path(i["source_file"]):
                    covering.add(i["source_file"])

        truncated: dict[str, int] = {}

        def _cap(name: str, items: list[Any]) -> list[Any]:
            if len(items) > limit:
                truncated[name] = len(items) - limit
                return items[:limit]
            return items

        result["callers"] = _cap("callers", callers)
        result["callees"] = _cap("callees", callees)
        result["external_callees"] = _cap("external_callees", external)
        result["importers"] = _cap("importers", importers)
        result["covering_tests"] = _cap("covering_tests", sorted(covering))
        if unresolved:
            result["unresolved_name_matches"] = unresolved
        if truncated:
            result["truncated"] = truncated
        return result

    def find_examples(self, topic: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find indexed modules matching a role, pattern name, or path fragment."""
        needle = topic.strip().lower()
        matches: list[dict[str, Any]] = []
        for f in self.get_all_files():
            if (
                f["role"].lower() == needle
                or needle in [p.lower() for p in f["patterns"]]
                or needle in f["path"].lower()
            ):
                matches.append(
                    _compact(
                        {
                            "path": f["path"],
                            "role": f["role"],
                            "patterns": f["patterns"],
                            "purpose": f["purpose"],
                            "exposes": f["exposes"],
                            "tests": f["tests"],
                        }
                    )
                )
        return matches[:limit]

    def files_importing(self, rel_path: str) -> list[str]:
        """Files with an import edge that resolves to rel_path's module (or a symbol in it)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_file, target FROM edges WHERE edge_type = 'imports' AND source_file != ?",
                (rel_path,),
            ).fetchall()
        hits: set[str] = set()
        for r in rows:
            t = r["target"]
            if module_matches(t, rel_path) or (
                "." in t.lstrip(".") and module_matches(t.rsplit(".", 1)[0], rel_path)
            ):
                hits.add(r["source_file"])
        return sorted(hits)

    def tested_by(self, rel_path: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT target FROM edges WHERE source_file = ? AND edge_type = 'tested_by'",
                (rel_path,),
            ).fetchall()
        return [r["target"] for r in rows]

    def repo_map(
        self,
        role: str | None = None,
        scope: str | None = None,
        include_summaries: bool = False,
        depth: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return a fast, offline architectural map of indexed modules from SQLite.

        Args:
            role / scope: Optional filters (role tag, path prefix).
            include_summaries: Include stored module summaries.
            depth: When set, aggregate per directory (first `depth` path segments) instead of
                listing modules — the cheap first call on a large repo.
            limit: Max modules listed; overflow is reported as `truncated`.
        """
        role_needle = role.strip().lower() if role else None
        scope_needle = scope.strip().lstrip("./").rstrip("/") if scope else None

        counts_by_role: dict[str, int] = {}
        modules: list[dict[str, Any]] = []
        for f in self.get_all_files():
            path = f["path"]
            if scope_needle and not (path == scope_needle or path.startswith(f"{scope_needle}/")):
                continue
            f_role = f["role"] or "unspecified"
            if role_needle and f_role.lower() != role_needle:
                continue
            counts_by_role[f_role] = counts_by_role.get(f_role, 0) + 1
            entry: dict[str, Any] = {
                "path": path,
                "role": f_role,
                "patterns": f["patterns"],
                "purpose": f["purpose"],
                "exposes": f["exposes"],
                "tests": f["tests"],
                "line_count": f["line_count"],
            }
            if include_summaries and f["summary"]:
                entry["summary"] = f["summary"]
            modules.append(_compact(entry))

        out: dict[str, Any] = {
            "total_modules": len(modules),
            "role_filter": role_needle,
            "scope_filter": scope_needle,
            "counts_by_role": dict(sorted(counts_by_role.items())),
        }
        if depth is not None and depth >= 1:
            dirs: dict[str, dict[str, Any]] = {}
            for m in modules:
                parts = m["path"].split("/")[:-1]
                key = "/".join(parts[:depth]) or "."
                bucket = dirs.setdefault(key, {"modules": 0, "lines": 0, "roles": {}})
                bucket["modules"] += 1
                bucket["lines"] += m.get("line_count", 0)
                bucket["roles"][m["role"]] = bucket["roles"].get(m["role"], 0) + 1
            out["directories"] = dict(sorted(dirs.items()))
            return out
        out["modules"] = modules[:limit]
        if len(modules) > limit:
            out["truncated"] = len(modules) - limit
            out["hint"] = "Narrow with scope=/role=, or call repo_map(depth=1) for a directory overview."
        return out

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            files_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            symbols_n = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            edges_n = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            vectors_n = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            return {
                "files": files_n,
                "symbols": symbols_n,
                "edges": edges_n,
                "vectors": vectors_n,
            }
