import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ago } from "../api";

interface Lesson { id: string; kind: string; category: string | null; text: string; case_id: string | null; created_at: string }

const KIND_LABEL: Record<string, string> = { human_resolution: "Decision", human_takeover: "Hand-off", human_reply: "Reply" };

export function Lessons() {
  const [lessons, setLessons] = useState<Lesson[] | null>(null);
  const load = () => api<{ data: Lesson[] }>("/v1/lessons").then((r) => setLessons(r.data)).catch(() => setLessons([]));
  useEffect(() => { load(); }, []);
  if (!lessons) return null;
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h2 className="text-sm font-medium">What the agent has learned from you</h2>
        <p className="text-sm text-ink-soft">Every time a person replies, resolves, or takes over a case, the agent keeps it as a lesson and follows it on similar cases. Remove anything you don't want it to copy.</p>
      </div>
      {lessons.length === 0 ? (
        <p className="text-sm text-ink-soft">Nothing yet. Reply to or resolve a case from the case file and it will show up here.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {lessons.map((l) => (
            <li key={l.id} className="rounded-lg border border-line bg-panel px-3 py-2.5 text-sm flex gap-3 items-start">
              <span className="inline-flex h-5 px-1.5 rounded bg-moss-soft text-moss text-[11px] font-medium items-center whitespace-nowrap mt-0.5">{KIND_LABEL[l.kind] || l.kind}</span>
              <div className="flex-1 min-w-0">
                <div>{l.text}</div>
                <div className="text-xs text-ink-soft mt-1">{l.category || "any category"} · {ago(l.created_at)}{l.case_id && <> · <Link className="underline" to={`/cases/${l.case_id}`}>case</Link></>}</div>
              </div>
              <button type="button" className="text-xs text-ink-soft hover:text-clay" onClick={async () => { await api(`/v1/lessons/${l.id}`, { method: "DELETE" }); load(); }}>Forget</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
