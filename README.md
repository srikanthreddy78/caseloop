# Caseloop

Autonomous customer-issue resolution for any business. A customer writes in with a
complaint, a return, a billing question, anything. Caseloop takes the case, works out
what it needs to resolve it, keeps following up with the customer on its own until it
has everything, closes the case or hands it to a person, and tells the business what
people are unhappy about and why.

Attach it to what you already run:

| Channel | How |
|---|---|
| Email | Forward `support@` to the business's inbound address (Resend Receiving) |
| Website chat | One `<script>` tag, served from this app |
| SMS and AI phone calls | Dial (getdial.ai) number on the business |
| Anything else | `POST /v1/cases` with an API key, plus signed webhooks back to you |

## How it works

```
inbound message ──► case (new) ──► agent turn ──► ask for what's missing ──► awaiting_customer
                                       │                                          │
                                       │ everything in hand                       │ follow-ups at 24h, 72h, 7d
                                       ▼                                          ▼
                                   resolved                     reply ──► agent turn      no reply ──► abandoned ──► hand-off email
                                   (or escalated to a person with a packaged summary)
```

- The agent only acts through tools: `get_case`, `update_requirements`, `send_message`,
  `schedule_followup`, `close_case`, `escalate`, `request_phone_call`, `add_internal_note`.
  Guardrails live in code, not only in the prompt (one message per turn, follow-ups clamped to
  policy, cannot close with requirements missing, card and ID numbers scrubbed from prompts).
- Every state change, message, and tool call is an event on the case timeline.
- Insights: a 30-day report (themes, root causes, suggested fixes, escalation reasons) is
  regenerated daily per business and on demand.
- One FastAPI process: HTTP API, webhook receivers, and a Postgres-backed job queue
  (agent turns, follow-ups, transcripts, insights, outbound webhooks). No Redis.

## Run it locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+, Postgres 16
(Homebrew or `docker compose up -d db`).

```bash
cp .env.example backend/.env           # set OPENAI_API_KEY and CASELOOP_DATABASE_URL
make install                           # backend deps
make db-create && make migrate         # local Postgres databases + schema
make create-business ARGS="--name 'Acme Lamps' --slug acme --inbound-email support@acme.example --escalation-email ops@acme.example"
make dev                               # http://localhost:8000
```

Then:

- Dashboard: build it once with `cd dashboard && npm install && npm run build`, open
  `http://localhost:8000/dashboard`, paste the API key printed by `create-business`.
- Chat widget: open `widget/example.html#<widget_public_key>` in a browser.
- Simulate an inbound email without any provider (dev only):

```bash
curl -X POST localhost:8000/dev/simulate/email -H 'Content-Type: application/json' -d '{
  "to": "support@acme.example", "from_email": "jane@example.net", "from_name": "Jane",
  "subject": "Broken lamp", "text": "My lamp arrived cracked. Order #A12345."}'
```

The scheduler runs inside the server, so the agent replies within seconds. With no
`OPENAI_API_KEY`, a deterministic scripted agent is used instead.

## Demo

```bash
make demo        # scripted agent, fake channels, no network
make demo-live   # real OpenAI model drives the tools; nothing is actually sent
```

## Connect real providers

**Email (Resend):** set `RESEND_API_KEY`, verify a domain, create a Receiving webhook for
`email.received` pointing at `https://<host>/webhooks/resend`, and put its signing secret in
`RESEND_WEBHOOK_SECRET`. Point the business's `inbound_email` at an address on that domain.

**SMS and calls (Dial):** set `DIAL_API_KEY`, give the business its `dial_number_id` and
`dial_phone_number`, create a webhook subscription for `message.received`, `call.ended`,
`call.transcribed` at `https://<host>/webhooks/dial`, and store the secret in
`DIAL_WEBHOOK_SECRET`.

For local testing expose port 8000 with a tunnel such as ngrok.

## API

All business endpoints take `Authorization: Bearer <api_key>` and return `{"data": …}`;
errors are `{"error": {"code", "message"}}`.

| Method | Path | Purpose |
|---|---|---|
| GET / PATCH | `/v1/businesses/me` | Policy text, follow-up schedule, hand-off email, webhook URL |
| GET | `/v1/cases?state=open,gathering&limit=&offset=` | List cases |
| POST | `/v1/cases` | Open a case from any source |
| GET | `/v1/cases/{id}` and `/timeline` | Case file and full history |
| POST | `/v1/cases/{id}/messages` | A person replies on the customer's channel |
| POST | `/v1/cases/{id}/resolve`, `/escalate`, `/run-turn` | Human overrides |
| GET / POST | `/v1/insights/latest`, `/v1/insights/generate` | Insights report |
| POST | `/v1/chat/sessions`, `/v1/chat/sessions/{token}/messages` | Used by the widget |
| POST | `/webhooks/resend`, `/webhooks/dial` | Provider webhooks |

Outbound webhooks (`case.created`, `case.state_changed`) are signed with
`X-Caseloop-Signature: t=<unix>,v1=<hmac-sha256 of "t.body">` using the business's secret.

## Develop

```bash
make test        # pytest against caseloop_test
make lint        # ruff
make typecheck   # mypy
```

Layout: `backend/app` (FastAPI app: `cases/` state machine and service, `agent/` prompts,
tools, runners, `channels/` adapters, `api/` routers, `scheduler.py`, `insights/`),
`backend/tests`, `dashboard/` (Vite + React), `widget/` (vanilla JS).
