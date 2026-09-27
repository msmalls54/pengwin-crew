# Eventbrite public registration: verified boundary

Pengwin uses Eventbrite for free public RSVP pages. The user approved a dedicated Pengwin Events application and its API terms on September 26, 2026. Its private token is stored in the ignored project `.env`; never put the token in Git, a Slack message, the web process, or a sandbox. The application is attached to the Pengwin owner's Eventbrite organization.

The private token passed `GET /v3/users/me/` and `GET /v3/users/me/organizations/` with HTTP 200. The adapter then created two **private, unlisted drafts** and free RSVP ticket classes through the live Eventbrite API:

| Draft event ID | Free ticket class ID | Observed state |
| --- | --- | --- |
| `2002364784974` | `3455541084` | `draft`, `listed=false`, capacity 25 |
| `2002364922385` | `3455541270` | `draft`, `listed=false`, capacity 25 |

These were integration probes. Neither was published, advertised, or charged. They are not a finished Pengwin event. Reconcile or remove them from the Eventbrite organizer account before creating a real event with the same title.

The local Slack flow requires a future event time, exact RSVP capacity, and an HTTPS meeting link already present in the request. It displays a snapshot of the proposed title, dates, location, description, and capacity. An allowed admin must approve that exact snapshot in private `#pengwin-ops`. Events then creates a private draft, creates a free ticket class, publishes once, and reads the event back to verify the public URL. A timeout at any provider mutation holds the run for operator review; it never automatically repeats a potentially completed submission. A test also verifies this hold and preserves the draft/ticket IDs after an ambiguous publish.

The current release is deployed on the control VM with `EVENTBRITE_ENABLED=true`. The Events worker authenticated to `GET /v3/users/me/` after deployment. A live Slack request created a reviewable snapshot (run `ae190f8b-5bfb-4daa-a5c4-fc271d62ad1f`, snapshot `d7dcffc5b19c`), and `/crew reject` kept it unpublished.

The first approved live run, `6a8cd6b1-e29e-4f67-b19c-6bb8d9a1de68`, used snapshot `db50961d32c0`. Events created Eventbrite event `2002368493065` and free ticket class `3455545102` with capacity 40, published it, and read back `live` status. [The registration page](https://www.eventbrite.com/e/pengwin-safe-ai-agents-live-online-demo-tickets-2002368493065) opens to a visitor without organizer sign-in and offers a free RSVP checkout. It is an **online demo on October 2, 2026, 5–6 PM Pacific**, separate from the proposed Salesforce Park gathering. No individual invitation email was sent.

The first version of the listing cut the long meeting URL because Eventbrite summaries are capped at 140 characters. The live listing was edited to show the complete link, and the draft adapter now preserves the full link within that limit or holds before creating a draft if the link itself is too long. The public page was reopened and checked after the correction.

For a real event, use an online meeting link for now. In-person venues require a separately configured `EVENTBRITE_VENUE_ID` and exact `EVENTBRITE_VENUE_LABEL`. This flow opens public registration but does not send individual guest invitations, discover invitees, order food, or charge a card.

For another event, ask Events in Slack for a specific name, start/end time and timezone, HTTPS meeting link, and free RSVP capacity. Check the draft, then approve its snapshot in Slack. Treat the returned URL as live only after Events verifies the provider's `live` state and an independent browser visit confirms the registration form works.
