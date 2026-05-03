"""ui_view — LiteGraph.js node-graph UI (ComfyUI-style).

Single-file output. Loads litegraph.js (MIT) from a CDN — no npm/install.
LiteGraph is the same node-graph engine ComfyUI uses; we get colored
title-bars, group regions (the colored boxes around clusters of nodes),
sockets, drag/zoom, and minimap natively.

Each file → custom LGraphNode with title bar colored by node type and
two sockets (imports-in, imports-out). Each logic block → LGraphGroup
with the block name as the title-bar (this is what gives the ComfyUI
"Sampler Stage 1" / "Upscale Sampling(2x)" look). Click a node →
sidebar shows path/complexity/findings.

Usage from CLI:
    code-mapper /path --lint --ui              # writes repo-map-ui.html
    code-mapper /path --lint --ui custom.html
"""
import html
import json
from pathlib import Path

from .schema import NodeType, RepoMap


# Distinct colors for logic-block group regions (cycled).
# LiteGraph groups expect a CSS color string for the title bar; the body
# fill is derived from this with reduced alpha at draw time.
CLUSTER_COLORS = [
    "#4ec9b0",  # teal
    "#dcdcaa",  # yellow
    "#f48771",  # coral
    "#9cdcfe",  # sky
    "#c586c0",  # purple
    "#73c990",  # green
    "#ffaa5a",  # orange
    "#ff7bb4",  # pink
    "#78a0ff",  # indigo
]
TEST_CLUSTER_COLOR = "#888888"

# Per-node-type title bar colors (matches the file/class/function palette
# we've used in earlier UIs; LiteGraph uses these for the colored band at
# the top of each node).
NODE_TYPE_COLORS = {
    "file": "#3b6c8e",
    "class": "#7e3a93",
    "function": "#7a7a3c",
    "module": "#3b6c8e",
}


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Code Mapper UI — __PROJECT__</title>
<!-- LiteGraph.js (MIT) — same engine ComfyUI uses for its node graph -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/litegraph.js@0.7.18/css/litegraph.css">
<script src="https://cdn.jsdelivr.net/npm/litegraph.js@0.7.18/build/litegraph.js"></script>
<style>
  body { margin: 0; font-family: -apple-system, Segoe UI, sans-serif;
         background: #1a1a1a; color: #e0e0e0; overflow: hidden; }
  #root { position: absolute; top: 44px; left: 0; right: 320px; bottom: 0; }
  #graph-canvas { width: 100%; height: 100%; display: block;
                  background: #1a1a1a; }
  header { position: fixed; top: 0; left: 0; right: 0; height: 44px;
           padding: 0 16px; box-sizing: border-box;
           background: rgba(30,30,30,0.95); border-bottom: 1px solid #333;
           display: flex; align-items: center; gap: 12px; z-index: 10; }
  header h1 { margin: 0; font-size: 14px; font-weight: 600; color: #4ec9b0; }
  header .meta { color: #888; font-size: 11px; }
  header input, header select, header button {
    background: #2a2a2a; color: #ccc; border: 1px solid #444;
    padding: 4px 10px; font-size: 11px; border-radius: 3px;
  }
  header input { width: 240px; }
  #sidebar { position: fixed; top: 44px; right: 0; bottom: 0; width: 320px;
             background: #1e1e1e; border-left: 1px solid #333;
             padding: 14px 18px; overflow-y: auto; box-sizing: border-box; }
  #sidebar.collapsed { transform: translateX(100%); }
  #sidebar h2 { margin: 0 0 8px 0; font-size: 13px; color: #4ec9b0;
                font-weight: 600; }
  #sidebar .empty { color: #666; font-style: italic; font-size: 11px; }
  #sidebar .meta-row { font-size: 11px; color: #aaa; margin: 4px 0; }
  #sidebar .meta-row b { color: #ccc; }
  #sidebar .finding { margin: 6px 0; padding: 6px 8px; border-radius: 3px;
                       font-size: 11px; background: #2a2a2a;
                       border-left: 3px solid #555; }
  #sidebar .finding.high { border-left-color: #f48771; }
  #sidebar .finding.med  { border-left-color: #dcdcaa; }
  #sidebar .finding.low  { border-left-color: #555; }
  #sidebar .finding .rule { font-weight: 600; color: #9cdcfe; }
  #sidebar .finding .desc { color: #aaa; }
  #sidebar .conn { margin: 4px 0; padding: 6px 8px; border-radius: 3px;
                   font-size: 11px; background: #2a2a2a;
                   border-left: 3px solid #4ec9b0; cursor: pointer; }
  #sidebar .conn:hover { background: #353535; border-left-color: #6fe0c4; }
  #sidebar .conn .rule { font-weight: 600; color: #9cdcfe; }
  #sidebar .conn .etype { color: #888; font-size: 10px; padding-left: 6px; }
  #sidebar .conn .desc { color: #666; font-size: 10px; }
  #sidebar .empty { color: #666; font-style: italic; font-size: 11px; }
  #error-box { display: none; position: fixed; top: 60px; left: 20px;
               right: 340px; padding: 16px 20px; background: #2a1010;
               border: 1px solid #f48771; color: #f48771;
               font-family: monospace; font-size: 12px; z-index: 100;
               border-radius: 4px; }
  #blocks-panel { position: fixed; top: 56px; left: 12px; width: 240px;
                  max-height: calc(100vh - 80px);
                  background: rgba(30,30,30,0.96); border: 1px solid #333;
                  border-radius: 4px; padding: 10px 12px; z-index: 5;
                  display: flex; flex-direction: column; }
  #blocks-panel.collapsed { max-height: 36px; overflow: hidden; }
  #blocks-panel header { all: unset; display: flex; align-items: center;
                         gap: 8px; margin-bottom: 6px; flex-shrink: 0;
                         cursor: move; user-select: none; }
  #blocks-panel h3 { margin: 0; font-size: 12px; color: #4ec9b0;
                     font-weight: 600; flex: 1;
                     text-transform: uppercase; letter-spacing: 0.5px; }
  #blocks-panel .panel-btn { background: #2a2a2a; color: #ccc;
                              border: 1px solid #444; padding: 2px 6px;
                              font-size: 10px; cursor: pointer;
                              border-radius: 2px; }
  #blocks-panel .panel-btn:hover { background: #353535; }
  #blocks-list { overflow-y: auto; flex: 1; }
  .block-row { display: flex; align-items: center; gap: 6px;
               padding: 4px 6px; cursor: pointer; border-radius: 3px;
               font-size: 11px; color: #ccc; }
  .block-row:hover { background: #2a2a2a; }
  .block-row .swatch { width: 10px; height: 10px; border-radius: 2px;
                       flex-shrink: 0; }
  .block-row .name { flex: 1; overflow: hidden; text-overflow: ellipsis;
                     white-space: nowrap; }
  .block-row .toggle { padding: 2px 6px; background: #333; color: #ccc;
                       border-radius: 2px; font-size: 10px;
                       flex-shrink: 0; min-width: 32px; text-align: center; }
  .block-row.hidden .name { color: #555; }
  .block-row.hidden .toggle { background: #1a1a1a; color: #666; }
</style>
</head><body>
<header>
  <h1>Code Mapper</h1>
  <span class="meta">__PROJECT__ · __N_NODES__ nodes · __N_EDGES__ edges · __N_FINDINGS__ findings</span>
  <input id="filter" placeholder="Filter (file, function, rule)..."
         oninput="window.cm.applyFilter(this.value)">
  <select id="viewMode" onchange="window.cm.setView(this.value)">
    <option value="summary" selected>Summary (logic blocks)</option>
    <option value="detail">Detail (all files)</option>
  </select>
  <button onclick="window.cm.fitView()">Fit</button>
  <button onclick="document.getElementById('sidebar').classList.toggle('collapsed')">
    Toggle Sidebar
  </button>
</header>
<div id="root">
  <canvas id="graph-canvas"></canvas>
</div>
<div id="blocks-panel">
  <header>
    <h3>Logic Blocks</h3>
    <button class="panel-btn" onclick="window.cm.drillBack()">↶ Summary</button>
    <button class="panel-btn"
            onclick="document.getElementById('blocks-panel').classList.toggle('collapsed')">−</button>
  </header>
  <div id="blocks-list"></div>
</div>
<div id="error-box"></div>
<div id="sidebar">
  <h2>Selected Node</h2>
  <div id="sidebar-content"><div class="empty">Click a node to inspect</div></div>
</div>
<script>
(function(){
  "use strict";
  const data = __DATA_JSON__;

  // Surface load failures visibly
  window.addEventListener("error", function(e){
    if (typeof LiteGraph === "undefined") {
      const box = document.getElementById("error-box");
      box.style.display = "block";
      box.innerHTML = "<b>UI failed to load.</b><br>" +
        (e.message || e.error || "(unknown)") +
        "<br><br>Likely cause: jsdelivr CDN blocked. " +
        "Try a different network or download litegraph.js locally.";
    }
  });

  if (typeof LiteGraph === "undefined") {
    document.getElementById("error-box").style.display = "block";
    document.getElementById("error-box").innerHTML =
      "<b>LiteGraph not loaded.</b> Check network — jsdelivr.net blocked?";
    return;
  }

  // ---------------- Constants from Python side ----------------
  const NODE_TYPE_COLORS = __NODE_TYPE_COLORS__;

  // ---------------- Findings index ----------------
  const findingsByFile = {};
  for (const f of (data.findings || [])) {
    const p = f.path || f.file_path || "";
    if (!p) continue;
    (findingsByFile[p] = findingsByFile[p] || []).push(f);
  }

  // ---------------- Custom node class ----------------
  function CMFileNode() {
    this.addInput("in", "*");
    this.addOutput("out", "*");
    this.size = [220, 60];
    this.cmData = null;  // populated after instantiation
  }
  CMFileNode.title = "file";
  CMFileNode.prototype.onDrawForeground = function(ctx) {
    if (!this.cmData) return;
    const nd = this.cmData;
    const findings = findingsByFile[nd.path] || [];
    let high = 0, med = 0;
    for (const f of findings) {
      const sev = String(f.severity || "low").toLowerCase();
      if (sev === "high") high++;
      else if (sev === "med") med++;
    }
    ctx.fillStyle = "#bbb";
    ctx.font = "11px -apple-system, Segoe UI, sans-serif";
    ctx.fillText("cx " + (nd.complexity || 0), 10, 32);
    if (high > 0) {
      ctx.fillStyle = "#f48771";
      ctx.fillText(high + "H", 60, 32);
    }
    if (med > 0) {
      ctx.fillStyle = "#dcdcaa";
      ctx.fillText(med + "M", 80, 32);
    }
    if (findings.length > 0) {
      ctx.fillStyle = "#888";
      ctx.font = "10px -apple-system, Segoe UI, sans-serif";
      ctx.fillText("(" + findings.length + " findings)", 10, 48);
    }
  };
  CMFileNode.prototype.onSelected = function() {
    if (this.cmData) renderSidebar(this.cmData);
  };
  LiteGraph.registerNodeType("cm/file", CMFileNode);

  // ---------------- Layout ----------------
  // Group files by their logic block, then position blocks in a grid.
  // Within each block, files arrange in a column.
  function layout(nodesData, blocks) {
    const positions = {};  // id → {x,y}
    const fileToBlock = {};
    blocks.forEach((blk, bi) => {
      for (const id of (blk.node_ids || [])) fileToBlock[id] = bi;
    });

    // Bucket: nodes per block, plus an "unassigned" bucket
    const buckets = blocks.map(() => []);
    const unassigned = [];
    for (const n of nodesData) {
      const bi = fileToBlock[n.id];
      if (bi === undefined) unassigned.push(n);
      else buckets[bi].push(n);
    }
    if (unassigned.length) {
      buckets.push(unassigned);
    }

    // Block grid: ~sqrt(N) wide
    const NB = buckets.length;
    const blockCols = Math.max(1, Math.ceil(Math.sqrt(NB * 1.4)));
    const NODE_W = 220, NODE_H = 70, NODE_GAP = 14;
    const BLOCK_PAD = 50;

    // Compute size + position of each block
    const blockBoxes = [];
    let cursorX = 0, cursorY = 60, rowHeight = 0, colCount = 0;
    buckets.forEach((bucket, bi) => {
      const cols = Math.max(1, Math.ceil(Math.sqrt(bucket.length)));
      const rows = Math.ceil(bucket.length / cols);
      const w = cols * (NODE_W + NODE_GAP) + BLOCK_PAD;
      const h = rows * (NODE_H + NODE_GAP) + BLOCK_PAD + 40;
      blockBoxes.push({ bi, x: cursorX, y: cursorY, w, h, cols, rows, bucket });
      cursorX += w + 80;
      rowHeight = Math.max(rowHeight, h);
      colCount++;
      if (colCount >= blockCols) {
        cursorX = 0; cursorY += rowHeight + 80; rowHeight = 0; colCount = 0;
      }
    });

    // Place each file inside its block
    for (const box of blockBoxes) {
      const bx = box.x + BLOCK_PAD / 2;
      const by = box.y + BLOCK_PAD / 2 + 30;
      box.bucket.forEach((n, i) => {
        const r = Math.floor(i / box.cols);
        const c = i % box.cols;
        positions[n.id] = {
          x: bx + c * (NODE_W + NODE_GAP),
          y: by + r * (NODE_H + NODE_GAP),
        };
      });
    }
    return { positions, blockBoxes };
  }

  // ---------------- Build graph ----------------
  const canvas = document.getElementById("graph-canvas");
  const graph = new LGraph();
  const lgcanvas = new LGraphCanvas("#graph-canvas", graph);
  // Tweak LiteGraph theme to match the dark UI
  lgcanvas.background_image = null;
  lgcanvas.clear_background = true;
  lgcanvas.render_shadows = false;
  lgcanvas.render_canvas_border = false;
  lgcanvas.allow_searchbox = false;

  const blocksMeta = data.logic_blocks || [];

  // ---------------- Block-summary node class ----------------
  // ONE node per logic block. Big card, colored title bar = block color,
  // body shows file count + complexity total + finding totals.
  function CMBlockNode() {
    this.addOutput("→", "*");
    this.addInput("←", "*");
    this.size = [260, 110];
    this.cmBlock = null;
  }
  CMBlockNode.title = "block";
  CMBlockNode.prototype.onDrawForeground = function(ctx) {
    if (!this.cmBlock) return;
    const b = this.cmBlock;
    ctx.fillStyle = "#fff";
    ctx.font = "bold 13px -apple-system, Segoe UI, sans-serif";
    ctx.fillText(b.fileCount + " files", 12, 36);
    ctx.fillStyle = "#bbb";
    ctx.font = "11px -apple-system, Segoe UI, sans-serif";
    ctx.fillText("complexity " + b.totalCx, 12, 56);
    let x = 12;
    if (b.counts.high > 0) {
      ctx.fillStyle = "#f48771";
      ctx.fillText(b.counts.high + "H", x, 78); x += 32;
    }
    if (b.counts.med > 0) {
      ctx.fillStyle = "#dcdcaa";
      ctx.fillText(b.counts.med + "M", x, 78); x += 32;
    }
    if (b.counts.low > 0) {
      ctx.fillStyle = "#888";
      ctx.fillText(b.counts.low + "L", x, 78);
    }
    if (!b.counts.high && !b.counts.med && !b.counts.low) {
      ctx.fillStyle = "#4a4";
      ctx.fillText("clean", 12, 78);
    }
  };
  CMBlockNode.prototype.onSelected = function() {
    if (this.cmBlock) renderBlockSidebar(this.cmBlock);
  };
  LiteGraph.registerNodeType("cm/block", CMBlockNode);

  // Per-node title text color override. LiteGraph reads
  // NODE_TITLE_TEXT_COLOR globally, so we swap it during the draw of
  // any node that sets `_titleTextColor`. File nodes keep the default
  // white-on-dark; block-summary nodes use black-on-bright.
  const _origDrawNodeShape = LGraphCanvas.prototype.drawNodeShape;
  LGraphCanvas.prototype.drawNodeShape = function(node, ctx, ...rest) {
    const orig = LiteGraph.NODE_TITLE_TEXT_COLOR;
    if (node && node._titleTextColor) {
      LiteGraph.NODE_TITLE_TEXT_COLOR = node._titleTextColor;
    }
    try { return _origDrawNodeShape.call(this, node, ctx, ...rest); }
    finally { LiteGraph.NODE_TITLE_TEXT_COLOR = orig; }
  };

  // ---------------- Build adjacency + blocksMeta enrichment ----------------
  const adjacency = {};
  const fileById = {};
  for (const fn of (data.nodes || [])) {
    fileById[fn.id] = fn;
    adjacency[fn.id] = { imports: [], importedBy: [] };
  }
  for (const e of (data.edges || [])) {
    if (!fileById[e.source] || !fileById[e.target]) continue;
    adjacency[e.source].imports.push({ id: e.target, type: e.edge_type });
    adjacency[e.target].importedBy.push({ id: e.source, type: e.edge_type });
  }

  // Per-block aggregate metadata for summary nodes
  const fileToBlock = {};
  blocksMeta.forEach((blk, bi) => {
    for (const id of (blk.node_ids || [])) fileToBlock[id] = bi;
  });
  const blockAgg = blocksMeta.map((blk, bi) => ({
    bi, name: blk.name || `Block ${bi}`,
    is_tests: !!blk.is_tests,
    color: blk.is_tests
      ? (data.test_cluster_color || "#888888")
      : (data.cluster_colors[bi % data.cluster_colors.length] || "#4ec9b0"),
    fileCount: (blk.node_ids || []).length,
    member_ids: blk.node_ids || [],
    totalCx: 0, counts: { high: 0, med: 0, low: 0 },
  }));
  for (const n of (data.nodes || [])) {
    const bi = fileToBlock[n.id];
    if (bi === undefined) continue;
    blockAgg[bi].totalCx += (n.complexity || 0);
    const fnds = findingsByFile[n.path] || [];
    for (const f of fnds) {
      const sev = String(f.severity || "low").toLowerCase();
      if (sev === "high") blockAgg[bi].counts.high++;
      else if (sev === "med") blockAgg[bi].counts.med++;
      else blockAgg[bi].counts.low++;
    }
  }
  // Aggregated cluster→cluster edges with counts (block i → block j)
  const blockEdgeCounts = {};
  for (const e of (data.edges || [])) {
    const a = fileToBlock[e.source], b = fileToBlock[e.target];
    if (a === undefined || b === undefined || a === b) continue;
    const k = a + "->" + b;
    blockEdgeCounts[k] = (blockEdgeCounts[k] || 0) + 1;
  }

  // Track current view state + which detail-block is focused (if any)
  const nodesById = {};        // file id → LiteGraph node (detail mode)
  const blockNodesById = {};   // block index → LiteGraph node (summary mode)
  let currentMode = "summary";
  let detailFocusBlock = null;  // bi if drill-in mode, else null

  // ---------------- Builders: summary + detail ----------------
  function clearGraph() {
    // Remove all groups + nodes
    if (graph._groups) graph._groups.length = 0;
    const toRemove = [...(graph._nodes || [])];
    for (const node of toRemove) graph.remove(node);
    for (const k in nodesById) delete nodesById[k];
    for (const k in blockNodesById) delete blockNodesById[k];
  }

  function buildSummaryGraph() {
    clearGraph();
    // Layout: grid, ~sqrt(N) wide. Skip empty blocks.
    const visibleBlocks = blockAgg.filter(b => b.fileCount > 0);
    const cols = Math.max(1, Math.ceil(Math.sqrt(visibleBlocks.length * 1.2)));
    const W = 320, H = 160;
    visibleBlocks.forEach((b, k) => {
      const node = LiteGraph.createNode("cm/block");
      if (!node) return;
      node.title = b.name + " (" + b.fileCount + ")";
      node.pos = [(k % cols) * W, Math.floor(k / cols) * H];
      node.color = b.color;     // title bar
      node.bgcolor = "#1f1f1f"; // body
      node._titleTextColor = "#000";  // black title on bright bar
      node.cmBlock = b;
      graph.add(node);
      blockNodesById[b.bi] = node;
    });
    // Draw cross-block edges
    for (const k in blockEdgeCounts) {
      const [a, b] = k.split("->").map(Number);
      const na = blockNodesById[a], nb = blockNodesById[b];
      if (!na || !nb) continue;
      na.connect(0, nb, 0);
    }
    currentMode = "summary";
    detailFocusBlock = null;
    setTimeout(fitView, 30);
  }

  function buildDetailGraph(focusBi) {
    // focusBi = optional block index — restrict to that block's files
    clearGraph();
    const focused = (focusBi !== null && focusBi !== undefined);
    const visibleFileIds = focused
      ? new Set(blockAgg[focusBi].member_ids)
      : null;

    const { positions, blockBoxes } = layout(
      data.nodes.filter(n => !visibleFileIds || visibleFileIds.has(n.id)),
      blocksMeta
    );
    // Add LGraphGroups for visible blocks
    for (const box of blockBoxes) {
      if (box.bi >= blocksMeta.length) continue;
      if (focused && box.bi !== focusBi) continue;
      const blk = blocksMeta[box.bi];
      const color = blk.is_tests
        ? (data.test_cluster_color || "#888888")
        : (data.cluster_colors[box.bi % data.cluster_colors.length] || "#4ec9b0");
      const g = new LiteGraph.LGraphGroup();
      g.title = blk.name || `Block ${box.bi}`;
      g.pos = [box.x, box.y];
      g.size = [box.w, box.h];
      g.color = color;
      graph.add(g);
    }
    // Add file nodes
    for (const n of (data.nodes || [])) {
      if (visibleFileIds && !visibleFileIds.has(n.id)) continue;
      const node = LiteGraph.createNode("cm/file");
      if (!node) continue;
      node.title = n.name || "(unnamed)";
      const pos = positions[n.id] || { x: 0, y: 0 };
      node.pos = [pos.x, pos.y];
      node.color = NODE_TYPE_COLORS[n.type] || "#3b6c8e";
      node.bgcolor = "#2a2a2a";
      node._origBgcolor = node.bgcolor;
      node._origColor = node.color;
      node.cmData = n;
      graph.add(node);
      nodesById[n.id] = node;
    }
    // Edges (only between visible nodes)
    for (const e of (data.edges || [])) {
      const a = nodesById[e.source], b = nodesById[e.target];
      if (!a || !b) continue;
      a.connect(0, b, 0);
    }
    currentMode = "detail";
    detailFocusBlock = focused ? focusBi : null;
    setTimeout(fitView, 30);
  }

  // ---------------- Logic Blocks side panel ----------------
  // In summary mode: row click = drill into that block (detail mode focused)
  // In detail mode (full): row click = collapse just that block's files
  function buildBlocksPanel() {
    const blocksList = document.getElementById("blocks-list");
    blocksList.innerHTML = "";
    for (const b of blockAgg) {
      if (b.fileCount === 0) continue;
      const row = document.createElement("div");
      row.className = "block-row";

      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = b.color;
      row.appendChild(swatch);

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = b.name + " (" + b.fileCount + ")";
      row.appendChild(name);

      const tog = document.createElement("span");
      tog.className = "toggle";
      tog.textContent = "drill";
      row.appendChild(tog);

      row.addEventListener("click", () => {
        // Drill into this block: detail mode, only its files
        document.getElementById("viewMode").value = "detail";
        buildDetailGraph(b.bi);
      });
      blocksList.appendChild(row);
    }
  }
  buildBlocksPanel();

  // Make the Logic Blocks panel draggable by its header
  (function makeDraggable() {
    const panel = document.getElementById("blocks-panel");
    const handle = panel.querySelector("header");
    let dragging = false, ox = 0, oy = 0;
    handle.addEventListener("mousedown", function(e) {
      // Don't start drag when clicking a button inside the header
      if (e.target.tagName === "BUTTON") return;
      dragging = true;
      const r = panel.getBoundingClientRect();
      ox = e.clientX - r.left;
      oy = e.clientY - r.top;
      panel.style.right = "auto";  // detach from initial right anchor
      e.preventDefault();
    });
    window.addEventListener("mousemove", function(e) {
      if (!dragging) return;
      const x = Math.max(0, Math.min(window.innerWidth - 80, e.clientX - ox));
      const y = Math.max(44, Math.min(window.innerHeight - 40, e.clientY - oy));
      panel.style.left = x + "px";
      panel.style.top = y + "px";
    });
    window.addEventListener("mouseup", function() { dragging = false; });
  })();

  // ---------------- Initial build (summary mode) ----------------
  function fitView() {
    if (!graph._nodes.length) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const node of graph._nodes) {
      const x = node.pos[0], y = node.pos[1];
      const w = node.size[0], h = node.size[1];
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x + w > maxX) maxX = x + w;
      if (y + h > maxY) maxY = y + h;
    }
    for (const grp of graph._groups || []) {
      const x = grp.pos[0], y = grp.pos[1];
      const w = grp.size[0], h = grp.size[1];
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x + w > maxX) maxX = x + w;
      if (y + h > maxY) maxY = y + h;
    }
    const W = canvas.clientWidth, H = canvas.clientHeight;
    const PAD = 80;
    const sx = W / (maxX - minX + PAD * 2);
    const sy = H / (maxY - minY + PAD * 2);
    const s = Math.min(sx, sy, 1.0);
    lgcanvas.ds.scale = s;
    lgcanvas.ds.offset[0] = -(minX - PAD) * s;
    lgcanvas.ds.offset[1] = -(minY - PAD) * s;
    lgcanvas.setDirty(true, true);
  }

  // Resize canvas to container
  function resize() {
    const r = document.getElementById("root").getBoundingClientRect();
    canvas.width = r.width;
    canvas.height = r.height;
    lgcanvas.setDirty(true, true);
  }
  window.addEventListener("resize", resize);
  resize();

  // Build the initial graph (default = summary mode)
  buildSummaryGraph();

  // Highlight: dim non-neighbors, brighten selected + connected, mark
  // their links so LiteGraph draws them prominently.
  const DIM_BG = "#161616", DIM_TITLE = "#3a3a3a";
  const SEL_BG = "#3a3a3a";  // brightened body for selected node

  function applyHighlight(selectedId) {
    if (!selectedId) {
      for (const node of graph._nodes) {
        if (!node.cmData) continue;
        node.bgcolor = node._origBgcolor;
        node.color = node._origColor;
      }
      lgcanvas.highlighted_links = {};
      lgcanvas.setDirty(true, true);
      return;
    }
    const adj = adjacency[selectedId] || { imports: [], importedBy: [] };
    const neighbors = new Set([selectedId]);
    for (const x of adj.imports) neighbors.add(x.id);
    for (const x of adj.importedBy) neighbors.add(x.id);
    for (const node of graph._nodes) {
      if (!node.cmData) continue;
      if (node.cmData.id === selectedId) {
        node.bgcolor = SEL_BG;
        node.color = node._origColor;
      } else if (neighbors.has(node.cmData.id)) {
        node.bgcolor = node._origBgcolor;
        node.color = node._origColor;
      } else {
        node.bgcolor = DIM_BG;
        node.color = DIM_TITLE;
      }
    }
    const selNode = nodesById[selectedId];
    const linkIds = {};
    if (selNode) {
      for (const inp of (selNode.inputs || [])) {
        if (inp && inp.link != null) linkIds[inp.link] = true;
      }
      for (const out of (selNode.outputs || [])) {
        for (const lid of (out.links || [])) linkIds[lid] = true;
      }
    }
    lgcanvas.highlighted_links = linkIds;
    lgcanvas.setDirty(true, true);
  }

  // Selection → sidebar + highlight
  lgcanvas.onNodeSelected = function(n) {
    if (n && n.cmData) {
      renderSidebar(n.cmData);
      applyHighlight(n.cmData.id);
    } else if (n && n.cmBlock) {
      renderBlockSidebar(n.cmBlock);
    }
  };
  lgcanvas.onNodeDeselected = function() {
    document.getElementById("sidebar-content").innerHTML =
      '<div class="empty">Click a node to inspect</div>';
    applyHighlight(null);
  };

  // ---------------- View / filter / fit hooks ----------------
  window.cm = {
    fitView: fitView,
    applyFilter: function(q) {
      q = (q || "").toLowerCase();
      for (const node of graph._nodes) {
        if (!node.cmData) continue;
        const hit = !q ||
          String(node.cmData.path || "").toLowerCase().includes(q) ||
          String(node.cmData.name || "").toLowerCase().includes(q);
        node.flags = node.flags || {};
        if (hit) {
          node.bgcolor = "#2a2a2a";
          node.boxcolor = null;
        } else {
          node.bgcolor = "#1a1a1a";
        }
      }
      lgcanvas.setDirty(true, true);
    },
    setView: function(mode) {
      if (mode === "summary") buildSummaryGraph();
      else buildDetailGraph(null);
    },
    drillBack: function() {
      // Return to summary from a focused detail view
      document.getElementById("viewMode").value = "summary";
      buildSummaryGraph();
    },
  };

  // ---------------- Sidebar ----------------
  function _connHtml(x) {
    const f = fileById[x.id];
    if (!f) return "";
    return `<div class="conn" data-id="${escapeHtml(x.id)}">` +
      `<span class="rule">${escapeHtml(f.name)}</span>` +
      `<span class="etype">${escapeHtml(x.type || "import")}</span><br>` +
      `<span class="desc">${escapeHtml(f.path || "")}</span></div>`;
  }

  function renderSidebar(nd) {
    const el = document.getElementById("sidebar-content");
    if (!nd) { el.innerHTML = '<div class="empty">Click a node to inspect</div>'; return; }
    const findings = findingsByFile[nd.path] || [];
    const adj = adjacency[nd.id] || { imports: [], importedBy: [] };
    const lines = [
      `<h2>${escapeHtml(nd.name)}</h2>`,
      `<div class="meta-row"><b>Type:</b> ${escapeHtml(nd.type || "file")}</div>`,
      `<div class="meta-row"><b>Path:</b> ${escapeHtml(nd.path || "")}</div>`,
      `<div class="meta-row"><b>Complexity:</b> ${nd.complexity || 0}</div>`,
      `<div class="meta-row"><b>Lines:</b> ${nd.line_start || 1}–${nd.line_end || ""}</div>`,
      `<h2 style="margin-top:14px;">Imports (${adj.imports.length})</h2>`,
    ];
    if (!adj.imports.length) lines.push('<div class="empty">No outgoing edges</div>');
    for (const x of adj.imports) lines.push(_connHtml(x));
    lines.push(`<h2 style="margin-top:14px;">Imported by (${adj.importedBy.length})</h2>`);
    if (!adj.importedBy.length) lines.push('<div class="empty">Top-level / unused</div>');
    for (const x of adj.importedBy) lines.push(_connHtml(x));
    lines.push(`<h2 style="margin-top:14px;">Findings (${findings.length})</h2>`);
    if (findings.length === 0) lines.push('<div class="empty">No lint findings</div>');
    for (const f of findings) {
      const sev = String(f.severity || "low").toLowerCase();
      lines.push(
        `<div class="finding ${sev}">` +
        `<span class="rule">${escapeHtml(f.rule || "?")}</span> ` +
        `<span style="color:#888">@ line ${f.line || 0}</span><br>` +
        `<span class="desc">${escapeHtml((f.desc || "").slice(0, 200))}</span>` +
        `</div>`
      );
    }
    el.innerHTML = lines.join("");
    // Wire connection-row clicks → jump to that node
    for (const cel of el.querySelectorAll(".conn")) {
      cel.addEventListener("click", function() {
        const id = cel.getAttribute("data-id");
        const tn = nodesById[id]; if (!tn) return;
        if (lgcanvas.deselectAllNodes) lgcanvas.deselectAllNodes();
        if (lgcanvas.selectNode) lgcanvas.selectNode(tn, false);
        if (lgcanvas.centerOnNode) lgcanvas.centerOnNode(tn);
        renderSidebar(tn.cmData);
        applyHighlight(tn.cmData.id);
      });
    }
  }

  // Block-summary sidebar — fires when a CMBlockNode is clicked
  function renderBlockSidebar(b) {
    const el = document.getElementById("sidebar-content");
    const c = b.counts || {};
    const lines = [
      `<h2>${escapeHtml(b.name)}</h2>`,
      `<div class="meta-row"><b>Files:</b> ${b.fileCount}</div>`,
      `<div class="meta-row"><b>Total complexity:</b> ${b.totalCx}</div>`,
    ];
    if (c.high || c.med || c.low) {
      lines.push(`<div class="meta-row"><b>Findings:</b> ` +
        (c.high ? `<span style="color:#f48771">${c.high}H</span> ` : "") +
        (c.med ? `<span style="color:#dcdcaa">${c.med}M</span> ` : "") +
        (c.low ? `<span style="color:#888">${c.low}L</span>` : "") +
        `</div>`);
    }
    lines.push(`<button class="panel-btn" style="margin-top:10px;" ` +
      `onclick="window.cm.setView('detail');setTimeout(()=>{` +
      `document.getElementById('viewMode').value='detail';` +
      `},10);" data-bi="${b.bi}">Drill into this block</button>`);
    lines.push(`<h2 style="margin-top:14px;">Files (${b.fileCount})</h2>`);
    for (const id of (b.member_ids || [])) {
      const f = fileById[id]; if (!f) continue;
      const fnds = findingsByFile[f.path] || [];
      const cnt = fnds.length ? ` <span style="color:#888">(${fnds.length})</span>` : "";
      lines.push(`<div class="finding low">` +
        `<span class="rule">${escapeHtml(f.name)}</span>${cnt}<br>` +
        `<span class="desc" style="color:#666">${escapeHtml(f.path || "")}</span></div>`);
    }
    el.innerHTML = lines.join("");
    // Wire the drill-in button to also focus on this block
    const btn = el.querySelector("button[data-bi]");
    if (btn) {
      btn.addEventListener("click", function(ev) {
        ev.stopPropagation();
        document.getElementById("viewMode").value = "detail";
        buildDetailGraph(b.bi);
      });
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  graph.start();  // begin render loop
})();
</script>
</body></html>
"""


def _is_test_file(node: dict) -> bool:
    """File path / name patterns that mark a file as a test."""
    path = (node.get("path") or "").replace("\\", "/").lower()
    name = (node.get("name") or "").lower()
    if "/tests/" in path or "/__tests__/" in path or "/test/" in path:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    return False


def _top_dir(path: str) -> str:
    """First non-empty directory segment, normalized."""
    p = (path or "").replace("\\", "/")
    if "/" not in p:
        return "(root)"
    parts = [s for s in p.split("/") if s]
    return parts[0] if parts else "(root)"


def _split_oversized_block(name: str, node_ids: list,
                           id_to_path: dict, max_files: int) -> list:
    """If a block has > max_files, split by top-level directory."""
    if len(node_ids) <= max_files:
        return [{"name": name, "node_ids": node_ids}]

    by_dir: dict[str, list] = {}
    for nid in node_ids:
        d = _top_dir(id_to_path.get(nid, ""))
        by_dir.setdefault(d, []).append(nid)
    # Singletons collapse into "(misc)"
    misc: list = []
    out: list = []
    for d, ids in by_dir.items():
        if len(ids) < 2:
            misc.extend(ids)
        else:
            out.append({"name": f"{name} / {d}", "node_ids": ids})
    if misc:
        out.append({"name": f"{name} / (misc)", "node_ids": misc})
    # Sort largest first so summary nodes lay out predictably
    out.sort(key=lambda b: -len(b["node_ids"]))
    return out


def _build_logic_blocks(repo_map: RepoMap, valid_ids: set,
                        nodes_data: list) -> list:
    """Synthetic Tests cluster + filtered logic_blocks from repo_map.

    Big buckets (e.g. 'Utilities / Standalone' with 381 files in FRed)
    get split by top-level directory so no single block dominates.
    """
    SPLIT_THRESHOLD = 50
    test_ids = [n["id"] for n in nodes_data if _is_test_file(n)]
    test_id_set = set(test_ids)
    id_to_path = {n["id"]: n.get("path", "") for n in nodes_data}

    blocks = []
    if test_ids:
        blocks.append({"name": "Tests", "node_ids": test_ids,
                       "is_tests": True})
    for blk in (repo_map.logic_blocks or []):
        block_files = [nid for nid in (blk.node_ids or [])
                       if nid in valid_ids and nid not in test_id_set]
        if len(block_files) < 2:
            continue
        # Split mega-blocks by top-level dir
        for sub in _split_oversized_block(
            blk.name or "Block", block_files, id_to_path, SPLIT_THRESHOLD
        ):
            blocks.append(sub)
    return blocks


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
    logic_blocks = _build_logic_blocks(repo_map, valid_ids, nodes_data)

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
            .replace("__NODE_TYPE_COLORS__", json.dumps(NODE_TYPE_COLORS))
            .replace("__DATA_JSON__", payload))


def write_ui_html(repo_map: RepoMap, output_path: Path,
                   project_path: str = "") -> Path:
    output_path.write_text(render_ui(repo_map, project_path), encoding="utf-8")
    return output_path
