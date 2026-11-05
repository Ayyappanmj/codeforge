import { useCallback, useEffect, useRef, useState } from "react";
import { api, Detail, Page, Status, TERMINAL } from "./api";

const SAMPLES: Record<string, string> = {
  python: "name = input()\nprint(f'Hello, {name}!')\n",
  javascript: "const lines = require('fs').readFileSync(0, 'utf8').trim();\nconsole.log(`Hello, ${lines}!`);\n",
};

function Badge({ status }: { status: Status }) {
  return <span className={`badge ${status.toLowerCase()}`}>{status.replace("_", " ")}</span>;
}

function Result({ d, onCancel }: { d: Detail; onCancel: () => void }) {
  const active = !TERMINAL.includes(d.status);
  return (
    <section className="card" aria-live="polite">
      <div className="row">
        <h2>Result</h2>
        <Badge status={d.status} />
        {active && <button className="secondary" onClick={onCancel}>Cancel</button>}
      </div>
      {active && <p className="muted">{d.status === "QUEUED" ? "Waiting for a worker…" : "Running in a sandbox…"}</p>}
      {d.status === "TIMED_OUT" && <p className="error">Execution exceeded the time limit and was killed.</p>}
      {d.error_type && d.status !== "TIMED_OUT" && <p className="error">Error type: {d.error_type}</p>}
      <dl className="meta">
        <dt>Exit code</dt><dd>{d.exit_code ?? "—"}</dd>
        <dt>Duration</dt><dd>{d.execution_duration_ms != null ? `${d.execution_duration_ms} ms` : "—"}</dd>
        <dt>ID</dt><dd className="mono">{d.execution_id}</dd>
      </dl>
      <div className="panels">
        <div><h3>stdout</h3><pre>{d.stdout || <span className="muted">(empty)</span>}</pre></div>
        <div><h3>stderr</h3><pre className="err">{d.stderr || <span className="muted">(empty)</span>}</pre></div>
      </div>
    </section>
  );
}

function History({ refreshKey, onSelect }: { refreshKey: number; onSelect: (id: string) => void }) {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [data, setData] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.list(page, status, "")
      .then((p) => { if (!cancelled) { setData(p); setError(""); } })
      .catch((e: Error) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [page, status, refreshKey]);

  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  return (
    <section className="card">
      <div className="row">
        <h2>History</h2>
        <select value={status} onChange={(e) => { setPage(1); setStatus(e.target.value); }} aria-label="Filter by status">
          <option value="">All statuses</option>
          {["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"].map((s) => <option key={s}>{s}</option>)}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      {loading && !data && <p className="muted">Loading…</p>}
      {data && data.items.length === 0 && <p className="muted">No executions yet. Run some code!</p>}
      {data && data.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>When</th><th>Language</th><th>Status</th><th>Exit</th><th>Duration</th></tr></thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={i.execution_id} onClick={() => onSelect(i.execution_id)} tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && onSelect(i.execution_id)}>
                  <td>{new Date(i.created_at).toLocaleString()}</td>
                  <td>{i.language}</td>
                  <td><Badge status={i.status} /></td>
                  <td>{i.exit_code ?? "—"}</td>
                  <td>{i.execution_duration_ms != null ? `${i.execution_duration_ms} ms` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row">
        <button className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
        <span className="muted">Page {page} / {pages}</span>
        <button className="secondary" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </section>
  );
}

export default function App() {
  const [languages, setLanguages] = useState<string[]>(["python", "javascript"]);
  const [language, setLanguage] = useState("python");
  const [source, setSource] = useState(SAMPLES.python);
  const [stdin, setStdin] = useState("CodeForge");
  const [current, setCurrent] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const timer = useRef<number | null>(null);

  const stopPolling = () => { if (timer.current) { window.clearTimeout(timer.current); timer.current = null; } };

  const poll: (id: string, delay?: number) => void = useCallback((id: string, delay = 500) => {
    stopPolling();
    timer.current = window.setTimeout(async () => {
      try {
        const d = await api.get(id);
        setCurrent(d);
        if (TERMINAL.includes(d.status)) { setRefreshKey((k) => k + 1); return; } // stop polling
        poll(id, Math.min(delay * 1.5, 3000));
      } catch (e) {
        setError((e as Error).message);
      }
    }, delay);
  }, []);

  useEffect(() => { api.languages().then((r) => setLanguages(r.languages)).catch(() => undefined); return stopPolling; }, []);

  const run = async () => {
    setError(""); setSubmitting(true);
    try {
      const { execution_id } = await api.submit(language, source, stdin);
      setCurrent(await api.get(execution_id));
      setRefreshKey((k) => k + 1);
      poll(execution_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const select = async (id: string) => {
    setError("");
    try {
      const d = await api.get(id);
      setCurrent(d);
      if (!TERMINAL.includes(d.status)) poll(id); else stopPolling();
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <main>
      <header>
        <h1>CodeForge</h1>
        <p className="muted">Sandboxed code execution — educational/dev use only. Do not expose publicly.</p>
      </header>
      <section className="card">
        <div className="row">
          <label>Language
            <select value={language} onChange={(e) => { setLanguage(e.target.value); setSource(SAMPLES[e.target.value] ?? ""); }}>
              {languages.map((l) => <option key={l}>{l}</option>)}
            </select>
          </label>
          <button onClick={run} disabled={submitting || !source.trim()}>{submitting ? "Submitting…" : "Run"}</button>
        </div>
        <label>Source code
          <textarea className="code" rows={12} value={source} spellCheck={false} onChange={(e) => setSource(e.target.value)} />
        </label>
        <label>Standard input (optional)
          <textarea rows={3} value={stdin} spellCheck={false} onChange={(e) => setStdin(e.target.value)} />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
      </section>
      {current ? <Result d={current} onCancel={() => api.cancel(current.execution_id).then(() => poll(current.execution_id, 100)).catch((e: Error) => setError(e.message))} />
        : <section className="card"><p className="muted">Run some code to see results here.</p></section>}
      <History refreshKey={refreshKey} onSelect={select} />
    </main>
  );
}
