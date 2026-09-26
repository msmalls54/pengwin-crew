# Brackenrow Airwallex sandbox check

On September 26, 2026, Brackenrow submitted one operator-approved **sandbox** transfer for **EUR 19.20** to the fictional Kaffee Kontor Demo GmbH beneficiary. Airwallex returned transfer ID `b7346a53-ae10-4a38-8da0-663c6bb3db8d`. A subsequent read of that exact transfer returned **PROCESSING** at 20:48 UTC. This confirms provider submission, not settlement or delivery of goods.

The Brackenrow payment record is `2bfde6e6-66c7-447e-bb8c-c2a0ca0b90e4`; its status is `SUBMITTED_SANDBOX`. Re-running the integration check read the same payment and transfer IDs without creating a second transfer. The live Slack workflows remain in **simulated-payment mode**.

Before submission, the scoped IP-restricted key read the sandbox balance, three fictional beneficiaries were created, and Airwallex's transfer validation API accepted the exact EUR 19.20 payload. No credentials or bank account details are included here.
