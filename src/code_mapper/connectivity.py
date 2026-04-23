"""
Connectivity analysis — detect unreachable (dead) code and incomplete wiring.

Unreachable: no inbound edges from any entry point.
Incomplete: has inbound edges but no path to an effect (route response, file write, DB mutation).
"""

import logging
from collections import defaultdict, deque

from .schema import RepoMap, Node, NodeType, EdgeType, ConnectivityStatus

logger = logging.getLogger(__name__)


def analyze_connectivity(repo_map: RepoMap):
    forward = defaultdict(set)
    backward = defaultdict(set)

    all_node_ids = {n.id for n in repo_map.nodes}

    for edge in repo_map.edges:
        if edge.source in all_node_ids and edge.target in all_node_ids:
            forward[edge.source].add(edge.target)
            backward[edge.target].add(edge.source)

    for node in repo_map.nodes:
        if node.type != NodeType.FILE:
            if node.parent_id and node.parent_id in all_node_ids:
                forward[node.parent_id].add(node.id)
                backward[node.id].add(node.parent_id)

    entry_ids = set()
    for ep in repo_map.entry_points:
        if ep in all_node_ids:
            entry_ids.add(ep)

    if not entry_ids:
        for node in repo_map.nodes:
            if node.type == NodeType.FILE and node.routes:
                entry_ids.add(node.id)

    if not entry_ids:
        _ENTRY_NAMES = {"main", "__main__", "cli", "app", "server", "wsgi", "asgi"}
        for node in repo_map.nodes:
            if node.type == NodeType.FILE and node.name in _ENTRY_NAMES:
                entry_ids.add(node.id)

    reachable_from_entries = _bfs_forward(entry_ids, forward)

    effect_nodes = set()
    for node in repo_map.nodes:
        if node.type == NodeType.FILE:
            if node.routes:
                effect_nodes.add(node.id)
            if node.tables:
                effect_nodes.add(node.id)

    has_route_or_table_effects = bool(effect_nodes)
    effect_nodes.update(entry_ids)

    if has_route_or_table_effects:
        reaches_effect = _bfs_backward(effect_nodes, backward)
    else:
        reaches_effect = reachable_from_entries

    file_nodes = [n for n in repo_map.nodes if n.type == NodeType.FILE]

    structural_files = set()
    schema_files = set()
    config_files = set()
    middleware_files = set()
    standalone_scripts = set()
    _CONFIG_NAMES = {"config", "settings", "constants", "defaults", "env"}
    _MIDDLEWARE_NAMES = {"csrf", "middleware", "cors", "logging_middleware", "auth_middleware"}
    _STANDALONE_DIRS = {"tols-pbj", "updates", "migrations", "versions", "scripts", "tools"}
    for node in file_nodes:
        basename = node.path.split("/")[-1]
        stem = basename.replace(".py", "")
        if basename == "__init__.py":
            structural_files.add(node.id)
        if "/schemas/" in node.path or "schemas/" in node.path:
            schema_files.add(node.id)
        if stem in _CONFIG_NAMES and not node.routes and not node.tables:
            config_files.add(node.id)
        if stem in _MIDDLEWARE_NAMES:
            middleware_files.add(node.id)
        parts = set(node.path.replace("\\", "/").split("/")[:-1])
        if "/" not in node.path or parts & _STANDALONE_DIRS:
            standalone_scripts.add(node.id)
        if stem == "cli" or stem.endswith("_cli") or stem == "manage":
            standalone_scripts.add(node.id)

    unreachable = []
    incomplete = []

    for node in file_nodes:
        if node.id in structural_files:
            node.connectivity = ConnectivityStatus.REACHABLE
            continue

        has_inbound = bool(backward.get(node.id))
        in_reachable = node.id in reachable_from_entries
        in_effect = node.id in reaches_effect

        if not in_reachable and not has_inbound and node.id not in entry_ids:
            if node.id in standalone_scripts:
                node.connectivity = ConnectivityStatus.REACHABLE
                node.dead_confidence = 0
            else:
                node.connectivity = ConnectivityStatus.UNREACHABLE
                node.dead_confidence = 100
                unreachable.append(node.id)
        elif not in_reachable and has_inbound:
            node.connectivity = ConnectivityStatus.UNREACHABLE
            node.dead_confidence = 80
            unreachable.append(node.id)
        elif in_reachable and not in_effect and node.id not in effect_nodes:
            if node.id in schema_files or node.id in config_files or node.id in middleware_files:
                node.connectivity = ConnectivityStatus.REACHABLE
                node.dead_confidence = 0
            else:
                node.connectivity = ConnectivityStatus.INCOMPLETE
                node.dead_confidence = 40
                incomplete.append(node.id)
        else:
            node.connectivity = ConnectivityStatus.REACHABLE
            node.dead_confidence = 0

    if unreachable:
        logger.info(f"Unreachable files: {len(unreachable)}")
        for nid in unreachable:
            logger.debug(f"  UNREACHABLE: {nid}")

    if incomplete:
        logger.info(f"Incomplete wiring: {len(incomplete)}")
        for nid in incomplete:
            logger.debug(f"  INCOMPLETE: {nid}")

    cycles = _detect_circular_dependencies(repo_map)
    if cycles:
        logger.info(f"Circular dependencies: {len(cycles)} cycles")
        for cycle in cycles:
            logger.debug(f"  CYCLE: {' → '.join(cycle)}")

    return {
        "reachable": len(file_nodes) - len(unreachable) - len(incomplete),
        "unreachable": unreachable,
        "incomplete": incomplete,
        "circular_dependencies": cycles,
    }


def _bfs_forward(start_ids: set, graph: dict) -> set:
    visited = set()
    queue = deque(start_ids)
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited


def _bfs_backward(start_ids: set, reverse_graph: dict) -> set:
    visited = set()
    queue = deque(start_ids)
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for neighbor in reverse_graph.get(current, []):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited


def _detect_circular_dependencies(repo_map: RepoMap) -> list[list[str]]:
    """Find all cycles in the file-level import graph using DFS."""
    file_graph = defaultdict(set)
    file_ids = set()

    for node in repo_map.nodes:
        if node.type == NodeType.FILE:
            file_ids.add(node.id)

    for edge in repo_map.edges:
        if edge.type.value == "import" and edge.source in file_ids and edge.target in file_ids:
            file_graph[edge.source].add(edge.target)

    cycles = []
    visited = set()
    path_set = set()
    path_list = []

    def _dfs(node):
        if node in path_set:
            cycle_start = path_list.index(node)
            cycle = [n.replace("file:", "") for n in path_list[cycle_start:]] + [node.replace("file:", "")]
            if len(cycle) > 2:
                normalized = _normalize_cycle(cycle)
                if normalized not in seen_cycles:
                    seen_cycles.add(normalized)
                    cycles.append(cycle[:-1])
            return
        if node in visited:
            return

        visited.add(node)
        path_set.add(node)
        path_list.append(node)

        for neighbor in file_graph.get(node, []):
            _dfs(neighbor)

        path_set.remove(node)
        path_list.pop()

    seen_cycles = set()
    for node in sorted(file_ids):
        visited.clear()
        path_set.clear()
        path_list.clear()
        _dfs(node)

    return cycles


def _normalize_cycle(cycle: list[str]) -> tuple:
    """Normalize a cycle so the same cycle found from different start nodes is deduplicated."""
    without_repeat = cycle[:-1]
    if not without_repeat:
        return tuple(cycle)
    min_idx = without_repeat.index(min(without_repeat))
    rotated = without_repeat[min_idx:] + without_repeat[:min_idx]
    return tuple(rotated)
