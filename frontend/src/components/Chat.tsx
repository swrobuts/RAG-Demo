import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { streamQuery, type LLMProvider, type Source, type Strategy } from "../api";
import { SourcesPanel } from "./Sources";

interface Props {
  strategy: Strategy;
  llm: LLMProvider;
  ingested: boolean;
}

export function Chat({ strategy, llm, ingested }: Props) {
  const [input, setInput] = useState("");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [trace, setTrace] = useState<Record<string, unknown>>({});
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const send = () => {
    if (!input.trim() || streaming) return;
    setAnswer("");
    setSources([]);
    setTrace({});
    setError(null);
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    streamQuery(
      { query: input.trim(), strategy, llm },
      {
        onMeta: (m) => { setSources(m.sources); setTrace(m.trace); },
        onToken: (t) => setAnswer((a) => a + t),
        onDone: () => setStreaming(false),
        onError: (e) => { setError(String(e)); setStreaming(false); },
      },
      controller.signal,
    );
  };

  const stop = () => { abortRef.current?.abort(); setStreaming(false); };

  if (!ingested) {
    return (
      <div className="flex-1 grid place-items-center text-center p-8">
        <div className="max-w-md">
          <h2 className="text-xl font-semibold mb-2">Diese Strategie ist noch nicht ingested.</h2>
          <p className="text-slate-600">
            Wechsle in den Admin-Tab und starte den Ingest für <code>{strategy}</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex min-h-0">
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex-1 overflow-y-auto p-6">
          {answer ? (
            <div className="prose-chat max-w-3xl text-[15px] leading-relaxed">
              <ReactMarkdown>{answer}</ReactMarkdown>
            </div>
          ) : (
            <div className="text-slate-400 max-w-3xl">
              Stelle eine Frage zum deutschen Wikipedia-Artikel über <em>Apple</em>.
              Beispiele:
              <ul className="list-disc ml-6 mt-2 space-y-1">
                <li>Wer hat Apple gegründet und wann?</li>
                <li>Was sind die wichtigsten Produktlinien?</li>
                <li>Welche Rolle spielte Steve Jobs in der Unternehmensgeschichte?</li>
              </ul>
            </div>
          )}
          {error && (
            <div className="mt-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">
              {error}
            </div>
          )}
        </div>
        <div className="border-t border-slate-200 bg-white p-4">
          <form
            className="flex gap-2"
            onSubmit={(e) => { e.preventDefault(); send(); }}
          >
            <input
              className="flex-1 border border-slate-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Frage stellen…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={streaming}
            />
            {streaming ? (
              <button type="button" onClick={stop}
                className="px-4 py-2 rounded-lg bg-slate-200 text-slate-700 hover:bg-slate-300">
                Stop
              </button>
            ) : (
              <button type="submit"
                className="px-4 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                disabled={!input.trim()}>
                Senden
              </button>
            )}
          </form>
        </div>
      </div>
      <SourcesPanel sources={sources} trace={trace} />
    </div>
  );
}
