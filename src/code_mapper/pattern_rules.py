"""
User-extensible AST pattern rules loaded from .codemapper.json.

Rules are defined in the "rules" key of .codemapper.json:
{
  "rules": [
    {
      "id": "no-eval",
      "pattern": "eval(...)",
      "message": "eval() is dangerous — use ast.literal_eval() instead",
      "severity": "high"
    },
    {
      "id": "no-print-in-production",
      "pattern": "print(...)",
      "message": "Use logger instead of print()",
      "severity": "low",
      "exclude_files": ["*test*", "*cli*"]
    }
  ]
}

Pattern syntax (simplified Semgrep-like):
  "func(...)"         — any call to func with any args
  "module.func(...)"  — qualified call
  "$X = None"         — assignment to None
  "except:..."        — bare except (no exception type)
"""

import ast
import fnmatch
import logging
import re
from pathlib import Path

from .linter import LintFinding

logger = logging.getLogger(__name__)


def run_pattern_rules(project_root: Path, rules: list[dict],
                      exclude_dirs: set = None) -> list[LintFinding]:
    findings = []
    if not rules:
        return findings

    if exclude_dirs is None:
        exclude_dirs = set()

    compiled = [_compile_rule(r) for r in rules if _compile_rule(r)]

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

        for rule in compiled:
            if rule.get("exclude_files"):
                if any(fnmatch.fnmatch(rel, pat) for pat in rule["exclude_files"]):
                    continue

            matches = _match_pattern(tree, rule)
            for line in matches:
                findings.append(LintFinding(
                    file_path=rel,
                    line=line,
                    rule=f"PATTERN:{rule['id']}",
                    severity=rule.get("severity", "med"),
                    desc=rule.get("message", f"Pattern '{rule['pattern']}' matched"),
                ))

    return findings


def _compile_rule(rule: dict) -> dict | None:
    if "id" not in rule or "pattern" not in rule:
        logger.warning(f"Pattern rule missing 'id' or 'pattern': {rule}")
        return None

    pattern = rule["pattern"]
    matcher = _parse_pattern(pattern)
    if not matcher:
        logger.warning(f"Could not parse pattern: {pattern}")
        return None

    return {**rule, "_matcher": matcher}


def _parse_pattern(pattern: str) -> dict | None:
    pattern = pattern.strip()

    if pattern == "except:...":
        return {"type": "bare_except"}

    m = re.match(r"^([\w.]+)\(\.\.\.\)$", pattern)
    if m:
        func_name = m.group(1)
        return {"type": "call", "func": func_name}

    m = re.match(r"^\$\w+\s*=\s*(.+)$", pattern)
    if m:
        value_str = m.group(1).strip()
        return {"type": "assign_value", "value": value_str}

    m = re.match(r"^([\w.]+)\((.+)\)$", pattern)
    if m:
        func_name = m.group(1)
        arg_pattern = m.group(2).strip()
        return {"type": "call_with_arg", "func": func_name, "arg": arg_pattern}

    return {"type": "text_search", "text": pattern}


def _match_pattern(tree: ast.Module, rule: dict) -> list[int]:
    matcher = rule["_matcher"]
    matches = []

    if matcher["type"] == "call":
        target_func = matcher["func"]
        parts = target_func.split(".")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if len(parts) == 1:
                if isinstance(func, ast.Name) and func.id == parts[0]:
                    matches.append(node.lineno)
                elif isinstance(func, ast.Attribute) and func.attr == parts[0]:
                    matches.append(node.lineno)
            elif len(parts) == 2:
                if (isinstance(func, ast.Attribute) and func.attr == parts[1]
                        and isinstance(func.value, ast.Name) and func.value.id == parts[0]):
                    matches.append(node.lineno)

    elif matcher["type"] == "bare_except":
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                matches.append(node.lineno)

    elif matcher["type"] == "assign_value":
        target_val = matcher["value"]
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if isinstance(node.value, ast.Constant):
                    if str(node.value.value) == target_val or repr(node.value.value) == target_val:
                        matches.append(node.lineno)

    elif matcher["type"] == "call_with_arg":
        target_func = matcher["func"]
        parts = target_func.split(".")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_match = False
            if len(parts) == 1:
                if isinstance(func, ast.Name) and func.id == parts[0]:
                    func_match = True
            elif len(parts) == 2:
                if (isinstance(func, ast.Attribute) and func.attr == parts[1]
                        and isinstance(func.value, ast.Name) and func.value.id == parts[0]):
                    func_match = True
            if func_match:
                matches.append(node.lineno)

    elif matcher["type"] == "text_search":
        pass

    return matches
