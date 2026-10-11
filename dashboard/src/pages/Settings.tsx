import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import type { Business, ShellContext } from "../types";
import { OrderSources } from "../components/OrderSources";
import { Queue } from "../components/Queue";
import { Team } from "../components/Team";

const escapeAttribute = (value: string) => value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function Settings() {
  const { business, setBusiness, member } = useOutletContext<ShellContext>();
  const [form, setForm] = useState({
    context_text: business.context_text,
    escalation_email: business.escalation_email,
    followup_schedule_hours: business.followup_schedule_hours.join(", "),
    max_followups: business.max_followups,
    outbound_webhook_url: business.outbound_webhook_url || "",
    allowed_origins: (business.allowed_origins || []).join("\n"),
    slack_webhook_url: business.slack_webhook_url || "",
  });
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const origin = window.location.origin;
  const snippet = `<script src="${origin}/widget.js" data-key="${escapeAttribute(business.widget_public_key)}" data-name="${escapeAttribute(business.name)}"></script>`;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const hours = form.followup_schedule_hours.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => n > 0);
    const origins = form.allowed_origins.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    try {
      const r = await api<{ data: Business }>("/v1/businesses/me", {
        method: "PATCH",
        body: JSON.stringify({ context_text: form.context_text, escalation_email: form.escalation_email, followup_schedule_hours: hours, max_followups: Number(form.max_followups), outbound_webhook_url: form.outbound_webhook_url || null, allowed_origins: origins, slack_webhook_url: form.slack_webhook_url || null }),
      });
      setBusiness(r.data); setSaved(true); setTimeout(() => setSaved(false), 2000);
    } catch (ex) { setError((ex as Error).message); }
  };

  return (
    <div className="grid grid-cols-[1fr_340px] gap-12">
      <div className="flex flex-col gap-6">
      <form onSubmit={save} className="flex flex-col gap-6">
        <header>
          <h1 className="text-2xl font-semibold">Setup</h1>
          <p className="text-ink-soft text-sm mt-1">What the agent is allowed to promise, how hard it chases, and who it hands off to.</p>
        </header>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="ctx">Policy the agent follows</label>
          <p className="text-sm text-ink-soft mb-2">Plain language. Refund windows, what needs photos, what it must never promise, when to hand off.</p>
          <textarea id="ctx" className="field min-h-56" value={form.context_text} onChange={(e) => setForm({ ...form, context_text: e.target.value })} />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="sched">Follow-up after (hours)</label>
            <input id="sched" className="field" value={form.followup_schedule_hours} onChange={(e) => setForm({ ...form, followup_schedule_hours: e.target.value })} />
            <p className="text-xs text-ink-soft mt-1">First nudge, second, third… the last one repeats.</p>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="max">Give up after</label>
            <input id="max" type="number" min={0} max={10} className="field" value={form.max_followups} onChange={(e) => setForm({ ...form, max_followups: Number(e.target.value) })} />
            <p className="text-xs text-ink-soft mt-1">Nudges before the case is handed to you.</p>
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="esc">Hand-off email</label>
          <input id="esc" type="email" className="field" value={form.escalation_email} onChange={(e) => setForm({ ...form, escalation_email: e.target.value })} />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="wh">Webhook URL (optional)</label>
          <input id="wh" className="field" placeholder="https://your-system.example/caseloop" value={form.outbound_webhook_url} onChange={(e) => setForm({ ...form, outbound_webhook_url: e.target.value })} />
          <p className="text-xs text-ink-soft mt-1">Receives case.created and case.state_changed as JSON.</p>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="origins">Websites allowed to use the chat widget (optional)</label>
          <textarea id="origins" className="field font-mono text-xs min-h-20" placeholder={"https://www.example.com\nhttps://shop.example.com"} value={form.allowed_origins} onChange={(e) => setForm({ ...form, allowed_origins: e.target.value })} />
          <p className="text-xs text-ink-soft mt-1">One origin per line, scheme and host only. Leave empty to accept chats from any site.</p>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="slack">Slack alerts (optional)</label>
          <input id="slack" className="field" placeholder="https://hooks.slack.com/services/…" value={form.slack_webhook_url} onChange={(e) => setForm({ ...form, slack_webhook_url: e.target.value })} />
          <p className="text-xs text-ink-soft mt-1">Incoming-webhook URL. Each case that needs a person is posted here as well as emailed.</p>
        </div>
        {error && <p className="text-clay text-sm">{error}</p>}
        <div><button className="btn btn-primary" type="submit">{saved ? "Saved" : "Save changes"}</button></div>
      </form>
        <div className="border-t border-line pt-6"><OrderSources /></div>
        <div className="border-t border-line pt-6"><Queue /></div>
      </div>
      <div className="border-t border-line pt-6"><Team member={member} /></div>

      <aside className="flex flex-col gap-6 text-sm">
        <div>
          <h2 className="font-semibold mb-1">Email</h2>
          <p className="text-ink-soft">Forward support mail to</p>
          <code className="block mt-1 bg-panel border border-line rounded px-2 py-1 break-all">{business.inbound_email}</code>
        </div>
        <div>
          <h2 className="font-semibold mb-1">Website chat</h2>
          <p className="text-ink-soft">Paste before <code>&lt;/body&gt;</code> on any page.</p>
          <textarea readOnly className="field mt-1 font-mono text-xs min-h-24" value={snippet} onFocus={(e) => e.currentTarget.select()} />
        </div>
        {business.dial_phone_number && (
          <div>
            <h2 className="font-semibold mb-1">Text and calls</h2>
            <p className="text-ink-soft">Customers can text</p>
            <code className="block mt-1 bg-panel border border-line rounded px-2 py-1">{business.dial_phone_number}</code>
          </div>
        )}
        <div>
          <h2 className="font-semibold mb-1">API</h2>
          <p className="text-ink-soft">Key starts with <code>{business.api_key_prefix}…</code>. Open cases from anything you already run:</p>
          <pre className="mt-1 bg-panel border border-line rounded px-2 py-2 text-xs overflow-x-auto">{`POST ${origin}/v1/cases
Authorization: Bearer ${business.api_key_prefix}…
{"customer_email":"…","text":"…"}`}</pre>
        </div>
      </aside>
    </div>
  );
}
