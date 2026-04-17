"""
Connectivity analysis — detect unreachable (dead) code and incomplete wiring.

Unreachable: no inbound edges from any entry point.
Incomplete: has inbound edges but no path to an effect (route response, file write, DB mutation).
"""

import logging
from collections import defaultdict

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
        for node in repo_map.nodes:
            if node.type == NodeType.FILE and node.name == "main":
                entry_ids.add(node.id)

    reachable_from_entries = _bfs_forward(entry_ids, forward)

    effect_nodes = set()
    for node in repo_map.nodes:
        if node.type == NodeType.FILE:
            if node.routes:
                effect_nodes.add(node.id)
            if node.tables:
                effect_nodes.add(node.id)

    reaches_effect = _bfs_backward(effect_nodes, backward)

    file_nodes = [n for n in repo_map.nodes if n.type == NodeType.FILE]

    unreachable = []
    incomplete = []

    for node in file_nodes:
        has_inbound = bool(backward.get(node.id))
        in_reachable = node.id in reachable_from_entries
        in_effect = node.id in reaches_effect

        if not in_reachable and not has_inbound and node.id not in entry_ids:
            node.connectivity = ConnectivityStatus.UNREACHABLE
            unreachable.append(node.id)
        elif in_reachable and not in_effect and node.id not in effect_nodes:
            node.connectivity = ConnectivityStatus.INCOMPLETE
            incomplete.append(node.id)
        else:
            node.connectivity = ConnectivityStatus.REACHABLE

    if unreachable:
        logger.info(f"Unreachable files: {len(unreachable)}")
        for nid in unreachable:
            logger.debug(f"  UNREACHABLE: {nid}")

    if incomplete:
        logger.info(f"Incomplete wiring: {len(incomplete)}")
        for nid in incomplete:
            logger.debug(f"  INCOMPLETE: {nid}")

    return {
        "reachable": len(file_nodes) - len(unreachable) - len(incomplete),
        "unreachable": unreachable,
        "incomplete": incomplete,
    }


def _bfs_forward(start_ids: set, graph: dict) -> set:
    visited = set()
    queue = list(start_ids)
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited


def _bfs_backward(start_ids: set, reverse_graph: dict) -> set:
    visited = set()
    queue = list(start_ids)
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for neighbor in reverse_graph.get(current, []):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited
