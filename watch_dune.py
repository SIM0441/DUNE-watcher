#!/usr/bin/env python3
"""Alert when new Dune: Part Three IMAX 70mm screenings appear at Cinema City Flora (Prague).

Polls Cinema City's public quickbook API (the same one the website uses) and
notifies on screenings it hasn't seen before, or ones that come back from sold out.

Notifications:
  - always printed to stdout
  - Telegram, if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (comma-separated) are set;
    each alert is sent TELEGRAM_REPEAT times (default 3), a little apart, so nobody misses it
  - Windows toast + sound when running under WSL (via powershell.exe)

Usage:
  python3 watch_dune.py                 # loop forever
  python3 watch_dune.py --once          # single check (used by GitHub Actions)
  python3 watch_dune.py --test-notify   # verify notifications work
  python3 watch_dune.py --get-chat-ids  # list chats that have messaged the bot
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

API = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
CINEMA_ID = "1052"          # Praha Flora, OC FLORA
FILM_ID = "8105s2r"         # Duna: část třetí
STATE_FILE = Path(os.environ.get("DUNE_STATE_FILE") or Path(__file__).with_name("seen_screenings.json"))
HEADERS = {"User-Agent": "Mozilla/5.0 (dune-imax-watcher)"}
PARAMS = {"attr": "", "lang": "cs_CZ"}


def get(path):
    r = requests.get(f"{API}/{path}", params=PARAMS, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()["body"]


def is_imax_70mm(event):
    return "70-mm" in event["attributeIds"] or event.get("auditoriumTinyName") == "IMAX"


def fetch_screenings():
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [d for d in get(f"dates/in-cinema/{CINEMA_ID}/until/2027-12-31")["dates"] if d >= today]
    screenings = {}
    for d in dates:
        for e in get(f"film-events/in-cinema/{CINEMA_ID}/at-date/{d}")["events"]:
            if e["filmId"] == FILM_ID and is_imax_70mm(e):
                screenings[e["id"]] = {
                    "time": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "soldOut": e.get("soldOut", False),
                    "availability": e.get("availabilityRatio"),
                    "blockOnlineSales": e.get("compositeBookingLink", {}).get("blockOnlineSales", False),
                    "link": e.get("bookingRouterLaunchLink") or e.get("bookingLink"),
                    "attrs": e["attributeIds"],
                }
        time.sleep(0.3)  # be polite
    return screenings


OPEN_BROWSER = False


def notify(title, message, url=None, repeat=None):
    print(f"\n*** {title} ***\n{message}\n", flush=True)

    if OPEN_BROWSER and url and shutil.which("powershell.exe"):
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if shutil.which("powershell.exe"):
        t = title.replace("'", "''")
        m = message.replace("'", "''")[:250]
        ps = (
            "[System.Media.SystemSounds]::Exclamation.Play();"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
            f"$n.ShowBalloonTip(30000,'{t}','{m}',[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 10;$n.Dispose()"
        )
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    if token and chat_ids:
        repeat = repeat or int(os.environ.get("TELEGRAM_REPEAT", "3"))
        text = f"🎬 {title}\n\n{message}"
        for i in range(repeat):
            if i:
                time.sleep(20)
            for chat_id in chat_ids:
                try:
                    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15, json={
                        "chat_id": chat_id, "text": text if i == 0 else f"⏰ Reminder {i + 1}/{repeat}\n{text}",
                        "disable_web_page_preview": True,
                    })
                    if not r.ok:
                        print(f"telegram to {chat_id} failed: {r.text}", file=sys.stderr)
                except requests.RequestException as ex:
                    print(f"telegram to {chat_id} failed: {ex}", file=sys.stderr)


def get_chat_ids():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    chats = {}
    for u in updates.get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name"), f"@{chat['username']}" if chat.get("username") else None]))
    if not chats:
        print("No chats yet - have everyone send /start to the bot (or add it to a group and post a message), then rerun.")
    for cid, name in chats.items():
        print(f"{cid}\t{name}")
    if chats:
        print("\nTELEGRAM_CHAT_IDS=" + ",".join(str(c) for c in chats))


def fmt(s):
    when = datetime.fromisoformat(s["time"]).strftime("%a %d.%m. %H:%M")
    status = "SOLD OUT" if s["soldOut"] else f"avail {s['availability']}"
    if s["blockOnlineSales"]:
        status += ", online sales blocked"
    return f"{when} | {s['auditorium']} | {status}\n  {s['link']}"


def check(state, first_run):
    current = fetch_screenings()
    new = [k for k in current if k not in state]
    # The public API's soldOut flag isn't reliable (it stays False on sold-out shows), so also
    # treat any rise in availabilityRatio as seats being freed (cancellations, expired holds).
    freed = [k for k in current if k in state and not current[k]["soldOut"] and (
        state[k]["soldOut"] or (current[k]["availability"] or 0) > (state[k]["availability"] or 0))]

    if first_run and new:
        print("Baseline - already-listed IMAX 70mm screenings (no alert):")
        for k in sorted(new, key=lambda k: current[k]["time"]):
            print("  " + fmt(current[k]))
    else:
        if new:
            lines = [fmt(current[k]) for k in sorted(new, key=lambda k: current[k]["time"])]
            notify(f"DUNE 3 IMAX 70mm @ Flora: {len(new)} new screening(s)!", "\n".join(lines), current[new[0]]["link"])
        if freed:
            lines = [f"{fmt(current[k])}\n  availability {state[k]['availability']} -> {current[k]['availability']}"
                     for k in sorted(freed, key=lambda k: current[k]["time"])]
            # Single message: freed seats go fast and can flicker, so don't spam reminders.
            notify(f"DUNE 3 IMAX 70mm @ Flora: seats freed up - go now!", "\n".join(lines),
                   current[freed[0]]["link"], repeat=1)

    state.update(current)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return len(current)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--interval", type=int, default=180, help="seconds between checks (default 180)")
    ap.add_argument("--alert-existing", action="store_true",
                    help="on first run, alert about already-listed screenings too")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--get-chat-ids", action="store_true")
    ap.add_argument("--open-browser", action="store_true", help="open the booking page on alert (Windows/WSL)")
    args = ap.parse_args()

    global OPEN_BROWSER
    OPEN_BROWSER = args.open_browser

    if args.get_chat_ids:
        return get_chat_ids()

    if args.test_notify:
        os.environ["TELEGRAM_REPEAT"] = "1"
        notify("Dune watcher test", "If you see this, notifications work.",
               "https://www.cinemacity.cz/films/duna-cast-treti/8105s2r")
        time.sleep(3)
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_run = not state and not args.alert_existing

    while True:
        try:
            n = check(state, first_run)
            first_run = False
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ok - {n} IMAX 70mm screening(s) tracked", flush=True)
        except Exception as ex:  # keep running through network hiccups
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] error: {ex}", file=sys.stderr, flush=True)
            if args.once:
                sys.exit(1)  # fail the GitHub Actions run so GitHub emails you
        if args.once:
            break
        time.sleep(args.interval + random.uniform(0, 30))


if __name__ == "__main__":
    main()
