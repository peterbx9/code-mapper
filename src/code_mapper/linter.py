"""
AST-based lint rules for Code Mapper (Tier 1).

Catches intra-file issues that the structural parser misses:
- Dead imports (imported but never referenced)
- Unused parameters
- Unused module-level constants/variables
- raise NotImplementedError stubs
- list.pop(0) performance anti-pattern
- json.loads/json.dumps without try/except
- Stats/counts before filtering (order-of-operations smell)
"""

import ast
import logging
from pathlib import Path
from typing import Optional

from .schema import RepoMap, Node, NodeType

logger = logging.getLogger(__name__)


class LintFinding:
    def __init__(self, file_path: str, line: int, rule: str, severity: str, desc: str):
        self.file_path = file_path
        self.line = line
        self.rule = rule
        self.severity = severity
        self.desc = desc

    def to_dict(self):
        return {
            "file": self.file_path,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "desc": self.desc,
        }

    def __repr__(self):
        return f"[{self.severity}] {self.file_path}:{self.line} {self.rule}: {self.desc}"


def lint_project(project_root: Path, repo_map: Optional[RepoMap] = None,
                 exclude_dirs: set = None) -> list[LintFinding]:
    findings = []
    if exclude_dirs is None:
        exclude_dirs = set()

    for py_file in sorted(project_root.rglob("*.py")):
        rel = str(py_file.relative_to(project_root)).replace("\\", "/")

        skip = False
        for part in py_file.relative_to(project_root).parts[:-1]:
            if part in exclude_dirs:
                skip = True
                break
        if skip or py_file.name.endswith("-OFF.py"):
            continue

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        findings.extend(_check_dead_imports(tree, rel))
        findings.extend(_check_unused_params(tree, rel))
        findings.extend(_check_unused_constants(tree, rel))
        findings.extend(_check_notimplemented_stubs(tree, rel))
        findings.extend(_check_list_pop_zero(tree, rel))
        findings.extend(_check_unguarded_json(tree, rel))
        findings.extend(_check_unused_argparse_args(tree, rel))
        findings.extend(_check_self_assign_in_except(tree, rel))
        findings.extend(_check_swallowed_exceptions(tree, rel))
        findings.extend(_check_unguarded_file_open(tree, rel))
        findings.extend(_check_magic_numbers(tree, rel))

    return findings


def _get_all_names_used(tree: ast.Module) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                names.add(node.value.id)
    return names


def _check_dead_imports(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    names_used = _get_all_names_used(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                if local_name not in names_used:
                    findings.append(LintFinding(
                        file_path=file_path,
                        line=node.lineno,
                        rule="DEAD_IMPORT",
                        severity="low",
                        desc=f"'{alias.name}' imported but '{local_name}' never used",
                    ))

        elif isinstance(node, ast.ImportFrom):
            if node.names and node.names[0].name == "*":
                continue
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname or alias.name
                if local_name not in names_used:
                    findings.append(LintFinding(
                        file_path=file_path,
                        line=node.lineno,
                        rule="DEAD_IMPORT",
                        severity="low",
                        desc=f"'{alias.name}' from '{module}' imported but '{local_name}' never used",
                    ))

    return findings


def _check_unused_params(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    SKIP_PARAMS = {"self", "cls", "args", "kwargs", "db", "request", "response", "_",
                   "user", "current_user", "session", "token", "background_tasks"}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if node.name.startswith("_") and node.name != "__init__":
            continue

        body_names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                body_names.add(child.id)

        for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
            param_name = arg.arg
            if param_name in SKIP_PARAMS or param_name.startswith("_"):
                continue
            if param_name not in body_names:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=arg.lineno if hasattr(arg, 'lineno') else node.lineno,
                    rule="UNUSED_PARAM",
                    severity="low",
                    desc=f"Parameter '{param_name}' in '{node.name}()' is never used in function body",
                ))

    return findings


def _check_unused_constants(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    names_used = _get_all_names_used(tree)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name.startswith("_") or name.startswith("__"):
                        continue
                    is_constant_style = name.isupper() or (
                        "_" in name and all(
                            p[0].isupper() if p else True for p in name.split("_")
                        )
                    )
                    if is_constant_style and len(name) > 1 and name not in names_used:
                        findings.append(LintFinding(
                            file_path=file_path,
                            line=node.lineno,
                            rule="UNUSED_CONSTANT",
                            severity="low",
                            desc=f"Module-level constant '{name}' defined but never referenced",
                        ))

    return findings


def _check_notimplemented_stubs(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        body = node.body
        effective_body = body
        if (len(body) >= 1 and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], 'value', None), (ast.Constant, ast.Str))):
            effective_body = body[1:]

        if len(effective_body) == 1:
            stmt = effective_body[0]
            if isinstance(stmt, ast.Raise):
                exc = stmt.exc
                if isinstance(exc, ast.Call):
                    func = exc.func
                    if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                        findings.append(LintFinding(
                            file_path=file_path,
                            line=node.lineno,
                            rule="NOTIMPLEMENTED_STUB",
                            severity="med",
                            desc=f"'{node.name}()' only raises NotImplementedError — likely a stub",
                        ))

    return findings


def _check_list_pop_zero(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "pop":
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 0):
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="LIST_POP_ZERO",
                    severity="med",
                    desc="list.pop(0) is O(n) — use collections.deque.popleft() for O(1)",
                ))

    return findings


def _check_unguarded_json(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    scope_tags = _build_try_scope(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = None
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "json":
                func_name = func.attr
        if func_name in ("loads", "load"):
            if not scope_tags.get(id(node), False):
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="UNGUARDED_JSON",
                    severity="med",
                    desc=f"json.{func_name}() called without try/except — JSONDecodeError will crash",
                ))

    return findings


def _check_unused_argparse_args(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue

        dest_name = None
        for arg in node.args:
            if isinstance(arg, (ast.Constant, ast.Str)):
                val = arg.value if isinstance(arg, ast.Constant) else arg.s
                if isinstance(val, str) and val.startswith("--"):
                    dest_name = val.lstrip("-").replace("-", "_")
                    break

        if not dest_name:
            continue

        attr_ref = f"args.{dest_name}"
        module_node = tree
        found = False
        for search_node in ast.walk(module_node):
            if isinstance(search_node, ast.Attribute):
                if (isinstance(search_node.value, ast.Name)
                        and search_node.value.id == "args"
                        and search_node.attr == dest_name):
                    if search_node is not node:
                        found = True
                        break

        if not found:
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="UNUSED_ARG_FLAG",
                severity="med",
                desc=f"argparse flag '--{dest_name.replace('_', '-')}' defined but 'args.{dest_name}' never read",
            ))

    return findings


def _check_self_assign_in_except(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect X = X inside except blocks — usually a NameError waiting to happen."""
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Name):
                    if target.id == stmt.value.id:
                        findings.append(LintFinding(
                            file_path=file_path,
                            line=stmt.lineno,
                            rule="SELF_ASSIGN_IN_EXCEPT",
                            severity="high",
                            desc=f"'{target.id} = {stmt.value.id}' in except block — if the try failed before assigning '{target.id}', this raises NameError",
                        ))

    return findings


def _check_swallowed_exceptions(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect except blocks that catch broadly and don't log/raise/re-raise."""
    findings = []

    LOGGING_CALLS = {"log", "logger", "logging", "print", "traceback", "warn", "warning", "error", "critical"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        is_broad = (node.type is None or
                    (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")))
        if not is_broad:
            continue

        has_raise = False
        has_log = False
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                has_raise = True
            if isinstance(child, ast.Call):
                func = child.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                    if isinstance(func.value, ast.Name):
                        name = f"{func.value.id}.{func.attr}"
                if any(log_name in name.lower() for log_name in LOGGING_CALLS):
                    has_log = True

        if not has_raise and not has_log:
            body_stmts = [s for s in node.body if not isinstance(s, ast.Pass)]
            if len(body_stmts) <= 1:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="SWALLOWED_EXCEPTION",
                    severity="med",
                    desc="Broad except catches Exception but doesn't log, raise, or re-raise — failures are invisible",
                ))

    return findings


def _check_unguarded_file_open(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect open() calls not inside try/except."""
    findings = []
    scope_tags = _build_try_scope(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_open = False
        if isinstance(func, ast.Name) and func.id == "open":
            is_open = True
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            is_open = True

        if is_open and not scope_tags.get(id(node), False):
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="UNGUARDED_FILE_OPEN",
                severity="low",
                desc="open() called without try/except — FileNotFoundError or PermissionError will crash",
            ))

    return findings


def _check_magic_numbers(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect numeric literals that match a module-level constant's value but use the literal instead."""
    findings = []

    constants = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                        if node.value.value not in (0, 1, 2, -1, 100, True, False):
                            constants[node.value.value] = target.id

    if not constants:
        return findings

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, (int, float)):
            continue
        if node.value not in constants:
            continue

        is_module_level_assign = False
        for top_node in ast.iter_child_nodes(tree):
            if isinstance(top_node, ast.Assign):
                for child in ast.walk(top_node):
                    if child is node:
                        is_module_level_assign = True
                        break
        if is_module_level_assign:
            continue

        const_name = constants[node.value]
        findings.append(LintFinding(
            file_path=file_path,
            line=node.lineno,
            rule="MAGIC_NUMBER_VS_CONSTANT",
            severity="low",
            desc=f"Literal {node.value} used instead of constant '{const_name}' (defined in this file with the same value)",
        ))

    return findings


def _build_try_scope(tree: ast.Module) -> dict:
    tags = {}

    def _walk(node, in_try=False):
        if isinstance(node, (ast.Try, ast.ExceptHandler)):
            in_try = True
        if isinstance(node, ast.Call):
            tags[id(node)] = in_try
        for child in ast.iter_child_nodes(node):
            _walk(child, in_try)

    _walk(tree)
    return tags
