import type { Source } from "../api";

interface Props {
  sources: Source[];
  trace: Record<string, unknown>;
}

export function SourcesPanel({ sources, trace }: Props) {
  return (
    <aside className="w-96 shrink-0 border-l border-slate-200 bg-white overflow-y-auto">
      <div className="p-4 border-b border-slate-100">
        <div className="text-xs uppercase tracking-wider text-slate-500 mb-1">Trace</div>
        <pre className="text-xs bg-slate-50 rounded p-2 overflow-x-auto whitespace-pre-wrap break-words">
          {JSON.stringify(trace, null, 2)}
        </pre>
      </div>
      <div className="p-4">
        <div className="text-xs uppercase tracking-wider text-slate-500 mb-2">
          Quellen ({sources.length})
        </div>
        <ol className="space-y-3">
          {sources.map((s, i) => (
            <li key={s.chunk_id ?? i} className="border border-slate-200 rounded-lg p-3 bg-slate-50">
              <div className="text-xs text-slate-500 mb-1 flex justify-between">
                <span>{s.section_path ?? "—"}</span>
                {s.distance != null && (
                  <span className="font-mono">d = {s.distance.toFixed(4)}</span>
                )}
              </div>
              <div className="text-sm text-slate-700 line-clamp-6 whitespace-pre-wrap">
                {s.text}
              </div>
            </li>
          ))}
          {sources.length === 0 && (
            <li className="text-sm text-slate-400">Noch keine Quellen.</li>
          )}
        </ol>
      </div>
    </aside>
  );
}
