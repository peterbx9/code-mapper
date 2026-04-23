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
        file_findings.extend(_check_unsatisfiable_conditions(tree, rel))
        file_findings.extend(_check_type_mismatch_heuristics(tree, rel))
        file_findings.extend(_check_redundant_abstractions(tree, rel))
        file_findings.extend(_check_taint_risks(tree, rel))
        file_findings.extend(_check_mutable_default_arg(tree, rel))
        file_findings.extend(_check_fn_call_in_default_arg(tree, rel))
        file_findings.extend(_check_zip_without_strict(tree, rel))
        file_findings.extend(_check_compare_to_none_with_eq(tree, rel))
        file_findings.extend(_check_compare_to_bool_with_eq(tree, rel))
        file_findings.extend(_check_bare_except(tree, rel))
        file_findings.extend(_check_raise_without_from(tree, rel))
        file_findings.extend(_check_return_break_in_finally(tree, rel))
        file_findings.extend(_check_datetime_now_no_tz(tree, rel))
        file_findings.extend(_check_mutate_loop_iterable(tree, rel))
        file_findings.extend(_check_open_without_encoding(tree, rel))
        file_findings.extend(_check_subprocess_no_returncode_check(tree, rel))
        file_findings.extend(_check_assert_in_non_test(tree, rel))
        file_findings.extend(_check_sys_exit_in_library(tree, rel))
        file_findings.extend(_check_f_string_in_logging(tree, rel))
        file_findings.extend(_check_multi_stmt_pytest_raises(tree, rel))
        file_findings.extend(_check_blocking_io_in_async(tree, rel))
        file_findings.extend(_check_fn_in_loop_late_binding(tree, rel))
        file_findings.extend(_check_sync_orm_in_async_endpoint(tree, rel))
        file_findings.extend(_check_string_concat_in_loop(tree, rel))

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


def _check_unsatisfiable_conditions(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect if False:, while False:, if 0:, while 0: dead branches."""
    findings = []
    _FALSY = {False, 0, None, ""}

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            test = node.test
            if isinstance(test, ast.Constant) and test.value in _FALSY:
                kind = "if" if isinstance(node, ast.If) else "while"
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="UNSATISFIABLE_CONDITION",
                    severity="high",
                    desc=f"'{kind} {repr(test.value)}:' — condition is always false, body is dead code",
                ))
            elif isinstance(test, ast.NameConstant) and test.value in _FALSY:
                kind = "if" if isinstance(node, ast.If) else "while"
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="UNSATISFIABLE_CONDITION",
                    severity="high",
                    desc=f"'{kind} {repr(test.value)}:' — condition is always false, body is dead code",
                ))

    return findings


def _check_type_mismatch_heuristics(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect likely type mismatches without full type checking."""
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        returns = []
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and child.value is not None:
                returns.append(child)

        if len(returns) < 2:
            continue

        return_types = set()
        for ret in returns:
            val = ret.value
            if val is None or (isinstance(val, ast.Constant) and val.value is None):
                return_types.add("None")
            elif isinstance(val, ast.Dict):
                return_types.add("dict")
            elif isinstance(val, ast.List):
                return_types.add("list")
            elif isinstance(val, (ast.Tuple, ast.Set)):
                return_types.add(type(val).__name__.lower())
            elif isinstance(val, ast.Constant):
                return_types.add(type(val.value).__name__)
            elif isinstance(val, ast.Call):
                return_types.add("call")
            else:
                return_types.add("expr")

        concrete = return_types - {"call", "expr"}
        if "None" in concrete and len(concrete) > 1:
            other = concrete - {"None"}
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="MIXED_RETURN_TYPES",
                severity="med",
                desc=f"Function '{node.name}' returns both None and {'/'.join(other)} in different branches — callers may not handle None",
            ))

    return findings


_FRAMEWORK_BASE_CLASSES = {
    "Base", "DeclarativeBase", "BaseModel", "Model",
    "Middleware", "BaseHTTPMiddleware",
    "Exception", "BaseException", "Error", "Warning",
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
    "TypedDict", "NamedTuple", "Protocol", "Generic",
    "TestCase", "IsolatedAsyncioTestCase",
    "HTMLParser",
}


def _has_framework_base(class_node: ast.ClassDef) -> bool:
    """Return True if class inherits from a framework base that requires class-form."""
    for base in class_node.bases:
        name = None
        if isinstance(base, ast.Name):
            name = base.id
        elif isinstance(base, ast.Attribute):
            name = base.attr
        if name and (name in _FRAMEWORK_BASE_CLASSES
                     or name.endswith("Base") or name.endswith("Middleware")
                     or name.endswith("Error") or name.endswith("Exception")):
            return True
    return False


def _is_orm_model(class_node: ast.ClassDef) -> bool:
    """Heuristic: class with __tablename__ or Column() at class-body level."""
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    return True
            if isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if fname in ("Column", "mapped_column", "relationship"):
                    return True
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if fname in ("Column", "mapped_column", "relationship"):
                return True
    return False


_DATA_DECORATORS = {"dataclass", "attrs", "define", "frozen", "attr.s"}


def _is_data_class(class_node: ast.ClassDef) -> bool:
    for dec in class_node.decorator_list:
        name = None
        if isinstance(dec, ast.Name):
            name = dec.id
        elif isinstance(dec, ast.Attribute):
            name = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                name = dec.func.id
            elif isinstance(dec.func, ast.Attribute):
                name = dec.func.attr
        if name in _DATA_DECORATORS:
            return True
    return False


def _check_redundant_abstractions(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Detect classes with only 1 public method (should be a function).
    Skips framework-required class forms: ORM models, middleware, exceptions, enums, protocols,
    and @dataclass / @attrs classes (which exist for auto-generated __init__/__repr__/__eq__)."""
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        if _has_framework_base(node) or _is_orm_model(node) or _is_data_class(node):
            continue

        methods = [n for n in node.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        public_methods = [m for m in methods if not m.name.startswith("_") or m.name == "__init__"]
        non_init = [m for m in public_methods if m.name != "__init__"]

        if len(non_init) == 1 and len(methods) <= 2:
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="REDUNDANT_CLASS",
                severity="low",
                desc=f"Class '{node.name}' has only 1 public method '{non_init[0].name}' — consider using a plain function instead",
            ))

    return findings


DANGEROUS_SINKS = {
    "os.system", "os.popen", "subprocess.call", "subprocess.run",
    "subprocess.Popen", "subprocess.check_output", "subprocess.check_call",
    "eval", "exec", "compile",
}

DANGEROUS_ATTRS = {"system", "popen", "call", "run", "Popen", "check_output", "check_call"}


def _check_taint_risks(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Simplified taint tracking: flag dangerous sinks called with non-literal args."""
    findings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        func_name = None

        if isinstance(func, ast.Name):
            if func.id in ("eval", "exec", "compile"):
                func_name = func.id
        elif isinstance(func, ast.Attribute):
            if func.attr in DANGEROUS_ATTRS:
                if isinstance(func.value, ast.Name):
                    func_name = f"{func.value.id}.{func.attr}"
                else:
                    func_name = func.attr

        if not func_name:
            continue

        if node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant):
                continue
            if isinstance(first_arg, ast.JoinedStr):
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="TAINT_FSTRING_IN_SINK",
                    severity="high",
                    desc=f"f-string passed to {func_name}() — potential command/code injection",
                ))
            elif isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, (ast.Add, ast.Mod)):
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="TAINT_CONCAT_IN_SINK",
                    severity="high",
                    desc=f"String concatenation/format passed to {func_name}() — potential injection",
                ))
            elif isinstance(first_arg, ast.Name):
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="TAINT_VARIABLE_IN_SINK",
                    severity="med",
                    desc=f"Variable '{first_arg.id}' passed to {func_name}() — verify input is sanitized",
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

        is_broad = (isinstance(node.type, ast.Name)
                    and node.type.id in ("Exception", "BaseException"))
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


def _build_parent_map(tree: ast.Module) -> dict:
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


_EMPTY_CONSTRUCTORS = {"list", "dict", "set"}


def _check_mutable_default_arg(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        for default in defaults:
            is_mutable = False
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                is_mutable = True
            elif (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                    and default.func.id in _EMPTY_CONSTRUCTORS
                    and not default.args and not default.keywords):
                is_mutable = True
            if is_mutable:
                findings.append(LintFinding(
                    file_path=file_path,
                    line=default.lineno,
                    rule="MUTABLE_DEFAULT_ARG",
                    severity="high",
                    desc=f"Mutable default argument in '{node.name}()' — shared across all calls (classic Python foot-gun)",
                ))
    return findings


_FRAMEWORK_MARKERS = {
    "Depends", "Query", "Path", "Body", "Header", "Cookie", "File", "Form",
    "Security", "Param", "Field", "Provide",
}


def _check_fn_call_in_default_arg(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag func calls in default args (eval'd once at def time). Skip empty list/dict/set (MUTABLE_DEFAULT_ARG)
    and framework markers like FastAPI's Depends/Query/Body (re-evaluated per-request by the framework)."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        for default in defaults:
            if not isinstance(default, ast.Call):
                continue
            if (isinstance(default.func, ast.Name)
                    and default.func.id in _EMPTY_CONSTRUCTORS
                    and not default.args and not default.keywords):
                continue
            if isinstance(default.func, ast.Name) and default.func.id in _FRAMEWORK_MARKERS:
                continue
            if isinstance(default.func, ast.Attribute) and default.func.attr in _FRAMEWORK_MARKERS:
                continue
            call_desc = _describe_call(default)
            findings.append(LintFinding(
                file_path=file_path,
                line=default.lineno,
                rule="FN_CALL_IN_DEFAULT_ARG",
                severity="med",
                desc=f"Default argument in '{node.name}()' calls '{call_desc}' — evaluated once at def time, frozen for all calls",
            ))
    return findings


def _describe_call(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return f"{func.id}()"
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{func.attr}()"
        return f"<expr>.{func.attr}()"
    return "<call>"


def _check_zip_without_strict(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "zip"):
            continue
        if len(node.args) < 2:
            continue
        has_strict = any(kw.arg == "strict" for kw in node.keywords)
        if has_strict:
            continue
        findings.append(LintFinding(
            file_path=file_path,
            line=node.lineno,
            rule="ZIP_WITHOUT_STRICT",
            severity="med",
            desc="zip() without strict= kwarg — silently truncates at shortest iterable",
        ))
    return findings


def _check_compare_to_none_with_eq(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if isinstance(comparator, ast.Constant) and comparator.value is None:
                op_sym = "==" if isinstance(op, ast.Eq) else "!="
                replacement = "is None" if isinstance(op, ast.Eq) else "is not None"
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="COMPARE_TO_NONE_WITH_EQ",
                    severity="low",
                    desc=f"'{op_sym} None' — use '{replacement}' (safer against overloaded __eq__)",
                ))
    return findings


def _check_compare_to_bool_with_eq(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, bool):
                op_sym = "==" if isinstance(op, ast.Eq) else "!="
                findings.append(LintFinding(
                    file_path=file_path,
                    line=node.lineno,
                    rule="COMPARE_TO_BOOL_WITH_EQ",
                    severity="low",
                    desc=f"'{op_sym} {comparator.value}' — use truthy check or 'is'/'is not' (breaks on truthy non-bools)",
                ))
    return findings


def _check_bare_except(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            findings.append(LintFinding(
                file_path=file_path,
                line=node.lineno,
                rule="BARE_EXCEPT",
                severity="high",
                desc="'except:' with no class — swallows KeyboardInterrupt / SystemExit; use 'except Exception:' instead",
            ))
    return findings


def _check_raise_without_from(tree: ast.Module, file_path: str) -> list[LintFinding]:
    findings = []
    parents = _build_parent_map(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        if node.exc is None or node.cause is not None:
            continue
        current = parents.get(id(node))
        in_except = False
        while current is not None:
            if isinstance(current, ast.ExceptHandler):
                in_except = True
                break
            current = parents.get(id(current))
        if not in_except:
            continue
        findings.append(LintFinding(
            file_path=file_path,
            line=node.lineno,
            rule="RAISE_WITHOUT_FROM",
            severity="med",
            desc="raise inside except without 'from err' / 'from None' — loses original traceback",
        ))
    return findings


def _check_return_break_in_finally(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag return/break/continue inside finally: that would exit the finally block and
    silently swallow any active exception. Respects nested function and nested-loop scopes."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.finalbody:
            _scan_finally_body(stmt, file_path, findings, in_loop=False, in_func=False)
    return findings


def _scan_finally_body(node, file_path, findings, in_loop, in_func):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
        return
    if isinstance(node, ast.Return) and not in_func:
        findings.append(LintFinding(
            file_path=file_path, line=node.lineno,
            rule="RETURN_BREAK_IN_FINALLY", severity="med",
            desc="'return' inside finally: silently swallows any active exception",
        ))
    elif isinstance(node, (ast.Break, ast.Continue)) and not in_loop and not in_func:
        kind = type(node).__name__.lower()
        findings.append(LintFinding(
            file_path=file_path, line=node.lineno,
            rule="RETURN_BREAK_IN_FINALLY", severity="med",
            desc=f"'{kind}' inside finally: silently swallows any active exception",
        ))
    next_in_loop = in_loop or isinstance(node, (ast.For, ast.While, ast.AsyncFor))
    for child in ast.iter_child_nodes(node):
        _scan_finally_body(child, file_path, findings, next_in_loop, in_func)


def _check_datetime_now_no_tz(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag datetime.now() / datetime.utcnow() / datetime.fromtimestamp() without tz.
    Accepts tz passed positionally OR as tz=/tzinfo= kwarg. utcnow() is always flagged
    (deprecated in 3.12, always naive)."""
    findings = []
    # now: tz at position 0; fromtimestamp: tz at position 1; utcnow: no tz arg at all.
    POSITIONAL_TZ_INDEX = {"now": 0, "fromtimestamp": 1}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        method = func.attr
        if method not in ("now", "utcnow", "fromtimestamp"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id in ("datetime", "dt")):
            continue
        has_tz_kwarg = any(kw.arg in ("tz", "tzinfo") for kw in node.keywords)
        pos_tz_idx = POSITIONAL_TZ_INDEX.get(method)
        has_positional_tz = pos_tz_idx is not None and len(node.args) > pos_tz_idx
        if has_tz_kwarg or has_positional_tz:
            continue
        suffix = "deprecated in 3.12, always naive" if method == "utcnow" else "returns naive datetime (TZ bugs on cross-system data)"
        findings.append(LintFinding(
            file_path=file_path,
            line=node.lineno,
            rule="DATETIME_NOW_NO_TZ",
            severity="med",
            desc=f"datetime.{method}() without tz — {suffix}",
        ))
    return findings


_LOOP_MUTATORS = {"append", "extend", "insert", "remove", "pop", "clear",
                  "update", "popitem", "setdefault", "__setitem__", "__delitem__"}


def _check_mutate_loop_iterable(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag mutations of the loop iterable inside its own for-loop body."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        iter_name = _loop_iter_name(node.iter)
        if not iter_name:
            continue
        seen = set()
        for child in ast.walk(node):
            if child is node:
                continue
            line = getattr(child, "lineno", None)
            if line in seen:
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if (isinstance(func, ast.Attribute) and func.attr in _LOOP_MUTATORS
                        and isinstance(func.value, ast.Name) and func.value.id == iter_name):
                    findings.append(LintFinding(
                        file_path=file_path, line=line,
                        rule="MUTATE_LOOP_ITERABLE", severity="med",
                        desc=f"Mutating '{iter_name}.{func.attr}()' inside its own for-loop — skips items or raises",
                    ))
                    seen.add(line)
            elif isinstance(child, ast.Delete):
                for target in child.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == iter_name):
                        findings.append(LintFinding(
                            file_path=file_path, line=line,
                            rule="MUTATE_LOOP_ITERABLE", severity="med",
                            desc=f"'del {iter_name}[...]' inside its own for-loop — skips items or raises",
                        ))
                        seen.add(line)
                        break
    return findings


def _loop_iter_name(iter_node) -> Optional[str]:
    if isinstance(iter_node, ast.Name):
        return iter_node.id
    if isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Attribute):
        if iter_node.func.attr in ("items", "keys", "values"):
            if isinstance(iter_node.func.value, ast.Name):
                return iter_node.func.value.id
    return None


_OPEN_MODULE_PREFIXES = {"io", "codecs", "pathlib"}


def _check_open_without_encoding(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag open()/io.open()/codecs.open() text-mode calls missing encoding= kwarg.
    Silently skipped for binary mode ('b' in mode string)."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_open = False
        if isinstance(func, ast.Name) and func.id == "open":
            is_open = True
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            if isinstance(func.value, ast.Name) and func.value.id in _OPEN_MODULE_PREFIXES:
                is_open = True
        if not is_open:
            continue
        mode = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        else:
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
                    break
        if isinstance(mode, str) and "b" in mode:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        findings.append(LintFinding(
            file_path=file_path, line=node.lineno,
            rule="OPEN_WITHOUT_ENCODING", severity="low",
            desc="open() in text mode without encoding= — silent Windows/Linux divergence",
        ))
    return findings


_SUBPROCESS_FNS = {"run", "Popen", "call"}


def _check_subprocess_no_returncode_check(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag subprocess.run/Popen/call where return value is discarded AND check=True is not set,
    OR the return value is assigned but .returncode/.check_returncode() is never accessed
    in the enclosing function."""
    findings = []
    parents = _build_parent_map(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_FNS):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        has_check_true = any(
            kw.arg == "check" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if has_check_true:
            continue
        parent = parents.get(id(node))
        target_name = None
        is_discarded = False
        if isinstance(parent, ast.Expr):
            is_discarded = True
        elif isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
            target_name = parent.targets[0].id
        else:
            continue
        if is_discarded:
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="SUBPROCESS_NO_RETURNCODE_CHECK", severity="high",
                desc=f"subprocess.{func.attr}() return value discarded — child exit status ignored; pass check=True or inspect .returncode",
            ))
            continue
        enclosing = parent
        while enclosing is not None and not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            enclosing = parents.get(id(enclosing))
        if enclosing is None:
            continue
        referenced = False
        for sub in ast.walk(enclosing):
            if isinstance(sub, ast.Attribute) and sub.attr in ("returncode", "check_returncode"):
                if isinstance(sub.value, ast.Name) and sub.value.id == target_name:
                    referenced = True
                    break
        if not referenced:
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="SUBPROCESS_NO_RETURNCODE_CHECK", severity="high",
                desc=f"subprocess.{func.attr}() assigned to '{target_name}' but '.returncode' / '.check_returncode()' never accessed",
            ))
    return findings


def _walk_same_scope(fn_node):
    """Walk descendants of fn_node but do NOT descend into nested
    FunctionDef / AsyncFunctionDef / Lambda bodies."""
    stack = list(ast.iter_child_nodes(fn_node))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _is_test_file(file_path: str) -> bool:
    parts = file_path.replace("\\", "/").split("/")
    name = parts[-1]
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(p in ("tests", "test") for p in parts[:-1])


def _check_assert_in_non_test(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag `assert` outside tests/ — stripped under `python -O`."""
    if _is_test_file(file_path):
        return []
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="ASSERT_IN_NON_TEST", severity="med",
                desc="assert outside tests/ — stripped under 'python -O'; use explicit raise instead",
            ))
    return findings


def _has_main_guard(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        if (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.left, ast.Name) and test.left.id == "__name__"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"):
            return True
    return False


_ENTRY_POINT_NAMES = {"__main__.py", "cli.py", "main.py", "run.py", "manage.py", "app.py"}


def _is_entry_point_file(file_path: str, tree: ast.Module) -> bool:
    name = file_path.replace("\\", "/").split("/")[-1]
    if name in _ENTRY_POINT_NAMES or name.endswith("_cli.py") or name.endswith("-cli.py"):
        return True
    return _has_main_guard(tree)


def _check_sys_exit_in_library(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag sys.exit()/exit()/quit() calls in library-style modules. Files that are
    entry points (have __main__ guard, or named cli.py / main.py / __main__.py /
    *_cli.py / run.py / manage.py / app.py) are exempt — sys.exit() there is legit."""
    if _is_entry_point_file(file_path, tree):
        return []
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call_name = None
        if isinstance(func, ast.Name) and func.id in ("exit", "quit"):
            call_name = func.id
        elif (isinstance(func, ast.Attribute) and func.attr == "exit"
              and isinstance(func.value, ast.Name) and func.value.id == "sys"):
            call_name = "sys.exit"
        if call_name:
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="SYS_EXIT_IN_LIBRARY", severity="med",
                desc=f"{call_name}() in a library-style module — kills host process if imported",
            ))
    return findings


_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}


def _check_f_string_in_logging(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag `logger.info(f"...")` — breaks lazy formatting; eager-evaluates even when
    log level filters the message."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.JoinedStr):
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="F_STRING_IN_LOGGING", severity="low",
                desc=f".{func.attr}(f\"...\") — breaks lazy formatting; use .{func.attr}('... %s ...', arg)",
            ))
    return findings


def _check_multi_stmt_pytest_raises(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag `with pytest.raises(X): stmt1; stmt2` — can't tell which statement raised."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        has_pytest_raises = False
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call):
                f = expr.func
                if (isinstance(f, ast.Attribute) and f.attr == "raises"
                        and isinstance(f.value, ast.Name) and f.value.id == "pytest"):
                    has_pytest_raises = True
                    break
        if not has_pytest_raises:
            continue
        body = [s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), (ast.Constant,)))]
        if len(body) > 1:
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="MULTI_STMT_PYTEST_RAISES", severity="low",
                desc="with pytest.raises(): body has multiple statements — can't tell which raised; split the with",
            ))
    return findings


_BLOCKING_IN_ASYNC = {
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "requests.patch", "requests.head", "requests.options", "requests.request",
    "urllib.request.urlopen",
    "time.sleep",
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.Popen",
}


def _check_blocking_io_in_async(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag known blocking I/O calls inside async def (event-loop killers)."""
    findings = []
    for afn in ast.walk(tree):
        if not isinstance(afn, ast.AsyncFunctionDef):
            continue
        for node in _walk_same_scope(afn):
            if not isinstance(node, ast.Call):
                continue
            fq = _qualified_call(node.func)
            if fq in _BLOCKING_IN_ASYNC:
                findings.append(LintFinding(
                    file_path=file_path, line=node.lineno,
                    rule="BLOCKING_IO_IN_ASYNC", severity="med",
                    desc=f"{fq}() inside async def '{afn.name}' — blocks the event loop; use an async equivalent",
                ))
    return findings


def _qualified_call(func_node) -> Optional[str]:
    """Return 'module.attr' or 'module.sub.attr' for Attribute chains; None otherwise."""
    if isinstance(func_node, ast.Attribute):
        parts = [func_node.attr]
        cur = func_node.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def _extract_target_names(target) -> set:
    if isinstance(target, ast.Name):
        return {target.id}
    names = set()
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names.update(_extract_target_names(elt))
    return names


def _check_fn_in_loop_late_binding(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag lambda / nested def inside a for/while that references the loop variable
    without capturing it as a default arg (classic late-binding closure trap).
    Skipped if the name is captured via default (lambda x=x: ...) or locally rebound."""
    findings = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor)):
            continue
        target_names = _extract_target_names(loop.target)
        if not target_names:
            continue
        for inner in ast.walk(loop):
            if inner is loop:
                continue
            if not isinstance(inner, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            captured = {a.arg for a in inner.args.args}
            captured.update(a.arg for a in inner.args.kwonlyargs)
            captured.update(a.arg for a in getattr(inner.args, "posonlyargs", []))
            rebound = set()
            for sub in ast.walk(inner):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    rebound.add(sub.id)
            uncaptured = set()
            for sub in ast.walk(inner):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id in target_names and sub.id not in captured and sub.id not in rebound:
                        uncaptured.add(sub.id)
            if uncaptured:
                kind = "lambda" if isinstance(inner, ast.Lambda) else type(inner).__name__
                findings.append(LintFinding(
                    file_path=file_path, line=inner.lineno,
                    rule="FN_IN_LOOP_LATE_BINDING", severity="med",
                    desc=f"{kind} in loop closes over {sorted(uncaptured)} without default-arg capture — all calls see the LAST value",
                ))
    return findings


_ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "options", "head"}
_SYNC_ORM_METHODS = {"query", "commit", "rollback", "flush", "refresh"}


def _is_route_handler(fn_node) -> bool:
    for dec in fn_node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS:
            return True
    return False


def _async_session_param_names(fn_node) -> set:
    """Return parameter names whose type annotation mentions 'Async' (AsyncSession, AsyncEngine, etc.).
    Accepts plain name, Subscript (Annotated[...]), Attribute, and str forward-refs."""
    names = set()
    all_args = (list(fn_node.args.args) + list(fn_node.args.kwonlyargs)
                + list(getattr(fn_node.args, "posonlyargs", [])))
    for arg in all_args:
        ann = arg.annotation
        if ann is None:
            continue
        ann_str = None
        if isinstance(ann, ast.Name):
            ann_str = ann.id
        elif isinstance(ann, ast.Attribute):
            ann_str = ann.attr
        elif isinstance(ann, ast.Subscript):
            val = ann.value
            ann_str = val.id if isinstance(val, ast.Name) else getattr(val, "attr", None)
        elif isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            ann_str = ann.value
        if ann_str and "Async" in ann_str:
            names.add(arg.arg)
    return names


def _check_sync_orm_in_async_endpoint(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag un-awaited ORM ops on AsyncSession-annotated params inside async FastAPI routes.
    Only fires when the param's annotation contains 'Async' — sync Session with async def
    is a valid (though event-loop-blocking) FastAPI pattern and NOT flagged here."""
    findings = []
    parents = _build_parent_map(tree)
    for afn in ast.walk(tree):
        if not isinstance(afn, ast.AsyncFunctionDef) or not _is_route_handler(afn):
            continue
        async_params = _async_session_param_names(afn)
        if not async_params:
            continue
        for node in _walk_same_scope(afn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in _SYNC_ORM_METHODS):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id in async_params):
                continue
            if isinstance(parents.get(id(node)), ast.Await):
                continue
            findings.append(LintFinding(
                file_path=file_path, line=node.lineno,
                rule="SYNC_ORM_IN_ASYNC_ENDPOINT", severity="high",
                desc=f"unawaited '{func.value.id}.{func.attr}()' — '{func.value.id}' is AsyncSession-annotated; needs 'await'",
            ))
    return findings


def _check_string_concat_in_loop(tree: ast.Module, file_path: str) -> list[LintFinding]:
    """Flag `s += x` inside a for/while where `s` is likely a str (O(n²))."""
    findings = []
    parents = _build_parent_map(tree)
    str_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    val = node.value
                    if (isinstance(val, ast.Constant) and isinstance(val.value, str)) \
                            or isinstance(val, ast.JoinedStr):
                        str_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann = node.annotation
            if isinstance(ann, ast.Name) and ann.id == "str":
                str_names.add(node.target.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign) or not isinstance(node.op, ast.Add):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id not in str_names:
            continue
        current = parents.get(id(node))
        in_loop = False
        while current is not None:
            if isinstance(current, (ast.For, ast.While, ast.AsyncFor)):
                in_loop = True
                break
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                break
            current = parents.get(id(current))
        if not in_loop:
            continue
        findings.append(LintFinding(
            file_path=file_path, line=node.lineno,
            rule="STRING_CONCAT_IN_LOOP", severity="low",
            desc=f"'{node.target.id} += ...' inside a loop where '{node.target.id}' is str — O(n²); use list.append() + ''.join() instead",
        ))
    return findings

