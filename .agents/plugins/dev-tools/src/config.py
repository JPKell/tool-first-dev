"""
Purpose: Load .agents/config.json and discover project roots, source files, and test files.
Role: service
Patterns: service-layer
Tests: tests/test_toolkit.py, tests/test_navigation.py
Invariants: Paths returned are project-relative; config defaults keep the toolkit usable offline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REQUIRED_HEADER_FIELDS = ("Purpose", "Role", "Patterns", "Tests", "Invariants")
TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec"})


def is_test_path(path: str) -> bool:
    """True if a project-relative path looks like a test file or lives in a test directory."""
    p = Path(path.split("::", 1)[0])
    name = p.name
    if any(part in TEST_DIR_NAMES for part in p.parts[:-1]):
        return True
    if name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts")):
        return True
    # `test_*.py` outside a test dir only counts at the repo root (src/test_runner.py is not a test)
    return name.startswith("test_") and len(p.parts) == 1


def find_project_root(start: str | Path) -> Path:
    """Walk up from start to the nearest directory containing .agents/config.json (else start)."""
    origin = Path(start).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / ".agents" / "config.json").is_file():
            return candidate
    return origin


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
    tests: dict[str, Any] = field(default_factory=dict)

    @property
    def templates_dir(self) -> Path:
        return self.agents_dir / "templates"

    @property
    def test_timeout_s(self) -> int:
        return int(self.tests.get("timeout_s", 300))

    def remote_enabled(self, kind: str) -> bool:
        """True if 'embeddings' or 'summaries' is configured to use OpenWeightGym."""
        section = self.embeddings if kind == "embeddings" else self.summaries
        return section.get("provider") == "openweightgym"

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
            raw.get("required_header_fields", list(DEFAULT_REQUIRED_HEADER_FIELDS))
        ),
        embeddings=dict(raw.get("embeddings", {"provider": "local-bm25"})),
        summaries=dict(raw.get("summaries", {"provider": "none"})),
        tests=dict(raw.get("tests", {})),
    )
