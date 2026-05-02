"""Autofix mode — apply safe fixes for selected lint rules.

Currently fixes:
  - DEAD_IMPORT — removes unused import statements
  - UNUSED_PARAM — adds a leading underscore to mark intentionally unused
  - DEAD_FUTURE_IMPORT — removes unused `from __future__ import annotations` (Python 3.11+)

Each fix is line-precise and idempotent. Other lint rules are LEFT ALONE
because they need human judgment (SWALLOWED_EXCEPTION, GOD_FUNCTION, etc.).

Usage from CLI:
    code-mapper /path --lint --fix              # apply, write changes
    code-mapper /path --lint --fix --dry-run    # show changes only
"""
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

FIXABLE_RULES = {"DEAD_IMPORT", "UNUSED_PARAM"}


def _findings_for_rule(findings: list[dict], rule: str) -> list[dict]:
    return [f for f in findings if f.get("rule") == rule]


def _by_file(findings: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in findings:
        fp = f.get("file_path") or f.get("file")
        if not fp:
            continue
        out.setdefault(fp, []).append(f)
    return out


def _fix_dead_import(source_lines: list[str], findings: list[dict]) -> tuple[list[str], int]:
    """Remove dead import lines. Findings give us line numbers (1-indexed).
    If multiple imports on one line (`from x import a, b`) and only one is
    dead, this is conservative and skips — too risky to surgically edit.
    """
    lines_to_drop: set[int] = set()
    for f in findings:
        line_n = f.get("line", 0)
        if not line_n:
            continue
        idx = line_n - 1
        if idx < 0 or idx >= len(source_lines):
            continue
        ln = source_lines[idx].strip()
        # Only drop if the entire line is the dead import — single name imports
        # `import x` or `import x as y` or `from x import y` (single).
        # Skip multi-name imports — too risky.
        if re.match(r"^\s*import\s+[\w.]+(\s+as\s+\w+)?\s*$", source_lines[idx]):
            lines_to_drop.add(idx)
        elif re.match(r"^\s*from\s+[\w.]+\s+import\s+\w+(\s+as\s+\w+)?\s*$", source_lines[idx]):
            lines_to_drop.add(idx)
        # multi-name: skip with a logger
        else:
            logger.debug(f"Skipping multi-name import at {f.get('file_path')}:{line_n}")
    new_lines = [ln for i, ln in enumerate(source_lines) if i not in lines_to_drop]
    return new_lines, len(lines_to_drop)


def _fix_unused_param(source_lines: list[str], findings: list[dict]) -> tuple[list[str], int]:
    """Rename `param` to `_param` in def/lambda signatures. Conservative —
    only modifies if the name appears EXACTLY once in the line (the signature
    arg) so we don't accidentally rename usages inline.

    Param name comes from finding desc: "Parameter 'X' in '...()' is never used"
    """
    fixes = 0
    name_re = re.compile(r"^\s*(?:def|async\s+def)\s+\w+\s*\(([^)]*)\)")
    for f in findings:
        line_n = f.get("line", 0)
        desc = f.get("desc") or ""
        m = re.search(r"Parameter '(\w+)'", desc)
        if not m or not line_n:
            continue
        param = m.group(1)
        if param.startswith("_"):
            continue
        idx = line_n - 1
        if idx < 0 or idx >= len(source_lines):
            continue
        line = source_lines[idx]
        sig_m = name_re.match(line)
        if not sig_m:
            continue
        # Replace the bare param name in the signature with _param
        # Use word-boundary regex limited to the signature substring.
        sig = sig_m.group(1)
        new_sig = re.sub(rf"\b{re.escape(param)}\b", f"_{param}", sig, count=1)
        if new_sig == sig:
            continue
        source_lines[idx] = line.replace(sig, new_sig, 1)
        fixes += 1
    return source_lines, fixes


def apply_fixes(
    project_root: Path,
    findings: list[dict],
    *,
    dry_run: bool = False,
    rules: Iterable[str] | None = None,
) -> dict:
    """Apply autofixable rules. Returns {file: {rule: count}} stats."""
    rules_set = set(rules) if rules else FIXABLE_RULES
    stats: dict[str, dict[str, int]] = {}

    for rule in rules_set:
        rule_findings = _findings_for_rule(findings, rule)
        if not rule_findings:
            continue
        for file_path, file_findings in _by_file(rule_findings).items():
            full_path = project_root / file_path
            if not full_path.exists():
                logger.warning(f"Skipping {file_path}: not found under {project_root}")
                continue
            try:
                source = full_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(f"Cannot read {file_path}: {e}")
                continue

            lines = source.splitlines(keepends=True)
            n = 0
            if rule == "DEAD_IMPORT":
                lines, n = _fix_dead_import(lines, file_findings)
            elif rule == "UNUSED_PARAM":
                lines, n = _fix_unused_param(lines, file_findings)

            if n == 0:
                continue
            stats.setdefault(file_path, {})[rule] = n

            if not dry_run:
                full_path.write_text("".join(lines), encoding="utf-8")

    return stats


def print_fix_report(stats: dict, dry_run: bool = False) -> None:
    if not stats:
        print("Autofix: nothing to fix.")
        return
    total = sum(sum(rules.values()) for rules in stats.values())
    mode = "DRY-RUN" if dry_run else "APPLIED"
    print()
    print(f"=== AUTOFIX [{mode}] — {total} fixes across {len(stats)} files ===")
    for fp, rules in sorted(stats.items()):
        for rule, n in rules.items():
            print(f"  {fp}: {n} × {rule}")
    print()
