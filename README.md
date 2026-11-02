# YUR restock watcher

Watches **size 34** of the [100% linen denim trousers — washed brown](https://yurofficial.com/products/100-linen-denim-trousers-washed-brown)
and pings Telegram the moment it is back in stock.

Runs on GitHub Actions every 5 minutes, so nothing needs to stay powered on.

## How it works

The store is Shopify, so instead of scraping HTML the script reads
`https://yurofficial.com/products/<handle>.js`. That payload carries a per-size
`available` flag, the exact variant id to open, and the price — no browser, no
selectors to break when the theme changes. `/products/<handle>.json` is used as
a fallback, and each request is retried with backoff.

Alert rules:

| Situation | Behaviour |
|---|---|
| Size 34 goes sold out → in stock | Alert immediately |
| Still in stock | Remind at most every 2 hours (`REMIND_MINUTES`) |
| Goes sold out again | Silence, and reset so the next restock alerts again |
| Fetch fails | Run fails, GitHub emails the repo owner |

State lives in `state.json` and is committed back to the repo only when
something meaningful changes (plus a heartbeat every 20 days, which stops GitHub
from disabling the schedule after 60 days of inactivity).

## One-time setup

### 1. Create a Telegram bot (about 2 minutes)

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name.
2. Copy the token it returns (`123456789:AA...`).
3. Open your new bot's chat and send it any message (e.g. `hi`), otherwise it can't write to you.

### 2. Add the two repo secrets

```bash
export TELEGRAM_BOT_TOKEN='paste-token-here'

# Find your chat id: message the bot first, then
python3 check.py --chat-id        # prints: 123456789   your_username

gh secret set TELEGRAM_BOT_TOKEN --repo adi-amatdev/yur-restock-watch --body "$TELEGRAM_BOT_TOKEN"
gh secret set TELEGRAM_CHAT_ID  --repo adi-amatdev/yur-restock-watch --body "<chat id>"
```

### 3. Smoke test

```bash
gh workflow run check.yml --repo adi-amatdev/yur-restock-watch
gh run watch --repo adi-amatdev/yur-restock-watch
```

You should get a "Restock watcher is connected" message in Telegram. To test the
real alert path without waiting for a restock, run locally with
`SIZES=34 REMIND_MINUTES=0` — the second run re-alerts because the reminder
window is zero.

## Local use

```bash
python3 check.py --dry-run     # print status, never message Telegram
python3 check.py               # real check + alert
python3 check.py --test-notify # send a test message
```

No third-party packages are required; only Python 3.9+.

## Configuration

All optional, set as env vars (workflow file or shell):

| Variable | Default | Meaning |
|---|---|---|
| `PRODUCT_URL` | the YUR trousers | Any Shopify product URL |
| `SIZES` | `34` | Comma separated sizes, e.g. `32,34` |
| `REMIND_MINUTES` | `120` | Re-alert interval while still in stock |
| `CURRENCY` | `₹` | Symbol used in messages |
| `STATE_PATH` | `./state.json` | Where state is persisted |

## Caveats worth knowing

- GitHub's cron minimum is 5 minutes and scheduled runs can start 5–15 minutes
  late at peak times. For a sold-out-in-minutes drop this can mean missing the
  first batch — the alert still fires while stock lasts.
- The repo is **public** so Actions minutes are unlimited. On a private repo,
  every run bills a minimum of 1 minute and a 5-minute schedule would exceed the
  free 2,000 minutes/month. If you need it private, change the cron to
  `*/30 * * * *` (~1,440 minutes/month).
- Schedules only run on the default branch.
