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
    def ok(text: str) -> bool:
        # Real players/picks are short; long text = sheet annotation (side bets etc.)
        return 0 < len(text) <= 60

    trades: list[dict] = []
    for row in rows:
        c = (row + [""] * max_col)[:max_col]
        if c[1] == "Owner" or (not any(c)):
            continue
        if DATE_RE.match(c[0]):
            # Owner cells get the annotation guard too — side bets have been
            # found logged in the trade sheet with their own date row.
            o1 = c[1] if 0 < len(c[1]) <= 40 else ""
            o2 = c[4] if 0 < len(c[4]) <= 40 else ""
            trades.append({
                "date": c[0],
                "sides": [
                    {"owner": o1, "players": [c[2]] if ok(c[2]) else [], "picks": [c[3]] if ok(c[3]) else []},
                    {"owner": o2, "players": [c[5]] if ok(c[5]) else [], "picks": [c[6]] if ok(c[6]) else []},
                ],
            })
        elif trades:
            t = trades[-1]
            if ok(c[2]): t["sides"][0]["players"].append(c[2])
            if ok(c[3]): t["sides"][0]["picks"].append(c[3])
            if ok(c[5]): t["sides"][1]["players"].append(c[5])
            if ok(c[6]): t["sides"][1]["picks"].append(c[6])
    # drop rows that were pure annotation (no owners, no assets survive the guards)
    return [t for t in trades
            if any(sd["owner"] or sd["players"] or sd["picks"] for sd in t["sides"])]


# ---------------------------------------------------------------- Draft boards

def _merge_draft_results(boards: list[dict]) -> list[dict]:
    """A drafted year appears twice in the sheet under one label: the table of
    players taken, then a color-matched duplicate of the pick-order board
    (managers per slot, with trade chains). Join them by pick number so the
    results board shows "Player (Manager)" — who drafted whom. The manager is
    the name before any parenthetical: the slot's final owner made the pick."""
    by_year: dict[int, dict] = {}
    for b in boards:
        by_year.setdefault(b["year"], {})[b["results"]] = b
    out = []
    for year in sorted(by_year, reverse=True):
        pair = by_year[year]
        if True in pair and False in pair:
            res, own = pair[True], pair[False]
            for r_idx, rnd in enumerate(res["rounds"]):
                if r_idx >= len(own["rounds"]):
                    continue
                own_r = own["rounds"][r_idx]
                own_by_num = {p["num"]: p for p in own_r["picks"]}
                for s, p in enumerate(rnd["picks"]):
                    src = own_by_num.get(p["num"])
                    if src is None and s < len(own_r["picks"]):
                        src = own_r["picks"][s]
                    if src is None or "(" in p["owner"]:
                        continue  # no match, or manager already typed by hand
                    manager = src["owner"].split("(")[0].strip()
                    if manager:
                        p["owner"] = f"{p['owner'].strip()} ({manager})"
            out.append(res)
        else:
            out.append(pair.get(True) or pair[False])
    return out


def parse_draftboard(rows: list[list[str]]) -> list[dict]:
    """Year blocks: a label row ("2027" for future ownership boards, or
    "2026 Picks" for past draft results), a round-header row (1st/2nd/...,
    possibly with Compensatory columns), then slot rows alternating
    pick#/name column pairs. A drafted year stacks TWO tables under one
    label — players taken, then the pick-order duplicate — separated by a
    repeated round-header row. Annotation rows (side bets etc.) are ignored."""
    YEAR_LABEL = re.compile(r"^((?:19|20)\d{2})(?:\s+picks)?$", re.I)
    ROUND = re.compile(r"^(\d+(?:st|nd|rd|th)|comp\w*)$", re.I)
    boards: list[dict] = []
    current = None
    for row in rows:
        non_empty = [c for c in row if c]
        if not non_empty:
            continue
        m = YEAR_LABEL.match(non_empty[0]) if len(non_empty) == 1 else None
        if m:
            current = {"year": int(m.group(1)), "rounds": [], "results": "picks" in non_empty[0].lower()}
            boards.append(current)
            continue
        if current is None:
            continue
        hits = [c for c in non_empty if ROUND.match(c)]
        if hits and len(hits) >= max(2, len(non_empty) - 1):
            if current["rounds"]:
                # repeated header under the same year label: the companion
                # pick-order table starts here — make it its own board
                current = {"year": current["year"], "rounds": [], "results": False}
                boards.append(current)
            current["rounds"] = [{"round": c, "picks": []} for c in non_empty]
            continue
        if not current["rounds"]:
            continue
        if row[0].isdigit() and len(row[0]) <= 3:
            i = 0
            while i + 1 < len(row):
                num, name = row[i], row[i + 1]
                r_idx = i // 2  # each round owns a fixed pick#/name column pair
                if num.isdigit() and name and len(name) <= 48 and r_idx < len(current["rounds"]):
                    current["rounds"][r_idx]["picks"].append({"num": int(num), "owner": name})
                i += 2
    return _merge_draft_results([b for b in boards if any(r["picks"] for r in b["rounds"])])


# ---------------------------------------------------------------- driver

def apply_aliases(data, aliases: dict):
    """Normalize misspelled owner names everywhere (incl. inside pick chains)."""
    if not aliases:
        return data
    pats = [(re.compile(r"\b" + re.escape(k) + r"\b", re.I), v) for k, v in aliases.items()]

    def fix(x):
        if isinstance(x, str):
            for pat, repl in pats:
                x = pat.sub(repl, x)
            return x
        if isinstance(x, list):
            return [fix(v) for v in x]
        if isinstance(x, dict):
            return {k: fix(v) for k, v in x.items()}
        return x

    return fix(data)


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
        data = apply_aliases(data, cfg.get("aliases") or {})
        n = len(data)
        (OUT_DIR / f"{out_name}.json").write_text(json.dumps(data, indent=1))
        print(f"[bylaws] {tab} ({mode}) -> {out_name}.json: {n} items")
        if n == 0:
            print(f"[bylaws] WARNING: {tab!r} produced 0 items — is the tab name exact?")


if __name__ == "__main__":
    export_bylaws()
