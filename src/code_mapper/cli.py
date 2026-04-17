"""
Code Mapper CLI — point at any Python project, get a repo-map.json.

Usage:
    python -m code_mapper /path/to/project
    python -m code_mapper /path/to/project --output my-map.json
    python -m code_mapper /path/to/project --verbose
    python -m code_mapper /path/to/project --force
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .assembler import assemble_map, load_config
from .clustering import cluster_logic_blocks
from .connectivity import analyze_connectivity


def main():
    parser = argparse.ArgumentParser(
        prog="code-mapper",
        description="Parse a Python project into a structured repo-map.json",
    )
    parser.add_argument("project", help="Path to project root")
    parser.add_argument("--output", "-o", default=None, help="Output file (default: repo-map.json in project root)")
    parser.add_argument("--config", "-c", default=None, help="Path to .codemapper.json (default: project root)")
    parser.add_argument("--force", "-f", action="store_true", help="Force full rebuild (ignore cache)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--no-cluster", action="store_true", help="Skip logic block clustering")
    parser.add_argument("--no-connectivity", action="store_true", help="Skip connectivity analysis")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    project_root = Path(args.project).resolve()
    if not project_root.is_dir():
        print(f"Error: {project_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    config = None
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            print(f"Warning: config file {config_path} not found, using defaults", file=sys.stderr)
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

    output_path = Path(args.output) if args.output else project_root / "repo-map.json"
    output_path.write_text(repo_map.to_json(), encoding="utf-8")
    print(f"\nMap written to: {output_path}")


if __name__ == "__main__":
    main()
