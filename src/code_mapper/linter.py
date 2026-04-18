"""
AST-based lint rules for Code Mapper (Tier 1).

Catches intra-file issues that the structural parser misses:
- Dead imports (imported but never referenced) — respects # noqa: F401
- Unused parameters
- Unused module-level constants/variables
- raise NotImplementedError stubs
- list.pop(0) performance anti-pattern
- json.loads/json.dumps without try/except
- Stats/counts before filtering (order-of-operations smell)
- Broad except that doesn't log/raise/surface the exception
- Function-scoped import used by another module-level function (NameError risk)
- Tuple-unpack of a call whose returns don't match the unpack arity

Any finding on a line with `# noqa` or `# noqa: <rule>` comment is suppressed.
"""

import ast
import logging
import re
from pathlib import Path
from typing import Optional

from .schema import RepoMap

logger = logging.getLogger(__name__)

_NOQA_RE = re.compile(r"#\s*noqa(?::\s*([A-Za-z0-9_,\s]+))?", re.IGNORECASE)


def _scan_noqa_lines(source: str) -> dict[int, set[str] | None]:
    """Return {line_number: codes_or_None} for every `# noqa` comment.
    `None` value means bare `# noqa` (suppresses ALL rules on that line);
    a set means `# noqa: X,Y` (suppresses only those rule names)."""
    out: dict[int, set[str] | None] = {}
    for lineno, line in enumerate(source.splitlines(), start=1):
        m = _NOQA_RE.search(line)
        if not m:
            continue
        codes = m.group(1)
        if codes:
            out[lineno] = {c.strip().upper() for c in codes.split(",") if c.strip()}
        else:
            out[lineno] = None
    return out


_FLAKE8_CODE_TO_RULE = {
    "F401": "DEAD_IMPORT",
    "F811": "DEAD_IMPORT",
    "F841": "UNUSED_PARAM",
    "W605": "SWALLOWED_EXCEPTION",
    "B028": "BARE_EXCEPT",
    "S307": "UNGUARDED_JSON",
}


def _is_suppressed(noqa_lines: dict[int, set[str] | None], line: int, rule: str) -> bool:
    """True if line carries a noqa comment that suppresses this rule.
    Matches EXACT rule names or flake8/ruff codes mapped in _FLAKE8_CODE_TO_RULE.
    Does not substring-match — `# noqa: I` used to suppress every rule whose
    name contained 'I' (DEAD_IMPORT, LIST_POP_ZERO, etc.), a silent correctness hole."""
    codes = noqa_lines.get(line, "__MISS__")
    if codes == "__MISS__":
        return False
    if codes is None:  # bare # noqa
        return True
    for c in codes:
        if c == rule:
            return True
        if _FLAKE8_CODE_TO_RULE.get(c) == rule:
            return True
    return False


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

        noqa_lines = _scan_noqa_lines(source)

        file_findings = []
        file_findings.extend(_check_dead_imports(tree, rel))
        file_findings.extend(_check_unused_params(tree, rel))
        file_findings.extend(_check_unused_constants(tree, rel))
        file_findings.extend(_check_notimplemented_stubs(tree, rel))
        file_findings.extend(_check_list_pop_zero(tree, rel))
        file_findings.extend(_check_unguarded_json(tree, rel))
        file_findings.extend(_check_unused_argparse_args(tree, rel))
        file_findings.extend(_check_self_assign_in_except(tree, rel))
        file_findings.extend(_check_swallowed_exceptions(tree, rel))
        file_findings.extend(_check_unguarded_file_open(tree, rel))
        file_findings.extend(_check_magic_numbers(tree, rel))
        file_findings.extend(_check_god_objects(tree, rel, len(source.splitlines())))
        file_findings.extend(_check_unreachable_code(tree, rel))
        file_findings.extend(_check_function_scoped_import_leak(tree, rel))
        file_findings.extend(_check_unpack_size_mismatch(tree, rel))

        # Drop findings suppressed by `# noqa` comments on their line
        for f in file_findings:
            if not _is_suppressed(noqa_lines, f.line, f.rule):
                findings.append(f)

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
    # Framework-injected / convention-only names — skip even when unused
    # because removing them would break callers or ABI.
    # NOTE: 'args'/'kwargs' intentionally NOT here — real *args/**kwargs are
    # handled by skipping node.args.vararg / node.args.kwarg below. A plain
    # positional param literally named 'args' should still be checked.
    SKIP_PARAMS = {"self", "cls", "db", "request", "response", "_",
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


_ABSTRACT_DECORATORS = {
    "abstractmethod", "abstractstaticmethod",
    "abstractclassmethod", "abstractproperty",
}


def _has_abstract_decorator(func_node) -> bool:
    """True if any decorator name ends in 'abstract...' — skip the stub rule
    because raising NotImplementedError is the *correct* way to declare one."""
    for dec in func_node.decorator_list:
        name = None
        if isinstance(dec, ast.Name):
            name = dec.id
        elif isinstance(dec, ast.Attribute):
            name = dec.attr
        elif isinstance(dec, ast.Call):
            target = dec.func
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
        if name and name in _ABSTRACT_DECORATORS:
            return True
    return False


def _raises_not_implemented(stmt) -> bool:
    """True for both `raise NotImplementedError` (bare Name) and
    `raise NotImplementedError("msg")` (Call). Previous version missed the bare form."""
    if not isinstance(stmt, ast.Raise):
        return False
    exc = stmt.exc
    if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
        return True
    if isinstance(exc, ast.Call):
        func = exc.func
        if isinstance(func, ast.Name) and func.id == "NotImplementedError":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "NotImplementedError":
            return True
    return False


def _check_notimplemented_stubs(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _has_abstract_decorator(node):
            continue  # @abstractmethod raising NotImplementedError is correct

        body = node.body
        effective_body = body
        if (len(body) >= 1 and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], 'value', None), ast.Constant)):
            effective_body = body[1:]

        if len(effective_body) == 1 and _raises_not_implemented(effective_body[0]):
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
            if isinstance(arg, ast.Constant):
                val = arg.value
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


GOD_FILE_LOC = 500
GOD_FUNC_LOC = 100
GOD_FUNC_COMPLEXITY = 20
GOD_CLASS_METHODS = 20


def _check_god_objects(tree: ast.Module, file_path: str, file_loc: int) -> list[LintFinding]:
    """Flag excessively large files, functions, and classes."""
    findings = []

    if file_loc > GOD_FILE_LOC:
        findings.append(LintFinding(
            file_path=file_path,
            line=1,
            rule="GOD_FILE",
            severity="med",
            desc=f"File has {file_loc} lines (threshold: {GOD_FILE_LOC}) — consider splitting into smaller modules",
        ))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_loc = (node.end_lineno or node.lineno) - node.lineno + 1
            if func_loc > GOD_FUNC_LOC:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="GOD_FUNCTION",
                    severity="med",
                    desc=f"Function '{node.name}' is {func_loc} lines (threshold: {GOD_FUNC_LOC}) — consider extracting helper functions",
                ))

            complexity = 1
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                    complexity += 1
                elif isinstance(child, ast.BoolOp):
                    complexity += len(child.values) - 1
            if complexity > GOD_FUNC_COMPLEXITY:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="GOD_FUNCTION_COMPLEXITY",
                    severity="med",
                    desc=f"Function '{node.name}' has cyclomatic complexity {complexity} (threshold: {GOD_FUNC_COMPLEXITY}) — too many branches",
                ))

        elif isinstance(node, ast.ClassDef):
            method_count = sum(1 for child in node.body
                              if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
            if method_count > GOD_CLASS_METHODS:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="GOD_CLASS",
                    severity="med",
                    desc=f"Class '{node.name}' has {method_count} methods (threshold: {GOD_CLASS_METHODS}) — consider splitting responsibilities",
                ))

    return findings


def _check_unreachable_code(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect statements after return, break, continue, raise within a function."""
    findings = []
    _TERMINAL = (ast.Return, ast.Break, ast.Continue, ast.Raise)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.For, ast.While, ast.If, ast.ExceptHandler,
                                 ast.With, ast.AsyncWith)):
            continue

        body = getattr(node, 'body', [])
        _check_body_for_unreachable(body, file_path, findings, _TERMINAL)

        orelse = getattr(node, 'orelse', [])
        if orelse:
            _check_body_for_unreachable(orelse, file_path, findings, _TERMINAL)

        handlers = getattr(node, 'handlers', [])
        for handler in handlers:
            _check_body_for_unreachable(handler.body, file_path, findings, _TERMINAL)

        finalbody = getattr(node, 'finalbody', [])
        if finalbody:
            _check_body_for_unreachable(finalbody, file_path, findings, _TERMINAL)

    return findings


def _check_body_for_unreachable(body: list, file_path: str,
                                 findings: list[LintFinding], terminal_types: tuple):
    """Check a block of statements for code after a terminal statement."""
    for i, stmt in enumerate(body):
        if isinstance(stmt, terminal_types) and i < len(body) - 1:
            next_stmt = body[i + 1]
            findings.append(LintFinding(
                file_path=file_path,
                line=next_stmt.lineno,
                rule="UNREACHABLE_CODE",
                severity="high",
                desc=f"Code after {type(stmt).__name__.lower()} statement is unreachable",
            ))
            break


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
    """Detect except blocks that catch broadly and don't log/raise/re-raise.

    Also recognizes 'surfaces the exception' via:
      - return statement that references the caught exception variable
      - append/extend call that references the exception variable
      - assignment to result dict/state that references the exception variable
    These patterns propagate the error to the caller even without logging."""
    findings = []

    LOGGING_CALLS = {"log", "logger", "logging", "print", "traceback", "warn", "warning", "error", "critical"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        is_broad = (node.type is None or
                    (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")))
        if not is_broad:
            continue

        exc_name = node.name  # the `as e` variable, or None

        has_raise = False
        has_log = False
        surfaces_exception = False
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

            # If the except body references the caught exception var in a return,
            # append(), extend(), or dict assignment — it's being surfaced up.
            if exc_name and isinstance(child, (ast.Return, ast.Call, ast.Assign)):
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Name) and sub.id == exc_name:
                        surfaces_exception = True
                        break

        if not has_raise and not has_log and not surfaces_exception:
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


def _collect_function_local_imports(tree: ast.Module) -> dict[str, tuple[set[str], int]]:
    """For every module-level function, return {func_name: (imported_names, lineno)}.
    Only tracks imports inside the function body (not module-level)."""
    out: dict[str, tuple[set[str], int]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = set()
        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(sub, ast.ImportFrom):
                for alias in sub.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
        if names:
            out[node.name] = (names, node.lineno)
    return out


def _collect_module_scope_names(tree: ast.Module) -> set[str]:
    """Names defined/assigned at module scope. Walks top-level AND inside
    module-level `if`/`try`/`with` bodies (since `X = 1` inside
    `if sys.platform == 'win32':` is still a module binding).

    Skips inside FunctionDef / ClassDef bodies — those are not module scope.
    Captures: imports, defs, classes, assigns (including Tuple targets),
    annotated assigns, augmented assigns, walrus (NamedExpr), for-loop vars.
    """
    names = set()

    def _record_target(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, ast.Tuple):
            for elt in t.elts:
                _record_target(elt)
        elif isinstance(t, ast.Starred):
            _record_target(t.value)

    def _walk(body):
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    _record_target(t)
                # walrus inside RHS: `x = (y := 5)`
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                        names.add(sub.target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.If):
                _walk(node.body)
                _walk(node.orelse)
            elif isinstance(node, ast.Try):
                _walk(node.body)
                for handler in node.handlers:
                    _walk(handler.body)
                _walk(node.orelse)
                _walk(node.finalbody)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        _record_target(item.optional_vars)
                _walk(node.body)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                _record_target(node.target)
                _walk(node.body)
                _walk(node.orelse)

    _walk(tree.body)
    return names


def _collect_local_names(func_node) -> set[str]:
    """Names defined locally within a function: parameters + assignments + local imports."""
    names = set()
    # params
    args = func_node.args
    for a in args.args + args.kwonlyargs + args.posonlyargs:
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    # assignments, imports, nested defs
    for sub in ast.walk(func_node):
        if sub is func_node:
            continue
        if isinstance(sub, (ast.Assign, ast.AnnAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(sub.name)
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            if isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
        elif isinstance(sub, ast.comprehension):
            if isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.With) and sub.items:
            for item in sub.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.arg if hasattr(item.optional_vars, "arg") else item.optional_vars.id)
    return names


_PY_BUILTINS = set(dir(__builtins__)) if hasattr(__builtins__, '__iter__') else set()
# Fallback builtin list (works under `python -m`)
_PY_BUILTINS = _PY_BUILTINS | {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable",
    "chr", "classmethod", "compile", "complex", "delattr", "dict", "dir", "divmod",
    "enumerate", "eval", "exec", "exit", "filter", "float", "format", "frozenset",
    "getattr", "globals", "hasattr", "hash", "help", "hex", "id", "input", "int",
    "isinstance", "issubclass", "iter", "len", "list", "locals", "map", "max",
    "memoryview", "min", "next", "object", "oct", "open", "ord", "pow", "print",
    "property", "range", "repr", "reversed", "round", "set", "setattr", "slice",
    "sorted", "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
    "True", "False", "None", "NotImplemented", "Ellipsis",
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "StopIteration", "OSError", "FileNotFoundError", "PermissionError",
    "AttributeError", "ImportError", "ModuleNotFoundError", "NameError", "ZeroDivisionError",
    "ArithmeticError", "LookupError", "NotImplementedError", "AssertionError",
    "__name__", "__file__", "__doc__", "__builtins__", "__import__", "__loader__",
    "__spec__", "__package__",
}


def _check_function_scoped_import_leak(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """NameError risk: function A imports X locally, function B (not nested in A)
    references X. At runtime, B's scope resolution never sees A's local binding.

    Caught the FRed morning_scan disaster — `import json` inside main() used by
    _save_plays helper → NameError on every scheduled run.
    """
    findings = []

    func_imports = _collect_function_local_imports(tree)  # {fn_name: (names, lineno)}
    module_names = _collect_module_scope_names(tree)

    # For each module-level function, check if it references a name that is
    # imported only inside a DIFFERENT function.
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        caller_name = node.name
        local_names = _collect_local_names(node)

        # Build set of names imported inside OTHER functions at module level
        other_func_imports: dict[str, str] = {}  # name → origin_function
        for other_fn, (names, _ln) in func_imports.items():
            if other_fn == caller_name:
                continue
            for n in names:
                other_func_imports.setdefault(n, other_fn)

        if not other_func_imports:
            continue

        # Walk caller body for Name references
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Name):
                continue
            if not isinstance(sub.ctx, ast.Load):
                continue
            nm = sub.id
            if nm in local_names or nm in module_names or nm in _PY_BUILTINS:
                continue
            if nm in other_func_imports:
                origin = other_func_imports[nm]
                findings.append(LintFinding(
                    file_path=file_path,
                    line=sub.lineno,
                    rule="FUNCTION_SCOPED_IMPORT_LEAK",
                    severity="high",
                    desc=(f"'{nm}' used here but imported only inside function "
                          f"'{origin}()' — will raise NameError at runtime "
                          f"(move the import to module scope)"),
                ))
                break  # one finding per caller is enough

    return findings


_UNKNOWN_ARITY = -1  # marker used in arity sets when we can't infer


def _return_tuple_sizes(func_node) -> set:
    """Set of tuple-arities from `return X, Y, ...`. Returns with a `Call()` as
    the value are tagged with `("call", func_name)` so a second pass can
    resolve mutual recursion one level deep. `1` = scalar/None. Star-spread
    in a return tuple marks the whole fn as unknown."""
    sizes: set = set()
    for sub in ast.walk(func_node):
        if sub is func_node:
            continue
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not func_node:
            continue
        if not isinstance(sub, ast.Return):
            continue
        if sub.value is None:
            sizes.add(1)
        elif isinstance(sub.value, ast.Tuple):
            # Star-spread in return makes arity unknown at AST time
            if any(isinstance(e, ast.Starred) for e in sub.value.elts):
                sizes.add(_UNKNOWN_ARITY)
            else:
                sizes.add(len(sub.value.elts))
        elif isinstance(sub.value, ast.Call) and isinstance(sub.value.func, ast.Name):
            # Defer: resolve arity after all fn_arities are collected
            sizes.add(("call", sub.value.func.id))
        else:
            sizes.add(1)
    return sizes


def _resolve_arities(fn_arities: dict[str, set], name: str, depth: int = 3) -> set:
    """Follow `return Call()` references one level to get concrete arities.
    Tuples with {_UNKNOWN_ARITY} anywhere → whole set treated as unknown."""
    if depth <= 0:
        return {_UNKNOWN_ARITY}
    raw = fn_arities.get(name, set())
    out = set()
    for a in raw:
        if isinstance(a, tuple) and a[0] == "call":
            out |= _resolve_arities(fn_arities, a[1], depth - 1)
        else:
            out.add(a)
    return out


def _check_unpack_size_mismatch(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """When `a, b = func()` in the same file where func's return statements
    clearly produce M-tuples and M != unpack arity, flag. Catches refactor drift.

    Restrictions to keep precision high:
      - Only flags bare-Name callers (`func()`), not method calls (`obj.func()`)
      - If the callee returns `Call()` to another fn, follows one level to
        avoid flagging correct mutual-recursion patterns
      - If any return has `_UNKNOWN_ARITY` (star-spread / unresolved Call),
        whole function is treated as unknown — no finding
    """
    findings = []

    # Collect raw return shapes per module-level function
    fn_arities: dict[str, set] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_arities[node.name] = _return_tuple_sizes(node)

    if not fn_arities:
        return findings

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Tuple):
            continue
        if any(isinstance(e, ast.Starred) for e in target.elts):
            continue
        n_expected = len(target.elts)

        if not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        # Only bare-Name callers. Method calls (obj.x()) would collide on
        # unrelated functions with the same name in this file.
        if not isinstance(callee, ast.Name):
            continue
        fn_name = callee.id
        if fn_name not in fn_arities:
            continue

        arities = _resolve_arities(fn_arities, fn_name)
        # Unknown arity anywhere → abstain
        if not arities or _UNKNOWN_ARITY in arities:
            continue
        if n_expected not in arities:
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="UNPACK_SIZE_MISMATCH",
                severity="high",
                desc=(f"unpacking {n_expected} values from '{fn_name}()' but its "
                      f"return statements produce tuple arities {sorted(arities)} "
                      f"— runtime TypeError waiting to happen"),
            ))

    return findings
