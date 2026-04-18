"""
AI-powered code review via local LLM (Tier 2).

Two modes:
  --ai           : 7B fast pass on every file (triage)
  --ai --deep    : 7B triage + 32B deep-dive on flagged files

Each file is reviewed individually with its xref edges + known static findings
as context (NOT the full block source — that overwhelmed 7B last time).
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

TRIAGE_PROMPT = """You are an expert Python code reviewer. Review this single file for bugs that static analysis CANNOT catch.

FILE: {file_path} ({line_count} lines)
ROLE IN PROJECT: {file_role}

CROSS-FILE RELATIONSHIPS (who imports/calls this, and what it imports/calls):
{xref_context}

STATIC ANALYSIS ALREADY FOUND THESE (do NOT repeat):
{known_issues}

SOURCE CODE:
{source_code}

FIND ONLY these categories — ignore style, naming, and anything the static analyzer already caught:
1. LOGIC ERRORS: wrong variable used, inverted condition, off-by-one, unreachable branch
2. MISSING VALIDATION: function accepts input but never checks type/range/null before using it
3. BROKEN CHAINS: function loads a value (from DB, config, or argument) but never uses it in any decision
4. REGRESSION RISK: code claims to implement X (via comments, docstring, or variable names) but the implementation is missing or incomplete
5. SILENT FAILURES: exceptions caught and swallowed without logging or re-raising, making bugs invisible

EXAMPLES of what I'm looking for:
- "invite_expires is set to datetime.now() instead of now + timedelta — tokens expire instantly"
- "retry logic claimed in docstring but no retry loop exists in the function body"
- "engine.roles loaded at line 110 but never consulted in the dispatch decision at line 220"
- "db.commit() called before async task completes — writes silently lost"

Output STRICT JSON — start with {{ end with }}. No prose, no markdown fences.
{{"findings": [{{"line": N, "severity": "crit|high|med|low", "category": "logic|validation|chain|regression|silent", "desc": "one sentence"}}]}}

If nothing found beyond what static analysis caught, return {{"findings": []}}.
Begin with {{."""

DEEP_PROMPT = """You are a senior Python architect doing a deep security + correctness review.

FILE: {file_path} ({line_count} lines)
ROLE IN PROJECT: {file_role}

CROSS-FILE RELATIONSHIPS:
{xref_context}

TRIAGE FINDINGS (from fast pass — verify and expand):
{triage_findings}

STATIC ANALYSIS FINDINGS:
{known_issues}

SOURCE CODE:
{source_code}

Go deeper than the triage pass. Look for:
1. Cross-file contract violations: does this file honor the contracts its callers expect?
2. Temporal bugs: ordering issues (commit before async completes, close before flush)
3. Security: path traversal, injection, credential exposure, missing auth checks
4. Feature completeness: does the code do what the docstring/comments promise?
5. Data integrity: can concurrent calls corrupt shared state?

Output STRICT JSON:
{{"findings": [{{"line": N, "severity": "crit|high|med|low", "category": "contract|temporal|security|completeness|integrity", "desc": "one sentence"}}]}}

Begin with {{."""


def review_project(project_root: Path, repo_map: RepoMap,
                   model: str = None, ollama_url: str = None,
                   xref_data: dict = None,
                   lint_findings: list = None,
                   deep: bool = False,
                   deep_model: str = None) -> list[dict]:
    project_root = project_root.resolve()
    ollama_url = ollama_url or DEFAULT_OLLAMA_URL
    if model is None:
        model = DEFAULT_MODEL_FAST
    if deep_model is None:
        deep_model = DEFAULT_MODEL_DEEP

    if not _check_ollama(ollama_url):
        logger.error(f"Ollama not reachable at {ollama_url}")
        return []

    file_nodes = [n for n in repo_map.nodes
                  if n.type == NodeType.FILE
                  and not n.path.endswith("__init__.py")]

    all_findings = []
    files_to_deep_dive = []

    logger.info(f"Tier 2 triage: {len(file_nodes)} files with {model}")

    for node in file_nodes:
        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"Can't read {node.path}: {e} — skipping")
            continue
        lines = source.splitlines()
        if len(lines) < 5:
            continue
        # 7B context is ~8K tokens; rough budget for source is ~3000 lines.
        # Silent server-side truncation produces garbage reviews — warn loudly.
        if len(lines) > 3000:
            logger.warning(
                f"{node.path}: {len(lines)} lines exceeds ~3000 line budget "
                f"for Ollama context; review may be incomplete"
            )

        t0 = time.time()
        findings = _review_file(
            node, source, repo_map, model, ollama_url,
            xref_data, lint_findings, TRIAGE_PROMPT
        )
        elapsed = time.time() - t0

        if findings:
            logger.info(f"  {node.path}: {len(findings)} findings ({elapsed:.1f}s)")
            for f in findings:
                f["file"] = node.path
                f["source"] = "ai_triage"
            all_findings.extend(findings)
            files_to_deep_dive.append(node)
        else:
            logger.debug(f"  {node.path}: clean ({elapsed:.1f}s)")

    if deep and files_to_deep_dive:
        logger.info(f"\nTier 2 deep-dive: {len(files_to_deep_dive)} flagged files with {deep_model}")

        triage_by_file = {}
        for f in all_findings:
            fp = f.get("file", "")
            if fp not in triage_by_file:
                triage_by_file[fp] = []
            triage_by_file[fp].append(f)

        for node in files_to_deep_dive:
            file_path = project_root / node.path
            source = file_path.read_text(encoding="utf-8", errors="replace")

            t0 = time.time()
            deep_findings = _review_file(
                node, source, repo_map, deep_model, ollama_url,
                xref_data, lint_findings, DEEP_PROMPT,
                triage_findings=triage_by_file.get(node.path, [])
            )
            elapsed = time.time() - t0

            if deep_findings:
                logger.info(f"  DEEP {node.path}: {len(deep_findings)} findings ({elapsed:.1f}s)")
                for f in deep_findings:
                    f["file"] = node.path
                    f["source"] = "ai_deep"
                all_findings.extend(deep_findings)
            else:
                logger.info(f"  DEEP {node.path}: confirmed clean ({elapsed:.1f}s)")

    return all_findings


def _review_file(node, source: str, repo_map: RepoMap,
                 model: str, ollama_url: str,
                 xref_data: dict = None,
                 lint_findings: list = None,
                 prompt_template: str = TRIAGE_PROMPT,
                 triage_findings: list = None) -> list[dict]:

    lines = source.splitlines()
    line_count = len(lines)

    file_role = _describe_file_role(node)
    xref_context = _build_xref_context(node.path, xref_data)
    known_issues = _build_known_issues(node.path, lint_findings)
    triage_str = ""
    if triage_findings:
        triage_str = "\n".join(
            f"  line {f.get('line','?')}: [{f.get('severity','?')}] {f.get('desc','?')}"
            for f in triage_findings
        )

    prompt = prompt_template.format(
        file_path=node.path,
        line_count=line_count,
        file_role=file_role,
        xref_context=xref_context or "  (no cross-file data available)",
        known_issues=known_issues or "  None",
        triage_findings=triage_str or "  None",
        source_code=source,
    )

    response = _call_ollama(prompt, model, ollama_url)
    if not response:
        return []

    return _parse_response(response, node.path)


def _describe_file_role(node) -> str:
    parts = []
    if node.routes:
        route_count = sum(1 for r in node.routes if r.get("method"))
        include_count = sum(1 for r in node.routes if r.get("type") == "include_router")
        if route_count:
            parts.append(f"{route_count} API endpoints")
        if include_count:
            parts.append(f"registers {include_count} sub-routers")
    if node.tables:
        parts.append(f"touches tables: {', '.join(node.tables[:5])}")
    if node.is_stub:
        parts.append("STUB (no-op implementation)")
    if node.connectivity.value == "unreachable":
        parts.append("UNREACHABLE (dead code)")
    elif node.connectivity.value == "incomplete":
        parts.append("INCOMPLETE WIRING (connected but chain is broken)")
    if node.docstring:
        parts.append(f"purpose: {node.docstring}")
    return "; ".join(parts) if parts else "utility module"


def _build_xref_context(file_path: str, xref_data: dict) -> str:
    if not xref_data or "symbols" not in xref_data:
        return ""

    parts = []
    for key, sym in xref_data["symbols"].items():
        if sym.get("defined_in") != file_path:
            continue
        name = sym.get("name", "?")
        imported_by = sym.get("imported_by", [])
        called_from = sym.get("called_from", [])

        if imported_by:
            importers = [i["file"] for i in imported_by[:3]]
            parts.append(f"  {name}: imported by {importers}")
        if called_from:
            callers = [f"{c['file']}:{c.get('line','?')}" for c in called_from[:3]]
            parts.append(f"  {name}: called from {callers}")
        if not imported_by and not called_from and sym.get("usage_count", 0) == 0:
            parts.append(f"  {name}: NEVER USED outside this file")

    for key, sym in xref_data["symbols"].items():
        if sym.get("defined_in") == file_path:
            continue
        for ref in sym.get("imported_by", []):
            if ref.get("file") == file_path:
                parts.append(f"  this file imports {sym['name']} from {sym['defined_in']}")
                break

    return "\n".join(parts[:20])


def _build_known_issues(file_path: str, lint_findings: list) -> str:
    if not lint_findings:
        return ""
    parts = []
    for f in lint_findings:
        fp = f.get("file", "") if isinstance(f, dict) else getattr(f, "file_path", "")
        if fp != file_path:
            continue
        if isinstance(f, dict):
            parts.append(f"  [{f['severity']}] line {f['line']}: {f['rule']}: {f['desc']}")
        else:
            parts.append(f"  [{f.severity}] line {f.line}: {f.rule}: {f.desc}")
    return "\n".join(parts[:15])


def _check_ollama(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


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
                    "num_predict": 1024,
                },
            },
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "")
        else:
            logger.warning(f"Ollama returned {resp.status_code}: {resp.text[:200]}")
            return None
    except httpx.TimeoutException:
        logger.warning(f"Ollama timed out (120s) for model {model}")
        return None
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return None


def _parse_response(response: str, file_path: str) -> list[dict]:
    response = response.strip()

    json_start = response.find("{")
    json_end = response.rfind("}") + 1
    if json_start == -1 or json_end <= json_start:
        logger.warning(f"{file_path}: no JSON found in response")
        return []

    json_str = response[json_start:json_end]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"{file_path}: JSON parse error: {e}")
        return []

    findings = data.get("findings", [])
    valid = []
    for f in findings:
        if isinstance(f, dict) and "desc" in f:
            valid.append(f)

    return valid
