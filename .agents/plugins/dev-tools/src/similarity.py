"""Dense embedding similarity search backed by OpenWeightGym /v1/embeddings."""

from __future__ import annotations

import math
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


def search_similar(
    query: str,
    files: list[dict[str, Any]],
    vectors: dict[str, dict[str, Any]],
    owg_client: OWGClient,
    role: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank indexed modules most similar to a file path or natural-language query via OWG embeddings."""
    if not files:
        return {"embedding_model": "", "matches": []}
    if not vectors:
        return {
            "embedding_model": "",
            "matches": [],
            "warning": "No OpenWeightGym embeddings stored yet. Run reindex with .env configured.",
        }

    by_path = {f["path"]: f for f in files}
    norm_query = query.strip().lstrip("./")
    exclude_path: str | None = None
    embed_model = ""

    if norm_query in vectors:
        exclude_path = norm_query
        query_vec = vectors[norm_query]["vector"]
        embed_model = vectors[norm_query].get("model", "")
    else:
        embed_model, embedded = owg_client.embed_texts([query], trace="mcp-find-similar")
        query_vec = embedded[0] if embedded else []

    role_filter = role.strip().lower() if role else None
    scored: list[tuple[float, dict[str, Any]]] = []
    for path, f in by_path.items():
        if path == exclude_path:
            continue
        if role_filter and f["role"].lower() != role_filter:
            continue
        vinfo = vectors.get(path)
        if not vinfo:
            continue
        score = cosine_similarity(query_vec, vinfo["vector"])
        scored.append((score, f))

    scored.sort(key=lambda item: (-item[0], item[1]["path"]))
    matches = [
        {
            "path": f["path"],
            "score": round(score, 4),
            "role": f["role"],
            "patterns": f["patterns"],
            "purpose": f["purpose"],
            "exposes": f["exposes"],
            "tests": f["tests"],
            "summary": f["summary"],
        }
        for score, f in scored[:limit]
    ]
    return {"embedding_model": embed_model, "matches": matches}
