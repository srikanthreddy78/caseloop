import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ago, api } from "../api";
import { StatePill } from "../components/StatePill";
import type { Case, CaseState } from "../types";

const FILTERS: { label: string; states: CaseState[] | null }[] = [
  { label: "Open", states: ["new", "gathering", "awaiting_customer"] },
  { label: "Needs a person", states: ["escalated", "abandoned"] },
  { label: "Resolved", states: ["resolved"] },
  { label: "All", states: null },
];
const PAGE_SIZE = 100;

const CHANNEL_LABEL: Record<string, string> = { email: "email", webchat: "website chat", sms: "text", voice: "phone call", api: "api" };

function waitingFor(c: Case): string {
  if (c.state === "awaiting_customer") {
    const missing = c.requirements.filter((r) => r.status === "missing").map((r) => r.label.toLowerCase());
    return missing.length ? `Customer: ${missing.join(", ")}` : "Customer reply";
  }
  if (c.state === "escalated") return c.escalation?.reason?.replace(/_/g, " ") || "A decision";
  if (c.state === "abandoned") return `No reply after ${c.followups_sent} follow-ups`;
  if (c.state === "resolved") return c.resolution?.type?.replace(/_/g, " ") || "Closed";
  return "Agent";
}

export function Cases() {
  const [params, setParams] = useSearchParams();
  const filter = FILTERS.find((f) => f.label === params.get("f")) || FILTERS[0];
  const requestedOffset = Number(params.get("offset") || 0);
  const offset = Number.isSafeInteger(requestedOffset) && requestedOffset >= 0 ? requestedOffset : 0;
  const [cases, setCases] = useState<Case[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setAttempt((a) => a + 1), 5000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    const q = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (filter.states) q.set("state", filter.states.join(","));
    api<{ data: Case[]; meta: { total: number } }>(`/v1/cases?${q}`).then((r) => {
      if (!cancelled) { setCases(r.data); setTotal(r.meta.total); }
    }).catch((ex) => { if (!cancelled) setError((ex as Error).message); });
    return () => { cancelled = true; };
  }, [filter, offset, attempt]);

  const goToOffset = (nextOffset: number) => setParams((current) => {
    const next = new URLSearchParams(current);
    if (nextOffset) next.set("offset", String(nextOffset)); else next.delete("offset");
    return next;
  });

  return (
    <div>
      <header className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold">Cases</h1>
          <p className="text-ink-soft text-sm mt-1">{total} {filter.label.toLowerCase()}{filter.states ? "" : " cases"}</p>
        </div>
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <button key={f.label} className={`btn h-8 ${f === filter ? "bg-ink text-paper border-ink" : ""}`} onClick={() => setParams(f.label === "Open" ? {} : { f: f.label })}>{f.label}</button>
          ))}
        </div>
      </header>

      {error ? (
        <div>
          <p className="text-sm text-clay" role="alert">{error}</p>
          <button className="btn mt-2" type="button" onClick={() => setAttempt((n) => n + 1)}>Retry</button>
        </div>
      ) : cases === null ? (
        <p className="text-ink-soft">Loading…</p>
      ) : cases.length === 0 ? (
        <div className="border border-dashed border-line rounded-lg p-10 text-center">
          <p className="font-medium">Nothing here yet.</p>
          <p className="text-ink-soft text-sm mt-1">Forward support email to your inbound address, add the chat widget, or open a case through the API. See Setup.</p>
        </div>
      ) : (
        <ul className="border-t border-line">
          {cases.map((c) => (
            <li key={c.id} className="border-b border-line-soft">
              <Link to={`/cases/${c.id}`} className="row-link grid grid-cols-[1fr_180px_220px_90px] gap-4 items-center px-2 py-3 -mx-2 rounded-md">
                <div className="min-w-0">
                  <div className="font-medium truncate">{c.subject || c.summary || "Untitled case"}</div>
                  <div className="text-sm text-ink-soft truncate">{c.customer.name || c.customer.email || c.customer.phone} · {c.category || "unclassified"} · {CHANNEL_LABEL[c.channel] || c.channel}</div>
                </div>
                <StatePill state={c.state} />
                <div className="text-sm text-ink-soft truncate">{waitingFor(c)}</div>
                <div className="text-sm text-ink-soft text-right">{ago(c.updated_at)}</div>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {(total > PAGE_SIZE || offset > 0) && (
        <div className="flex items-center justify-between mt-4 text-sm">
          <button className="btn" type="button" disabled={offset === 0 || (cases === null && !error)} onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
          {cases && <span className="text-ink-soft">{cases.length ? `${offset + 1}–${offset + cases.length}` : "0"} of {total}</span>}
          <button className="btn" type="button" disabled={cases === null || offset + PAGE_SIZE >= total} onClick={() => goToOffset(offset + PAGE_SIZE)}>Next</button>
        </div>
      )}
    </div>
  );
}
