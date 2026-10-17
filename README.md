# cryptoshop-bot

A Telegram shop bot that sells digital goods for **Telegram Stars**, **Crypto Pay
(@CryptoBot)** and **OxaPay**. Built with aiogram 3 and SQLAlchemy 2 (async).

The bot, the payment callback server and the reconciliation loop run in a single
process and share one event loop.

## What it does

- Browse a catalog, pick a product, choose a payment method.
- Telegram Stars: in-chat `sendInvoice` in XTR, `pre_checkout_query` validation,
  `successful_payment` delivery, refunds via `refundStarPayment`.
- Crypto Pay: hosted invoice via `createInvoice`, webhook verified with the
  `crypto-pay-api-signature` header, status polling via `getInvoices`.
- OxaPay: hosted invoice, `HMAC`-signed webhook, status polling via
  `/v1/payment/{track_id}`.
- Automatic delivery of a stock item once payment is confirmed, with a retry path
  when stock runs out.

## Correctness guarantees

These are the properties the tests pin down, because they are what separate a demo
from something that can take real money.

- **Replay-safe webhooks.** Every notification is recorded in `payment_events`,
  keyed by `(provider, event_id)`. A duplicate insert hits a unique constraint
  inside a SAVEPOINT and is reported as a replay, so the notification is never
  applied twice and the caller's transaction survives.
- **Single delivery under concurrency.** Order state changes go through an atomic
  compare-and-swap `UPDATE ... WHERE status IN (open states)`, not a
  read-then-write. Concurrent webhook retries cannot both win, so a paid order
  yields exactly one delivered item.
- **Exact money.** Amounts are integers in the currency's smallest unit and are
  compared as such. Floats are never used, and a provider that reports a different
  currency than we billed is rejected rather than trusted.
- **Signature verification.** An unsigned or tampered callback is rejected with a
  non-2xx response before any order state changes, so the provider retries.
- **Lost webhooks recover.** The reconciler re-polls open orders against the
  provider and expires ones that were never paid.

## Layout

```
app/
  config.py              environment-driven settings and startup validation
  main.py                entrypoint; build_application() wires everything
  bot/
    dispatcher.py        bot + dispatcher construction, router registration
    keyboards.py         inline keyboards
    texts.py             user-facing copy
    middlewares.py       one DB session per update
    routers/
      shop.py            catalog browsing
      payments.py        checkout, Stars pre-checkout, successful_payment
      orders.py          buyer order history
      admin.py           operator commands
  db/
    models.py            User, Product, StockItem, Order, PaymentEvent
    session.py           engine / session factory / schema init
  providers/
    base.py              provider protocol and normalised types
    stars.py             Telegram Stars
    cryptopay.py         Crypto Pay API
    oxapay.py            OxaPay merchant API
    registry.py          provider wiring
  services/
    money.py             exact decimal helpers
    catalog.py           product and stock access
    orders.py            order lifecycle, idempotency, fulfillment
    checkout.py          create order + provider invoice
    notifier.py          buyer/admin notifications from webhook context
  web/
    server.py            callback endpoints and the Telegram webhook
    reconciler.py        background status reconciliation
tests/                   real-DB and real-HTTP tests (no mocks)
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env`:

- `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
- `ADMIN_IDS` — your numeric Telegram user id (comma-separated).
- `PUBLIC_BASE_URL` — the public HTTPS URL that reaches this process. Crypto Pay
  and OxaPay deliver callbacks here, so it must be internet-reachable. For local
  work use a tunnel (`cloudflared`, `ngrok`) and paste its HTTPS URL.
- Enable at least one provider. Stars works with no extra credentials.

Run it:

```bash
python -m app.main
```

`BOT_MODE=polling` (default) is for local development; `BOT_MODE=webhook` is for
production and registers `{PUBLIC_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}`
with Telegram. Webhook mode requires an `https://` `PUBLIC_BASE_URL` and a
`WEBHOOK_SECRET`.

The process refuses to start if no provider is enabled or the configuration is
inconsistent, and it logs the reason.

## Provider setup

**Telegram Stars** — nothing to configure. Stars payments need no provider token.
Digital goods only; see Telegram's terms.

**Crypto Pay** — open [@CryptoBot](https://t.me/CryptoBot) → Crypto Pay → Create
App, copy the API token into `CRYPTOPAY_TOKEN`. For testnet use
`CRYPTOPAY_BASE_URL=https://testnet-pay.crypt.bot` with an app from
[@CryptoTestnetBot](https://t.me/CryptoTestnetBot). Set `CRYPTOPAY_WEBHOOK_SECRET`
to add a secret path segment to the callback URL; the HMAC signature is verified
either way.

**OxaPay** — in the OxaPay dashboard generate a Merchant API Key and set
`OXAPAY_API_KEY`. Set `OXAPAY_SANDBOX=true` to create sandbox invoices.

Point each provider's webhook/callback URL at:

- Crypto Pay: `{PUBLIC_BASE_URL}/webhooks/cryptopay[/<CRYPTOPAY_WEBHOOK_SECRET>]`
- OxaPay: `{PUBLIC_BASE_URL}/webhooks/oxapay`

`GET /health` reports the enabled providers.

## Operating the shop

Products and stock are managed with admin commands (only `ADMIN_IDS` may use
them):

```
/addproduct sku | Title | stars_price | usd_price
/setstock sku            (then payloads, one per line; or reply to a message)
/products                list products with stock and status
/stats                   order counts by status
/retry <order_id>        retry delivery for a paid order that had no stock
/refund <order_id>       refund a Telegram Stars order
```

Each unit of stock is one deliverable payload (a key, a link, an account). A paid
order claims exactly one unit. If a product sells out after payment, the order
stays `paid` and admins are alerted; add stock and run `/retry`.

OxaPay and Crypto Pay refunds are done from the provider dashboard. The bot only
refunds Stars, because that is the only provider with a refund API here.

## Tests

```bash
pytest
```

The suite uses a real SQLite database and real HTTP servers on loopback. There
are no mocks of the code under test: the webhook tests drive the actual aiohttp
app with real HMAC signatures, the bot tests run the real dispatcher with a
recording Telegram transport, and the provider tests hit a live local HTTP
server to assert the exact request that goes out.

## Notes

- SQLite is the default (`DATABASE_URL`). For production use Postgres:
  `postgresql+asyncpg://user:pass@host/db` (install the `postgres` extra).
- Schema is created on startup. For real deployments, move to a migration tool.
- Money columns are integers; `amount_str` keeps the exact decimal string handed
  to the provider.
