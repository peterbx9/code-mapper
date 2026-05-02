"""
Code Mapper CLI — point at any Python project, get a repo-map.json.

Usage:
    python -m code_mapper /path/to/project
    python -m code_mapper /path/to/project --output my-map.json
    python -m code_mapper /path/to/project --verbose
    python -m code_mapper /path/to/project --lint --xref
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .assembler import assemble_map, load_config
from .clustering import cluster_logic_blocks
from .connectivity import analyze_connectivity
from .linter import lint_project
from .schema import NodeType
from .xref import build_xref


_SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}


def _collect_all_findings(repo_map) -> list:
    """Gather every finding from every tier for --fail-on + exit-code logic."""
    out = []
    out.extend(repo_map.stats.get("lint_findings", []))
    xref = repo_map.stats.get("xref")
    if xref and "findings" in xref:
        out.extend(xref["findings"])
    out.extend(repo_map.stats.get("ai_findings", []))
    out.extend(repo_map.stats.get("verified_findings", []))
    out.extend(repo_map.stats.get("claude_findings", []))
    return out


def _exit_code_for_findings(repo_map, fail_on: str | None) -> int:
    """0 if no fail-on threshold or no findings at/above threshold. 1 otherwise."""
    if not fail_on:
        return 0
    threshold = _SEVERITY_RANK[fail_on]
    for f in _collect_all_findings(repo_map):
        sev = (f.get("severity") or "").lower()
        if _SEVERITY_RANK.get(sev, 0) >= threshold:
            return 1
    return 0


def _run_ai(project_root, repo_map, args):
    """Tier 2 open-ended AI review (older, kept for exploration)."""
    from .ai_reviewer import review_project
    ai_model = args.model or "qwen2.5-coder:7b"
    xref_data_for_ai = repo_map.stats.get("xref") if args.xref else None
    lint_for_ai = repo_map.stats.get("lint_findings") if args.lint else None

    mode = "triage + deep-dive" if args.deep else "triage only"
    print(f"\n  AI REVIEW ({mode}, model: {ai_model}):")
    ai_findings = review_project(
        project_root, repo_map,
        model=ai_model,
        ollama_url=args.ollama_url,
        xref_data=xref_data_for_ai,
        lint_findings=lint_for_ai,
        deep=args.deep,
        deep_model=args.deep_model,
    )
    if not ai_findings:
        print("  AI: no findings")
        return
    triage = [f for f in ai_findings if f.get("source") == "ai_triage"]
    deep = [f for f in ai_findings if f.get("source") == "ai_deep"]
    print(f"  TRIAGE FINDINGS ({len(triage)}):")
    for f in triage:
        _print_finding(f)
    if deep:
        print(f"  DEEP FINDINGS ({len(deep)}):")
        for f in deep:
            _print_finding(f)
    repo_map.stats["ai_findings"] = ai_findings


def _run_verify(project_root, repo_map, args):
    """Tier 2 v3 targeted yes/no questioner via Ollama 7B."""
    from .questioner import generate_and_verify
    verify_model = args.model or "qwen2.5-coder:7b"
    xref_data_for_verify = repo_map.stats.get("xref") if args.xref else None
    ollama = args.ollama_url or "http://127.0.0.1:11434"

    print(f"\n  TARGETED VERIFICATION (model: {verify_model}):")
    todo_file = Path(args.todo) if args.todo else None
    verify_findings = generate_and_verify(
        project_root, repo_map,
        xref_data=xref_data_for_verify,
        model=verify_model,
        ollama_url=ollama,
        todo_path=todo_file,
    )
    if not verify_findings:
        print("  Verification: all checks passed")
        return
    print(f"  VERIFIED BUGS ({len(verify_findings)}):")
    for f in verify_findings:
        print(f"    [HIGH] {f['file']}:{f['line']} — {f['bug_desc']}")
        if f.get("evidence"):
            print(f"           Evidence: {f['evidence']}")
        if f.get("signal"):
            print(f"           Signal: {f['signal']}")
    repo_map.stats["verified_findings"] = verify_findings


def _run_claude(project_root, repo_map, args):
    """Tier 3 Claude API cross-file synthesis."""
    from .deep_reviewer import deep_review
    all_prior = []
    all_prior.extend(repo_map.stats.get("lint_findings", []))
    if "xref" in repo_map.stats and "findings" in repo_map.stats["xref"]:
        all_prior.extend(repo_map.stats["xref"]["findings"])
    all_prior.extend(repo_map.stats.get("verified_findings", []))
    all_prior.extend(repo_map.stats.get("ai_findings", []))

    flagged = [n.path for n in repo_map.nodes
               if n.type == NodeType.FILE
               and n.connectivity.value in ("unreachable", "incomplete")]

    todo_content = None
    if args.todo:
        todo_file = Path(args.todo)
        if todo_file.exists():
            todo_content = todo_file.read_text(encoding="utf-8", errors="replace")

    print(f"\n  CLAUDE DEEP REVIEW (Tier 3):")
    claude_findings = deep_review(
        project_root, repo_map,
        prior_findings=all_prior,
        flagged_files=flagged,
        todo_content=todo_content,
    )
    if not claude_findings:
        print("  Claude: no additional findings")
        return
    print(f"  CLAUDE FINDINGS ({len(claude_findings)}):")
    for f in claude_findings:
        _print_finding(f)
    repo_map.stats["claude_findings"] = claude_findings


def _validate_project_root(project_arg: str) -> Path:
    """Resolve + validate the project path. Exits on any error."""
    project_root = Path(project_arg).resolve()
    if not project_root.exists():
        print(f"Error: {project_root} does not exist", file=sys.stderr)
        sys.exit(1)
    if not project_root.is_dir():
        print(f"Error: {project_root} is not a directory", file=sys.stderr)
        sys.exit(1)
    return project_root


def _validate_output_dir(output_arg: str | None):
    """Pre-flight the output dir before running expensive tiers."""
    if not output_arg:
        return
    out_parent = Path(output_arg).parent
    if out_parent and not out_parent.exists():
        print(f"Error: output directory {out_parent} does not exist", file=sys.stderr)
        sys.exit(1)


def _load_user_config(args, project_root: Path) -> dict:
    """--config path takes priority; else load_config from project root; else {}."""
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Warning: config file {config_path} not found, using defaults",
                  file=sys.stderr)
            return {}
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"Warning: {config_path} unreadable ({e}) — using defaults",
                  file=sys.stderr)
            return {}
    return load_config(project_root) or {}


def _print_map_summary(repo_map):
    print(f"  Files: {repo_map.stats.get('files', 0)}")
    print(f"  Classes: {repo_map.stats.get('classes', 0)}")
    print(f"  Functions: {repo_map.stats.get('functions', 0)}")
    print(f"  Stubs: {repo_map.stats.get('stubs', 0)}")
    print(f"  Edges: {repo_map.stats.get('edges', 0)}")
    print(f"  Tables: {', '.join(repo_map.stats.get('tables', []))}")


def _run_cluster(repo_map, config):
    resolution = (config or {}).get("clustering", {}).get("resolution", 1.0)
    blocks = cluster_logic_blocks(repo_map, resolution=resolution)
    repo_map.logic_blocks = blocks
    print(f"  Logic blocks: {len(blocks)}")
    for b in blocks:
        print(f"    [{b.id}] {b.name} ({len(b.node_ids)} files)")


def _run_connectivity(repo_map):
    conn = analyze_connectivity(repo_map)
    repo_map.stats["connectivity"] = conn
    unreachable = conn.get("unreachable", [])
    incomplete = conn.get("incomplete", [])
    if unreachable:
        print(f"  UNREACHABLE ({len(unreachable)}):")
        for u in unreachable:
            print(f"    {u}")
    if incomplete:
        print(f"  INCOMPLETE WIRING ({len(incomplete)}):")
        for inc in incomplete:
            print(f"    {inc}")
    cycles = conn.get("circular_dependencies", [])
    if cycles:
        print(f"  CIRCULAR DEPENDENCIES ({len(cycles)}):")
        for cycle in cycles:
            print(f"    {' → '.join(cycle)}")


def _run_lint(project_root, repo_map, config):
    from .assembler import DEFAULT_EXCLUDE
    exclude = DEFAULT_EXCLUDE | set((config or {}).get("exclude", []))
    lint_findings = lint_project(project_root, repo_map, exclude_dirs=exclude)
    if not lint_findings:
        print("  Lint: clean")
        return
    print(f"  LINT FINDINGS ({len(lint_findings)}):")
    for f in lint_findings:
        print(f"    [{f.severity}] {f.file_path}:{f.line} {f.rule}: {f.desc}")
    repo_map.stats["lint_findings"] = [f.to_dict() for f in lint_findings]


def _run_xref(project_root, repo_map, config):
    from .assembler import DEFAULT_EXCLUDE
    exclude = DEFAULT_EXCLUDE | set((config or {}).get("exclude", []))
    xref = build_xref(project_root, repo_map, exclude_dirs=exclude)
    xref_data = xref.to_dict()
    stats = xref_data["stats"]
    print(f"  XREF: {stats['total_symbols']} symbols, {stats['unused']} unused")
    if xref.findings:
        print(f"  XREF FINDINGS ({len(xref.findings)}):")
        for f in xref.findings:
            print(f"    [{f['severity']}] {f['file']}:{f['line']} {f['rule']}: {f['desc']}")
    if stats["most_referenced"]:
        print(f"  Most referenced:")
        for m in stats["most_referenced"]:
            print(f"    {m['name']} ({m['usage_count']}x) — {m['defined_in']}")
    repo_map.stats["xref"] = xref_data


def _write_repo_map(repo_map, output_arg: str | None, project_root: Path):
    """Atomic write: tmp + rename avoids a half-written file on crash/interrupt."""
    output_path = Path(output_arg) if output_arg else project_root / "repo-map.json"
    tmp_output = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        tmp_output.write_text(repo_map.to_json(), encoding="utf-8")
        import os as _os
        _os.replace(tmp_output, output_path)
    except OSError as e:
        print(f"\nError: failed to write {output_path}: {e}", file=sys.stderr)
        try:
            tmp_output.unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(1)
    print(f"\nMap written to: {output_path}")


def _print_finding(f: dict):
    """Uniform one-line print for a Tier 2/3 finding dict."""
    sev = f.get("severity", "?")
    fp = f.get("file", "?")
    ln = f.get("line", "?")
    cat = f.get("category", "?")
    desc = f.get("desc") or f.get("bug_desc") or "?"
    print(f"    [{sev}] {fp}:{ln} [{cat}]: {desc}")
    evidence = f.get("evidence")
    if evidence:
        print(f"           Evidence: {evidence}")


def main():
    parser = argparse.ArgumentParser(
        prog="code-mapper",
        description="Parse a Python project into a structured repo-map.json",
    )
    parser.add_argument("project", help="Path to project root")
    parser.add_argument("--output", "-o", default=None, help="Output file (default: repo-map.json in project root)")
    parser.add_argument("--config", "-c", default=None, help="Path to .codemapper.json (default: project root)")
    # --force was declared but never implemented (no cache layer exists).
    # Removed to stop promising a feature that doesn't work.
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--no-cluster", action="store_true", help="Skip logic block clustering")
    parser.add_argument("--no-connectivity", action="store_true", help="Skip connectivity analysis")
    parser.add_argument("--lint", action="store_true", help="Run Tier 1 AST lint rules")
    parser.add_argument("--xref", action="store_true", help="Build cross-reference symbol table (Tier 1.5)")
    parser.add_argument("--ai", action="store_true", help="Tier 2: AI review per file via Ollama")
    parser.add_argument("--deep", action="store_true", help="Tier 2 deep: 7B triage + 32B deep-dive on flagged files")
    parser.add_argument("--claude", action="store_true", help="Tier 3: Claude API deep review of flagged files")
    parser.add_argument("--verify", action="store_true", help="Tier 2 v3: targeted yes/no questions from structural signals")
    parser.add_argument("--todo", default=None, help="Path to TODO/changelog doc for doc-vs-code verification")
    parser.add_argument("--model", default=None, help="Ollama triage model (default: qwen2.5-coder:7b)")
    parser.add_argument("--deep-model", default=None, help="Ollama deep model (default: qwen2.5-coder:32b)")
    parser.add_argument("--ollama-url", default=None, help="Ollama API URL (default: http://127.0.0.1:11434)")
    parser.add_argument(
        "--fail-on", choices=["low", "med", "high"], default=None,
        help="Exit non-zero if any finding at this severity or higher. For CI integration.",
    )
    parser.add_argument(
        "--diff", default=None, metavar="BASELINE",
        help="Compare findings against a baseline. BASELINE can be a path "
             "to a saved repo-map.json, a git ref (HEAD~1, main, SHA), or "
             "'auto' to use .codemapper-baseline.json.",
    )
    parser.add_argument(
        "--html", nargs="?", const="repo-map-report.html", default=None,
        metavar="OUT",
        help="Render an HTML report. Default output: repo-map-report.html",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Auto-fix DEAD_IMPORT and UNUSED_PARAM lint findings.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Used with --fix to preview changes without writing files.",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    project_root = _validate_project_root(args.project)
    _validate_output_dir(args.output)
    config = _load_user_config(args, project_root)

    print(f"Mapping: {project_root}")
    repo_map = assemble_map(project_root, config)
    _print_map_summary(repo_map)

    if not args.no_cluster:
        _run_cluster(repo_map, config)
    if not args.no_connectivity:
        _run_connectivity(repo_map)

    custom_rules = (config or {}).get("rules", [])
    if custom_rules:
        from .pattern_rules import run_pattern_rules
        from .assembler import DEFAULT_EXCLUDE
        exclude = set(config.get("exclude", [])) | DEFAULT_EXCLUDE if config else DEFAULT_EXCLUDE
        pattern_findings = run_pattern_rules(project_root, custom_rules, exclude_dirs=exclude)
        if pattern_findings:
            print(f"  PATTERN RULES ({len(pattern_findings)}):")
            for f in pattern_findings:
                print(f"    [{f.severity}] {f.file_path}:{f.line} {f.rule}: {f.desc}")
            repo_map.stats["pattern_findings"] = [f.to_dict() for f in pattern_findings]

    if args.lint:
        _run_lint(project_root, repo_map, config)
    if args.xref:
        _run_xref(project_root, repo_map, config)

    if args.ai:
        _run_ai(project_root, repo_map, args)

    if args.verify:
        _run_verify(project_root, repo_map, args)

    if args.claude:
        _run_claude(project_root, repo_map, args)

    _write_repo_map(repo_map, args.output, project_root)

    # Optional post-processing modes
    if args.fix:
        from .autofix import apply_fixes, print_fix_report, FIXABLE_RULES
        all_findings = _collect_all_findings(repo_map)
        stats = apply_fixes(project_root, all_findings, dry_run=args.dry_run)
        print_fix_report(stats, dry_run=args.dry_run)

    # Run diff first so its result can be embedded in the HTML report
    diff_exit = 0
    diff_result = None
    if args.diff is not None:
        from .diff import run_diff_data
        diff_exit, diff_result = run_diff_data(repo_map, args.diff, project_root)

    if args.html is not None:
        from .html_report import write_html_report
        out_html = Path(args.html) if Path(args.html).is_absolute() else (project_root / args.html)
        write_html_report(repo_map, out_html, str(project_root), diff=diff_result)
        print(f"HTML report: {out_html}")

    fail_exit = _exit_code_for_findings(repo_map, args.fail_on)
    return diff_exit or fail_exit


if __name__ == "__main__":
    sys.exit(main() or 0)
