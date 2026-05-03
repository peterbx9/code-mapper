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

from .schema import NodeType, RepoMap


# Distinct colors for logic-block group regions (cycled).
# Punchier alpha + saturated borders so clusters read at zoom-out.
CLUSTER_COLORS = [
    "rgba(78,201,176,0.22)",   # teal
    "rgba(220,220,170,0.22)",  # yellow
    "rgba(244,135,113,0.22)",  # coral
    "rgba(156,220,254,0.22)",  # sky
    "rgba(197,134,192,0.22)",  # purple
    "rgba(115,201,144,0.22)",  # green
    "rgba(255,170,90,0.22)",   # orange
    "rgba(255,123,180,0.22)",  # pink
    "rgba(120,160,255,0.22)",  # indigo
]
TEST_CLUSTER_COLOR = "rgba(180,180,180,0.18)"


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Code Mapper UI — __PROJECT__</title>
<script type="importmap">
{
  "imports": {
    "react": "https://esm.sh/react@18.3.1",
    "react/jsx-runtime": "https://esm.sh/react@18.3.1/jsx-runtime",
    "react-dom": "https://esm.sh/react-dom@18.3.1?deps=react@18.3.1",
    "react-dom/client": "https://esm.sh/react-dom@18.3.1/client?deps=react@18.3.1",
    "reactflow": "https://esm.sh/reactflow@11.11.4?deps=react@18.3.1,react-dom@18.3.1&external=react,react-dom"
  }
}
</script>
<link rel="stylesheet" href="https://esm.sh/reactflow@11.11.4/dist/style.css">
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
  .cm-cluster { width: 100%; height: 100%; border-radius: 8px;
                border: 2px solid rgba(255,255,255,0.32);
                box-shadow: inset 0 0 24px rgba(0,0,0,0.35);
                pointer-events: none; position: relative; }
  .cm-cluster .label { position: absolute; top: 6px; left: 12px;
                        font-size: 18px; color: #fff; font-weight: 700;
                        font-family: -apple-system, Segoe UI, sans-serif;
                        letter-spacing: 1.2px; text-transform: uppercase;
                        text-shadow: 0 1px 4px rgba(0,0,0,0.85);
                        background: rgba(0,0,0,0.45); padding: 2px 8px;
                        border-radius: 3px; }
  /* Inline finding list (when expanded) */
  .cm-node .findings-inline { margin-top: 6px; max-width: 280px; }
  .cm-node .findings-inline .f { padding: 2px 0; font-size: 10px; color: #aaa; }
  .cm-node .findings-inline .f.high { color: #f48771; }
  .cm-node .findings-inline .f.med { color: #dcdcaa; }
  .cm-node .expand-btn { display: inline-block; margin-left: 6px;
                          padding: 0 5px; background: #333; color: #888;
                          border-radius: 2px; cursor: pointer; font-size: 10px; }
  .cm-node .expand-btn:hover { background: #444; color: #ccc; }
  /* Summary-mode big block card (one per logic block) */
  .cm-block { min-width: 240px; padding: 14px 18px;
              border: 2px solid rgba(255,255,255,0.4);
              border-radius: 10px; cursor: pointer;
              box-shadow: 0 4px 16px rgba(0,0,0,0.45),
                          inset 0 0 32px rgba(0,0,0,0.25);
              transition: transform 80ms, box-shadow 80ms; }
  .cm-block:hover { transform: translateY(-1px);
                    box-shadow: 0 6px 22px rgba(0,0,0,0.55),
                                inset 0 0 32px rgba(0,0,0,0.25); }
  .cm-block.selected { border-color: #4ec9b0; }
  .cm-block .block-title { font-size: 18px; font-weight: 700; color: #fff;
                           letter-spacing: 0.6px; text-transform: uppercase;
                           text-shadow: 0 1px 3px rgba(0,0,0,0.7);
                           margin-bottom: 6px; }
  .cm-block .block-stat { font-size: 12px; color: rgba(255,255,255,0.85);
                          margin-bottom: 4px; }
  .cm-block .block-findings { font-size: 12px; font-weight: 600;
                              color: rgba(255,255,255,0.85); }
  .cm-block .block-findings .lint-high { color: #f48771; }
  .cm-block .block-findings .lint-med { color: #dcdcaa; }
</style>
</head><body>
<header>
  <h1>Code Mapper</h1>
  <span class="meta">__PROJECT__ · __N_NODES__ nodes · __N_EDGES__ edges · __N_FINDINGS__ findings</span>
  <input id="filter" placeholder="Filter (file, function, rule)..." oninput="window.cm.applyFilter(this.value)">
  <select id="viewMode" onchange="window.cm.setView(this.value)">
    <option value="summary">Summary (logic blocks)</option>
    <option value="detail">Detail (all files)</option>
  </select>
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
// Visible error fallback if any module fails to load
window.addEventListener("error", (e) => {
  const root = document.getElementById("root");
  if (root && root.childNodes.length === 0) {
    root.innerHTML = `<div style="padding:40px;color:#f48771;font-family:monospace;">
      <h2>UI failed to load</h2>
      <p><b>Error:</b> ${e.message || e.error}</p>
      <p>Filename: ${e.filename || "?"}, line ${e.lineno || "?"}</p>
      <p>Likely cause: esm.sh blocked, CDN slow, or React Flow ESM mismatch.</p>
      <p>Open browser devtools → Console tab for full stack trace.</p>
    </div>`;
  }
});
window.addEventListener("unhandledrejection", (e) => {
  const root = document.getElementById("root");
  if (root && root.childNodes.length === 0) {
    root.innerHTML = `<div style="padding:40px;color:#f48771;font-family:monospace;">
      <h2>UI failed to load (promise rejection)</h2>
      <p>${e.reason}</p>
    </div>`;
  }
});

import React, { useState, useMemo, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import ReactFlow, {
  Controls, MiniMap, Background, Handle, Position,
  applyNodeChanges, applyEdgeChanges,
} from "reactflow";

const data = __DATA_JSON__;
const findingsByFile = {};
for (const f of (data.findings || [])) {
  const k = f.file || f.file_path || "";
  if (!findingsByFile[k]) findingsByFile[k] = [];
  findingsByFile[k].push(f);
}

// Custom node renderer (stateless — avoids React-instance mismatch with
// reactflow's bundled React under esm.sh import maps).
const h = React.createElement;

function CMNode(props) {
  const nd = props.data || {};
  const selected = !!props.selected;
  const findings = findingsByFile[nd.path] || [];
  let highCount = 0, medCount = 0;
  for (let i = 0; i < findings.length; i++) {
    const sev = String(findings[i].severity || "low").toLowerCase();
    if (sev === "high") highCount++;
    else if (sev === "med") medCount++;
  }
  const typeStr = String(nd.type || "file");
  const nameStr = String(nd.name || "");
  const cxStr = String(nd.complexity || 0);

  const headerKids = [
    h("span", { key: "tb", className: "type-badge type-" + typeStr }, typeStr),
    h("span", { key: "nm", className: "name" }, nameStr),
  ];
  if (findings.length > 0) {
    headerKids.push(
      h("span", { key: "fc", style: { color: "#888", marginLeft: "6px", fontSize: "10px" } },
        "(" + findings.length + ")")
    );
  }

  const statsKids = [h("span", { key: "cx" }, "cx " + cxStr)];
  if (highCount > 0) statsKids.push(h("span", { key: "hi", className: "lint-high" }, " · " + highCount + "H"));
  if (medCount > 0) statsKids.push(h("span", { key: "md", className: "lint-med" }, " · " + medCount + "M"));

  return h("div", { className: "cm-node " + (selected ? "selected" : "") },
    h(Handle, { type: "target", position: Position.Left, style: { background: "#555" } }),
    h("div", { key: "header" }, headerKids),
    h("div", { key: "stats", className: "stats" }, statsKids),
    h(Handle, { type: "source", position: Position.Right, style: { background: "#555" } })
  );
}

// Cluster (group region) node renderer — colored bounding box for logic blocks
function CMCluster(props) {
  const nd = props.data || {};
  return h("div",
    { className: "cm-cluster",
      style: { background: String(nd.color || "rgba(78,201,176,0.06)") } },
    h("div", { className: "label" }, String(nd.label || ""))
  );
}

// CMBlock — Summary-mode interactive block. Big colored card per logic
// block showing name + file count + finding totals + total complexity.
function CMBlock(props) {
  const nd = props.data || {};
  const selected = !!props.selected;
  const counts = nd.counts || {};
  const findingKids = [];
  if (counts.high) findingKids.push(h("span", { key: "h", className: "lint-high" }, counts.high + "H"));
  if (counts.med) findingKids.push(h("span", { key: "m", className: "lint-med" }, " " + counts.med + "M"));
  if (counts.low) findingKids.push(h("span", { key: "l" }, " " + counts.low + "L"));
  return h("div", {
    className: "cm-block " + (selected ? "selected" : ""),
    style: {
      background: String(nd.color || "rgba(78,201,176,0.22)"),
      borderColor: String(nd.borderColor || "rgba(255,255,255,0.4)"),
    },
  },
    h(Handle, { type: "target", position: Position.Left, style: { background: "#888" } }),
    h("div", { key: "ttl", className: "block-title" }, String(nd.label || "Block")),
    h("div", { key: "stat", className: "block-stat" },
      String(nd.fileCount || 0) + " files · cx " + String(nd.totalCx || 0)),
    findingKids.length > 0 ? h("div", { key: "fnd", className: "block-findings" }, findingKids) : null,
    h(Handle, { type: "source", position: Position.Right, style: { background: "#888" } })
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

// Color edges by type (import gray, call teal, route pink, table_access yellow)
const EDGE_COLORS = {
  import: "#555", call: "#4ec9b0", route: "#f48771",
  table_access: "#dcdcaa", inheritance: "#c586c0", decorator: "#9cdcfe",
  reexport: "#888",
};

// Compute bounding box per logic block once nodes are laid out
function computeClusterBBoxes(nodes, blocks) {
  const byNodeId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const out = [];
  blocks.forEach((blk, i) => {
    const members = (blk.node_ids || []).map(id => byNodeId[id]).filter(Boolean);
    if (members.length < 2) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const m of members) {
      const x = m.position.x, y = m.position.y;
      const w = 200, h = 80;  // approx node size
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x + w > maxX) maxX = x + w;
      if (y + h > maxY) maxY = y + h;
    }
    const PAD = 28;
    const color = blk.is_tests
      ? (data.test_cluster_color || "rgba(180,180,180,0.18)")
      : data.cluster_colors[i % data.cluster_colors.length];
    out.push({
      id: `cluster-${i}`, type: "cluster",
      data: { label: blk.name || `Block ${i}`, color: color },
      position: { x: minX - PAD, y: minY - PAD - 26 },
      style: { width: maxX - minX + PAD * 2, height: maxY - minY + PAD * 2 + 26,
                pointerEvents: "none", zIndex: -1 },
      selectable: false, draggable: false,
    });
  });
  return out;
}

// Build SUMMARY-mode payload: one big block per logic_block, edges
// aggregated cluster→cluster (cross-cluster file refs collapsed).
function buildSummaryGraph(fileNodes, fileEdges, blocks) {
  const fileToBlock = {};
  const blockMeta = blocks.map((blk, i) => {
    const ids = blk.node_ids || [];
    for (const id of ids) fileToBlock[id] = i;
    return {
      i, blk, fileCount: ids.length,
      totalCx: 0, counts: { high: 0, med: 0, low: 0 },
    };
  });
  // Aggregate complexity + findings per block
  for (const fn of fileNodes) {
    const bi = fileToBlock[fn.id];
    if (bi === undefined) continue;
    const meta = blockMeta[bi];
    meta.totalCx += (fn.data.complexity || 0);
    const findings = findingsByFile[fn.data.path] || [];
    for (const f of findings) {
      const sev = String(f.severity || "low").toLowerCase();
      if (sev === "high") meta.counts.high++;
      else if (sev === "med") meta.counts.med++;
      else meta.counts.low++;
    }
  }
  // Grid layout: ~sqrt(N) wide, large gaps so blocks read at zoom-out
  const N = blockMeta.length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(N * 1.4)));
  const W = 320, H = 160;  // cell size including gap
  const summaryNodes = blockMeta.map((meta, k) => {
    const blk = meta.blk;
    const color = blk.is_tests
      ? (data.test_cluster_color || "rgba(180,180,180,0.32)")
      : data.cluster_colors[k % data.cluster_colors.length];
    // Boost saturation for the block fill (separate from cluster region)
    const fill = String(color).replace(/0\.\d+\)$/, "0.55)");
    const border = String(color).replace(/0\.\d+\)$/, "1.0)");
    return {
      id: `block-${meta.i}`,
      type: "block",
      position: { x: (k % cols) * W, y: Math.floor(k / cols) * H },
      data: {
        label: blk.name || `Block ${meta.i}`,
        fileCount: meta.fileCount,
        totalCx: meta.totalCx,
        counts: meta.counts,
        color: fill,
        borderColor: border,
        block_index: meta.i,
        is_tests: !!blk.is_tests,
        member_ids: blk.node_ids || [],
      },
    };
  });
  // Aggregate cluster→cluster edges with weights
  const edgeKey = {};
  for (const e of fileEdges) {
    const a = fileToBlock[e.source], b = fileToBlock[e.target];
    if (a === undefined || b === undefined || a === b) continue;
    const k = `${a}->${b}`;
    edgeKey[k] = (edgeKey[k] || 0) + 1;
  }
  const summaryEdges = Object.entries(edgeKey).map(([k, w]) => {
    const [a, b] = k.split("->").map(Number);
    return {
      id: `se-${k}`, source: `block-${a}`, target: `block-${b}`,
      type: "default", animated: false,
      style: { stroke: "#888", strokeWidth: Math.min(6, 1 + Math.log2(w)),
               opacity: 0.6 },
      label: w > 1 ? String(w) : "",
      data: { weight: w },
    };
  });
  return { summaryNodes, summaryEdges };
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
    style: { stroke: EDGE_COLORS[e.edge_type] || "#555",
             strokeWidth: 1.5, opacity: 0.45 },
    data: { edge_type: e.edge_type },
  })).filter(e =>
    initialNodes.some(n => n.id === e.source) &&
    initialNodes.some(n => n.id === e.target)
  );
  const laid = useMemo(() => forceLayout(initialNodes, initialEdges), []);
  const clusters = useMemo(() => computeClusterBBoxes(laid, data.logic_blocks || []), [laid]);
  const summary = useMemo(
    () => buildSummaryGraph(laid, initialEdges, data.logic_blocks || []),
    [laid]
  );

  const [viewMode, setViewMode] = useState("summary");  // default to summary
  const detailNodes = useMemo(() => [...clusters, ...laid], [clusters, laid]);
  const [nodes, setNodes] = useState(summary.summaryNodes);
  const [edges, setEdges] = useState(summary.summaryEdges);
  const [selected, setSelected] = useState(null);

  const onNodesChange = useCallback((changes) => setNodes(ns => applyNodeChanges(changes, ns)), []);
  const onEdgesChange = useCallback((changes) => setEdges(es => applyEdgeChanges(changes, es)), []);
  const onNodeClick = useCallback((_, node) => {
    setSelected(node);
    if (node.type === "block") {
      renderBlockSidebar(node);
    } else {
      renderSidebar(node);
    }
  }, []);

  // Expose filter + layout + view switch on window
  useEffect(() => {
    window.cm = window.cm || {};
    window.cm.applyFilter = (q) => {
      q = (q || "").toLowerCase();
      setNodes(ns => ns.map(n => ({
        ...n,
        hidden: q && !(n.data.path || "").toLowerCase().includes(q)
                  && !(n.data.label || n.data.name || "").toLowerCase().includes(q),
      })));
    };
    window.cm.setLayout = (mode) => {
      if (viewMode !== "detail") return;  // layout switching only meaningful in detail
      setNodes(ns => {
        const fileNodes = ns.filter(n => n.type === "cm");
        const laid2 = mode === "hierarchical"
          ? hierarchicalLayout(fileNodes, edges)
          : forceLayout(fileNodes, edges);
        const newClusters = computeClusterBBoxes(laid2, data.logic_blocks || []);
        return [...newClusters, ...laid2];
      });
    };
    window.cm.setView = (mode) => {
      setViewMode(mode);
      if (mode === "summary") {
        setNodes(summary.summaryNodes);
        setEdges(summary.summaryEdges);
      } else {
        setNodes(detailNodes);
        setEdges(initialEdges);
      }
    };
  }, [edges, viewMode, summary, detailNodes]);

  const nodeTypes = useMemo(
    () => ({ cm: CMNode, cluster: CMCluster, block: CMBlock }),
    []
  );

  return React.createElement(ReactFlow, {
    nodes, edges, nodeTypes,
    onNodesChange, onEdgesChange, onNodeClick,
    fitView: true, fitViewOptions: { padding: 0.18, includeHiddenNodes: false },
    minZoom: 0.03, maxZoom: 4,
    style: { background: "#1a1a1a" },
  },
    React.createElement(Background, { color: "#333", gap: 20, size: 1 }),
    React.createElement(Controls, { style: { left: 12, bottom: 12 } }),
    React.createElement(MiniMap, {
      nodeColor: (n) => {
        if (n.type === "block") return n.data?.borderColor || "#888";
        if (n.type === "cluster") return "transparent";
        return ({ file: "#9cdcfe", class: "#c586c0", function: "#dcdcaa" }[n.data?.type] || "#666");
      },
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

function renderBlockSidebar(node) {
  const el = document.getElementById("sidebar-content");
  const nd = node.data || {};
  const memberIds = nd.member_ids || [];
  const fileById = {};
  for (const f of (data.nodes || [])) fileById[f.id] = f;
  const lines = [
    `<h2>${escapeHtml(nd.label || "Block")}</h2>`,
    `<div class="meta-row"><b>Files:</b> ${nd.fileCount || 0}</div>`,
    `<div class="meta-row"><b>Total complexity:</b> ${nd.totalCx || 0}</div>`,
  ];
  const c = nd.counts || {};
  if (c.high || c.med || c.low) {
    lines.push(`<div class="meta-row"><b>Findings:</b> ` +
      (c.high ? `<span style="color:#f48771">${c.high}H</span> ` : "") +
      (c.med ? `<span style="color:#dcdcaa">${c.med}M</span> ` : "") +
      (c.low ? `<span style="color:#888">${c.low}L</span>` : "") +
      `</div>`);
  }
  lines.push(`<h2 style="margin-top:14px;">Files in this block</h2>`);
  for (const id of memberIds) {
    const f = fileById[id];
    if (!f) continue;
    const fnds = findingsByFile[f.path] || [];
    const cnt = fnds.length ? ` <span style="color:#888">(${fnds.length})</span>` : "";
    lines.push(`<div class="finding low">` +
      `<span class="rule">${escapeHtml(f.name)}</span>${cnt}<br>` +
      `<span class="desc" style="color:#666">${escapeHtml(f.path || "")}</span></div>`);
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
        {"source": e.source, "target": e.target,
         "edge_type": e.type.value if hasattr(e.type, "value") else str(e.type)}
        for e in repo_map.edges
        if e.source in valid_ids and e.target in valid_ids
    ]
    # Synthetic "Tests" cluster — any file matching test_*.py / *_test.py /
    # /tests/ / /__tests__/ / *.test.* / *.spec.*. Built first so we can
    # exclude these IDs from logic-block clusters (one home per node).
    def _is_test_file(n: dict) -> bool:
        path = (n.get("path") or "").replace("\\", "/").lower()
        name = (n.get("name") or "").lower()
        if "/tests/" in path or "/__tests__/" in path or "/test/" in path:
            return True
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        if ".test." in name or ".spec." in name:
            return True
        return False

    test_ids = [n["id"] for n in nodes_data if _is_test_file(n)]
    test_id_set = set(test_ids)

    # Logic blocks → cluster regions (skip files already in tests cluster)
    logic_blocks = []
    if test_ids:
        logic_blocks.append({
            "name": "Tests",
            "node_ids": test_ids,
            "is_tests": True,
        })
    for blk in (repo_map.logic_blocks or []):
        # Filter node_ids to only files (nodes_data is files-only)
        block_files = [nid for nid in (blk.node_ids or [])
                       if nid in valid_ids and nid not in test_id_set]
        if len(block_files) < 2:
            continue
        logic_blocks.append({
            "name": blk.name or "",
            "node_ids": block_files,
        })
    findings = []
    for key in ("lint_findings", "ai_findings", "verified_findings",
                "claude_findings", "pattern_findings"):
        findings.extend(repo_map.stats.get(key, []))
    xref = repo_map.stats.get("xref")
    if xref and isinstance(xref, dict):
        findings.extend(xref.get("findings", []))

    payload = json.dumps({
        "nodes": nodes_data, "edges": edges_data, "findings": findings,
        "logic_blocks": logic_blocks,
        "cluster_colors": CLUSTER_COLORS,
        "test_cluster_color": TEST_CLUSTER_COLOR,
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
