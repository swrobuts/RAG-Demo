import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  api,
  type CompareResponse,
  type LLMProvider,
  type Strategy,
  type StrategyInfo,
} from "../api";

const STRATEGIES: { id: Strategy; label: string; subtitle: string }[] = [
  { id: "ue1", label: "UE1", subtitle: "Simple RAG" },
  { id: "ue2", label: "UE2", subtitle: "+ PageIndex" },
  { id: "ue3", label: "UE3", subtitle: "+ GraphRAG" },
  { id: "ue4", label: "UE4", subtitle: "+ Ontology" },
];

const CRITERIA: { key: keyof CriteriaScores; label: string }[] = [
  { key: "korrektheit", label: "Korrektheit" },
  { key: "vollstaendigkeit", label: "Vollständigkeit" },
  { key: "quellenbezug", label: "Quellenbezug" },
  { key: "fokussiertheit", label: "Fokussiertheit" },
];

type CriteriaScores = {
  korrektheit: number;
  vollstaendigkeit: number;
  quellenbezug: number;
  fokussiertheit: number;
};

interface Props {
  llm: LLMProvider;
  strategies: Record<Strategy, StrategyInfo> | null;
}

export function Compare({ llm, strategies }: Props) {
  const [input, setInput] = useState("");
  const [selected, setSelected] = useState<Strategy[]>(["ue1", "ue2"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CompareResponse | null>(null);

  const toggleStrategy = (s: Strategy) => {
    setSelected((cur) =>
      cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s],
    );
  };

  const run = async () => {
    if (!input.trim() || selected.length === 0 || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.compare({ query: input.trim(), strategies: selected, llm });
      setResult(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-6">
      {/* Wide container so all 4 strategy columns fit comfortably side-by-
          side without the 4th getting clipped. ~1600px target. */}
      <div className="max-w-[1600px] mx-auto space-y-6">
        <section className="bg-white border border-slate-200 rounded-lg p-5">
          <h3 className="font-semibold mb-3">Vergleich</h3>
          <p className="text-sm text-slate-600 mb-4">
            Stellt deine Frage parallel an alle ausgewählten Strategien und lässt
            Gemini als Schiedsrichter die Antworten bewerten (deutsche
            Schulnoten 1–5).
          </p>
          <form
            onSubmit={(e) => { e.preventDefault(); run(); }}
            className="space-y-3"
          >
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Frage eingeben…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={busy}
            />
            <div className="flex items-center gap-4">
              <span className="text-sm text-slate-600">Strategien:</span>
              {STRATEGIES.map((s) => {
                const info = strategies?.[s.id];
                const ready = !!info?.implemented && !!info?.ingested;
                const checked = selected.includes(s.id);
                return (
                  <label key={s.id} className={"flex items-center gap-1.5 text-sm " + (ready ? "" : "opacity-50")}>
                    <input
                      type="checkbox"
                      checked={checked && ready}
                      onChange={() => ready && toggleStrategy(s.id)}
                      disabled={!ready || busy}
                    />
                    <span className="font-bold">{s.label}</span>
                    <span className="text-slate-500 text-xs">{s.subtitle}</span>
                  </label>
                );
              })}
              <button
                type="submit"
                disabled={!input.trim() || selected.length === 0 || busy}
                className="ml-auto px-4 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed"
              >
                {busy ? "Läuft…" : "Vergleichen"}
              </button>
            </div>
          </form>
        </section>

        {error && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3">
            {error}
          </div>
        )}

        {busy && (
          <div className="bg-white border border-slate-200 rounded-lg p-5 text-sm text-slate-600">
            Alle Strategien antworten parallel, Gemini bewertet die Ergebnisse. Das dauert ca. 10–20 s.
          </div>
        )}

        {result && <ResultsTable data={result} />}
      </div>
    </div>
  );
}


function ResultsTable({ data }: { data: CompareResponse }) {
  const winnerKey = data.evaluation.gewinner;
  const judgeModel = data.evaluation.judge_model;

  return (
    <section className="bg-white border border-slate-200 rounded-lg p-5 space-y-4">
      <div className="flex items-baseline gap-3 flex-wrap">
        <h3 className="font-semibold">Ergebnis</h3>
        <span className="text-xs text-slate-500">
          Gesamtlaufzeit {Math.round(data.total_latency_ms)} ms · Schiedsrichter: {judgeModel}
        </span>
      </div>

      {/* Winner banner */}
      <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4">
        <div className="text-xs uppercase tracking-wider text-emerald-700 mb-1">
          {winnerKey === "tie" ? "Gleichstand" : "Gewinner"}
        </div>
        <div className="font-bold text-emerald-900 text-lg">
          {winnerKey === "tie" ? "—" : winnerKey.toUpperCase()}
        </div>
        <div className="text-sm text-emerald-900 mt-1">
          {data.evaluation.begruendung}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm" style={{ tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th className="text-left text-slate-500 font-medium w-40 align-bottom py-2 pr-3 border-b border-slate-200">
                Kriterium
              </th>
              {data.results.map((r) => (
                <th
                  key={r.strategy}
                  className={
                    "text-left py-2 px-3 border-b border-slate-200 align-bottom " +
                    (r.strategy === winnerKey ? "bg-emerald-50" : "")
                  }
                >
                  <div className="font-bold">{r.strategy.toUpperCase()}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* Antwort row */}
            <Row label="Antwort">
              {data.results.map((r) => (
                <td key={r.strategy}
                    className={"py-3 px-3 align-top border-b border-slate-100 " + bgIf(r.strategy === winnerKey)}>
                  <div className="prose-chat max-h-64 overflow-y-auto text-[13px] leading-snug">
                    <ReactMarkdown>{r.answer}</ReactMarkdown>
                  </div>
                  {r.skipped_llm && (
                    <div className="mt-1 text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5 inline-block">
                      keine Quellen — Antwort entfällt
                    </div>
                  )}
                </td>
              ))}
            </Row>

            {/* Quellen */}
            <Row label="Quellen">
              {data.results.map((r) => (
                <td key={r.strategy}
                    className={"py-3 px-3 align-top border-b border-slate-100 text-xs " + bgIf(r.strategy === winnerKey)}>
                  <div className="font-mono text-slate-500">{r.sources.length} Chunks</div>
                  <ul className="mt-1 space-y-0.5">
                    {uniqueSections(r.sources).slice(0, 8).map((p, i) => (
                      <li key={i} className="text-slate-700 truncate" title={p}>{p}</li>
                    ))}
                    {uniqueSections(r.sources).length > 8 && (
                      <li className="text-slate-400">… +{uniqueSections(r.sources).length - 8}</li>
                    )}
                  </ul>
                </td>
              ))}
            </Row>

            {/* Metriken */}
            <MetricRow label="LLM-Calls" data={data} value={(r) => r.llm_calls} winnerKey={winnerKey} />
            <MetricRow label="Latenz (ms)" data={data} value={(r) => Math.round(r.latency_ms)} winnerKey={winnerKey} />
            <MetricRow label="Prompt-Tokens" data={data} value={(r) => r.token_usage.prompt_tokens || 0} winnerKey={winnerKey} />
            <MetricRow label="Completion-Tokens" data={data} value={(r) => r.token_usage.completion_tokens || 0} winnerKey={winnerKey} />

            {/* Bewertung */}
            <tr>
              <td colSpan={data.results.length + 1} className="pt-4 pb-1 text-xs uppercase tracking-wider text-slate-500">
                Bewertung
              </td>
            </tr>
            {CRITERIA.map((c) => (
              <Row key={c.key} label={c.label}>
                {data.results.map((r) => {
                  const sc = data.evaluation.scores[r.strategy];
                  const value = sc ? (sc as unknown as CriteriaScores)[c.key] : 3;
                  return (
                    <td key={r.strategy}
                        className={"py-2 px-3 border-b border-slate-100 " + bgIf(r.strategy === winnerKey)}>
                      <GradeBadge grade={value} />
                    </td>
                  );
                })}
              </Row>
            ))}

            {/* Kommentar */}
            <Row label="Kommentar">
              {data.results.map((r) => {
                const sc = data.evaluation.scores[r.strategy];
                return (
                  <td key={r.strategy}
                      className={"py-2 px-3 align-top text-xs border-b border-slate-100 " + bgIf(r.strategy === winnerKey)}>
                    {sc?.kommentar || "—"}
                  </td>
                );
              })}
            </Row>

            {/* Gesamtnote */}
            <Row label="Gesamtnote" emphasis>
              {data.results.map((r) => {
                const sc = data.evaluation.scores[r.strategy];
                return (
                  <td key={r.strategy}
                      className={"py-2 px-3 " + bgIf(r.strategy === winnerKey)}>
                    <span className="font-bold text-lg">
                      <GradeBadge grade={sc?.gesamtnote ?? 3} large />
                    </span>
                  </td>
                );
              })}
            </Row>
          </tbody>
        </table>
      </div>
    </section>
  );
}


function Row({ label, children, emphasis }: { label: string; children: React.ReactNode; emphasis?: boolean }) {
  return (
    <tr>
      <td className={"py-2 pr-3 text-slate-600 align-top " + (emphasis ? "font-semibold text-slate-900" : "")}>
        {label}
      </td>
      {children}
    </tr>
  );
}

function MetricRow({
  label, data, value, winnerKey,
}: {
  label: string;
  data: CompareResponse;
  value: (r: CompareResponse["results"][number]) => number;
  winnerKey: string;
}) {
  return (
    <Row label={label}>
      {data.results.map((r) => (
        <td key={r.strategy}
            className={"py-2 px-3 font-mono text-sm border-b border-slate-100 " + bgIf(r.strategy === winnerKey)}>
          {value(r)}
        </td>
      ))}
    </Row>
  );
}

function GradeBadge({ grade, large }: { grade: number; large?: boolean }) {
  const rounded = Math.round(grade * 10) / 10;
  const color =
    grade <= 1.5 ? "bg-emerald-100 text-emerald-800 border-emerald-300" :
    grade <= 2.5 ? "bg-lime-100 text-lime-800 border-lime-300" :
    grade <= 3.5 ? "bg-amber-100 text-amber-800 border-amber-300" :
    grade <= 4.5 ? "bg-orange-100 text-orange-800 border-orange-300" :
                   "bg-rose-100 text-rose-800 border-rose-300";
  return (
    <span className={
      "inline-flex items-center justify-center rounded border font-bold " + color +
      (large ? " text-base px-3 py-1 min-w-[2.5rem]" : " text-xs px-2 py-0.5 min-w-[2rem]")
    }>
      {Number.isInteger(rounded) ? rounded : rounded.toFixed(1)}
    </span>
  );
}

function bgIf(active: boolean): string {
  return active ? "bg-emerald-50/40" : "";
}

function uniqueSections(sources: { section_path: string | null }[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of sources) {
    const p = s.section_path || "?";
    if (!seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out;
}
