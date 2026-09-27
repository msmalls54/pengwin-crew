# Pengwin sandbox execution check

Checked on September 26, 2026, after deploying the updated control workers and sandbox image to the two Vultr VMs.

| Check | Observed result |
| --- | --- |
| Fixed Python calculation in a disposable sandbox | Exit `0`, stdout `42` |
| Python exception in a disposable sandbox | Exit `1`, stderr contained `ValueError: repair me` |
| Infinite loop in a disposable sandbox | Exit `124` after the 10-second code limit |
| Container lifecycle | `0` ephemeral containers before and `0` after the three jobs |
| Live Vultr model to sandbox | Model-generated code hash `8264de697341`, 89 characters; sandbox exit `0`, stdout `42` |
| Public HTTPS demo to sandbox | Run `526a55d6-811a-40ea-b5c4-9d1d7011cf5f` completed; Vultr wrote `print(19 + 23)` (hash `fcbf27b9835b`); remote sandbox stdout was `42` |

The code task is configured with Docker `network=none`, a read-only root filesystem, a 128 MB `/tmp`, one CPU, 512 MB memory, dropped capabilities, and a non-root UID. No Slack, Vultr, Airwallex, Eventbrite, or Luma credential is passed to the sandbox. The verification ran through the deployed Buyer's remote Docker connection; it did not post to Slack or call a payment or calendar provider.

A separate live Vultr inference call drafted a fictional Luma event for October 1, 2026, 12:00–13:00 `Europe/Berlin`, with `guest@example.com`. Its checked snapshot was `2a11071eb26a`. That call created only a draft in memory; no Luma API request, invitation, or email was sent. Luma publishing remains disabled on the deployed control host.
