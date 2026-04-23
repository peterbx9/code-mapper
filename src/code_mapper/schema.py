"""
repo-map.json schema definitions.

The map has three node levels (file, class, function) and typed edges
connecting them. Logic blocks and connectivity annotations are layered on top.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
import json


class NodeType(str, Enum):
    FILE = "file"
    CLASS = "class"
    FUNCTION = "function"


class EdgeType(str, Enum):
    IMPORT = "import"
    CALL = "call"
    ROUTE = "route"
    TABLE_ACCESS = "table_access"
    INHERITANCE = "inheritance"
    DECORATOR = "decorator"
    REEXPORT = "reexport"


class EdgeFlag(str, Enum):
    DYNAMIC = "dynamic"
    CONDITIONAL = "conditional"
    FUNCTION_SCOPE = "function_scope"


class ConnectivityStatus(str, Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    INCOMPLETE = "incomplete"


@dataclass
class Node:
    id: str
    type: NodeType
    path: str
    name: str
    line_start: int = 0
    line_end: int = 0
    docstring: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    complexity: int = 0
    is_stub: bool = False
    connectivity: ConnectivityStatus = ConnectivityStatus.REACHABLE
    dead_confidence: int = 0

    def to_dict(self):
        d = asdict(self)
        d["type"] = self.type.value
        d["connectivity"] = self.connectivity.value
        return d


@dataclass
class Edge:
    source: str
    target: str
    type: EdgeType
    flags: list[EdgeFlag] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["type"] = self.type.value
        d["flags"] = [f.value for f in self.flags]
        return d


@dataclass
class LogicBlock:
    id: str
    name: str
    node_ids: list[str] = field(default_factory=list)
    shared_tables: list[str] = field(default_factory=list)
    shared_imports: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class RepoMap:
    project_name: str
    root: str
    generated_at: str = ""
    version: str = "1.0"
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    logic_blocks: list[LogicBlock] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "project_name": self.project_name,
            "root": self.root,
            "generated_at": self.generated_at,
            "version": self.version,
            "entry_points": self.entry_points,
            "stats": self.stats,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "logic_blocks": [lb.to_dict() for lb in self.logic_blocks],
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def get_node(self, node_id: str) -> Optional[Node]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_edges_from(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]

    def get_edges_to(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id]
