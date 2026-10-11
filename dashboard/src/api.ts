const KEY_STORE = "caseloop.apiKey";

export function getApiKey(): string | null { return localStorage.getItem(KEY_STORE); }
export function setApiKey(key: string | null) { if (key) localStorage.setItem(KEY_STORE, key); else localStorage.removeItem(KEY_STORE); }

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

// People sign in with an emailed link and carry an httpOnly session cookie; an API key in
// localStorage (the old flow, still accepted) is sent as a bearer token when present.
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const key = getApiKey();
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(key ? { Authorization: `Bearer ${key}` } : {}), ...(init.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body?.error?.message || `Request failed (${res.status})`);
  return body as T;
}

export const fmtTime = (iso: string | null | undefined) => {
  if (!iso) return "";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

export const ago = (iso: string | null | undefined) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
