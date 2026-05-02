"""Diff mode — compare current repo-map findings against a baseline.

Usage from CLI:
    code-mapper /path --lint --diff baseline.json
    code-mapper /path --lint --diff HEAD~1   # auto-loads baseline from git

Output: only NEW findings (introduced since baseline) + summary of resolved.
"""
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _finding_key(f: dict) -> tuple:
    """Stable key for a finding so identical findings across runs match."""
    return (
        f.get("rule") or f.get("type") or "?",
        f.get("file_path") or f.get("file") or "?",
        f.get("line") or 0,
        (f.get("desc") or f.get("message") or "")[:120],
    )


def _collect_findings(repo_map_dict: dict) -> list[dict]:
    """Pull every finding from every tier of a serialized repo-map."""
    out: list[dict] = []
    stats = repo_map_dict.get("stats", {})
    for key in ("lint_findings", "ai_findings", "verified_findings",
                "claude_findings", "pattern_findings"):
        out.extend(stats.get(key, []))
    xref = stats.get("xref")
    if xref and isinstance(xref, dict):
        out.extend(xref.get("findings", []))
    return out


def load_baseline(spec: str, project_root: Path) -> dict | None:
    """Load baseline repo-map. spec can be:
      - a file path to a saved repo-map.json
      - a git ref ('HEAD~1', 'main', a SHA) — pulls repo-map.json from git
      - 'auto' — tries last-good baseline at .codemapper-baseline.json
    """
    if spec == "auto":
        path = project_root / ".codemapper-baseline.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    p = Path(spec)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    # Try as git ref
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{spec}:repo-map.json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load baseline from git ref {spec}: {e}")

    logger.warning(f"Baseline {spec} not found (no file, no git ref). Skipping diff.")
    return None


def diff_findings(current: list[dict], baseline: list[dict]) -> dict:
    """Return {new, resolved, unchanged} bucketed by stable finding key."""
    cur_keys = {_finding_key(f): f for f in current}
    base_keys = {_finding_key(f): f for f in baseline}
    new = [f for k, f in cur_keys.items() if k not in base_keys]
    resolved = [f for k, f in base_keys.items() if k not in cur_keys]
    unchanged_n = sum(1 for k in cur_keys if k in base_keys)
    return {
        "new": new, "resolved": resolved,
        "n_new": len(new), "n_resolved": len(resolved),
        "n_unchanged": unchanged_n,
    }


def by_severity(findings: list[dict]) -> dict:
    """Bucket findings by severity for compact summary."""
    out = {"high": 0, "med": 0, "low": 0, "other": 0}
    for f in findings:
        sev = (f.get("severity") or "").lower()
        if sev in out:
            out[sev] += 1
        else:
            out["other"] += 1
    return out


def print_diff_report(d: dict, max_show: int = 25) -> None:
    """Compact human-readable diff report."""
    n_new, n_resolved, n_unchanged = d["n_new"], d["n_resolved"], d["n_unchanged"]
    print()
    print("=" * 70)
    print("  CODE MAPPER — DIFF vs baseline")
    print("=" * 70)
    print(f"  NEW issues:       {n_new}")
    print(f"  RESOLVED issues:  {n_resolved}")
    print(f"  Unchanged:        {n_unchanged}")
    if n_new:
        s = by_severity(d["new"])
        print(f"  NEW by severity:  high={s['high']}  med={s['med']}  low={s['low']}")
    if n_resolved:
        s = by_severity(d["resolved"])
        print(f"  RESOLVED by sev:  high={s['high']}  med={s['med']}  low={s['low']}")

    if n_new:
        print()
        print("--- NEW ISSUES ---")
        # Sort: high > med > low, then file path
        rank = {"high": 3, "med": 2, "low": 1}
        sorted_new = sorted(
            d["new"],
            key=lambda f: (-rank.get((f.get("severity") or "low").lower(), 0),
                           f.get("file_path") or f.get("file") or "")
        )
        for f in sorted_new[:max_show]:
            sev = f.get("severity") or "?"
            file_p = f.get("file_path") or f.get("file") or "?"
            line = f.get("line") or 0
            rule = f.get("rule") or f.get("type") or "?"
            desc = f.get("desc") or f.get("message") or ""
            print(f"  [{sev}] {file_p}:{line} {rule}: {desc}")
        if len(sorted_new) > max_show:
            print(f"  ... ({len(sorted_new) - max_show} more)")

    if n_resolved:
        print()
        print(f"--- RESOLVED ({n_resolved}) — nice work ---")
        for f in d["resolved"][:5]:
            file_p = f.get("file_path") or f.get("file") or "?"
            line = f.get("line") or 0
            rule = f.get("rule") or "?"
            print(f"  {file_p}:{line} {rule}")
        if n_resolved > 5:
            print(f"  ... ({n_resolved - 5} more)")
    print()


def run_diff(repo_map, baseline_spec: str, project_root: Path) -> int:
    """Entry point from cli. Returns 0 if no NEW HIGH issues, else 1."""
    # Convert RepoMap to dict for finding extraction
    if hasattr(repo_map, "to_dict"):
        cur_dict = repo_map.to_dict()
    elif hasattr(repo_map, "stats"):
        cur_dict = {"stats": repo_map.stats}
    else:
        cur_dict = repo_map

    baseline = load_baseline(baseline_spec, project_root)

    # Always update the rolling baseline cache so the next `--diff auto`
    # run has something to compare against.
    try:
        (project_root / ".codemapper-baseline.json").write_text(
            json.dumps(cur_dict, default=str, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning(f"Could not write baseline cache: {e}")

    if baseline is None:
        print()
        print(f"  [diff] No baseline at '{baseline_spec}' — initialized for next run.")
        return 0

    cur_findings = _collect_findings(cur_dict)
    base_findings = _collect_findings(baseline)
    d = diff_findings(cur_findings, base_findings)
    print_diff_report(d)

    # Exit non-zero if any NEW HIGH-severity issue (regression gate)
    sev_counts = by_severity(d["new"])
    return 1 if sev_counts["high"] > 0 else 0
