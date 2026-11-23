"""
Purpose: Rank modules similar to a query via OpenWeightGym embeddings or an offline BM25 fallback.
Role: service
Patterns: service-layer
Tests: tests/test_toolkit.py, tests/test_navigation.py
Invariants: Always returns a ranking when modules are indexed; dense search is optional.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .owg_client import OWGClient


def build_embedding_document(file_row: dict[str, Any], symbols: list[dict[str, Any]]) -> str:
    """Build concise rich text representing a module for dense embedding."""
    sym_names = [s["qualified_name"] for s in symbols if not s["name"].startswith("_")][:20]
    lines = [
        f"Path: {file_row['path']}",
        f"Role: {file_row['role'] or 'unspecified'}",
        f"Patterns: {', '.join(file_row['patterns']) or 'none'}",
        f"Purpose: {file_row['purpose']}",
        f"Exposes: {', '.join(file_row['exposes']) or ', '.join(sym_names)}",
        f"Depends on: {', '.join(file_row['depends_on']) or 'none'}",
        f"Invariants: {file_row['invariants'] or 'none'}",
        f"Summary: {file_row['summary']}",
    ]
    return "\n".join(line for line in lines if line.split(":", 1)[1].strip())


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it",
        "of", "on", "or", "the", "to", "with", "none", "path", "role", "patterns", "purpose",
        "exposes", "depends", "invariants", "summary", "py", "js",
    }
)
SUMMARY_CHARS = 300


def tokenize(text: str) -> list[str]:
    """Split identifiers (snake_case, camelCase, paths) and prose into lowercase terms."""
    terms: list[str] = []
    for raw in re.split(r"[^A-Za-z0-9]+", text):
        for part in TOKEN_RE.findall(raw):
            low = part.lower()
            if len(low) > 1 and low not in STOPWORDS:
                terms.append(low[:-1] if len(low) > 4 and low.endswith("s") else low)
    return terms


def bm25_scores(query: str, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    """Okapi BM25 score of every document for the query (pure Python, no dependencies)."""
    tokenized = {key: tokenize(text) for key, text in docs.items()}
    if not tokenized:
        return {}
    avg_len = sum(len(t) for t in tokenized.values()) / len(tokenized) or 1.0
    df: Counter[str] = Counter()
    for terms in tokenized.values():
        df.update(set(terms))
    n_docs = len(tokenized)
    q_terms = set(tokenize(query))
    scores: dict[str, float] = {}
    for key, terms in tokenized.items():
        tf = Counter(terms)
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * len(terms) / avg_len))
        scores[key] = score
    return scores


def _match_entry(f: dict[str, Any], score: float) -> dict[str, Any]:
    entry = {
        "path": f["path"],
        "score": round(score, 4),
        "role": f["role"],
        "patterns": f["patterns"],
        "purpose": f["purpose"],
        "exposes": f["exposes"],
        "tests": f["tests"],
        "summary": (f.get("summary") or "")[:SUMMARY_CHARS],
    }
    return {k: v for k, v in entry.items() if v not in ("", [], None)}


def search_similar(
    query: str,
    files: list[dict[str, Any]],
    vectors: dict[str, dict[str, Any]],
    owg_client: OWGClient | None,
    role: str | None = None,
    limit: int = 5,
    symbols_by_file: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Rank indexed modules most similar to a file path or natural-language query.

    Uses dense OpenWeightGym embeddings when vectors are stored and a client is available
    (or the query is an indexed path with a stored vector); otherwise, or if the embedding
    call fails, falls back to offline BM25 over each module's header, symbols, and summary.
    Returns {"method": "dense"|"bm25", "embedding_model", "matches", "warning"?}.
    """
    if not files:
        return {"method": "none", "embedding_model": "", "matches": []}

    by_path = {f["path"]: f for f in files}
    norm_query = query.strip().lstrip("./")
    role_filter = role.strip().lower() if role else None
    exclude_path = norm_query if norm_query in by_path else None
    warning = ""

    def _eligible(path: str, f: dict[str, Any]) -> bool:
        return path != exclude_path and not (role_filter and f["role"].lower() != role_filter)

    query_vec: list[float] = []
    embed_model = ""
    if vectors:
        if norm_query in vectors:
            query_vec = vectors[norm_query]["vector"]
            embed_model = vectors[norm_query].get("model", "")
        elif owg_client is not None:
            try:
                embed_model, embedded = owg_client.embed_texts([query], trace="mcp-find-similar")
                query_vec = embedded[0] if embedded else []
            except Exception as exc:
                warning = f"Dense embedding failed ({exc}); used BM25."

    if query_vec:
        scored = [
            (cosine_similarity(query_vec, vectors[p]["vector"]), f)
            for p, f in by_path.items()
            if _eligible(p, f) and p in vectors
        ]
        method = "dense"
    else:
        symbols_by_file = symbols_by_file or {}
        docs = {p: build_embedding_document(f, symbols_by_file.get(p, [])) for p, f in by_path.items()}
        q_text = docs[exclude_path] if exclude_path else query
        bm25 = bm25_scores(q_text, docs)
        scored = [(bm25.get(p, 0.0), f) for p, f in by_path.items() if _eligible(p, f)]
        scored = [item for item in scored if item[0] > 0]
        method = "bm25"

    scored.sort(key=lambda item: (-item[0], item[1]["path"]))
    out: dict[str, Any] = {
        "method": method,
        "embedding_model": embed_model,
        "matches": [_match_entry(f, score) for score, f in scored[:limit]],
    }
    if warning:
        out["warning"] = warning
    return out
