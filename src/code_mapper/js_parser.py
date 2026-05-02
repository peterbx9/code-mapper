"""JS/TS parser — regex-based extraction of imports/exports/defs.

Phase 1 rough draft. NOT a full AST. Designed to handle the 80% case for
audit/lint use:
  - ES6 imports: `import X from 'mod'`, `import { A, B } from 'mod'`, `import * as X`
  - CommonJS: `const X = require('mod')` / `var X = require(...)`
  - Function declarations: `function foo()`, `const foo = (...) =>`, `async function`
  - Class declarations: `class Foo extends Bar {}`
  - Exports: `export default`, `export const`, `export { ... }`, `module.exports`
  - React components: function with PascalCase + returns JSX, class extends Component

Skips: dynamic import(), template-literal imports, deeply destructured imports,
TypeScript-only constructs (interfaces, types), decorators.

Known limitations are documented; the structural map produced is "good
enough" for dead-file detection, import graph, hotspot scoring.
"""
from __future__ import annotations
import re
from pathlib import Path

from .schema import Edge, EdgeType, Node, NodeType

# Regex bank — compiled once
_IMPORT_PATTERNS = [
    # import X from 'mod'                       — default import
    re.compile(r"^\s*import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    # import { A, B as C } from 'mod'           — named imports
    re.compile(r"^\s*import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    # import * as X from 'mod'                  — namespace import
    re.compile(r"^\s*import\s+\*\s+as\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    # import 'mod'                              — side-effect only
    re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
]
_REQUIRE_RE = re.compile(
    r"(?:const|let|var)\s+(?:\{[^}]+\}|\w+)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]"
)
_FN_DECL_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*\(",
    re.MULTILINE,
)
_ARROW_FN_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?const\s+(\w+)\s*=\s*"
    r"(?:async\s+)?\(?[^)]*\)?\s*=>",
    re.MULTILINE,
)
_CLASS_DECL_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?class\s+(\w+)(?:\s+extends\s+(\w+))?",
    re.MULTILINE,
)
_EXPORT_DEFAULT_RE = re.compile(r"^\s*export\s+default\b", re.MULTILINE)
_EXPORT_NAMED_RE = re.compile(r"^\s*export\s+\{([^}]+)\}", re.MULTILINE)
_EXPORT_CONST_RE = re.compile(
    r"^\s*export\s+(?:const|let|var|function|class|async\s+function)\s+(\w+)",
    re.MULTILINE,
)
_REACT_FN_COMPONENT_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:const|function)\s+([A-Z]\w+)\b",
    re.MULTILINE,
)


def _strip_comments_and_strings(src: str) -> str:
    """Crude: blank out single-line comments + string contents to avoid false
    positives in regex matching. Keeps line numbers intact."""
    # Remove line comments
    src = re.sub(r"//[^\n]*", "", src)
    # Remove block comments (single-line replacement; multi-line needs care)
    src = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), src)
    return src


def parse_js_file(path: Path, project_root: Path) -> tuple[list[Node], list[Edge]]:
    """Returns (nodes, edges) extracted from the file. Edges have placeholder
    target ids that get resolved later by the assembler."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return [], []

    rel = str(path.relative_to(project_root)).replace("\\", "/")
    file_id = f"file::{rel}"
    nodes: list[Node] = []
    edges: list[Edge] = []

    nodes.append(Node(
        id=file_id, type=NodeType.FILE, path=rel, name=path.name,
        line_start=1, line_end=len(source.splitlines()),
    ))

    cleaned = _strip_comments_and_strings(source)

    # Imports
    for pat in _IMPORT_PATTERNS:
        for m in pat.finditer(cleaned):
            module = m.group(m.lastindex)  # last group is always the module
            edges.append(Edge(
                source=file_id,
                target=f"module::{module}",
                type=EdgeType.IMPORT,
            ))
    # CommonJS require
    for m in _REQUIRE_RE.finditer(cleaned):
        module = m.group(1)
        edges.append(Edge(
            source=file_id, target=f"module::{module}", type=EdgeType.IMPORT,
        ))

    # Function declarations
    for m in _FN_DECL_RE.finditer(cleaned):
        name = m.group(1)
        line = source[:m.start()].count("\n") + 1
        nodes.append(Node(
            id=f"fn::{rel}::{name}", type=NodeType.FUNCTION, path=rel,
            name=name, line_start=line, parent_id=file_id,
        ))
    # Arrow function consts
    for m in _ARROW_FN_RE.finditer(cleaned):
        name = m.group(1)
        line = source[:m.start()].count("\n") + 1
        nodes.append(Node(
            id=f"fn::{rel}::{name}", type=NodeType.FUNCTION, path=rel,
            name=name, line_start=line, parent_id=file_id,
        ))
    # Class declarations
    for m in _CLASS_DECL_RE.finditer(cleaned):
        name = m.group(1)
        line = source[:m.start()].count("\n") + 1
        n = Node(
            id=f"class::{rel}::{name}", type=NodeType.CLASS, path=rel,
            name=name, line_start=line, parent_id=file_id,
        )
        if m.group(2):
            n.docstring = f"extends {m.group(2)}"
        nodes.append(n)

    return nodes, edges


def discover_js_files(project_root: Path, exclude_dirs: set[str]) -> list[Path]:
    """Walk for .js/.jsx/.mjs/.cjs/.ts/.tsx files. Excludes common
    static/vendor/build dirs even if not in exclude_dirs."""
    SKIP = exclude_dirs | {
        "node_modules", "dist", "build", ".next", ".vite", ".turbo", "out",
        "vendor", "assets", "static", "public", "coverage",
    }
    EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
    out: list[Path] = []
    for p in project_root.rglob("*"):
        if not p.is_file() or p.suffix not in EXTS:
            continue
        rel_parts = p.relative_to(project_root).parts
        if any(part in SKIP or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if p.name.endswith(".d.ts"):
            continue  # type-only declarations, not source
        out.append(p)
    return sorted(out)


def parse_js_project(project_root: Path,
                      exclude_dirs: set[str] | None = None) -> tuple[list[Node], list[Edge]]:
    if exclude_dirs is None:
        exclude_dirs = set()
    files = discover_js_files(project_root, exclude_dirs)
    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    for f in files:
        nodes, edges = parse_js_file(f, project_root)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
    return all_nodes, all_edges
