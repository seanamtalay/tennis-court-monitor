#!/usr/bin/env python3
"""Poll rqclubsport.com tennis schedule and notify via Telegram when a court frees up."""

import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCHEDULE_URL = "http://rqclubsport.com/schedule-cus/2"
STATE_FILE = Path(__file__).parent / "state.json"

# Courts that close earlier than the rest — slots starting at/after this hour are invalid.
EARLY_CLOSE_COURTS = {"Court 5", "Court 6", "Court 7"}
EARLY_CLOSE_HOUR = 21

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


def main() -> None:
    html = fetch_html()
    current = parse_available_slots(html)

    old_state = load_state()
    new_state: dict[str, list[list[str]]] = {}
    newly_available: list[str] = []

    for date, slots in current.items():
        slot_keys = sorted(f"{t}|{c}" for t, c in slots)
        new_state[date] = slot_keys
        previously_seen = set(old_state.get(date, []))
        for key in slot_keys:
            if key not in previously_seen:
                time_str, court = key.split("|", 1)
                newly_available.append(f"{date} {time_str} - {court}")

    if newly_available:
        message = "🎾 New open tennis slot(s):\n" + "\n".join(sorted(newly_available))
        print(message)
        send_telegram(message)
    else:
        print("No new available slots.")

    save_state(new_state)


if __name__ == "__main__":
    sys.exit(main() or 0)
