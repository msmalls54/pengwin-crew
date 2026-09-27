# Pengwin Crew

## Run office operations from one Slack channel

Pengwin brings event coordination, office snacks, swag sourcing, and budget decisions into the Slack thread where the team already works. Ask Concierge once; Events, Buyer, and Treasurer each take their part and return their work to the same conversation. The crew saves the project, so the next question starts with the plan already in hand. San Francisco and Berlin work in their own USD and EUR budgets.

**[Watch the one-minute demo](https://104-156-229-63.sslip.io/static/pengwin-challenge1-59s.mp4)** · **[See the saved agent work](https://104-156-229-63.sslip.io/)** · [View the submission deck](assets/deck/pengwin-agent-arena-submission.pdf)

In the recorded Slack run, one request became a Salesforce Park event proposal, an invitation draft, a sourced estimate for 24 water bottles, and a budget review. Follow-up questions to the individual agents brought back the same saved project after a restart. [Run the Slack walkthrough](docs/demo-runbook.md).

## Plan an event in Slack

Tell Concierge: **“Plan a Pengwin event at Salesforce Park in about a month, get 24 water bottles, and draft invites for local AI founders.”** Concierge creates one project and hands off the work. Events proposes October 27–29, finds the official park inquiry route, and drafts the event description and invitation. Buyer checks Printful's catalog and estimates **$486–$561.84** for 24 bottles. Treasurer checks that estimate against the saved **$1,100 swag allocation** and identifies the exact delivered quote needed for approval. Ask any role a follow-up in the same Slack thread and it recalls the plan.

If the time, capacity, bottle artwork, delivery destination, or audience is missing, the crew asks for it in Slack and keeps its work linked to the same project. A revised quantity replaces the earlier checkout handoff. The Printful figure is a product estimate; Buyer needs the exact variant, shipping, tax, and artwork for a delivered checkout quote.

## One crew across offices and currencies

The same Slack workflow handles a Berlin pantry request in **EUR** and a San Francisco swag request in **USD**. Each office has its own budget and currency. Buyer gathers the quote; Treasurer checks the office allocation and the matching-currency Airwallex wallet balance before a payment submission. The Airwallex adapter uses that wallet currency as the source for a local payout in the recipient's currency: an EUR order requests an EUR transfer from the EUR wallet. The verified integration read a sandbox wallet balance and submitted a **EUR 19.20** Berlin transfer, which Airwallex reported as **PROCESSING**. See the [Airwallex receipt](docs/airwallex-sandbox-receipt.md).

This gives a distributed team one place to request supplies, review spend, and revisit the reason for an order. The deployed Slack crew currently records checkouts in simulated-payment mode; the separately approved Airwallex sandbox transfer demonstrates the provider handoff. Pengwin's offices, stores, and transaction examples are fictional demo data.

## What powers each handoff

| Service | Job in Pengwin |
| --- | --- |
| **Slack** | The home for requests, agent replies, approvals, and follow-up questions. |
| **Vultr Inference** | DeepSeek V4.1 Flash interprets requests and proposes agent tasks; policy checks decide what can run. |
| **Vultr Cloud Compute** | Hosts the crew and a separate disposable sandbox for browser and code execution. |
| **Postgres** | Saves project facts, conversation context, job results, budgets, and audit records across restarts. |
| **Eventbrite API** | Creates an approved public RSVP page and reads back its status through the Events worker. |
| **Printful catalog** | Supplies publisher-listed water-bottle products and prices for Buyer to source. |
| **Airwallex sandbox API** | Reads currency wallet balances and accepts policy-reviewed test transfers through Treasurer. |

For [Challenge 1](https://104-156-229-63.sslip.io/sandbox), Vultr inference also wrote a Python calculation. A disposable, networkless container returned `42`, contained an infinite loop at ten seconds, and cleaned up afterward. [Inspect the execution receipt](docs/sandbox-execution-receipt.md).

Events can also answer “What events do we currently have live?” and a same-thread “on Oct 2nd.” It reads saved Pengwin publications and checks current status through the credential-holding Events worker. If Eventbrite cannot be reached, it says the page was published by Pengwin and that the current status was not checked. The lookup never publishes or emails anything.

For external facts, all four roles share a capped, read-only Brave research service. Search results are leads until checked against a publisher page; the raw search response is not stored. With no Brave key configured, agents say live search is unavailable. The judge feed excludes Slack text, contact details, addresses, and credentials; it separates estimates, simulated checkouts, sandbox transfers, and model-call counts.

The anonymous activity endpoint is an explicit demo setting (`PUBLIC_JUDGE_FEED=true` on the current control host) and defaults off elsewhere. It aggregates fictional demo records; disable it or add tenant-scoped data selection before putting real customer work in this database.

## Talk to the crew

In `#pengwin-ops`, mention Concierge: **“Can you order 3 oat milk cartons and 2 coffee bags for Berlin?”** Or mention Buyer directly with a simple order. Concierge extracts the stated items, quantities, and office; Buyer checks each fictional store quote; Treasurer checks the budget and records a **demo checkout**. Mention Events for a team lunch or an approval-gated event draft. Missing quantities, unclear offices, and unsupported items get a clarification instead of a guessed order. The four agents report in their own voices in one Slack thread. `/crew` remains available for shortcuts and run controls.

The fictional checkout remains restricted to its demo catalog and simulated-payment mode. For unsupported goods such as office chairs, Buyer can return a read-only sourcing proposal; it does not turn a search result into an order. A model suggestion cannot add an unmentioned item or quantity.

For an open-ended calculation or small data task, use `/crew code <plain-English goal>` or ask Concierge to calculate or analyze something. Vultr inference writes a Python program; Buyer runs it in a fresh container on the sandbox VM with no network or credentials, records stdout/stderr and a code hash, and gets one repair attempt after an execution error. This is real code execution, but its inputs are limited to the text of the Slack request and the sandbox has only the Python standard library. It cannot browse external sites, access private files, contact people, or make purchases.

Events can prepare a free public registration page from a plain-English request that includes the name, date, start/end time, time zone, online meeting link, and capacity. Concierge uses Vultr inference to extract a draft, verifies that the link and RSVP capacity came from the request, and shows the details in Slack. An approved admin replies in that draft's thread with `@Pengwin Events approve this event` or `@Pengwin Events cancel this draft`. The thread binds the decision to the exact saved draft; `/crew review`, `/crew approve`, and `/crew reject` remain available as operator fallbacks. The entire request, review, decision, and result remain in Slack. After approval, the Events worker creates an Eventbrite draft and free ticket, publishes the page, and reports its verified URL. Each provider mutation is attempted once; an uncertain outcome is held for reconciliation.

The Eventbrite account, application key, organization ID, draft creation, free ticket creation, publication, and live readback have been verified against the provider. In Slack, Pengwin honored a rejection without publishing, then an approved run created [a real 40-person free RSVP page for the October 2 online demo](https://www.eventbrite.com/e/pengwin-safe-ai-agents-live-online-demo-tickets-2002368493065). A visitor without organizer sign-in can open the page and select a free ticket. The private key belongs only in the Events control worker, never in a sandbox. The public RSVP route does not email named guests or recruit attendees. The older Luma path remains disabled because its API requires a paid Plus calendar. The Salesforce Park gathering is a separate proposal and has not been permitted or published.

The three preset shortcuts remain available:

| Request | What the demo shows |
| --- | --- |
| Berlin is out of oat milk and coffee | Two EUR pending orders, policy decisions, and payment receipts. |
| A new hire starts in Berlin Monday | A welcome kit and a separate team lunch task with a downloadable `.ics` invite. |
| Hoodies for the company | Berlin and San Francisco orders. The Berlin mock page contains an instruction to order 500 instead of 20; the local attack replay shows the policy block and a corrected quote. |

The local operator dashboard distinguishes **SIMULATED** payments from **SUBMITTED_SANDBOX** Airwallex transfers. A provider submission is not represented as a settled payment. In the attack replay, the 500-hoodie proposal is an explicit simulation of a compromised Buyer decision, not a claim that the live model obeyed the injection.

Vultr `deepseek-v4.1-flash` parsed live English requests and checked Buyer quotes. The two Vultr VMs completed a control-to-sandbox browser round trip against the fictional stores. The three preset workflows and English requests completed from Slack in the Pengwin workspace. All four roles posted under their own identities, and direct mentions now work for each role. In the hoodie case, the model kept the employee's request for 20; the 500-hoodie block is a labeled deterministic attack replay. A scoped, IP-restricted Airwallex sandbox key read the wallet balance; three fictional beneficiaries were created, and three transfer payloads passed Airwallex validation. One separately approved EUR 19.20 sandbox transfer was submitted through the Treasurer adapter and read back as **PROCESSING**. The [receipt](docs/airwallex-sandbox-receipt.md) distinguishes submission from settlement. The deployed Slack crew remains in demo-checkout mode. A separate [sandbox receipt](docs/sandbox-execution-receipt.md) records a live model-to-container calculation, stderr capture, an infinite-loop timeout, and container cleanup.

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

The container receives no Vultr, Slack, Airwallex, Eventbrite, Luma, or Printful credentials. It is read-only, runs as a non-root user with dropped capabilities and resource limits. Browser jobs reach only the internal mock-store network; code jobs have no network at all. The control plane rejects a sandbox quote that differs from its canonical catalog. The payment executor uses one idempotency key per pending order, reserves budget before submission, and holds uncertain provider outcomes for reconciliation instead of retrying with a new ID.

| Role | Slack | Quote | Browse mock store | Request payment |
| --- | --- | --- | --- | --- |
| Concierge | Direct mentions and outbound coordination | No | No | No |
| Buyer | Direct mentions and outbound task reports | Yes | Yes, in sandbox; isolated Python tasks | No |
| Events | Direct mentions and outbound task reports | No | Fixed fictional venue flow | No |
| Treasurer | Direct mentions and outbound budget reports | No | No | Through policy only |

Each role claims only its assigned job kinds from the durable queue. Treasurer is the only worker given Airwallex credentials when sandbox mode is enabled; Buyer and Events get the remote Docker key. The workers currently share one database login, so application role checks and credential separation do not amount to database-enforced isolation.

A worker sweep handles claims interrupted for more than 15 minutes. Read-only research and status work may retry once; steps that could dispatch, execute code, buy, publish, or pay are held for review. A late worker cannot overwrite a newer claim. If a Slack intake claim is left pending and Slack never redelivers, the user must resend the request; the stored conversation turn is redacted and cannot safely recreate it.

## Run locally

Python 3.12 or later is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
# Replace ADMIN_TOKEN and WEB_DEMO_TOKEN with different random values of at least 24 characters.
$env:ADMIN_TOKEN = 'your-long-local-demo-token'
$env:WEB_DEMO_TOKEN = 'a-different-long-local-demo-token'
.\.venv\Scripts\python.exe -m uvicorn crew.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, enter the `ADMIN_TOKEN` in the operator control, and run a flow. The `WEB_DEMO_TOKEN` grants only the sandbox code lab; it cannot read the office audit or invoke payment workflows. The default SQLite database, deterministic planner, and simulated payment provider keep this setup local. The app does not send a real order or transfer in the default mode. Run checks with `python -m pytest -q`.

The project `.env` is ignored by Git. Copy only key **names** from `.env.example` into documentation; never commit values. Office workflow mutations and audit reads require the admin token. The browser lab accepts a separate, capped demo token.

## Run on Vultr

The deployed layout has two Silicon Valley hosts: a control VM for Postgres, a [public judge-facing browser lab](https://104-156-229-63.sslip.io), and the Slack role workers; and a sandbox VM for `shopnet`, mock stores, and disposable Playwright/Python containers. The sandbox publishes no store ports. HTTPS reaches only the control VM through its own firewall group; the sandbox remains on its SSH-only group. On September 26, 2026, a request through the public URL caused Vultr inference to write `print(19 + 23)`, the remote sandbox container executed it, and the public API returned `42` with the code hash and stdout. The separate demo token can start a code run and inspect it by run ID; office audits and receipts require the operator token. Share the demo token privately with judges, never in Git or a public URL.

1. Install Docker Engine and Compose from Docker's official Ubuntu packages on both hosts. The supplied `vultr-setup.sh` was not run. Keep host-key pinning and the one-IP SSH firewall rule when reproducing the layout.
2. Build `compose.sandbox.yml` and `office-ops-sandbox:latest` on the sandbox host. Set `DOCKER_HOST=ssh://...` on the control VM with a key restricted to Docker's `system dial-stdio`. Only Buyer and Events mount that key.
3. Use `SANDBOX_MODE=docker` after a sandbox round trip. The deployed workers use `PLANNER_MODE=vultr` with `VULTR_MODEL=deepseek-v4.1-flash`; use an exact model ID from Vultr's `/v1/models` endpoint. The app limits page text to 8,000 characters, output to 512 tokens, and paid inference attempts to `VULTR_MAX_CALLS` (default 1,000). A simulated-demo reset preserves the call counter.
4. Set `PAYMENT_MODE=airwallex_sandbox` only with a scoped sandbox key, saved fictional sandbox beneficiaries, and a reviewed payout plan. The payment adapter refuses any host other than `api.sandbox.airwallex.com`. Do not use production credentials. The live deployment currently stays in `simulated` mode even though its sandbox credentials and beneficiaries are configured.
5. Configure the Slack workers using the steps below in a separate workspace. The current Pengwin control host is running the web process, four Slack listeners, four task workers, Postgres, and Caddy at `https://104-156-229-63.sslip.io`. The web API queues fixed demos and code tasks through the same workers, so the app process never runs generated code. The ignored `.env` holds the demo token. The two VMs draw on the hackathon's $200 Vultr credit at a quoted combined rate of about $0.06/hour. No automatic shutdown is configured; monitor the credit balance and its expiration. Stopping the guest OS is not a billing safeguard.

Real Printful quotes and FX conversion are future extensions; no comparison should be presented as a live landed-cost decision until actual product, shipping, tax, and rate inputs are available. Any tax and duty calculations would be simplified estimates.

## Connect Slack

Slack is the request, update, and approval surface. The browser lab exists to show judges the execution loop required by Challenge 1. The live deployment uses one private `#pengwin-ops` channel with four installed bots. To reproduce it, use the [Concierge manifest](slack-app-manifest.yaml), [Buyer manifest](slack-manifests/buyer.yaml), [Events manifest](slack-manifests/events.yaml), and [Treasurer manifest](slack-manifests/treasurer.yaml), and invite all four bots to the channel. All four receive `app_mentions:read` and `chat:write`; Concierge alone also receives `commands` for `/crew`. No app needs `chat:write.customize` impersonation permission.

The four locally supplied penguin avatars are sized for Slack app icons. The Concierge icon is live in Slack. These user-supplied images are kept out of the source repository; teams deploying their own crew should upload icons they have rights to use. Slack's workspace display name is Pengwin.

Enable Socket Mode and the `app_mention` event on all four apps. Generate each app's `connections:write` token, and put those plus each bot's `xoxb` token into the ignored `.env` using the `SLACK_...` names in `.env.example`. Set `SLACK_DEMO_CHANNEL_ID` to the ID of `#pengwin-ops`, `SLACK_ALLOWED_USER_IDS` to the members permitted to run requests, and `SLACK_ADMIN_USER_IDS` to the members permitted to freeze/unfreeze or reset the local simulation. Each listener refuses to start without a channel and at least one allowed member. Tokens and IDs never belong in Git or the browser sandbox. [Slack's Socket Mode guide](https://docs.slack.dev/apis/events-api/using-socket-mode/) documents the app-level token and connection.

For the separated worker deployment, supply each role worker only its own bot token and task-specific backend credentials. Each chat listener receives only its own Socket Mode app token and bot token. The outbound helper refuses any channel other than the configured demo channel and posts plain text without interpreting model-provided mentions.

Run `python -m crew.slack_bot` locally or `docker compose -f compose.control.yml up -d --build db init concierge concierge-worker buyer events treasurer buyer-chat events-chat treasurer-chat` on the control host. In the demo channel, mention a specialist directly or mention Concierge with a request involving several roles. The `pantry`, `welcome`, and `hoodies` shortcut commands still run the fixed demos. The addressed bot acknowledges the request in ordinary English; role workers report in the requester's original Slack thread. Ask Concierge “what happened with my last request?” or use `/crew latest` for progress without copying a run ID. A Slack delivery claim, run, dispatch job, and audit entry are written in one transaction, so a retry cannot enqueue a second payment-capable run. The user allowlist and channel check apply before any submission.

Only configured admins can run `/crew freeze`, `/crew unfreeze`, or `/crew reset-demo`; reset is disabled outside simulated payment mode. A `SIMULATED` outcome is local only. A `SUBMITTED_SANDBOX` Airwallex result means the transfer was submitted, not settled. If a run fails after a provider call, inspect the audit and provider receipt before sending a new request.

To enable public registration, set `EVENTBRITE_PRIVATE_TOKEN` and `EVENTBRITE_ORGANIZATION_ID` for the Events worker, then set `EVENTBRITE_ENABLED=true` for Concierge after deployment. Use an HTTPS online meeting link in the request; physical events also require a separately configured Eventbrite venue ID and exact venue label. Review the complete draft in Slack before approving. The Eventbrite token is never committed to Git or sent to a browser sandbox. See [Eventbrite integration status](docs/eventbrite-integration.md).

## DoorDash reuse boundary

The [Orderly reuse audit](docs/orderly-doordash-reuse.md) maps the prior DoorDash code and includes an offline preview validator for synthetic cart data. The bundled license in the official DoorDash CLI v0.2.4 release restricts the CLI to personal consumer use and bars an office purchasing product relying on it. The CLI is therefore not connected to these flows. A live lunch checkout needs a separately authorized business integration.

The existing personal DoorDash account has a saved payment method. A real personal checkout would charge that method; Pengwin's Airwallex connection is a sandbox and cannot reimburse a real charge. No live DoorDash order or reimbursement was made by this project.

## Open-source credits

Python; [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [SQLAlchemy](https://www.sqlalchemy.org/), [Psycopg](https://www.psycopg.org/psycopg3/), [Pydantic](https://docs.pydantic.dev/), [HTTPX](https://www.python-httpx.org/), [python-dotenv](https://github.com/theskumar/python-dotenv), [Docker SDK for Python](https://docker-py.readthedocs.io/), [Playwright](https://playwright.dev/python/), [Slack Bolt for Python](https://slack.dev/bolt-python/), [PostgreSQL](https://www.postgresql.org/), and [Docker Compose](https://docs.docker.com/compose/). The sandbox image uses Microsoft's Playwright base image with Chromium.
