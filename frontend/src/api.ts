export type Strategy = "ue1" | "ue2" | "ue3" | "ue4";
export type LLMProvider = "gemini" | "local";

export interface Source {
  chunk_id: number | null;
  section_path: string | null;
  text: string;
  distance: number | null;
}

export interface Health {
  status: string;
  db_ok: boolean;
  gemini_configured: boolean;
  local_llm_url: string;
  wikipedia_url: string;
}

export interface StrategyInfo {
  ingested: boolean;
  implemented: boolean;
  chunk_count: number;
  last_run: null | {
    id: number;
    strategy: string;
    snapshot_id: number;
    started_at: string;
    finished_at: string | null;
    status: string;
    stats: Record<string, unknown>;
    error: string | null;
  };
}

export interface SnapshotInfo {
  snapshot: null | {
    id: number;
    url: string;
    fetched_at: string;
    revision_id: string | null;
    content_hash: string;
  };
}

const j = (r: Response) => {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
};

export interface StrategyResult {
  strategy: Strategy;
  answer: string;
  sources: Source[];
  trace: Record<string, unknown>;
  latency_ms: number;
  llm_calls: number;
  token_usage: { prompt_tokens: number; completion_tokens: number };
  skipped_llm: boolean;
}

export interface JudgeScore {
  korrektheit: number;
  vollstaendigkeit: number;
  quellenbezug: number;
  fokussiertheit: number;
  kommentar: string;
  gesamtnote: number;
}

export interface CompareResponse {
  query: string;
  llm: LLMProvider;
  results: StrategyResult[];
  evaluation: {
    scores: Record<string, JudgeScore>;
    gewinner: string;
    begruendung: string;
    judge_model: string;
  };
  total_latency_ms: number;
}

export const api = {
  health: () => fetch("/api/health").then(j) as Promise<Health>,
  snapshot: () => fetch("/api/snapshot").then(j) as Promise<SnapshotInfo>,
  strategies: () => fetch("/api/strategies").then(j) as Promise<{ strategies: Record<Strategy, StrategyInfo> }>,
  ingest: (strategy: Strategy, force = false) =>
    fetch("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, force }),
    }).then(j) as Promise<{ status: string; strategy: Strategy; run_id: number; snapshot_id: number }>,
  ingestStatus: (runId: number) => fetch(`/api/ingest/${runId}`).then(j),
  compare: (body: { query: string; strategies: Strategy[]; llm: LLMProvider; k?: number }) =>
    fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(j) as Promise<CompareResponse>,
  graph: (params?: { min_mentions?: number; types?: string; limit_entities?: number; min_edge_weight?: number }) => {
    const q = new URLSearchParams();
    if (params?.min_mentions != null) q.set("min_mentions", String(params.min_mentions));
    if (params?.types) q.set("types", params.types);
    if (params?.limit_entities != null) q.set("limit_entities", String(params.limit_entities));
    if (params?.min_edge_weight != null) q.set("min_edge_weight", String(params.min_edge_weight));
    const url = "/api/graph" + (q.toString() ? `?${q.toString()}` : "");
    return fetch(url).then(j) as Promise<GraphPayload>;
  },
  sparqlTranslate: (query: string) =>
    fetch("/api/sparql/translate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }).then(j) as Promise<{ ok: boolean; sparql?: string; error?: string }>,
  sparqlExecute: (query: string) =>
    fetch("/api/sparql", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }).then(j) as Promise<{ ok: boolean; kind?: string; result?: any; error?: string }>,
  ue4Validate: () =>
    fetch("/api/ue4/validate", { method: "POST" }).then(j) as Promise<{
      ok: boolean;
      stats?: {
        canonical_persons_fetched: number;
        canonical_persons_added: number;
        unverified_persons_total: number;
        persons_confirmed: number;
        persons_demoted: number;
      };
      error?: string;
    }>,
  ue4EnrichProducts: () =>
    fetch("/api/ue4/enrich-products", { method: "POST" }).then(j) as Promise<{
      ok: boolean;
      stats?: {
        products_fetched: number;
        products_added: number;
        successor_added: number;
        predecessor_added: number;
      };
      error?: string;
    }>,
  ue4EnrichEdges: () =>
    fetch("/api/ue4/enrich-edges", { method: "POST" }).then(j) as Promise<{
      ok: boolean;
      stats?: {
        edges_fetched: number;
        edges_upserted: number;
        by_relation: Record<string, number>;
      };
      error?: string;
    }>,
  ue3EnrichCooccurrence: () =>
    fetch("/api/ue3/enrich-cooccurrence", { method: "POST" }).then(j) as Promise<{
      ok: boolean;
      stats?: {
        pairs_total: number;
        pairs_above_threshold: number;
        pairs_below_threshold: number;
        threshold: number;
      };
      error?: string;
    }>,
  ue3SyncCanonical: () =>
    fetch("/api/ue3/sync-canonical", { method: "POST" }).then(j) as Promise<{
      ok: boolean;
      stats?: {
        canonical_fetched: number;
        pg_inserted: number;
        neo4j_inserted: number;
        skipped: number;
      };
      error?: string;
    }>,
};

export interface GraphNode {
  id: string;
  name: string;
  type: string;
  description: string;
  mentions: number;
  community_id: string | null;
  /** OWL sub-class names from GraphDB — e.g. ["CEO","Executive","Founder"]
   *  for Steve Jobs, ["Smartphone"] for iPhone. Empty if UE4 isn't
   *  available or the entity has only the broad type. */
  roles: string[];
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  weight: number;
}

export interface GraphCommunity {
  id: string;
  level: number;
  size: number;
  summary: string;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  communities: GraphCommunity[];
}

export interface StreamHandlers {
  onMeta: (meta: { sources: Source[]; trace: Record<string, unknown> }) => void;
  onToken: (text: string) => void;
  onDone: () => void;
  onError: (err: unknown) => void;
}

export async function streamQuery(
  body: { query: string; strategy: Strategy; llm: LLMProvider; k?: number },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const resp = await fetch("/api/query/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!resp.ok || !resp.body) throw new Error(`${resp.status} ${resp.statusText}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const events = buf.split("\n\n");
      buf = events.pop() ?? "";
      for (const ev of events) {
        const line = ev.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const payload = JSON.parse(line.slice(6));
        if (payload.type === "meta") handlers.onMeta(payload);
        else if (payload.type === "token") handlers.onToken(payload.text);
        else if (payload.type === "done") handlers.onDone();
      }
    }
  } catch (err) {
    handlers.onError(err);
  }
}
