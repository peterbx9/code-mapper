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
                        ollama_url: str = "http://127.0.0.1:11434",
                        todo_path: Path = None) -> list[dict]:
    project_root = project_root.resolve()

    questions = _generate_questions(project_root, repo_map, xref_data, todo_path)
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
                        xref_data: dict = None,
                        todo_path: Path = None) -> list[TargetedQuestion]:
    questions = []

    has_connectivity = any(
        n.connectivity != ConnectivityStatus.REACHABLE
        for n in repo_map.nodes if n.type == NodeType.FILE
    )
    if not has_connectivity:
        from .connectivity import analyze_connectivity
        analyze_connectivity(repo_map)

    questions.extend(_questions_from_connectivity(project_root, repo_map))
    questions.extend(_questions_from_xref(project_root, repo_map, xref_data))
    questions.extend(_questions_from_soft_delete(project_root, repo_map))
    questions.extend(_questions_from_loaded_fields(project_root, repo_map))
    questions.extend(_questions_from_temporal_ordering(project_root, repo_map))
    questions.extend(_questions_from_constraint_enforcement(project_root, repo_map))
    if todo_path:
        questions.extend(_questions_from_doc_vs_code(project_root, repo_map, todo_path))

    return questions


def _questions_from_connectivity(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    questions = []

    incomplete_files = [n for n in repo_map.nodes
                        if n.type == NodeType.FILE
                        and n.connectivity == ConnectivityStatus.INCOMPLETE]

    for node in incomplete_files:
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
            if func_node.name.startswith("_") and func_node.name != "__init__":
                continue

            func_source = "\n".join(lines[func_node.line_start - 1:func_node.line_end])
            if len(func_source) < 20:
                continue

            questions.append(TargetedQuestion(
                file_path=node.path,
                question=f"In function '{func_node.name}', does it produce any observable effect — writing to a database, returning data to a caller, modifying a file, or sending a network request? Or does it load/compute data that is never actually used?",
                code_snippet=func_source,
                start_line=func_node.line_start,
                end_line=func_node.line_end,
                source_signal=f"INCOMPLETE wiring — {node.path} is reachable but has no path to an effect",
                bug_if_no=f"Function '{func_node.name}' in {node.path} loads/computes data but never produces an effect — dead logic chain",
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

    soft_delete_tables = set()
    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        file_path = project_root / node.path
        if not file_path.exists():
            continue
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            if "deleted_at" in source and "__tablename__" in source:
                for table in node.tables:
                    if not table.startswith("rel:") and "via" not in table:
                        soft_delete_tables.add(table)
        except Exception:
            continue

    if not soft_delete_tables:
        return questions

    logger.debug(f"Soft-delete tables found: {soft_delete_tables}")

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        has_query_keyword = any(kw in source.lower() for kw in ["query", "filter", "select", "execute", ".all()", ".first()"])
        references_soft_table = any(t in source.lower() for t in soft_delete_tables)

        if not (has_query_keyword and references_soft_table):
            continue
        if "deleted_at" in source:
            continue

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for ast_node in ast.walk(tree):
            if not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            func_lines = source.splitlines()[ast_node.lineno - 1:(ast_node.end_lineno or ast_node.lineno)]
            func_source = "\n".join(func_lines)

            if not any(t in func_source.lower() for t in soft_delete_tables):
                continue
            if not any(kw in func_source.lower() for kw in ["query", "filter", "select", "execute", ".all()", ".first()"]):
                continue

            matched_tables = [t for t in soft_delete_tables if t in func_source.lower()]
            questions.append(TargetedQuestion(
                file_path=node.path,
                question=f"In function '{ast_node.name}', when querying from tables that support soft-delete ({', '.join(matched_tables)}), does the query include a filter for 'deleted_at IS NULL' or 'deleted_at == None' to exclude soft-deleted records?",
                code_snippet=func_source,
                start_line=ast_node.lineno,
                end_line=ast_node.end_lineno or ast_node.lineno,
                source_signal=f"File queries soft-delete table(s) {matched_tables} but does not reference deleted_at",
                bug_if_no=f"Query returns soft-deleted records from {matched_tables} — deleted data still active",
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


def _questions_from_temporal_ordering(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    """Detect db.commit() before async completion, close() before flush, etc."""
    questions = []

    ASYNC_PATTERNS = ["ensure_future", "create_task", "asyncio.gather", "run_in_executor"]
    COMMIT_PATTERNS = ["db.commit()", "session.commit()", ".commit()"]
    CLOSE_PATTERNS = [".close()", "db.close()", "session.close()"]

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        file_path = project_root / node.path
        if not file_path.exists():
            continue

        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        has_async = any(p in source for p in ASYNC_PATTERNS)
        has_commit = any(p in source for p in COMMIT_PATTERNS)
        has_close = any(p in source for p in CLOSE_PATTERNS)

        if not ((has_async and has_commit) or (has_async and has_close)):
            continue

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for ast_node in ast.walk(tree):
            if not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            func_lines = source.splitlines()[ast_node.lineno - 1:(ast_node.end_lineno or ast_node.lineno)]
            func_source = "\n".join(func_lines)

            func_has_async = any(p in func_source for p in ASYNC_PATTERNS)
            func_has_commit = any(p in func_source for p in COMMIT_PATTERNS)
            func_has_close = any(p in func_source for p in CLOSE_PATTERNS)

            if func_has_async and func_has_commit:
                questions.append(TargetedQuestion(
                    file_path=node.path,
                    question=f"In function '{ast_node.name}', is db.commit() or session.commit() called AFTER the async operation (ensure_future/create_task/gather) has completed? Or does the commit happen BEFORE the async result is available?",
                    code_snippet=func_source,
                    start_line=ast_node.lineno,
                    end_line=ast_node.end_lineno or ast_node.lineno,
                    source_signal="Function has both async dispatch and db.commit — temporal ordering risk",
                    bug_if_no="db.commit() runs before async task completes — database writes from the async task are silently lost",
                ))

            if func_has_async and func_has_close:
                questions.append(TargetedQuestion(
                    file_path=node.path,
                    question=f"In function '{ast_node.name}', is the resource (db/session/connection) closed AFTER the async operation has completed? Or does close() happen while the async task is still running?",
                    code_snippet=func_source,
                    start_line=ast_node.lineno,
                    end_line=ast_node.end_lineno or ast_node.lineno,
                    source_signal="Function has both async dispatch and resource close — temporal ordering risk",
                    bug_if_no="Resource closed before async task completes — async task operates on closed connection",
                ))

    return questions


def _questions_from_constraint_enforcement(project_root: Path, repo_map: RepoMap) -> list[TargetedQuestion]:
    """When a constraint (e.g., 150-char cap) is enforced in one file, check sibling consumers."""
    questions = []

    CONSTRAINT_PATTERNS = [
        {"search": "[:147]", "desc": "150-char truncation", "field": "alt.text|alt_text"},
        {"search": "[:125]", "desc": "125-char truncation", "field": "alt.text|alt_text"},
        {"search": "max_length", "desc": "max_length constraint", "field": None},
    ]

    enforcer_files = {}

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            continue
        file_path = project_root / node.path
        if not file_path.exists():
            continue
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for pattern in CONSTRAINT_PATTERNS:
            if pattern["search"] in source:
                key = pattern["desc"]
                if key not in enforcer_files:
                    enforcer_files[key] = []
                enforcer_files[key].append(node.path)

    for constraint_desc, enforcer_paths in enforcer_files.items():
        for node in repo_map.nodes:
            if node.type != NodeType.FILE:
                continue
            if node.path in enforcer_paths:
                continue

            file_path = project_root / node.path
            if not file_path.exists():
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for cp in CONSTRAINT_PATTERNS:
                if cp["desc"] != constraint_desc:
                    continue
                if not cp["field"]:
                    continue

                field_patterns = cp["field"].split("|")
                if not any(fp in source.lower() for fp in field_patterns):
                    continue

                has_write = any(kw in source.lower() for kw in [
                    "pikepdf", "write", "save", "set_alt", "apply", "remediat"
                ])
                if not has_write:
                    continue

                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    continue

                for ast_node in ast.walk(tree):
                    if not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    func_lines = source.splitlines()[ast_node.lineno - 1:(ast_node.end_lineno or ast_node.lineno)]
                    func_source = "\n".join(func_lines)

                    if not any(fp in func_source.lower() for fp in field_patterns):
                        continue

                    questions.append(TargetedQuestion(
                        file_path=node.path,
                        question=f"In function '{ast_node.name}', is the {constraint_desc} enforced before writing/applying the value? (It IS enforced in {', '.join(enforcer_paths)} but this file also handles the same data.)",
                        code_snippet=func_source,
                        start_line=ast_node.lineno,
                        end_line=ast_node.end_lineno or ast_node.lineno,
                        source_signal=f"{constraint_desc} enforced in {enforcer_paths} but {node.path} also writes this field",
                        bug_if_no=f"{constraint_desc} not enforced in {node.path} — constraint bypassed when data flows through this path",
                    ))
                break

    return questions


def _questions_from_doc_vs_code(project_root: Path, repo_map: RepoMap,
                                todo_path: Path) -> list[TargetedQuestion]:
    """Parse a TODO/changelog doc for items marked DONE, verify each claim against code."""
    questions = []

    if not todo_path.exists():
        return questions

    try:
        todo_content = todo_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return questions

    CLAIM_PATTERNS = [
        {"claim": "MAX_RETRIES", "search_in": "link_checker", "question": "Does this file contain a MAX_RETRIES constant or retry loop with exponential backoff?"},
        {"claim": "Semaphore", "search_in": "link_checker", "question": "Does this file use asyncio.Semaphore to limit concurrent requests?"},
        {"claim": "deleted_at", "search_in": "ada_checker", "question": "Does this file filter queries using 'deleted_at IS NULL' or 'deleted_at == None'?"},
        {"claim": "cli.py", "search_in": "cli", "question": "Does this file exist and contain init-db, seed, and create-admin commands?"},
    ]

    done_items = []
    for line in todo_content.splitlines():
        if "**DONE**" in line or "✅" in line or "FIXED" in line:
            done_items.append(line.strip())

    for cp in CLAIM_PATTERNS:
        claimed_done = any(cp["claim"].lower() in item.lower() for item in done_items)
        if not claimed_done:
            continue

        target_file = None
        for node in repo_map.nodes:
            if node.type == NodeType.FILE and cp["search_in"] in node.path:
                target_file = node
                break

        if not target_file:
            questions.append(TargetedQuestion(
                file_path=f"(missing: *{cp['search_in']}*)",
                question=cp["question"],
                code_snippet="FILE NOT FOUND",
                start_line=0,
                end_line=0,
                source_signal=f"TODO claims '{cp['claim']}' is DONE but target file not found",
                bug_if_no=f"TODO regression: '{cp['claim']}' claimed done but file doesn't exist",
            ))
            continue

        file_path = project_root / target_file.path
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")

        questions.append(TargetedQuestion(
            file_path=target_file.path,
            question=cp["question"],
            code_snippet=source[:3000],
            start_line=1,
            end_line=min(len(source.splitlines()), 100),
            source_signal=f"TODO claims '{cp['claim']}' is DONE — verifying against code",
            bug_if_no=f"TODO regression: '{cp['claim']}' claimed done but not found in code",
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
