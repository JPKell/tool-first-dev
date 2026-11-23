#!/usr/bin/env python3
"""Scaffold this Tool-First Development template into another project or extract as a shared package."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

IGNORE_PATTERNS = shutil.ignore_patterns(".venv", "state", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")

ROOT_TEMPLATE_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".mcp.json",
    ".env.example",
    "DevDockerfile",
    ".dockerignore",
]
ROOT_TEMPLATE_DIRS = [
    ".agents",
    ".claude",
    ".codex",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def apply_template(target: Path, force: bool = False) -> list[str]:
    """Copy the Tool-First Development template files into target project directory."""
    src_root = _repo_root()
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    for rel in ROOT_TEMPLATE_FILES:
        src = src_root / rel
        dst = target / rel
        if not src.exists():
            continue
        if dst.exists() and not force:
            continue
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel in ROOT_TEMPLATE_DIRS:
        src = src_root / rel
        dst = target / rel
        if not src.exists():
            continue
        shutil.copytree(src, dst, ignore=IGNORE_PATTERNS, dirs_exist_ok=True)
        copied.append(f"{rel}/")

    return copied


def extract_package(target: Path) -> Path:
    """Export the MCP server & indexer into a standalone installable Python package repo."""
    plugin_root = Path(__file__).resolve().parent
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    pkg_src = target / "tool_first_dev"
    if pkg_src.exists():
        shutil.rmtree(pkg_src)
    shutil.copytree(plugin_root / "src", pkg_src, ignore=IGNORE_PATTERNS)

    pkg_tests = target / "tests"
    if (plugin_root / "tests").exists():
        if pkg_tests.exists():
            shutil.rmtree(pkg_tests)
        shutil.copytree(plugin_root / "tests", pkg_tests, ignore=IGNORE_PATTERNS)

    pyproject = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tool-first-dev"
version = "0.2.0"
description = "Shared Tool-First Development MCP server, Tree-sitter knowledge-graph indexer, and multi-agent hooks."
requires-python = ">=3.11"
dependencies = [
    "mcp>=2.0.0",
    "httpx>=0.27.0",
    "tree-sitter>=0.23.0",
    "tree-sitter-python>=0.23.0",
    "tree-sitter-javascript>=0.23.0",
    "ruff>=0.9.0",
    "pytest>=8.0.0",
]

[project.scripts]
tool-first-mcp = "tool_first_dev.mcp_server:main"
tool-first-index = "tool_first_dev.indexer:main"
tool-first-hook = "tool_first_dev.hook_handler:main"
"""
    (target / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="Scaffold or extract the Tool-First Development toolkit.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply", help="Apply template to another project repository.")
    p_apply.add_argument("target", type=Path, help="Target project directory.")
    p_apply.add_argument("--force", action="store_true", help="Overwrite existing root files.")

    p_extract = sub.add_parser("extract-package", help="Extract toolkit into a standalone Python package.")
    p_extract.add_argument("target", type=Path, help="Destination package directory.")

    args = ap.parse_args()
    if args.cmd == "apply":
        items = apply_template(args.target, force=args.force)
        print(f"Applied template to {args.target}: {', '.join(items)}")
    elif args.cmd == "extract-package":
        dest = extract_package(args.target)
        print(f"Extracted standalone package to {dest}")


if __name__ == "__main__":
    main()
