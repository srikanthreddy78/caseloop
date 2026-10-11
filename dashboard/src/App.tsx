import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { ApiError, api, getApiKey, setApiKey } from "./api";
import type { Business, Member, Session } from "./types";

type Auth =
  | { status: "loading" }
  | { status: "signed_out" }
  | { status: "error"; message: string }
  | { status: "signed_in"; business: Business; member: Member | null };

export function Shell() {
  const [auth, setAuth] = useState<Auth>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    setAuth({ status: "loading" });
    (async () => {
      try {
        if (getApiKey()) {
          const r = await api<{ data: Business }>("/v1/businesses/me");
          if (!cancelled) setAuth({ status: "signed_in", business: r.data, member: null });
          return;
        }
        const r = await api<{ data: Session }>("/v1/auth/me");
        if (!cancelled) setAuth({ status: "signed_in", business: r.data.business, member: r.data.member });
      } catch (ex) {
        if (cancelled) return;
        // Only a 401 means the credentials are bad. A rate limit, an outage or a flaky network
        // must not throw away a working key or session.
        if (ex instanceof ApiError && ex.status === 401) {
          setApiKey(null);
          setAuth({ status: "signed_out" });
        } else {
          setAuth({ status: "error", message: ex instanceof Error ? ex.message : "Request failed" });
        }
      }
    })();
    return () => { cancelled = true; };
  }, [attempt]);

  if (auth.status === "loading") return <main className="min-h-screen" />;
  if (auth.status === "signed_out") return <SignIn />;
  if (auth.status === "error") {
    return (
      <main className="min-h-screen grid place-items-center px-6">
        <div className="w-full max-w-sm text-center">
          <h1 className="text-2xl font-semibold mb-1">Could not reach Caseloop</h1>
          <p className="text-ink-soft mb-6">{auth.message}. You are still signed in; try again in a moment.</p>
          <button className="btn btn-primary" type="button" onClick={() => setAttempt((n) => n + 1)}>Retry</button>
        </div>
      </main>
    );
  }

  const { business, member } = auth;
  const setBusiness = (b: Business) => setAuth({ status: "signed_in", business: b, member });
  const signOut = async () => {
    setApiKey(null);
    try { await api("/v1/auth/logout", { method: "POST" }); } catch { /* already signed out */ }
    setAuth({ status: "signed_out" });
    navigate("/");
  };

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
          <NavLink to="/orders" className={link}>Orders</NavLink>
          <NavLink to="/insights" className={link}>Insights</NavLink>
          <NavLink to="/settings" className={link}>Setup</NavLink>
        </nav>
        <div className="mt-auto text-sm">
          {member && <div className="text-xs text-ink-soft truncate mb-1" title={member.email}>{member.name || member.email}</div>}
          <button className="text-left text-ink-soft hover:text-ink" onClick={signOut}>Sign out</button>
        </div>
      </aside>
      <main className="px-10 py-8 max-w-[1120px] w-full">
        <Outlet context={{ business, setBusiness, member }} />
      </main>
    </div>
  );
}

function SignIn() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<{ email: string; link: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (sent) {
    // In dev the API echoes the emailed link; open it inside this app so it works under vite too.
    const token = sent.link ? new URL(sent.link).searchParams.get("token") : null;
    return (
      <main className="min-h-screen grid place-items-center px-6">
        <div className="w-full max-w-sm">
          <h1 className="text-2xl font-semibold mb-1">Check your email</h1>
          <p className="text-ink-soft mb-6">
            If <span className="text-ink">{sent.email}</span> belongs to a team, a sign-in link is on its way. It works once and expires in 15 minutes.
          </p>
          {token && (
            <div className="rounded-md border border-amber-soft bg-amber-soft px-3 py-2 text-sm mb-6">
              <div className="text-amber text-xs font-medium mb-1">Dev mode: the link was also logged by the server</div>
              <Link className="underline" to={`/login?token=${encodeURIComponent(token)}`}>Open link</Link>
            </div>
          )}
          <button className="text-sm text-ink-soft hover:text-ink" type="button" onClick={() => setSent(null)}>Use a different email</button>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen grid place-items-center px-6">
      <form
        className="w-full max-w-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          const address = email.trim();
          try {
            const r = await api<{ data: { link: string | null } }>("/v1/auth/magic-link", { method: "POST", body: JSON.stringify({ email: address }) });
            setSent({ email: address, link: r.data.link });
          } catch (ex) { setError((ex as Error).message); }
          finally { setBusy(false); }
        }}
      >
        <h1 className="text-2xl font-semibold mb-1">Caseloop</h1>
        <p className="text-ink-soft mb-6">Enter your work email and we will send you a sign-in link.</p>
        <label className="block text-sm text-ink-soft mb-1" htmlFor="email">Work email</label>
        <input id="email" type="email" required autoFocus className="field mb-3" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoComplete="email" />
        {error && <p className="text-clay text-sm mb-3">{error}</p>}
        <button className="btn btn-primary w-full justify-center" type="submit" disabled={busy}>{busy ? "Sending…" : "Email me a sign-in link"}</button>
      </form>
    </main>
  );
}
