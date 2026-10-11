# Caseloop

Caseloop helps a business handle customer support from the first message to the final follow-up.
It brings email, website chat, texts, and phone conversations into one place, asks customers
for missing details, and hands cases to your team when a person needs to make a decision.

For example, a customer says their lamp arrived broken. Caseloop checks the order,
asks for photos, follows up if they do not reply, and records the outcome or sends your
team a summary of what needs attention.

## What you can do

- **Manage cases:** see the conversation, attachments, order details, and next steps together.
- **Keep customers updated:** let the agent ask questions and follow up using your business policy.
- **Take over when needed:** reply yourself, resolve a case, or retry a message that failed to send.
- **Understand recurring problems:** the Feedback page groups recent complaints into themes and suggested fixes.
- **Teach the agent:** human replies and decisions become lessons you can review in Setup.

## How a case moves through Caseloop

1. A customer contacts you through email, chat, SMS, a call, or your API integration.
2. Caseloop opens a case and works out what information is needed.
3. The agent checks connected orders and asks the customer for anything missing.
4. If the customer goes quiet, it follows up on your schedule. The default waits are 24 hours,
   then 72 hours, then 7 days between nudges.
5. The case is resolved or handed to a person with its history and a summary. Cases with no
   reply after the follow-up limit are marked abandoned and handed off.

You can change the policy, follow-up schedule, and hand-off email in **Setup**.
The timeline records messages, state changes, and the agent's actions. Email hand-offs,
optional Slack alerts, and a daily digest help your team keep track of open work.

## Connect your support channels

| Channel | How it connects |
|---|---|
| Email | Forward support mail to your business's inbound address through Resend |
| Website chat | Copy the script snippet from Setup into your website |
| SMS and calls | Connect a Dial number to your business |
| Your own app | Create cases with `POST /v1/cases` and receive signed webhooks |

## Orders from anywhere

The agent can only resolve what it can see, so Caseloop takes orders from whatever a business
already has and labels every order with its source (Shopify, Amazon, Your website API, Tracking
page, Forwarded email, CSV import, Pushed via API). Source labels are visible to your team; customer replies focus on the order facts.
Delivery dates help the agent check policy windows.

| Business has… | Connect it by… |
|---|---|
| Shopify | Setup → Order sources → shop domain + Admin API token |
| Their own API or tracking system | Setup → "Your website API": URL pattern with `{order_number}`, optional headers, field mapping. Test it on the Orders page |
| Only a tracking page on their site | Setup → "Tracking page": page URL pattern; the page is read and extracted |
| No API at all | Auto-forward order confirmation emails to `orders@<inbound domain>` (list the store's sending addresses or domains under Orders → "Accept order emails from"; mail from anyone else is ignored), or upload a CSV on the Orders page, or `POST /v1/orders` |
| Amazon | Import a CSV export or forward order emails. Live Selling Partner API lookup is not implemented yet |

## Run it locally

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20.19+ or 22.12+, and Postgres 16
(Homebrew or `docker compose up -d db`).

Start from the repository root. Copy the environment file, then edit `backend/.env`.
Set `CASELOOP_DATABASE_URL` to your local database. For a first run without provider accounts,
set `CASELOOP_AGENT_RUNNER=scripted` and leave `OPENAI_API_KEY`, `RESEND_API_KEY`,
`DIAL_API_KEY`, and both webhook secrets empty. Remove the example key values.

```bash
cp .env.example backend/.env
make install                           # backend deps
make db-create && make migrate         # local Postgres databases + schema
make create-business ARGS="--name 'Acme Lamps' --slug acme --inbound-email support@acme.example --escalation-email ops@acme.example"
make dev                               # http://localhost:8000
```

If you use the Postgres container from this repository, run the database creation command as
`PGUSER=postgres PGPASSWORD=postgres make db-create`. For a local Postgres installation,
use its username and password in both the connection URL and the `psql` environment.

Keep the server running. In another terminal, build the dashboard:

```bash
cd dashboard
npm ci
npm run build
```

Open **http://localhost:8000/dashboard**.

Then:

- Sign in with the `--escalation-email` address (the
  business owner). A single-use link is emailed through Resend; in dev the link is also
  logged and shown on screen, so no mailbox is needed. Owners add teammates under Setup → Team.
  To give someone owner access from the shell:
  `make add-owner ARGS="--slug acme --email you@acme.example"`.
- Chat widget: open `widget/example.html#<widget_public_key>` in a browser.
- Simulate an inbound email without any provider (dev only):

```bash
curl -X POST localhost:8000/dev/simulate/email -H 'Content-Type: application/json' -d '{
  "to": "support@acme.example", "from_email": "jane@example.net", "from_name": "Jane",
  "subject": "Broken lamp", "text": "My lamp arrived cracked. Order #A12345."}'
```

The scheduler runs inside the server, so the agent replies within seconds. With no
`OPENAI_API_KEY`, a deterministic scripted agent is used instead.

`CASELOOP_ENV` defaults to `prod` (no `/dev` endpoints, signed webhooks required); `make dev`
and `.env.example` set `dev` explicitly.

## Demo

```bash
make demo        # scripted agent, fake channels, no network
make demo-live   # real OpenAI model drives the tools; nothing is actually sent
```

## Connect real providers

**Email (Resend):** set `RESEND_API_KEY`, verify a domain, create a Receiving webhook for
`email.received` pointing at `https://<host>/webhooks/resend`, and put its signing secret in
`RESEND_WEBHOOK_SECRET`. Point the business's `inbound_email` at an address on that domain.

**SMS and calls (Dial):** set `DIAL_API_KEY` and give the business its `dial_number_id` and
`dial_phone_number` (Setup, or `PATCH /v1/businesses/me`). With the key set, the app subscribes to
Dial's account event stream at startup, so inbound texts reach Caseloop with no public URL. For
multi-instance deployments or call events, also create a webhook subscription for
`message.received`, `call.ended`, `call.transcribed` at `https://<host>/webhooks/dial` and store
the secret in `DIAL_WEBHOOK_SECRET`; duplicates between the stream and the webhook are ignored.
Set the number's inbound instruction (`dial.set_number_properties`) so inbound calls are answered
by Dial's voice agent with the business's script.

For local testing expose port 8000 with a tunnel such as ngrok.

## API

The interactive API reference is available at **http://localhost:8000/docs** while the server runs.

<details>
<summary>Endpoints, authentication, and request limits</summary>

All business endpoints take `Authorization: Bearer <api_key>` (integrations) or the
dashboard's `caseloop_session` cookie (people) and return `{"data": …}`; errors are
`{"error": {"code", "message"}}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/auth/magic-link`, `/v1/auth/magic-link/exchange`, `/v1/auth/logout` | Email a sign-in link, turn it into a session cookie, sign out |
| GET | `/v1/auth/me` | Signed-in member and their business |
| GET / POST / DELETE | `/v1/team`, `/v1/team/{id}` | List, add (owners), remove (owners) team members |
| GET / PATCH | `/v1/businesses/me` | Policy text, follow-up schedule, hand-off email, webhook URL, Slack alert URL |
| GET | `/v1/cases?state=new,gathering,awaiting_customer&limit=50&offset=0` | List cases |
| POST | `/v1/cases` | Open a case from any source |
| GET | `/v1/cases/{id}` and `/timeline` | Case file and full history |
| POST | `/v1/cases/{id}/messages` | A person replies on the customer's channel |
| POST | `/v1/cases/{id}/messages/{message_id}/resend` | Retry a message the channel rejected; hands the case back to the agent if it escalated over the failure |
| POST | `/v1/cases/{id}/resolve`, `/escalate`, `/run-turn` | Human overrides |
| GET / POST | `/v1/insights/latest`, `/v1/insights/generate` | Insights report |
| GET / POST | `/v1/orders`, `/v1/orders/import` | Search stored orders, push JSON rows, import CSV |
| GET / PUT / POST | `/v1/orders/sources`, `/v1/orders/sources/test` | Configure live sources (PUT: signed-in owners only; secrets come back masked and stay with their source `id`), test a lookup |
| GET | `/v1/cases/{id}/orders` | Orders matching a case from every source |
| GET | `/v1/cases/{id}/attachments/{index}` | Download a customer's photo or PDF (`index` as shown on the timeline) |
| POST | `/v1/chat/sessions`, `/v1/chat/sessions/{token}/messages` | Used by the widget |
| POST | `/v1/chat/sessions/{token}/attachments` | Widget photo upload (multipart, up to 5 images or PDFs of 8 MB; 25 files / 50 MB per case; file bytes must match the declared type) |
| GET | `/v1/chat/sessions/{token}/stream` | Server-sent events for the widget: `message` and `state` events (closed after 10 minutes, the browser reconnects; 3 open streams per chat, 20 per IP) |
| POST | `/webhooks/resend`, `/webhooks/dial` | Provider webhooks |

Rate limits are in-memory per process: 60 requests a minute per API key, dashboard session or
client IP on the business API; 60 a minute per IP and 600 per widget key on the chat API; sign-in
links 10 a minute per IP and 3 per address every 10 minutes. Over the limit is a 429 with
`Retry-After`.

Outbound webhooks (`case.created`, `case.state_changed`) are signed with
`X-Caseloop-Signature: t=<unix>,v1=<hmac-sha256 of "t.body">` using the business's secret.

</details>

## Deploy

The image is built from `backend/Dockerfile` with the repo root as context (it bundles the widget and
the built dashboard) and runs `alembic upgrade head` before starting uvicorn on `$PORT`.

- **Docker Compose:** `docker compose up --build` starts Postgres and the app on port 8000.
- **Fly.io:** `fly launch --copy-config` uses `fly.toml` (one always-on machine, since the scheduler runs in-process; `/data` volume for attachments).
- **Railway:** `railway.json` uses the Dockerfile, runs migrations before each deploy, and health-checks `/healthz`.

Set the secrets from `.env.example` on the host. Rate limits and access logs key on the client IP,
which uvicorn only takes from `X-Forwarded-For` when the connection comes from
`FORWARDED_ALLOW_IPS` (default `127.0.0.1`, so nothing is trusted when the app is exposed
directly). Behind a platform proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address range
(Railway's and Fly's edge proxies are the only way in, so `*` is correct there; for your own
reverse proxy use its IP or CIDR). CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest
against a Postgres service, the dashboard build, and a Docker image build on every push and PR.

## Development and tests

The backend uses FastAPI and PostgreSQL. The dashboard uses React, TypeScript, and Vite;
the chat widget is plain JavaScript. The API and background scheduler run in one process,
with PostgreSQL storing queued jobs.

Run the backend checks from the repository root:

```bash
make test        # pytest against caseloop_test
make lint        # ruff
make typecheck   # mypy
make eval        # 25 scripted customer conversations (backend/evals/cases.yaml), scripted agent
make eval-live   # same cases driven by the real model; costs money, expects 80% to pass
```

Run the dashboard and widget checks after `npm ci`:

```bash
cd dashboard
npm run lint
node --test tests/*.test.mjs ../widget/tests/*.test.cjs
npm run build
```

CI runs backend lint, formatting, type checks, and PostgreSQL tests, plus dashboard and widget
regression tests, the frontend build, and a Docker image build.

The scripted demo and evaluations use an empty scratch database and fake delivery channels.
`make demo-live` and `make eval-live` use your OpenAI key and incur model charges.
Order integrations read order data; recording a case resolution does not itself execute a
refund or create a replacement shipment in your store.

Layout: `backend/app` (FastAPI app: `cases/` state machine and service, `agent/` prompts,
tools, runners, `channels/` adapters, `api/` routers, `scheduler.py`, `insights/`),
`backend/tests`, `dashboard/` (Vite + React), `widget/` (vanilla JS).
