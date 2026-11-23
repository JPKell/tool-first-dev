"""SQLite-backed knowledge graph store for files, symbols, edges, summaries, and embeddings."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .headers import ModuleHeader
from .parser import ParsedFile

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

    def lookup_symbol(self, symbol: str, include_impact: bool = True) -> dict[str, Any]:
        """Find symbol definitions, callers, callees, importers, and covering tests."""
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

            result: dict[str, Any] = {
                "symbol": symbol,
                "definitions": definitions,
            }
            if not include_impact:
                return result

            short_name = symbol.split(".")[-1]
            callers_rows = conn.execute(
                """
                SELECT DISTINCT source_file, source_symbol, target, line_number
                FROM edges
                WHERE edge_type = 'calls'
                  AND (target = ? OR target = ? OR target LIKE ? OR target LIKE ?)
                ORDER BY source_file, line_number
                """,
                (symbol, short_name, f"%.{short_name}", f"%.{symbol}"),
            ).fetchall()

            def_files = {d["file_path"] for d in definitions}
            callees: list[dict[str, Any]] = []
            if definitions:
                clauses: list[str] = []
                params: list[str] = []
                for d in definitions:
                    if d["kind"] == "class":
                        clauses.append(
                            "(source_file = ? AND (source_symbol = ? OR source_symbol LIKE ?))"
                        )
                        params.extend([d["file_path"], d["qualified_name"], f"{d['qualified_name']}.%"])
                    else:
                        clauses.append("(source_file = ? AND source_symbol = ?)")
                        params.extend([d["file_path"], d["qualified_name"]])
                where_sql = " OR ".join(clauses)
                callee_rows = conn.execute(
                    f"""
                    SELECT source_file, source_symbol, target, MIN(line_number) AS line_number
                    FROM edges
                    WHERE edge_type = 'calls' AND ({where_sql})
                    GROUP BY source_file, source_symbol, target
                    ORDER BY source_file, line_number
                    """,
                    tuple(params),
                ).fetchall()
                callees = [dict(r) for r in callee_rows]

            importers_rows = conn.execute(
                """
                SELECT DISTINCT source_file, target, line_number
                FROM edges
                WHERE edge_type = 'imports'
                  AND (target = ? OR target LIKE ?)
                ORDER BY source_file, line_number
                """,
                (short_name, f"%.{short_name}"),
            ).fetchall()

            covering_tests: set[str] = set()
            for fpath in def_files:
                t_rows = conn.execute(
                    "SELECT target FROM edges WHERE source_file = ? AND edge_type = 'tested_by'",
                    (fpath,),
                ).fetchall()
                covering_tests.update(r["target"] for r in t_rows)

            for r in callers_rows:
                if r["source_file"].startswith("tests/"):
                    covering_tests.add(f"{r['source_file']}::{r['source_symbol']}")
            for r in importers_rows:
                if r["source_file"].startswith("tests/"):
                    covering_tests.add(r["source_file"])

            result["callers"] = [dict(r) for r in callers_rows]
            result["callees"] = callees
            result["importers"] = [dict(r) for r in importers_rows]
            result["covering_tests"] = sorted(covering_tests)
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
                    {
                        "path": f["path"],
                        "role": f["role"],
                        "patterns": f["patterns"],
                        "purpose": f["purpose"],
                        "exposes": f["exposes"],
                        "tests": f["tests"],
                        "summary": f["summary"],
                    }
                )
        return matches[:limit]

    def repo_map(
        self,
        role: str | None = None,
        scope: str | None = None,
        include_summaries: bool = False,
    ) -> dict[str, Any]:
        """Return a fast, offline architectural map of indexed modules from SQLite."""
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
            modules.append(entry)

        return {
            "total_modules": len(modules),
            "role_filter": role_needle,
            "scope_filter": scope_needle,
            "counts_by_role": dict(sorted(counts_by_role.items())),
            "modules": modules,
        }

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
