# Eventbrite public registration: verified boundary

Pengwin uses Eventbrite for free public RSVP pages. The user approved a dedicated Pengwin Events application and its API terms on September 26, 2026. Its private token is stored in the ignored project `.env`; never put the token in Git, a Slack message, the web process, or a sandbox. The application is attached to the Pengwin owner's Eventbrite organization.

The private token passed `GET /v3/users/me/` and `GET /v3/users/me/organizations/` with HTTP 200. The adapter then created two **private, unlisted drafts** and free RSVP ticket classes through the live Eventbrite API:

| Draft event ID | Free ticket class ID | Observed state |
| --- | --- | --- |
| `2002364784974` | `3455541084` | `draft`, `listed=false`, capacity 25 |
| `2002364922385` | `3455541270` | `draft`, `listed=false`, capacity 25 |

These were integration probes. Neither was published, advertised, or charged. They are not a finished Pengwin event. Reconcile or remove them from the Eventbrite organizer account before creating a real event with the same title.

The local Slack flow requires a future event time, exact RSVP capacity, and an HTTPS meeting link already present in the request. It displays a snapshot of the proposed title, dates, location, description, and capacity. An allowed admin must approve that exact snapshot in private `#pengwin-ops`. Events then creates a private draft, creates a free ticket class, publishes once, and reads the event back to verify the public URL. A timeout at any provider mutation holds the run for operator review; it never automatically repeats a potentially completed submission. A test also verifies this hold and preserves the draft/ticket IDs after an ambiguous publish.

The current release is deployed on the control VM with `EVENTBRITE_ENABLED=true`. The Events worker authenticated to `GET /v3/users/me/` after deployment. A live Slack request created a reviewable snapshot (run `ae190f8b-5bfb-4daa-a5c4-fc271d62ad1f`, snapshot `d7dcffc5b19c`), and `/crew reject` kept it unpublished. No public page has been published through Pengwin, so the final publish/readback path is still unverified against Eventbrite.

For a real event, use an online meeting link for now. In-person venues require a separately configured `EVENTBRITE_VENUE_ID` and exact `EVENTBRITE_VENUE_LABEL`. This flow opens public registration but does not send individual guest invitations, discover invitees, order food, or charge a card.

Once the release is live, ask Events in Slack for a specific named event, start/end time and timezone, HTTPS meeting link, and free RSVP capacity. Check the draft, then approve its snapshot in Slack. Treat the returned URL as live only after Events verifies the provider's `live` state and an independent browser visit confirms the registration form works.
