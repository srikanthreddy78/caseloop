import { useEffect, useState } from "react";
import { ago, api } from "../api";
import type { Job } from "../types";

const trim = (s: string | null, n = 240) => {
  if (!s) return "";
  const last = s.trim().split("\n").filter(Boolean).pop() || "";
  return last.length > n ? `…${last.slice(-n)}` : last;
};

export function Queue() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api<{ data: Job[] }>("/v1/jobs?status=failed&limit=50").then((r) => setJobs(r.data)).catch((e) => setMsg((e as Error).message));
  useEffect(() => { load(); }, []);

  const retry = async (id: string) => {
    setBusy(id); setMsg(null);
    try { await api(`/v1/jobs/${id}/retry`, { method: "POST" }); await load(); }
    catch (e) { setMsg((e as Error).message); }
    finally { setBusy(null); }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-sm font-medium">Queue</h2>
        <p className="text-sm text-ink-soft">Background work that gave up after three attempts. Retry puts it back on the queue right away.</p>
      </div>
      {jobs === null ? (
        <p className="text-sm text-ink-soft">Loading…</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm text-ink-soft">Nothing has failed.</p>
      ) : (
        <ul className="border-t border-line">
          {jobs.map((j) => (
            <li key={j.id} className="border-b border-line-soft py-3 grid grid-cols-[150px_1fr_auto] gap-4 items-start">
              <div className="min-w-0">
                <div className="text-sm font-medium">{j.kind.replace(/_/g, " ")}</div>
                <div className="text-xs text-ink-soft">{j.attempts} attempts · {ago(j.updated_at)}</div>
              </div>
              <code className="text-xs text-clay break-all leading-relaxed" title={j.last_error || undefined}>{trim(j.last_error) || "no error recorded"}</code>
              <button type="button" className="btn h-8" disabled={busy === j.id} onClick={() => retry(j.id)}>{busy === j.id ? "Retrying…" : "Retry"}</button>
            </li>
          ))}
        </ul>
      )}
      {msg && <p className="text-clay text-sm">{msg}</p>}
    </div>
  );
}
