import { useEffect, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import { OrderCard, SourceBadge } from "../components/OrderCard";
import type { Business, LookupSummary, OrderOut, ShellContext, SourcesInfo } from "../types";

export function Orders() {
  const { business, setBusiness } = useOutletContext<ShellContext>();
  const [senders, setSenders] = useState((business.order_email_senders || []).join("\n"));
  const [sendersMsg, setSendersMsg] = useState<string | null>(null);
  const [info, setInfo] = useState<SourcesInfo | null>(null);
  const [orders, setOrders] = useState<OrderOut[]>([]);
  const [q, setQ] = useState("");
  const [test, setTest] = useState<LookupSummary | null>(null);
  const [testNumber, setTestNumber] = useState("");
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const latestLoad = useRef(0);
  const activeQuery = useRef("");

  const load = async (query = activeQuery.current) => {
    const request = ++latestLoad.current;
    activeQuery.current = query;
    try {
      const [s, o] = await Promise.all([
        api<{ data: SourcesInfo }>("/v1/orders/sources"),
        api<{ data: OrderOut[] }>(`/v1/orders${query ? `?q=${encodeURIComponent(query)}` : ""}`),
      ]);
      if (request !== latestLoad.current) return;
      setError(null); setInfo(s.data); setOrders(o.data);
    } catch (e) {
      if (request === latestLoad.current) setError((e as Error).message);
    }
  };
  useEffect(() => { load(); return () => { latestLoad.current += 1; }; }, []);

  const importFile = async (file: File) => {
    setBusy(true); setImportMsg(null);
    try {
      const text = await file.text();
      const r = await api<{ data: { imported: number; source_label: string; columns_used: Record<string, string> } }>("/v1/orders/import", { method: "POST", body: JSON.stringify({ csv: text }) });
      setImportMsg(`Imported ${r.data.imported} orders as “${r.data.source_label}”. Columns used: ${Object.entries(r.data.columns_used).map(([k, v]) => `${k} ← ${v}`).join(", ")}.`);
      await load();
    } catch (e) { setImportMsg((e as Error).message); }
    finally { setBusy(false); }
  };

  const saveSenders = async () => {
    setBusy(true); setSendersMsg(null);
    try {
      const list = senders.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      const r = await api<{ data: Business }>("/v1/businesses/me", { method: "PATCH", body: JSON.stringify({ order_email_senders: list }) });
      setBusiness(r.data); setSenders(r.data.order_email_senders.join("\n")); setSendersMsg("Saved");
    } catch (e) { setSendersMsg((e as Error).message); }
    finally { setBusy(false); }
  };

  const runTest = async () => {
    if (busy || !testNumber.trim()) return;
    setBusy(true); setTest(null); setTestError(null);
    try { const r = await api<{ data: LookupSummary }>("/v1/orders/sources/test", { method: "POST", body: JSON.stringify({ order_number: testNumber }) }); setTest(r.data); await load(); }
    catch (e) { setTestError((e as Error).message); }
    finally { setBusy(false); }
  };

  if (!info) return <div><p className="text-ink-soft">{error || "Loading…"}</p>{error && <button className="btn mt-3" onClick={() => load()}>Retry</button>}</div>;
  const stored = Object.entries(info.stored_counts);

  return (
    <div>
      <header className="mb-8">
        <h1 className="text-2xl font-semibold">Orders</h1>
        <p className="text-ink-soft text-sm mt-1">Everything the agent can look up, and where each order came from.</p>
      </header>

      <div className="grid grid-cols-[1fr_340px] gap-12">
        <section>
          <div className="flex items-center gap-2 mb-4">
            <input className="field" placeholder="Search by order number, email, or phone" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") load(q); }} />
            <button className="btn" onClick={() => load(q)}>Search</button>
          </div>
          {error && <p className="text-sm text-clay mb-4">{error}</p>}
          {orders.length === 0 ? (
            <div className="border border-dashed border-line rounded-lg p-8 text-center">
              <p className="font-medium">No orders stored yet.</p>
              <p className="text-ink-soft text-sm mt-1">Connect a source on the right, import a file, or forward confirmation emails.</p>
            </div>
          ) : (
            <ul className="border-t border-line">
              {orders.map((o) => (
                <li key={`${o.source}:${o.order_number}`} className="border-b border-line-soft py-2.5 grid grid-cols-[120px_1fr_110px_150px] gap-3 items-center text-sm">
                  <div className="font-medium">#{o.order_number}</div>
                  <div className="text-ink-soft truncate">{o.customer_name || o.customer_email || "—"}{o.items.length ? ` · ${o.items.map((i) => i.name).join(", ")}` : ""}</div>
                  <div>{o.status}</div>
                  <div className="flex justify-end"><SourceBadge label={o.source_label} /></div>
                </li>
              ))}
            </ul>
          )}

          <div className="mt-10 border-t border-line pt-6">
            <h2 className="font-semibold mb-1">Try a lookup</h2>
            <p className="text-sm text-ink-soft mb-3">Exactly what the agent gets when it checks an order, with the source named.</p>
            <div className="flex gap-2 mb-4">
              <input className="field max-w-xs" placeholder="Order number" value={testNumber} disabled={busy} onChange={(e) => setTestNumber(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") runTest(); }} />
              <button className="btn btn-primary" onClick={runTest} disabled={busy}>Look up</button>
            </div>
            {testError && <p className="text-sm text-clay">{testError}</p>}
            {test && (
              <div className="flex flex-col gap-2">
                {test.no_order_system_connected && <p className="text-sm text-amber">No order source is connected yet, so nothing was checked.</p>}
                {test.sources_checked.length > 0 && <p className="text-sm text-ink-soft">Checked: {test.sources_checked.join(", ")}</p>}
                {test.found.map((o, i) => <OrderCard key={i} o={o} />)}
                {test.found.length === 0 && !test.no_order_system_connected && <p className="text-sm text-ink-soft">No order with that number in any connected source.</p>}
                {test.errors.map((e, i) => <p key={i} className="text-sm text-clay">{e.source}: {e.error}</p>)}
              </div>
            )}
          </div>
        </section>

        <aside className="flex flex-col gap-6 text-sm">
          <div>
            <h2 className="font-semibold mb-2">Connected sources</h2>
            {info.configured.length === 0 ? <p className="text-ink-soft">None yet. Add Shopify, Amazon, your own API, or a tracking page in Setup.</p> : (
              <ul className="flex flex-col gap-1">{info.configured.map((c, i) => <li key={i} className="flex items-center gap-2"><SourceBadge label={c.label || c.type} /><span className="text-ink-soft truncate">{String(c.shop || c.url_template || "")}</span></li>)}</ul>
            )}
          </div>
          <div>
            <h2 className="font-semibold mb-2">Stored orders by source</h2>
            {stored.length === 0 ? <p className="text-ink-soft">Nothing imported yet.</p> : (
              <ul className="flex flex-col gap-1">{stored.map(([k, v]) => <li key={k} className="flex justify-between"><span>{v.label}</span><span className="text-ink-soft">{v.count}</span></li>)}</ul>
            )}
          </div>
          <div>
            <h2 className="font-semibold mb-1">Import a file</h2>
            <p className="text-ink-soft mb-2">CSV export from any store or spreadsheet. Columns like order number, email, status, date, tracking, item, qty, total are picked up by name.</p>
            <input type="file" accept=".csv,text/csv" disabled={busy} onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }} className="text-sm" />
            {importMsg && <p className="mt-2 text-ink-soft">{importMsg}</p>}
          </div>
          <div>
            <h2 className="font-semibold mb-1">Forward order emails</h2>
            <p className="text-ink-soft">Auto-forward confirmation and shipping emails from any store to</p>
            <code className="block mt-1 bg-panel border border-line rounded px-2 py-1 break-all">{info.forward_address}</code>
            <p className="text-ink-soft mt-1">Each one becomes a stored order labeled “Forwarded email”.</p>
            <label className="block font-medium mt-3 mb-1" htmlFor="senders">Accept order emails from</label>
            <textarea id="senders" className="field font-mono text-xs min-h-20" placeholder={"orders@shopify.com\namazon.com"} value={senders} onChange={(e) => setSenders(e.target.value)} />
            <p className="text-xs text-ink-soft mt-1">Addresses or domains, one per line. Mail from anyone else is ignored, so nobody can plant orders by guessing the address.</p>
            <div className="flex items-center gap-3 mt-2">
              <button type="button" className="btn" disabled={busy} onClick={saveSenders}>Save senders</button>
              {sendersMsg && <span className="text-xs text-ink-soft">{sendersMsg}</span>}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
