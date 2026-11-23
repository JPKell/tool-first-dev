"""Structured module header parser and contract validator."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ToolkitConfig

FIELD_RE = re.compile(r"^\s*(?:[*#-]\s*)?([A-Za-z][A-Za-z _-]{1,24})\s*:\s*(.*)$")


@dataclass
class ModuleHeader:
    present: bool = False
    purpose: str = ""
    role: str = ""
    patterns: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    exposes: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    invariants: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def _split_csv(value: str) -> list[str]:
    if not value or value.strip().lower() in {"none", "n/a", "-"}:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _extract_python_header_text(source: str) -> str | None:
    try:
        tree = ast.parse(source)
        doc = ast.get_docstring(tree, clean=True)
        if doc:
            return doc
    except SyntaxError:
        pass

    # Fallback: contiguous leading `#` comment block
    lines = source.splitlines()
    comment_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped and not comment_lines:
            continue
        if stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            comment_lines.append(stripped.lstrip("#").strip())
        else:
            break
    return "\n".join(comment_lines) if comment_lines else None


def _extract_js_header_text(source: str) -> str | None:
    stripped = source.lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/")
        if end != -1:
            block = stripped[2:end]
            cleaned = [re.sub(r"^\s*\*\s?", "", line) for line in block.splitlines()]
            return "\n".join(cleaned).strip()
    comment_lines: list[str] = []
    for line in source.splitlines():
        s = line.strip()
        if not s and not comment_lines:
            continue
        if s.startswith("//"):
            comment_lines.append(s[2:].strip())
        else:
            break
    return "\n".join(comment_lines) if comment_lines else None


def parse_and_validate_header(
    rel_path: Path,
    source: str,
    config: ToolkitConfig,
) -> ModuleHeader:
    """Parse structured header fields from a module and validate against project rules."""
    header = ModuleHeader()
    raw_text = (
        _extract_python_header_text(source)
        if rel_path.suffix == ".py"
        else _extract_js_header_text(source)
    )

    canonical_map = {f.lower(): f for f in config.required_header_fields}
    if raw_text:
        current_key: str | None = None
        for line in raw_text.splitlines():
            m = FIELD_RE.match(line)
            if m:
                key_norm = m.group(1).strip().lower()
                if key_norm in canonical_map:
                    canon = canonical_map[key_norm]
                    header.raw_fields[canon] = m.group(2).strip()
                    current_key = canon
                    continue
            if current_key and line.strip():
                header.raw_fields[current_key] = (
                    f"{header.raw_fields[current_key]} {line.strip()}".strip()
                )

    header.present = bool(header.raw_fields)
    header.purpose = header.raw_fields.get("Purpose", "")
    header.role = header.raw_fields.get("Role", "").strip().lower()
    header.patterns = [p.lower() for p in _split_csv(header.raw_fields.get("Patterns", ""))]
    header.depends_on = _split_csv(header.raw_fields.get("Depends on", ""))
    header.exposes = _split_csv(header.raw_fields.get("Exposes", ""))
    header.tests = _split_csv(header.raw_fields.get("Tests", ""))
    header.invariants = header.raw_fields.get("Invariants", "")

    required = config.requires_header(rel_path)
    if not required and not header.present:
        # Optional for test files, but infer role='test' when omitted
        if rel_path.parts and rel_path.parts[0] == "tests":
            header.role = "test"
        return header

    if not header.present:
        header.errors.append("Missing required module header.")
        return header

    for field_name in config.required_header_fields:
        if field_name not in header.raw_fields or not header.raw_fields[field_name].strip():
            header.errors.append(f"Missing required header field: {field_name}")

    allowed_roles = set(config.roles)
    if header.role and header.role not in allowed_roles:
        header.errors.append(
            f"Unknown Role '{header.role}'. Allowed: {', '.join(config.roles)}"
        )

    allowed_patterns = set(config.patterns)
    for pattern in header.patterns:
        if pattern not in allowed_patterns:
            header.errors.append(
                f"Unknown Pattern '{pattern}'. Allowed: {', '.join(config.patterns)}"
            )

    return header
