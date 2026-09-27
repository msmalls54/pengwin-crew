# Challenge 1 submission receipt and fields

Submitted September 27, 2026 at about 2:52 a.m. PDT (09:52 UTC) through the signed-in organizer form at https://cerebralvalley.ai/e/vultr-the-agent-arena/hackathon/submit . The form showed **“Project submitted successfully!”** and navigated to [Your Submission](https://cerebralvalley.ai/e/vultr-the-agent-arena/hackathon/projects), where **Pengwin Crew** and the GitHub, video, slides, live URL, Challenge 1 category, and other fields below were visible. The organizer page did not display an immutable submission ID. Its guide says submissions are due at **12:00 p.m. PDT September 27**.

| Field | Entry |
| --- | --- |
| Team name | Pengwin Crew |
| One-line summary | Plan an event in Slack while four agents coordinate, remember decisions, and prove safe execution on Vultr. |
| Public GitHub | https://github.com/msmalls54/pengwin-crew |
| Slides | https://github.com/msmalls54/pengwin-crew/blob/master/assets/deck/pengwin-agent-arena-submission.pdf |
| One-minute video | https://104-156-229-63.sslip.io/static/pengwin-challenge1-demo.mp4 |
| Live URL | https://104-156-229-63.sslip.io |
| Hosting platform | Vultr Cloud Compute: control and isolated sandbox VMs. |
| Problem statement | 1. Blast Radius Zero: Safe Agent Execution on Vultr |
| NetBird | No |
| Partner technologies | Vultr Cloud Compute and Vultr Inference; Slack; Eventbrite; Airwallex sandbox. |

## Description

Pengwin is a Slack-first event crew with Concierge, Events, Buyer, and Treasurer. A test sent by Codex through Mike's signed-in Slack account asks for a Salesforce Park gathering, 24 water bottles, and invitation drafts. The crew proposes dates, the official venue permit inquiry route, an official Printful product-only estimate, and a budget review. In a separate bot-authored test, a worker restart and same-thread quantity change show durable project recall. Direct Buyer and Treasurer follow-ups in the signed-in-user test also recall the 24-bottle estimate without claiming payment; an Events follow-up checks the saved October 2 RSVP page through Eventbrite. No venue booking, Eventbrite publication for the new gathering, order, payment, or invitation send is claimed. The public read-only feed shows role steps and keeps estimates separate from actual charges. For Challenge 1, Vultr inference writes Python; a disposable, networkless container on a separate Vultr VM executes it, enforces a ten-second timeout, is removed, and allows the next run to succeed. A small policy/sandbox scaffold was committed 11 minutes before the 11:30 a.m. September 26 kickoff; the deployed integrations and this event flow were built after kickoff. Brave search is implemented but has no configured key in this demo.

## Problem and outcome

Event teams lose context across tools, and unconstrained agent execution can reach credentials or infrastructure. Pengwin keeps work in Slack, records scoped project facts in Postgres, shows a safe handoff rather than claiming unsupported bookings or purchases, and proves that model-generated code runs with network and resource limits in a disposable sandbox. The recorded sequence produced `42`, contained an infinite loop with exit `124` after ten seconds, then produced `42` again.

## Private judge access

The activity feed and video open anonymously. Running a new sandbox task requires the separate `WEB_DEMO_TOKEN`; it is not in Git, the video, or these submission fields. The signed-in submission form has no private credentials field, and no private digital organizer channel has been verified. Give the token to judges in person during the live demo, or through a private organizer channel only if one is verified later.
