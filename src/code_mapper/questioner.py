"""
Targeted question generator + verifier (Tier 2 v3).

Instead of "review this file" (which 7B can't do deeply), we:
1. Generate specific yes/no questions from xref/connectivity signals
2. Ask 7B each question with minimal context (just the relevant function)
3. A "no" answer = confirmed bug

Question sources:
- INCOMPLETE wiring → "Does function X use the Y field it loads?"
- XREF unused symbol → "Is there any code path that calls function X?"
- Soft-delete tables → "Does the query filter on deleted_at?"
- Loaded-but-unused → "After loading Z at line N, does any branch use Z?"
- Claimed features → "Does the docstring's claimed behavior match the code?"
"""

import ast
import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from .schema import RepoMap, NodeType, ConnectivityStatus

logger = logging.getLogger(__name__)

QUESTION_PROMPT = """Answer this ONE question about the Python code below. Reply with ONLY valid JSON.

QUESTION: {question}

CODE ({file_path}, lines {start_line}-{end_line}):
```python
{code_snippet}
```

Reply format: {{"answer": "yes"|"no", "evidence": "one sentence citing the specific line", "line": N}}
Begin with {{."""


class TargetedQuestion:
    def __init__(self, file_path: str, question: str, code_snippet: str,
                 start_line: int, end_line: int, source_signal: str,
                 bug_if_no: str):
        self.file_path = file_path
        self.question = question
        self.code_snippet = code_snippet
        self.start_line = start_line
        self.end_line = end_line
        self.source_signal = source_signal
        self.bug_if_no = bug_if_no

    def to_dict(self):
        return {
            "file": self.file_path,
            "question": self.question,
            "source_signal": self.source_signal,
            "bug_if_no": self.bug_if_no,
        }


def generate_and_verify(project_root: Path, repo_map: RepoMap,
                        xref_data: dict = None,
                        model: str = "qwen2.5-coder:7b",
                        ollama_url: str = "http://127.0.0.1:11434") -> list[dict]:
    project_root = project_root.resolve()

    questions = _generate_questions(project_root, repo_map, xref_data)
    logger.info(f"Generated {len(questions)} targeted questions")

    if not questions:
        return []

    if not _check_ollama(ollama_url):
        logger.error(f"Ollama not reachable at {ollama_url}")
        return []

    findings = []
    for q in questions:
        t0 = time.time()
        result = _ask_question(q, model, ollama_url)
        elapsed = time.time() - t0

        if result and result.get("answer", "").lower() == "no":
            finding = {
                "file": q.file_path,
                "line": result.get("line", q.start_line),
                "severity": "high",
                "category": "targeted_verify",
                "source": "questioner",
                "question": q.question,
                "evidence": result.get("evidence", ""),
                "bug_desc": q.bug_if_no,
                "signal": q.source_signal,
            }
            findings.append(finding)
            logger.info(f"  CONFIRMED: {q.file_path} — {q.bug_if_no} ({elapsed:.1f}s)")
        elif result:
            logger.debug(f"  OK: {q.file_path} — {q.question} → yes ({elapsed:.1f}s)")
        else:
            logger.debug(f"  SKIP: {q.file_path} — no response ({elapsed:.1f}s)")

    return findings


def _generate_questions(project_root: Path, repo_map: RepoMap,
                        xref_data: dict = None) -> list[TargetedQuestion]:
    questions = []

    questions.extend(_questions_from_connectivity(project_root, repo_map))
    questions.extend(_questions_from_xref(project_root, repo_map, xref_data))
    questions.extend(_questions_from_soft_delete(project_root, repo_map))
    questions.extend(_questions_from_loaded_fields(project_root, repo_map))

    return questions


def _questions_from_connectivity(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    questions = []

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        if node.connectivity != ConnectivityStatus.INCOMPLETE:
            continue

        file_path = project_root / node.path
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()

        for func_node in repo_map.nodes:
            if func_node.type != NodeType.FUNCTION:
                continue
            if func_node.path != node.path:
                continue

            func_source = "\n".join(lines[func_node.line_start - 1:func_node.line_end])

            for edge in repo_map.edges:
                if edge.source != node.id:
                    continue
                if edge.type.value == "import":
                    target_name = edge.target.split(":")[-1].split(".")[-1]
                    if target_name in func_source and "roles" in target_name.lower():
                        questions.append(TargetedQuestion(
                            file_path=node.path,
                            question=f"In function '{func_node.name}', after the '{target_name}' data is loaded, is it actually used in any decision logic (if statement, return value, function argument)?",
                            code_snippet=func_source,
                            start_line=func_node.line_start,
                            end_line=func_node.line_end,
                            source_signal=f"INCOMPLETE wiring on {node.path}",
                            bug_if_no=f"'{target_name}' loaded but never used in any decision — feature wired but idle",
                        ))

    return questions


def _questions_from_xref(project_root: Path, repo_map: RepoMap,
                         xref_data: dict = None) -> list[TargetedQuestion]:
    questions = []
    if not xref_data or "findings" not in xref_data:
        return questions

    for finding in xref_data.get("findings", []):
        if finding.get("rule") != "XREF_IMPORTED_NOT_CALLED":
            continue

        func_name = finding.get("desc", "").split("'")[1] if "'" in finding.get("desc", "") else ""
        if not func_name:
            continue

        file_path = finding.get("file", "")
        full_path = project_root / file_path
        if not full_path.exists():
            continue

        source = full_path.read_text(encoding="utf-8", errors="replace")

        for func_node in repo_map.nodes:
            if func_node.type != NodeType.FUNCTION and func_node.type != NodeType.CLASS:
                continue
            if func_node.path != file_path:
                continue
            short_name = func_node.name.split(".")[-1]
            if short_name != func_name:
                continue

            lines = source.splitlines()
            func_source = "\n".join(lines[func_node.line_start - 1:func_node.line_end])

            questions.append(TargetedQuestion(
                file_path=file_path,
                question=f"Is function '{func_name}' called via a framework mechanism (e.g., FastAPI Depends(), decorator registration, or event handler) rather than a direct function call?",
                code_snippet=func_source,
                start_line=func_node.line_start,
                end_line=func_node.line_end,
                source_signal=f"XREF_IMPORTED_NOT_CALLED: {func_name}",
                bug_if_no=f"'{func_name}' is imported but never invoked — dead import chain",
            ))
            break

    return questions


def _questions_from_soft_delete(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    questions = []

    files_with_rules_table = []
    for node in repo_map.nodes:
        if node.type == NodeType.FILE:
            for table in node.tables:
                if "rules" in table.lower() or "ada_rules" in table.lower():
                    files_with_rules_table.append(node)
                    break

    for node in files_with_rules_table:
        file_path = project_root / node.path
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for ast_node in ast.walk(tree):
            if not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            func_source_lines = source.splitlines()[ast_node.lineno - 1:(ast_node.end_lineno or ast_node.lineno)]
            func_source = "\n".join(func_source_lines)

            if "ada_rules" in func_source.lower() or "rule" in ast_node.name.lower():
                if "query" in func_source.lower() or "filter" in func_source.lower() or "select" in func_source.lower():
                    questions.append(TargetedQuestion(
                        file_path=node.path,
                        question=f"In function '{ast_node.name}', when querying rules from the database, does the query include a filter for 'deleted_at IS NULL' or 'deleted_at == None' to exclude soft-deleted records?",
                        code_snippet=func_source,
                        start_line=ast_node.lineno,
                        end_line=ast_node.end_lineno or ast_node.lineno,
                        source_signal="soft-delete table 'ada_rules' queried without deleted_at check",
                        bug_if_no="Query returns soft-deleted rules — deleted rules still applied during ADA checks",
                    ))

    return questions


def _questions_from_loaded_fields(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    questions = []

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue

        file_path = project_root / node.path
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for ast_node in ast.walk(tree):
            if not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            func_lines = source.splitlines()[ast_node.lineno - 1:(ast_node.end_lineno or ast_node.lineno)]
            func_source = "\n".join(func_lines)

            if "expires" in func_source.lower() and ("datetime.now" in func_source or "datetime.utcnow" in func_source):
                if "timedelta" not in func_source:
                    questions.append(TargetedQuestion(
                        file_path=node.path,
                        question=f"In function '{ast_node.name}', when setting an expiration time (e.g., invite_expires, token_expires), is the expiration set to a FUTURE time (now + timedelta) rather than the current time (now)?",
                        code_snippet=func_source,
                        start_line=ast_node.lineno,
                        end_line=ast_node.end_lineno or ast_node.lineno,
                        source_signal="expiration field set with datetime.now but no timedelta offset",
                        bug_if_no="Expiration set to current time — tokens/invites expire instantly",
                    ))

    return questions


def _ask_question(q: TargetedQuestion, model: str, url: str) -> Optional[dict]:
    prompt = QUESTION_PROMPT.format(
        question=q.question,
        file_path=q.file_path,
        start_line=q.start_line,
        end_line=q.end_line,
        code_snippet=q.code_snippet[:3000],
    )

    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 256,
                },
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None

        response = resp.json().get("response", "").strip()

        json_start = response.find("{")
        json_end = response.rfind("}") + 1
        if json_start == -1 or json_end <= json_start:
            return None

        return json.loads(response[json_start:json_end])

    except Exception as e:
        logger.debug(f"Question failed: {e}")
        return None


def _check_ollama(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
