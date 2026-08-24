"""GroupMe record alerts.

The nightly sync snapshots the record book before rebuilding it and diffs
after: a new holder posts a RECORD BROKEN message to the league GroupMe, a
holder beating their own mark posts RECORD EXTENDED. Needs the GROUPME_BOT_ID
environment variable (a GitHub Actions secret); without it, messages just
print to the log.

Test the hookup:  GROUPME_BOT_ID=<bot id> python3 -m mtalga.alerts test
"""

from __future__ import annotations

import os
import sqlite3
import time

import requests

API = "https://api.groupme.com/v3/bots/post"
SITE = "makethealgreatagain.com"
MAX_PER_RUN = 8


def snapshot(conn: sqlite3.Connection) -> dict:
    try:
        return {(r["category"], r["scope"]): dict(r) for r in conn.execute("SELECT * FROM records_book")}
    except sqlite3.OperationalError:
        return {}


def diff_messages(conn: sqlite3.Connection, before: dict) -> list[str]:
    if not before:
        return []  # first ever run — nothing to compare against
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM owners")}
    who = lambda s: names.get(s, s or "?")
    msgs = []
    for key, new in snapshot(conn).items():
        old = before.get(key)
        if old is None:
            continue  # newly added record category — not a broken record
        try:
            rel = abs((new["value"] or 0) - (old["value"] or 0)) / max(abs(old["value"] or 1.0), 1.0)
        except (TypeError, ZeroDivisionError):
            rel = 1.0
        det = f" ({new['detail']})" if new.get("detail") and new["detail"] != "all-time" else ""
        if new["owner_slug"] != old["owner_slug"]:
            msgs.append(
                f"🚨 RECORD BROKEN — {new['category']}: {who(new['owner_slug'])} now holds it "
                f"at {new['display']}{det}. Previous: {who(old['owner_slug'])}, {old['display']}."
            )
        elif new["display"] != old["display"] and rel >= 0.01:
            msgs.append(
                f"📈 RECORD EXTENDED — {new['category']}: {who(new['owner_slug'])} pushes their "
                f"own record to {new['display']}{det} (was {old['display']})."
            )
    return msgs


def post(text: str) -> bool:
    bot_id = os.environ.get("GROUPME_BOT_ID")
    if not bot_id:
        print(f"[alerts] (no GROUPME_BOT_ID set) {text}")
        return False
    r = requests.post(API, json={"bot_id": bot_id, "text": text[:990]}, timeout=15)
    print(f"[alerts] sent ({r.status_code}): {text[:80]}")
    return r.status_code < 300


def send_all(msgs: list[str]) -> None:
    for m in msgs[:MAX_PER_RUN]:
        post(m)
        time.sleep(1)
    if len(msgs) > MAX_PER_RUN:
        post(f"…and {len(msgs) - MAX_PER_RUN} more record changes today. Full book: {SITE}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        post("🤖 MTALGA record bot connected. When a league record falls, you'll hear it here first. ⚾")
    else:
        print(__doc__)
