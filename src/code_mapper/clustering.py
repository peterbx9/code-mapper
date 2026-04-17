"""
Logic block clustering using shared-table + shared-import affinity.

Uses a simplified community detection approach. If networkx is available,
uses Louvain. Otherwise falls back to connected-component grouping on the
affinity graph.
"""

import logging
from collections import defaultdict

from .schema import RepoMap, LogicBlock, NodeType, EdgeType

logger = logging.getLogger(__name__)


def cluster_logic_blocks(repo_map: RepoMap, resolution: float = 1.0) -> list[LogicBlock]:
    file_nodes = [n for n in repo_map.nodes if n.type == NodeType.FILE]
    if not file_nodes:
        return []

    table_to_files = defaultdict(set)
    for node in file_nodes:
        for table in node.tables:
            table_to_files[table].add(node.id)

    import_graph = defaultdict(set)
    for edge in repo_map.edges:
        if edge.type == EdgeType.IMPORT:
            src_file = edge.source
            tgt = edge.target
            if tgt.startswith("file:"):
                import_graph[src_file].add(tgt)
                import_graph[tgt].add(src_file)

    affinity = defaultdict(float)
    for table, files in table_to_files.items():
        file_list = list(files)
        for i in range(len(file_list)):
            for j in range(i + 1, len(file_list)):
                pair = tuple(sorted([file_list[i], file_list[j]]))
                affinity[pair] += 2.0

    for file_id, neighbors in import_graph.items():
        for neighbor in neighbors:
            pair = tuple(sorted([file_id, neighbor]))
            affinity[pair] += 1.0

    try:
        import networkx as nx
        from networkx.algorithms.community import louvain_communities
        blocks = _louvain_cluster(file_nodes, affinity, resolution)
    except ImportError:
        logger.info("networkx not available, falling back to component clustering")
        blocks = _component_cluster(file_nodes, affinity)

    for block in blocks:
        block.shared_tables = _find_shared(block.node_ids, repo_map, "tables")
        block.shared_imports = _find_shared_imports(block.node_ids, repo_map)

    return blocks


def _louvain_cluster(file_nodes, affinity, resolution) -> list[LogicBlock]:
    import networkx as nx
    from networkx.algorithms.community import louvain_communities

    G = nx.Graph()
    for node in file_nodes:
        G.add_node(node.id)

    for (a, b), weight in affinity.items():
        G.add_edge(a, b, weight=weight)

    communities = louvain_communities(G, resolution=resolution, seed=42)

    blocks = []
    for i, community in enumerate(sorted(communities, key=len, reverse=True)):
        node_ids = sorted(community)
        name = _infer_block_name(node_ids)
        blocks.append(LogicBlock(
            id=f"block:{i}",
            name=name,
            node_ids=node_ids,
        ))

    return blocks


def _component_cluster(file_nodes, affinity) -> list[LogicBlock]:
    adj = defaultdict(set)
    for (a, b), weight in affinity.items():
        if weight >= 1.0:
            adj[a].add(b)
            adj[b].add(a)

    all_ids = {n.id for n in file_nodes}
    visited = set()
    components = []

    for node_id in sorted(all_ids):
        if node_id in visited:
            continue
        component = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adj.get(current, []):
                if neighbor not in visited and neighbor in all_ids:
                    stack.append(neighbor)
        components.append(sorted(component))

    blocks = []
    for i, comp in enumerate(sorted(components, key=len, reverse=True)):
        name = _infer_block_name(comp)
        blocks.append(LogicBlock(
            id=f"block:{i}",
            name=name,
            node_ids=comp,
        ))

    return blocks


def _infer_block_name(node_ids: list[str]) -> str:
    paths = [nid.replace("file:", "") for nid in node_ids]

    subdir_counts = defaultdict(int)
    for p in paths:
        parts = p.split("/")
        if len(parts) >= 2:
            subdir_counts[parts[-2]] += 1

    deepest_common = ""
    if subdir_counts:
        top_subdir = max(subdir_counts, key=subdir_counts.get)
        if subdir_counts[top_subdir] >= len(paths) * 0.5:
            deepest_common = top_subdir

    keywords = defaultdict(int)
    skip_words = {"__init__", "backend", "routers", "services", "models", "schemas", "jobs", "py"}
    for p in paths:
        stem = p.split("/")[-1].replace(".py", "").replace("_", " ")
        for word in stem.split():
            if len(word) > 2 and word.lower() not in skip_words:
                keywords[word] += 1

    top_keywords = sorted(keywords.items(), key=lambda x: -x[1])[:3]
    kw_label = " + ".join(kw.title() for kw, _ in top_keywords if _ >= 2)

    if deepest_common and deepest_common.lower() not in skip_words:
        label = deepest_common.replace("_", " ").title()
        if kw_label:
            label = f"{label}: {kw_label}"
        return label

    if deepest_common:
        if kw_label:
            return kw_label
        dir_path_parts = set()
        for p in paths:
            parts = p.split("/")
            if len(parts) >= 3:
                dir_path_parts.add(parts[-2])
        if len(dir_path_parts) == 1:
            return list(dir_path_parts)[0].replace("_", " ").title()
        if len(dir_path_parts) > 1:
            return " + ".join(sorted(d.replace("_", " ").title() for d in dir_path_parts))

    if kw_label:
        return kw_label

    stems = [p.split("/")[-1].replace(".py", "") for p in paths[:3]]
    return ", ".join(stems)


def _find_shared(node_ids: list[str], repo_map: RepoMap, attr: str) -> list[str]:
    values = defaultdict(int)
    for nid in node_ids:
        node = repo_map.get_node(nid)
        if node:
            for val in getattr(node, attr, []):
                values[val] += 1
    return [v for v, count in values.items() if count >= 2]


def _find_shared_imports(node_ids: list[str], repo_map: RepoMap) -> list[str]:
    targets = defaultdict(int)
    nid_set = set(node_ids)
    for edge in repo_map.edges:
        if edge.type == EdgeType.IMPORT and edge.source in nid_set:
            targets[edge.target] += 1
    return [t for t, count in targets.items() if count >= 2]
