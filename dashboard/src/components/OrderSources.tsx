import { useEffect, useState } from "react";
import { api } from "../api";
import type { SourceConfig, SourcesInfo } from "../types";

type Kind = "shopify" | "amazon" | "custom_api" | "tracking_page";

const KINDS: { type: Kind; label: string; help: string; fields: { key: string; label: string; placeholder?: string; secret?: boolean; textarea?: boolean }[] }[] = [
  { type: "shopify", label: "Shopify", help: "Admin API access token with read_orders and read_fulfillments.", fields: [{ key: "shop", label: "Shop domain", placeholder: "acme.myshopify.com" }, { key: "token", label: "Admin API token", placeholder: "shpat_…", secret: true }] },
  { type: "custom_api", label: "Your website API", help: "Any JSON endpoint you already have. {order_number} is replaced in the URL. Map your field names to ours below.", fields: [{ key: "url_template", label: "URL", placeholder: "https://shop.example.com/api/orders/{order_number}" }, { key: "headers", label: "Headers (JSON)", placeholder: '{"Authorization": "Bearer …"}', secret: true }, { key: "list_path", label: "Path to the order object (optional)", placeholder: "data" }, { key: "mapping", label: "Field mapping (JSON: ours → yours)", placeholder: '{"order_number": "id", "status": "state", "delivered_at": "delivered_on", "tracking_number": "shipment.tracking", "total": "amount"}', textarea: true }] },
  { type: "tracking_page", label: "Tracking page", help: "No API? Give the public tracking page pattern and we read it.", fields: [{ key: "url_template", label: "Page URL", placeholder: "https://shop.example.com/track?order={order_number}" }] },
  { type: "amazon", label: "Amazon", help: "Selling Partner API credentials. Until these are in, import Amazon orders by CSV or forwarded emails.", fields: [{ key: "client_id", label: "LWA client id" }, { key: "client_secret", label: "LWA client secret", secret: true }, { key: "refresh_token", label: "Refresh token", secret: true }, { key: "marketplace_id", label: "Marketplace id", placeholder: "ATVPDKIKX0DER" }] },
];

function toForm(c: SourceConfig): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(c)) out[k] = typeof v === "string" ? v : v == null ? "" : JSON.stringify(v);
  return out;
}
function fromForm(f: Record<string, string>): SourceConfig {
  const out: SourceConfig = { type: f.type };
  for (const [k, v] of Object.entries(f)) {
    if (k === "type" || k === "label" || v === "") continue;
    if ((k === "headers" || k === "mapping") && v !== "••••") { try { out[k] = JSON.parse(v); } catch { throw new Error(`${k} must be valid JSON`); } }
    else out[k] = v;
  }
  return out;
}

export function OrderSources() {
  const [rows, setRows] = useState<Record<string, string>[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => { api<{ data: SourcesInfo }>("/v1/orders/sources").then((r) => setRows(r.data.configured.map(toForm))); }, []);
  if (!rows) return null;

  const save = async () => {
    setMsg(null);
    try {
      const r = await api<{ data: SourceConfig[] }>("/v1/orders/sources", { method: "PUT", body: JSON.stringify({ order_sources: rows.map(fromForm) }) });
      setRows(r.data.map(toForm)); setMsg("Saved");
      setTimeout(() => setMsg(null), 2000);
    } catch (e) { setMsg((e as Error).message); }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-sm font-medium">Order sources</h2>
        <p className="text-sm text-ink-soft">Where the agent looks orders up. Every result is labeled with its source. Files and forwarded emails are added on the Orders page.</p>
      </div>
      {rows.map((row, i) => {
        const kind = KINDS.find((k) => k.type === row.type) || KINDS[0];
        return (
          <div key={i} className="rounded-lg border border-line bg-panel p-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <select className="field max-w-xs" value={row.type} onChange={(e) => setRows(rows.map((r, j) => (j === i ? { type: e.target.value } : r)))}>
                {KINDS.map((k) => <option key={k.type} value={k.type}>{k.label}</option>)}
              </select>
              <button type="button" className="text-sm text-ink-soft hover:text-clay" onClick={() => setRows(rows.filter((_, j) => j !== i))}>Remove</button>
            </div>
            <p className="text-xs text-ink-soft">{kind.help}</p>
            {kind.fields.map((f) => (
              <div key={f.key}>
                <label className="block text-xs text-ink-soft mb-1">{f.label}</label>
                {f.textarea ? (
                  <textarea className="field font-mono text-xs min-h-20" placeholder={f.placeholder} value={row[f.key] || ""} onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, [f.key]: e.target.value } : r)))} />
                ) : (
                  <input className="field" type={f.secret && row[f.key] !== "••••" ? "password" : "text"} placeholder={f.placeholder} value={row[f.key] || ""} onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, [f.key]: e.target.value } : r)))} />
                )}
              </div>
            ))}
          </div>
        );
      })}
      <div className="flex items-center gap-2">
        <button type="button" className="btn" onClick={() => setRows([...rows, { type: "shopify" }])}>Add a source</button>
        <button type="button" className="btn btn-primary" onClick={save}>Save sources</button>
        {msg && <span className="text-sm text-ink-soft">{msg}</span>}
      </div>
    </div>
  );
}
