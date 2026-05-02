"""Hotspot detector — files with high complexity AND high git churn = bug factories.

Standard formula (Tornhill 2015): hotspot_score = normalize(complexity) ×
normalize(churn). Both 0-1. Top of the list = audit priority.

Churn = number of commits touching the file in the lookback window.
Complexity = sum of cyclomatic complexity across functions in the file
(plus a +1 base for non-empty file).

Usage:
    code-mapper /path --hotspots                    # last 180 days
    code-mapper /path --hotspots --hotspot-days 90  # last 90 days
"""
from __future__ import annotations
import logging
import subprocess
from collections import Counter
from pathlib import Path

from .schema import NodeType, RepoMap

logger = logging.getLogger(__name__)


def _git_churn(project_root: Path, lookback_days: int = 180) -> Counter:
    """Returns Counter[relative_path] = #commits touching the file.
    Empty Counter if not a git repo or git missing."""
    counter: Counter = Counter()
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "log",
             f"--since={lookback_days}.days", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.debug(f"git log failed (rc={result.returncode}): {result.stderr[:200]}")
            return counter
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f"git not available: {e}")
        return counter

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".py"):
            continue
        # Normalize path separators
        counter[line.replace("\\", "/")] += 1
    return counter


def _file_complexity(repo_map: RepoMap) -> dict[str, int]:
    """Sum cyclomatic complexity per file from function nodes."""
    out: dict[str, int] = {}
    file_paths = {n.id: n.path for n in repo_map.nodes if n.type == NodeType.FILE}
    for n in repo_map.nodes:
        if n.type == NodeType.FUNCTION:
            # parent_id may point to file or class; resolve to file
            file_id = n.parent_id
            while file_id and file_id in {x.id: x for x in repo_map.nodes}:
                node = next((x for x in repo_map.nodes if x.id == file_id), None)
                if not node or node.type == NodeType.FILE:
                    break
                file_id = node.parent_id
            if file_id and file_id in file_paths:
                out[file_paths[file_id]] = out.get(file_paths[file_id], 0) + max(1, n.complexity)
    # Files with 0 functions but non-empty get a base 1
    for fid, fpath in file_paths.items():
        out.setdefault(fpath, 1)
    return out


def _normalize(values: dict[str, int | float]) -> dict[str, float]:
    if not values:
        return {}
    mx = max(values.values()) or 1
    return {k: v / mx for k, v in values.items()}


def find_hotspots(repo_map: RepoMap, project_root: Path,
                   lookback_days: int = 180) -> list[dict]:
    churn = _git_churn(project_root, lookback_days)
    complexity = _file_complexity(repo_map)
    if not churn:
        logger.info("No git churn data — hotspot scoring will be complexity-only")
    nrm_churn = _normalize(dict(churn))
    nrm_cx = _normalize(complexity)

    rows = []
    paths = set(complexity) | set(churn)
    for p in paths:
        c = nrm_cx.get(p, 0.0)
        ch = nrm_churn.get(p, 0.0)
        score = c * ch if churn else c
        rows.append({
            "file_path": p,
            "complexity": complexity.get(p, 0),
            "churn": churn.get(p, 0),
            "complexity_norm": round(c, 3),
            "churn_norm": round(ch, 3),
            "score": round(score, 3),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def print_hotspot_report(rows: list[dict], top_n: int = 20) -> None:
    if not rows:
        print()
        print("=== HOTSPOTS ===")
        print("  No files to score.")
        return
    print()
    print(f"=== HOTSPOTS — top {min(top_n, len(rows))} (complexity × churn) ===")
    print(f"  {'rank':>4}  {'score':>6}  {'cx':>4}  {'churn':>6}  file")
    print(f"  {'-'*4}  {'-'*6}  {'-'*4}  {'-'*6}  {'-'*40}")
    for i, r in enumerate(rows[:top_n], 1):
        print(f"  {i:>4}  {r['score']:>6.3f}  {r['complexity']:>4}  "
              f"{r['churn']:>6}  {r['file_path']}")
