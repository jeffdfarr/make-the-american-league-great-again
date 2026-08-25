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
    row = conn.execute("SELECT MAX(year) y FROM seasons").fetchone()
    cur_year = row["y"] if row else None
    msgs = []
    for key, new in snapshot(conn).items():
        old = before.get(key)
        if old is None:
            continue  # newly added record category — not a broken record
        if new["scope"] == "legend":
            continue  # franchise-legend leaderboard, not a record — never alert
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
            # An in-progress record fattening itself weekly (same holder, same
            # current season) is accumulation, not news — the BREAK already
            # alerted. Streak records are the exception: each extension is an
            # event worth announcing.
            same_current = (new["owner_slug"] == old["owner_slug"]
                            and new["year"] is not None and new["year"] == old["year"]
                            and new["year"] == cur_year)
            if same_current and "streak" not in new["category"].lower():
                continue
            msgs.append(
                f"📈 RECORD EXTENDED — {new['category']}: {who(new['owner_slug'])} pushes their "
                f"own record to {new['display']}{det} (was {old['display']})."
            )
    return msgs


def _quiet_hours() -> bool:
    """True between 10pm and 9am Central — no record pings while the league sleeps."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo("America/Chicago")).hour
    except Exception:
        from datetime import datetime, timezone
        hour = (datetime.now(timezone.utc).hour - 5) % 24  # rough CT fallback
    return hour >= 22 or hour < 9


def _pending(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS pending_alerts (msg TEXT)")
    return [r[0] for r in conn.execute("SELECT msg FROM pending_alerts")]


def post(text: str) -> bool:
    bot_id = os.environ.get("GROUPME_BOT_ID")
    if not bot_id:
        print(f"[alerts] (no GROUPME_BOT_ID set) {text}")
        return False
    r = requests.post(API, json={"bot_id": bot_id, "text": text[:990]}, timeout=15)
    print(f"[alerts] sent ({r.status_code}): {text[:80]}")
    return r.status_code < 300


def send_all(conn: sqlite3.Connection, msgs: list[str]) -> None:
    """Send now, or park in the DB during quiet hours — the next daytime run
    delivers whatever's waiting."""
    queue = _pending(conn)
    msgs = queue + [m for m in msgs if m not in queue]
    if not msgs:
        return
    if _quiet_hours():
        conn.execute("DELETE FROM pending_alerts")
        conn.executemany("INSERT INTO pending_alerts (msg) VALUES (?)", [(m,) for m in msgs])
        conn.commit()
        print(f"[alerts] quiet hours — {len(msgs)} message(s) held for the morning run")
        return
    for m in msgs[:MAX_PER_RUN]:
        post(m)
        time.sleep(1)
    if len(msgs) > MAX_PER_RUN:
        post(f"…and {len(msgs) - MAX_PER_RUN} more record changes today. Full book: {SITE}")
    conn.execute("DELETE FROM pending_alerts")
    conn.commit()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        post("🤖 MTALGA record bot connected. When a league record falls, you'll hear it here first. ⚾")
    else:
        print(__doc__)
