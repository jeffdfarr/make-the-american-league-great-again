"""Fetch the league bylaws from the Google Sheet rulebook into site data.

The rulebook sheet is link-readable; each tab is fetched as CSV via the
gviz endpoint. Layout convention in the sheet: column position = depth
(col A: section headings, col B: bullets, col C: sub-bullets / table cells).

Tabs are allow-listed in config/bylaws.yaml — the Owners tab (personal
contact info) must never be exported to the public site.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import requests
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "bylaws.yaml"
OUT = Path(__file__).resolve().parent.parent / "site" / "data" / "bylaws.json"

GVIZ = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={tab}"


def parse_rows(text: str) -> list[dict]:
    """CSV -> blocks: {depth, cells}. Depth = index of first non-empty column."""
    blocks = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row]
        non_empty = [(i, c) for i, c in enumerate(cells) if c]
        if not non_empty:
            continue
        depth = non_empty[0][0]
        blocks.append({"depth": depth, "cells": [c for _, c in non_empty]})
    return blocks


def fetch_bylaws() -> dict:
    cfg = yaml.safe_load(CONFIG.read_text())
    sheet_id = cfg["sheet_id"]
    excluded = {t.lower() for t in cfg.get("exclude_tabs", [])}
    sections = []
    for tab in cfg["tabs"]:
        if tab.lower() in excluded:
            print(f"[bylaws] SKIPPING excluded tab: {tab}")
            continue
        url = GVIZ.format(sheet_id=sheet_id, tab=requests.utils.quote(tab))
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        blocks = parse_rows(r.text)
        # Guard: if Google fell back to the first tab (unknown tab name),
        # the content would repeat — detect via first cell fingerprint.
        fingerprint = blocks[0]["cells"][0] if blocks else ""
        sections.append({"tab": tab, "blocks": blocks, "fingerprint": fingerprint})
        print(f"[bylaws] {tab}: {len(blocks)} blocks")

    # Drop tabs that returned identical content to another (gviz fallback)
    seen = {}
    kept = []
    for s in sections:
        key = (len(s["blocks"]), s["fingerprint"])
        if key in seen and s["blocks"] == seen[key]["blocks"]:
            print(f"[bylaws] WARNING: tab {s['tab']!r} returned duplicate content "
                  f"(same as {seen[key]['tab']!r}) — probably a renamed/missing tab; skipped")
            continue
        seen[key] = s
        kept.append({"tab": s["tab"], "blocks": s["blocks"]})

    # Safety net: refuse to publish anything that looks like the contact list
    for s in kept:
        joined = " ".join(c for b in s["blocks"] for c in b["cells"])[:2000].lower()
        if "phone" in joined and any(ch.isdigit() for ch in joined):
            raise SystemExit(
                f"[bylaws] ABORT: tab {s['tab']!r} looks like contact info — "
                "check config/bylaws.yaml tabs/exclude_tabs.")
    return {"sections": kept}


def export_bylaws() -> None:
    data = fetch_bylaws()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    print(f"[bylaws] wrote {OUT.name}: {len(data['sections'])} sections")


if __name__ == "__main__":
    export_bylaws()
