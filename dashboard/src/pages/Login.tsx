import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, setApiKey } from "../api";

/** Landing page for emailed links: exchanges ?token= for a session cookie, then goes to cases. */
export function Login() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const exchange = useRef<{ token: string; request: Promise<unknown> } | null>(null);
  const token = params.get("token");

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (!token) { setError("This link is missing its token."); return; }
    // Reuse the request during StrictMode's effect replay; a new link needs its own exchange.
    if (exchange.current?.token !== token) {
      exchange.current = { token, request: api("/v1/auth/magic-link/exchange", { method: "POST", body: JSON.stringify({ token }) }) };
    }
    exchange.current.request
      .then(() => { if (!cancelled) { setApiKey(null); navigate("/cases", { replace: true }); } })
      .catch((e) => { if (!cancelled) setError((e as Error).message); });
    return () => { cancelled = true; };
  }, [token, navigate]);

  return (
    <main className="min-h-screen grid place-items-center px-6">
      <div className="w-full max-w-sm">
        {error ? (
          <>
            <h1 className="text-2xl font-semibold mb-1">That link did not work</h1>
            <p className="text-ink-soft mb-6">{error}</p>
            <Link className="btn btn-primary" to="/">Request a new link</Link>
          </>
        ) : (
          <p className="text-ink-soft">Signing you in…</p>
        )}
      </div>
    </main>
  );
}
