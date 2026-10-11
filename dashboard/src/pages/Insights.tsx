import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, fmtTime } from "../api";
import type { Snapshot } from "../types";

export function Insights() {
  const [snap, setSnap] = useState<Snapshot | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);

  const load = () => api<{ data: Snapshot }>("/v1/insights/latest").then((r) => setSnap(r.data)).catch((e) => { if (e instanceof ApiError && e.status === 404) setSnap(null); else throw e; });
  useEffect(() => { load(); }, []);

  const generate = async () => {
    setBusy(true);
    try { const r = await api<{ data: Snapshot }>("/v1/insights/generate", { method: "POST" }); setSnap(r.data); }
    finally { setBusy(false); }
  };

  if (snap === undefined) return <p className="text-ink-soft">Loading…</p>;

  return (
    <div>
      <header className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold">Why customers write in</h1>
          <p className="text-ink-soft text-sm mt-1">{snap ? `Last 30 days, ${snap.case_count} cases, refreshed ${fmtTime(snap.generated_at)}. Refreshes itself daily.` : "A report from the last 30 days of cases."}</p>
        </div>
        <button className="btn btn-primary" onClick={generate} disabled={busy}>{busy ? "Reading cases…" : "Refresh now"}</button>
      </header>

      {!snap ? (
        <div className="border border-dashed border-line rounded-lg p-10 text-center">
          <p className="font-medium">No report yet.</p>
          <p className="text-ink-soft text-sm mt-1">Once a few cases exist, refresh to see themes, causes, and fixes.</p>
        </div>
      ) : (
        <div className="grid grid-cols-[1fr_300px] gap-12">
          <section>
            <p className="text-[28px] leading-snug font-medium mb-10 max-w-[28ch]">{snap.report.headline}</p>
            <ol className="flex flex-col gap-8">
              {snap.report.themes.map((t) => (
                <li key={t.name}>
                  <div className="flex items-baseline justify-between mb-1.5">
                    <h2 className="font-semibold">{t.name}</h2>
                    <span className="text-sm text-ink-soft">{t.share_pct}% of cases</span>
                  </div>
                  <div className="h-1.5 bg-line-soft rounded-full overflow-hidden mb-3"><div className="bar h-full bg-moss rounded-full" style={{ width: `${Math.min(100, t.share_pct)}%` }} /></div>
                  <div className="grid grid-cols-2 gap-6 text-sm">
                    <div>
                      <div className="text-ink-soft mb-1">Likely causes</div>
                      <ul className="list-disc pl-4 flex flex-col gap-0.5">{t.root_causes.map((x, i) => <li key={i}>{x}</li>)}</ul>
                    </div>
                    <div>
                      <div className="text-ink-soft mb-1">What to try</div>
                      <ul className="list-disc pl-4 flex flex-col gap-0.5">{t.suggested_fixes.map((x, i) => <li key={i}>{x}</li>)}</ul>
                    </div>
                  </div>
                  {t.example_case_ids.length > 0 && (
                    <div className="text-xs text-ink-soft mt-2">Examples: {t.example_case_ids.slice(0, 3).map((id) => <Link key={id} className="underline mr-2" to={`/cases/${id}`}>{id.slice(0, 8)}</Link>)}</div>
                  )}
                </li>
              ))}
            </ol>
          </section>
          <aside className="flex flex-col gap-6 text-sm">
            <div><h3 className="font-semibold mb-1">Mood</h3><p>{snap.report.sentiment_trend}</p></div>
            <div>
              <h3 className="font-semibold mb-1">New this week</h3>
              {snap.report.emerging_issues.length ? <ul className="list-disc pl-4">{snap.report.emerging_issues.map((x, i) => <li key={i}>{x}</li>)}</ul> : <p className="text-ink-soft">Nothing new.</p>}
            </div>
            <div>
              <h3 className="font-semibold mb-1">Why cases needed a person</h3>
              {snap.report.top_escalation_reasons.length ? <ul className="list-disc pl-4">{snap.report.top_escalation_reasons.map((x, i) => <li key={i}>{x.replace(/_/g, " ")}</li>)}</ul> : <p className="text-ink-soft">None escalated.</p>}
            </div>
            <p className="text-xs text-ink-soft">Written by {snap.model === "openai" ? "the model" : "simple counting rules"} from case summaries, not raw messages.</p>
          </aside>
        </div>
      )}
    </div>
  );
}
