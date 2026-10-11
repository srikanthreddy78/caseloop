import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, fmtTime } from "../api";
import { StatePill } from "../components/StatePill";
import type { CaseDetail as CaseDetailT, LookupSummary, TimelineItem } from "../types";
import { OrderCard } from "../components/OrderCard";
import { Attachments } from "../components/Attachments";

const EVENT_COPY: Record<string, (d: Record<string, unknown>) => string> = {
  case_created: (d) => `Case opened over ${d.channel}`,
  order_lookup: (d) => `Checked orders in ${((d.sources as string[] | undefined) || []).join(", ") || "no connected source"}: ${d.found} found`,
  case_assessed: (d) => `Agent read it: ${d.category}, ${d.sentiment}, urgency ${d.urgency}`,
  state_changed: (d) => `Moved from ${String(d.from).replace(/_/g, " ")} to ${String(d.to).replace(/_/g, " ")}${d.reason ? ` (${String(d.reason).replace(/_/g, " ")})` : ""}`,
  followup_scheduled: (d) => `Follow-up in ${d.hours} h: ${d.reason}`,
  escalation_notified: (d) => `Escalation email sent to ${d.to}`,
  call_started: () => "Agent placed a phone call",
  call_ended: (d) => `Call ended (${d.status}, ${d.duration_seconds ?? 0}s)`,
  agent_error: (d) => `Agent error: ${d.error}`,
  delivery_failed: (d) => `Could not deliver on ${d.channel}: ${d.error}`,
};

export function CaseDetail() {
  const { id } = useParams();
  const [c, setCase] = useState<CaseDetailT | null>(null);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [reply, setReply] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [orders, setOrders] = useState<LookupSummary | null>(null);

  const load = useCallback(async () => {
    const [a, b] = await Promise.all([
      api<{ data: CaseDetailT }>(`/v1/cases/${id}`),
      api<{ data: TimelineItem[] }>(`/v1/cases/${id}/timeline`),
    ]);
    setCase(a.data); setTimeline(b.data);
    api<{ data: LookupSummary }>(`/v1/cases/${id}/orders`).then((r) => setOrders(r.data)).catch(() => setOrders(null));
  }, [id]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  if (!c) return <p className="text-ink-soft">Loading…</p>;

  const act = async (path: string, body: unknown) => {
    setBusy(true); setError(null);
    try { await api(path, { method: "POST", body: JSON.stringify(body) }); await load(); return true; }
    catch (ex) { setError((ex as Error).message); await load(); return false; }
    finally { setBusy(false); }
  };
  const missing = c.requirements.filter((r) => r.status === "missing");
  const open = !["resolved", "escalated", "abandoned"].includes(c.state);

  return (
    <div>
      <Link to="/cases" className="text-sm text-ink-soft hover:text-ink">← Cases</Link>
      <header className="mt-2 mb-6 flex items-start justify-between gap-6">
        <div>
          <h1 className="text-2xl font-semibold leading-tight">{c.subject || "Untitled case"}</h1>
          <p className="text-ink-soft mt-1">{c.customer.name || "Customer"} · {c.customer.email || c.customer.phone} · over {c.channel}</p>
        </div>
        <StatePill state={c.state} />
      </header>

      <div className="grid grid-cols-[1fr_320px] gap-10">
        <section>
          {c.summary && <p className="mb-6 text-[15px] leading-relaxed border-l-2 border-moss pl-4">{c.summary}</p>}
          <ol className="flex flex-col gap-3">
            {timeline.filter((t) => t.kind === "message" || showTools || (t.type && t.type !== "tool_called" && t.type !== "message_sent" && t.type !== "message_received")).map((t, i) => (
              t.kind === "message" ? (
                <li key={i} className={`rounded-lg border px-4 py-3 ${t.direction === "inbound" ? "border-line bg-panel" : t.direction === "internal" ? "border-dashed border-line bg-paper" : t.sender === "human" ? "border-slate-soft bg-slate-soft/50" : "border-moss-soft bg-moss-soft/50"}`}>
                  <div className="flex justify-between text-xs text-ink-soft mb-1">
                    <span>{t.direction === "inbound" ? c.customer.name || "Customer" : t.direction === "internal" ? "Internal note" : t.sender === "human" ? "You" : "Agent"} · {t.channel}{t.meta?.delivery === "failed" && <span className="ml-2 text-clay font-medium">not delivered</span>}</span>
                    <span>{fmtTime(t.at)}</span>
                  </div>
                  <div className="whitespace-pre-wrap text-[15px]">{t.text}</div>
                  {t.meta?.delivery === "failed" && t.id && (
                    <div className="mt-2 flex items-center gap-3 text-xs">
                      <button type="button" className="btn" disabled={busy} onClick={() => act(`/v1/cases/${c.id}/messages/${t.id}/resend`, {})}>Resend</button>
                      {typeof t.meta.error === "string" && <span className="text-clay">{t.meta.error}</span>}
                    </div>
                  )}
                  {t.attachments && t.attachments.length > 0 && <Attachments caseId={c.id} items={t.attachments} />}
                </li>
              ) : (
                <li key={i} className="text-sm text-ink-soft px-1 flex justify-between">
                  <span>{(EVENT_COPY[t.type!] || ((d) => `${t.type}${d.tool ? `: ${d.tool}` : ""}`))(t.data || {})}</span>
                  <span>{fmtTime(t.at)}</span>
                </li>
              )
            ))}
          </ol>
          <button className="mt-4 text-xs text-ink-soft hover:text-ink" onClick={() => setShowTools(!showTools)}>{showTools ? "Hide" : "Show"} every agent step</button>

          {error && <p className="mt-4 text-sm text-clay">{error}</p>}
          <form className="mt-8 border-t border-line pt-6" onSubmit={async (e) => { e.preventDefault(); if (!reply.trim()) return; if (await act(`/v1/cases/${c.id}/messages`, { text: reply })) setReply(""); }}>
            <label className="block text-sm font-medium mb-1" htmlFor="reply">Reply as a person</label>
            <p className="text-sm text-ink-soft mb-2">Sent on the same channel the customer used. The agent keeps working unless you resolve or take the case.</p>
            <textarea id="reply" className="field min-h-24" value={reply} onChange={(e) => setReply(e.target.value)} />
            <div className="flex gap-2 mt-2">
              <button className="btn btn-primary" disabled={busy || !reply.trim()}>Send reply</button>
            </div>
          </form>
        </section>

        <aside className="flex flex-col gap-6">
          <div>
            <h2 className="text-sm font-semibold mb-2">What the agent needs</h2>
            {c.requirements.length === 0 ? <p className="text-sm text-ink-soft">Nothing identified yet.</p> : (
              <ul className="flex flex-col gap-1.5">
                {c.requirements.map((r) => (
                  <li key={r.key} className="flex items-start gap-2 text-sm">
                    <span className={`mt-0.5 inline-grid place-items-center w-4 h-4 rounded-sm border text-[10px] ${r.status === "provided" ? "bg-moss border-moss text-white" : r.status === "not_applicable" ? "border-line text-ink-soft" : "border-amber"}`}>{r.status === "provided" ? "✓" : r.status === "not_applicable" ? "–" : ""}</span>
                    <span>
                      <span className={r.status === "provided" ? "" : "font-medium"}>{r.label}</span>
                      {r.status === "provided" && r.value != null && <span className="text-ink-soft"> · {String(r.value)}</span>}
                      {r.status === "missing" && r.asked_count > 0 && <span className="text-ink-soft"> · asked {r.asked_count}×</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <h2 className="text-sm font-semibold mb-2">Orders found</h2>
            {orders === null ? <p className="text-sm text-ink-soft">Looking…</p> : orders.no_order_system_connected ? (
              <p className="text-sm text-ink-soft">No order source connected. Add one in Setup so the agent can check orders itself.</p>
            ) : orders.found.length === 0 ? (
              <p className="text-sm text-ink-soft">Nothing matched in {orders.sources_checked.join(", ")}.</p>
            ) : (
              <div className="flex flex-col gap-2">{orders.found.map((o, i) => <OrderCard key={i} o={o} />)}</div>
            )}
            {orders?.errors.map((e, i) => <p key={i} className="text-xs text-clay mt-1">{e.source}: {e.error}</p>)}
          </div>

          <dl className="text-sm grid grid-cols-[110px_1fr] gap-y-1.5">
            <dt className="text-ink-soft">Wants</dt><dd>{c.remedy_requested || "—"}</dd>
            <dt className="text-ink-soft">Mood</dt><dd>{c.sentiment || "—"}{c.urgency ? `, ${c.urgency} urgency` : ""}</dd>
            <dt className="text-ink-soft">Follow-ups</dt><dd>{c.followups_sent} sent</dd>
            {c.external_ref && <><dt className="text-ink-soft">Your ref</dt><dd>{c.external_ref}</dd></>}
          </dl>

          {c.resolution && (
            <div className="rounded-lg bg-moss-soft p-3 text-sm">
              <div className="font-medium">Resolved: {c.resolution.type?.replace(/_/g, " ")}</div>
              <div className="text-ink-soft mt-1">{c.resolution.notes}</div>
            </div>
          )}
          {c.escalation && c.state !== "resolved" && (
            <div className="rounded-lg bg-clay-soft p-3 text-sm">
              <div className="font-medium text-clay">Why it needs you: {c.escalation.reason?.replace(/_/g, " ")}</div>
              <div className="mt-1 whitespace-pre-wrap">{c.escalation.summary}</div>
            </div>
          )}

          <div className="flex flex-col gap-2">
            {c.state !== "resolved" && (
              <button className="btn" disabled={busy} onClick={() => act(`/v1/cases/${c.id}/resolve`, { resolution_type: missing.length ? "other" : "information_provided", notes: "Closed from the dashboard" })}>Mark resolved</button>
            )}
            {open && (
              <>
                <button className="btn btn-danger" disabled={busy} onClick={() => act(`/v1/cases/${c.id}/escalate`, { reason: "taken_by_human", summary_for_human: "Taken over from the dashboard" })}>Take this case</button>
                <button className="btn" disabled={busy} onClick={() => act(`/v1/cases/${c.id}/run-turn`, {})}>Ask the agent to look again</button>
              </>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
