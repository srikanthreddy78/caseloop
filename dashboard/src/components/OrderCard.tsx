import type { OrderSummary } from "../types";

const STATUS_STYLE: Record<string, string> = {
  delivered: "bg-moss-soft text-moss",
  shipped: "bg-slate-soft text-slate",
  processing: "bg-amber-soft text-amber",
  placed: "bg-amber-soft text-amber",
  cancelled: "bg-clay-soft text-clay",
  refunded: "bg-line-soft text-ink-soft",
  returned: "bg-line-soft text-ink-soft",
  unknown: "bg-line-soft text-ink-soft",
};

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : null);

export function SourceBadge({ label }: { label: string }) {
  return <span className="inline-flex items-center h-5 px-1.5 rounded border border-line text-[11px] text-ink-soft bg-panel whitespace-nowrap">{label}</span>;
}

export function OrderCard({ o }: { o: OrderSummary }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2.5 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="font-medium">#{o.order_number}</div>
        <div className="flex items-center gap-1.5">
          <span className={`inline-flex h-5 px-1.5 rounded text-[11px] font-medium items-center ${STATUS_STYLE[o.status] || STATUS_STYLE.unknown}`}>{o.status}</span>
          <SourceBadge label={o.source} />
        </div>
      </div>
      <dl className="mt-1.5 grid grid-cols-[88px_1fr] gap-y-0.5 text-ink-soft">
        {o.ordered_at && <><dt>Ordered</dt><dd className="text-ink">{day(o.ordered_at)}</dd></>}
        {o.delivered_at && <><dt>Delivered</dt><dd className="text-ink">{day(o.delivered_at)}</dd></>}
        {o.total != null && <><dt>Total</dt><dd className="text-ink">{o.total.toFixed(2)} {o.currency || ""}</dd></>}
        {o.tracking?.number && (
          <><dt>Tracking</dt><dd className="text-ink">{o.tracking.url ? <a className="underline" href={o.tracking.url} target="_blank" rel="noreferrer">{o.tracking.number}</a> : o.tracking.number}{o.tracking.carrier ? ` · ${o.tracking.carrier}` : ""}{o.tracking.status ? ` · ${o.tracking.status}` : ""}</dd></>
        )}
        {o.items.length > 0 && <><dt>Items</dt><dd className="text-ink">{o.items.map((i) => `${i.quantity}× ${i.name}`).join(", ")}</dd></>}
      </dl>
    </div>
  );
}
