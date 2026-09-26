# Orderly DoorDash reuse audit

Checked September 26, 2026. The source project is the local Orderly checkout at
`C:\Users\Small\OneDrive\Documents\ChatGPT\Mikes Hckathin project` (HEAD `5ebaa6b`).
It was inspected read-only. Orderly's README reports one supervised, delivered
DoorDash order; this audit inspected the code, tests, and sanitized visual
receipt, but did not re-query DoorDash or independently confirm delivery.

## Reusable design

- `src/doordash/cli.ts` pins `dd-cli` 0.2.4, uses an argument array rather than
  a shell, gives the token only to the child process, requires JSON output, and
  treats a failed mutation as potentially ambiguous.
- `src/order/doordash-cart-backend.ts` validates cart, destination, fulfillment,
  price, and payment summary before building a canonical preview hash. The
  tests in `test/doordash-order-backend.test.ts` use synthetic provider-shaped
  responses; they are not a live CLI compatibility receipt.
- `src/order/phone-order.ts` binds approval to the preview, rechecks it before
  purchase, consumes one durable purchase run, and holds uncertain outcomes for
  reconciliation instead of retrying a possibly charged order.
- `contracts/order-state-machine.md` describes the transaction states and
  approval boundary. This pattern is relevant to Office Ops Crew's own vendor
  and Airwallex paths even though the DoorDash provider is not usable here.

`crew/doordash.py` implements only the **offline preview validation** portion,
using synthetic JSON shaped like the Orderly test fixtures. It accepts a
supplied cart and quote, checks exact items and destination, rejects restricted
items and excessive totals, and returns a snapshot hash without any address or
card data. It has no CLI runner, credentials, cart mutation, or checkout method;
the project does not invoke it in its office workflows. Its snapshot is not
purchase authorization. Real checkout would need a fresh quote, durable
approval, idempotency/reconciliation, and provider permission.

## Why the live CLI is excluded

The official [DoorDash CLI v0.2.4 release](https://github.com/doordash-oss/doordash-cli/releases/tag/v0.2.4)
offers macOS ARM64 and Linux AMD64 builds and remains waitlist-gated. Its
bundled `LICENSE.txt`, from the [official Linux release archive](https://github.com/doordash-oss/doordash-cli/releases/download/v0.2.4/dd-cli-v0.2.4-linux-amd64.tar.gz),
limits use to a person's own consumer account. Sections 3 and 4.1 bar business
activity, orders on behalf of an entity, and a product or service that relies
on CLI access. Sections 6.2 and 6.3 restrict export, retention, analysis, and
price comparison of CLI data. We verified the archive against the published
SHA-256 `37eec0c72bcb663aaf9759ea098d49d9c02266bb895cbbfbadeae41866608dd4`
before reading its terms; the binary was not executed or installed.

Office Ops Crew's company lunch is an entity/business use, so Orderly's
single-account CLI access cannot be transferred to it. A real DoorDash lane
would require a separate DoorDash-approved business/developer integration and
terms that allow this use. The fictional lunch demo and Airwallex sandbox path
remain separate. DoorDash checkout would charge a saved DoorDash payment method,
not serve as an Airwallex payout rail.
