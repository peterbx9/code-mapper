"""JS/TS parser Phase 2 — real AST via tree-sitter.

Replaces the Phase-1 regex parser. Handles JS, JSX, TS, TSX correctly
including dynamic imports, deeply destructured imports, decorators,
TypeScript types/interfaces, and JSX in string literals.

Falls back gracefully if tree-sitter packages aren't installed.

Output schema matches js_parser.py (Node + Edge from .schema) so all
downstream features (--cycles, --hotspots, --html, --graph) work
without changes.
"""
from __future__ import annotations
import logging
from pathlib import Path

from .schema import Edge, EdgeType, Node, NodeType

logger = logging.getLogger(__name__)

try:
    import tree_sitter_javascript
    import tree_sitter_typescript
    from tree_sitter import Language, Parser
    JS_LANG = Language(tree_sitter_javascript.language())
    TS_LANG = Language(tree_sitter_typescript.language_typescript())
    TSX_LANG = Language(tree_sitter_typescript.language_tsx())
    AVAILABLE = True
except Exception as e:
    logger.debug(f"tree-sitter unavailable: {e}")
    AVAILABLE = False


def _parser_for_ext(ext: str):
    if not AVAILABLE:
        return None
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return Parser(JS_LANG)
    if ext == ".ts":
        return Parser(TS_LANG)
    if ext == ".tsx":
        return Parser(TSX_LANG)
    return None


def _node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node):
    """Generator: yield all descendant nodes."""
    cursor = node.walk()
    visited_children = False
    while True:
        if not visited_children:
            yield cursor.node
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break


def _extract_string_literal(node, source_bytes: bytes) -> str | None:
    """Extract value from a string literal node, stripping quotes."""
    if node is None:
        return None
    text = _node_text(node, source_bytes)
    if len(text) >= 2 and text[0] in ('"', "'", "`") and text[-1] == text[0]:
        return text[1:-1]
    return None


def _find_import_source(import_node, source_bytes: bytes) -> str | None:
    """Walk children of an import_statement to find the source string."""
    for child in _walk(import_node):
        if child.type == "string":
            for sc in child.children:
                if sc.type == "string_fragment":
                    return _node_text(sc, source_bytes)
            # fallback: strip quotes
            return _extract_string_literal(child, source_bytes)
    return None


def parse_js_file_v2(path: Path, project_root: Path) -> tuple[list[Node], list[Edge]]:
    """Real-AST extraction of imports, functions, classes from a JS/TS file."""
    parser = _parser_for_ext(path.suffix)
    if parser is None:
        return [], []
    try:
        source_bytes = path.read_bytes()
    except OSError:
        return [], []

    tree = parser.parse(source_bytes)
    rel = str(path.relative_to(project_root)).replace("\\", "/")
    file_id = f"file::{rel}"
    nodes: list[Node] = [
        Node(id=file_id, type=NodeType.FILE, path=rel, name=path.name,
             line_start=1, line_end=tree.root_node.end_point[0] + 1)
    ]
    edges: list[Edge] = []

    seen_fn_ids: set[str] = set()

    def _add_fn(name: str, line: int):
        nid = f"fn::{rel}::{name}"
        if nid in seen_fn_ids:
            return
        seen_fn_ids.add(nid)
        nodes.append(Node(
            id=nid, type=NodeType.FUNCTION, path=rel, name=name,
            line_start=line, parent_id=file_id,
        ))

    for node in _walk(tree.root_node):
        nt = node.type

        # ES6 import statement
        if nt == "import_statement":
            src = _find_import_source(node, source_bytes)
            if src:
                edges.append(Edge(
                    source=file_id, target=f"module::{src}", type=EdgeType.IMPORT,
                ))
            continue

        # CommonJS require: call_expression where fn name is "require"
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and _node_text(fn, source_bytes) == "require":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for arg in args.children:
                        if arg.type == "string":
                            src = _extract_string_literal(arg, source_bytes)
                            if src:
                                edges.append(Edge(
                                    source=file_id, target=f"module::{src}",
                                    type=EdgeType.IMPORT,
                                ))
                                break

        # Dynamic import expression: import("path")
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and _node_text(fn, source_bytes) == "import":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for arg in args.children:
                        if arg.type == "string":
                            src = _extract_string_literal(arg, source_bytes)
                            if src:
                                edges.append(Edge(
                                    source=file_id, target=f"module::{src}",
                                    type=EdgeType.IMPORT,
                                    flags=[],  # could mark dynamic
                                ))
                                break

        # Function declarations
        if nt == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                _add_fn(_node_text(name_node, source_bytes), node.start_point[0] + 1)
            continue

        # Arrow / function expression assigned to const/let/var
        if nt in ("variable_declarator", "lexical_declaration"):
            # variable_declarator has name + value
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if (name_node is not None and value_node is not None
                    and value_node.type in ("arrow_function", "function_expression")):
                _add_fn(_node_text(name_node, source_bytes), node.start_point[0] + 1)
            continue

        # Class declaration
        if nt in ("class_declaration", "abstract_class_declaration"):
            name_node = node.child_by_field_name("name")
            heritage = None
            for child in node.children:
                if child.type == "class_heritage":
                    heritage = _node_text(child, source_bytes)
                    break
            if name_node is not None:
                cname = _node_text(name_node, source_bytes)
                cls = Node(
                    id=f"class::{rel}::{cname}", type=NodeType.CLASS, path=rel,
                    name=cname, line_start=node.start_point[0] + 1,
                    parent_id=file_id,
                )
                if heritage:
                    cls.docstring = heritage
                nodes.append(cls)

        # Method definitions inside classes
        if nt == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                mname = _node_text(name_node, source_bytes)
                # Don't dedupe with same name across methods in different classes
                # — use position-based ID
                nid = f"fn::{rel}::method:{mname}@{node.start_point[0] + 1}"
                if nid not in seen_fn_ids:
                    seen_fn_ids.add(nid)
                    nodes.append(Node(
                        id=nid, type=NodeType.FUNCTION, path=rel, name=mname,
                        line_start=node.start_point[0] + 1, parent_id=file_id,
                    ))

    return nodes, edges


def parse_js_project_v2(project_root: Path,
                          exclude_dirs: set[str] | None = None
                          ) -> tuple[list[Node], list[Edge]]:
    if not AVAILABLE:
        return [], []
    from .js_parser import discover_js_files
    if exclude_dirs is None:
        exclude_dirs = set()
    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    for f in discover_js_files(project_root, exclude_dirs):
        try:
            nodes, edges = parse_js_file_v2(f, project_root)
            all_nodes.extend(nodes)
            all_edges.extend(edges)
        except Exception as e:
            logger.warning(f"tree-sitter parse failed for {f}: {e}")
            continue
    return all_nodes, all_edges
