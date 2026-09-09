#!/usr/bin/env python3
"""Poll rqclubsport.com tennis schedule and notify via Telegram when a court frees up."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SCHEDULE_URL = "http://rqclubsport.com/schedule-cus/2"
STATE_FILE = Path(__file__).parent / "state.json"

# Courts that close earlier than the rest — slots starting at/after this hour are invalid.
EARLY_CLOSE_COURTS = {"Court 5", "Court 6", "Court 7"}
EARLY_CLOSE_HOUR = 21

# Only send notifications within this local-time window (Bangkok), every day.
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
NOTIFY_START_HOUR = 9
NOTIFY_END_HOUR = 19  # 7pm

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def fetch_html(retries: int = 3, backoff_seconds: float = 5) -> str:
    """The site's connection is flaky, so retry a few times before giving up."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(SCHEDULE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException:
            if attempt == retries:
                raise
            time.sleep(backoff_seconds)


def parse_available_slots(html: str) -> dict[str, list[tuple[str, str]]]:
    """Return {date: [(time, court_name), ...]} for currently open (bookable) slots."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, list[tuple[str, str]]] = {}

    tabs = soup.select("li a[data-toggle='tab']")
    panes = soup.select(".tab-content .tab-pane")

    for tab, pane in zip(tabs, panes):
        date = tab.get_text(strip=True)
        table = pane.find("table")
        if not table:
            continue

        headers = [th.get_text(strip=True) for th in table.select("thead th")][1:]  # skip "Time"
        available = []

        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            time_text = cells[0].get_text(strip=True)  # e.g. "18 : 00"
            hour = int(re.search(r"\d+", time_text).group())
            time_str = f"{hour:02d}:00"

            for court_name, cell in zip(headers, cells[1:]):
                classes = cell.get("class") or []
                is_available = "td-box" in classes and "td-have" not in classes
                if not is_available:
                    continue
                if court_name in EARLY_CLOSE_COURTS and hour >= EARLY_CLOSE_HOUR:
                    continue
                available.append((time_str, court_name))

        result[date] = available

    return result


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; message would have been:\n" + message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=20,
    )
    resp.raise_for_status()


def in_notify_window(now: datetime) -> bool:
    return NOTIFY_START_HOUR <= now.astimezone(BANGKOK_TZ).hour < NOTIFY_END_HOUR


def is_past(date_str: str, time_str: str, now: datetime) -> bool:
    """Whether a given date (YYYY-MM-DD) + hour (HH:00) slot has already started, Bangkok time."""
    slot_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=BANGKOK_TZ)
    return slot_dt <= now.astimezone(BANGKOK_TZ)


def day_label(date_str: str, now: datetime) -> str:
    """e.g. 'Today (8 Sep 2026)' / 'Tomorrow (9 Sep 2026)' / 'Wed (10 Sep 2026)'."""
    date = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = now.astimezone(BANGKOK_TZ).date()
    days_ahead = (date - today).days
    prefix = {0: "Today", 1: "Tomorrow"}.get(days_ahead, date.strftime("%a"))
    return f"{prefix} ({date.strftime('%-d %b %Y')})"


def format_message(
    all_available: dict[str, list[tuple[str, str]]],
    new_keys: set[str],
    now: datetime,
) -> str:
    lines = ["🎾 <b>Open Tennis Courts</b>", ""]
    for date in sorted(all_available):
        lines.append(f"<b>{day_label(date, now)}</b>")
        for time_str, court in sorted(all_available[date]):
            marker = " 🆕" if f"{date}|{time_str}|{court}" in new_keys else ""
            lines.append(f"• {time_str} — {court}{marker}")
        lines.append("")
    lines.append("🆕 = newly opened since last update")
    lines.append(f"🔗 More details: {SCHEDULE_URL}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending Telegram messages")
    parser.add_argument("--ignore-window", action="store_true", help="Ignore the 9am-7pm Bangkok time gate")
    parser.add_argument("--reset-state", action="store_true", help="Clear saved state before running")
    args = parser.parse_args()

    if args.reset_state:
        save_state({})

    html = fetch_html()
    current = parse_available_slots(html)

    old_notified = load_state()
    new_notified: dict[str, list[str]] = {}
    all_available: dict[str, list[tuple[str, str]]] = {}
    new_keys: set[str] = set()
    has_diff = False

    now = datetime.now(tz=ZoneInfo("UTC"))
    within_window = args.ignore_window or in_notify_window(now)

    for date, slots in current.items():
        current_keys = set(
            f"{t}|{c}" for t, c in slots if not is_past(date, t, now)
        )
        if not current_keys:
            continue
        all_available[date] = [tuple(key.split("|", 1)) for key in current_keys]

        # Drop entries for slots that got booked again, so if they reopen later we re-alert.
        still_notified = set(old_notified.get(date, [])) & current_keys
        diff = current_keys - still_notified

        if diff and within_window:
            has_diff = True
            new_keys |= {f"{date}|{key}" for key in diff}
            new_notified[date] = sorted(current_keys)  # mark everything currently open as notified
        else:
            # Outside the notify window (or nothing new): keep the diff pending for next run.
            new_notified[date] = sorted(still_notified)

    if has_diff:
        message = format_message(all_available, new_keys, now)
        print(message)
        if not args.dry_run:
            send_telegram(message)
    elif not within_window:
        print("Outside notify window (9am-7pm Bangkok) — skipping.")
    else:
        print("No new available slots.")

    save_state(new_notified)


if __name__ == "__main__":
    sys.exit(main() or 0)
