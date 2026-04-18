"""
Python AST parser for Code Mapper.

Extracts from .py files:
- Imports (module-level + function-scope + conditional + star)
- Function/class definitions with decorators
- FastAPI route registrations
- SQLAlchemy __tablename__ and relationship() strings
- Call edges (direct calls to project modules)
- Docstrings and complexity estimates
"""

import ast
import re
import logging
from pathlib import Path
from typing import Optional

from .schema import Node, Edge, NodeType, EdgeType, EdgeFlag

logger = logging.getLogger(__name__)

FASTAPI_ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "head", "options"}
ROUTER_FACTORIES = {"APIRouter", "FastAPI"}


def parse_file(file_path: Path, project_root: Path) -> tuple[list[Node], list[Edge]]:
    rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        logger.warning(f"Syntax error in {rel_path}: {e}")
        node = Node(
            id=f"file:{rel_path}",
            type=NodeType.FILE,
            path=rel_path,
            name=file_path.stem,
            docstring=f"PARSE ERROR: {e}",
        )
        return [node], []

    nodes = []
    edges = []

    file_node = Node(
        id=f"file:{rel_path}",
        type=NodeType.FILE,
        path=rel_path,
        name=file_path.stem,
        line_start=1,
        line_end=len(source.splitlines()),
        docstring=_get_docstring(tree),
    )

    _extract_tables(tree, file_node)
    _extract_routes_from_module(tree, file_node, source)
    nodes.append(file_node)

    _extract_imports(tree, rel_path, edges)
    _extract_definitions(tree, rel_path, project_root, nodes, edges)
    _extract_call_edges(tree, rel_path, source, edges)

    return nodes, edges


def _get_docstring(node) -> Optional[str]:
    try:
        ds = ast.get_docstring(node)
        if ds:
            first_line = ds.strip().split("\n")[0]
            return first_line[:200]
        return None
    except Exception:
        return None


def _extract_tables(tree: ast.Module, file_node: Node):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    if isinstance(node.value, ast.Constant):
                        val = node.value.value
                        if isinstance(val, str):
                            file_node.tables.append(val)

        if isinstance(node, ast.Call):
            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            if func_name == "relationship":
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        val = arg.value
                        if isinstance(val, str) and val not in file_node.tables:
                            file_node.tables.append(f"rel:{val}")


def _extract_routes_from_module(tree: ast.Module, file_node: Node, source: str):
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) and not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            route_info = _parse_route_decorator(dec)
            if route_info:
                route_info["handler"] = node.name
                route_info["line"] = node.lineno
                file_node.routes.append(route_info)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "include_router":
                route_info = {"type": "include_router", "line": node.lineno}
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        val = kw.value.value
                        route_info["prefix"] = val
                    elif kw.arg == "tags":
                        pass
                if "prefix" not in route_info:
                    route_info["prefix"] = None
                    route_info["warning"] = "include_router called without prefix"
                file_node.routes.append(route_info)


def _parse_route_decorator(dec) -> Optional[dict]:
    if isinstance(dec, ast.Call):
        func = dec.func
        method = None
        if isinstance(func, ast.Attribute) and func.attr in FASTAPI_ROUTE_DECORATORS:
            method = func.attr.upper()
        elif isinstance(func, ast.Name) and func.id in FASTAPI_ROUTE_DECORATORS:
            method = func.id.upper()

        if method and dec.args:
            path_arg = dec.args[0]
            if isinstance(path_arg, ast.Constant):
                path = path_arg.value
                return {"method": method, "path": str(path)}
    return None


def _extract_imports(tree: ast.Module, file_rel_path: str, edges: list[Edge]):
    file_id = f"file:{file_rel_path}"

    scope_tags = _build_scope_tags(tree)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        node_id = id(node)
        flags = []
        if scope_tags.get(node_id, {}).get("in_function"):
            flags.append(EdgeFlag.FUNCTION_SCOPE)
        if scope_tags.get(node_id, {}).get("in_try"):
            flags.append(EdgeFlag.CONDITIONAL)

        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(Edge(
                    source=file_id,
                    target=f"module:{alias.name}",
                    type=EdgeType.IMPORT,
                    flags=flags,
                    metadata={"alias": alias.asname, "line": node.lineno},
                ))

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level or 0

            if level > 0:
                resolved = _resolve_relative_import(file_rel_path, module, level)
            else:
                resolved = module

            if node.names and node.names[0].name == "*":
                edges.append(Edge(
                    source=file_id,
                    target=f"module:{resolved}",
                    type=EdgeType.IMPORT,
                    flags=flags + [EdgeFlag.DYNAMIC],
                    metadata={"star_import": True, "line": node.lineno},
                ))
            else:
                for alias in node.names:
                    edges.append(Edge(
                        source=file_id,
                        target=f"module:{resolved}.{alias.name}" if resolved else f"module:{alias.name}",
                        type=EdgeType.IMPORT,
                        flags=flags,
                        metadata={"alias": alias.asname, "line": node.lineno},
                    ))


def _resolve_relative_import(file_rel_path: str, module: str, level: int) -> str:
    parts = file_rel_path.replace("\\", "/").replace(".py", "").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    for _ in range(level - 1):
        if parts:
            parts.pop()
    package = ".".join(parts[:-1]) if len(parts) > 1 else ""
    if module:
        return f"{package}.{module}" if package else module
    return package


def _build_scope_tags(tree: ast.Module) -> dict:
    tags = {}

    def _walk_with_scope(node, in_function=False, in_try=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        if isinstance(node, (ast.Try, ast.ExceptHandler)):
            in_try = True

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            tags[id(node)] = {"in_function": in_function, "in_try": in_try}

        for child in ast.iter_child_nodes(node):
            _walk_with_scope(child, in_function, in_try)

    _walk_with_scope(tree)
    return tags


def _extract_definitions(tree: ast.Module, file_rel_path: str, project_root: Path,
                         nodes: list[Node], edges: list[Edge]):
    file_id = f"file:{file_rel_path}"

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            class_id = f"class:{file_rel_path}:{node.name}"
            class_node = Node(
                id=class_id,
                type=NodeType.CLASS,
                path=file_rel_path,
                name=node.name,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=_get_docstring(node),
                decorators=[_decorator_name(d) for d in node.decorator_list],
                parent_id=file_id,
            )

            for base in node.bases:
                base_name = _resolve_name(base)
                if base_name and base_name not in ("object", "Base", "BaseModel"):
                    edges.append(Edge(
                        source=class_id,
                        target=f"class_ref:{base_name}",
                        type=EdgeType.INHERITANCE,
                        metadata={"line": node.lineno},
                    ))

            nodes.append(class_node)

            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_id = f"func:{file_rel_path}:{node.name}.{item.name}"
                    is_stub = _is_stub(item)
                    func_node = Node(
                        id=func_id,
                        type=NodeType.FUNCTION,
                        path=file_rel_path,
                        name=f"{node.name}.{item.name}",
                        line_start=item.lineno,
                        line_end=item.end_lineno or item.lineno,
                        docstring=_get_docstring(item),
                        decorators=[_decorator_name(d) for d in item.decorator_list],
                        parent_id=class_id,
                        is_stub=is_stub,
                        complexity=_estimate_complexity(item),
                    )
                    nodes.append(func_node)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_id = f"func:{file_rel_path}:{node.name}"
            is_stub = _is_stub(node)
            func_node = Node(
                id=func_id,
                type=NodeType.FUNCTION,
                path=file_rel_path,
                name=node.name,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=_get_docstring(node),
                decorators=[_decorator_name(d) for d in node.decorator_list],
                parent_id=file_id,
                is_stub=is_stub,
                complexity=_estimate_complexity(node),
            )
            nodes.append(func_node)


def _extract_call_edges(tree: ast.Module, file_rel_path: str, source: str, edges: list[Edge]):
    file_id = f"file:{file_rel_path}"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        call_name = None

        if isinstance(func, ast.Name):
            call_name = func.id
        elif isinstance(func, ast.Attribute):
            call_name = func.attr
            if isinstance(func.value, ast.Name):
                call_name = f"{func.value.id}.{func.attr}"

        if call_name and not _is_builtin_call(call_name):
            edges.append(Edge(
                source=file_id,
                target=f"call:{call_name}",
                type=EdgeType.CALL,
                metadata={"line": node.lineno},
            ))


def _decorator_name(dec) -> str:
    if isinstance(dec, ast.Name):
        return dec.id
    elif isinstance(dec, ast.Attribute):
        return f"{_resolve_name(dec.value)}.{dec.attr}"
    elif isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return "unknown"


def _resolve_name(node) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        parent = _resolve_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return None


def _is_stub(func_node) -> bool:
    body = func_node.body
    if not body:
        return True
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return True
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            return True
    if len(body) == 2:
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[1], (ast.Pass, ast.Return)):
                return True
    return False


def _estimate_complexity(func_node) -> int:
    complexity = 1
    for node in ast.walk(func_node):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += len(node.values) - 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            complexity += 1
    return complexity


_BUILTINS = frozenset({
    "print", "len", "range", "int", "str", "float", "bool", "list", "dict",
    "set", "tuple", "type", "isinstance", "issubclass", "hasattr", "getattr",
    "setattr", "delattr", "super", "property", "staticmethod", "classmethod",
    "enumerate", "zip", "map", "filter", "sorted", "reversed", "min", "max",
    "sum", "any", "all", "abs", "round", "repr", "id", "hash", "next", "iter",
    "open", "format", "vars", "dir", "callable", "input",
})


def _is_builtin_call(name: str) -> bool:
    base = name.split(".")[0]
    return base in _BUILTINS
