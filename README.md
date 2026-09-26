# Office Ops Crew

An office request can lead to a quote, a booking, or a payment. Office Ops Crew shows how to let agents do that work without giving the agent that reads a vendor page the authority to spend. A browser worker runs in a disposable, secret-free container; a separate control plane checks the requested item, quantity, vendor, currency, budget, and freeze switch before it calls a payment provider.

Built during Agent Arena, September 26–27, 2026. The planning document was written beforehand; all application code was written at the event. The company, stores, employees, and addresses in the demo are fictional.

## Try the three requests

| Request | What the demo shows |
| --- | --- |
| Berlin is out of oat milk and coffee | Two EUR pending orders, policy decisions, and payment receipts. |
| A new hire starts in Berlin Monday | A welcome kit and a separate team lunch task with a downloadable `.ics` invite. |
| Hoodies for the company | Berlin and San Francisco orders. The Berlin mock page contains an instruction to order 500 instead of 20; the local attack replay shows the policy block and a corrected quote. |

The dashboard makes the distinction between **SIMULATED** local payments and **SUBMITTED_SANDBOX** Airwallex transfers visible. A provider submission is not represented as a settled payment. In local mode, the 500-hoodie proposal is an explicit replay of a compromised Buyer decision, not a claim that a live model obeyed the injection. Live model behavior must be measured separately.

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
| Concierge | Yes | No | No | No |
| Buyer | No | Yes | Yes, in sandbox | No |
| Events | No | No | Fixed fictional venue flow | No |
| Treasurer | No | No | No | Through policy only |

The container/control-plane split and payment checks are implemented. The four roles are logical workflow roles today; the full role-based tool gateway and all model-backed role decisions in the planning document are not yet implemented.

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

The intended deployment is two hosts: a control-plane host for FastAPI, Postgres, and optional Slack Socket Mode; and a sandbox host for `shopnet`, the mock stores, and the disposable Playwright image. `compose.control.yml` publishes the app only on `127.0.0.1:8000`; `compose.sandbox.yml` publishes no store ports. Use a private SSH tunnel or an authenticated exposure layer for the dashboard.

1. Create the two hosts and install Docker Engine with Compose. Keep a network firewall allowing SSH only. The supplied `vultr-setup.sh` is a bootstrap reference; review it before use.
2. Clone the project to each host and build the matching Compose stack. Configure `DOCKER_HOST=ssh://...` on the control host to reach the sandbox host's Docker daemon over SSH.
3. Set `SANDBOX_MODE=docker` and `PLANNER_MODE=vultr` only after a sandbox round-trip and a live model call have been verified. Set a model ID returned by Vultr's `/v1/models` endpoint. The app limits page text to 8,000 characters, output to 512 tokens, and paid inference attempts to `VULTR_MAX_CALLS` (default 100) per database seed.
4. Set `PAYMENT_MODE=airwallex_sandbox` only with a scoped sandbox key and saved fictional sandbox beneficiaries. The payment adapter refuses any host other than `api.sandbox.airwallex.com`. Do not use production credentials.
5. Add `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` to enable the separate Socket Mode worker. A fictional test workspace should be used for the demo.

These are deployment instructions, not a claim that the two hosts or integrations have already been verified. Real Printful quotes and FX conversion are future extensions; no comparison should be presented as a live landed-cost decision until actual product, shipping, tax, and rate inputs are available. Any tax and duty calculations would be simplified estimates.

## Open-source credits

Python; [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [SQLAlchemy](https://www.sqlalchemy.org/), [Psycopg](https://www.psycopg.org/psycopg3/), [Pydantic](https://docs.pydantic.dev/), [HTTPX](https://www.python-httpx.org/), [python-dotenv](https://github.com/theskumar/python-dotenv), [Docker SDK for Python](https://docker-py.readthedocs.io/), [Playwright](https://playwright.dev/python/), [Slack Bolt for Python](https://slack.dev/bolt-python/), [PostgreSQL](https://www.postgresql.org/), and [Docker Compose](https://docs.docker.com/compose/). The sandbox image uses Microsoft's Playwright base image with Chromium.
