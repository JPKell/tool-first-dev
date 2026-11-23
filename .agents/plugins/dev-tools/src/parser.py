"""
Purpose: Tree-sitter based symbol, import, and call-graph extractor for Python and JavaScript.
Role: util
Patterns: none
Tests: tests/test_toolkit.py, tests/test_navigation.py
Invariants: Pure parsing (no I/O); nested functions are indexed as `outer.inner` and own their calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

PY_LANGUAGE = Language(tspython.language())
JS_LANGUAGE = Language(tsjs.language())

NOISE_PY_CALLS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "chr",
        "dict",
        "dir",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "object",
        "open",
        "ord",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "slice",
        "sorted",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "vars",
        "zip",
    }
)

NOISE_PY_ATTR_SUFFIXES = frozenset(
    {
        "add",
        "append",
        "clear",
        "copy",
        "count",
        "decode",
        "encode",
        "endswith",
        "extend",
        "format",
        "get",
        "index",
        "insert",
        "isdigit",
        "items",
        "join",
        "keys",
        "lower",
        "lstrip",
        "pop",
        "remove",
        "replace",
        "rstrip",
        "setdefault",
        "sort",
        "split",
        "splitlines",
        "startswith",
        "strip",
        "title",
        "update",
        "upper",
        "values",
    }
)

NOISE_JS_CALLS = frozenset(
    {
        "Array",
        "Boolean",
        "Date",
        "Error",
        "Map",
        "Number",
        "Object",
        "Promise",
        "RegExp",
        "Set",
        "String",
        "Symbol",
        "decodeURIComponent",
        "encodeURIComponent",
        "isFinite",
        "isNaN",
        "parseFloat",
        "parseInt",
        "setInterval",
        "setTimeout",
        "clearInterval",
        "clearTimeout",
        "JSON.parse",
        "JSON.stringify",
        "Object.entries",
        "Object.fromEntries",
        "Object.keys",
        "Object.values",
        "Object.assign",
        "Array.from",
        "Array.isArray",
        "Date.now",
    }
)

NOISE_JS_PREFIXES = ("console.", "Math.", "Number.", "Promise.")

NOISE_JS_ATTR_SUFFIXES = frozenset(
    {
        "add",
        "addEventListener",
        "append",
        "appendChild",
        "blur",
        "closest",
        "concat",
        "contains",
        "createElement",
        "endsWith",
        "every",
        "filter",
        "find",
        "flat",
        "flatMap",
        "focus",
        "forEach",
        "getAttribute",
        "getElementById",
        "has",
        "includes",
        "indexOf",
        "join",
        "keys",
        "lastIndexOf",
        "localeCompare",
        "map",
        "match",
        "padStart",
        "pop",
        "preventDefault",
        "push",
        "querySelector",
        "querySelectorAll",
        "reduce",
        "remove",
        "removeAttribute",
        "removeChild",
        "removeEventListener",
        "repeat",
        "replace",
        "replaceAll",
        "reverse",
        "scrollIntoView",
        "setAttribute",
        "shift",
        "slice",
        "some",
        "sort",
        "splice",
        "split",
        "startsWith",
        "stopPropagation",
        "substring",
        "test",
        "toFixed",
        "toISOString",
        "toLocaleString",
        "toLocaleTimeString",
        "toLowerCase",
        "toString",
        "toUpperCase",
        "toggle",
        "trim",
        "unshift",
        "values",
    }
)


@dataclass
class ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str  # class | function | method
    start_line: int
    end_line: int
    signature: str
    docstring: str = ""
    top_level: bool = True  # False for closures and defs inside `if __name__ == "__main__"`


@dataclass
class ExtractedEdge:
    source_symbol: str
    target: str
    edge_type: str  # imports | calls
    line_number: int


@dataclass
class ParsedFile:
    language: str
    line_count: int
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    edges: list[ExtractedEdge] = field(default_factory=list)
    import_modules: list[str] = field(default_factory=list)  # module names only, in source order


def _node_text(node: Node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _clean_docstring(raw: str) -> str:
    s = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        if s.startswith(quote) and s.endswith(quote) and len(s) >= 2 * len(quote):
            s = s[len(quote) : -len(quote)]
            break
    return " ".join(line.strip() for line in s.splitlines()).strip()[:400]


def _extract_py_docstring(body_node: Node | None, src_bytes: bytes) -> str:
    if body_node is None:
        return ""
    for child in body_node.named_children:
        if child.type == "expression_statement" and child.named_children:
            expr = child.named_children[0]
            if expr.type == "string":
                return _clean_docstring(_node_text(expr, src_bytes))
        break
    return ""


def _extract_py_signature(node: Node, body: Node | None, src_bytes: bytes) -> str:
    if body is not None and body.start_byte > node.start_byte:
        header = src_bytes[node.start_byte : body.start_byte].decode("utf-8", errors="ignore").strip()
        if header.endswith(":"):
            header = header[:-1].rstrip()
        return " ".join(header.split())[:240]
    first_line = _node_text(node, src_bytes).splitlines()[0].strip().rstrip(":")
    return " ".join(first_line.split())[:240]


def _is_noisy_py_call(target: str) -> bool:
    if not target or len(target) > 120 or "\n" in target or "(" in target or "[" in target:
        return True
    if target in NOISE_PY_CALLS:
        return True
    if "." in target:
        leaf = target.rsplit(".", 1)[-1]
        if leaf in NOISE_PY_ATTR_SUFFIXES:
            return True
    return False


def _is_noisy_js_call(target: str) -> bool:
    if not target or len(target) > 120 or "\n" in target or "(" in target or "[" in target:
        return True
    if target in NOISE_JS_CALLS or target.startswith(NOISE_JS_PREFIXES):
        return True
    if "." in target:
        leaf = target.rsplit(".", 1)[-1]
        if leaf in NOISE_JS_ATTR_SUFFIXES:
            return True
    return False


PY_DEF_TYPES = frozenset({"function_definition", "class_definition", "decorated_definition"})
JS_DEF_TYPES = frozenset({"function_declaration", "class_declaration"})


def _nested_defs(node: Node, def_types: frozenset[str]) -> list[Node]:
    """Return definitions nested anywhere under node without descending into other definitions."""
    found: list[Node] = []
    for child in node.named_children:
        if child.type in def_types:
            found.append(child)
        else:
            found.extend(_nested_defs(child, def_types))
    return found


def _is_main_guard(node: Node, src_bytes: bytes) -> bool:
    cond = node.child_by_field_name("condition")
    return node.type == "if_statement" and cond is not None and "__name__" in _node_text(cond, src_bytes)


def _collect_py_calls(node: Node, src_bytes: bytes, owner: str, out: list[ExtractedEdge]) -> None:
    if node.type == "call":
        func = node.child_by_field_name("function")
        if func is not None:
            target = _node_text(func, src_bytes).strip()
            if not _is_noisy_py_call(target):
                out.append(
                    ExtractedEdge(
                        source_symbol=owner,
                        target=target,
                        edge_type="calls",
                        line_number=node.start_point.row + 1,
                    )
                )
    for child in node.named_children:
        # Don't attribute nested function/class bodies to the outer owner
        if child.type in {"function_definition", "class_definition", "decorated_definition"}:
            continue
        _collect_py_calls(child, src_bytes, owner, out)


def _parse_python(src_bytes: bytes) -> ParsedFile:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node
    result = ParsedFile(
        language="python",
        line_count=len(src_bytes.splitlines()),
    )

    def walk(
        node: Node,
        class_prefix: str = "",
        in_function: bool = False,
        top_level: bool = True,
        start_override: int | None = None,
    ) -> None:
        ntype = node.type

        if ntype == "import_statement":
            for child in node.named_children:
                if child.type in {"dotted_name", "aliased_import"}:
                    target = _node_text(child, src_bytes).split(" as ")[0].strip()
                    result.import_modules.append(target)
                    result.edges.append(
                        ExtractedEdge(
                            source_symbol="<module>",
                            target=target,
                            edge_type="imports",
                            line_number=node.start_point.row + 1,
                        )
                    )
            return

        if ntype == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            mod_name = _node_text(mod_node, src_bytes).strip() if mod_node else ""
            if mod_name:
                result.import_modules.append(mod_name)
            imported_names: list[str] = []
            for child in node.named_children:
                if child is mod_node:
                    continue
                if child.type in {"dotted_name", "aliased_import"}:
                    name = _node_text(child, src_bytes).split(" as ")[0].strip()
                    imported_names.append(f"{mod_name}.{name}" if mod_name else name)
            if imported_names:
                for target in imported_names:
                    result.edges.append(
                        ExtractedEdge(
                            source_symbol="<module>",
                            target=target,
                            edge_type="imports",
                            line_number=node.start_point.row + 1,
                        )
                    )
            elif mod_name:
                result.edges.append(
                    ExtractedEdge(
                        source_symbol="<module>",
                        target=mod_name,
                        edge_type="imports",
                        line_number=node.start_point.row + 1,
                    )
                )
            return

        if ntype == "class_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                first_line = _node_text(node, src_bytes).splitlines()[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind="class",
                        start_line=start_override or node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=first_line,
                        docstring=_extract_py_docstring(body, src_bytes),
                        top_level=top_level and not class_prefix,
                    )
                )
                if body:
                    for child in body.named_children:
                        walk(child, qname, in_function=False, top_level=False)
            return

        if ntype == "function_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                kind = "method" if class_prefix and not in_function else "function"
                header_text = _extract_py_signature(node, body, src_bytes)
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind=kind,
                        start_line=start_override or node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=header_text,
                        docstring=_extract_py_docstring(body, src_bytes),
                        top_level=top_level and not class_prefix,
                    )
                )
                if body:
                    _collect_py_calls(body, src_bytes, qname, result.edges)
                    # Closures/local classes become `outer.inner` symbols that own their calls
                    for nested in _nested_defs(body, PY_DEF_TYPES):
                        walk(nested, qname, in_function=True, top_level=False)
            return

        if ntype == "decorated_definition":
            for child in node.named_children:
                if child.type in {"function_definition", "class_definition"}:
                    walk(
                        child,
                        class_prefix,
                        in_function=in_function,
                        top_level=top_level,
                        start_override=node.start_point.row + 1,
                    )
            return

        if not class_prefix:
            _collect_py_calls(node, src_bytes, "<module>", result.edges)
            # Defs inside module-level blocks (if/try/with) are still real symbols
            nested_top = top_level and not _is_main_guard(node, src_bytes)
            for nested in _nested_defs(node, PY_DEF_TYPES):
                walk(nested, "", top_level=nested_top)
            return

        for child in node.named_children:
            walk(child, class_prefix, in_function=in_function, top_level=False)

    for child in root.named_children:
        walk(child)
    return result


def _collect_js_calls(node: Node, src_bytes: bytes, owner: str, out: list[ExtractedEdge]) -> None:
    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func is not None:
            target = _node_text(func, src_bytes).strip()
            if not _is_noisy_js_call(target):
                out.append(
                    ExtractedEdge(
                        source_symbol=owner,
                        target=target,
                        edge_type="calls",
                        line_number=node.start_point.row + 1,
                    )
                )
    for child in node.named_children:
        if child.type in {"function_declaration", "class_declaration", "method_definition"}:
            continue
        _collect_js_calls(child, src_bytes, owner, out)


def _extract_js_import_names(node: Node, src_bytes: bytes) -> list[str]:
    names: list[str] = []

    def _visit(n: Node) -> None:
        if n.type == "import_specifier":
            orig = n.child_by_field_name("name")
            if orig is not None:
                names.append(_node_text(orig, src_bytes).strip())
            return
        if n.type == "import_clause":
            for c in n.named_children:
                if c.type == "identifier":
                    names.append(_node_text(c, src_bytes).strip())
                else:
                    _visit(c)
            return
        for c in n.named_children:
            _visit(c)

    _visit(node)
    return names


def _parse_javascript(src_bytes: bytes) -> ParsedFile:
    parser = Parser(JS_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node
    result = ParsedFile(
        language="javascript",
        line_count=len(src_bytes.splitlines()),
    )

    def _handle_lexical_declaration(node: Node, class_prefix: str) -> bool:
        """Extract top-level arrow/function expressions assigned to const/let/var."""
        handled_fn = False
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and value_node is not None
                and value_node.type in {"arrow_function", "function_expression"}
            ):
                name = _node_text(name_node, src_bytes).strip()
                qname = f"{class_prefix}.{name}" if class_prefix else name
                body = value_node.child_by_field_name("body")
                raw_sig = _node_text(decl, src_bytes).split("{", 1)[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind="function",
                        start_line=decl.start_point.row + 1,
                        end_line=decl.end_point.row + 1,
                        signature=" ".join(raw_sig.split())[:240],
                        top_level=not class_prefix,
                    )
                )
                if body is not None:
                    _collect_js_calls(body, src_bytes, qname, result.edges)
                handled_fn = True
            elif value_node is not None and not class_prefix:
                _collect_js_calls(value_node, src_bytes, "<module>", result.edges)
        return handled_fn

    def walk(node: Node, class_prefix: str = "") -> None:
        ntype = node.type

        if ntype == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node:
                mod_target = _node_text(source_node, src_bytes).strip("'\"")
                result.import_modules.append(mod_target)
                line_no = node.start_point.row + 1
                result.edges.append(
                    ExtractedEdge(
                        source_symbol="<module>",
                        target=mod_target,
                        edge_type="imports",
                        line_number=line_no,
                    )
                )
                for sym_name in _extract_js_import_names(node, src_bytes):
                    if sym_name:
                        result.edges.append(
                            ExtractedEdge(
                                source_symbol="<module>",
                                target=f"{mod_target}.{sym_name}",
                                edge_type="imports",
                                line_number=line_no,
                            )
                        )
            return

        if ntype == "export_statement":
            for child in node.named_children:
                walk(child, class_prefix)
            return

        if ntype in {"lexical_declaration", "variable_declaration"}:
            _handle_lexical_declaration(node, class_prefix)
            return

        if ntype == "class_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                first_line = _node_text(node, src_bytes).splitlines()[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind="class",
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=first_line,
                        top_level=not class_prefix,
                    )
                )
                if body:
                    for child in body.named_children:
                        walk(child, qname)
            return

        if ntype in {"function_declaration", "method_definition"}:
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                kind = "method" if ntype == "method_definition" else "function"
                first_line = _node_text(node, src_bytes).split("{", 1)[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind=kind,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=" ".join(first_line.split())[:240],
                        top_level=not class_prefix,
                    )
                )
                if body:
                    _collect_js_calls(body, src_bytes, qname, result.edges)
                    for nested in _nested_defs(body, JS_DEF_TYPES):
                        first_new = len(result.symbols)
                        walk(nested, qname)
                        for sym in result.symbols[first_new:]:
                            sym.top_level = False
            return

        if not class_prefix:
            _collect_js_calls(node, src_bytes, "<module>", result.edges)
            return

        for child in node.named_children:
            walk(child, class_prefix)

    for child in root.named_children:
        walk(child)
    return result


def parse_file(rel_path: Path, source: str) -> ParsedFile:
    """Parse a Python or JavaScript source file using Tree-sitter."""
    src_bytes = source.encode("utf-8")
    if rel_path.suffix == ".py":
        return _parse_python(src_bytes)
    if rel_path.suffix == ".js":
        return _parse_javascript(src_bytes)
    return ParsedFile(language="unknown", line_count=len(source.splitlines()))
