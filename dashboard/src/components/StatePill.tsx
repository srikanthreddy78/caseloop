import type { CaseState } from "../types";

const STYLES: Record<CaseState, string> = {
  new: "bg-slate-soft text-slate",
  gathering: "bg-moss-soft text-moss",
  awaiting_customer: "bg-amber-soft text-amber",
  resolved: "bg-line-soft text-ink-soft",
  escalated: "bg-clay-soft text-clay",
  abandoned: "bg-line-soft text-ink-soft",
};

export const STATE_LABEL: Record<CaseState, string> = {
  new: "New",
  gathering: "Agent working",
  awaiting_customer: "Waiting on customer",
  resolved: "Resolved",
  escalated: "Needs a person",
  abandoned: "Customer went quiet",
};

export function StatePill({ state }: { state: CaseState }) {
  return (
    <span className={`inline-flex items-center h-6 px-2 rounded text-xs font-medium whitespace-nowrap ${STYLES[state]}`}>
      {STATE_LABEL[state]}
    </span>
  );
}
