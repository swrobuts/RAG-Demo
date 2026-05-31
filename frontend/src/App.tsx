import { useEffect, useState } from "react";
import { Admin } from "./components/Admin";
import { Chat } from "./components/Chat";
import { Compare } from "./components/Compare";
import { Graph } from "./components/Graph";
import { api, type LLMProvider, type Strategy, type StrategyInfo } from "./api";

type Tab = "chat" | "vergleich" | "graph" | "admin";

const STRATEGIES: { id: Strategy; label: string; subtitle: string }[] = [
  { id: "ue1", label: "UE1", subtitle: "Simple RAG" },
  { id: "ue2", label: "UE2", subtitle: "+ PageIndex" },
  { id: "ue3", label: "UE3", subtitle: "+ GraphRAG" },
  { id: "ue4", label: "UE4", subtitle: "+ Ontology" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [strategy, setStrategy] = useState<Strategy>("ue1");
  const [llm, setLLM] = useState<LLMProvider>("gemini");
  const [strategies, setStrategies] = useState<Record<Strategy, StrategyInfo> | null>(null);
  const [geminiOK, setGeminiOK] = useState(true);

  useEffect(() => {
    api.health().then((h) => setGeminiOK(h.gemini_configured)).catch(() => setGeminiOK(false));
    const load = () => api.strategies().then((d) => setStrategies(d.strategies)).catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const currentInfo = strategies?.[strategy];

  return (
    <div className="h-screen flex flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="px-6 py-3 flex items-center gap-6">
          <div className="font-bold text-lg">UC5 · RAG <span className="text-slate-400 font-normal">über de.wikipedia.org/wiki/Apple</span></div>
          <nav className="ml-auto flex gap-1">
            {(["chat", "vergleich", "graph", "admin"] as Tab[]).map((t) => (
              <button key={t}
                onClick={() => setTab(t)}
                className={
                  "px-3 py-1.5 rounded text-sm capitalize " +
                  (tab === t ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100")
                }>
                {t}
              </button>
            ))}
          </nav>
        </div>
        <div className="px-6 py-2 flex items-center gap-4 border-t border-slate-100 bg-slate-50/50">
          <div className="flex gap-1" role="tablist" aria-label="Strategie">
            {STRATEGIES.map((s) => {
              const info = strategies?.[s.id];
              const disabled = info ? !info.implemented : false;
              const active = strategy === s.id;
              return (
                <button key={s.id}
                  onClick={() => !disabled && setStrategy(s.id)}
                  disabled={disabled}
                  className={
                    "px-3 py-1.5 rounded text-sm flex flex-col items-start min-w-[110px] " +
                    (active ? "bg-blue-600 text-white" :
                      disabled ? "bg-slate-100 text-slate-400 cursor-not-allowed" :
                      "bg-white border border-slate-300 hover:bg-slate-100")
                  }>
                  <span className="font-bold">{s.label}</span>
                  <span className={"text-[11px] " + (active ? "text-blue-100" : "text-slate-500")}>
                    {s.subtitle}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="h-8 w-px bg-slate-200" />
          <label className="text-sm text-slate-600 flex items-center gap-2">
            LLM:
            <select value={llm} onChange={(e) => setLLM(e.target.value as LLMProvider)}
              className="border border-slate-300 rounded px-2 py-1 text-sm bg-white">
              <option value="gemini" disabled={!geminiOK}>
                Gemini {geminiOK ? "" : "(API-Key fehlt)"}
              </option>
              <option value="local">Lokal (LM Studio)</option>
            </select>
          </label>
        </div>
      </header>
      <main className="flex-1 flex min-h-0">
        {tab === "chat" && (
          <Chat strategy={strategy} llm={llm}
                ingested={currentInfo?.ingested ?? false} />
        )}
        {tab === "vergleich" && <Compare llm={llm} strategies={strategies} />}
        {tab === "graph" && <Graph />}
        {tab === "admin" && <Admin />}
      </main>
    </div>
  );
}
