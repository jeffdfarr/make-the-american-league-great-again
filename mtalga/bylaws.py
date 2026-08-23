"""Fetch the rulebook Google Sheet into structured site data.

Three tabs, three shapes, three parsers:
  * Rules        -> outline (column position = indent depth)     -> rules.json
  * Trades       -> trade log (positional columns A..G)          -> trades.json
  * Rookie Draft -> pick-ownership boards by year                -> draft.json

PRIVACY: only the tabs configured in config/bylaws.yaml are fetched; the
Owners tab (contact info) is never listed and a hard guard refuses to write
anything that looks like a phone list.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import requests
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "bylaws.yaml"
OUT_DIR = Path(__file__).resolve().parent.parent / "site" / "data"

GVIZ = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={tab}"

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]


def _guard_no_contacts(rows: list[list[str]], tab: str) -> None:
    joined = " ".join(c for row in rows[:5] for c in row).lower()
    if "phone" in joined:
        raise SystemExit(f"[bylaws] ABORT: tab {tab!r} looks like contact info — refusing to publish.")


# ---------------------------------------------------------------- Rules (outline)

def parse_outline(rows: list[list[str]]) -> list[dict]:
    blocks = []
    for row in rows:
        non_empty = [(i, c) for i, c in enumerate(row) if c]
        if not non_empty:
            continue
        blocks.append({"depth": non_empty[0][0], "cells": [c for _, c in non_empty]})
    return blocks


# ---------------------------------------------------------------- Trades (log)

def parse_trades(rows: list[list[str]], max_col: int = 7) -> list[dict]:
    """Columns A..G: date, owner1, player1, pick1, owner2, player2, pick2.
    Continuation rows add more players/picks to the trade above. Columns
    beyond max_col belong to a side summary table and are ignored."""
    trades: list[dict] = []
    for row in rows:
        c = (row + [""] * max_col)[:max_col]
        if c[1] == "Owner" or (not any(c)):
            continue
        if DATE_RE.match(c[0]):
            trades.append({
                "date": c[0],
                "sides": [
                    {"owner": c[1], "players": [c[2]] if c[2] else [], "picks": [c[3]] if c[3] else []},
                    {"owner": c[4], "players": [c[5]] if c[5] else [], "picks": [c[6]] if c[6] else []},
                ],
            })
        elif trades:
            t = trades[-1]
            if c[2]: t["sides"][0]["players"].append(c[2])
            if c[3]: t["sides"][0]["picks"].append(c[3])
            if c[5]: t["sides"][1]["players"].append(c[5])
            if c[6]: t["sides"][1]["picks"].append(c[6])
    return trades


# ---------------------------------------------------------------- Draft boards

def parse_draftboard(rows: list[list[str]]) -> list[dict]:
    """Year blocks: a label row ("2027" for future ownership boards, or
    "2026 Picks" for past draft results), a round-header row (1st/2nd/...,
    possibly with Compensatory columns), then slot rows alternating
    pick#/name pairs. Annotation rows (side bets etc.) are ignored."""
    YEAR_LABEL = re.compile(r"^((?:19|20)\d{2})(?:\s+picks)?$", re.I)
    ROUND = re.compile(r"^(\d+(?:st|nd|rd|th)|comp\w*)$", re.I)
    boards: list[dict] = []
    current = None
    for row in rows:
        non_empty = [c for c in row if c]
        m = YEAR_LABEL.match(non_empty[0]) if len(non_empty) == 1 else None
        if m:
            current = {"year": int(m.group(1)), "rounds": [], "results": "picks" in non_empty[0].lower()}
            boards.append(current)
            continue
        if current is None:
            continue
        if not current["rounds"]:
            hits = [c for c in non_empty if ROUND.match(c)]
            if hits and len(hits) >= max(2, len(non_empty) - 1):
                current["rounds"] = [{"round": c, "picks": []} for c in non_empty]
            continue
        if row and row[0].isdigit() and len(row[0]) <= 3:
            pairs = []
            i = 0
            while i + 1 < len(row):
                num, name = row[i], row[i + 1]
                if num.isdigit() and name and len(name) <= 48:
                    pairs.append((int(num), name))
                elif name and len(name) > 48:
                    pass  # annotation text sharing the row — drop it
                i += 2
            for r_idx, (num, name) in enumerate(pairs[: len(current["rounds"])]):
                current["rounds"][r_idx]["picks"].append({"num": num, "owner": name})
    return [b for b in boards if any(r["picks"] for r in b["rounds"])]


# ---------------------------------------------------------------- driver

PARSERS = {"outline": parse_outline, "trades": parse_trades, "draftboard": parse_draftboard}


def export_bylaws() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    sheet_id = cfg["sheet_id"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for out_name, spec in cfg["pages"].items():
        tab, mode = spec["tab"], spec["mode"]
        url = GVIZ.format(sheet_id=sheet_id, tab=requests.utils.quote(tab))
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        rows = _rows(r.text)
        _guard_no_contacts(rows, tab)
        data = PARSERS[mode](rows)
        n = len(data)
        (OUT_DIR / f"{out_name}.json").write_text(json.dumps(data, indent=1))
        print(f"[bylaws] {tab} ({mode}) -> {out_name}.json: {n} items")
        if n == 0:
            print(f"[bylaws] WARNING: {tab!r} produced 0 items — is the tab name exact?")


if __name__ == "__main__":
    export_bylaws()
