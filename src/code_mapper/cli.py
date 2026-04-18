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

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"Error: {project_root} does not exist", file=sys.stderr)
        sys.exit(1)
    if not project_root.is_dir():
        print(f"Error: {project_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Validate output dir is writable BEFORE running (potentially slow) tiers,
    # so --ai / --verify / --claude don't do expensive work and then fail at
    # the final write step.
    if args.output:
        out_parent = Path(args.output).parent
        if out_parent and not out_parent.exists():
            print(f"Error: output directory {out_parent} does not exist", file=sys.stderr)
            sys.exit(1)

    config = None
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"Warning: {config_path} unreadable ({e}) — using defaults",
                      file=sys.stderr)
                config = {}
        else:
            print(f"Warning: config file {config_path} not found, using defaults", file=sys.stderr)
            config = {}
    else:
        config = load_config(project_root)

    print(f"Mapping: {project_root}")
    repo_map = assemble_map(project_root, config)
    print(f"  Files: {repo_map.stats.get('files', 0)}")
    print(f"  Classes: {repo_map.stats.get('classes', 0)}")
    print(f"  Functions: {repo_map.stats.get('functions', 0)}")
    print(f"  Stubs: {repo_map.stats.get('stubs', 0)}")
    print(f"  Edges: {repo_map.stats.get('edges', 0)}")
    print(f"  Tables: {', '.join(repo_map.stats.get('tables', []))}")

    if not args.no_cluster:
        resolution = (config or {}).get("clustering", {}).get("resolution", 1.0)
        blocks = cluster_logic_blocks(repo_map, resolution=resolution)
        repo_map.logic_blocks = blocks
        print(f"  Logic blocks: {len(blocks)}")
        for b in blocks:
            print(f"    [{b.id}] {b.name} ({len(b.node_ids)} files)")

    if not args.no_connectivity:
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

    if args.lint:
        from .assembler import DEFAULT_EXCLUDE
        exclude = DEFAULT_EXCLUDE | set((config or {}).get("exclude", []))
        lint_findings = lint_project(project_root, repo_map, exclude_dirs=exclude)
        if lint_findings:
            print(f"  LINT FINDINGS ({len(lint_findings)}):")
            for f in lint_findings:
                print(f"    [{f.severity}] {f.file_path}:{f.line} {f.rule}: {f.desc}")
            repo_map.stats["lint_findings"] = [f.to_dict() for f in lint_findings]
        else:
            print("  Lint: clean")

    if args.xref:
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

    if args.ai:
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
        if ai_findings:
            triage = [f for f in ai_findings if f.get("source") == "ai_triage"]
            deep = [f for f in ai_findings if f.get("source") == "ai_deep"]
            print(f"  TRIAGE FINDINGS ({len(triage)}):")
            for f in triage:
                sev = f.get("severity", "?")
                fp = f.get("file", "?")
                ln = f.get("line", "?")
                cat = f.get("category", "?")
                desc = f.get("desc", "?")
                print(f"    [{sev}] {fp}:{ln} [{cat}]: {desc}")
            if deep:
                print(f"  DEEP FINDINGS ({len(deep)}):")
                for f in deep:
                    sev = f.get("severity", "?")
                    fp = f.get("file", "?")
                    ln = f.get("line", "?")
                    cat = f.get("category", "?")
                    desc = f.get("desc", "?")
                    print(f"    [{sev}] {fp}:{ln} [{cat}]: {desc}")
            repo_map.stats["ai_findings"] = ai_findings
        else:
            print("  AI: no findings")

    if args.verify:
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
        if verify_findings:
            print(f"  VERIFIED BUGS ({len(verify_findings)}):")
            for f in verify_findings:
                print(f"    [HIGH] {f['file']}:{f['line']} — {f['bug_desc']}")
                print(f"           Evidence: {f.get('evidence', '?')}")
                print(f"           Signal: {f['signal']}")
            repo_map.stats["verified_findings"] = verify_findings
        else:
            print("  Verification: all checks passed")

    if args.claude:
        from .deep_reviewer import deep_review
        all_prior = []
        if "lint_findings" in repo_map.stats:
            all_prior.extend(repo_map.stats["lint_findings"])
        if "xref" in repo_map.stats and "findings" in repo_map.stats["xref"]:
            all_prior.extend(repo_map.stats["xref"]["findings"])
        if "verified_findings" in repo_map.stats:
            all_prior.extend(repo_map.stats["verified_findings"])
        if "ai_findings" in repo_map.stats:
            all_prior.extend(repo_map.stats["ai_findings"])

        flagged = [n.path for n in repo_map.nodes
                   if n.type == NodeType.FILE and n.connectivity.value in ("unreachable", "incomplete")]

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
        if claude_findings:
            print(f"  CLAUDE FINDINGS ({len(claude_findings)}):")
            for f in claude_findings:
                sev = f.get("severity", "?")
                fp = f.get("file", "?")
                ln = f.get("line", "?")
                cat = f.get("category", "?")
                desc = f.get("desc", "?")
                evidence = f.get("evidence", "")
                print(f"    [{sev}] {fp}:{ln} [{cat}]: {desc}")
                if evidence:
                    print(f"           Evidence: {evidence}")
            repo_map.stats["claude_findings"] = claude_findings
        else:
            print("  Claude: no additional findings")

    output_path = Path(args.output) if args.output else project_root / "repo-map.json"
    # Atomic write: tmp + rename avoids a half-written repo-map.json if
    # the process crashes or is interrupted mid-write.
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


if __name__ == "__main__":
    main()
