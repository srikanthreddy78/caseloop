import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, fmtTime } from "../api";
import type { Case, Snapshot } from "../types";

export function Insights() {
  const [snap, setSnap] = useState<Snapshot | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [cases, setCases] = useState<Case[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const requestId = useRef(0);
  const statsRequestId = useRef(0);
  const generating = useRef(false);

  const load = useCallback(async () => {
    const request = ++requestId.current;
    setError(null);
    try {
      const r = await api<{ data: Snapshot }>("/v1/insights/latest");
      if (request === requestId.current) setSnap(r.data);
    } catch (ex) {
      if (request !== requestId.current) return;
      if (ex instanceof ApiError && ex.status === 404) setSnap(null);
      else setError((ex as Error).message);
    }
  }, []);
  useEffect(() => { void load(); return () => { requestId.current++; }; }, [load]);

  const loadStats = useCallback(async () => {
    const request = ++statsRequestId.current;
    setCases(null); setStatsError(null);
    try {
      const collected: Case[] = [];
      let total = 0;
      do {
        const r = await api<{ data: Case[]; meta: { total: number } }>(`/v1/cases?limit=200&offset=${collected.length}`);
        if (request !== statsRequestId.current) return;
        collected.push(...r.data);
        total = r.meta.total;
        if (r.data.length === 0) break;
      } while (collected.length < total);
      setCases(collected);
    } catch (ex) {
      if (request === statsRequestId.current) setStatsError((ex as Error).message);
    }
  }, []);
  useEffect(() => { void loadStats(); return () => { statsRequestId.current++; }; }, [loadStats]);

  const byChannel = (keys: string[]) => cases?.filter((c) => keys.includes(c.channel)).length;
  const byState = (keys: string[]) => cases?.filter((c) => keys.includes(c.state)).length;
  const stats = [
    { label: "Email", value: byChannel(["email"]) },
    { label: "Website chat", value: byChannel(["webchat"]) },
    { label: "Phone & text", value: byChannel(["sms", "voice"]) },
    { label: "Waiting on customer", value: byState(["awaiting_customer"]) },
    { label: "Resolved", value: byState(["resolved"]) },
    { label: "Needs a person", value: byState(["escalated", "abandoned"]) },
  ];

  const generate = async () => {
    if (generating.current) return;
    generating.current = true;
    const request = ++requestId.current;
    setBusy(true); setError(null);
    try {
      const r = await api<{ data: Snapshot }>("/v1/insights/generate", { method: "POST" });
      if (request === requestId.current) { setSnap(r.data); await loadStats(); }
    } catch (ex) {
      if (request === requestId.current) setError((ex as Error).message);
    } finally {
      generating.current = false;
      if (request === requestId.current) setBusy(false);
    }
  };

  if (snap === undefined) return error ? (
    <div>
      <p className="text-sm text-clay" role="alert">{error}</p>
      <button className="btn mt-2" type="button" onClick={() => void load()}>Retry</button>
    </div>
  ) : <p className="text-ink-soft">Loading…</p>;

  return (
    <div>
      <header className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold">Feedback</h1>
          <p className="text-ink-soft text-sm mt-1">{snap ? `Last 30 days, ${snap.case_count} cases, refreshed ${fmtTime(snap.generated_at)}. Refreshes itself daily.` : "A report from the last 30 days of cases."}</p>
        </div>
        <button className="btn btn-primary" onClick={generate} disabled={busy}>{busy ? "Reading cases…" : "Refresh now"}</button>
      </header>

      {error && <p className="text-sm text-clay mb-4" role="alert">{error}</p>}

      <section className="grid grid-cols-6 gap-3 mb-10">
        {stats.map((s) => (
          <div key={s.label} className="rounded-lg border border-line bg-panel px-4 py-3">
            <div className="text-2xl font-semibold leading-none">{s.value ?? (statsError ? "—" : "…")}</div>
            <div className="text-xs text-ink-soft mt-1.5">{s.label}</div>
          </div>
        ))}
      </section>

      {statsError && (
        <div className="mb-4">
          <p className="text-sm text-clay" role="alert">Could not load case counts: {statsError}</p>
          <button className="btn mt-2" type="button" onClick={() => void loadStats()}>Retry counts</button>
        </div>
      )}

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
