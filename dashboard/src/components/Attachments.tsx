import { useEffect, useState } from "react";
import { apiObjectUrl } from "../api";
import type { Attachment } from "../types";

const fmtSize = (n: number | null) => (n == null ? "" : n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`);

/** Loads one case attachment through the authenticated API and keeps the object URL alive while mounted. */
function useAttachmentUrl(path: string): { url: string | null; failed: boolean } {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let created: string | null = null;
    setUrl(null); setFailed(false);
    apiObjectUrl(path)
      .then((u) => { if (active) { created = u; setUrl(u); } else URL.revokeObjectURL(u); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; if (created) URL.revokeObjectURL(created); };
  }, [path]);
  return { url, failed };
}

function ImageThumb({ caseId, a }: { caseId: string; a: Attachment }) {
  const { url, failed } = useAttachmentUrl(`/v1/cases/${caseId}/attachments/${a.index}`);
  if (failed) return <FileLink caseId={caseId} a={a} />;
  return (
    <a
      href={url ?? undefined}
      target="_blank"
      rel="noopener"
      title={`${a.filename}${a.size ? ` · ${fmtSize(a.size)}` : ""}`}
      className="block w-28 h-28 rounded-md overflow-hidden border border-line bg-line-soft"
      aria-label={`Open ${a.filename}`}
    >
      {url && <img src={url} alt={a.filename} className="w-full h-full object-cover" loading="lazy" />}
    </a>
  );
}

function FileLink({ caseId, a }: { caseId: string; a: Attachment }) {
  const { url, failed } = useAttachmentUrl(`/v1/cases/${caseId}/attachments/${a.index}`);
  return (
    <a
      href={url ?? undefined}
      download={a.filename}
      target="_blank"
      rel="noopener"
      className={`inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md border border-line bg-panel text-sm ${url ? "hover:border-ink" : "text-ink-soft"}`}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
      <span className="max-w-56 truncate">{a.filename}</span>
      {a.size != null && <span className="text-xs text-ink-soft">{fmtSize(a.size)}</span>}
      {failed && <span className="text-xs text-clay">unavailable</span>}
    </a>
  );
}

export function Attachments({ caseId, items }: { caseId: string; items: Attachment[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {items.map((a) => ((a.content_type || "").startsWith("image/") ? <ImageThumb key={a.index} caseId={caseId} a={a} /> : <FileLink key={a.index} caseId={caseId} a={a} />))}
    </div>
  );
}
