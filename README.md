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

## Orders from anywhere

The agent can only resolve what it can see, so Caseloop takes orders from whatever a business
already has and labels every order with its source (Shopify, Amazon, Your website API, Tracking
page, Forwarded email, CSV import, Pushed via API). The agent names the source when it uses the
facts ("I found order 1234 in Shopify, delivered 12 Sep") and uses delivery dates for policy windows.

| Business has… | Connect it by… |
|---|---|
| Shopify | Setup → Order sources → shop domain + Admin API token |
| Their own API or tracking system | Setup → "Your website API": URL pattern with `{order_number}`, optional headers, field mapping. Test it on the Orders page |
| Only a tracking page on their site | Setup → "Tracking page": page URL pattern; the page is read and extracted |
| No API at all | Auto-forward order confirmation emails to `orders@<inbound domain>`, or upload a CSV on the Orders page, or `POST /v1/orders` |
| Amazon | Setup → Amazon (Selling Partner API credentials); CSV export works today |

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
  `http://localhost:8000/dashboard`, and sign in with the `--escalation-email` address (the
  business owner). A single-use link is emailed through Resend; in dev the link is also
  logged and shown on screen, so no mailbox is needed. Owners add teammates under Setup → Team.
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

All business endpoints take `Authorization: Bearer <api_key>` (integrations) or the
dashboard's `caseloop_session` cookie (people) and return `{"data": …}`; errors are
`{"error": {"code", "message"}}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/auth/magic-link`, `/v1/auth/magic-link/exchange`, `/v1/auth/logout` | Email a sign-in link, turn it into a session cookie, sign out |
| GET | `/v1/auth/me` | Signed-in member and their business |
| GET / POST / DELETE | `/v1/team`, `/v1/team/{id}` | List, add (owners), remove (owners) team members |
| GET / PATCH | `/v1/businesses/me` | Policy text, follow-up schedule, hand-off email, webhook URL |
| GET | `/v1/cases?state=open,gathering&limit=&offset=` | List cases |
| POST | `/v1/cases` | Open a case from any source |
| GET | `/v1/cases/{id}` and `/timeline` | Case file and full history |
| POST | `/v1/cases/{id}/messages` | A person replies on the customer's channel |
| POST | `/v1/cases/{id}/resolve`, `/escalate`, `/run-turn` | Human overrides |
| GET / POST | `/v1/insights/latest`, `/v1/insights/generate` | Insights report |
| GET / POST | `/v1/orders`, `/v1/orders/import` | Search stored orders, push JSON rows, import CSV |
| GET / PUT / POST | `/v1/orders/sources`, `/v1/orders/sources/test` | Configure live sources, test a lookup |
| GET | `/v1/cases/{id}/orders` | Orders matching a case from every source |
| GET | `/v1/cases/{id}/attachments/{index}` | Download a customer's photo or PDF (`index` as shown on the timeline) |
| POST | `/v1/chat/sessions`, `/v1/chat/sessions/{token}/messages` | Used by the widget |
| POST | `/v1/chat/sessions/{token}/attachments` | Widget photo upload (multipart, up to 5 images or PDFs of 8 MB) |
| GET | `/v1/chat/sessions/{token}/stream` | Server-sent events for the widget: `message` and `state` events |
| POST | `/webhooks/resend`, `/webhooks/dial` | Provider webhooks |

Outbound webhooks (`case.created`, `case.state_changed`) are signed with
`X-Caseloop-Signature: t=<unix>,v1=<hmac-sha256 of "t.body">` using the business's secret.

## Deploy

The image is built from `backend/Dockerfile` with the repo root as context (it bundles the widget and
the built dashboard) and runs `alembic upgrade head` before starting uvicorn on `$PORT`.

- **Docker Compose:** `docker compose up --build` starts Postgres and the app on port 8000.
- **Fly.io:** `fly launch --copy-config` uses `fly.toml` (one always-on machine, since the scheduler runs in-process; `/data` volume for attachments).
- **Railway:** `railway.json` uses the Dockerfile, runs migrations before each deploy, and health-checks `/healthz`.

Set the secrets from `.env.example` on the host. CI (`.github/workflows/ci.yml`) runs ruff, mypy,
pytest against a Postgres service, the dashboard build, and a Docker image build on every push and PR.

## Develop

```bash
make test        # pytest against caseloop_test
make lint        # ruff
make typecheck   # mypy
```

Layout: `backend/app` (FastAPI app: `cases/` state machine and service, `agent/` prompts,
tools, runners, `channels/` adapters, `api/` routers, `scheduler.py`, `insights/`),
`backend/tests`, `dashboard/` (Vite + React), `widget/` (vanilla JS).
