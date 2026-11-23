"""Incremental and full knowledge-graph indexing pipeline with OpenWeightGym summaries & embeddings."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from .config import ToolkitConfig, load_config
from .graph_store import GraphStore
from .headers import parse_and_validate_header
from .owg_client import OWGClient, OWGError, load_owg_settings
from .parser import parse_file
from .similarity import build_embedding_document


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_current_hashes(config: ToolkitConfig) -> dict[str, str]:
    """Hash every discovered source file in the workspace."""
    hashes: dict[str, str] = {}
    for rel in config.discover_source_files():
        full = config.project_root / rel
        if full.is_file():
            text = full.read_text(encoding="utf-8", errors="ignore")
            hashes[rel.as_posix()] = sha256_text(text)
    return hashes


def reindex_project(
    config: ToolkitConfig | None = None,
    full: bool = False,
    sync_remote: bool = True,
    trigger: str = "manual",
    owg_client: OWGClient | None = None,
    max_remote_summaries: int | None = None,
    summary_workers: int = 4,
) -> dict[str, Any]:
    """Incrementally (or fully) index source files into the SQLite knowledge graph.

    When sync_remote=True, stale/missing summaries and embeddings are generated via
    OpenWeightGym (/v1/chat/completions and /v1/embeddings). When sync_remote=False
    (e.g., mid-turn PostToolUse), AST symbols, edges, and headers update instantly while
    leaving remote summary/embedding sync for the post-turn Stop hook or explicit reindex.
    """
    cfg = config or load_config()
    store = GraphStore(cfg.db_path)

    discovered = cfg.discover_source_files()
    existing_hashes = {} if full else store.get_indexed_hashes()

    current_paths: set[str] = set()
    updated_files: list[str] = []
    skipped_files = 0
    invalid_headers: list[dict[str, Any]] = []
    sources_by_path: dict[str, str] = {}

    for rel in discovered:
        rel_str = rel.as_posix()
        current_paths.add(rel_str)
        full_path = cfg.project_root / rel
        source = full_path.read_text(encoding="utf-8", errors="ignore")
        sources_by_path[rel_str] = source
        content_hash = sha256_text(source)

        if not full and existing_hashes.get(rel_str) == content_hash:
            skipped_files += 1
            continue

        header = parse_and_validate_header(rel, source, cfg)
        parsed = parse_file(rel, source)
        prev = store.get_file(rel_str)
        prev_summary = prev["summary"] if prev and not full else ""
        prev_summary_hash = prev["summary_hash"] if prev and not full else ""
        prev_summary_provider = prev["summary_provider"] if prev and not full else ""

        store.upsert_file(
            rel_path=rel_str,
            content_hash=content_hash,
            parsed=parsed,
            header=header,
            summary=prev_summary,
            summary_hash=prev_summary_hash,
            summary_provider=prev_summary_provider,
        )
        updated_files.append(rel_str)
        if not header.is_valid:
            invalid_headers.append({"path": rel_str, "errors": header.errors})

    stale_deleted = sorted(set(existing_hashes.keys()) - current_paths)
    store.delete_files(stale_deleted)

    summarized_files: list[str] = []
    deferred_summaries = 0
    embedded_files: list[str] = []
    remote_errors: list[str] = []
    summary_model_used = ""
    embedding_model_used = ""

    if sync_remote and current_paths:
        client = owg_client or OWGClient(
            load_owg_settings(
                cfg.project_root,
                suite_application=cfg.embeddings.get("suite_application", "tool-first-dev"),
            )
        )
        all_files = store.get_all_files()
        max_chars = int(cfg.summaries.get("max_source_chars", 8000))

        # 1. Generate OpenWeightGym summaries concurrently for files whose content hash changed
        need_summary = [f for f in all_files if full or f["summary_hash"] != f["content_hash"]]
        if max_remote_summaries is not None and len(need_summary) > max_remote_summaries:
            deferred_summaries = len(need_summary) - max_remote_summaries
            summary_batch = need_summary[:max_remote_summaries]
        else:
            summary_batch = need_summary

        can_summarize = True
        if summary_batch and hasattr(client, "resolve_summary_model"):
            try:
                client.resolve_summary_model()
            except OWGError as exc:
                remote_errors.append(f"summary(init): {exc}")
                can_summarize = False
            except Exception as exc:
                remote_errors.append(f"summary(init): {exc}")
                can_summarize = False

        if summary_batch and can_summarize:
            workers = max(1, min(summary_workers, len(summary_batch)))

            def _summarize_one(file_row: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
                src = sources_by_path.get(file_row["path"], "")
                m_id, s_text = client.summarize_module(
                    rel_path=file_row["path"],
                    role=file_row["role"],
                    patterns=file_row["patterns"],
                    source=src,
                    max_source_chars=max_chars,
                    trace=f"kg-{trigger}",
                )
                return file_row, m_id, s_text

            if workers == 1:
                for f in summary_batch:
                    try:
                        _, model_id, summary_text = _summarize_one(f)
                        summary_model_used = model_id
                        store.update_summary(
                            rel_path=f["path"],
                            summary=summary_text,
                            summary_hash=f["content_hash"],
                            summary_provider=f"openweightgym:{model_id}",
                        )
                        summarized_files.append(f["path"])
                    except Exception as exc:
                        remote_errors.append(f"summary({f['path']}): {exc}")
                        if isinstance(exc, OWGError) and exc.status_code in (None, 401, 403):
                            break
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_summarize_one, f): f for f in summary_batch}
                    for fut in as_completed(futures):
                        f = futures[fut]
                        try:
                            _, model_id, summary_text = fut.result()
                            summary_model_used = model_id
                            store.update_summary(
                                rel_path=f["path"],
                                summary=summary_text,
                                summary_hash=f["content_hash"],
                                summary_provider=f"openweightgym:{model_id}",
                            )
                            summarized_files.append(f["path"])
                        except Exception as exc:
                            remote_errors.append(f"summary({f['path']}): {exc}")
                summarized_files.sort()

        # 2. Generate OpenWeightGym embeddings in one batch for stale/missing vectors
        all_files = store.get_all_files()
        symbols_by_file = store.get_all_symbols_by_file()
        existing_vectors = {} if full else store.get_all_vectors()
        need_embed = [
            f
            for f in all_files
            if full
            or f["path"] not in existing_vectors
            or existing_vectors[f["path"]].get("content_hash") != f["content_hash"]
            or f["path"] in summarized_files
        ]
        if need_embed:
            docs = [build_embedding_document(f, symbols_by_file.get(f["path"], [])) for f in need_embed]
            try:
                model_id, vecs = client.embed_texts(docs, trace=f"kg-{trigger}")
                embedding_model_used = model_id
                for f, vec in zip(need_embed, vecs, strict=True):
                    store.upsert_vector(
                        rel_path=f["path"],
                        provider="openweightgym",
                        model=model_id,
                        content_hash=f["content_hash"],
                        vector=vec,
                    )
                    embedded_files.append(f["path"])
            except Exception as exc:
                remote_errors.append(f"embeddings: {exc}")

    now = datetime.now(UTC).isoformat()
    store.set_meta("last_indexed_at", now)
    store.set_meta("last_reindex_trigger", trigger)
    store.set_meta("schema_version", "2")
    if summary_model_used:
        store.set_meta("last_summary_model", summary_model_used)
    if embedding_model_used:
        store.set_meta("last_embedding_model", embedding_model_used)

    return {
        "trigger": trigger,
        "full": full,
        "sync_remote": sync_remote,
        "updated_files": updated_files,
        "skipped_unchanged": skipped_files,
        "deleted_files": stale_deleted,
        "summarized_files": summarized_files,
        "deferred_summaries": deferred_summaries,
        "embedded_files": embedded_files,
        "summary_model": summary_model_used,
        "embedding_model": embedding_model_used,
        "invalid_headers": invalid_headers,
        "remote_errors": remote_errors,
        "graph_stats": store.stats(),
        "indexed_at": now,
    }


def get_index_status(config: ToolkitConfig | None = None) -> dict[str, Any]:
    """Compare live workspace files against the stored knowledge graph, summaries, and embeddings."""
    cfg = config or load_config()
    store = GraphStore(cfg.db_path)

    live_hashes = compute_current_hashes(cfg)
    indexed_hashes = store.get_indexed_hashes()

    unindexed = sorted(set(live_hashes.keys()) - set(indexed_hashes.keys()))
    deleted = sorted(set(indexed_hashes.keys()) - set(live_hashes.keys()))
    stale = sorted(
        p
        for p in set(live_hashes.keys()) & set(indexed_hashes.keys())
        if live_hashes[p] != indexed_hashes[p]
    )

    all_files = store.get_all_files()
    vectors = store.get_all_vectors()

    invalid_headers = [
        {"path": f["path"], "errors": f["header_errors"]}
        for f in all_files
        if not f["header_valid"]
    ]
    stale_summaries = [
        f["path"] for f in all_files if not f["summary"] or f["summary_hash"] != f["content_hash"]
    ]
    stale_embeddings = [
        f["path"]
        for f in all_files
        if f["path"] not in vectors or vectors[f["path"]].get("content_hash") != f["content_hash"]
    ]

    owg_settings = load_owg_settings(cfg.project_root)
    fresh = not (unindexed or deleted or stale or stale_summaries or stale_embeddings)
    return {
        "fresh": fresh,
        "owg_configured": owg_settings.is_configured,
        "owg_base_url": owg_settings.base_url if owg_settings.is_configured else "",
        "graph_stats": store.stats(),
        "meta": store.get_meta(),
        "unindexed_files": unindexed,
        "stale_files": stale,
        "deleted_files": deleted,
        "invalid_headers": invalid_headers,
        "stale_summaries": stale_summaries,
        "stale_embeddings": stale_embeddings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild or update the project knowledge graph.")
    ap.add_argument("--full", action="store_true", help="Rebuild every file, summary, and embedding.")
    ap.add_argument("--local-only", action="store_true", help="Only update AST/headers (skip OWG calls).")
    ap.add_argument("--trigger", default="cli", help="Trigger label stored in graph metadata.")
    args = ap.parse_args()
    result = reindex_project(full=args.full, sync_remote=not args.local_only, trigger=args.trigger)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
