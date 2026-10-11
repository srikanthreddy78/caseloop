import { useEffect, useState } from "react";
import { api } from "../api";
import type { Member } from "../types";

export function Team({ member }: { member: Member | null }) {
  const [members, setMembers] = useState<Member[] | null>(null);
  const [form, setForm] = useState({ email: "", name: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<string | null>(null);
  const isOwner = member?.role === "owner";

  useEffect(() => {
    api<{ data: Member[] }>("/v1/team").then((r) => setMembers(r.data)).catch((e) => setError((e as Error).message));
  }, []);

  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null); setAdded(null);
    try {
      const r = await api<{ data: Member }>("/v1/team", { method: "POST", body: JSON.stringify({ email: form.email.trim(), name: form.name.trim() }) });
      setMembers((ms) => [...(ms || []), r.data]);
      setAdded(r.data.email);
      setForm({ email: "", name: "" });
    } catch (ex) { setError((ex as Error).message); }
    finally { setBusy(false); }
  };

  const remove = async (m: Member) => {
    if (!window.confirm(`Remove ${m.name || m.email} from the team? They will be signed out.`)) return;
    setError(null);
    try {
      await api(`/v1/team/${m.id}`, { method: "DELETE" });
      setMembers((ms) => (ms || []).filter((x) => x.id !== m.id));
    } catch (ex) { setError((ex as Error).message); }
  };

  return (
    <section>
      <h2 className="font-semibold">Team</h2>
      <p className="text-sm text-ink-soft mt-1 mb-3">Everyone signs in with a link sent to their work email. Owners can add and remove people.</p>
      {members === null ? (
        <p className="text-sm text-ink-soft">Loading…</p>
      ) : (
        <ul className="border border-line rounded-md bg-panel divide-y divide-line-soft">
          {members.map((m) => (
            <li key={m.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <div className="flex-1 min-w-0">
                <div className="truncate">{m.name || m.email}{member && m.id === member.id && <span className="text-ink-soft"> (you)</span>}</div>
                {m.name && <div className="text-xs text-ink-soft truncate">{m.email}</div>}
              </div>
              <span className="text-xs text-ink-soft">{m.role}</span>
              {isOwner && member && m.id !== member.id && (
                <button type="button" className="text-xs text-clay hover:underline" onClick={() => remove(m)}>Remove</button>
              )}
            </li>
          ))}
        </ul>
      )}
      {isOwner ? (
        <form onSubmit={invite} className="mt-3 grid grid-cols-[1fr_1fr_auto] gap-2 items-start">
          <input type="email" required className="field" placeholder="name@company.com" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} aria-label="Email" />
          <input className="field" placeholder="Name (optional)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="Name" />
          <button className="btn" type="submit" disabled={busy}>{busy ? "Adding…" : "Add"}</button>
        </form>
      ) : (
        <p className="text-xs text-ink-soft mt-2">{member ? "Only owners can add or remove people." : "Sign in with your email to manage the team."}</p>
      )}
      {added && <p className="text-sm text-moss mt-2">Sign-in link sent to {added}.</p>}
      {error && <p className="text-sm text-clay mt-2">{error}</p>}
    </section>
  );
}
