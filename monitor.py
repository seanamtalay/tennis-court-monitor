#!/usr/bin/env python3
"""Poll rqclubsport.com tennis schedule and notify via Telegram when a court frees up."""

import argparse
import json
import os
import re
import sys
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


def fetch_html() -> str:
    resp = requests.get(SCHEDULE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    return resp.text


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
    newly_available: list[str] = []

    within_window = args.ignore_window or in_notify_window(datetime.now(tz=ZoneInfo("UTC")))

    for date, slots in current.items():
        current_keys = set(f"{t}|{c}" for t, c in slots)
        # Drop entries for slots that got booked again, so if they reopen later we re-alert.
        still_notified = set(old_notified.get(date, [])) & current_keys
        diff = current_keys - still_notified

        if diff and within_window:
            for key in sorted(diff):
                time_str, court = key.split("|", 1)
                newly_available.append(f"{date} {time_str} - {court}")
            new_notified[date] = sorted(current_keys)  # mark everything currently open as notified
        else:
            # Outside the notify window (or nothing new): keep the diff pending for next run.
            new_notified[date] = sorted(still_notified)

    if newly_available:
        message = (
            "🎾 New open tennis slot(s):\n"
            + "\n".join(sorted(newly_available))
            + f"\n\nBook here: {SCHEDULE_URL}"
        )
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
