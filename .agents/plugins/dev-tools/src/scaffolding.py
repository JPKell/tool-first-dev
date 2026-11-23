"""
Purpose: Create new modules (and their linked test stubs) from role templates with valid headers.
Role: service
Patterns: service-layer, structured-errors
Tests: tests/test_navigation.py
Invariants: Never overwrites an existing file; generated headers always validate against config.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import ToolkitConfig, is_test_path
from .headers import parse_and_validate_header
from .indexer import reindex_project
from .parser import parse_file

FALLBACK_TEMPLATES = {
    ".py": '{header}\n\nfrom __future__ import annotations\n\n\ndef {func_name}() -> None:\n'
    '    """TODO: describe purpose, params, return value, and side effects."""\n'
    "    raise NotImplementedError\n",
    ".js": "{header}\n\nexport function {func_name}() {{\n  throw new Error(\"TODO\");\n}}\n",
}
FALLBACK_TEST_TEMPLATES = {
    ".py": "from {module} import {first_symbol}\n\n\ndef test_{stem}_exposes_contract():\n"
    "    assert {first_symbol} is not None\n",
    ".js": 'import {{ {first_symbol} }} from "{import_path}";\n\n'
    'test("{stem} exposes contract", () => {{\n  expect({first_symbol}).toBeDefined();\n}});\n',
}


def _pascal(stem: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-.]+", stem) if part)


def _camel(stem: str) -> str:
    pascal = _pascal(stem)
    return pascal[:1].lower() + pascal[1:]


def _test_dir(cfg: ToolkitConfig) -> str:
    for d in cfg.source_dirs:
        if is_test_path(f"{d}/x"):
            return d
    return "tests"


def _default_test_path(cfg: ToolkitConfig, rel: Path) -> str:
    stem = rel.stem
    if rel.suffix == ".py":
        return f"{_test_dir(cfg)}/test_{stem}.py"
    return f"{_test_dir(cfg)}/{stem}.test{rel.suffix}"


def _load_template(cfg: ToolkitConfig, role: str, suffix: str, test: bool = False) -> str:
    name = f"test{suffix}.tmpl" if test else f"{role}{suffix}.tmpl"
    candidates = [cfg.templates_dir / name]
    if not test:
        candidates.append(cfg.templates_dir / f"default{suffix}.tmpl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    table = FALLBACK_TEST_TEMPLATES if test else FALLBACK_TEMPLATES
    if suffix not in table:
        raise ValueError(f"No template for '{suffix}' files; add .agents/templates/{name}.")
    return table[suffix]


def _render_header(cfg: ToolkitConfig, suffix: str, fields: dict[str, str]) -> str:
    ordered = [f for f in cfg.required_header_fields if f in fields] + [
        f for f in fields if f not in cfg.required_header_fields
    ]
    lines = [f"{key}: {fields[key]}" for key in ordered]
    if suffix == ".py":
        return '"""\n' + "\n".join(lines) + '\n"""'
    return "/**\n" + "\n".join(f" * {line}" for line in lines) + "\n */"


def scaffold_module(
    cfg: ToolkitConfig,
    path: str,
    role: str,
    purpose: str,
    patterns: str = "none",
    invariants: str = "none",
    with_test: bool = True,
) -> dict[str, Any]:
    """Create a module from `.agents/templates/<role><ext>.tmpl` plus a linked test stub.

    Args:
        path: New project-relative module path (e.g. app/services/order_service.py).
        role: One of the configured roles.
        purpose: One-sentence Purpose for the header.
        patterns: Comma-separated configured pattern names (or "none").
        invariants: Header Invariants text.
        with_test: Also create the linked test file if it does not exist.
    Returns:
        {created, header_valid, header_errors, next_steps}.
    Side effects: Writes new files and runs a local (no network) incremental reindex.
    Raises:
        ValueError: unknown role/pattern, path outside project, or path already exists.
    """
    role = role.strip().lower()
    if role not in cfg.roles:
        raise ValueError(f"Unknown role '{role}'. Allowed: {', '.join(cfg.roles)}")
    pattern_list = [p.strip().lower() for p in patterns.split(",") if p.strip()] or ["none"]
    bad = [p for p in pattern_list if p not in cfg.patterns]
    if bad:
        raise ValueError(f"Unknown pattern(s) {', '.join(bad)}. Allowed: {', '.join(cfg.patterns)}")

    rel = Path(path.strip().lstrip("./"))
    full = (cfg.project_root / rel).resolve()
    if cfg.project_root.resolve() not in full.parents:
        raise ValueError(f"Path '{path}' is outside the project root.")
    if full.exists():
        raise ValueError(f"'{rel.as_posix()}' already exists; edit it instead.")
    if rel.suffix not in cfg.extensions:
        raise ValueError(f"Extension '{rel.suffix}' is not indexed (extensions: {', '.join(cfg.extensions)}).")

    test_rel = _default_test_path(cfg, rel) if with_test else "none"
    stem = rel.stem
    module = ".".join(rel.with_suffix("").parts)
    values = {
        "class_name": _pascal(stem),
        "func_name": _camel(stem) if rel.suffix != ".py" else stem,
        "module": module,
        "stem": stem,
    }
    header_fields = {
        "Purpose": purpose.strip().rstrip(".") + ".",
        "Role": role,
        "Patterns": ", ".join(pattern_list),
        "Tests": test_rel,
        "Invariants": invariants.strip() or "none",
    }
    if "Depends on" in cfg.required_header_fields:
        header_fields["Depends on"] = "none"

    body_tmpl = _load_template(cfg, role, rel.suffix)
    body = body_tmpl.format(header="{header}", **values)
    if "Exposes" in cfg.required_header_fields:
        draft = parse_file(rel, body.replace("{header}", ""))
        public = [s.qualified_name for s in draft.symbols if s.top_level and not s.name.startswith("_")]
        header_fields["Exposes"] = ", ".join(public) or "none"
    content = body.replace("{header}", _render_header(cfg, rel.suffix, header_fields))

    created: list[str] = []
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    created.append(rel.as_posix())

    if with_test:
        test_full = cfg.project_root / test_rel
        if not test_full.exists():
            parsed = parse_file(rel, content)
            public = [s.name for s in parsed.symbols if s.top_level and not s.name.startswith("_")]
            import_path = Path("../" * (len(Path(test_rel).parts) - 1)) / rel
            test_body = _load_template(cfg, role, rel.suffix, test=True).format(
                first_symbol=public[0] if public else values["class_name"],
                import_path=import_path.as_posix(),
                **values,
            )
            test_full.parent.mkdir(parents=True, exist_ok=True)
            test_full.write_text(test_body, encoding="utf-8")
            created.append(test_rel)

    header = parse_and_validate_header(rel, content, cfg, parsed=parse_file(rel, content))
    reindex_project(config=cfg, sync_remote=False, trigger="mcp:scaffold_module")
    return {
        "created": created,
        "header_valid": header.is_valid,
        "header_errors": header.errors,
        "next_steps": [
            f"Write real assertions in {test_rel} first (TDD red), then run_tests(scope='{test_rel}').",
            f"Implement {rel.as_posix()}, then lint_and_headers(scope='{rel.as_posix()}').",
        ],
    }
