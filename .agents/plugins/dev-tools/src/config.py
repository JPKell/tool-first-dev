"""Configuration and path discovery for the Tool-First Development toolkit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolkitConfig:
    project_root: Path
    agents_dir: Path
    rules_path: Path
    ruff_config_path: Path
    state_dir: Path
    db_path: Path
    usage_log_path: Path
    non_mcp_log_path: Path
    project_name: str
    source_dirs: tuple[str, ...]
    header_required_dirs: tuple[str, ...]
    ignore_dirs: frozenset[str]
    extensions: tuple[str, ...]
    roles: tuple[str, ...]
    patterns: tuple[str, ...]
    required_header_fields: tuple[str, ...]
    embeddings: dict[str, Any] = field(default_factory=dict)
    summaries: dict[str, Any] = field(default_factory=dict)

    def discover_source_files(self, scope: str | None = None) -> list[Path]:
        """Return sorted project-relative source file paths under configured source_dirs."""
        roots: list[Path] = []
        if scope:
            scoped = (self.project_root / scope).resolve()
            if scoped.is_file():
                if scoped.suffix in self.extensions:
                    return [scoped.relative_to(self.project_root)]
                return []
            if scoped.is_dir():
                roots = [scoped]
            else:
                return []
        else:
            for rel in self.source_dirs:
                candidate = self.project_root / rel
                if candidate.exists():
                    roots.append(candidate)

        discovered: list[Path] = []
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in self.ignore_dirs and not d.startswith(".venv")
                )
                for fname in sorted(filenames):
                    fpath = Path(dirpath) / fname
                    if fpath.suffix in self.extensions:
                        discovered.append(fpath.relative_to(self.project_root))
        return sorted(set(discovered))

    def requires_header(self, rel_path: Path) -> bool:
        """True if this relative path falls inside header_required_dirs."""
        if rel_path.name == "__init__.py":
            full = self.project_root / rel_path
            if full.exists() and len(full.read_text(encoding="utf-8", errors="ignore").strip()) < 120:
                return False
        parts = rel_path.parts
        if not parts:
            return False
        return any(
            rel_path.as_posix() == d or rel_path.as_posix().startswith(f"{d}/")
            for d in self.header_required_dirs
        )


def resolve_project_root(explicit_root: str | Path | None = None) -> Path:
    """Resolve the repository root from explicit arg, env var, or plugin layout."""
    if explicit_root:
        return Path(explicit_root).resolve()
    env_root = os.environ.get("PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()
    # Default: <repo>/.agents/plugins/dev-tools/src/config.py -> parents[4]
    return Path(__file__).resolve().parents[4]


def load_config(explicit_root: str | Path | None = None) -> ToolkitConfig:
    """Load .agents/config.json with sensible defaults."""
    project_root = resolve_project_root(explicit_root)
    agents_dir = project_root / ".agents"
    cfg_path = agents_dir / "config.json"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    state_dir = agents_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    return ToolkitConfig(
        project_root=project_root,
        agents_dir=agents_dir,
        rules_path=agents_dir / "RULES.md",
        ruff_config_path=agents_dir / "ruff.toml",
        state_dir=state_dir,
        db_path=state_dir / "knowledge_graph.db",
        usage_log_path=state_dir / "tool_usage.jsonl",
        non_mcp_log_path=state_dir / "non_mcp_tool_usage.jsonl",
        project_name=raw.get("project_name", project_root.name),
        source_dirs=tuple(raw.get("source_dirs", ["app", "tests"])),
        header_required_dirs=tuple(raw.get("header_required_dirs", ["app"])),
        ignore_dirs=frozenset(
            raw.get("ignore_dirs", [".git", ".agents", ".venv", "__pycache__", "node_modules"])
        ),
        extensions=tuple(raw.get("extensions", [".py", ".js"])),
        roles=tuple(
            raw.get(
                "roles",
                ["page", "component", "service", "repository", "router", "schema", "util", "test"],
            )
        ),
        patterns=tuple(raw.get("patterns", ["none"])),
        required_header_fields=tuple(
            raw.get(
                "required_header_fields",
                ["Purpose", "Role", "Patterns", "Depends on", "Exposes", "Tests", "Invariants"],
            )
        ),
        embeddings=dict(raw.get("embeddings", {"provider": "local-tfidf"})),
        summaries=dict(raw.get("summaries", {"provider": "heuristic"})),
    )
