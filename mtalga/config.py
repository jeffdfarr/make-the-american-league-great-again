"""Load the YAML config files (seasons, owners, adjustments)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass
class SeasonCfg:
    year: int
    league_id: str
    reg_season_games: int | None = None
    playoff_teams: int | None = None
    byes: int | None = None
    notes: str | None = None


@dataclass
class Config:
    seasons: dict[int, SeasonCfg]
    current_season: int
    owners: list[dict]
    team_map: dict[int, dict[str, str]]  # year -> fantrax_team_id -> owner_slug
    adjustments: list[dict] = field(default_factory=list)


def load(config_dir: Path | str | None = None) -> Config:
    d = Path(config_dir) if config_dir else CONFIG_DIR

    seasons_raw = yaml.safe_load((d / "seasons.yaml").read_text())
    owners_raw = yaml.safe_load((d / "owners.yaml").read_text())
    adj_path = d / "adjustments.yaml"
    adjustments = []
    if adj_path.exists():
        adjustments = (yaml.safe_load(adj_path.read_text()) or {}).get("adjustments") or []

    seasons = {
        int(year): SeasonCfg(
            year=int(year),
            league_id=str(cfg["league_id"]),
            reg_season_games=cfg.get("reg_season_games"),
            playoff_teams=cfg.get("playoff_teams"),
            byes=cfg.get("byes"),
            notes=cfg.get("notes"),
        )
        for year, cfg in seasons_raw["seasons"].items()
    }
    team_map = {
        int(year): dict(mapping or {})
        for year, mapping in (owners_raw.get("team_map") or {}).items()
    }
    return Config(
        seasons=seasons,
        current_season=int(seasons_raw["current_season"]),
        owners=owners_raw["owners"],
        team_map=team_map,
        adjustments=adjustments,
    )
