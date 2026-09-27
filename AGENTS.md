# Pengwin Crew: instructions for the next build agent

Read [CURRENT_STATE.md](CURRENT_STATE.md) first. It is the handoff for the overnight build, including what was verified, what is deployed, what remains a demo, and the ordered work. Check the current Git tree, test results, public lab, and live Slack behavior before changing a status claim. Update `CURRENT_STATE.md` whenever a material finding or deployment changes it; do not treat this file or an earlier assistant message as fresh production proof.

## Immediate objective

Give Concierge, Buyer, Events, and Treasurer durable, scoped memory of their tasks and relevant Slack conversation. The current Postgres queue remembers jobs and receipts, but the chat reply path sees one message at a time. The observed failure: Events could not answer “what events do we currently have live,” then failed to connect “on Oct 2nd” to that question even though Pengwin had already published the October 2 Eventbrite page. Repair the factual lookup and conversational continuity, then verify them in the real `#pengwin-ops` channel. Keep every user-facing answer in plain English.

Implement memory as durable application data, not as an unbounded prompt or Slack history scrape. Scope reads by the configured channel and requesting user, associate messages with a Slack thread and Pengwin run, deduplicate Slack deliveries, keep verified provider facts separate from model-generated prose, and minimize retention of personal data and secrets. A model must not infer that a provider action happened merely because a past message requested it. Preserve the existing approval gate and provider idempotency/uncertain-outcome holds. Do not grant additional Slack history scopes simply to make memory work without inspecting the resulting data access.

The acceptance check is concrete: after a restart, Events can answer which Pengwin RSVP events were published, give the October 2 event's actual link and details, handle a follow-up such as “on Oct 2nd” in context, and say when current Eventbrite state has not been freshly checked. A user outside the allowlist or configured channel cannot retrieve another user's private task history. This lookup must not create, republish, or email an event. Also verify Concierge's latest-request lookup and plain-English replies from Buyer and Treasurer.

## Work boundaries

- The four agents live in the **Pengwin** Slack workspace, private `#pengwin-ops`. The small public web lab is required to demonstrate sandbox execution to Challenge 1 judges; it is not the primary product UI.
- Vultr control VM and Serverless Inference are mandatory. Executed code and browsers run in disposable containers on the separate Vultr sandbox VM, with no application credentials in those containers.
- The live Slack purchasing flows use fictional stores and simulated checkouts. The Airwallex key is sandbox-only. Do not describe a demo checkout as a real charge or a submitted sandbox transfer as settled.
- The existing October 2 Eventbrite page is already public. Do not create a duplicate page to test conversation or approval. A Salesforce Park gathering remains a proposal, not a booked venue.
- The personal Orderly DoorDash CLI is **not** connected to Pengwin. The audited CLI license restricts product/business use. A real food order requires an authorized business path or a human checkout handoff; a saved personal card does not solve that boundary.
- Keep credentials in ignored `.env` files and the authorized control VM. Never print, commit, or send tokens to Slack, the public lab, or the sandbox. Do not copy the private SSH key into the repository.
- When a test or deployment fails, record the exact boundary and fix the cause. Do not claim the whole product is ready because a container started or a provider returned HTTP 200.

The source repository is `https://github.com/msmalls54/pengwin-crew`. Local project files are in this directory. The deployment is in `/home/linuxuser/brackenrow` on the Vultr control VM, with `compose.control.yml`; the older remote directory/container prefix is only a compatibility name. The ignored SSH configuration is under the parent task's `work/keys/pengwin-jump-config`. Read `README.md` and `docs/demo-runbook.md` for the product and judge flow. Run the full tests after code changes, then verify the exact requested behavior in Slack and update the handoff before closeout.
