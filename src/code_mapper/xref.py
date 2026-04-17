"""
Cross-reference symbol table for Code Mapper (Tier 1.5).

Tracks every symbol (function, class, constant, variable) across the project:
where it's defined, where it's imported, where it's called/referenced.
Detects cross-file dead symbols without any LLM.
"""

import ast
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schema import RepoMap, NodeType

logger = logging.getLogger(__name__)


@dataclass
class SymbolRef:
    name: str
    type: str
    defined_in: str
    defined_line: int
    imported_by: list[dict] = field(default_factory=list)
    called_from: list[dict] = field(default_factory=list)
    referenced_in: list[dict] = field(default_factory=list)

    @property
    def is_used(self) -> bool:
        return bool(self.imported_by or self.called_from or self.referenced_in)

    @property
    def usage_count(self) -> int:
        return len(self.imported_by) + len(self.called_from) + len(self.referenced_in)

    def to_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "defined_in": self.defined_in,
            "defined_line": self.defined_line,
            "imported_by": self.imported_by,
            "called_from": self.called_from,
            "referenced_in": self.referenced_in,
            "is_used": self.is_used,
            "usage_count": self.usage_count,
        }


@dataclass
class XRefTable:
    symbols: dict[str, SymbolRef] = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    _internal_refs: dict[str, set] = field(default_factory=dict)

    def to_dict(self):
        return {
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
            "findings": self.findings,
            "stats": {
                "total_symbols": len(self.symbols),
                "unused": sum(1 for s in self.symbols.values() if not s.is_used),
                "most_referenced": self._top_referenced(5),
            },
        }

    def _top_referenced(self, n: int) -> list[dict]:
        ranked = sorted(self.symbols.values(), key=lambda s: s.usage_count, reverse=True)
        return [{"name": s.name, "defined_in": s.defined_in, "usage_count": s.usage_count}
                for s in ranked[:n]]


def build_xref(project_root: Path, repo_map: RepoMap, exclude_dirs: set = None) -> XRefTable:
    project_root = project_root.resolve()
    if exclude_dirs is None:
        exclude_dirs = set()

    xref = XRefTable()

    for node in repo_map.nodes:
        if node.type == NodeType.FUNCTION:
            short_name = node.name.split(".")[-1] if "." in node.name else node.name
            key = f"{node.path}:{short_name}"
            xref.symbols[key] = SymbolRef(
                name=short_name,
                type="function",
                defined_in=node.path,
                defined_line=node.line_start,
            )
        elif node.type == NodeType.CLASS:
            key = f"{node.path}:{node.name}"
            xref.symbols[key] = SymbolRef(
                name=node.name,
                type="class",
                defined_in=node.path,
                defined_line=node.line_start,
            )

    _scan_module_level_symbols(project_root, repo_map, xref, exclude_dirs)
    _scan_internal_references(project_root, repo_map, xref)
    _scan_references(project_root, repo_map, xref, exclude_dirs)
    xref._repo_map = repo_map
    _detect_cross_file_issues(xref)
    _detect_duplicate_functions(project_root, repo_map, xref)

    return xref


def _scan_module_level_symbols(project_root: Path, repo_map: RepoMap,
                                xref: XRefTable, exclude_dirs: set):
    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue

        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            continue

        for stmt in ast.iter_child_nodes(tree):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        if name.startswith("_") and not name.startswith("__"):
                            continue
                        key = f"{node.path}:{name}"
                        if key not in xref.symbols:
                            sym_type = "constant" if name.isupper() or "_" in name and name[0].isupper() else "variable"
                            xref.symbols[key] = SymbolRef(
                                name=name,
                                type=sym_type,
                                defined_in=node.path,
                                defined_line=stmt.lineno,
                            )


def _scan_internal_references(project_root: Path, repo_map: RepoMap, xref: XRefTable):
    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        file_path = project_root / node.path
        if not file_path.exists():
            continue
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            continue

        defined_names = set()
        for key, sym in xref.symbols.items():
            if sym.defined_in == node.path:
                defined_names.add(sym.name)

        used_names = set()
        defined_at_lines = {}
        for stmt in ast.iter_child_nodes(tree):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        defined_at_lines[target.id] = stmt.lineno
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined_at_lines[stmt.name] = stmt.lineno
            elif isinstance(stmt, ast.ClassDef):
                defined_at_lines[stmt.name] = stmt.lineno

        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.Name) and ast_node.id in defined_names:
                name = ast_node.id
                def_line = defined_at_lines.get(name, 0)
                if hasattr(ast_node, 'lineno') and ast_node.lineno != def_line:
                    used_names.add(name)
            elif isinstance(ast_node, ast.Attribute):
                if isinstance(ast_node.value, ast.Name) and ast_node.value.id in defined_names:
                    used_names.add(ast_node.value.id)

        for stmt in ast.walk(tree):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(stmt):
                    if isinstance(child, ast.Name) and child.id in defined_names:
                        used_names.add(child.id)

        xref._internal_refs[node.path] = used_names


def _scan_references(project_root: Path, repo_map: RepoMap,
                     xref: XRefTable, exclude_dirs: set):
    symbol_name_index = defaultdict(list)
    for key, sym in xref.symbols.items():
        symbol_name_index[sym.name].append(key)

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue

        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            continue

        defined_in_this_file = {
            sym.name for key, sym in xref.symbols.items()
            if sym.defined_in == node.path
        }

        imports_map = _get_imports_map(tree)

        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.ImportFrom):
                module = ast_node.module or ""
                for alias in ast_node.names:
                    imported_name = alias.name
                    candidates = symbol_name_index.get(imported_name, [])
                    for cand_key in candidates:
                        cand = xref.symbols[cand_key]
                        if cand.defined_in != node.path:
                            cand.imported_by.append({
                                "file": node.path,
                                "line": ast_node.lineno,
                                "as": alias.asname,
                            })

            if isinstance(ast_node, ast.Call):
                call_name = _get_call_name(ast_node)
                if call_name:
                    base_name = call_name.split(".")[-1]
                    resolved = imports_map.get(call_name.split(".")[0], call_name.split(".")[0])
                    candidates = symbol_name_index.get(base_name, [])
                    for cand_key in candidates:
                        cand = xref.symbols[cand_key]
                        if cand.defined_in != node.path:
                            cand.called_from.append({
                                "file": node.path,
                                "line": ast_node.lineno,
                                "call": call_name,
                            })

            if isinstance(ast_node, ast.Name):
                name = ast_node.id
                if name in defined_in_this_file:
                    continue
                candidates = symbol_name_index.get(name, [])
                for cand_key in candidates:
                    cand = xref.symbols[cand_key]
                    if cand.defined_in != node.path and cand.type in ("constant", "variable"):
                        if not any(r["file"] == node.path for r in cand.referenced_in):
                            cand.referenced_in.append({
                                "file": node.path,
                                "line": ast_node.lineno,
                            })


def _get_imports_map(tree: ast.Module) -> dict:
    mapping = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[-1]
                mapping[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                mapping[local] = f"{module}.{alias.name}" if module else alias.name
    return mapping


def _find_decorated_handlers(xref: XRefTable) -> set:
    handlers = set()
    FRAMEWORK_DECORATORS = {"get", "post", "put", "delete", "patch", "head", "options",
                            "route", "websocket", "on_event", "middleware"}
    for key, sym in xref.symbols.items():
        if sym.type != "function":
            continue
        node = _find_node_in_repo(xref, sym)
        if node and node.decorators:
            for dec in node.decorators:
                dec_parts = dec.lower().split(".")
                if any(part in FRAMEWORK_DECORATORS for part in dec_parts):
                    handlers.add(key)
                    break
    return handlers


def _find_depends_targets(xref: XRefTable) -> set:
    targets = set()
    DEPENDS_INJECTED = {"get_db", "get_current_user", "require_admin", "require_permission",
                        "get_session", "get_token"}
    FRAMEWORK_OVERRIDES = {
        "handle_starttag", "handle_endtag", "handle_data", "handle_startendtag",
        "handle_comment", "handle_decl", "handle_pi", "handle_charref",
        "handle_entityref", "feed", "close", "reset",
        "setUp", "tearDown", "setUpClass", "tearDownClass",
        "test_", "__enter__", "__exit__", "__repr__", "__str__",
        "__init__", "__del__", "__hash__", "__eq__", "__lt__", "__gt__",
    }
    for key, sym in xref.symbols.items():
        if sym.name in DEPENDS_INJECTED:
            targets.add(sym.name)
        if sym.name in FRAMEWORK_OVERRIDES:
            targets.add(sym.name)
    return targets


def _find_node_in_repo(xref: XRefTable, sym: SymbolRef):
    if not hasattr(xref, '_repo_map'):
        return None
    repo_map = xref._repo_map
    for node in repo_map.nodes:
        if node.type == NodeType.FUNCTION and node.path == sym.defined_in:
            short = node.name.split(".")[-1] if "." in node.name else node.name
            if short == sym.name and node.line_start == sym.defined_line:
                return node
    return None


def _detect_duplicate_functions(project_root: Path, repo_map: RepoMap, xref: XRefTable):
    """Find functions with near-identical bodies across different files."""
    import hashlib

    func_hashes = defaultdict(list)

    for node in repo_map.nodes:
        if node.type != NodeType.FUNCTION:
            continue
        if node.name.startswith("_") and node.name != "__init__":
            continue

        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = source.splitlines()
        func_lines = lines[node.line_start - 1:node.line_end]
        if len(func_lines) < 3:
            continue

        body_lines = func_lines[1:]
        normalized = "\n".join(line.strip() for line in body_lines if line.strip())
        if len(normalized) < 30:
            continue

        body_hash = hashlib.md5(normalized.encode()).hexdigest()
        short_name = node.name.split(".")[-1] if "." in node.name else node.name
        func_hashes[body_hash].append({
            "name": short_name,
            "file": node.path,
            "line": node.line_start,
            "body_len": len(func_lines),
        })

    for body_hash, funcs in func_hashes.items():
        if len(funcs) < 2:
            continue

        files_involved = {f["file"] for f in funcs}
        if len(files_involved) < 2:
            continue

        locations = [f"{f['file']}:{f['line']}" for f in funcs]
        names = list({f["name"] for f in funcs})

        for func in funcs:
            this_loc = f"{func['file']}:{func['line']}"
            other_locs = ", ".join(loc for loc in locations if loc != this_loc)
            xref.findings.append({
                "rule": "XREF_DUPLICATE_FUNCTION",
                "severity": "med",
                "file": func["file"],
                "line": func["line"],
                "desc": f"Function '{func['name']}' body is identical to {other_locs} — extract to shared module",
            })


def _get_call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    elif isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return None


def _detect_cross_file_issues(xref: XRefTable):
    decorated_handlers = _find_decorated_handlers(xref)
    depends_functions = _find_depends_targets(xref)

    for key, sym in xref.symbols.items():
        if sym.name.startswith("_"):
            continue
        if sym.name in ("__init__", "__main__", "main"):
            continue
        if key in decorated_handlers:
            continue
        if sym.name in depends_functions:
            continue

        internally_used = sym.name in xref._internal_refs.get(sym.defined_in, set())

        if not sym.is_used and not internally_used:
            if sym.type in ("function", "class"):
                xref.findings.append({
                    "rule": "XREF_UNUSED_SYMBOL",
                    "severity": "med",
                    "file": sym.defined_in,
                    "line": sym.defined_line,
                    "desc": f"{sym.type.title()} '{sym.name}' defined and never used (not even in its own file)",
                })
            elif sym.type == "constant":
                xref.findings.append({
                    "rule": "XREF_UNUSED_CONSTANT",
                    "severity": "low",
                    "file": sym.defined_in,
                    "line": sym.defined_line,
                    "desc": f"Constant '{sym.name}' defined but never referenced anywhere",
                })

        if sym.imported_by and not sym.called_from and not sym.referenced_in:
            if sym.type == "function" and not internally_used:
                importers = [i["file"] for i in sym.imported_by]
                xref.findings.append({
                    "rule": "XREF_IMPORTED_NOT_CALLED",
                    "severity": "low",
                    "file": sym.defined_in,
                    "line": sym.defined_line,
                    "desc": f"Function '{sym.name}' imported by {importers} but never actually called",
                })
