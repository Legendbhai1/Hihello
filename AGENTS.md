# AGENTS.md

Repository notes for future sessions. Keep this short and factual.

## Project

`cryptoshop-bot` — a Telegram shop bot (aiogram 3 + SQLAlchemy 2 async) selling
digital goods for Telegram Stars, Crypto Pay (@CryptoBot) and OxaPay.

## Commands

```bash
python -m pytest tests/ -q          # full suite, ~3s, 79 tests
python -m ruff check app tests      # lint; must be clean
python -m app.main                  # run the bot (needs a real BOT_TOKEN)
```

There is no separate build step. `pip install -e ".[dev]"` for dev deps.

## Testing conventions

- No mocks of the code under test. Tests use a real SQLite database
  (`tests/conftest.py`), real aiohttp servers on loopback for provider HTTP
  contracts (`tests/test_providers_http.py`), real HMAC signatures, and a
  recording aiogram `BaseSession` double that implements both `make_request`
  and `stream_content` (`tests/test_bot_flows.py`).
- `asyncio_mode = "auto"`, so async tests need no decorator.

## Architecture invariants

- `build_application(settings)` in `app/main.py` wires everything without
  touching the network, so tests can boot it and inspect `AppContext`.
- Routers are **factories** (`build_router()`), not module-level `Router`
  instances. An aiogram `Router` can only be attached to one `Dispatcher`, so
  module-level routers break the second test that builds a dispatcher.
- Idempotency: `record_event` inserts into `payment_events` keyed by
  `(provider, event_id)` inside a SAVEPOINT; an `IntegrityError` means replay.
- Order status transitions use `_cas_status` (atomic UPDATE ... WHERE status IN).
  Never read-then-write order status.
- Money is integer minor units plus a currency exponent. Never use floats.
  `_amount_ok` rejects a provider that reports a different currency.

## Provider gotchas (verified against provider docs)

- **OxaPay webhooks**: `amount` + `currency` are what the *payer sent in crypto*
  (e.g. 10 POL). `value` is that payment's worth in the invoice currency (USD).
  Verify against `value`. `GET /v1/payment/{track_id}` returns the invoice
  currency in `amount`/`currency`, so polling compares `amount` directly.
- **Crypto Pay webhooks**: for fiat invoices `amount` is in the fiat currency.
  Guard on `currency_type == "fiat"` so a crypto invoice amount is never read as
  USD. Signature is `HMAC_SHA256(SHA256(api_token), raw_body)` in the
  `crypto-pay-api-signature` header.
- **OxaPay signature**: `HMAC_SHA512(raw_body, merchant_api_key)` in an `HMAC`
  header. aiohttp lowercases header names, so look up both cases.
- **Telegram Stars**: no HTTP callback; delivery comes from the
  `successful_payment` update. `pre_checkout_query` must answer within 10s.

## Environment

`app/config.py` uses pydantic-settings with `alias=` per field. `ADMIN_IDS`
accepts `"1,2,3"`, `"1; 2"`, `"42"` or a JSON list via a before-validator.
`Settings.validate_runtime()` fails fast on unusable config; webhook mode needs
an `https://` `PUBLIC_BASE_URL` and a non-empty `WEBHOOK_SECRET`.
