"""
Tier 3: Claude API deep reviewer.

Sends repo-map + all prior findings + source of flagged files to Claude.
Single synthesis pass for bugs that require cross-file reasoning,
doc-vs-code comparison, and architectural judgment.

Uses prompt caching for the repo-map context.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from .schema import RepoMap, NodeType

logger = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are a senior software architect doing a deep code review. You have access to:
1. A structural map of the project (files, edges, logic blocks, connectivity)
2. Prior findings from static analysis (lint, xref, targeted questions)
3. Source code of flagged files

Your job: find bugs that ALL prior analysis tiers missed. Focus on:
1. Cross-file contract violations (function A promises X, function B expects Y)
2. Regression detection (TODO/doc claims feature exists, code doesn't implement it)
3. Temporal ordering bugs (commit before async, close before flush)
4. Feature completeness gaps (feature declared but never wired end-to-end)
5. Data integrity risks (concurrent access, partial updates, lost writes)

Output STRICT JSON:
{"findings": [{"file": "path", "line": N, "severity": "crit|high|med|low", "category": "contract|regression|temporal|completeness|integrity", "desc": "one sentence", "evidence": "cite specific lines/functions"}]}

Be specific. Cite lines. Only report issues NOT already found by prior tiers."""


def deep_review(project_root: Path, repo_map: RepoMap,
                prior_findings: list = None,
                flagged_files: list = None,
                todo_content: str = None) -> list[dict]:
    project_root = project_root.resolve()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Return sentinel so CLI can distinguish "no additional findings" from
        # "Claude was never called". Previously both returned [] silently.
        logger.error("ANTHROPIC_API_KEY not set — Claude deep review SKIPPED")
        return [{
            "file": "-",
            "line": 0,
            "severity": "high",
            "category": "tool_failure",
            "source": "deep_reviewer",
            "bug_desc": "ANTHROPIC_API_KEY not set; Claude deep review SKIPPED — "
                        "results are UNKNOWN, not clean",
        }]

    context_parts = []

    context_parts.append("## PROJECT MAP SUMMARY")
    context_parts.append(f"Project: {repo_map.project_name}")
    context_parts.append(f"Files: {repo_map.stats.get('files', 0)}, "
                         f"Functions: {repo_map.stats.get('functions', 0)}, "
                         f"Edges: {repo_map.stats.get('edges', 0)}")

    context_parts.append("\n## LOGIC BLOCKS")
    for block in repo_map.logic_blocks:
        context_parts.append(f"  [{block.id}] {block.name} ({len(block.node_ids)} files)")

    context_parts.append("\n## CONNECTIVITY ISSUES")
    conn = repo_map.stats.get("connectivity", {})
    for u in conn.get("unreachable", []):
        context_parts.append(f"  UNREACHABLE: {u}")
    for i in conn.get("incomplete", []):
        context_parts.append(f"  INCOMPLETE: {i}")

    context_parts.append("\n## ROUTES")
    for node in repo_map.nodes:
        if node.type == NodeType.FILE and node.routes:
            for r in node.routes:
                if r.get("method"):
                    context_parts.append(f"  {r['method']} {r.get('path', '?')} → {node.path}:{r.get('handler', '?')}")
                elif r.get("type") == "include_router":
                    prefix = r.get("prefix", "NO PREFIX")
                    warning = f" ⚠ {r['warning']}" if r.get("warning") else ""
                    context_parts.append(f"  include_router prefix={prefix} → {node.path}{warning}")

    if prior_findings:
        context_parts.append(f"\n## PRIOR FINDINGS ({len(prior_findings)} total)")
        context_parts.append("These have already been found — do NOT repeat them:")
        for f in prior_findings[:50]:
            if isinstance(f, dict):
                context_parts.append(f"  [{f.get('severity','?')}] {f.get('file','?')}:{f.get('line','?')} {f.get('desc', f.get('bug_desc', '?'))}")

    if todo_content:
        context_parts.append("\n## TODO DOCUMENT (claims about what's implemented)")
        lines = todo_content.splitlines()
        for line in lines[:200]:
            if any(kw in line for kw in ["DONE", "FIXED", "OPEN", "REGRESSED", "SHIPPED"]):
                context_parts.append(f"  {line.strip()}")

    context = "\n".join(context_parts)

    source_parts = []
    files_to_send = set()

    if flagged_files:
        files_to_send.update(flagged_files)

    for node in repo_map.nodes:
        if node.type == NodeType.FILE:
            if node.connectivity.value in ("unreachable", "incomplete"):
                files_to_send.add(node.path)

    total_lines = 0
    MAX_SOURCE_LINES = 5000

    for file_path_str in sorted(files_to_send):
        fp = project_root / file_path_str
        if not fp.exists():
            continue
        source = fp.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        if total_lines + len(lines) > MAX_SOURCE_LINES:
            remaining = MAX_SOURCE_LINES - total_lines
            if remaining > 30:
                source = "\n".join(lines[:remaining])
                source_parts.append(f"\n=== {file_path_str} (truncated {remaining}/{len(lines)}) ===\n{source}")
                total_lines += remaining
            break
        source_parts.append(f"\n=== {file_path_str} ({len(lines)} lines) ===\n{source}")
        total_lines += len(lines)

    user_message = f"""{context}

## SOURCE CODE OF FLAGGED FILES ({len(files_to_send)} files, {total_lines} lines)
{''.join(source_parts)}

Find bugs that all prior analysis missed. Focus on cross-file issues, regressions, and temporal ordering. Output JSON only."""

    logger.info(f"Sending to Claude: ~{len(context) + len(''.join(source_parts))} chars, {len(files_to_send)} files")

    t0 = time.time()
    response = _call_claude(api_key, user_message)
    elapsed = time.time() - t0

    if not response:
        return []

    findings = _parse_response(response)
    logger.info(f"Claude deep review: {len(findings)} findings in {elapsed:.1f}s")

    for f in findings:
        f["source"] = "claude_deep"

    return findings


def _call_claude(api_key: str, user_message: str) -> Optional[str]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_message}
        ],
    }

    try:
        resp = httpx.post(
            CLAUDE_API_URL,
            headers=headers,
            json=body,
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                return content[0]["text"]
            return None
        else:
            logger.error(f"Claude API error {resp.status_code}: {resp.text[:300]}")
            return None
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return None


def _parse_response(response: str) -> list[dict]:
    response = response.strip()

    json_start = response.find("{")
    json_end = response.rfind("}") + 1
    if json_start == -1 or json_end <= json_start:
        logger.warning("Claude response: no JSON found")
        return []

    try:
        data = json.loads(response[json_start:json_end])
    except json.JSONDecodeError as e:
        logger.warning(f"Claude response: JSON parse error: {e}")
        return []

    return data.get("findings", [])
