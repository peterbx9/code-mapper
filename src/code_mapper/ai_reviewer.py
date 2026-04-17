"""
AI-powered code review via local LLM (Tier 2).

Sends each logic block's files + xref context to Ollama (Qwen Coder).
Reviews per-block, not per-file, so the LLM sees cross-file relationships
within a logical unit.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from .schema import RepoMap, LogicBlock, NodeType

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL_FAST = "qwen2.5-coder:7b"
DEFAULT_MODEL_DEEP = "qwen2.5-coder:32b"

REVIEW_PROMPT = """You are a code reviewer auditing a logic block of a Python project.

PROJECT: {project_name}
BLOCK: "{block_name}" ({file_count} files)

FILES IN THIS BLOCK:
{file_listing}

KNOWN ISSUES (already found by static analysis — do NOT repeat these):
{known_issues}

CROSS-REFERENCE DATA:
{xref_summary}

SOURCE CODE:
{source_code}

Find issues the static analyzer CANNOT catch:
1. Hardcoded project-specific values that should be configurable
2. Missing error handling (unguarded calls that can throw)
3. Logic errors (wrong variable, inverted condition, off-by-one)
4. Incomplete implementations (TODO/FIXME, stubbed behavior)
5. Security concerns (path traversal, injection, credential handling)
6. Performance anti-patterns beyond simple list.pop(0)

Output STRICT JSON only — start with {{ end with }}. No prose.
Schema:
{{"findings": [{{"file": "path", "line": N, "severity": "crit|high|med|low", "desc": "short description"}}]}}

If no issues found, return {{"findings": []}}.
Begin with {{."""


def review_project(project_root: Path, repo_map: RepoMap,
                   model: str = None, ollama_url: str = None,
                   xref_data: dict = None,
                   lint_findings: list = None) -> list[dict]:
    project_root = project_root.resolve()
    ollama_url = ollama_url or DEFAULT_OLLAMA_URL
    if model is None:
        model = DEFAULT_MODEL_FAST

    if not _check_ollama(ollama_url):
        logger.error(f"Ollama not reachable at {ollama_url}")
        return []

    if not repo_map.logic_blocks:
        logger.warning("No logic blocks — run clustering first")
        return []

    all_findings = []

    for block in repo_map.logic_blocks:
        logger.info(f"Reviewing block: {block.name} ({len(block.node_ids)} files)")
        t0 = time.time()

        findings = _review_block(
            project_root, repo_map, block,
            model, ollama_url,
            xref_data, lint_findings,
        )

        elapsed = time.time() - t0
        logger.info(f"  {len(findings)} findings in {elapsed:.1f}s")
        all_findings.extend(findings)

    return all_findings


def _check_ollama(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _review_block(project_root: Path, repo_map: RepoMap, block: LogicBlock,
                  model: str, ollama_url: str,
                  xref_data: dict = None,
                  lint_findings: list = None) -> list[dict]:

    file_nodes = []
    for nid in block.node_ids:
        node = repo_map.get_node(nid)
        if node and node.type == NodeType.FILE:
            file_nodes.append(node)

    if not file_nodes:
        return []

    source_parts = []
    file_listing_parts = []
    total_lines = 0
    MAX_LINES = 3000

    for node in file_nodes:
        file_path = project_root / node.path
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()

        if total_lines + len(lines) > MAX_LINES:
            remaining = MAX_LINES - total_lines
            if remaining > 50:
                source = "\n".join(lines[:remaining])
                source_parts.append(f"=== {node.path} (truncated at {remaining}/{len(lines)} lines) ===\n{source}")
                total_lines += remaining
            break

        source_parts.append(f"=== {node.path} ({len(lines)} lines) ===\n{source}")
        total_lines += len(lines)

        routes_str = ""
        if node.routes:
            routes_str = f" | routes: {len(node.routes)}"
        tables_str = ""
        if node.tables:
            tables_str = f" | tables: {', '.join(node.tables[:5])}"
        conn_str = ""
        if node.connectivity.value != "reachable":
            conn_str = f" | ⚠ {node.connectivity.value}"

        file_listing_parts.append(
            f"  {node.path} ({node.line_start}-{node.line_end}){routes_str}{tables_str}{conn_str}"
        )

    known_parts = []
    if lint_findings:
        for f in lint_findings:
            if isinstance(f, dict):
                fpath = f.get("file", "")
            else:
                fpath = f.file_path
            block_paths = {n.path for n in file_nodes}
            if fpath in block_paths:
                if isinstance(f, dict):
                    known_parts.append(f"  [{f['severity']}] {fpath}:{f['line']} {f['rule']}: {f['desc']}")
                else:
                    known_parts.append(f"  [{f.severity}] {fpath}:{f.line} {f.rule}: {f.desc}")

    xref_parts = []
    if xref_data and "findings" in xref_data:
        block_paths = {n.path for n in file_nodes}
        for f in xref_data["findings"]:
            if f.get("file") in block_paths:
                xref_parts.append(f"  [{f['severity']}] {f['file']}:{f['line']} {f['rule']}: {f['desc']}")

    prompt = REVIEW_PROMPT.format(
        project_name=repo_map.project_name,
        block_name=block.name,
        file_count=len(file_nodes),
        file_listing="\n".join(file_listing_parts) or "  (none)",
        known_issues="\n".join(known_parts) or "  None",
        xref_summary="\n".join(xref_parts) or "  None",
        source_code="\n\n".join(source_parts),
    )

    response = _call_ollama(prompt, model, ollama_url)
    if not response:
        return []

    findings = _parse_response(response, block.name)
    return findings


def _call_ollama(prompt: str, model: str, url: str) -> Optional[str]:
    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 2048,
                },
            },
            timeout=300,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "")
        else:
            logger.warning(f"Ollama returned {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return None


def _parse_response(response: str, block_name: str) -> list[dict]:
    response = response.strip()

    json_start = response.find("{")
    json_end = response.rfind("}") + 1
    if json_start == -1 or json_end <= json_start:
        logger.warning(f"Block '{block_name}': no JSON found in response")
        return []

    json_str = response[json_start:json_end]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"Block '{block_name}': JSON parse error: {e}")
        logger.debug(f"  Raw: {json_str[:500]}")
        return []

    findings = data.get("findings", [])
    valid = []
    for f in findings:
        if isinstance(f, dict) and "desc" in f:
            f["block"] = block_name
            f["source"] = "ai_review"
            valid.append(f)

    return valid
