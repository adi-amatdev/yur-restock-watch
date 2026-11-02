#!/usr/bin/env python3
"""Restock watcher for a Shopify product.

Polls Shopify's public product endpoints and sends a Telegram message when a
watched size flips to "available". No third-party dependencies, so it runs as
fast as Python starts.

Usage:
    python3 check.py                 # check + notify
    python3 check.py --dry-run       # print status, never contact Telegram
    python3 check.py --test-notify   # send a test Telegram message and exit
    python3 check.py --chat-id       # print chat ids that have messaged the bot

Env vars:
    PRODUCT_URL        product page URL (default: the YUR trousers)
    SIZES              comma separated sizes to watch (default: 34)
    REMIND_MINUTES     re-alert interval while still in stock (default: 120)
    TELEGRAM_BOT_TOKEN bot token from @BotFather
    TELEGRAM_CHAT_ID   your chat id
    STATE_PATH         where to persist state (default: ./state.json)
    CURRENCY           symbol used in messages (default: ₹)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://yurofficial.com/products/100-linen-denim-trousers-washed-brown",
)
SIZES = [s.strip() for s in os.environ.get("SIZES", "34").split(",") if s.strip()]
REMIND_MINUTES = int(os.environ.get("REMIND_MINUTES", "120"))
STATE_PATH = Path(os.environ.get("STATE_PATH", str(Path(__file__).with_name("state.json"))))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CURRENCY = os.environ.get("CURRENCY", "₹")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- helpers


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> str | None:
    """Shopify .js prices are integers in the smallest unit; .json uses strings."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
        # .json already returns major units (e.g. "1682.00")
        amount = value
    elif isinstance(value, int):
        amount = value / 100
    else:
        amount = float(value)
    if amount <= 0:
        return None
    return f"{CURRENCY}{amount:,.0f}" if amount == int(amount) else f"{CURRENCY}{amount:,.2f}"


def http_get(url: str, tries: int = 3, timeout: int = 20) -> str:
    """GET with a browser-ish header set and simple backoff."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    last_error: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 4xx other than 429 will not get better by retrying.
            if exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:  # noqa: BLE001 - network layer varies
            last_error = exc
        if attempt < tries:
            time.sleep(2 * attempt)
    raise RuntimeError(f"GET {url} failed: {last_error}")


# ---------------------------------------------------------------- product


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    attributes = payload.get("variants") or []
    variants = []
    for variant in attributes:
        variants.append(
            {
                "id": str(variant.get("id", "")),
                "size": str(
                    variant.get("public_title")
                    or variant.get("title")
                    or variant.get("option1")
                    or ""
                ),
                "available": bool(variant.get("available")),
                "price": money(variant.get("price")),
                "compare_at": money(variant.get("compare_at_price")),
            }
        )
    return {
        "title": payload.get("title", ""),
        "handle": payload.get("handle", ""),
        "available": bool(payload.get("available", any(v["available"] for v in variants))),
        "variants": variants,
    }


def fetch_product() -> dict[str, Any]:
    base = PRODUCT_URL.split("?")[0].rstrip("/")
    errors: list[str] = []
    # .js is the cheapest and most reliable; .json is the fallback.
    for suffix in (".js", ".json"):
        try:
            data = json.loads(http_get(base + suffix))
            product = data.get("product", data)
            result = normalize(product)
            if result["variants"]:
                return result
            errors.append(f"{suffix}: no variants in payload")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{suffix}: {exc}")
    raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------- state


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"sizes": {}}


VOLATILE_KEYS = ("last_run_utc", "last_commit_utc")
HEARTBEAT_DAYS = 20


def _comparable(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if k not in VOLATILE_KEYS}


def save_state(state: dict[str, Any]) -> bool:
    """Write state only when something meaningful changed.

    Committing every 5 minutes would bury the repo in noise, so timestamps that
    change on every run are ignored. A heartbeat write every HEARTBEAT_DAYS
    keeps the scheduled workflow from being disabled after 60 days of repo
    inactivity.
    """
    previous = load_state()
    meaningful_change = _comparable(previous) != _comparable(state)
    last_commit = parse_iso(previous.get("last_commit_utc"))
    heartbeat_due = last_commit is None or (now() - last_commit).days >= HEARTBEAT_DAYS

    if not (meaningful_change or heartbeat_due):
        return False

    state["last_commit_utc"] = iso(now())
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return True


# ---------------------------------------------------------------- telegram


def telegram_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(see README for the 2 minute setup)."
        )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.dumps(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Telegram rejected the message (HTTP {exc.code}): {detail}") from None


def product_link(variant_id: str | None = None) -> str:
    url = PRODUCT_URL.split("?")[0].rstrip("/")
    return f"{url}?variant={variant_id}" if variant_id else url


def build_message(product: dict[str, Any], hit: dict[str, Any], sizes_in_stock: list[str]) -> str:
    link = product_link(hit["id"])
    lines = [f"🚨 <b>Size {html.escape(hit['size'])} is back in stock</b>", ""]
    lines.append(f"<b>{html.escape(product['title'])}</b>")

    price_bits = []
    if hit.get("price"):
        price_bits.append(f"<b>{hit['price']}</b>")
    if hit.get("compare_at") and hit.get("compare_at") != hit.get("price"):
        price_bits.append(f"<s>{hit['compare_at']}</s>")
    if price_bits:
        lines.append(" · ".join(price_bits))

    lines.append(f"In stock now: {html.escape(', '.join(sizes_in_stock))}")
    lines.append("")
    lines.append(f"Buy → {link}")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def cmd_chat_id() -> int:
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
        return 1
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    data = json.loads(http_get(url))
    if not data.get("ok"):
        print(f"Telegram error: {data}", file=sys.stderr)
        return 1
    seen: dict[str, str] = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            seen[str(chat["id"])] = name
    if not seen:
        print("No messages found. Open Telegram, message your bot once, then re-run.")
        return 1
    for chat_id, name in seen.items():
        print(f"{chat_id}\t{name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a Shopify product for restocks.")
    parser.add_argument("--dry-run", action="store_true", help="never send Telegram messages")
    parser.add_argument("--test-notify", action="store_true", help="send a test message and exit")
    parser.add_argument("--chat-id", action="store_true", help="print chat ids known to the bot")
    args = parser.parse_args()

    if args.chat_id:
        return cmd_chat_id()

    if args.test_notify:
        telegram_send(
            "✅ <b>Restock watcher is connected</b>\n"
            f"I'll ping you here when size {html.escape(', '.join(SIZES))} is available.\n"
            f"{product_link()}"
        )
        print("Test message sent.")
        return 0

    stamp = iso(now())
    if not args.dry_run and not (BOT_TOKEN and CHAT_ID):
        print(
            "ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. "
            "See README (one-time setup) or run with --dry-run.",
            file=sys.stderr,
        )
        return 1

    print(f"[{stamp}] checking {PRODUCT_URL} for sizes {', '.join(SIZES)}")

    try:
        product = fetch_product()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not fetch product: {exc}", file=sys.stderr)
        return 1

    by_size = {v["size"]: v for v in product["variants"]}
    state = load_state()
    state.setdefault("sizes", {})
    state["last_run_utc"] = stamp
    state["product"] = {
        "title": product["title"],
        "handle": product["handle"],
        "url": PRODUCT_URL.split("?")[0].rstrip("/"),
    }

    sizes_in_stock = [s for s, v in by_size.items() if v["available"]]
    pending: list[tuple[dict[str, Any], str]] = []
    exit_code = 0

    for size in SIZES:
        variant = by_size.get(size)
        if variant is None:
            print(f"  size {size}: NOT FOUND on product page")
            continue

        entry = state["sizes"].setdefault(size, {})
        was_available = bool(entry.get("available", False))
        last_alert = parse_iso(entry.get("last_alert_utc"))
        changed = entry.get("available") is not None and was_available != variant["available"]

        entry["available"] = variant["available"]
        entry["variant_id"] = variant["id"]
        entry["price"] = variant["price"]
        if changed:
            entry["last_change_utc"] = stamp

        status = "IN STOCK" if variant["available"] else "sold out"
        print(f"  size {size}: {status} ({variant['price'] or 'n/a'})")

        if not variant["available"]:
            # Reset so the next restock always alerts, no matter how long ago.
            entry["last_alert_utc"] = None
            continue

        due = last_alert is None or (now() - last_alert).total_seconds() >= REMIND_MINUTES * 60
        if not due:
            mins = int((now() - last_alert).total_seconds() // 60)
            print(f"  size {size}: already alerted {mins}m ago, staying quiet")
            continue

        pending.append((entry, build_message(product, variant, sizes_in_stock or [size])))

    # Never record an alert that did not actually reach Telegram, otherwise a
    # transient send failure would suppress retries for REMIND_MINUTES.
    for entry, text in pending:
        if args.dry_run:
            print("\n--- would send ---\n" + text)
            continue
        try:
            telegram_send(text)
            entry["last_alert_utc"] = stamp
            print("  telegram: alert sent")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR sending alert: {exc}", file=sys.stderr)
            exit_code = 1

    if not args.dry_run:
        changed_state = save_state(state)
        print(f"  state {'updated' if changed_state else 'unchanged'}")
    if not pending:
        print("  no alert needed")

    # Surface a compact status in the Actions run summary.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Restock check · {stamp}\n\n")
            fh.write(f"**{product['title']}**\n\n")
            fh.write("| Size | Status | Price |\n|---|---|---|\n")
            for size, variant in by_size.items():
                flag = "🟢 in stock" if variant["available"] else "🔴 sold out"
                watched = " (watched)" if size in SIZES else ""
                fh.write(f"| {size}{watched} | {flag} | {variant['price'] or '—'} |\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
