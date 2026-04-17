"""
Map assembler — walks a project tree, runs the parser on each file,
and assembles a RepoMap with resolved edges.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schema import RepoMap, Node, Edge, EdgeType, NodeType
from .python_parser import parse_file

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDE = {
    "venv", ".venv", "node_modules", "__pycache__", ".git", "dist", "build",
    ".egg-info", ".tox", ".mypy_cache", ".pytest_cache", "BAD",
}

DEFAULT_EXCLUDE_FILES = {
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.egg",
}


def load_config(project_root: Path) -> dict:
    config_path = project_root / ".codemapper.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {config_path}: {e}")
    return {}


def assemble_map(project_root: Path, config: Optional[dict] = None) -> RepoMap:
    project_root = project_root.resolve()
    if config is None:
        config = load_config(project_root)

    project_name = config.get("project_name", project_root.name)
    exclude_dirs = set(config.get("exclude", [])) | DEFAULT_EXCLUDE
    entry_points = config.get("python", {}).get("entry_points", [])

    repo_map = RepoMap(
        project_name=project_name,
        root=str(project_root),
        generated_at=datetime.now(timezone.utc).isoformat(),
        entry_points=[f"file:{ep}" for ep in entry_points],
    )

    py_files = _find_python_files(project_root, exclude_dirs)
    logger.info(f"Found {len(py_files)} Python files in {project_root}")

    all_nodes = []
    all_edges = []

    for py_file in py_files:
        try:
            nodes, edges = parse_file(py_file, project_root)
            all_nodes.extend(nodes)
            all_edges.extend(edges)
        except Exception as e:
            logger.warning(f"Failed to parse {py_file}: {e}")
            rel = str(py_file.relative_to(project_root)).replace("\\", "/")
            all_nodes.append(Node(
                id=f"file:{rel}",
                type=NodeType.FILE,
                path=rel,
                name=py_file.stem,
                docstring=f"PARSE ERROR: {e}",
            ))

    repo_map.nodes = all_nodes
    repo_map.edges = all_edges

    _resolve_edges(repo_map)
    _resolve_relationship_tables(repo_map)

    file_count = sum(1 for n in all_nodes if n.type == NodeType.FILE)
    class_count = sum(1 for n in all_nodes if n.type == NodeType.CLASS)
    func_count = sum(1 for n in all_nodes if n.type == NodeType.FUNCTION)
    stub_count = sum(1 for n in all_nodes if n.is_stub)

    repo_map.stats = {
        "files": file_count,
        "classes": class_count,
        "functions": func_count,
        "stubs": stub_count,
        "edges": len(all_edges),
        "tables": list({t for n in all_nodes for t in n.tables}),
    }

    return repo_map


def _find_python_files(root: Path, exclude_dirs: set) -> list[Path]:
    py_files = []

    gitignore_patterns = _load_gitignore(root)

    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        parts = rel.parts

        skip = False
        for part in parts[:-1]:
            if part in exclude_dirs:
                skip = True
                break
            for excl in exclude_dirs:
                if excl.endswith("/") and part == excl.rstrip("/"):
                    skip = True
                    break
            if skip:
                break

        if skip:
            continue

        rel_str = str(rel).replace("\\", "/")
        if any(rel_str.startswith(p.rstrip("/")) for p in exclude_dirs if "/" in p):
            continue

        if _matches_gitignore(rel_str, gitignore_patterns):
            continue

        if path.name.endswith("-OFF.py"):
            continue

        py_files.append(path)

    return sorted(py_files)


def _load_gitignore(root: Path) -> list[str]:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _matches_gitignore(rel_path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        clean = pattern.rstrip("/")
        if clean in rel_path.split("/"):
            return True
    return False


def _resolve_edges(repo_map: RepoMap):
    node_lookup = {}
    for node in repo_map.nodes:
        node_lookup[node.id] = node
        if node.type == NodeType.FILE:
            module_path = node.path.replace("/", ".").replace("\\", ".")
            if module_path.endswith(".py"):
                module_path = module_path[:-3]
            node_lookup[f"module:{module_path}"] = node

            parts = module_path.split(".")
            if parts[-1] == "__init__":
                package_path = ".".join(parts[:-1])
                node_lookup[f"module:{package_path}"] = node

            for i in range(len(parts)):
                partial = ".".join(parts[:i+1])
                sub_key = f"module:{partial}"
                if sub_key not in node_lookup:
                    node_lookup[sub_key] = node

            for name_in_file in _get_exported_names(node, repo_map):
                export_key = f"module:{module_path}.{name_in_file}"
                if export_key not in node_lookup:
                    node_lookup[export_key] = node

        elif node.type == NodeType.FUNCTION:
            func_short = node.name.split(".")[-1] if "." in node.name else node.name
            call_key = f"call:{func_short}"
            if call_key not in node_lookup:
                node_lookup[call_key] = node
            call_full = f"call:{node.name}"
            if call_full not in node_lookup:
                node_lookup[call_full] = node

        elif node.type == NodeType.CLASS:
            class_ref_key = f"class_ref:{node.name}"
            if class_ref_key not in node_lookup:
                node_lookup[class_ref_key] = node

    resolved = []
    for edge in repo_map.edges:
        target_node = node_lookup.get(edge.target)
        if not target_node and edge.target.startswith("module:"):
            mod_path = edge.target.replace("module:", "")
            for suffix in [".py", "/__init__.py"]:
                file_key = f"file:{mod_path.replace('.', '/')}{suffix}"
                if file_key in node_lookup:
                    target_node = node_lookup[file_key]
                    break
            if not target_node:
                parts = mod_path.split(".")
                for i in range(len(parts), 0, -1):
                    candidate = "/".join(parts[:i]) + ".py"
                    file_key = f"file:{candidate}"
                    if file_key in node_lookup:
                        target_node = node_lookup[file_key]
                        break

        if target_node:
            edge.target = target_node.id
            resolved.append(edge)
        elif edge.target.startswith("module:") and not _is_stdlib_or_external(edge.target):
            resolved.append(edge)
        elif edge.type == EdgeType.CALL:
            pass
        else:
            resolved.append(edge)

    repo_map.edges = resolved


def _get_exported_names(file_node: Node, repo_map: RepoMap) -> list[str]:
    names = []
    for node in repo_map.nodes:
        if node.parent_id == file_node.id:
            short_name = node.name.split(".")[-1] if "." in node.name else node.name
            names.append(short_name)
    return names


def _resolve_relationship_tables(repo_map: RepoMap):
    model_to_table = {}
    for node in repo_map.nodes:
        if node.type == NodeType.CLASS:
            for table in (repo_map.get_node(node.parent_id) or Node(id="", type=NodeType.FILE, path="", name="")).tables:
                if not table.startswith("rel:"):
                    class_name = node.name.split(".")[-1] if "." in node.name else node.name
                    model_to_table[class_name] = table

    for node in repo_map.nodes:
        resolved_tables = []
        for table in node.tables:
            if table.startswith("rel:"):
                model_name = table[4:]
                actual_table = model_to_table.get(model_name)
                if actual_table:
                    resolved_tables.append(f"{actual_table} (via {model_name})")
                else:
                    resolved_tables.append(table)
            else:
                resolved_tables.append(table)
        node.tables = resolved_tables


def _is_stdlib_or_external(module_ref: str) -> bool:
    name = module_ref.replace("module:", "").split(".")[0]
    stdlib = {
        "os", "sys", "re", "json", "datetime", "pathlib", "typing", "logging",
        "asyncio", "collections", "functools", "itertools", "io", "math",
        "hashlib", "uuid", "base64", "time", "shutil", "tempfile", "copy",
        "enum", "dataclasses", "abc", "contextlib", "socket", "secrets",
        "email", "urllib", "http", "html", "xml", "csv", "sqlite3",
        "threading", "multiprocessing", "subprocess", "signal", "traceback",
    }
    external = {
        "fastapi", "uvicorn", "sqlalchemy", "pydantic", "httpx", "bcrypt",
        "pikepdf", "fitz", "pdfplumber", "pytesseract", "PIL", "pillow",
        "requests", "numpy", "pandas", "paramiko", "dotenv",
    }
    return name in stdlib or name in external
