import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, setApiKey } from "../api";

/** Landing page for emailed links: exchanges ?token= for a session cookie, then goes to cases. */
export function Login() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);
  const token = params.get("token");

  useEffect(() => {
    if (started.current) return; // the token is single-use; never exchange it twice (StrictMode)
    started.current = true;
    if (!token) { setError("This link is missing its token."); return; }
    api("/v1/auth/magic-link/exchange", { method: "POST", body: JSON.stringify({ token }) })
      .then(() => { setApiKey(null); navigate("/cases", { replace: true }); })
      .catch((e) => setError((e as Error).message));
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
