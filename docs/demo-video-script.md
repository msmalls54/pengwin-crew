# Challenge 1 video provenance

The [59-second MP4](../assets/demo/pengwin-challenge1-demo.mp4) is 1920×1080 H.264/AAC. It combines recorded clips from the deployed Slack workers and public judge page. It is edited for time; the twelve-second timeout segment plays at normal speed. Narration is in [demo-narration.txt](demo-narration.txt) and was rendered locally with Microsoft Mark. The source captures remain outside the public repository because they include a private Slack workspace.

## Evidence shown

| Video time | Source | What it shows |
| --- | --- | --- |
| 0–3s | Title card | An automated, bot-authored live worker test. |
| 3–8s | Live Slack thread | Bot-labelled park gathering request; Concierge delegates to Events, Buyer, and Treasurer. |
| 8–13s | Same Slack thread | Events invitation draft, official Printful bottle source, initial 30-bottle estimate, Treasurer's no-payment boundary. |
| 13–18s | Same Slack project after worker restart | Buyer revises quantity to 24 and product-only subtotal to $486.00–$561.84. This is a later message in the same project, not a staged screenshot. |
| 18–26s | Deployed public judge page | Read-only role/action feed, latest project state, estimates separated from simulated transfers and settled spend. |
| 26–32s | Deployed public sandbox | Model-generated `print(19 + 23)` executes and returns stdout `42`; run `38e5323b-0e76-4d88-99fc-0db3ba352c6e`. |
| 32–49s | Deployed public sandbox | The page submits an infinite-loop goal, shows the run in progress, then shows generated Python, exit `124`, and stderr `Execution timed out after 10 seconds`; run `d306749c-ecec-424f-98bb-2d9563e6125e`. |
| 49–55s | Deployed public sandbox | A subsequent run completes with stdout `42`; run `bdb4cb6f-1856-47bc-bc5a-fd5961371cc4`. |
| 55–59s | Boundary card | No venue booking, order, payment, or invitation was made for this event test. The brief policy/sandbox scaffold committed 11 minutes before kickoff is disclosed. |

The bot-authored root Slack run was `6e9e6317-8e40-4d0f-b9ef-640653fa6cf9`, and its post-restart follow-up was `91cf57f1-5d7a-467f-b2e4-303d9f4d1367` on the same project. Both completed with all four role jobs done. The video does not present the bot text as a human-authored conversation.

The source capture showed only the Pengwin demo Slack thread and the public judge page. The judge-page credential field was password-masked. No private address, contact list, token value, or unrelated Slack conversation appears in the final frames. The screenshot segment for spend is a still from the live public page and is treated as recorded evidence, not a live feed update.
