import { useEffect, useState } from "react";
import { api, type SnapshotInfo, type Strategy, type StrategyInfo } from "../api";

const STRATS: Strategy[] = ["ue1", "ue2", "ue3", "ue4"];

export function Admin() {
  const [snapshot, setSnapshot] = useState<SnapshotInfo["snapshot"] | null>(null);
  const [strategies, setStrategies] = useState<Record<Strategy, StrategyInfo> | null>(null);
  const [busy, setBusy] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const s = await api.snapshot();
      setSnapshot(s.snapshot);
      const st = await api.strategies();
      setStrategies(st.strategies);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, []);

  const start = async (strategy: Strategy, force: boolean) => {
    setError(null);
    setBusy(strategy);
    try {
      await api.ingest(strategy, force);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="max-w-3xl mx-auto space-y-6">
        <section className="bg-white border border-slate-200 rounded-lg p-5">
          <h3 className="font-semibold mb-3">Aktueller Snapshot</h3>
          {snapshot ? (
            <dl className="text-sm grid grid-cols-[160px,1fr] gap-y-1">
              <dt className="text-slate-500">URL</dt>
              <dd className="break-all">{snapshot.url}</dd>
              <dt className="text-slate-500">Fetched at</dt>
              <dd>{new Date(snapshot.fetched_at).toLocaleString()}</dd>
              <dt className="text-slate-500">Revision</dt>
              <dd>{snapshot.revision_id ?? "—"}</dd>
              <dt className="text-slate-500">Hash</dt>
              <dd className="font-mono text-xs">{snapshot.content_hash.slice(0, 16)}…</dd>
            </dl>
          ) : (
            <p className="text-slate-500 text-sm">Kein Snapshot vorhanden. Starte einen Ingest.</p>
          )}
        </section>

        <section className="bg-white border border-slate-200 rounded-lg p-5">
          <h3 className="font-semibold mb-3">Strategien</h3>
          <div className="space-y-3">
            {STRATS.map((s) => {
              const info = strategies?.[s];
              return (
                <div key={s} className="border border-slate-100 rounded p-3 flex items-center gap-3">
                  <div className="w-12 font-mono text-sm font-bold uppercase">{s}</div>
                  <div className="flex-1 text-sm">
                    {info ? (
                      <>
                        <span
                          className={
                            "inline-block px-2 py-0.5 rounded text-xs mr-2 " +
                            (info.ingested ? "bg-green-100 text-green-800" : "bg-slate-100 text-slate-600")
                          }
                        >
                          {info.implemented ? (info.ingested ? "ingested" : "leer") : "nicht implementiert"}
                        </span>
                        {info.implemented && (
                          <span className="text-slate-500">{info.chunk_count} Chunks</span>
                        )}
                        {info.last_run && (
                          <span className="text-slate-400 ml-2 text-xs">
                            letzter Lauf: {info.last_run.status} ·{" "}
                            {new Date(info.last_run.started_at).toLocaleString()}
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="text-slate-400">…</span>
                    )}
                  </div>
                  <button
                    onClick={() => start(s, false)}
                    disabled={!info?.implemented || busy === s}
                    className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed"
                  >
                    {busy === s ? "Starte…" : "Ingest"}
                  </button>
                  <button
                    onClick={() => start(s, true)}
                    disabled={!info?.implemented || busy === s}
                    className="px-3 py-1.5 text-sm rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
                  >
                    Force
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        {error && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3">{error}</div>
        )}
      </div>
    </div>
  );
}
