# Challenge 1 submission receipt and fields

Submitted September 27, 2026 at about 2:52 a.m. PDT (09:52 UTC) through the signed-in organizer form at https://cerebralvalley.ai/e/vultr-the-agent-arena/hackathon/submit . The form showed **“Project submitted successfully!”**. At about 10:55 a.m. PDT, the existing entry was updated with the owner-recorded, narrated video. At about 11:14 a.m. PDT, it was updated again with the 59-second branded cut and value-first project story. The form showed **“Project updated successfully!”**, and [Your Submission](https://cerebralvalley.ai/e/vultr-the-agent-arena/hackathon/projects) displayed the new video, summary, GitHub, slides, live URL, and Challenge 1 category. No immutable submission ID was shown. The organizer guide says submissions are due at **12:00 p.m. PDT September 27**.

| Field | Entry |
| --- | --- |
| Team name | Pengwin Crew |
| One-line summary | One Slack channel for events, office snacks, swag, and budgets—coordinated by four agents with shared memory on Vultr. |
| Public GitHub | https://github.com/msmalls54/pengwin-crew |
| Slides | https://github.com/msmalls54/pengwin-crew/blob/master/assets/deck/pengwin-agent-arena-submission.pdf |
| One-minute video | https://104-156-229-63.sslip.io/static/pengwin-challenge1-59s.mp4 |
| Live URL | https://104-156-229-63.sslip.io |
| Hosting platform | Vultr Cloud Compute: control and isolated sandbox VMs. |
| Problem statement | 1. Blast Radius Zero: Safe Agent Execution on Vultr |
| NetBird | No |
| Partner technologies | Vultr Cloud Compute and Vultr Inference; Slack; Eventbrite; Airwallex sandbox. |

## Description

Pengwin puts event coordination and office operations in one Slack channel. In the recorded live test, Mike asked Concierge to plan a Salesforce Park meetup for 30 founders, source 24 water bottles, draft invitations, and review the swag budget. Concierge saved one project and delegated the work. Events proposed October 27–29 and found the official park inquiry route. Buyer checked Printful and estimated $486–$561.84 for 24 bottles. Treasurer reviewed that estimate against the recorded $1,100 allocation and identified the delivered total needed for approval. Follow-up questions to each agent recalled the same saved project. The public page shows their work and shared memory. Slack is the workspace; Vultr Cloud Compute and DeepSeek V4.1 Flash power the crew; Eventbrite API supports approved RSVP publishing; Airwallex connects currency-specific wallet balances to local-currency payouts. For Challenge 1, the separate `/sandbox` page shows Vultr-generated code running on an isolated, networkless VM with a ten-second limit, cleanup, and a subsequent successful run. A small sandbox scaffold predates kickoff; the deployed integrations and event workflow were built after kickoff.

## Problem and outcome

Event teams lose context across tools, and unconstrained agent execution can reach credentials or infrastructure. Pengwin keeps work in Slack, records scoped project facts in Postgres, shows a safe handoff rather than claiming unsupported bookings or purchases, and proves that model-generated code runs with network and resource limits in a disposable sandbox. The recorded sequence produced `42`, contained an infinite loop with exit `124` after ten seconds, then produced `42` again.

## Private judge access

The saved-project page and video open anonymously. The Challenge 1 execution proof is separately available at https://104-156-229-63.sslip.io/sandbox . Running a new sandbox task requires `WEB_DEMO_TOKEN`; it is not in Git, the video, or these submission fields. The signed-in form has no private credentials field and no private digital organizer channel has been verified. Give the token to judges in person during the live demo, or through a private organizer channel only if one is verified later.
