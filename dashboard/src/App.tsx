import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { api, getApiKey, setApiKey } from "./api";
import type { Business } from "./types";

export function Shell() {
  const [business, setBusiness] = useState<Business | null>(null);
  const [key, setKey] = useState(getApiKey() || "");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!getApiKey()) return;
    api<{ data: Business }>("/v1/businesses/me").then((r) => setBusiness(r.data)).catch((e) => { setError(e.message); setApiKey(null); });
  }, []);

  if (!business) {
    return (
      <main className="min-h-screen grid place-items-center px-6">
        <form
          className="w-full max-w-sm"
          onSubmit={async (e) => {
            e.preventDefault();
            setApiKey(key.trim());
            try { const r = await api<{ data: Business }>("/v1/businesses/me"); setBusiness(r.data); setError(null); navigate("/cases"); }
            catch (ex) { setError((ex as Error).message); setApiKey(null); }
          }}
        >
          <h1 className="text-2xl font-semibold mb-1">Caseloop</h1>
          <p className="text-ink-soft mb-6">Paste the API key that was printed when this business was created.</p>
          <label className="block text-sm text-ink-soft mb-1" htmlFor="key">API key</label>
          <input id="key" className="field mb-3" value={key} onChange={(e) => setKey(e.target.value)} placeholder="cl_…" autoComplete="off" />
          {error && <p className="text-clay text-sm mb-3">{error}</p>}
          <button className="btn btn-primary w-full justify-center" type="submit">Open dashboard</button>
        </form>
      </main>
    );
  }

  const link = ({ isActive }: { isActive: boolean }) =>
    `block px-3 py-2 rounded-md text-sm ${isActive ? "bg-panel font-medium" : "text-ink-soft hover:text-ink"}`;

  return (
    <div className="min-h-screen grid grid-cols-[220px_1fr]">
      <aside className="border-r border-line px-4 py-6 flex flex-col gap-6 sticky top-0 h-screen">
        <div>
          <div className="text-xs text-ink-soft">Caseloop</div>
          <div className="font-semibold leading-tight">{business.name}</div>
        </div>
        <nav className="flex flex-col gap-1">
          <NavLink to="/cases" className={link}>Cases</NavLink>
          <NavLink to="/insights" className={link}>Insights</NavLink>
          <NavLink to="/settings" className={link}>Setup</NavLink>
        </nav>
        <button className="mt-auto text-left text-sm text-ink-soft hover:text-ink" onClick={() => { setApiKey(null); setBusiness(null); }}>Sign out</button>
      </aside>
      <main className="px-10 py-8 max-w-[1120px] w-full">
        <Outlet context={{ business, setBusiness }} />
      </main>
    </div>
  );
}
