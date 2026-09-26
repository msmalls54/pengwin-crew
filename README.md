# Brackenrow Crew

Four Slack agents turn a workplace request into a checked purchase or event task. Concierge takes the request; Buyer or Events inspects a fictional vendor; Treasurer checks the quote and budget before recording a payment result. Brackenrow is a fictional software and design firm with offices in San Francisco and Berlin. Its stores, people, addresses, and transactions are demo data.

The agents run as separate workers behind their own Slack identities in the private [`#brackenrow-ops` channel](https://app.slack.com/client/T0C4K935FHT/C0C4910LVAB) of a separate Brackenrow workspace. Vendor pages open in disposable containers on a second Vultr VM without payment or Slack credentials. The control VM holds the durable task queue, audit trail, budgets, and policy checks. [Slack setup](#connect-slack) and [local setup](#run-locally) are below.

## Talk to the crew

In `#brackenrow-ops`, mention Concierge: **“Can you order 3 oat milk cartons and 2 coffee bags for Berlin?”** Or type `/crew order 3 oat milk cartons and 2 coffee bags for Berlin`. Concierge extracts the stated items, quantities, and office; Buyer checks each fictional store quote; Treasurer checks the budget and records a **simulated** payment. The same English intake handles “Set up a Berlin team lunch for 5 people next Monday.” Missing quantities, unclear offices, and unsupported items get a clarification instead of a guessed order. The four agents report in their own voices in one Slack thread.

English requests are intentionally restricted to the fictional catalog and simulated-payment mode. A model suggestion cannot add an unmentioned item or quantity. This is a demo of the handoff and controls, not an arbitrary vendor checkout.

The three preset shortcuts remain available:

| Request | What the demo shows |
| --- | --- |
| Berlin is out of oat milk and coffee | Two EUR pending orders, policy decisions, and payment receipts. |
| A new hire starts in Berlin Monday | A welcome kit and a separate team lunch task with a downloadable `.ics` invite. |
| Hoodies for the company | Berlin and San Francisco orders. The Berlin mock page contains an instruction to order 500 instead of 20; the local attack replay shows the policy block and a corrected quote. |

The local operator dashboard distinguishes **SIMULATED** payments from **SUBMITTED_SANDBOX** Airwallex transfers. A provider submission is not represented as a settled payment. In the attack replay, the 500-hoodie proposal is an explicit simulation of a compromised Buyer decision, not a claim that the live model obeyed the injection.

Vultr `deepseek-v4.1-flash` parsed three live English requests and checked Buyer quotes. The two Vultr VMs completed a control-to-sandbox browser round trip against the fictional stores. On September 26, 2026, the three preset workflows and three English requests completed from Slack in the Brackenrow workspace. All four roles posted under their own identities; the database holds ten **SIMULATED** payment receipts from those runs. In the hoodie case, the model kept the employee's request for 20; the 500-hoodie block is a labeled deterministic attack replay. A scoped, IP-restricted Airwallex sandbox key read the wallet balance; three fictional beneficiaries were created, and three transfer payloads passed Airwallex validation. One separately approved EUR 19.20 sandbox transfer was submitted through the Treasurer adapter and read back as **PROCESSING**. The [receipt](docs/airwallex-sandbox-receipt.md) distinguishes submission from settlement. The deployed Slack crew remains in simulated-payment mode.

## Where authority lives

```text
Slack Socket Mode or protected demo control
              │
              ▼
Control plane: requests, audit, budgets, policy, payment executor
              │                         │
              │ task data only          └── Airwallex sandbox API
              ▼
Disposable browser/code container ── internal shopnet ── fictional stores
```

The container receives no Vultr, Slack, Airwallex, or Printful credentials. It is read-only, runs as a non-root user with dropped capabilities and resource limits, and reaches only the internal mock-store network. The control plane rejects a sandbox quote that differs from its canonical catalog. The payment executor uses one idempotency key per pending order, reserves budget before submission, and holds uncertain provider outcomes for reconciliation instead of retrying with a new ID.

| Role | Slack | Quote | Browse mock store | Request payment |
| --- | --- | --- | --- | --- |
| Concierge | Inbound and outbound | No | No | No |
| Buyer | Outbound | Yes | Yes, in sandbox | No |
| Events | Outbound | No | Fixed fictional venue flow | No |
| Treasurer | Outbound | No | No | Through policy only |

Each role claims only its assigned job kinds from the durable queue. Treasurer is the only worker given Airwallex credentials when sandbox mode is enabled; Buyer and Events get the remote Docker key. The workers currently share one database login, so application role checks and credential separation do not amount to database-enforced isolation.

## Run locally

Python 3.12 or later is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
# Replace ADMIN_TOKEN with a random value of at least 24 characters.
$env:ADMIN_TOKEN = 'your-long-local-demo-token'
.\.venv\Scripts\python.exe -m uvicorn crew.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, enter the `ADMIN_TOKEN` in the demo control, and run a flow. The default SQLite database, deterministic planner, and simulated payment provider keep this setup local. The app does not send a real order or transfer in the default mode. Run checks with `python -m pytest -q`.

The project `.env` is ignored by Git. Copy only key **names** from `.env.example` into documentation; never commit values. Mutating API endpoints require the admin token. The public dashboard contains only fictional demo data.

## Run on Vultr

The deployed layout has two Silicon Valley hosts: a control VM for Postgres and the role workers, and a sandbox VM for `shopnet`, the mock stores, and disposable Playwright containers. The sandbox publishes no store ports. The control VM has no public app port. Both allow SSH from the operator, while the sandbox also allows the control VM's single IP for remote Docker. The dashboard is a local operator view, not the product surface.

1. Install Docker Engine and Compose from Docker's official Ubuntu packages on both hosts. The supplied `vultr-setup.sh` was not run. Keep host-key pinning and the one-IP SSH firewall rule when reproducing the layout.
2. Build `compose.sandbox.yml` and `office-ops-sandbox:latest` on the sandbox host. Set `DOCKER_HOST=ssh://...` on the control VM with a key restricted to Docker's `system dial-stdio`. Only Buyer and Events mount that key.
3. Use `SANDBOX_MODE=docker` after a sandbox round trip. The deployed workers use `PLANNER_MODE=vultr` with `VULTR_MODEL=deepseek-v4.1-flash`; use an exact model ID from Vultr's `/v1/models` endpoint. The app limits page text to 8,000 characters, output to 512 tokens, and paid inference attempts to `VULTR_MAX_CALLS` (default 1,000). A simulated-demo reset preserves the call counter.
4. Set `PAYMENT_MODE=airwallex_sandbox` only with a scoped sandbox key, saved fictional sandbox beneficiaries, and a reviewed payout plan. The payment adapter refuses any host other than `api.sandbox.airwallex.com`. Do not use production credentials. The live deployment currently stays in `simulated` mode even though its sandbox credentials and beneficiaries are configured.
5. Configure the Slack workers using the steps below in a separate workspace. The current Brackenrow control host is running all five worker processes and Postgres. The two VMs draw on the hackathon's $200 Vultr credit at a quoted combined rate of about $0.06/hour. No automatic shutdown is configured; monitor the credit balance and its expiration. Stopping the guest OS is not a billing safeguard.

Real Printful quotes and FX conversion are future extensions; no comparison should be presented as a live landed-cost decision until actual product, shipping, tax, and rate inputs are available. Any tax and duty calculations would be simplified estimates.

## Connect Slack

Slack is the request and agent conversation surface. The dashboard is an internal operator view. The live deployment uses one private `#brackenrow-ops` channel with four installed bots. To reproduce it, use the [Concierge manifest](slack-app-manifest.yaml), [Buyer manifest](slack-manifests/buyer.yaml), [Events manifest](slack-manifests/events.yaml), and [Treasurer manifest](slack-manifests/treasurer.yaml), and invite all four bots to the channel. Concierge alone receives `/crew` and `@mentions`, using `app_mentions:read`, `chat:write`, and `commands`. The other three bots need only `chat:write` to report their actual backend steps under separate identities. No app needs `chat:write.customize` impersonation permission.

Enable Socket Mode only on Concierge. Generate its app-level `connections:write` token, and put that token plus each bot's `xoxb` token into the ignored `.env` using the `SLACK_...` names in `.env.example`. Set `SLACK_DEMO_CHANNEL_ID` to the ID of `#brackenrow-ops`, `SLACK_ALLOWED_USER_IDS` to the members permitted to run requests, and `SLACK_ADMIN_USER_IDS` to the members permitted to freeze/unfreeze or reset the local simulation. The bot refuses to start without a channel and at least one allowed member. Tokens and IDs never belong in Git or the browser sandbox. [Slack's Socket Mode guide](https://docs.slack.dev/apis/events-api/using-socket-mode/) documents the app-level token and connection.

For the separated worker deployment, supply each role worker only its own bot token and task-specific backend credentials. The Concierge process needs the Socket Mode app token; Buyer, Events, and Treasurer do not receive inbound Slack events. The outbound helper refuses any channel other than the configured demo channel and posts plain text without interpreting model-provided mentions.

Run `python -m crew.slack_bot` locally or `docker compose -f compose.control.yml up -d --build db init concierge concierge-worker buyer events treasurer` on the control host. In the demo channel, mention Concierge with a specific English request or use `/crew order ...`. The `pantry`, `welcome`, and `hoodies` shortcut commands still run the fixed demos. Concierge replies with a run ID promptly; role workers post progress in a thread in the same channel. Slack delivery IDs are claimed before queue submission so a retry cannot enqueue a second payment-capable run. The user allowlist and channel check apply before any submission.

Only configured admins can run `/crew freeze`, `/crew unfreeze`, or `/crew reset-demo`; reset is disabled outside simulated payment mode. A `SIMULATED` outcome is local only. A `SUBMITTED_SANDBOX` Airwallex result means the transfer was submitted, not settled. If a run fails after a provider call, inspect the audit and provider receipt before sending a new request.

## DoorDash reuse boundary

The [Orderly reuse audit](docs/orderly-doordash-reuse.md) maps the prior DoorDash code and includes an offline preview validator for synthetic cart data. The bundled license in the official DoorDash CLI v0.2.4 release restricts the CLI to personal consumer use and bars an office purchasing product relying on it. The CLI is therefore not connected to these flows. A live lunch checkout needs a separately authorized business integration.

## Open-source credits

Python; [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [SQLAlchemy](https://www.sqlalchemy.org/), [Psycopg](https://www.psycopg.org/psycopg3/), [Pydantic](https://docs.pydantic.dev/), [HTTPX](https://www.python-httpx.org/), [python-dotenv](https://github.com/theskumar/python-dotenv), [Docker SDK for Python](https://docker-py.readthedocs.io/), [Playwright](https://playwright.dev/python/), [Slack Bolt for Python](https://slack.dev/bolt-python/), [PostgreSQL](https://www.postgresql.org/), and [Docker Compose](https://docs.docker.com/compose/). The sandbox image uses Microsoft's Playwright base image with Chromium.
