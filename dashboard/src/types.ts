export type CaseState = "new" | "gathering" | "awaiting_customer" | "resolved" | "escalated" | "abandoned";

export interface Customer { id: string; email: string | null; phone: string | null; name: string | null }
export interface Requirement { key: string; label: string; why_needed: string; status: "missing" | "provided" | "not_applicable"; value: unknown; asked_count: number }
export interface Message { id: string; direction: "inbound" | "outbound" | "internal"; channel: string; sender: string; body_text: string; attachments: { filename?: string }[]; meta: Record<string, unknown>; created_at: string }
export interface Case {
  id: string; state: CaseState; channel: string; category: string | null; subject: string | null; summary: string | null;
  sentiment: string | null; urgency: string | null; remedy_requested: string | null; external_ref: string | null;
  followups_sent: number; last_customer_message_at: string | null; last_agent_message_at: string | null;
  resolution: { type?: string; notes?: string; by?: string } | null; escalation: { reason?: string; summary?: string } | null;
  closed_at: string | null; created_at: string; updated_at: string; customer: Customer; requirements: Requirement[];
}
export interface CaseDetail extends Case { messages: Message[] }
export interface TimelineItem { kind: "event" | "message"; at: string; type?: string; actor?: string; data?: Record<string, unknown>; direction?: string; channel?: string; sender?: string; text?: string; meta?: Record<string, unknown> | null }
export interface Business {
  id: string; name: string; slug: string; inbound_email: string; from_email: string; escalation_email: string; widget_public_key: string;
  api_key_prefix: string; dial_phone_number: string | null; context_text: string; followup_schedule_hours: number[]; max_followups: number; outbound_webhook_url: string | null;
  allowed_origins: string[];
}
export interface Job { id: string; kind: string; status: "pending" | "running" | "done" | "failed" | "cancelled"; case_id: string | null; attempts: number; due_at: string; last_error: string | null; payload: Record<string, unknown>; created_at: string; updated_at: string }
export interface Member { id: string; email: string; name: string; role: "owner" | "member"; created_at: string }
export interface Session { member: Member; business: Business }
export interface ShellContext { business: Business; setBusiness: (b: Business) => void; member: Member | null }
export interface Theme { name: string; share_pct: number; example_case_ids: string[]; root_causes: string[]; suggested_fixes: string[] }
export interface InsightsReport { headline: string; themes: Theme[]; sentiment_trend: string; emerging_issues: string[]; top_escalation_reasons: string[] }
export interface Snapshot { id: string; period_start: string; period_end: string; case_count: number; model: string; report: InsightsReport; generated_at: string }

export interface OrderOut {
  source: string; source_label: string; order_number: string; status: string; customer_email: string | null; customer_name: string | null;
  total: number | null; currency: string | null; ordered_at: string | null; delivered_at: string | null;
  tracking: { carrier?: string | null; number?: string | null; url?: string | null; status?: string | null } | null; items: { name: string; quantity: number }[]; synced_at?: string | null;
}
export interface OrderSummary { source: string; order_number: string; status: string; ordered_at: string | null; shipped_at: string | null; delivered_at: string | null; total: number | null; currency: string | null; items: { name: string; quantity: number }[]; tracking: OrderOut["tracking"]; customer_email: string | null; customer_name: string | null; notes: string | null }
export interface LookupSummary { found: OrderSummary[]; sources_checked: string[]; errors: { source: string; error: string }[]; no_order_system_connected: boolean }
export interface SourceConfig { type: string; label?: string; [key: string]: unknown }
export interface SourcesInfo { configured: SourceConfig[]; stored_counts: Record<string, { count: number; label: string }>; forward_address: string; live_types: string[]; stored_types: string[] }
