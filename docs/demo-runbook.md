# Pengwin Challenge 1 live demo

Show a real request in the private `#pengwin-ops` Slack channel, then show the [public activity page](https://104-156-229-63.sslip.io) as a read-only record of the crew's saved work. Mike recorded the source clips and then asked Codex to assemble a shorter narrated demo from them. Keep the voiceover and final edit grounded in those real recordings.

## Before recording

- A labeled live smoke test at 10:21:56 a.m. PDT on September 27 used the exact opening prompt below and received substantive Concierge, Events, Buyer, and Treasurer replies within 13 seconds. Mike then wrote his own **fresh top-level Slack request at 10:28:52 a.m.**; all four roles completed by 10:29:07. Use that clean owner-written thread for the recording.
- Use Mike's signed-in Slack account. Post message 1 as a new top-level message; send messages 2–5 as replies in that same thread, one at a time after the preceding response finishes. Do not record the old, crowded assistant-operated test thread as a customer walkthrough.
- Before showing the website, confirm its featured project displays the same venue, bottle count, and four agent actions from Mike's new thread. The public page was explicitly switched to that owner-written project at about 10:31 a.m. It shows a selected saved project; it does not automatically follow whichever Slack thread is open.
- Keep the private demo token, private Slack contact details, delivery address, and credentials out of the recording. The token is only for opening protected sandbox traces.

## What Mike types in Slack

1. **Concierge, new top-level message:** `@Pengwin Concierge Plan a meetup for 30 local AI founders at Salesforce Park about a month from now, starting around 5 p.m. for 90 minutes. Draft invitation copy, source 24 custom water bottles, and have Treasurer review the budget. Keep the plan in this thread.`

   Show Concierge creating one saved event plan and assigning venue/date research to Events, bottle sourcing to Buyer, and budget review to Treasurer. The response should name what the crew found and what decision Mike should make next.

2. **Events, reply in that thread:** `@Pengwin Events Show me the date options, the official venue inquiry route, and the invitation draft. What do you need me to decide next?`

   Show date choices, the official Salesforce Park permit or inquiry route, target audience, and invitation copy. Call the venue **unconfirmed** until there is a reservation receipt.

3. **Buyer, reply in that thread:** `@Pengwin Buyer What did you source for 24 bottles, what is the current product estimate, and what do you need for a checkout-ready quote?`

   Show the sourced product link, quantity, per-item estimate, and product-only total. Ask for the variant, artwork, and destination needed to calculate shipping and tax. This is sourcing and a purchase handoff, not an order.

4. **Treasurer, reply in that thread:** `@Pengwin Treasurer Review the bottle estimate for this meetup against our swag budget. Does it fit, and what final total do you need before approval?`

   Show the saved reason for the expense, the estimate, and the missing final checkout total. Treasurer should distinguish an estimate from a charged payment.

5. **Concierge, reply in that thread:** `@Pengwin Concierge What is the latest saved project in this thread?`

   Show Concierge recalling the same event, date window, audience, bottle quantity, work by all four agents, and the next decision. This demonstrates memory without re-entering the plan.

## What to show on the judge page

Reload the [public page](https://104-156-229-63.sslip.io) after Mike's new project is selected. Start with **What the crew remembers** and the four-role activity timeline. Point to the saved event facts, sourced bottle estimate, and each agent's action. The page is a privacy-safe log of recorded work, not the place where the agents plan the event. The estimate is separate from an actual charge, and model-call counts are usage counters rather than a billed amount.

The executable sandbox is separate from the customer-facing activity page at [the Challenge 1 technical proof page](https://104-156-229-63.sslip.io/sandbox). It is optional in the short event-planning video but remains available for judges. To show it, run `Calculate 19 plus 23 and print the answer.` in **Sandbox lab** and inspect the generated code and output `42` in the protected trace. The earlier successful post-timeout run was `8bd19e20-f8bd-4b20-87e0-b3d68ef74b6b` (exit 0, stdout 42). The durable web counter was 21/30 after testing, so avoid repeated practice runs.

The page pins a containment receipt. Its protected trace for run `817ca2ca-f238-4b68-8b62-0fe12f4d5f98` shows **HELD**, exit **124**, and worker stderr `Execution timed out after 10 seconds.` If reproducing, choose **Timeout containment** and inspect the actual result before calling it a timeout; a softer prior prompt stopped itself. The verified 124 run and following calculation left zero disposable sandbox containers.

Do not claim that this gathering has a confirmed venue, Eventbrite publication, purchase, payment, mailing list import, or sent invitation. The [official TJPA permit route](https://www.tjpa.org/permits-reservations) is a source, not a booking receipt. The Printful estimate does not include a selected variant, shipping, tax, artwork, or a checkout total. Brave Web/Place search has no configured key in the live worker, so venue search may be unavailable; do not describe an unverified provider lookup as a live search.

## Existing evidence and submission links

The [assistant-operated signed-in-user test thread](https://app.slack.com/archives/C0C4910LVAB/p1790500067228739) proved a four-agent Salesforce Park plan, readback after restart, and budget reasoning before this live recording. Codex sent those messages through Mike's signed-in account; Mike did not personally type them. It contains an earlier global-ledger mix-up and a provenance correction, so it is evidence for engineering review rather than the recommended recording thread. A separate [bot-labelled automated thread](https://app.slack.com/archives/C0C4910LVAB/p1790495631139539) shows restart and quantity-amendment behavior.

Events' separate saved October 2 online event was published earlier only after exact-draft admin approval. A read-only question and the follow-up “And on Oct 2nd?” checked its Eventbrite status without creating another event. [That free RSVP page](https://www.eventbrite.com/e/pengwin-safe-ai-agents-live-online-demo-tickets-2002368493065) is unlisted; do not approve or republish a duplicate while recording.

The [submission slides](https://github.com/msmalls54/pengwin-crew/blob/master/assets/deck/pengwin-agent-arena-submission.pdf) and earlier [59-second video on the judge site](https://104-156-229-63.sslip.io/static/pengwin-challenge1-demo.mp4) are existing artifacts; the same MP4 is [archived in GitHub](https://github.com/msmalls54/pengwin-crew/blob/master/assets/demo/pengwin-challenge1-demo.mp4). The earlier video is not the requested new live walkthrough. Verify any submission link anonymously before using it.

The first Git commit at 11:19 a.m. PDT September 26 was a small local policy/sandbox scaffold, before the organizer's 11:30 a.m. start. Vultr inference, the two-VM deployment, Slack/Eventbrite integration, and executable proof followed kickoff. September 27 work added scoped durable projects and conversation memory, read-only provider status, coordinated planning, and the judge activity feed. Keep that chronology accurate in any pitch or submission update.
