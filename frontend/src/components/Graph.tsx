import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import {
  api, type GraphCommunity, type GraphEdge, type GraphNode, type GraphPayload,
} from "../api";

// ─── Restrained palette ────────────────────────────────────────────────────
// Inspired by Aicher's systematic categorical coding and Tufte's
// "use colour to encode information, not to decorate". Each type gets a
// distinct but desaturated tone that reads cleanly on warm paper.
const TYPE_COLORS: Record<string, string> = {
  PERSON:       "#1f1f1f", // ink
  ORGANIZATION: "#8c4a3c", // brick
  PRODUCT:      "#2c4a6b", // ink-navy
  EVENT:        "#8a6a3a", // umber
  LOCATION:     "#4a6b3a", // olive
  CONCEPT:      "#5a4a6b", // dust-purple
};
const ACCENT      = "#c8503c"; // Munich-1972 warm red — used only for state
const TEXT_INK    = "#111111";
const TEXT_MUTED  = "#6b6b6b";
const RULE        = "#d8d4cf"; // hairline rule colour
const PAPER       = "#f7f5ef";
const PAPER_SOFT  = "#efebe2";

const DEFAULT_COLOR = "#9c9c9c";
const TYPE_OPTIONS = ["PERSON", "ORGANIZATION", "PRODUCT", "EVENT", "LOCATION", "CONCEPT"];

// OWL sub-classes → monochrome canvas pictogram drawn inside the disc.
// Aicher-style: each role gets a single recognisable geometric mark.
// Drawn in PAPER colour (white-on-coloured-disc) so they read on every
// type colour. Each fn gets (ctx, cx, cy, s) where s is the half-size
// of the icon — caller pre-clipped to the disc.
type IconDraw = (ctx: CanvasRenderingContext2D, x: number, y: number, s: number) => void;

const ICON_DRAW: Record<string, IconDraw> = {
  // CEO — crown (3 spikes, classical leadership glyph)
  CEO: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.moveTo(x - s, y + s * 0.4);
    ctx.lineTo(x - s, y - s * 0.2);
    ctx.lineTo(x - s * 0.5, y + s * 0.1);
    ctx.lineTo(x, y - s * 0.7);
    ctx.lineTo(x + s * 0.5, y + s * 0.1);
    ctx.lineTo(x + s, y - s * 0.2);
    ctx.lineTo(x + s, y + s * 0.4);
    ctx.closePath();
    ctx.fill();
  },
  // Founder — 5-pointed star
  Founder: (ctx, x, y, s) => {
    ctx.beginPath();
    for (let i = 0; i < 10; i++) {
      const r = i % 2 === 0 ? s : s * 0.45;
      const a = -Math.PI / 2 + (i * Math.PI) / 5;
      const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.fill();
  },
  // Designer — drafting compass (T-shape with serif)
  Designer: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.moveTo(x - s * 0.6, y - s * 0.7);
    ctx.lineTo(x, y + s * 0.7);
    ctx.lineTo(x + s * 0.6, y - s * 0.7);
    ctx.lineTo(x + s * 0.25, y - s * 0.7);
    ctx.lineTo(x, y);
    ctx.lineTo(x - s * 0.25, y - s * 0.7);
    ctx.closePath();
    ctx.fill();
  },
  // Engineer — gear cog (4 teeth simplified)
  Engineer: (ctx, x, y, s) => {
    const teeth = 8;
    const inner = s * 0.55, outer = s * 0.95;
    ctx.beginPath();
    for (let i = 0; i < teeth * 2; i++) {
      const r = i % 2 === 0 ? outer : inner;
      const a = (i * Math.PI) / teeth;
      const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.fill();
  },
  // Executive — two horizontal bars (rank insignia)
  Executive: (ctx, x, y, s) => {
    ctx.fillRect(x - s, y - s * 0.5, s * 2, s * 0.35);
    ctx.fillRect(x - s, y + s * 0.15, s * 2, s * 0.35);
  },
  // Employee — single bar
  Employee: (ctx, x, y, s) => {
    ctx.fillRect(x - s, y - s * 0.2, s * 2, s * 0.4);
  },
  // UnrelatedPerson — small dot
  UnrelatedPerson: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.arc(x, y, s * 0.3, 0, Math.PI * 2);
    ctx.fill();
  },
  // Company — square building (3 windows)
  Company: (ctx, x, y, s) => {
    ctx.fillRect(x - s * 0.9, y - s * 0.7, s * 1.8, s * 1.5);
  },
  // Shareholder — dollar/circle-with-line
  Shareholder: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.arc(x, y, s * 0.85, 0, Math.PI * 2);
    ctx.fill();
  },
  // Supplier — arrow → (incoming supply)
  Supplier: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.moveTo(x - s, y - s * 0.4);
    ctx.lineTo(x + s * 0.3, y - s * 0.4);
    ctx.lineTo(x + s * 0.3, y - s * 0.8);
    ctx.lineTo(x + s, y);
    ctx.lineTo(x + s * 0.3, y + s * 0.8);
    ctx.lineTo(x + s * 0.3, y + s * 0.4);
    ctx.lineTo(x - s, y + s * 0.4);
    ctx.closePath();
    ctx.fill();
  },
  // Smartphone — vertical rounded rect with screen + button
  Smartphone: (ctx, x, y, s) => {
    const w = s * 1.0, h = s * 1.8, rad = s * 0.2;
    const left = x - w / 2, top = y - h / 2;
    ctx.beginPath();
    ctx.moveTo(left + rad, top);
    ctx.lineTo(left + w - rad, top);
    ctx.quadraticCurveTo(left + w, top, left + w, top + rad);
    ctx.lineTo(left + w, top + h - rad);
    ctx.quadraticCurveTo(left + w, top + h, left + w - rad, top + h);
    ctx.lineTo(left + rad, top + h);
    ctx.quadraticCurveTo(left, top + h, left, top + h - rad);
    ctx.lineTo(left, top + rad);
    ctx.quadraticCurveTo(left, top, left + rad, top);
    ctx.closePath();
    ctx.fill();
  },
  // Tablet — wider rounded rect
  Tablet: (ctx, x, y, s) => {
    ctx.fillRect(x - s, y - s * 0.7, s * 2, s * 1.4);
  },
  // Wearable — small ring
  Wearable: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.arc(x, y, s * 0.85, 0, Math.PI * 2);
    ctx.arc(x, y, s * 0.45, 0, Math.PI * 2, true);
    ctx.fill();
  },
  // Computer — monitor (rect + stand)
  Computer: (ctx, x, y, s) => {
    ctx.fillRect(x - s, y - s * 0.7, s * 2, s * 1.2);
    ctx.fillRect(x - s * 0.4, y + s * 0.5, s * 0.8, s * 0.3);
  },
  Desktop: (ctx, x, y, s) => ICON_DRAW.Computer(ctx, x, y, s),
  // Notebook — laptop (rect + base wedge)
  Notebook: (ctx, x, y, s) => {
    ctx.fillRect(x - s * 0.85, y - s * 0.7, s * 1.7, s * 0.95);
    ctx.beginPath();
    ctx.moveTo(x - s, y + s * 0.4);
    ctx.lineTo(x + s, y + s * 0.4);
    ctx.lineTo(x + s * 0.85, y + s * 0.7);
    ctx.lineTo(x - s * 0.85, y + s * 0.7);
    ctx.closePath();
    ctx.fill();
  },
  // OperatingSystem — 2×2 grid of dots
  OperatingSystem: (ctx, x, y, s) => {
    const d = s * 0.4, off = s * 0.45;
    for (const dx of [-off, off]) for (const dy of [-off, off]) {
      ctx.beginPath();
      ctx.arc(x + dx, y + dy, d, 0, Math.PI * 2);
      ctx.fill();
    }
  },
  // OnlineService — cloud (3 bumps)
  OnlineService: (ctx, x, y, s) => {
    ctx.beginPath();
    ctx.arc(x - s * 0.5, y + s * 0.1, s * 0.5, Math.PI, 0);
    ctx.arc(x,           y - s * 0.2, s * 0.55, Math.PI, 0);
    ctx.arc(x + s * 0.5, y + s * 0.1, s * 0.5, Math.PI, 0);
    ctx.lineTo(x + s, y + s * 0.5);
    ctx.lineTo(x - s, y + s * 0.5);
    ctx.closePath();
    ctx.fill();
  },
  // ProductFamily — three stacked rectangles (family tree)
  ProductFamily: (ctx, x, y, s) => {
    ctx.fillRect(x - s, y - s * 0.7, s * 2, s * 0.4);
    ctx.fillRect(x - s, y - s * 0.2, s * 2, s * 0.4);
    ctx.fillRect(x - s, y + s * 0.3, s * 2, s * 0.4);
  },
  // Era — clock face (circle + hand)
  Era: (ctx, x, y, s) => {
    ctx.lineWidth = s * 0.18;
    ctx.strokeStyle = ctx.fillStyle as string;
    ctx.beginPath();
    ctx.arc(x, y, s * 0.85, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x, y - s * 0.55);
    ctx.moveTo(x, y);
    ctx.lineTo(x + s * 0.4, y);
    ctx.stroke();
  },
};
// Higher = more interesting to show as the badge letter.
const ROLE_PRIORITY: Record<string, number> = {
  CEO: 100, Founder: 95, Designer: 90, Engineer: 85, Executive: 80,
  Smartphone: 78, Tablet: 75, Computer: 70, OperatingSystem: 68,
  Company: 60, Shareholder: 55, Supplier: 50,
  Wearable: 48, Notebook: 45, Desktop: 42, OnlineService: 40,
  ProductFamily: 35, Era: 30, Employee: 20, UnrelatedPerson: 5,
};

function primaryRole(roles: string[]): string | null {
  if (!roles || roles.length === 0) return null;
  return [...roles].sort((a, b) =>
    (ROLE_PRIORITY[b] ?? 0) - (ROLE_PRIORITY[a] ?? 0)
  )[0];
}

interface FGNode extends GraphNode {
  x?: number; y?: number;
  fx?: number | null; fy?: number | null;
  __radius?: number;
}

interface FGLink extends GraphEdge {
  source: string | FGNode;
  target: string | FGNode;
}

type ColourBy = "type" | "community";
type Layout   = "cluster" | "concentric";

// Ring order for concentric layout — closest to Apple = most directly
// related types (persons running the company, then orgs, then products
// they make, then events shaping the firm, then peripheral location/
// concept references on the outer rings).
const RING_ORDER = ["PERSON", "ORGANIZATION", "PRODUCT", "EVENT", "LOCATION", "CONCEPT"];

// ─── Geometry helpers (no extra deps) ──────────────────────────────────────

/** Graham-scan convex hull, returns the boundary points in order.
 *  Input: array of [x, y]; output: subset of those points forming the hull.
 *  Returns at most the input (fewer when many points are collinear). */
function convexHull(points: [number, number][]): [number, number][] {
  if (points.length < 3) return points.slice();
  const sorted = [...points].sort((a, b) => a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]);
  const cross = (o: [number, number], a: [number, number], b: [number, number]) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: [number, number][] = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper: [number, number][] = [];
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  lower.pop(); upper.pop();
  return lower.concat(upper);
}

/** BFS shortest path on the link list (unweighted, undirected).
 *  Returns ids in order from→to, or [] if unreachable / same. */
function shortestPath(nodes: FGNode[], links: FGLink[], from: string, to: string): string[] {
  if (from === to) return [from];
  const idOf = (x: string | FGNode) => typeof x === "string" ? x : x.id;
  const adj = new Map<string, Set<string>>();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const l of links) {
    const a = idOf(l.source), b = idOf(l.target);
    adj.get(a)?.add(b); adj.get(b)?.add(a);
  }
  const prev = new Map<string, string>();
  const seen = new Set<string>([from]);
  const queue = [from];
  while (queue.length) {
    const cur = queue.shift()!;
    if (cur === to) {
      const path = [to];
      let p = to;
      while (prev.has(p)) { p = prev.get(p)!; path.push(p); }
      return path.reverse();
    }
    for (const nb of adj.get(cur) || []) {
      if (seen.has(nb)) continue;
      seen.add(nb); prev.set(nb, cur); queue.push(nb);
    }
  }
  return [];
}

/** Aggressive label truncation for dense clusters. The UE3 extractor
 *  produces verbose entity names like "GJ 2018 (Okt. 17 – Sep. 18)" or
 *  "iPhone 4 (das vierte Modell)" — useful as full names but unreadable
 *  when 12 of them stack inside the EVENT hull. Strategy: cut at first
 *  parenthesis or comma, then hard-cap at 18 chars. */
function shortLabel(name: string): string {
  const cut = name.split(/\s*[(,]/)[0].trim();
  if (cut.length <= 18) return cut;
  return cut.slice(0, 16).trimEnd() + "…";
}

/** Find the entity considered the "centre of the graph" — Apple Inc.
 *  Falls back to the most-mentioned ORGANIZATION if no exact match. */
function findCentreNode(nodes: FGNode[]): FGNode | null {
  const exact = nodes.find(n => /^Apple($| Inc)/i.test(n.name));
  if (exact) return exact;
  const orgs = nodes.filter(n => n.type === "ORGANIZATION");
  if (!orgs.length) return null;
  return orgs.reduce((best, n) => n.mentions > best.mentions ? n : best, orgs[0]);
}

/** Humanise an UPPER_SNAKE relation type for display.
 *  "DESIGNED_BY" → "designed by", "associatedWith" → "associated with". */
function prettifyRelation(t: string): string {
  if (!t) return "verbunden mit";
  // Split camelCase into words first, then handle UPPER_SNAKE.
  const spaced = t
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .toLowerCase()
    .trim();
  return spaced || "verbunden mit";
}

/** Group all neighbours of `node` by the relation type on the edge,
 *  preserving direction (selected→other = "out", other→selected = "in").
 *  Each group is sorted by weight desc, then by neighbour mentions. */
interface ConnectionGroup {
  relType: string;
  direction: "out" | "in";
  entries: { node: GraphNode; weight: number }[];
}
function buildConnections(
  node: GraphNode | null,
  allNodes: GraphNode[],
  edges: GraphEdge[],
): ConnectionGroup[] {
  if (!node) return [];
  const byId = new Map(allNodes.map(n => [n.id, n]));
  const groups = new Map<string, ConnectionGroup>();
  for (const e of edges) {
    let other: GraphNode | undefined;
    let dir: "out" | "in" | null = null;
    if (e.source === node.id) { other = byId.get(e.target); dir = "out"; }
    else if (e.target === node.id) { other = byId.get(e.source); dir = "in"; }
    if (!other || !dir) continue;
    const key = `${e.type}::${dir}`;
    if (!groups.has(key)) groups.set(key, { relType: e.type, direction: dir, entries: [] });
    groups.get(key)!.entries.push({ node: other, weight: e.weight });
  }
  for (const g of groups.values()) {
    g.entries.sort((a, b) =>
      b.weight - a.weight || b.node.mentions - a.node.mentions
    );
  }
  // Order groups: outgoing first (you "act on"), then incoming, then by
  // number of entries desc so the most-populated relation is on top.
  return Array.from(groups.values()).sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === "out" ? -1 : 1;
    return b.entries.length - a.entries.length;
  });
}

export function Graph() {
  const [data, setData] = useState<GraphPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<FGNode | null>(null);
  const [hovered, setHovered] = useState<FGNode | null>(null);
  // Default 2 to suppress the long tail of single-mention LOCATIONs and
  // CONCEPTs the UE3 extractor picks up from Wikipedia (e.g. all 16 Apple
  // Store addresses including "Breuningerland Sindelfingen") — they're
  // technically in the article but produce a noisy scatter of isolated
  // 1-node communities.
  const [minMentions, setMinMentions] = useState(2);
  // Default 1: the actual extraction data has 93 % of relations at
  // weight=1 (real entity relationships extracted by the LLM), so
  // setting a higher floor strips meaningful structure including
  // canonical CEO/Founder edges. Users can crank it up via the slider
  // if they want only repeatedly co-mentioned pairs.
  const [minEdgeWeight, setMinEdgeWeight] = useState(1);
  const [search, setSearch] = useState("");
  const [typesEnabled, setTypesEnabled] = useState<Set<string>>(new Set(TYPE_OPTIONS));
  const [colourBy, setColourBy] = useState<ColourBy>("type");
  const [layout, setLayout] = useState<Layout>("cluster");
  const [showHulls, setShowHulls] = useState(true);
  const [pathMode, setPathMode] = useState(false);
  const [pathFrom, setPathFrom] = useState<FGNode | null>(null);
  const [pathIds, setPathIds] = useState<string[]>([]);
  const [highlightedIds, setHighlightedIds] = useState<Set<string>>(new Set());
  const [hoveredLink, setHoveredLink] = useState<FGLink | null>(null);
  const [sparqlOpen, setSparqlOpen] = useState(true);
  // Navigation history (breadcrumb) — last few selected entities, so the
  // user can wander Wikipedia-style through the graph and step back.
  const [history, setHistory] = useState<FGNode[]>([]);
  // Focus mode: hide everything except the 1-hop ego network of `selected`.
  const [focusMode, setFocusMode] = useState(false);
  // Show edges? In Konzentrik they cross all rings and create visual
  // noise without adding much — default OFF for Konzentrik. In Cluster
  // they show the inter-cluster structure — default ON.
  const [showEdges, setShowEdges] = useState(true);
  // User-adjustable size scales — exposed via the settings gear so the
  // user can dial node/edge weight up or down for their screen.
  const [nodeSizeScale, setNodeSizeScale] = useState(1.0);
  const [edgeStrokeScale, setEdgeStrokeScale] = useState(1.0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Show relation labels on hover/path edges? Default off — too noisy
  // for browsing. Users who want them turn on via the gear.
  const [showEdgeLabels, setShowEdgeLabels] = useState(false);
  // Background theme. "paper" = warm off-white (Aicher/Tufte default),
  // "white" = pure white for high-contrast presentations / print.
  const [theme, setTheme] = useState<"paper" | "white">("paper");
  // Collapse the LeftRail entirely for presentation mode → canvas
  // gets the freed space.
  const [leftRailOpen, setLeftRailOpen] = useState(true);
  // Time-filter slider: when active, hides EVENT entities whose name
  // does not contain the chosen year. Apple + non-EVENT stay visible.
  // null = inactive (show everything).
  const [yearFilter, setYearFilter] = useState<number | null>(null);
  const graphRef = useRef<ForceGraphMethods<FGNode, FGLink> | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  const load = async () => {
    setLoading(true); setError(null);
    try {
      const types = TYPE_OPTIONS.every(t => typesEnabled.has(t))
        ? undefined : Array.from(typesEnabled).join(",");
      const r = await api.graph({ min_mentions: minMentions, types, limit_entities: 500, min_edge_weight: minEdgeWeight });
      setData(r);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // Reload graph data when the edge-weight filter changes (cheap,
  // backend just adds a WHERE clause).
  useEffect(() => {
    if (data) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minEdgeWeight]);

  // Konzentrik: edges cross all rings and are visual noise → default OFF.
  // Cluster: edges show inter-cluster structure → default ON.
  useEffect(() => {
    setShowEdges(layout === "cluster");
  }, [layout]);

  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(([entry]) => {
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  const graphData = useMemo(() => {
    if (!data) return { nodes: [] as FGNode[], links: [] as FGLink[] };

    // ─── Ego-Network layout (focusMode + selected) ──────────────────────
    // Selected at (0,0), 1-hop neighbours on an inner ring. When 1-hop
    // is sparse (< 5 — common on this graph because UE3 emits ~20
    // edges total for 40 nodes), automatically expand to a 2-hop ring
    // so the user sees real context instead of just "Macintosh — Apple"
    // and a giant empty canvas.
    if (focusMode && selected) {
      const hop1 = new Set<string>([selected.id]);
      for (const e of data.edges) {
        if (e.source === selected.id) hop1.add(e.target);
        if (e.target === selected.id) hop1.add(e.source);
      }
      const expand2Hop = hop1.size < 5;
      const hop2 = new Set<string>(hop1);
      if (expand2Hop) {
        for (const e of data.edges) {
          if (hop1.has(e.source)) hop2.add(e.target);
          if (hop1.has(e.target)) hop2.add(e.source);
        }
      }
      const egoIds = hop2;
      const nodes = data.nodes
        .filter(n => egoIds.has(n.id))
        .map(n => ({ ...n })) as FGNode[];
      const links: FGLink[] = data.edges
        .filter(e => egoIds.has(e.source) && egoIds.has(e.target))
        .map(e => ({ ...e }));
      const centre = nodes.find(n => n.id === selected.id);
      if (centre) { centre.fx = 0; centre.fy = 0; }

      // Inner ring = 1-hop neighbours, grouped by relation type into
      // angular sectors. Outer ring (only if 2-hop expansion was needed)
      // = 2-hop neighbours, sorted by mentions on the ring.
      const innerNeighbours = nodes.filter(n => n.id !== selected.id && hop1.has(n.id));
      const outerNeighbours = expand2Hop
        ? nodes.filter(n => !hop1.has(n.id))
        : [];

      // Inner ring: bucket by relation type the edge to selected uses.
      const relOf = new Map<string, string>();
      for (const e of links) {
        if (e.source === selected.id) relOf.set(e.target as string, e.type);
        if (e.target === selected.id) relOf.set(e.source as string, e.type);
      }
      const innerBuckets = new Map<string, FGNode[]>();
      for (const n of innerNeighbours) {
        const rel = relOf.get(n.id) || "_other";
        if (!innerBuckets.has(rel)) innerBuckets.set(rel, []);
        innerBuckets.get(rel)!.push(n);
      }
      const Rinner = 150;
      const innerTotal = Math.max(innerNeighbours.length, 1);
      const sectors = Array.from(innerBuckets.entries())
        .sort((a, b) => b[1].length - a[1].length);
      let theta = -Math.PI / 2;
      for (const [, members] of sectors) {
        const angle = (members.length / innerTotal) * Math.PI * 2;
        members.sort((a, b) => b.mentions - a.mentions);
        for (let i = 0; i < members.length; i++) {
          const t2 = theta + ((i + 0.5) / members.length) * angle;
          members[i].fx = Rinner * Math.cos(t2);
          members[i].fy = Rinner * Math.sin(t2);
        }
        theta += angle;
      }

      // Outer ring: 2-hop neighbours, evenly distributed by mention rank.
      if (outerNeighbours.length > 0) {
        const Router = 260;
        outerNeighbours.sort((a, b) => b.mentions - a.mentions);
        for (let i = 0; i < outerNeighbours.length; i++) {
          const t2 = -Math.PI / 2 + (i / outerNeighbours.length) * Math.PI * 2;
          outerNeighbours[i].fx = Router * Math.cos(t2);
          outerNeighbours[i].fy = Router * Math.sin(t2);
        }
      }
      return { nodes, links };
    }

    const q = search.trim().toLowerCase();
    let keep = new Set(data.nodes.map(n => n.id));

    // Year filter: when active, drop EVENT entities whose name doesn't
    // contain the year. Non-EVENT nodes stay (they have no year metadata
    // and we don't want to lose Apple, products, persons just because
    // the user picked 2010). This is the lightweight version — once
    // the backend exposes release_year / birth_year on entities the
    // filter can be tightened.
    if (yearFilter != null) {
      const yearStr = String(yearFilter);
      keep = new Set(
        data.nodes.filter(n =>
          n.type !== "EVENT" || n.name.includes(yearStr)
        ).map(n => n.id)
      );
    }

    if (q) {
      const matches = new Set(
        data.nodes.filter(n =>
          n.name.toLowerCase().includes(q) ||
          n.description.toLowerCase().includes(q)
        ).map(n => n.id)
      );
      // 1-hop neighbours from direct edges
      const neighbours = new Set<string>();
      for (const e of data.edges) {
        if (matches.has(e.source)) neighbours.add(e.target);
        if (matches.has(e.target)) neighbours.add(e.source);
      }
      // When the immediate neighbourhood is sparse (≤ 3 nodes incl.
      // matches), expand to 2-hop so the user sees actual context
      // instead of an empty page. Without this, searching "Gil Amelio"
      // would only show Gil + Apple — useless for "show me his world".
      const oneHop = new Set([...matches, ...neighbours]);
      let keepSet = oneHop;
      if (oneHop.size <= 3) {
        const twoHop = new Set(oneHop);
        for (const e of data.edges) {
          if (oneHop.has(e.source)) twoHop.add(e.target);
          if (oneHop.has(e.target)) twoHop.add(e.source);
        }
        keepSet = twoHop;
      }
      keep = keepSet;
    }
    const nodes = data.nodes.filter(n => keep.has(n.id)).map(n => ({ ...n })) as FGNode[];
    const ids = new Set(nodes.map(n => n.id));
    const links: FGLink[] = data.edges
      .filter(e => ids.has(e.source) && ids.has(e.target))
      .map(e => ({ ...e }));

    // ─── Cluster + Typ: deterministic pack layout ─────────────────────
    //
    // The force-based cluster mode produced random-looking blobs because
    // the simulation found a different local minimum on every layout
    // and the convex hulls just traced the chaos. For type-clustering
    // we instead PIN every node: Apple at centre, one fixed cluster
    // centre per type on a 6-spoke wheel around Apple, members arranged
    // inside each cluster in a phyllotaxis spiral (sunflower-seed
    // pattern) for tight, organic packing. Hulls become perfect circles
    // — uniform, calm, signage-quality, Aicher would approve.
    //
    // (Cluster + Community keeps the messy force layout — that's the
    // didactic counterexample to deterministic semantic grouping.)
    if (layout === "cluster" && colourBy === "type") {
      const apple = findCentreNode(nodes);
      const typesPresent = RING_ORDER.filter(t =>
        nodes.some(n => n.type === t && n !== apple)
      );
      const memberRadius = 16;          // intra-cluster spacing constant
      const padding = 32;                // extra space around each circle
      // Cluster radius scales with √members so clusters look balanced
      // even when member counts vary a lot (3 PERSONs vs 14 EVENTs).
      const clusterRadii = new Map<string, number>();
      for (const t of typesPresent) {
        const n = nodes.filter(m => m.type === t && m !== apple).length;
        clusterRadii.set(t, memberRadius * Math.sqrt(Math.max(n, 1)) + 24);
      }
      // Place cluster centres on a circle around Apple. The orbit radius
      // is chosen so the biggest cluster's edge clears Apple by `padding`.
      const maxR = Math.max(...clusterRadii.values(), 60);
      const orbitR = maxR + padding + 60;
      for (let ti = 0; ti < typesPresent.length; ti++) {
        const t = typesPresent[ti];
        const angle = -Math.PI / 2 + (ti / typesPresent.length) * Math.PI * 2;
        const cx = orbitR * Math.cos(angle);
        const cy = orbitR * Math.sin(angle);
        const members = nodes.filter(m => m.type === t && m !== apple)
                              .sort((a, b) => b.mentions - a.mentions);
        // Phyllotaxis (golden angle): tight, no overlap, no obvious
        // grid. Top-mention member at centre because Math.sqrt(0)=0.
        const golden = Math.PI * (3 - Math.sqrt(5)); // ≈ 2.39996
        for (let i = 0; i < members.length; i++) {
          const r = memberRadius * Math.sqrt(i);
          const theta = i * golden;
          members[i].fx = cx + r * Math.cos(theta);
          members[i].fy = cy + r * Math.sin(theta);
        }
      }
      if (apple) { apple.fx = 0; apple.fy = 0; }
    }
    // ─── Concentric layout: pin nodes to type-stratified rings ──────────
    //
    // Aicher-grade radial composition: Apple at the centre, then rings by
    // type. Within each ring, sort by mentions (most-mentioned at 12
    // o'clock, rotating clockwise) so the eye lands on important nodes
    // immediately. fx/fy are honoured by d3-force as fixed positions.
    else if (layout === "concentric") {
      // Konzentrik with subclass sectors: within each ring, peers are
      // grouped by their OWL primary role (CEO, Designer, Smartphone …)
      // and each role-group gets a contiguous angular sector. Sectors
      // are sized proportionally to member count and ordered by role
      // priority. Result: a Designer-Sektor, CEO-Sektor, Founder-
      // Sektor etc. inside the PERSON ring; Smartphones / Computers /
      // OperatingSystems as sectors inside the PRODUCT ring.
      const centre = findCentreNode(nodes);
      const types = RING_ORDER.filter(t => nodes.some(n => n.type === t && n !== centre));
      const inner = 90, step = 60;
      if (centre) { centre.fx = 0; centre.fy = 0; }
      // First clear any prior pinning for the centre + nodes-without-a-ring.
      for (const n of nodes) {
        if (n === centre) continue;
        if (!types.includes(n.type)) { n.fx = null; n.fy = null; }
      }
      for (let ti = 0; ti < types.length; ti++) {
        const t = types[ti];
        const r = inner + ti * step;
        const peers = nodes.filter(m => m.type === t && m !== centre);
        if (peers.length === 0) continue;
        // Bucket by primary role; null role → "_other"
        const buckets = new Map<string, FGNode[]>();
        for (const p of peers) {
          const key = primaryRole(p.roles || []) ?? "_other";
          if (!buckets.has(key)) buckets.set(key, []);
          buckets.get(key)!.push(p);
        }
        // Sort each bucket: most-mentioned first (gets the centre angle)
        for (const arr of buckets.values()) arr.sort((a, b) => b.mentions - a.mentions);
        // Sort buckets: known roles by priority desc, "_other" last
        const sectors = Array.from(buckets.entries()).sort((a, b) => {
          if (a[0] === "_other") return 1;
          if (b[0] === "_other") return -1;
          return (ROLE_PRIORITY[b[0]] ?? 0) - (ROLE_PRIORITY[a[0]] ?? 0);
        });
        let theta = -Math.PI / 2;       // start at 12 o'clock, rotate clockwise
        for (const [, members] of sectors) {
          const angle = (members.length / peers.length) * Math.PI * 2;
          for (let i = 0; i < members.length; i++) {
            // Place at the centre of each member's slice within the sector
            const t2 = theta + ((i + 0.5) / members.length) * angle;
            members[i].fx = r * Math.cos(t2);
            members[i].fy = r * Math.sin(t2);
          }
          theta += angle;
        }
      }
    } else {
      // Cluster + Community mode — release pinning so the force simulation
      // runs. The messy result is the didactic point.
      for (const n of nodes) { n.fx = null; n.fy = null; }
    }

    return { nodes, links };
  }, [data, search, layout, colourBy, size.w, size.h, focusMode, selected, yearFilter]);

  // ─── Cluster force: in cluster mode, pull each node toward its
  // community's centroid. Without this, d3-force produces the classic
  // "hairball"; with it, communities visibly separate into islands.
  // We compute centroids on every tick from current node positions.
  useEffect(() => {
    if (!graphRef.current) return;
    // When positions are pinned (concentric, OR cluster+type pack,
    // OR ego-network when focusing on a selection), disable all
    // drift-producing forces.
    const allPinned = layout === "concentric" ||
                      (layout === "cluster" && colourBy === "type") ||
                      (focusMode && selected != null);
    const charge = graphRef.current.d3Force("charge") as any;
    if (charge && typeof charge.strength === "function") {
      charge.strength(allPinned ? 0 : -120);
    }
    const link = graphRef.current.d3Force("link") as any;
    if (link && typeof link.distance === "function") {
      link.distance(allPinned ? 1 : 40);
    }
    // Add a centering force in cluster mode: pulls every node gently toward
    // (0,0). Without this, isolated 1-2-node components drift off into the
    // empty canvas corners (Breuningerland Sindelfingen wandering off into
    // the top-right wasteland was the original symptom). The strength is
    // small so well-connected clusters can still form local centroids.
    if (layout === "cluster" && !allPinned) {
      const centerForce = (alpha: number) => {
        const strength = 0.04 * alpha;
        for (const n of graphData.nodes) {
          if (n.x == null || n.y == null) continue;
          (n as any).vx = ((n as any).vx || 0) - n.x * strength;
          (n as any).vy = ((n as any).vy || 0) - n.y * strength;
        }
      };
      graphRef.current.d3Force("centerPull", centerForce as any);

      // Collision force — prevents nodes from piling onto the same spot.
      // Crucial in type-clustered mode where all 12 EVENT fiscal-year nodes
      // get pulled to the same centroid and would otherwise stack into one
      // unreadable blob. Simple O(n²) sweep is fine at 40-250 nodes.
      const collisionForce = (alpha: number) => {
        const nodes = graphData.nodes;
        const pad = 6;
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          if (a.x == null || a.y == null) continue;
          const ar = (a.__radius || 8);
          for (let j = i + 1; j < nodes.length; j++) {
            const b = nodes[j];
            if (b.x == null || b.y == null) continue;
            const br = (b.__radius || 8);
            const dx = b.x - a.x, dy = b.y - a.y;
            const dist = Math.hypot(dx, dy);
            const minDist = ar + br + pad;
            if (dist < minDist && dist > 0.01) {
              const push = (minDist - dist) / dist * 0.5 * alpha;
              const px = dx * push, py = dy * push;
              (a as any).vx = ((a as any).vx || 0) - px;
              (a as any).vy = ((a as any).vy || 0) - py;
              (b as any).vx = ((b as any).vx || 0) + px;
              (b as any).vy = ((b as any).vy || 0) + py;
            }
          }
        }
      };
      graphRef.current.d3Force("collision", collisionForce as any);
    } else {
      graphRef.current.d3Force("centerPull", null as any);
      graphRef.current.d3Force("collision", null as any);
    }
    if (allPinned) {
      graphRef.current.d3Force("cluster", null as any);
    } else {
      // The user's colourBy choice ALSO drives the clustering logic.
      // colourBy=type  → semantically meaningful clusters (all PERSONs
      //                  together, all PRODUCTs together, etc.). This
      //                  gives 6 readable islands.
      // colourBy=community → Louvain communities. Mathematically valid
      //                  but unintuitive on a sparse graph (e.g. iPhone 4
      //                  and iPhone 6 each form their own community
      //                  because they're each connected only to Apple).
      const groupKey = (n: FGNode) =>
        colourBy === "community" ? n.community_id : n.type;
      const clusterForce = (alpha: number) => {
        if (!data) return;
        const buckets = new Map<string, { x: number; y: number; n: number }>();
        for (const n of graphData.nodes) {
          const key = groupKey(n);
          if (!key || n.x == null || n.y == null) continue;
          const b = buckets.get(key) || { x: 0, y: 0, n: 0 };
          b.x += n.x; b.y += n.y; b.n += 1;
          buckets.set(key, b);
        }
        for (const [, b] of buckets) { b.x /= b.n; b.y /= b.n; }
        const k = 0.9 * alpha;
        for (const n of graphData.nodes) {
          const key = groupKey(n);
          if (!key || n.x == null || n.y == null) continue;
          const c = buckets.get(key);
          if (!c) continue;
          (n as any).vx = ((n as any).vx || 0) + (c.x - n.x) * k;
          (n as any).vy = ((n as any).vy || 0) + (c.y - n.y) * k;
        }
      };
      graphRef.current.d3Force("cluster", clusterForce as any);
    }
    // Re-heat the simulation only when there's actually a force to settle.
    // In ego mode every node is pinned, so a reheat just wastes ticks and
    // produces the jittery "verschiebt sich" feeling the user reported.
    if (!(focusMode && selected)) {
      graphRef.current.d3ReheatSimulation();
    }
  }, [layout, colourBy, graphData.nodes, data, focusMode, selected]);

  // Cluster bubbles (centre + radius per type) for the pack-layout mode.
  // Empty when we're in concentric or community-cluster mode.
  const clusterBubbles = useMemo(() => {
    if (!(layout === "cluster" && colourBy === "type")) return [] as Array<{
      type: string; cx: number; cy: number; r: number;
    }>;
    const apple = findCentreNode(graphData.nodes);
    const typesPresent = RING_ORDER.filter(t =>
      graphData.nodes.some(n => n.type === t && n !== apple)
    );
    const out: { type: string; cx: number; cy: number; r: number }[] = [];
    for (const t of typesPresent) {
      const members = graphData.nodes.filter(n => n.type === t && n !== apple);
      if (members.length === 0) continue;
      const xs = members.map(n => n.fx ?? n.x ?? 0);
      const ys = members.map(n => n.fy ?? n.y ?? 0);
      const cx = xs.reduce((s, v) => s + v, 0) / xs.length;
      const cy = ys.reduce((s, v) => s + v, 0) / ys.length;
      // Radius = farthest member distance from centre + breathing room
      const r = members.reduce((m, n) => {
        const dx = (n.fx ?? n.x ?? 0) - cx;
        const dy = (n.fy ?? n.y ?? 0) - cy;
        return Math.max(m, Math.hypot(dx, dy));
      }, 0) + 20;
      out.push({ type: t, cx, cy, r });
    }
    return out;
  }, [layout, colourBy, graphData.nodes]);

  // Communities → restrained categorical palette (still desaturated)
  const communityColour = useMemo(() => {
    const palette = [
      "#5a4a6b", "#8c4a3c", "#2c4a6b", "#4a6b3a", "#8a6a3a", "#6b4a5a",
      "#4a5a6b", "#6b5a4a", "#3a6b5a", "#6b3a5a", "#5a6b3a", "#3a4a6b",
      "#7c4f3c", "#3c5a7c", "#6b5a3a", "#5a3a4a", "#3a6b6b", "#4a3a6b",
    ];
    const m = new Map<string, string>();
    let i = 0;
    for (const c of data?.communities || []) {
      m.set(c.id, palette[i % palette.length]); i++;
    }
    return m;
  }, [data]);

  const colourOf = (n: FGNode): string => {
    if (colourBy === "community")
      return n.community_id ? (communityColour.get(n.community_id) || DEFAULT_COLOR) : DEFAULT_COLOR;
    return TYPE_COLORS[n.type] || DEFAULT_COLOR;
  };

  // 1-hop neighbour set of a given node (used for hover-dim and selection-
  // spotlight). Returns null if no node is given.
  const computeNeighbours = (n: GraphNode | null): Set<string> | null => {
    if (!n || !data) return null;
    const ids = new Set<string>([n.id]);
    for (const e of data.edges) {
      if (e.source === n.id) ids.add(e.target);
      if (e.target === n.id) ids.add(e.source);
    }
    return ids;
  };
  const neighbourIds   = useMemo(() => computeNeighbours(hovered),  [hovered, data]);
  const selectedEgoIds = useMemo(() => computeNeighbours(selected), [selected, data]);

  // Connections grouped by relation type (incoming/outgoing) for the
  // selected entity — feeds the Connection Panel.
  const connections = useMemo<ConnectionGroup[]>(() => {
    if (!selected || !data) return [];
    return buildConnections(selected, data.nodes, data.edges);
  }, [selected, data]);

  const isDimmed = (id: string) => {
    if (highlightedIds.size > 0) return !highlightedIds.has(id);
    // In ego mode the graph is ALREADY filtered to the focused subset
    // (selected + 1-hop, possibly + 2-hop). Every visible node is by
    // definition "in focus" — don't grey out the outer ring just
    // because it isn't 1-hop. Only hover-dim still applies inside ego.
    if (focusMode && selected) {
      return neighbourIds != null ? !neighbourIds.has(id) : false;
    }
    if (neighbourIds != null) return !neighbourIds.has(id);
    if (selectedEgoIds != null) return !selectedEgoIds.has(id);
    return false;
  };

  // Wander to another entity (clicking in the Connection Panel calls this).
  // Pushes the previous selection onto history so the user can go back.
  const navigateTo = (id: string) => {
    if (!data) return;
    const target = data.nodes.find(n => n.id === id) as FGNode | undefined;
    if (!target) return;
    if (selected && selected.id !== target.id) {
      setHistory(h => [...h.slice(-9), selected]);
    }
    setSelected(target);
    // Try to fly to the existing live node (in graphData with x/y) — if
    // present. data.nodes lacks live coordinates.
    const live = graphData.nodes.find(n => n.id === id);
    if (live && live.x != null && live.y != null && graphRef.current) {
      graphRef.current.centerAt(live.x, live.y, 700);
      graphRef.current.zoom(Math.max(graphRef.current.zoom(), 2.0), 700);
    }
  };
  const goBack = () => {
    setHistory(h => {
      if (h.length === 0) return h;
      const prev = h[h.length - 1];
      setSelected(prev);
      const live = graphData.nodes.find(n => n.id === prev.id);
      if (live && live.x != null && live.y != null && graphRef.current) {
        graphRef.current.centerAt(live.x, live.y, 600);
      }
      return h.slice(0, -1);
    });
  };

  const toggleType = (t: string) => {
    setTypesEnabled(cur => {
      const next = new Set(cur);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });
  };

  const handleSparqlExecuted = (matched: string[]) => {
    setHighlightedIds(new Set(matched));
    if (matched.length > 0 && graphRef.current && data) {
      const n = graphData.nodes.find(nn => nn.id === matched[0]);
      if (n && n.x != null && n.y != null) {
        graphRef.current.centerAt(n.x, n.y, 800);
        graphRef.current.zoom(2.0, 800);
      }
    }
  };

  // Focus-mode camera: centre on (0,0) where the selected entity is
  // pinned, then zoomToFit the ego ring. Two-step so the camera moves
  // deliberately rather than the zoomToFit defaulting to old positions.
  //
  // Zoom-cap: when the selected node has only 1 visible neighbour,
  // zoomToFit blows up the pair to fill the canvas (Apple becomes a
  // hand-sized red square in the screenshot). Clamp the zoom level
  // after the fit so tiny ego networks render at a comfortable scale.
  useEffect(() => {
    if (!focusMode || !selected || !graphRef.current) return;
    const t1 = setTimeout(() => {
      graphRef.current?.centerAt(0, 0, 400);
    }, 40);
    const t2 = setTimeout(() => {
      graphRef.current?.zoomToFit(500, 80);
    }, 250);
    const t3 = setTimeout(() => {
      if (!graphRef.current) return;
      const z = graphRef.current.zoom();
      if (typeof z === "number" && z > 3) {
        graphRef.current.zoom(3, 400);
      }
    }, 800);
    return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
  }, [focusMode, selected]);

  // Fly camera to the top fuzzy-match when the search field changes.
  useEffect(() => {
    if (!search.trim() || !graphRef.current) return;
    const q = search.trim().toLowerCase();
    const match = graphData.nodes
      .filter(n => n.name.toLowerCase().includes(q))
      .sort((a, b) => {
        // Prefer label-startsWith, then by mentions
        const sa = a.name.toLowerCase().startsWith(q) ? 0 : 1;
        const sb = b.name.toLowerCase().startsWith(q) ? 0 : 1;
        return sa - sb || b.mentions - a.mentions;
      })[0];
    if (match && match.x != null && match.y != null) {
      graphRef.current.centerAt(match.x, match.y, 700);
      graphRef.current.zoom(2.4, 700);
    }
  }, [search, graphData.nodes]);

  const handleNodeClick = (n: FGNode) => {
    if (pathMode) {
      if (!pathFrom) {
        setPathFrom(n); setPathIds([n.id]); setSelected(n);
        return;
      }
      const path = shortestPath(graphData.nodes, graphData.links, pathFrom.id, n.id);
      if (path.length > 0) {
        setPathIds(path);
        // Frame the path: fit the bbox of pathway nodes
        const pts = path
          .map(id => graphData.nodes.find(nn => nn.id === id))
          .filter((nn): nn is FGNode => !!nn && nn.x != null && nn.y != null);
        if (pts.length > 0 && graphRef.current) {
          const xs = pts.map(p => p.x!), ys = pts.map(p => p.y!);
          const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
          const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
          graphRef.current.centerAt(cx, cy, 800);
        }
      } else {
        setPathIds([pathFrom.id, n.id]); // unreachable visualised as endpoints only
      }
      setPathFrom(null);
      setSelected(n);
      return;
    }
    if (selected && selected.id !== n.id) setHistory(h => [...h.slice(-9), selected]);
    setSelected(n);
    // Auto-enter ego-network mode on click. This is what the user
    // expects from "Klick auf Knoten" — the graph rebuilds with this
    // node as the new centre, neighbours arranged around. Background
    // click exits the mode and returns to the cluster overview.
    setFocusMode(true);
    // No explicit centerAt — the focus-mode useEffect will zoomToFit
    // the new ego network after the layout pins. Calling centerAt here
    // would conflict with that and produce the "graph just shifts"
    // jankiness the user reported.
  };

  const pathIdSet = useMemo(() => new Set(pathIds), [pathIds]);

  // Top 15 mentions get their label drawn always — Tufte: don't hide
  // important data points behind hover.
  const alwaysLabeled = useMemo(() => {
    const sorted = [...graphData.nodes].sort((a, b) => b.mentions - a.mentions);
    return new Set(sorted.slice(0, 15).map(n => n.id));
  }, [graphData.nodes]);

  // Theme-derived background. PAPER constant stays for tooltips and
  // smaller surfaces that should always feel "warm" — but main canvas,
  // root container and sidebars switch to white when the user picks it.
  const bgMain = theme === "white" ? "#ffffff" : PAPER;

  // Keyboard shortcuts: "/" focuses the search, "Esc" exits ego/clears.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "/") {
        e.preventDefault();
        (document.querySelector(
          'input[placeholder="Name oder Beschreibung"]'
        ) as HTMLInputElement | null)?.focus();
      } else if (e.key === "Escape") {
        if (selected || focusMode) {
          setSelected(null); setFocusMode(false);
          setTimeout(() => graphRef.current?.zoomToFit(600, 60), 60);
        } else if (highlightedIds.size > 0) {
          setHighlightedIds(new Set());
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, focusMode, highlightedIds.size]);

  return (
    <div className="flex-1 flex min-h-0 overflow-hidden"
         style={{ background: bgMain, color: TEXT_INK,
                  fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, sans-serif" }}>
      {/* Left rail — collapsible. Closed = clear, accent-bordered tab
          along the edge so it's obvious you can re-open it. */}
      {!leftRailOpen && (
        <button
          onClick={() => setLeftRailOpen(true)}
          className="shrink-0 flex flex-col items-center justify-center gap-2 hover:opacity-100 transition group"
          style={{
            width: "28px",
            background: bgMain,
            borderRight: `2px solid ${ACCENT}`,
            color: ACCENT,
          }}
          title="Seitenleiste einblenden"
        >
          <span className="text-[18px] font-light">›</span>
          <span className="text-[9px] uppercase tracking-[0.25em]"
                style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}>
            Filter
          </span>
        </button>
      )}
      {leftRailOpen && <LeftRail
        data={data}
        graphData={graphData}
        loading={loading}
        error={error}
        minMentions={minMentions}
        setMinMentions={setMinMentions}
        minEdgeWeight={minEdgeWeight}
        setMinEdgeWeight={setMinEdgeWeight}
        typesEnabled={typesEnabled}
        toggleType={toggleType}
        colourBy={colourBy}
        setColourBy={setColourBy}
        layout={layout}
        setLayout={setLayout}
        showHulls={showHulls}
        setShowHulls={setShowHulls}
        showEdges={showEdges}
        setShowEdges={setShowEdges}
        yearFilter={yearFilter}
        setYearFilter={setYearFilter}
        pathMode={pathMode}
        setPathMode={(v) => { setPathMode(v); if (!v) { setPathFrom(null); setPathIds([]); } }}
        pathFrom={pathFrom}
        pathIds={pathIds}
        clearPath={() => { setPathFrom(null); setPathIds([]); }}
        load={load}
        selected={selected}
        communityById={Object.fromEntries((data?.communities || []).map(c => [c.id, c]))}
        colourOf={colourOf}
        connections={connections}
        navigateTo={navigateTo}
        history={history}
        goBack={goBack}
        focusMode={focusMode}
        setFocusMode={setFocusMode}
        onCollapse={() => setLeftRailOpen(false)}
        bgMain={bgMain}
      />}

      {/* Canvas */}
      <main ref={containerRef} className="flex-1 relative" style={{ background: bgMain }}>
        {/* Search rule, top */}
        <div className="absolute top-0 left-0 right-0 z-10 px-6 py-3 flex items-center gap-4"
             style={{ borderBottom: `1px solid ${RULE}`, background: bgMain }}>
          <label className="text-[10px] uppercase tracking-[0.2em]" style={{ color: TEXT_MUTED }}>
            Suche
          </label>
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Name oder Beschreibung"
            className="flex-1 max-w-md bg-transparent text-sm focus:outline-none"
            style={{ borderBottom: `1px solid ${RULE}`, color: TEXT_INK, paddingBottom: "2px" }}
            onFocus={e => e.target.style.borderBottomColor = ACCENT}
            onBlur={e => e.target.style.borderBottomColor = RULE}
          />
          {highlightedIds.size > 0 && (
            <button
              onClick={() => setHighlightedIds(new Set())}
              className="text-[11px] uppercase tracking-wider hover:opacity-70 transition"
              style={{ color: ACCENT }}
            >
              SPARQL-Auswahl auflösen ({highlightedIds.size})
            </button>
          )}
          {focusMode && selected && (
            <div className="text-[11px] uppercase tracking-wider flex items-center gap-2"
                 style={{ color: ACCENT }}>
              <span className="opacity-70">EGO ·</span>
              <span style={{ color: TEXT_INK }}>{selected.name}</span>
              <button
                onClick={() => { setFocusMode(false); setSelected(null);
                  setTimeout(() => graphRef.current?.zoomToFit(600, 60), 80); }}
                className="ml-1 hover:opacity-70 transition"
                title="zurück zur Übersicht"
              >
                ✕
              </button>
            </div>
          )}
          <div className="text-[11px]" style={{ color: TEXT_MUTED }}>
            {graphData.nodes.length}/{data?.nodes.length ?? 0} Entitäten
            <span className="mx-2" style={{ color: RULE }}>·</span>
            {graphData.links.length} Relationen
          </div>
        </div>

        {/* Inline legend, bottom-left — Tufte-style with type counts */}
        <div className="absolute bottom-4 left-6 z-10 text-[11px]" style={{ color: TEXT_MUTED }}>
          <div className="uppercase tracking-[0.2em] mb-2" style={{ color: TEXT_INK, fontSize: "10px" }}>Legende</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            {TYPE_OPTIONS.map(t => {
              const count = (data?.nodes || []).filter(n => n.type === t).length;
              return (
                <div key={t} className="flex items-center gap-2">
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: TYPE_COLORS[t] }} />
                  <span style={{ color: TEXT_INK }}>{t}</span>
                  <span className="font-mono" style={{ color: TEXT_MUTED }}>{count}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Settings gear — floating top-right of canvas, opens a small
            panel with sliders for node size and edge thickness. */}
        <div className="absolute top-2 right-2 z-20" style={{ marginRight: sparqlOpen ? "0" : 0 }}>
          <button
            onClick={() => setSettingsOpen(o => !o)}
            className="w-8 h-8 flex items-center justify-center text-[16px] transition hover:opacity-100"
            style={{
              opacity: settingsOpen ? 1 : 0.6,
              color: settingsOpen ? ACCENT : TEXT_INK,
              background: settingsOpen ? PAPER : "transparent",
              border: `1px solid ${settingsOpen ? ACCENT : RULE}`,
            }}
            title="Darstellung anpassen"
          >
            ⚙
          </button>
          {settingsOpen && (
            <div className="absolute top-9 right-0 w-64 p-4"
                 style={{ background: PAPER, border: `1px solid ${TEXT_INK}` }}>
              <div className="text-[10px] uppercase tracking-[0.25em] mb-3" style={{ color: TEXT_MUTED }}>
                Darstellung
              </div>
              <Label>
                Knotengröße
                <span className="font-mono ml-2" style={{ color: TEXT_INK }}>
                  {nodeSizeScale.toFixed(1)}×
                </span>
              </Label>
              <input type="range" min={0.5} max={2.5} step={0.1}
                     value={nodeSizeScale}
                     onChange={e => setNodeSizeScale(parseFloat(e.target.value))}
                     className="w-full mt-1 mb-3"
                     style={{ accentColor: ACCENT }} />
              <Label>
                Kantenstärke
                <span className="font-mono ml-2" style={{ color: TEXT_INK }}>
                  {edgeStrokeScale.toFixed(1)}×
                </span>
              </Label>
              <input type="range" min={0.5} max={4.0} step={0.1}
                     value={edgeStrokeScale}
                     onChange={e => setEdgeStrokeScale(parseFloat(e.target.value))}
                     className="w-full mt-1"
                     style={{ accentColor: ACCENT }} />
              <div className="mt-4 pt-3" style={{ borderTop: `1px solid ${RULE}` }}>
                <label className="flex items-center gap-2 cursor-pointer text-[12px]" style={{ color: TEXT_INK }}>
                  <input type="checkbox" checked={showEdgeLabels}
                         onChange={e => setShowEdgeLabels(e.target.checked)}
                         className="rounded" style={{ accentColor: ACCENT }} />
                  Kanten-Beschriftungen zeigen
                </label>
                <p className="text-[10px] mt-0.5 ml-6" style={{ color: TEXT_MUTED }}>
                  Pfad-Modus zeigt sie immer.
                </p>
              </div>
              <div className="mt-3">
                <Label>Hintergrund</Label>
                <div className="flex gap-1 mt-1" style={{ border: `1px solid ${RULE}` }}>
                  {(["paper", "white"] as const).map(opt => (
                    <button key={opt} onClick={() => setTheme(opt)}
                            className="flex-1 px-2 py-1.5 text-[11px] uppercase tracking-wider transition"
                            style={{
                              background: theme === opt ? TEXT_INK : "transparent",
                              color: theme === opt ? PAPER : TEXT_MUTED,
                            }}>
                      {opt === "paper" ? "Papier" : "Weiß"}
                    </button>
                  ))}
                </div>
              </div>
              <div className="mt-4 pt-3 text-[10px]"
                   style={{ borderTop: `1px solid ${RULE}`, color: TEXT_MUTED }}>
                <span style={{ color: TEXT_INK }}>/</span> Suche fokussieren
                <span className="mx-2">·</span>
                <span style={{ color: TEXT_INK }}>Esc</span> Auswahl auflösen
              </div>
              <button onClick={() => { setNodeSizeScale(1); setEdgeStrokeScale(1); setShowEdgeLabels(false); setTheme("paper"); }}
                      className="mt-3 text-[10px] uppercase tracking-wider hover:opacity-100 transition"
                      style={{ color: TEXT_MUTED, opacity: 0.7 }}>
                Zurücksetzen
              </button>
            </div>
          )}
        </div>

        {/* Zoom — three plain buttons, no decoration */}
        <div className="absolute bottom-4 right-6 z-10 flex items-center gap-3 text-[11px] uppercase tracking-wider"
             style={{ color: TEXT_MUTED }}>
          <button onClick={() => graphRef.current?.zoom(graphRef.current.zoom() * 1.4, 300)}
                  className="hover:opacity-100 transition" style={{ opacity: 0.7 }}>Heran</button>
          <span style={{ color: RULE }}>·</span>
          <button onClick={() => graphRef.current?.zoom(graphRef.current.zoom() * 0.7, 300)}
                  className="hover:opacity-100 transition" style={{ opacity: 0.7 }}>Weg</button>
          <span style={{ color: RULE }}>·</span>
          <button onClick={() => graphRef.current?.zoomToFit(600, 80)}
                  className="hover:opacity-100 transition" style={{ opacity: 0.7 }}>Anpassen</button>
        </div>

        {/* The graph */}
        <div className="absolute inset-0" style={{ paddingTop: "44px" }}>
          <ForceGraph2D<FGNode, FGLink>
            ref={graphRef}
            width={size.w}
            height={size.h - 44}
            backgroundColor={bgMain}
            graphData={graphData}
            nodeId="id"
            nodeRelSize={1}
            d3AlphaDecay={
              focusMode && selected ? 0.15
              : layout === "concentric" ? 0.06
              : 0.018
            }
            d3VelocityDecay={
              focusMode && selected ? 0.7
              : layout === "concentric" ? 0.5
              : 0.28
            }
            cooldownTicks={
              focusMode && selected ? 20
              : layout === "concentric" ? 60
              : 500
            }
            warmupTicks={
              focusMode && selected ? 0
              : layout === "concentric" ? 0
              : 80
            }
            enableNodeDrag={layout === "cluster" && colourBy === "community"}
            enableZoomInteraction
            onNodeHover={(n) => setHovered(n)}
            onLinkHover={(l) => setHoveredLink(l)}
            onNodeClick={(n) => handleNodeClick(n as FGNode)}
            onBackgroundClick={() => {
              setSelected(null);
              setFocusMode(false);   // exit ego, return to overview
              if (pathMode) { setPathFrom(null); setPathIds([]); }
              // Refit camera to show the full overview again
              setTimeout(() => graphRef.current?.zoomToFit(600, 60), 80);
            }}
            onEngineStop={() => {
              // Frame the graph nicely after the first layout settles
              if (!graphRef.current) return;
              graphRef.current.zoomToFit(600, 60);
            }}

            // ─── Hulls (cluster mode only): one soft convex polygon per
            // community, drawn UNDER nodes and edges so structure reads at
            // a glance without colour overload. Updated every frame to
            // follow the simulation. ─────────────────────────────────────
            onRenderFramePre={(ctx, scale) => {
              // Ego mode skips ALL hulls — the type-cluster polygons
              // from the cluster layout would otherwise still draw
              // around wherever the ego nodes happen to land, looking
              // like leftover cobwebs from the previous view.
              if (focusMode && selected) return;

              // ─── Concentric layout: tinted annular bands + ring
              //     guides + labels. Each ring band is filled with its
              //     type colour at ~7% alpha — gives the layout the
              //     layered-target depth that pure circles alone lack,
              //     while staying inside the Aicher restraint budget.
              if (layout === "concentric") {
                const types = RING_ORDER.filter(t =>
                  graphData.nodes.some(n => n.type === t && !/^Apple/i.test(n.name))
                );
                const inner = 90, step = 60;
                // Pass 1: filled annular bands, drawn outside-in so the
                // wider outer ring doesn't paint over the inner ones.
                for (let ti = types.length - 1; ti >= 0; ti--) {
                  const r = inner + ti * step;
                  const colour = TYPE_COLORS[types[ti]] || DEFAULT_COLOR;
                  const bandOuter = r + step / 2;
                  const bandInner = Math.max(0, r - step / 2);
                  // Annulus = outer disc minus inner disc (even-odd /
                  // counter-clockwise inner arc punches the hole).
                  ctx.beginPath();
                  ctx.arc(0, 0, bandOuter, 0, Math.PI * 2);
                  ctx.arc(0, 0, bandInner, 0, Math.PI * 2, true);
                  ctx.fillStyle = colour + "12"; // ~7% alpha
                  ctx.fill();
                }
                // Pass 2: ring guide lines + labels on top of the bands
                for (let ti = 0; ti < types.length; ti++) {
                  const r = inner + ti * step;
                  const colour = TYPE_COLORS[types[ti]] || DEFAULT_COLOR;
                  ctx.beginPath();
                  ctx.arc(0, 0, r, 0, Math.PI * 2);
                  ctx.strokeStyle = colour + "44";  // ~27% alpha hairline
                  ctx.lineWidth = 0.6 / scale;
                  ctx.stroke();
                  const fs = 11 / scale;
                  ctx.font = `${fs}px Inter, ui-sans-serif`;
                  ctx.textAlign = "center";
                  ctx.textBaseline = "bottom";
                  ctx.lineWidth = 4 / scale;
                  ctx.strokeStyle = PAPER;
                  ctx.strokeText(types[ti], 0, -r + fs * 0.6);
                  ctx.fillStyle = colour;
                  ctx.fillText(types[ti], 0, -r + fs * 0.6);
                }
                return;
              }

              if (layout !== "cluster" || !showHulls) return;

              // ─── Pack mode: clean circles, one per type ─────────────
              if (colourBy === "type" && clusterBubbles.length > 0) {
                for (const b of clusterBubbles) {
                  const colour = TYPE_COLORS[b.type] || DEFAULT_COLOR;
                  ctx.beginPath();
                  ctx.arc(b.cx, b.cy, b.r, 0, Math.PI * 2);
                  ctx.fillStyle   = colour + "0d";  // ~5% alpha
                  ctx.strokeStyle = colour + "55";  // ~33% alpha hairline
                  ctx.lineWidth   = 0.7 / scale;
                  ctx.fill();
                  ctx.stroke();
                  // Type label in the cluster colour above the circle.
                  const fs = 13 / scale;
                  ctx.font = `${fs}px Inter, ui-sans-serif`;
                  ctx.textAlign = "center";
                  ctx.textBaseline = "bottom";
                  ctx.lineWidth = 4 / scale;
                  ctx.strokeStyle = PAPER;
                  ctx.strokeText(b.type, b.cx, b.cy - b.r - 6 / scale);
                  ctx.fillStyle = colour;
                  ctx.fillText(b.type, b.cx, b.cy - b.r - 6 / scale);
                }
                return;
              }

              // ─── Community-cluster mode: convex hulls around messy
              //     force-positioned groups (didactic counterexample) ──
              const groups = new Map<string, [number, number][]>();
              for (const n of graphData.nodes) {
                const key = colourBy === "community" ? n.community_id : n.type;
                if (!key || n.x == null || n.y == null) continue;
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key)!.push([n.x, n.y]);
              }
              for (const [key, pts] of groups) {
                // Skip tiny groups — single dots or pairs as polygons
                // are visual lint. Threshold of 4 keeps only meaningful
                // groupings (≥4 members).
                if (pts.length < 4) continue;
                const hull = convexHull(pts);
                if (hull.length < 3) continue;
                const colour = colourBy === "community"
                  ? (communityColour.get(key) || DEFAULT_COLOR)
                  : (TYPE_COLORS[key] || DEFAULT_COLOR);
                // Expand outward for breathing room.
                const cx = hull.reduce((s, p) => s + p[0], 0) / hull.length;
                const cy = hull.reduce((s, p) => s + p[1], 0) / hull.length;
                const pad = 18 / scale;
                ctx.beginPath();
                hull.forEach((p, i) => {
                  const dx = p[0] - cx, dy = p[1] - cy;
                  const len = Math.hypot(dx, dy) || 1;
                  const x = p[0] + dx / len * pad, y = p[1] + dy / len * pad;
                  if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                });
                ctx.closePath();
                ctx.fillStyle   = colour + "0d";   // ~5% alpha
                ctx.strokeStyle = colour + "55";   // ~33% alpha hairline
                ctx.lineWidth   = 0.6 / scale;
                ctx.fill();
                ctx.stroke();

                // Type-mode: label each island in the hull's colour at
                // top of the bbox. Community-mode: no labels (community
                // IDs aren't human-readable).
                if (colourBy === "type") {
                  const minY = Math.min(...pts.map(p => p[1]));
                  const fs = 13 / scale;
                  ctx.font = `${fs}px Inter, ui-sans-serif`;
                  ctx.textAlign = "center";
                  ctx.textBaseline = "bottom";
                  ctx.lineWidth = 4 / scale;
                  ctx.strokeStyle = PAPER;
                  ctx.strokeText(key, cx, minY - pad - 4 / scale);
                  ctx.fillStyle = colour;
                  ctx.fillText(key, cx, minY - pad - 4 / scale);
                }
              }
            }}

            // Restrained node rendering: filled disk sized by mentions,
            // accent ring only for hover/selection/SPARQL-highlight/path.
            nodeCanvasObjectMode={() => "replace"}
            nodeCanvasObject={(n, ctx, scale) => {
              const colour = colourOf(n);
              // Bigger base so they read on a busy canvas — addresses
              // "optisch zu klein". sqrt scaling keeps top nodes legible
              // without crushing the long tail. Multiplied by user-
              // adjustable nodeSizeScale from the settings gear.
              //
              // Cap at 11 so Apple (85 mentions → would be ~22) doesn't
              // dominate the viewport in low-density views (search,
              // focus mode). The Tufte way: don't let one outlier
              // visually swallow the dataset.
              const rawR = 3.2 + Math.sqrt(n.mentions + 1) * 2.1;
              const baseR = Math.min(rawR, 11) * nodeSizeScale;
              const isHovered = hovered?.id === n.id;
              const isSelected = selected?.id === n.id;
              const isHighlight = highlightedIds.has(n.id);
              const isOnPath = pathIdSet.has(n.id);
              const isPathFrom = pathFrom?.id === n.id;
              const dim = isDimmed(n.id) || (pathIds.length > 1 && !isOnPath);
              const r = isHovered ? baseR * 1.3 : baseR;
              n.__radius = r;
              ctx.save();
              if (dim) ctx.globalAlpha = 0.15;
              // Single filled disk — no halo, no glow
              ctx.beginPath();
              ctx.arc(n.x!, n.y!, r, 0, Math.PI * 2);
              ctx.fillStyle = isOnPath ? ACCENT : colour;
              ctx.fill();
              // Accent ring for state
              if (isHovered || isSelected || isHighlight || isOnPath || isPathFrom) {
                ctx.beginPath();
                ctx.arc(n.x!, n.y!, r + 3 / scale, 0, Math.PI * 2);
                ctx.strokeStyle = ACCENT;
                ctx.lineWidth = (isPathFrom ? 1.8 : 1.2) / scale;
                ctx.stroke();
              }
              // Role icon — Aicher-style monochrome pictogram inside the
              // disc. Tells you "this is a CEO" / "this is a Smartphone"
              // without expanding. Drawn in PAPER (white) so it reads on
              // every type colour. Only drawn when the disc is big enough
              // that the icon shape is recognisable.
              const role = primaryRole(n.roles || []);
              const drawIcon = role ? ICON_DRAW[role] : null;
              if (drawIcon && r * scale > 7) {
                const iconSize = r * 0.55;
                ctx.fillStyle = PAPER;
                drawIcon(ctx, n.x!, n.y!, iconSize);
              }
              // Labels are now drawn in onRenderFramePost so that we can
              // do priority-ordered collision detection (high-mention
              // labels first, others suppressed if they'd overlap).
              ctx.restore();
            }}
            // Edges
            linkCanvasObjectMode={() => "replace"}
            linkCanvasObject={(l, ctx, scale) => {
              const src = l.source as FGNode;
              const tgt = l.target as FGNode;
              if (!src.x || !src.y || !tgt.x || !tgt.y) return;
              // Edge-toggle: when hidden, only draw if part of an
              // explicit highlight (path, hover-incident, SPARQL-match).
              if (!showEdges) {
                const sid = src.id, tid = tgt.id;
                const involved = hovered && (sid === hovered.id || tid === hovered.id);
                const onPath = pathIds.length > 1 && pathIds.some((id, i) =>
                  i < pathIds.length - 1 &&
                  ((id === sid && pathIds[i+1] === tid) ||
                   (id === tid && pathIds[i+1] === sid))
                );
                const sparqlHl = highlightedIds.size > 0 &&
                  highlightedIds.has(sid) && highlightedIds.has(tid);
                if (!involved && !onPath && !sparqlHl) return;
              }
              const involved = hovered != null && (src.id === hovered.id || tgt.id === hovered.id);
              const sparqlHl = highlightedIds.size > 0 && (highlightedIds.has(src.id) && highlightedIds.has(tgt.id));
              // path: an edge is "on path" if both endpoints AND they are
              // adjacent in the path sequence
              const pathHl = (() => {
                if (pathIds.length < 2) return false;
                for (let i = 0; i < pathIds.length - 1; i++) {
                  if ((pathIds[i] === src.id && pathIds[i+1] === tgt.id) ||
                      (pathIds[i] === tgt.id && pathIds[i+1] === src.id)) return true;
                }
                return false;
              })();
              const dim = ((neighbourIds && !involved && !(neighbourIds.has(src.id) && neighbourIds.has(tgt.id)))
                       || (highlightedIds.size > 0 && !sparqlHl && !(highlightedIds.has(src.id) || highlightedIds.has(tgt.id)))
                       || (pathIds.length > 1 && !pathHl));
              ctx.save();
              if (dim) ctx.globalAlpha = 0.14;
              // Hover-incident edges now read MUCH more strongly: accent
              // colour, 2 px line. Used to be a faint #525252 hairline.
              // Path/SPARQL stay accent, regular edges stay hairline grey.
              ctx.strokeStyle = (pathHl || sparqlHl || involved) ? ACCENT : "#cfcbc3";
              // Edge weight → thickness. Edges with stronger evidence
              // (multiple co-occurrences, multiple supporting facts)
              // draw thicker so the eye sees the spine of the graph.
              // sqrt scaling so 10× weight isn't 10× the line width.
              const w = (l as any).weight ?? 1;
              const weightFactor = Math.min(2.5, Math.sqrt(Math.max(w, 1)) * 0.8);
              const baseW = pathHl ? 2.4
                          : involved ? 1.8
                          : sparqlHl ? 1.4
                          : 0.5 * weightFactor;
              ctx.lineWidth = (baseW * edgeStrokeScale) / scale;
              ctx.beginPath();
              ctx.moveTo(src.x, src.y);
              ctx.lineTo(tgt.x, tgt.y);
              ctx.stroke();
              ctx.restore();
            }}
            // ─── Label pass — drawn AFTER nodes/edges with priority-ordered
            // collision detection. High-mention labels (top of mentions
            // ranking) get drawn first; subsequent labels are skipped if
            // their bounding box would overlap an already-drawn one. This
            // produces a readable canvas even inside dense clusters
            // (the EVENT pile of fiscal years used to be unreadable). ──
            onRenderFramePost={(ctx, scale) => {
              const drawn: { x: number; y: number; w: number; h: number }[] = [];
              const fits = (r: { x: number; y: number; w: number; h: number }) => {
                for (const d of drawn) {
                  if (Math.abs(r.x - d.x) < (r.w + d.w) / 2 &&
                      Math.abs(r.y - d.y) < (r.h + d.h) / 2) return false;
                }
                return true;
              };
              // Priority order: hovered > selected > pathway > alwaysLabeled,
              // then everything else by mentions desc as a tiebreaker.
              const sortedNodes = [...graphData.nodes].sort((a, b) => {
                const pa = (hovered?.id === a.id ? 5 : 0)
                         + (selected?.id === a.id ? 4 : 0)
                         + (pathIdSet.has(a.id) ? 3 : 0)
                         + (alwaysLabeled.has(a.id) ? 1 : 0);
                const pb = (hovered?.id === b.id ? 5 : 0)
                         + (selected?.id === b.id ? 4 : 0)
                         + (pathIdSet.has(b.id) ? 3 : 0)
                         + (alwaysLabeled.has(b.id) ? 1 : 0);
                return (pb - pa) || (b.mentions - a.mentions);
              });
              for (const n of sortedNodes) {
                if (n.x == null || n.y == null) continue;
                const isHovered = hovered?.id === n.id;
                const isSelected = selected?.id === n.id;
                const isOnPath = pathIdSet.has(n.id);
                const isAlways = alwaysLabeled.has(n.id);
                const dim = isDimmed(n.id) || (pathIds.length > 1 && !isOnPath);
                if (dim && !isHovered && !isSelected && !isOnPath) continue;
                const eligible = isHovered || isSelected || isOnPath || isAlways || scale > 1.6;
                if (!eligible) continue;
                const fontSize = (isHovered || isOnPath ? 13 : 11) / scale;
                ctx.font = `${fontSize}px Inter, ui-sans-serif`;
                const label = shortLabel(n.name);
                const tw = ctx.measureText(label).width;
                const r = n.__radius || 8;
                const lx = n.x;
                const ly = n.y + r + 3 / scale + fontSize / 2;
                const box = { x: lx, y: ly, w: tw + 6 / scale, h: fontSize + 3 / scale };
                // Hovered/selected/path labels override collision — they
                // must be visible, even if they cover something underneath.
                if (!isHovered && !isSelected && !isOnPath && !fits(box)) continue;
                ctx.textAlign = "center";
                ctx.textBaseline = "top";
                ctx.lineWidth = 3.5 / scale;
                ctx.strokeStyle = PAPER;
                ctx.strokeText(label, lx, n.y + r + 3 / scale);
                ctx.fillStyle = isOnPath ? ACCENT : TEXT_INK;
                ctx.fillText(label, lx, n.y + r + 3 / scale);
                drawn.push(box);
              }

              // ─── Edge-label pass ─────────────────────────────────────
              // Path edges always labelled (the user explicitly picked
              // them and needs to read the path as a sentence). Hover/
              // hover-incident labels are opt-in via showEdgeLabels —
              // off by default because they clutter dense ego networks.
              const edgesToLabel: FGLink[] = [];
              const pushUnique = (l: FGLink) => {
                if (!edgesToLabel.includes(l)) edgesToLabel.push(l);
              };
              // Path edges (highest priority — always shown).
              if (pathIds.length > 1) {
                for (const l of graphData.links) {
                  const sid = typeof l.source === "string" ? l.source : (l.source as FGNode).id;
                  const tid = typeof l.target === "string" ? l.target : (l.target as FGNode).id;
                  for (let i = 0; i < pathIds.length - 1; i++) {
                    if ((pathIds[i] === sid && pathIds[i+1] === tid) ||
                        (pathIds[i] === tid && pathIds[i+1] === sid)) {
                      pushUnique(l); break;
                    }
                  }
                }
              }
              // Hover-node-incident edges + directly hovered edge are
              // ONLY labelled when the user has opted in.
              if (showEdgeLabels) {
                if (hovered) {
                  for (const l of graphData.links) {
                    const sid = typeof l.source === "string" ? l.source : (l.source as FGNode).id;
                    const tid = typeof l.target === "string" ? l.target : (l.target as FGNode).id;
                    if (sid === hovered.id || tid === hovered.id) pushUnique(l);
                  }
                }
                if (hoveredLink) pushUnique(hoveredLink);
              }

              for (const l of edgesToLabel) {
                const src = l.source as FGNode;
                const tgt = l.target as FGNode;
                if (!src.x || !src.y || !tgt.x || !tgt.y) continue;
                const relLabel = prettifyRelation(l.type);
                if (!relLabel) continue;
                const isOnPathEdge = (() => {
                  if (pathIds.length < 2) return false;
                  for (let i = 0; i < pathIds.length - 1; i++) {
                    if ((pathIds[i] === src.id && pathIds[i+1] === tgt.id) ||
                        (pathIds[i] === tgt.id && pathIds[i+1] === src.id)) return true;
                  }
                  return false;
                })();
                const mx = (src.x + tgt.x) / 2;
                const my = (src.y + tgt.y) / 2;
                // Rotate label to align with the edge but always read
                // left-to-right (flip 180° if it would read upside-down).
                let angle = Math.atan2(tgt.y - src.y, tgt.x - src.x);
                if (angle > Math.PI / 2 || angle < -Math.PI / 2) angle += Math.PI;
                ctx.save();
                ctx.translate(mx, my);
                ctx.rotate(angle);
                const fs = (isOnPathEdge ? 12 : 11) / scale;
                ctx.font = `${fs}px Inter, ui-sans-serif`;
                ctx.textAlign = "center";
                ctx.textBaseline = "bottom";
                ctx.lineWidth = 4 / scale;
                ctx.strokeStyle = PAPER;
                ctx.strokeText(relLabel, 0, -3 / scale);
                ctx.fillStyle = isOnPathEdge ? ACCENT : TEXT_INK;
                ctx.fillText(relLabel, 0, -3 / scale);
                ctx.restore();
              }
            }}
            linkDirectionalParticles={0}
          />
        </div>

        {hovered && (
          <NodeTooltip
            node={hovered}
            community={(data?.communities || []).find(c => c.id === hovered.community_id) || null}
            colour={colourOf(hovered)}
          />
        )}

        {/* When SPARQL panel is OPEN, show an in-panel collapse handle
            (rendered inside the panel itself, below). When CLOSED, the
            sliver to the right of <main> shows a re-open button. */}
      </main>

      {sparqlOpen && (
        <SparqlConsole
          onResults={handleSparqlExecuted}
          nodeIds={new Set(graphData.nodes.map(n => n.id))}
          onCollapse={() => setSparqlOpen(false)}
          bgMain={bgMain}
        />
      )}
      {!sparqlOpen && (
        <button
          onClick={() => setSparqlOpen(true)}
          className="shrink-0 flex flex-col items-center justify-center gap-2 hover:opacity-100 transition"
          style={{
            width: "28px",
            background: bgMain,
            borderLeft: `2px solid ${ACCENT}`,
            color: ACCENT,
          }}
          title="SPARQL-Konsole einblenden"
        >
          <span className="text-[18px] font-light">‹</span>
          <span className="text-[9px] uppercase tracking-[0.25em]"
                style={{ writingMode: "vertical-rl" }}>
            SPARQL
          </span>
        </button>
      )}
    </div>
  );
}


// ─── Left rail ─────────────────────────────────────────────────────────────
function LeftRail(props: {
  data: GraphPayload | null;
  graphData: { nodes: FGNode[]; links: FGLink[] };
  loading: boolean;
  error: string | null;
  minMentions: number;
  setMinMentions: (n: number) => void;
  minEdgeWeight: number;
  setMinEdgeWeight: (n: number) => void;
  typesEnabled: Set<string>;
  toggleType: (t: string) => void;
  colourBy: ColourBy;
  setColourBy: (c: ColourBy) => void;
  layout: Layout;
  setLayout: (l: Layout) => void;
  showHulls: boolean;
  setShowHulls: (v: boolean) => void;
  showEdges: boolean;
  setShowEdges: (v: boolean) => void;
  yearFilter: number | null;
  setYearFilter: (v: number | null) => void;
  pathMode: boolean;
  setPathMode: (v: boolean) => void;
  pathFrom: FGNode | null;
  pathIds: string[];
  clearPath: () => void;
  load: () => void;
  selected: FGNode | null;
  communityById: Record<string, GraphCommunity>;
  colourOf: (n: FGNode) => string;
  connections: ConnectionGroup[];
  navigateTo: (id: string) => void;
  history: FGNode[];
  goBack: () => void;
  focusMode: boolean;
  setFocusMode: (v: boolean) => void;
  onCollapse: () => void;
  bgMain: string;
}) {
  const {
    data, loading, error, minMentions, setMinMentions,
    minEdgeWeight, setMinEdgeWeight,
    typesEnabled,
    toggleType, colourBy, setColourBy, layout, setLayout, showHulls, setShowHulls,
    showEdges, setShowEdges, yearFilter, setYearFilter,
    pathMode, setPathMode, pathFrom, pathIds, clearPath,
    load, selected, communityById, colourOf,
    connections, navigateTo, history, goBack, focusMode, setFocusMode,
    onCollapse, bgMain,
  } = props;
  return (
    <aside className="w-72 shrink-0 overflow-y-auto"
           style={{ background: bgMain, borderRight: `1px solid ${RULE}`, color: TEXT_INK }}>
      <div className="px-5 pt-6 pb-5" style={{ borderBottom: `1px solid ${RULE}` }}>
        <div className="flex items-start justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em]" style={{ color: TEXT_MUTED }}>
              UC5 · Knowledge Graph
            </div>
            <h2 className="font-medium text-base mt-1" style={{ letterSpacing: "-0.01em" }}>
              Apple Ontologie
            </h2>
          </div>
          <button onClick={onCollapse}
                  className="text-[16px] leading-none flex items-center justify-center transition"
                  style={{
                    width: "26px", height: "26px",
                    border: `1px solid ${RULE}`,
                    color: TEXT_INK,
                    background: bgMain,
                  }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = ACCENT; e.currentTarget.style.color = ACCENT; }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = RULE; e.currentTarget.style.color = TEXT_INK; }}
                  title="Seitenleiste einklappen">
            ‹
          </button>
        </div>
        {data && (
          <dl className="mt-4 text-[11px]" style={{ color: TEXT_MUTED }}>
            <Row label="Entitäten" value={`${data.nodes.length}`} />
            <Row label="Relationen" value={`${data.edges.length}`} />
            <Row label="Communities" value={`${data.communities.length}`} />
          </dl>
        )}
      </div>

      <Section title="Anzeige">
        <Label>Mindesterwähnungen <span className="font-mono ml-2" style={{ color: TEXT_INK }}>{minMentions}</span></Label>
        <input
          type="range" min={1} max={10} value={minMentions}
          onChange={e => setMinMentions(Number(e.target.value))}
          className="w-full mt-1"
          style={{ accentColor: ACCENT }}
        />
        <div className="mt-3">
          <Label>Min. Kantengewicht <span className="font-mono ml-2" style={{ color: TEXT_INK }}>{minEdgeWeight}</span></Label>
          <input
            type="range" min={1} max={10} value={minEdgeWeight}
            onChange={e => setMinEdgeWeight(Number(e.target.value))}
            className="w-full mt-1"
            style={{ accentColor: ACCENT }}
          />
          <p className="text-[10px] mt-1" style={{ color: TEXT_MUTED }}>
            1 = alle Kanten · ≥2 nur wiederholt belegte Beziehungen
          </p>
        </div>
        <div className="mt-4">
          <div className="flex items-center justify-between">
            <Label>Zeitfilter (EVENT)
              <span className="font-mono ml-2" style={{ color: TEXT_INK }}>
                {yearFilter ?? "—"}
              </span>
            </Label>
            {yearFilter != null && (
              <button onClick={() => setYearFilter(null)}
                      className="text-[10px] uppercase tracking-wider hover:opacity-100"
                      style={{ color: ACCENT, opacity: 0.85 }}>
                aus
              </button>
            )}
          </div>
          <input
            type="range" min={1976} max={2025} step={1}
            value={yearFilter ?? 2000}
            onChange={e => setYearFilter(Number(e.target.value))}
            className="w-full mt-1"
            style={{ accentColor: ACCENT }}
          />
          {yearFilter != null && (
            <p className="text-[10px] mt-1" style={{ color: TEXT_MUTED }}>
              EVENT-Knoten ohne {yearFilter} im Namen sind ausgeblendet.
            </p>
          )}
        </div>
      </Section>

      <Section title="Entitätstypen">
        <div className="space-y-1">
          {TYPE_OPTIONS.map(t => {
            const on = typesEnabled.has(t);
            const count = (data?.nodes || []).filter(n => n.type === t).length;
            return (
              <label key={t} className="flex items-center gap-2.5 cursor-pointer text-[13px]">
                <input type="checkbox" checked={on} onChange={() => toggleType(t)}
                       className="rounded" style={{ accentColor: ACCENT }} />
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: TYPE_COLORS[t] }} />
                <span className="flex-1" style={{ color: on ? TEXT_INK : TEXT_MUTED }}>{t}</span>
                <span className="font-mono text-[11px]" style={{ color: TEXT_MUTED }}>{count}</span>
              </label>
            );
          })}
        </div>
      </Section>

      <Section title="Gruppierung & Farbe">
        <div className="flex gap-1" style={{ border: `1px solid ${RULE}` }}>
          {(["type", "community"] as ColourBy[]).map(opt => (
            <button key={opt} onClick={() => setColourBy(opt)}
                    className="flex-1 px-2 py-1.5 text-[11px] uppercase tracking-wider transition"
                    style={{
                      background: colourBy === opt ? TEXT_INK : "transparent",
                      color: colourBy === opt ? PAPER : TEXT_MUTED,
                    }}>
              {opt === "type" ? "Typ" : "Community"}
            </button>
          ))}
        </div>
        <p className="text-[11px] leading-relaxed mt-2" style={{ color: TEXT_MUTED }}>
          {colourBy === "type"
            ? "Eine Insel pro Entitätstyp (semantisch sauber: alle Produkte zusammen, alle Personen zusammen …)."
            : "Louvain-Communities aus Kanten-Dichte berechnet. Mathematisch sauber, aber bei dünnem Graph nicht immer intuitiv."}
        </p>
      </Section>

      <Section title="Layout">
        <div className="flex gap-1 mb-3" style={{ border: `1px solid ${RULE}` }}>
          {(["cluster", "concentric"] as Layout[]).map(opt => (
            <button key={opt} onClick={() => setLayout(opt)}
                    className="flex-1 px-2 py-1.5 text-[11px] uppercase tracking-wider transition"
                    style={{
                      background: layout === opt ? TEXT_INK : "transparent",
                      color: layout === opt ? PAPER : TEXT_MUTED,
                    }}>
              {opt === "cluster" ? "Cluster" : "Konzentrik"}
            </button>
          ))}
        </div>
        {layout === "cluster" && (
          <label className="flex items-center gap-2 cursor-pointer text-[12px] mb-2" style={{ color: TEXT_INK }}>
            <input type="checkbox" checked={showHulls}
                   onChange={e => setShowHulls(e.target.checked)}
                   className="rounded" style={{ accentColor: ACCENT }} />
            Cluster-Polygone zeigen
          </label>
        )}
        <label className="flex items-center gap-2 cursor-pointer text-[12px]" style={{ color: TEXT_INK }}>
          <input type="checkbox" checked={showEdges}
                 onChange={e => setShowEdges(e.target.checked)}
                 className="rounded" style={{ accentColor: ACCENT }} />
          Verbindungen zeigen
        </label>
        {!showEdges && (
          <p className="text-[10px] mt-1.5" style={{ color: TEXT_MUTED }}>
            Bei Hover/Selektion/Pfad trotzdem sichtbar.
          </p>
        )}
        {layout === "concentric" && (
          <p className="text-[11px] leading-relaxed mt-2" style={{ color: TEXT_MUTED }}>
            Apple im Zentrum, Ringe nach Typ. Innerhalb eines Rings
            sortiert nach Erwähnungen (12 Uhr = häufigste).
          </p>
        )}
      </Section>

      <Section title="Pfad-Modus">
        <button onClick={() => setPathMode(!pathMode)}
                className="w-full px-3 py-1.5 text-[11px] uppercase tracking-wider transition"
                style={{
                  border: `1px solid ${pathMode ? ACCENT : TEXT_INK}`,
                  background: pathMode ? ACCENT : "transparent",
                  color: pathMode ? PAPER : TEXT_INK,
                }}>
          {pathMode ? "aktiv  ·  beenden" : "aktivieren"}
        </button>
        {pathMode && (
          <p className="text-[11px] leading-relaxed mt-2" style={{ color: TEXT_MUTED }}>
            {pathFrom == null && pathIds.length === 0 && "Klicke den Startknoten."}
            {pathFrom != null && (
              <>Start: <span style={{ color: TEXT_INK }}>{pathFrom.name}</span> · klicke Ziel.</>
            )}
            {pathIds.length > 1 && (
              <>Pfad mit <span className="font-mono" style={{ color: TEXT_INK }}>{pathIds.length}</span> Knoten · {pathIds.length - 1} Schritte.</>
            )}
          </p>
        )}
        {pathIds.length > 0 && (
          <button onClick={clearPath}
                  className="mt-2 text-[10px] uppercase tracking-wider hover:opacity-100 transition"
                  style={{ color: TEXT_MUTED, opacity: 0.7 }}>
            Pfad löschen
          </button>
        )}
      </Section>

      <div className="px-5 py-4">
        <button onClick={load} disabled={loading}
                className="w-full px-3 py-2 text-[11px] uppercase tracking-wider transition"
                style={{
                  border: `1px solid ${TEXT_INK}`,
                  background: loading ? PAPER_SOFT : "transparent",
                  color: TEXT_INK,
                  opacity: loading ? 0.5 : 1,
                }}
                onMouseEnter={e => { if (!loading) { e.currentTarget.style.background = TEXT_INK; e.currentTarget.style.color = PAPER; } }}
                onMouseLeave={e => { if (!loading) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = TEXT_INK; } }}>
          {loading ? "Lädt …" : "Aktualisieren"}
        </button>
      </div>

      <DBpediaValidator onComplete={load} />


      {error && (
        <div className="mx-5 mb-4 text-[11px] p-2.5"
             style={{ border: `1px solid ${ACCENT}`, color: ACCENT }}>{error}</div>
      )}

      {selected && (
        <ConnectionPanel
          selected={selected}
          connections={connections}
          history={history}
          goBack={goBack}
          navigateTo={navigateTo}
          focusMode={focusMode}
          setFocusMode={setFocusMode}
          colourOf={colourOf}
          communityById={communityById}
        />
      )}
    </aside>
  );
}


// ─── Connection Panel — Wikipedia-style hyperlinked navigation ────────────
// When an entity is selected, this panel shows ALL its connections grouped
// by relation type and direction. Clicking any neighbour navigates to it
// (which fires camera fly-to, updates the panel, pushes the previous
// selection onto a back-stack so the user can wander Wikipedia-style).
function ConnectionPanel(props: {
  selected: FGNode;
  connections: ConnectionGroup[];
  history: FGNode[];
  goBack: () => void;
  navigateTo: (id: string) => void;
  focusMode: boolean;
  setFocusMode: (v: boolean) => void;
  colourOf: (n: FGNode) => string;
  communityById: Record<string, GraphCommunity>;
}) {
  const {
    selected, connections, history, goBack, navigateTo,
    focusMode, setFocusMode, colourOf, communityById,
  } = props;

  const totalConnections = connections.reduce((s, g) => s + g.entries.length, 0);
  const community = selected.community_id ? communityById[selected.community_id] : null;

  return (
    <div className="px-5 py-5" style={{ borderTop: `1px solid ${RULE}` }}>
      {/* Breadcrumb */}
      {history.length > 0 && (
        <div className="mb-3 text-[10px] uppercase tracking-[0.2em]">
          <button onClick={goBack} className="hover:opacity-100 transition"
                  style={{ color: ACCENT, opacity: 0.85 }}>
            ‹ zurück zu {history[history.length - 1].name}
          </button>
          <span className="mx-2" style={{ color: RULE }}>·</span>
          <span style={{ color: TEXT_MUTED }}>{history.length} Schritt{history.length === 1 ? "" : "e"}</span>
        </div>
      )}

      {/* Identity card */}
      <div className="text-[10px] uppercase tracking-[0.25em] mb-2" style={{ color: TEXT_MUTED }}>
        Ausgewählt
      </div>
      <div className="flex items-center gap-2 mb-1">
        <span className="w-2.5 h-2.5 rounded-full" style={{ background: colourOf(selected) }} />
        <span className="font-medium text-[16px]" style={{ letterSpacing: "-0.01em" }}>
          {selected.name}
        </span>
      </div>
      <div className="text-[11px] font-mono" style={{ color: TEXT_MUTED }}>
        {selected.type} · {selected.mentions} Erwähnungen
        {selected.community_id && <> · {selected.community_id}</>}
      </div>
      {/* OWL sub-class roles as compact tags. Click would filter the
          graph to other entities sharing that role — a TODO for a
          follow-up, currently informational only. */}
      {selected.roles && selected.roles.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {selected.roles.map(r => (
            <span key={r}
                  className="text-[10px] uppercase tracking-wider px-1.5 py-0.5"
                  style={{
                    border: `1px solid ${TYPE_COLORS[selected.type] || DEFAULT_COLOR}55`,
                    color: TYPE_COLORS[selected.type] || TEXT_INK,
                    background: PAPER_SOFT,
                  }}>
              {r}
            </span>
          ))}
        </div>
      )}
      {selected.description && (
        <p className="text-[13px] mt-3 leading-relaxed whitespace-pre-wrap">
          {selected.description}
        </p>
      )}

      {/* Focus toggle — only this entity's ego network visible in the graph */}
      <button onClick={() => setFocusMode(!focusMode)}
              className="mt-3 px-2.5 py-1 text-[10px] uppercase tracking-wider transition"
              style={{
                border: `1px solid ${focusMode ? ACCENT : RULE}`,
                background: focusMode ? ACCENT : "transparent",
                color: focusMode ? PAPER : TEXT_MUTED,
              }}>
        {focusMode ? "Übersicht zeigen" : "Ego-Netzwerk anzeigen"}
      </button>
      <p className="text-[10px] mt-1.5 leading-relaxed" style={{ color: TEXT_MUTED }}>
        {focusMode
          ? "Klick auf einen Nachbar wandert weiter. Klick auf leere Fläche kehrt zur Übersicht zurück."
          : "Klick auf den Knoten zeigt Ego-Netzwerk. Hover ohne Klick lässt die Übersicht stehen."}
      </p>

      {/* Connections grouped by relation type */}
      <div className="mt-5">
        <div className="flex items-baseline justify-between mb-3">
          <div className="text-[10px] uppercase tracking-[0.25em]" style={{ color: TEXT_INK }}>
            Verbindungen
          </div>
          <div className="text-[10px] font-mono" style={{ color: TEXT_MUTED }}>
            {totalConnections}
          </div>
        </div>

        {connections.length === 0 && (
          <p className="text-[11px]" style={{ color: TEXT_MUTED }}>
            Keine Verbindungen im aktuellen Sub-Graphen.
          </p>
        )}

        <div className="space-y-4">
          {connections.map(group => (
            <div key={`${group.relType}-${group.direction}`}>
              <div className="flex items-center gap-1.5 mb-1.5 text-[10px] uppercase tracking-[0.2em]"
                   style={{ color: TEXT_MUTED }}>
                <span style={{ color: TEXT_INK, fontFamily: "monospace" }}>
                  {group.direction === "out" ? "→" : "←"}
                </span>
                <span>{prettifyRelation(group.relType)}</span>
                <span className="ml-auto font-mono" style={{ color: TEXT_MUTED }}>
                  {group.entries.length}
                </span>
              </div>
              <div>
                {group.entries.map(e => (
                  <button
                    key={e.node.id}
                    onClick={() => navigateTo(e.node.id)}
                    className="w-full text-left flex items-center gap-2 py-1 px-1 -mx-1 transition group"
                    style={{ color: TEXT_INK }}
                    onMouseEnter={ev => ev.currentTarget.style.background = PAPER_SOFT}
                    onMouseLeave={ev => ev.currentTarget.style.background = "transparent"}
                  >
                    <span className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ background: TYPE_COLORS[e.node.type] || DEFAULT_COLOR }} />
                    <span className="text-[12.5px] truncate flex-1" title={e.node.name}>
                      {e.node.name}
                    </span>
                    <span className="text-[10px] font-mono opacity-60 group-hover:opacity-100 transition"
                          style={{ color: TEXT_MUTED }}>
                      {e.node.mentions}
                    </span>
                    <span className="text-[10px] opacity-0 group-hover:opacity-100 transition"
                          style={{ color: ACCENT }}>↗</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Community context */}
      {community && (
        <div className="mt-5 pt-4" style={{ borderTop: `1px solid ${RULE}` }}>
          <div className="text-[10px] uppercase tracking-[0.25em]" style={{ color: TEXT_MUTED }}>
            Community {community.id}
          </div>
          <div className="text-[11px] mt-1 mb-2 font-mono" style={{ color: TEXT_MUTED }}>
            Level {community.level} · {community.size} Mitglieder
          </div>
          <p className="text-[12px] leading-relaxed">{community.summary}</p>
        </div>
      )}
    </div>
  );
}


// ─── DBpedia enrichment — persons validator + products chronology ─────────
function DBpediaValidator({ onComplete }: { onComplete: () => void }) {
  const [runningP, setRunningP] = useState(false);
  const [runningR, setRunningR] = useState(false);
  const [runningE, setRunningE] = useState(false);
  const [runningC, setRunningC] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statsP, setStatsP] = useState<null | {
    canonical_persons_fetched: number;
    canonical_persons_added: number;
    unverified_persons_total: number;
    persons_confirmed: number;
    persons_demoted: number;
  }>(null);
  const [statsR, setStatsR] = useState<null | {
    products_fetched: number;
    products_added: number;
    successor_added: number;
    predecessor_added: number;
  }>(null);
  const [statsE, setStatsE] = useState<null | {
    edges_fetched: number;
    edges_upserted: number;
    by_relation: Record<string, number>;
  }>(null);
  const [statsC, setStatsC] = useState<null | {
    pairs_total: number;
    pairs_above_threshold: number;
    pairs_below_threshold: number;
    threshold: number;
  }>(null);
  const busy = runningP || runningR || runningE || runningC;

  const runPersons = async () => {
    setRunningP(true); setError(null); setStatsP(null);
    try {
      const r = await api.ue4Validate();
      if (!r.ok) { setError(r.error || "Validator fehlgeschlagen"); return; }
      setStatsP(r.stats || null);
      onComplete();
    } catch (e) { setError(String(e)); }
    finally { setRunningP(false); }
  };

  const runProducts = async () => {
    setRunningR(true); setError(null); setStatsR(null);
    try {
      const r = await api.ue4EnrichProducts();
      if (!r.ok) { setError(r.error || "Anreicherung fehlgeschlagen"); return; }
      setStatsR(r.stats || null);
      onComplete();
    } catch (e) { setError(String(e)); }
    finally { setRunningR(false); }
  };

  const runEdges = async () => {
    setRunningE(true); setError(null); setStatsE(null);
    try {
      const r = await api.ue4EnrichEdges();
      if (!r.ok) { setError(r.error || "Edge-Anreicherung fehlgeschlagen"); return; }
      setStatsE(r.stats || null);
      onComplete();
    } catch (e) { setError(String(e)); }
    finally { setRunningE(false); }
  };

  const runCooccurrence = async () => {
    setRunningC(true); setError(null); setStatsC(null);
    try {
      const r = await api.ue3EnrichCooccurrence();
      if (!r.ok) { setError(r.error || "Co-Occurrence fehlgeschlagen"); return; }
      setStatsC(r.stats || null);
      onComplete();
    } catch (e) { setError(String(e)); }
    finally { setRunningC(false); }
  };

  return (
    <Section title="DBpedia · Anreicherung" defaultOpen={false}>
      {/* Persons */}
      <p className="text-[11px] leading-relaxed mb-2" style={{ color: TEXT_MUTED }}>
        Personen: kanonische Apple-Personen aus DBpedia + Kontext-only
        rauswerfen.
      </p>
      <button onClick={runPersons} disabled={runningP || runningR}
              className="w-full px-3 py-2 text-[11px] uppercase tracking-wider transition"
              style={{
                border: `1px solid ${ACCENT}`,
                background: runningP ? PAPER_SOFT : "transparent",
                color: ACCENT,
                opacity: (runningP || runningR) ? 0.5 : 1,
              }}
              onMouseEnter={e => { if (!runningP && !runningR) { e.currentTarget.style.background = ACCENT; e.currentTarget.style.color = PAPER; } }}
              onMouseLeave={e => { if (!runningP && !runningR) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = ACCENT; } }}>
        {runningP ? "Validiert …" : "Personen validieren"}
      </button>
      {statsP && (
        <dl className="mt-2 mb-4 text-[11px] space-y-0.5" style={{ color: TEXT_MUTED }}>
          <Row label="geholt"      value={String(statsP.canonical_persons_fetched)} />
          <Row label="eingefügt"   value={String(statsP.canonical_persons_added)} />
          <Row label="ungeprüft"   value={String(statsP.unverified_persons_total)} />
          <Row label="bestätigt"   value={String(statsP.persons_confirmed)} />
          <Row label="demoted"     value={String(statsP.persons_demoted)} />
        </dl>
      )}

      {/* Products */}
      <div className="mt-3 mb-2" style={{ borderTop: `1px solid ${RULE}` }} />
      <p className="text-[11px] leading-relaxed mb-2 mt-2" style={{ color: TEXT_MUTED }}>
        Produkte: 40 + Apple-Produkte mit Vorgänger-/Nachfolger-Kette
        aus DBpedia ziehen. Schließt die Reasoning-Lücke bei Produkt-
        Chronologie-Fragen.
      </p>
      <button onClick={runProducts} disabled={runningP || runningR}
              className="w-full px-3 py-2 text-[11px] uppercase tracking-wider transition"
              style={{
                border: `1px solid ${ACCENT}`,
                background: runningR ? PAPER_SOFT : "transparent",
                color: ACCENT,
                opacity: (runningP || runningR) ? 0.5 : 1,
              }}
              onMouseEnter={e => { if (!runningP && !runningR) { e.currentTarget.style.background = ACCENT; e.currentTarget.style.color = PAPER; } }}
              onMouseLeave={e => { if (!runningP && !runningR) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = ACCENT; } }}>
        {runningR ? "Reichert an …" : "Produkte anreichern"}
      </button>
      {statsR && (
        <dl className="mt-2 text-[11px] space-y-0.5" style={{ color: TEXT_MUTED }}>
          <Row label="geholt"                value={String(statsR.products_fetched)} />
          <Row label="Produkte neu"          value={String(statsR.products_added)} />
          <Row label="Nachfolger-Triples"    value={String(statsR.successor_added)} />
          <Row label="Vorgänger-Triples"     value={String(statsR.predecessor_added)} />
        </dl>
      )}

      {/* DBpedia Cross-Edges */}
      <div className="mt-3 mb-2" style={{ borderTop: `1px solid ${RULE}` }} />
      <p className="text-[11px] leading-relaxed mb-2 mt-2" style={{ color: TEXT_MUTED }}>
        Cross-Edges: alle DBpedia-Relationen zwischen bereits bekannten
        Entitäten holen (Steve Jobs → Apple founder; Ive → iPhone
        designer; …). Verdichtet den Graphen erheblich.
      </p>
      <button onClick={runEdges} disabled={busy}
              className="w-full px-3 py-2 text-[11px] uppercase tracking-wider transition"
              style={{
                border: `1px solid ${ACCENT}`,
                background: runningE ? PAPER_SOFT : "transparent",
                color: ACCENT,
                opacity: busy ? 0.5 : 1,
              }}
              onMouseEnter={e => { if (!busy) { e.currentTarget.style.background = ACCENT; e.currentTarget.style.color = PAPER; } }}
              onMouseLeave={e => { if (!busy) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = ACCENT; } }}>
        {runningE ? "Reichert an …" : "Cross-Edges holen"}
      </button>
      {statsE && (
        <dl className="mt-2 text-[11px] space-y-0.5" style={{ color: TEXT_MUTED }}>
          <Row label="DBpedia-Treffer"   value={String(statsE.edges_fetched)} />
          <Row label="Neue Edges"        value={String(statsE.edges_upserted)} />
          {Object.entries(statsE.by_relation).slice(0, 6).map(([rel, n]) => (
            <Row key={rel} label={`· ${rel}`} value={String(n)} />
          ))}
        </dl>
      )}

      {/* Co-Occurrence */}
      <div className="mt-3 mb-2" style={{ borderTop: `1px solid ${RULE}` }} />
      <p className="text-[11px] leading-relaxed mb-2 mt-2" style={{ color: TEXT_MUTED }}>
        Co-Occurrence: Entitäten verbinden, die im gleichen Wikipedia-
        Abschnitt genannt werden (Schwelle: 2 Sektionen). Keine externen
        Daten — nutzt nur den UE1-Korpus.
      </p>
      <button onClick={runCooccurrence} disabled={busy}
              className="w-full px-3 py-2 text-[11px] uppercase tracking-wider transition"
              style={{
                border: `1px solid ${ACCENT}`,
                background: runningC ? PAPER_SOFT : "transparent",
                color: ACCENT,
                opacity: busy ? 0.5 : 1,
              }}
              onMouseEnter={e => { if (!busy) { e.currentTarget.style.background = ACCENT; e.currentTarget.style.color = PAPER; } }}
              onMouseLeave={e => { if (!busy) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = ACCENT; } }}>
        {runningC ? "Berechnet …" : "Co-Occurrence berechnen"}
      </button>
      {statsC && (
        <dl className="mt-2 text-[11px] space-y-0.5" style={{ color: TEXT_MUTED }}>
          <Row label="Paare gesamt"      value={String(statsC.pairs_total)} />
          <Row label={`≥ ${statsC.threshold} Sektionen`} value={String(statsC.pairs_above_threshold)} />
          <Row label="unter Schwelle"    value={String(statsC.pairs_below_threshold)} />
        </dl>
      )}

      {error && (
        <div className="mt-3 text-[11px] font-mono p-2 break-all"
             style={{ border: `1px solid ${ACCENT}`, color: ACCENT }}>{error}</div>
      )}
    </Section>
  );
}


// ─── Tooltip (small, restrained, no glow) ───────────────────────────────────
function NodeTooltip({ node, community, colour }: { node: FGNode; community: GraphCommunity | null; colour: string }) {
  return (
    <div className="absolute z-20 pointer-events-none bottom-6 left-1/2 -translate-x-1/2 max-w-md p-3"
         style={{ background: PAPER, border: `1px solid ${TEXT_INK}` }}>
      <div className="flex items-baseline gap-2 mb-1">
        <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: colour }} />
        <span className="font-medium text-[13px]" style={{ color: TEXT_INK }}>{node.name}</span>
        <span className="text-[10px] uppercase tracking-wider ml-auto" style={{ color: TEXT_MUTED }}>{node.type}</span>
      </div>
      <div className="text-[11px] font-mono mb-1.5" style={{ color: TEXT_MUTED }}>
        {node.mentions} Erwähnungen{community && <> · Community {community.id}</>}
      </div>
      {node.description && (
        <p className="text-[12px] leading-relaxed" style={{ color: TEXT_INK }}>{node.description}</p>
      )}
    </div>
  );
}


// ─── Small typographic helpers (Aicher: signage built on lockup grids) ─────
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between py-0.5">
      <dt>{label}</dt><dd className="font-mono" style={{ color: TEXT_INK }}>{value}</dd>
    </div>
  );
}

function Section({ title, children, defaultOpen = true }:
                 { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="px-5 py-4" style={{ borderBottom: `1px solid ${RULE}` }}>
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between text-[10px] uppercase tracking-[0.25em] mb-3 transition group"
        style={{ color: TEXT_MUTED }}
        title={open ? "einklappen" : "ausklappen"}
      >
        <span className="group-hover:text-current" style={{ color: open ? TEXT_INK : TEXT_MUTED }}>{title}</span>
        <span className="font-mono text-[14px] leading-none w-5 h-5 flex items-center justify-center transition"
              style={{
                color: open ? ACCENT : TEXT_INK,
                border: `1px solid ${open ? ACCENT : RULE}`,
              }}>
          {open ? "−" : "+"}
        </span>
      </button>
      {open && children}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[11px] uppercase tracking-wider" style={{ color: TEXT_MUTED }}>{children}</div>
  );
}


// ─── SPARQL console ────────────────────────────────────────────────────────
const EXAMPLE_QUERIES: { label: string; sparql: string }[] = [
  {
    label: "Personen mit Rolle (CEO, Founder, Designer)",
    sparql: `PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX apple: <http://uc5.butscher.cloud/apple#>
SELECT DISTINCT ?name ?role WHERE {
  VALUES ?role { apple:CEO apple:Founder apple:Designer apple:Executive apple:Engineer }
  ?p a ?role ; rdfs:label ?name .
} ORDER BY ?role ?name`,
  },
  {
    label: "Subklassen-Inferenz: alle Smartphones",
    sparql: `PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX apple: <http://uc5.butscher.cloud/apple#>
SELECT DISTINCT ?name WHERE {
  ?p rdf:type/rdfs:subClassOf* apple:Smartphone ; rdfs:label ?name .
}`,
  },
  {
    label: "Verteilung der Entitätstypen",
    sparql: `PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX apple: <http://uc5.butscher.cloud/apple#>
SELECT ?type (COUNT(?s) AS ?anzahl) WHERE {
  ?s a ?type .
  FILTER(STRSTARTS(STR(?type), "http://uc5.butscher.cloud/apple#"))
} GROUP BY ?type ORDER BY DESC(?anzahl)`,
  },
];


function SparqlConsole({ onResults, nodeIds, onCollapse, bgMain }: {
  onResults: (matched: string[]) => void;
  nodeIds: Set<string>;
  onCollapse: () => void;
  bgMain: string;
}) {
  const [nlQuery, setNlQuery] = useState("");
  const [sparql, setSparql] = useState("");
  const [translating, setTranslating] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);

  const translate = async () => {
    if (!nlQuery.trim()) return;
    setTranslating(true); setError(null); setResult(null);
    try {
      const r = await api.sparqlTranslate(nlQuery.trim());
      if (r.ok && r.sparql) {
        setSparql(r.sparql);
        // Auto-execute so the user sees the answer in one click.
        await executeWith(r.sparql);
      } else {
        setError(r.error || "Übersetzung fehlgeschlagen");
      }
    } catch (e) { setError(String(e)); }
    finally { setTranslating(false); }
  };

  const executeWith = async (querySparql: string) => {
    const s = querySparql.trim();
    if (!s) return;
    setExecuting(true); setError(null); setResult(null);
    try {
      const r = await api.sparqlExecute(s);
      if (!r.ok) { setError(r.error || "Fehler"); return; }
      setResult(r.result);
      const matched: string[] = [];
      const bindings = r.result?.results?.bindings || [];
      for (const b of bindings) {
        for (const v of Object.values(b) as any[]) {
          if (v?.type === "uri" && v.value.startsWith("http://uc5.butscher.cloud/apple#")) {
            const last = v.value.split("#")[1].toLowerCase();
            for (const id of nodeIds) {
              const idNorm = id.split(":")[1]?.replace(/\s+/g, "").toLowerCase();
              if (idNorm && (idNorm === last || last.includes(idNorm) || idNorm.includes(last))) {
                if (!matched.includes(id)) matched.push(id);
              }
            }
          }
        }
      }
      onResults(matched);
    } catch (e) { setError(String(e)); }
    finally { setExecuting(false); }
  };

  const execute = () => executeWith(sparql);

  return (
    <aside className="w-[30rem] shrink-0 flex flex-col"
           style={{ background: bgMain, borderLeft: `1px solid ${RULE}`, color: TEXT_INK }}>
      <div className="px-6 pt-6 pb-4" style={{ borderBottom: `1px solid ${RULE}` }}>
        <div className="flex items-start justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em]" style={{ color: TEXT_MUTED }}>
              UE4 · Konsole
            </div>
            <h2 className="font-medium text-base mt-1" style={{ letterSpacing: "-0.01em" }}>
              SPARQL Query
            </h2>
          </div>
          <button onClick={onCollapse}
                  className="text-[16px] leading-none flex items-center justify-center transition"
                  style={{
                    width: "26px", height: "26px",
                    border: `1px solid ${RULE}`,
                    color: TEXT_INK,
                    background: bgMain,
                  }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = ACCENT; e.currentTarget.style.color = ACCENT; }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = RULE; e.currentTarget.style.color = TEXT_INK; }}
                  title="SPARQL-Konsole einklappen">
            ›
          </button>
        </div>
      </div>

      <div className="px-6 py-5 space-y-5 overflow-y-auto flex-1">
        {/* NL → SPARQL */}
        <div>
          <Label>Frage in natürlicher Sprache</Label>
          <div className="flex gap-2 mt-1.5">
            <input
              value={nlQuery}
              onChange={e => setNlQuery(e.target.value)}
              onKeyDown={e => e.key === "Enter" && translate()}
              placeholder="Wer sind die CEOs?"
              className="flex-1 px-0 py-2 text-[13px] bg-transparent focus:outline-none"
              style={{ borderBottom: `1px solid ${RULE}`, color: TEXT_INK }}
              onFocus={e => e.target.style.borderBottomColor = ACCENT}
              onBlur={e => e.target.style.borderBottomColor = RULE}
            />
            <button onClick={translate} disabled={translating || !nlQuery.trim()}
                    className="px-3 py-1.5 text-[11px] uppercase tracking-wider transition disabled:opacity-30"
                    style={{ border: `1px solid ${TEXT_INK}`, color: TEXT_INK }}
                    onMouseEnter={e => { if (!translating && nlQuery.trim()) { e.currentTarget.style.background = TEXT_INK; e.currentTarget.style.color = PAPER; } }}
                    onMouseLeave={e => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = TEXT_INK; }}>
              {translating ? "…" : "Übersetzen"}
            </button>
          </div>
        </div>

        {/* Examples */}
        <div>
          <Label>Beispielqueries</Label>
          <div className="mt-1.5 space-y-1">
            {EXAMPLE_QUERIES.map((ex, i) => (
              <button key={i} onClick={() => setSparql(ex.sparql)}
                      className="block w-full text-left text-[12px] py-1.5 hover:opacity-100 transition"
                      style={{ color: TEXT_MUTED, opacity: 0.85 }}
                      onMouseEnter={e => e.currentTarget.style.color = ACCENT}
                      onMouseLeave={e => e.currentTarget.style.color = TEXT_MUTED}>
                <span className="font-mono mr-2" style={{ color: TEXT_INK }}>{(i + 1).toString().padStart(2, "0")}</span>
                {ex.label}
              </button>
            ))}
          </div>
        </div>

        {/* Editor */}
        <div>
          <Label>SPARQL</Label>
          <textarea
            value={sparql}
            onChange={e => setSparql(e.target.value)}
            placeholder="SELECT ?s WHERE { ?s a apple:CEO } …"
            spellCheck={false}
            className="w-full h-56 mt-1.5 px-3 py-2.5 text-[12px] font-mono leading-relaxed focus:outline-none resize-none"
            style={{ background: PAPER_SOFT, border: `1px solid ${RULE}`, color: TEXT_INK }}
            onFocus={e => e.target.style.borderColor = TEXT_INK}
            onBlur={e => e.target.style.borderColor = RULE}
          />
        </div>

        <div className="flex gap-2">
          <button onClick={execute} disabled={executing || !sparql.trim()}
                  className="flex-1 px-4 py-2 text-[11px] uppercase tracking-wider transition disabled:opacity-30"
                  style={{
                    background: TEXT_INK, color: PAPER,
                    border: `1px solid ${TEXT_INK}`,
                  }}
                  onMouseEnter={e => { if (!executing && sparql.trim()) { e.currentTarget.style.background = ACCENT; e.currentTarget.style.borderColor = ACCENT; } }}
                  onMouseLeave={e => { e.currentTarget.style.background = TEXT_INK; e.currentTarget.style.borderColor = TEXT_INK; }}>
            {executing ? "Läuft …" : "Ausführen"}
          </button>
          <button onClick={() => { setSparql(""); setResult(null); setError(null); onResults([]); }}
                  className="px-3 py-2 text-[11px] uppercase tracking-wider hover:opacity-100 transition"
                  style={{ color: TEXT_MUTED, opacity: 0.7 }}>
            Zurücksetzen
          </button>
        </div>

        {error && (
          <div className="text-[12px] font-mono p-2.5 break-all"
               style={{ border: `1px solid ${ACCENT}`, color: ACCENT }}>{error}</div>
        )}

        {executing && (
          <div className="text-[11px] uppercase tracking-wider" style={{ color: TEXT_MUTED }}>
            führt SPARQL aus …
          </div>
        )}

        {result && !executing && <ResultTable result={result} />}
      </div>
    </aside>
  );
}


function ResultTable({ result }: { result: any }) {
  const headVars: string[] = result?.head?.vars || [];
  const bindings: any[] = result?.results?.bindings || [];
  // ASK queries: GraphDB returns { boolean: true/false }
  if (typeof result?.boolean === "boolean") {
    return (
      <div className="py-2" style={{ borderTop: `1px solid ${RULE}`, borderBottom: `1px solid ${RULE}` }}>
        <span className="text-[10px] uppercase tracking-wider mr-3" style={{ color: TEXT_MUTED }}>ASK</span>
        <span className="font-mono text-[14px]"
              style={{ color: result.boolean ? TEXT_INK : ACCENT }}>
          {String(result.boolean)}
        </span>
      </div>
    );
  }
  // SELECT with zero rows — show explicit empty state so the user knows
  // the query *did* run, it just had no matches.
  if (Array.isArray(bindings) && bindings.length === 0) {
    return (
      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-[0.2em]" style={{ color: TEXT_MUTED }}>
          Ergebnis · <span className="font-mono" style={{ color: TEXT_INK }}>0</span> Zeilen
        </div>
        <div className="p-3 text-[12px] leading-relaxed"
             style={{ border: `1px solid ${RULE}`, background: PAPER_SOFT, color: TEXT_INK }}>
          Die Query lief erfolgreich, lieferte aber keine Bindings.
          Mögliche Ursachen:
          <ul className="mt-1.5 list-disc pl-5" style={{ color: TEXT_MUTED }}>
            <li>zu enger Filter (z.&nbsp;B. zusätzliches <code className="font-mono">apple:associatedWith apple:AppleInc</code>)</li>
            <li>falsche Klasse — versuche eine Oberklasse wie <code className="font-mono">apple:Person</code></li>
            <li>Reasoning greift nicht — prüfe Prefixe und <code className="font-mono">rdf:type</code></li>
          </ul>
        </div>
      </div>
    );
  }
  if (!Array.isArray(bindings)) {
    return <div className="text-[11px]" style={{ color: TEXT_MUTED }}>(kein Tabellenergebnis)</div>;
  }
  return (
    <div className="space-y-2">
      <div className="text-[10px] uppercase tracking-[0.2em]" style={{ color: TEXT_MUTED }}>
        Ergebnis · <span className="font-mono" style={{ color: TEXT_INK }}>{bindings.length}</span> Zeilen
      </div>
      {/* Tufte-style table: only horizontal hairlines, no vertical, sparse */}
      <div className="overflow-x-auto" style={{ borderTop: `1px solid ${TEXT_INK}`, borderBottom: `1px solid ${TEXT_INK}` }}>
        <table className="text-[12px] w-full" style={{ borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${RULE}` }}>
              {headVars.map(v => (
                <th key={v} className="text-left px-2 py-1.5 font-mono font-normal text-[10px] uppercase tracking-wider"
                    style={{ color: TEXT_MUTED }}>{v}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {bindings.slice(0, 100).map((row: any, i: number) => (
              <tr key={i}>
                {headVars.map(v => {
                  const val = row[v];
                  if (!val) return <td key={v} className="px-2 py-1" style={{ color: TEXT_MUTED }}>—</td>;
                  const display = val.type === "uri"
                    ? val.value.split(/[/#]/).pop()
                    : val.value;
                  return (
                    <td key={v}
                        title={val.value}
                        className={"px-2 py-1 truncate max-w-[16rem] " + (val.type === "uri" ? "font-mono" : "")}
                        style={{ color: TEXT_INK }}>
                      {display}
                    </td>
                  );
                })}
              </tr>
            ))}
            {bindings.length > 100 && (
              <tr><td colSpan={headVars.length} className="text-center text-[11px] py-1.5"
                       style={{ color: TEXT_MUTED }}>… {bindings.length - 100} weitere</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
