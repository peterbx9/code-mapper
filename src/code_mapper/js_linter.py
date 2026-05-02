"""JS/TS lint rules — regex-based for the same Phase-1 parser.

7 rules, all near-zero false positive:
- JS_VAR_DECLARATION    : `var` instead of `let`/`const`
- JS_LOOSE_EQUALITY     : `==` / `!=` (vs ===, !==)
- JS_CONSOLE_LOG        : console.log in non-test/non-debug source
- JS_USEEFFECT_NO_DEPS  : useEffect without dependency array
- JS_MISSING_KEY_PROP   : .map(...) producing JSX without key= prop (heuristic)
- JS_ASYNC_NO_CATCH     : async function whose body has no try/catch and no
                          .catch() on returned promise (caller-trust violation)
- JS_BARE_CATCH         : `catch (e) {}` — silently swallowed exception
"""
from __future__ import annotations
import re
from pathlib import Path

from .linter import LintFinding


_VAR_RE = re.compile(r"^\s*var\s+\w", re.MULTILINE)
_EQ_RE = re.compile(r"(?<![=!<>])(==|!=)(?!=)")
_CONSOLE_LOG_RE = re.compile(r"\bconsole\.log\s*\(")
_USEEFFECT_NO_DEPS_RE = re.compile(
    r"useEffect\s*\(\s*(?:\([^)]*\)\s*=>|function\s*\([^)]*\))[^,)]*\)",
)
_BARE_CATCH_RE = re.compile(r"catch\s*\(\s*\w*\s*\)\s*\{\s*\}")
_MAP_TO_JSX_RE = re.compile(
    r"\.map\s*\(\s*(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>\s*(?:\(\s*)?<(\w+)([^/>]*?)(?<!key=)\s*>",
)
_ASYNC_FN_RE = re.compile(
    r"(?:async\s+function\s+(\w+)|const\s+(\w+)\s*=\s*async\s*\([^)]*\)\s*=>)",
)


def _strip_strings(s: str) -> str:
    """Replace string contents with spaces of equal length so line numbers stay
    intact and regexes don't false-match inside strings."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in ('"', "'", "`"):
            quote = c
            j = i + 1
            while j < n and s[j] != quote:
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            # Replace string contents with spaces, keep newlines
            content = s[i+1:j]
            out.append(quote)
            out.append("".join(" " if ch != "\n" else "\n" for ch in content))
            if j < n:
                out.append(quote)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_comments(s: str) -> str:
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), s)
    return s


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _is_test_file(rel: str) -> bool:
    rl = rel.lower().replace("\\", "/")
    return any(seg in rl for seg in (
        "/test", "tests/", "/__tests__/", ".test.", ".spec.",
    ))


def lint_js_file(path: Path, project_root: Path) -> list[LintFinding]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []
    rel = str(path.relative_to(project_root)).replace("\\", "/")
    cleaned = _strip_comments(_strip_strings(source))
    findings: list[LintFinding] = []

    for m in _VAR_RE.finditer(cleaned):
        findings.append(LintFinding(
            file_path=rel, line=_line_of(source, m.start()),
            rule="JS_VAR_DECLARATION", severity="low",
            desc="`var` is function-scoped — use `let` (mutable) or `const` (default)",
        ))

    for m in _EQ_RE.finditer(cleaned):
        findings.append(LintFinding(
            file_path=rel, line=_line_of(source, m.start()),
            rule="JS_LOOSE_EQUALITY", severity="med",
            desc=f"`{m.group(1)}` does type coercion — use `{m.group(1)}=` for strict",
        ))

    if not _is_test_file(rel):
        for m in _CONSOLE_LOG_RE.finditer(cleaned):
            findings.append(LintFinding(
                file_path=rel, line=_line_of(source, m.start()),
                rule="JS_CONSOLE_LOG", severity="low",
                desc="console.log left in non-test source — use a logger or remove",
            ))

    for m in _USEEFFECT_NO_DEPS_RE.finditer(cleaned):
        findings.append(LintFinding(
            file_path=rel, line=_line_of(source, m.start()),
            rule="JS_USEEFFECT_NO_DEPS", severity="med",
            desc="useEffect with no dependency array — runs on every render. "
                 "Add [] for once-only or specify dependencies",
        ))

    for m in _BARE_CATCH_RE.finditer(cleaned):
        findings.append(LintFinding(
            file_path=rel, line=_line_of(source, m.start()),
            rule="JS_BARE_CATCH", severity="med",
            desc="empty catch block silently swallows errors — at minimum log the exception",
        ))

    for m in _MAP_TO_JSX_RE.finditer(cleaned):
        # Skip if the next 100 chars contain key= (broader window)
        window = cleaned[m.end():m.end()+200]
        if "key=" in window:
            continue
        findings.append(LintFinding(
            file_path=rel, line=_line_of(source, m.start()),
            rule="JS_MISSING_KEY_PROP", severity="med",
            desc="`.map(...)` returning JSX without `key=` prop — React reconciler will warn at runtime",
        ))

    # Async-without-error-handling: find async fn def, check body for try/catch
    for m in _ASYNC_FN_RE.finditer(cleaned):
        name = m.group(1) or m.group(2)
        # Find the function body (heuristic: next { ... matching })
        start = cleaned.find("{", m.end())
        if start < 0:
            continue
        depth = 1
        j = start + 1
        while j < len(cleaned) and depth > 0:
            if cleaned[j] == "{":
                depth += 1
            elif cleaned[j] == "}":
                depth -= 1
            j += 1
        body = cleaned[start:j]
        if "try" not in body and "catch" not in body and ".catch(" not in body:
            findings.append(LintFinding(
                file_path=rel, line=_line_of(source, m.start()),
                rule="JS_ASYNC_NO_CATCH", severity="low",
                desc=f"async function '{name}' has no try/catch — unhandled rejection bubbles up",
            ))

    return findings


def lint_js_project(project_root: Path,
                     exclude_dirs: set[str] | None = None) -> list[LintFinding]:
    from .js_parser import discover_js_files
    if exclude_dirs is None:
        exclude_dirs = set()
    findings: list[LintFinding] = []
    for p in discover_js_files(project_root, exclude_dirs):
        findings.extend(lint_js_file(p, project_root))
    return findings
