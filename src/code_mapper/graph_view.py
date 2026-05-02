"""Visual graph view — single-file HTML with D3 force-directed layout.

Generates a self-contained HTML page from the repo-map.json. Nodes = files,
edges = imports. Node size = complexity, color = language (Python blue,
JS yellow, mixed gray). Click + drag, zoom/pan, hover tooltip.

No npm install needed — D3 from CDN.

Usage from CLI:
    code-mapper /path --graph                 # writes repo-map-graph.html
    code-mapper /path --graph custom.html
"""
from __future__ import annotations
import html
import json
from pathlib import Path

from .schema import EdgeType, NodeType, RepoMap


GRAPH_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Code Mapper Graph — {project}</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<style>
  body {{ margin: 0; font-family: -apple-system, Segoe UI, sans-serif;
         background: #1e1e1e; color: #e0e0e0; }}
  header {{ position: fixed; top: 0; left: 0; right: 0; padding: 12px 24px;
           background: rgba(37, 37, 38, 0.95); border-bottom: 1px solid #444;
           z-index: 10; backdrop-filter: blur(4px); }}
  h1 {{ margin: 0; font-size: 18px; color: #4ec9b0; display: inline-block; }}
  .meta {{ display: inline-block; margin-left: 16px; color: #888; font-size: 12px; }}
  .controls {{ position: fixed; top: 56px; left: 16px; background: #252526;
               padding: 12px; border: 1px solid #444; border-radius: 4px;
               z-index: 10; font-size: 12px; }}
  .controls input {{ background: #1e1e1e; color: #e0e0e0; border: 1px solid #555;
                     padding: 4px 8px; width: 200px; }}
  .legend {{ position: fixed; bottom: 16px; left: 16px; background: #252526;
             padding: 10px; border: 1px solid #444; border-radius: 4px;
             font-size: 11px; }}
  .legend-row {{ display: flex; align-items: center; gap: 6px; margin: 2px 0; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  .tooltip {{ position: absolute; background: #2d2d30; border: 1px solid #555;
              padding: 6px 10px; border-radius: 3px; font-size: 12px;
              pointer-events: none; max-width: 320px; }}
  svg {{ width: 100vw; height: 100vh; }}
  .node circle {{ stroke: #444; stroke-width: 1px; cursor: pointer; }}
  .node text {{ font-size: 9px; fill: #aaa; pointer-events: none; }}
  .node:hover circle {{ stroke: #fff; stroke-width: 2px; }}
  .link {{ stroke: #555; stroke-opacity: 0.4; }}
  .link.highlight {{ stroke: #4ec9b0; stroke-opacity: 1; stroke-width: 2px; }}
</style>
</head><body>
<header>
  <h1>Code Mapper</h1>
  <span class="meta">{project} · {n_files} files · {n_edges} imports</span>
</header>
<div class="controls">
  <div><input id="filter" placeholder="Filter by name..." oninput="applyFilter()"></div>
  <div style="margin-top: 6px;"><label><input type="checkbox" id="showLabels" checked
        onchange="toggleLabels()"> Labels</label></div>
</div>
<div class="legend">
  <div class="legend-row"><span class="legend-dot" style="background:#9cdcfe"></span> Python</div>
  <div class="legend-row"><span class="legend-dot" style="background:#dcdcaa"></span> JS / TS / JSX</div>
  <div class="legend-row"><span class="legend-dot" style="background:#888"></span> Other</div>
  <div class="legend-row" style="margin-top:6px; color:#888;">Size = complexity</div>
</div>
<svg></svg>
<script>
const data = {data_json};
const svg = d3.select("svg");
const width = window.innerWidth, height = window.innerHeight;
const tooltip = d3.select("body").append("div").attr("class", "tooltip")
  .style("opacity", 0).style("position", "absolute");

function ext(path) {{
  const m = path.match(/\\.(\\w+)$/);
  return m ? m[1].toLowerCase() : "";
}}
function colorFor(path) {{
  const e = ext(path);
  if (e === "py") return "#9cdcfe";
  if (["js","jsx","ts","tsx","mjs","cjs"].includes(e)) return "#dcdcaa";
  return "#888";
}}
function radiusFor(complexity) {{
  return Math.max(4, Math.min(20, Math.sqrt(complexity || 1) * 1.5));
}}

const g = svg.append("g");
svg.call(d3.zoom().scaleExtent([0.1, 5]).on("zoom", e => g.attr("transform", e.transform)));

const nodeIds = new Set(data.nodes.map(n => n.id));
const links = data.edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

const sim = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(links).id(d => d.id).distance(60).strength(0.3))
  .force("charge", d3.forceManyBody().strength(-150))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collide", d3.forceCollide().radius(d => radiusFor(d.complexity) + 4));

const link = g.append("g").selectAll("line").data(links).enter().append("line")
  .attr("class", "link");

const node = g.append("g").selectAll(".node").data(data.nodes).enter().append("g")
  .attr("class", "node")
  .call(d3.drag()
    .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
    .on("drag", (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
    .on("end", (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }}));

node.append("circle")
  .attr("r", d => radiusFor(d.complexity))
  .attr("fill", d => colorFor(d.path))
  .on("mouseover", (e, d) => {{
    tooltip.style("opacity", 1)
      .html(`<b>${{d.path}}</b><br>complexity: ${{d.complexity || 0}}`)
      .style("left", (e.pageX + 12) + "px").style("top", (e.pageY + 12) + "px");
    link.classed("highlight", l => l.source.id === d.id || l.target.id === d.id);
  }})
  .on("mouseout", () => {{ tooltip.style("opacity", 0); link.classed("highlight", false); }});

const labels = node.append("text").attr("dx", 12).attr("dy", 4).text(d => d.name);

sim.on("tick", () => {{
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

function applyFilter() {{
  const q = document.getElementById("filter").value.toLowerCase();
  node.style("opacity", d => !q || d.path.toLowerCase().includes(q) ? 1 : 0.15);
  link.style("opacity", d => {{
    if (!q) return 0.4;
    return (d.source.path.toLowerCase().includes(q) ||
            d.target.path.toLowerCase().includes(q)) ? 0.9 : 0.05;
  }});
}}
function toggleLabels() {{
  labels.style("display", document.getElementById("showLabels").checked ? "" : "none");
}}
</script>
</body></html>"""


def render_graph(repo_map: RepoMap, project_path: str = "") -> str:
    file_nodes = [n for n in repo_map.nodes if n.type == NodeType.FILE]
    cx_by_file: dict[str, int] = {}
    for n in repo_map.nodes:
        if n.type == NodeType.FUNCTION and n.parent_id:
            cx_by_file[n.parent_id] = cx_by_file.get(n.parent_id, 0) + max(1, n.complexity)
    nodes_data = [
        {"id": n.id, "name": n.name, "path": n.path,
         "complexity": cx_by_file.get(n.id, 1)}
        for n in file_nodes
    ]
    valid_ids = {n["id"] for n in nodes_data}
    edges_data = [
        {"source": e.source, "target": e.target}
        for e in repo_map.edges
        if e.type == EdgeType.IMPORT and e.source in valid_ids and e.target in valid_ids
    ]
    return GRAPH_HTML.format(
        project=html.escape(project_path or "(unknown)"),
        n_files=len(nodes_data), n_edges=len(edges_data),
        data_json=json.dumps({"nodes": nodes_data, "edges": edges_data}),
    )


def write_graph_html(repo_map: RepoMap, output_path: Path,
                      project_path: str = "") -> Path:
    output_path.write_text(render_graph(repo_map, project_path), encoding="utf-8")
    return output_path
