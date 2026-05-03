"""ui_view — React Flow node-graph UI (ComfyUI-style).

Single-file output. Loads React + React Flow from esm.sh — no npm/install.
Custom node renderer shows file path, node type, complexity, lint count.
Sidebar lists findings for the selected node. Logic-block clusters get
group-region colored backgrounds.

Usage from CLI:
    code-mapper /path --lint --ui              # writes repo-map-ui.html
    code-mapper /path --lint --ui custom.html
"""
import html
import json
from pathlib import Path

from .schema import EdgeType, NodeType, RepoMap


# Distinct colors for logic-block group regions (cycled)
CLUSTER_COLORS = [
    "rgba(78,201,176,0.08)",   # teal
    "rgba(220,220,170,0.08)",  # yellow
    "rgba(244,135,113,0.08)",  # red
    "rgba(156,220,254,0.08)",  # blue
    "rgba(197,134,192,0.08)",  # purple
    "rgba(115,201,144,0.08)",  # green
    "rgba(206,145,120,0.08)",  # orange
]


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Code Mapper UI — __PROJECT__</title>
<script type="importmap">
{
  "imports": {
    "react": "https://esm.sh/react@18.3.1",
    "react-dom/client": "https://esm.sh/react-dom@18.3.1/client",
    "@xyflow/react": "https://esm.sh/@xyflow/react@12.3.5"
  }
}
</script>
<link rel="stylesheet" href="https://esm.sh/@xyflow/react@12.3.5/dist/style.css">
<style>
  body { margin: 0; font-family: -apple-system, Segoe UI, sans-serif;
         background: #1a1a1a; color: #e0e0e0; overflow: hidden; }
  #root { width: 100vw; height: 100vh; }
  header { position: fixed; top: 0; left: 0; right: 0; padding: 8px 16px;
           background: rgba(30,30,30,0.95); border-bottom: 1px solid #333;
           z-index: 100; display: flex; align-items: center; gap: 16px;
           backdrop-filter: blur(6px); }
  header h1 { margin: 0; font-size: 14px; color: #4ec9b0; font-weight: 600; }
  header .meta { color: #888; font-size: 11px; }
  header input { background: #1a1a1a; color: #e0e0e0; border: 1px solid #444;
                 padding: 4px 10px; border-radius: 3px; font-size: 12px;
                 width: 240px; }
  header select { background: #1a1a1a; color: #e0e0e0; border: 1px solid #444;
                  padding: 4px 10px; border-radius: 3px; font-size: 12px; }
  #sidebar { position: fixed; right: 0; top: 40px; bottom: 0; width: 360px;
             background: rgba(30,30,30,0.97); border-left: 1px solid #333;
             padding: 12px; overflow-y: auto; z-index: 50;
             transition: transform 0.2s; }
  #sidebar.collapsed { transform: translateX(360px); }
  #sidebar h2 { margin: 0 0 8px 0; font-size: 13px; color: #4ec9b0; }
  #sidebar .meta-row { font-size: 11px; color: #aaa; margin: 2px 0;
                       font-family: Consolas, monospace; }
  #sidebar .finding { margin: 6px 0; padding: 6px 8px; background: #252525;
                      border-radius: 3px; font-size: 11px; }
  #sidebar .finding.high { border-left: 3px solid #f48771; }
  #sidebar .finding.med { border-left: 3px solid #dcdcaa; }
  #sidebar .finding.low { border-left: 3px solid #9cdcfe; }
  #sidebar .rule { color: #c586c0; font-weight: 600; }
  #sidebar .desc { color: #d4d4d4; }
  #sidebar .empty { color: #666; font-style: italic; padding: 20px 0;
                    text-align: center; }
  /* React Flow custom node */
  .cm-node { background: #2a2a2a; border: 1px solid #555; border-radius: 4px;
             padding: 6px 10px; font-size: 11px; min-width: 140px;
             font-family: Consolas, monospace; cursor: pointer; }
  .cm-node:hover { border-color: #4ec9b0; }
  .cm-node.selected { border-color: #4ec9b0; box-shadow: 0 0 0 2px rgba(78,201,176,0.3); }
  .cm-node .name { color: #ce9178; font-weight: 600; }
  .cm-node .type-badge { display: inline-block; padding: 1px 5px;
                         border-radius: 2px; font-size: 9px; margin-right: 4px;
                         text-transform: uppercase; }
  .cm-node .type-file { background: #1d3a4a; color: #9cdcfe; }
  .cm-node .type-class { background: #3a1d4a; color: #c586c0; }
  .cm-node .type-function { background: #3a3a1d; color: #dcdcaa; }
  .cm-node .stats { color: #888; font-size: 10px; margin-top: 3px; }
  .cm-node .stats .cx { color: #dcdcaa; }
  .cm-node .stats .lint-high { color: #f48771; font-weight: 600; }
  .cm-node .stats .lint-med { color: #dcdcaa; }
  .react-flow__edge-path { stroke: #555; stroke-width: 1.5; }
  .react-flow__edge.selected .react-flow__edge-path { stroke: #4ec9b0; stroke-width: 2.5; }
  .react-flow__controls { background: #2a2a2a; border-color: #444; }
  .react-flow__controls-button { background: #2a2a2a; border-color: #444;
                                  fill: #ccc; }
  .react-flow__minimap { background: #1a1a1a; }
  /* Cluster group node */
  .cm-cluster { padding: 24px 12px 12px 12px; border-radius: 6px;
                border: 1px dashed rgba(255,255,255,0.15); pointer-events: none; }
  .cm-cluster .label { position: absolute; top: 4px; left: 8px;
                        font-size: 11px; color: #888; font-weight: 600; }
</style>
</head><body>
<header>
  <h1>Code Mapper</h1>
  <span class="meta">__PROJECT__ · __N_NODES__ nodes · __N_EDGES__ edges · __N_FINDINGS__ findings</span>
  <input id="filter" placeholder="Filter (file, function, rule)..." oninput="window.cm.applyFilter(this.value)">
  <select id="layoutMode" onchange="window.cm.setLayout(this.value)">
    <option value="force">Force layout</option>
    <option value="hierarchical">Hierarchical</option>
  </select>
  <button onclick="document.getElementById('sidebar').classList.toggle('collapsed')"
          style="background:#2a2a2a;color:#ccc;border:1px solid #444;padding:4px 10px;cursor:pointer;font-size:11px;">
    Toggle Sidebar
  </button>
</header>
<div id="root"></div>
<div id="sidebar">
  <h2>Selected Node</h2>
  <div id="sidebar-content"><div class="empty">Click a node to inspect</div></div>
</div>
<script type="module">
import React, { useState, useMemo, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import {
  ReactFlow, Controls, MiniMap, Background, Handle, Position,
  applyNodeChanges, applyEdgeChanges,
} from "@xyflow/react";

const data = __DATA_JSON__;
const findingsByFile = {};
for (const f of (data.findings || [])) {
  const k = f.file || f.file_path || "";
  if (!findingsByFile[k]) findingsByFile[k] = [];
  findingsByFile[k].push(f);
}

// Custom node renderer
function CMNode({ data: nd, selected }) {
  const findings = findingsByFile[nd.path] || [];
  const counts = { high: 0, med: 0, low: 0 };
  for (const f of findings) {
    const sev = (f.severity || "low").toLowerCase();
    if (counts[sev] !== undefined) counts[sev]++;
  }
  const typeClass = `type-${nd.type || "file"}`;
  return React.createElement("div",
    { className: `cm-node ${selected ? "selected" : ""}` },
    React.createElement(Handle, { type: "target", position: Position.Left, style: { background: "#555" } }),
    React.createElement("div", null,
      React.createElement("span", { className: `type-badge ${typeClass}` }, nd.type || "file"),
      React.createElement("span", { className: "name" }, nd.name)
    ),
    React.createElement("div", { className: "stats" },
      `cx ${nd.complexity || 0}`,
      counts.high > 0 && React.createElement("span", { className: "lint-high" }, ` · ${counts.high}H`),
      counts.med > 0 && React.createElement("span", { className: "lint-med" }, ` · ${counts.med}M`)
    ),
    React.createElement(Handle, { type: "source", position: Position.Right, style: { background: "#555" } })
  );
}

// Force-layout positions (approximate; React Flow stores as-is)
function forceLayout(nodes, edges) {
  const n = nodes.length;
  const cols = Math.ceil(Math.sqrt(n));
  return nodes.map((node, i) => ({
    ...node,
    position: { x: (i % cols) * 220, y: Math.floor(i / cols) * 100 },
  }));
}

function hierarchicalLayout(nodes, edges) {
  // Group by directory depth then arrange in rows
  const depth = (p) => (p || "").split("/").length;
  const sorted = [...nodes].sort((a, b) => depth(a.data.path) - depth(b.data.path));
  const byDepth = {};
  for (const n of sorted) {
    const d = depth(n.data.path);
    if (!byDepth[d]) byDepth[d] = [];
    byDepth[d].push(n);
  }
  const out = [];
  let y = 0;
  for (const d of Object.keys(byDepth).sort()) {
    byDepth[d].forEach((node, i) => {
      out.push({ ...node, position: { x: i * 200, y } });
    });
    y += 130;
  }
  return out;
}

function App() {
  const initialNodes = data.nodes.map(n => ({
    id: n.id,
    type: "cm",
    data: n,
    position: { x: 0, y: 0 },
  }));
  const initialEdges = data.edges.map((e, i) => ({
    id: `e${i}`, source: e.source, target: e.target,
    type: "default", animated: false,
  })).filter(e =>
    initialNodes.some(n => n.id === e.source) &&
    initialNodes.some(n => n.id === e.target)
  );
  const laid = useMemo(() => forceLayout(initialNodes, initialEdges), []);
  const [nodes, setNodes] = useState(laid);
  const [edges, setEdges] = useState(initialEdges);
  const [selected, setSelected] = useState(null);

  const onNodesChange = useCallback((changes) => setNodes(ns => applyNodeChanges(changes, ns)), []);
  const onEdgesChange = useCallback((changes) => setEdges(es => applyEdgeChanges(changes, es)), []);
  const onNodeClick = useCallback((_, node) => {
    setSelected(node);
    renderSidebar(node);
  }, []);

  // Expose filter + layout switch on window
  useEffect(() => {
    window.cm = window.cm || {};
    window.cm.applyFilter = (q) => {
      q = (q || "").toLowerCase();
      setNodes(ns => ns.map(n => ({
        ...n,
        hidden: q && !(n.data.path || "").toLowerCase().includes(q)
                  && !(n.data.name || "").toLowerCase().includes(q),
      })));
    };
    window.cm.setLayout = (mode) => {
      setNodes(ns => mode === "hierarchical"
        ? hierarchicalLayout(ns, edges)
        : forceLayout(ns, edges));
    };
  }, [edges]);

  return React.createElement(ReactFlow, {
    nodes, edges, nodeTypes: { cm: CMNode },
    onNodesChange, onEdgesChange, onNodeClick,
    fitView: true, minZoom: 0.05, maxZoom: 4,
    style: { background: "#1a1a1a" },
  },
    React.createElement(Background, { color: "#333", gap: 20, size: 1 }),
    React.createElement(Controls, { style: { left: 12, bottom: 12 } }),
    React.createElement(MiniMap, {
      nodeColor: (n) => ({ file: "#9cdcfe", class: "#c586c0", function: "#dcdcaa" }[n.data?.type] || "#666"),
      style: { background: "#1a1a1a", border: "1px solid #444" },
    })
  );
}

function renderSidebar(node) {
  const el = document.getElementById("sidebar-content");
  if (!node) { el.innerHTML = '<div class="empty">Click a node to inspect</div>'; return; }
  const findings = findingsByFile[node.data.path] || [];
  const lines = [
    `<h2>${escapeHtml(node.data.name)}</h2>`,
    `<div class="meta-row"><b>Type:</b> ${escapeHtml(node.data.type || "file")}</div>`,
    `<div class="meta-row"><b>Path:</b> ${escapeHtml(node.data.path || "")}</div>`,
    `<div class="meta-row"><b>Complexity:</b> ${node.data.complexity || 0}</div>`,
    `<div class="meta-row"><b>Lines:</b> ${node.data.line_start || 1}–${node.data.line_end || ""}</div>`,
    `<h2 style="margin-top:14px;">Findings (${findings.length})</h2>`,
  ];
  if (findings.length === 0) lines.push('<div class="empty">No lint findings</div>');
  for (const f of findings) {
    const sev = (f.severity || "low").toLowerCase();
    lines.push(
      `<div class="finding ${sev}">` +
      `<span class="rule">${escapeHtml(f.rule || "?")}</span> ` +
      `<span style="color:#888">@ line ${f.line || 0}</span><br>` +
      `<span class="desc">${escapeHtml((f.desc || "").slice(0, 200))}</span>` +
      `</div>`
    );
  }
  el.innerHTML = lines.join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

createRoot(document.getElementById("root")).render(React.createElement(App));
</script>
</body></html>
"""


def render_ui(repo_map: RepoMap, project_path: str = "") -> str:
    file_nodes = [n for n in repo_map.nodes if n.type == NodeType.FILE]
    cx_by_file: dict[str, int] = {}
    for n in repo_map.nodes:
        if n.type == NodeType.FUNCTION and n.parent_id:
            cx_by_file[n.parent_id] = cx_by_file.get(n.parent_id, 0) + max(1, n.complexity)

    nodes_data = [
        {
            "id": n.id, "name": n.name, "path": n.path,
            "type": n.type.value if hasattr(n.type, "value") else str(n.type),
            "complexity": cx_by_file.get(n.id, n.complexity or 0),
            "line_start": n.line_start, "line_end": n.line_end,
        }
        for n in file_nodes
    ]
    valid_ids = {n["id"] for n in nodes_data}
    edges_data = [
        {"source": e.source, "target": e.target}
        for e in repo_map.edges
        if e.type == EdgeType.IMPORT and e.source in valid_ids and e.target in valid_ids
    ]
    findings = []
    for key in ("lint_findings", "ai_findings", "verified_findings",
                "claude_findings", "pattern_findings"):
        findings.extend(repo_map.stats.get(key, []))
    xref = repo_map.stats.get("xref")
    if xref and isinstance(xref, dict):
        findings.extend(xref.get("findings", []))

    payload = json.dumps({
        "nodes": nodes_data, "edges": edges_data, "findings": findings,
    })
    return (HTML_TEMPLATE
            .replace("__PROJECT__", html.escape(project_path or "(unknown)"))
            .replace("__N_NODES__", str(len(nodes_data)))
            .replace("__N_EDGES__", str(len(edges_data)))
            .replace("__N_FINDINGS__", str(len(findings)))
            .replace("__DATA_JSON__", payload))


def write_ui_html(repo_map: RepoMap, output_path: Path,
                   project_path: str = "") -> Path:
    output_path.write_text(render_ui(repo_map, project_path), encoding="utf-8")
    return output_path
