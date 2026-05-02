"""Circular dependency detection — finds cycles in the import graph.

Works on the RepoMap's import edges between FILE nodes. Reports the
shortest cycle through each strongly connected component, plus alternate
paths if multiple cycles share a component.

Usage from CLI:
    code-mapper /path --cycles            # detect + print
    code-mapper /path --cycles --html     # render in HTML report

Findings get severity 'med' for 2-file cycles (often refactor noise),
'high' for 3+ file cycles (real architectural smell).
"""
from __future__ import annotations
import logging
from typing import Iterable

from .schema import EdgeType, NodeType, RepoMap

logger = logging.getLogger(__name__)


def _file_id_to_path(repo_map: RepoMap) -> dict[str, str]:
    return {n.id: n.path for n in repo_map.nodes if n.type == NodeType.FILE}


def _build_import_graph(repo_map: RepoMap) -> dict[str, set[str]]:
    """Adjacency list: file_id → set of file_ids it imports.
    Module-level imports only (skip function-scoped to reduce noise)."""
    file_ids = {n.id for n in repo_map.nodes if n.type == NodeType.FILE}
    g: dict[str, set[str]] = {fid: set() for fid in file_ids}
    for e in repo_map.edges:
        if e.type != EdgeType.IMPORT:
            continue
        # Skip dynamic / function-scope edges — those are not "real" import cycles
        if e.flags and any(f.value in ("function_scope", "dynamic")
                            for f in e.flags):
            continue
        if e.source in g and e.target in g and e.source != e.target:
            g[e.source].add(e.target)
    return g


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components. Returns components with size>=2."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[list[str]] = []

    def _strongconnect(v: str):
        indices[v] = lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, set()):
            if w not in indices:
                _strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        if lowlinks[v] == indices[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                sccs.append(scc)

    # Use iterative DFS for very large graphs to avoid recursion limit
    import sys as _sys
    old_limit = _sys.getrecursionlimit()
    _sys.setrecursionlimit(max(old_limit, 10000))
    try:
        for v in list(graph.keys()):
            if v not in indices:
                _strongconnect(v)
    finally:
        _sys.setrecursionlimit(old_limit)
    return sccs


def _shortest_cycle_in_scc(graph: dict[str, set[str]], scc: list[str]) -> list[str]:
    """BFS from each node in the SCC; return the smallest cycle found."""
    scc_set = set(scc)
    best: list[str] = []
    for start in scc:
        # BFS recording parent pointers
        parent: dict[str, str] = {}
        from collections import deque
        q = deque([start])
        visited = {start}
        found_at = None
        while q:
            v = q.popleft()
            for w in graph.get(v, set()):
                if w not in scc_set:
                    continue
                if w == start:
                    # Cycle found: reconstruct path start → ... → v → start
                    path = [start, v]
                    cur = v
                    while cur != start:
                        cur = parent.get(cur)
                        if cur is None or cur == start:
                            break
                        path.append(cur)
                    path.reverse()
                    found_at = path
                    break
                if w not in visited:
                    visited.add(w)
                    parent[w] = v
                    q.append(w)
            if found_at:
                break
        if found_at and (not best or len(found_at) < len(best)):
            best = found_at
    return best


def find_cycles(repo_map: RepoMap) -> list[dict]:
    """Return cycle findings: each {nodes: [path], length: N, severity}."""
    graph = _build_import_graph(repo_map)
    if not graph:
        return []
    id_to_path = _file_id_to_path(repo_map)
    sccs = _tarjan_scc(graph)
    findings = []
    for scc in sorted(sccs, key=len, reverse=True):
        cycle = _shortest_cycle_in_scc(graph, scc)
        if not cycle:
            continue
        path_names = [id_to_path.get(fid, fid) for fid in cycle]
        n = len(cycle)
        sev = "high" if n >= 3 else "med"
        findings.append({
            "rule": "CIRCULAR_DEPENDENCY",
            "severity": sev,
            "file_path": path_names[0],
            "line": 1,
            "cycle_length": n,
            "scc_size": len(scc),
            "cycle_nodes": path_names,
            "desc": (f"Import cycle of length {n}: "
                     + " → ".join(path_names) + f" → {path_names[0]}"
                     + (f" (SCC has {len(scc)} files total)" if len(scc) > n else "")),
        })
    return findings


def print_cycle_report(findings: list[dict]) -> None:
    if not findings:
        print()
        print("=== CIRCULAR DEPENDENCIES ===")
        print("  None — graph is acyclic.")
        return
    print()
    print(f"=== CIRCULAR DEPENDENCIES — {len(findings)} cycles ===")
    for f in findings:
        nodes = f["cycle_nodes"]
        print(f"  [{f['severity']}] cycle ({f['cycle_length']} files, "
              f"SCC={f['scc_size']}):")
        for i, p in enumerate(nodes):
            arrow = "  ↳ " if i > 0 else "    "
            print(f"  {arrow}{p}")
        print(f"    ↳ {nodes[0]}  (closes the cycle)")
