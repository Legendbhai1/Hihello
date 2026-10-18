# Deploying to Render

The bot, the payment callback server and the reconciliation loop run in one
process. Render runs that process as a web service and routes public HTTPS
traffic to it.

## Data backend: use Postgres, not SQLite

Render's container filesystem is **ephemeral**. It is discarded on every deploy,
restart and (on the free plan) after the service idles. A SQLite file stored
there loses every order, payment event and stock item each time.

So the database must be a managed Postgres instance. `render.yaml` creates one
and injects its connection string as `DATABASE_URL`.

The connection string Render provides starts with `postgres://`, which selects
SQLAlchemy's synchronous psycopg2 driver. `app/config.py` rewrites it to
`postgresql+asyncpg://` so the async engine can use it, and `pyproject.toml`
exposes the driver as the `postgres` extra.

`validate_runtime()` refuses to start webhook mode against a SQLite URL, because
that combination silently loses paid orders.

## Where stock lives

Stock is rows in the `stock_items` table, not files. Each row is one deliverable
unit: a license key, an invite link, an account. Columns:

- `product_id` — which product it belongs to
- `payload` — the text delivered to the buyer
- `order_id` — `NULL` until an order consumes it, then the owning order

You add stock through the bot, not the database:

1. Send `/panel` to the bot (you must be in `ADMIN_IDS`).
2. Pick a product.
3. Tap **Add stock** and send one item per line.

Delivery pops the oldest unsold row for the product and marks it sold. When a
product runs out, the bot reports it and the reconciler retries once stock is
refilled.

Because stock is in Postgres, it survives redeploys. It is also shared with any
other instance pointed at the same database, which is what lets you scale past
one process later.

## Steps

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, select the repo. Render reads `render.yaml`
   and creates the web service plus the database.
3. Fill the secrets Render prompts for (`sync: false`):
   - `BOT_TOKEN` — from @BotFather.
   - `PUBLIC_BASE_URL` — `https://<your-service>.onrender.com`.
   - `CRYPTOPAY_TOKEN` / `OXAPAY_API_KEY` — only if you enable those providers.
4. Apply. The first deploy runs `init_models`, which creates the tables.

## After the first deploy

Set the two values that depend on the live URL:

- `PUBLIC_BASE_URL=https://<your-service>.onrender.com`
- `BOT_MODE=webhook`

Webhook mode needs `PUBLIC_BASE_URL` to be `https://` and `WEBHOOK_SECRET` to be
non-empty; `render.yaml` generates the secret. Telegram then posts updates to
`{PUBLIC_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}`.

Check the service is up:

```bash
curl https://<your-service>.onrender.com/health
# {"status":"ok","providers":["stars"]}
```

## Free plan caveats

- The service **sleeps after ~15 minutes of inactivity** and cold-starts on the
  next request. Telegram retries webhooks, so payments still land, but there is
  a delay. OxaPay and Crypto Pay callbacks are retried by those providers too.
- Free Postgres instances expire after 90 days.
- One instance only. The reconciler and polling both assume a single consumer.

## Schema changes

There is no Alembic migration setup. `init_models` only creates missing tables;
it does not alter existing ones. After changing a model, either write the `ALTER
TABLE` yourself or drop and recreate the database, then reseed.
