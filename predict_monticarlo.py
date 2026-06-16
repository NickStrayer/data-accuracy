"""
predict_montecarlo.py

Builds the simulation-ready data bundle for the Predictor tab Monte Carlo engine.
Run this AFTER analyze_progression.py — it reads the DB and existing JSON outputs.

Outputs written to ./output/:
  montecarlo_data.json   — athlete roster + empirical improvement distributions
                           needed by the JS simulator

What it produces
----------------
{
  "athletes": {
    "<athlete_id>": {
      "name":        "Jane Doe",
      "school":      "Harvard",
      "conference":  "Ivy League",
      "xc_region":   "DI New England Region",
      "gender":      "F",
      "class_year":  "JR",          # inferred ordinal class (FR/SO/JR/SR/5TH)
      "events": {
        "1500M": {
          "best_time":   225.34,    # seconds
          "season_year": 2025,
          "decile":      8          # 1=slowest … 10=fastest nationally
        },
        ...
      }
    },
    ...
  },

  "improvement_distributions": {
    # Empirical improvement % arrays per event × gender × transition × decile.
    # Derived from rating_transitions.json (produced by analyze_progression.py) —
    # no DB re-query needed.
    #
    # Shape (JS-compatible — same as the previous version):
    #   {event}{gender}{transition}{"all": [...], "1": [...], ..., "10": [...]}
    #
    # Discounted variants are stored under a parallel top-level key
    # "improvement_distributions_discounted" with the identical shape, so the
    # JS can switch modes without changing its sampling logic.
    "1500M": {
      "M": {
        "FR_to_SO": {
          "all":  [1.2, 0.8, -0.3, ...],
          "1":    [...],
          ...
          "10":   [...]
        },
        ...
      }
    }
  },

  "improvement_distributions_discounted": { ... },   # same shape, field-adjusted values

  "percentile_benchmarks": {
    # Passed through directly from percentile_tables.json — no recomputation.
    "1500M": { "M": { "p10": 240.1, "p50": 225.3, ... }, "F": { ... } },
    ...
  },

  "conferences": ["Ivy League", "SEC", ...],
  "xc_regions":  ["DI New England Region", ...],
  "current_season": 2025
}

Key design: all heavy statistical work is done by analyze_progression.py.
This script does only three things:
  1. Build the current-season athlete roster (new — requires one DB query).
  2. Annotate athlete deciles using the pre-computed percentile_tables.json.
  3. Reformat improvement data from rating_transitions.json into per-decile
     arrays the JS simulator can sample from — no DB re-query needed.
"""

import json
import logging
import argparse
import csv
import sqlite3
from pathlib import Path

from database import get_connection, DB_PATH
from event_bounds import sql_plausible_time_where
from analyze_progression import FOCUS_CODES

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
ROOT_DIR = Path(__file__).parent

DEFAULT_CONF_CSV   = ROOT_DIR / "ncaa_d1_xc_teams.csv"
DEFAULT_REGION_CSV = ROOT_DIR / "ncaa_d1_xc_teams_by_region.csv"

# Must stay in sync with analyze_progression.py
TRANSITIONS    = [("FR", "SO"), ("SO", "JR"), ("JR", "SR"), ("SR", "5TH")]
ELIGIBLE_CLASSES = {"FR", "SO", "JR"}
CURRENT_SEASON = 2026
MIN_DECILE_SAMPLES = 5   # smaller buckets are dropped; JS falls back to row pool
MIN_CELL_SAMPLES   = 3   # minimum pairs in a start→end decile cell

# ─────────────────────────────────────────────────────────────────────────────
# I/O helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(filename: str) -> dict:
    p = OUTPUT_DIR / filename
    if p.exists():
        return json.loads(p.read_text())
    logger.warning("Missing %s — run analyze_progression.py first", filename)
    return {}


def _write_json(filename: str, data) -> None:
    p = OUTPUT_DIR / filename
    p.write_text(json.dumps(data, separators=(",", ":")))
    logger.info("Wrote %s  (%d bytes)", p, p.stat().st_size)


def _load_team_maps_from_csv(
    conf_path: Path,
    region_path: Path,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """
    Load school → conference / region maps from the NCAA D1 XC CSV files.
    Team names must match DB school_name exactly; unmatched schools are ignored.
    """
    conf_map: dict[str, str] = {}
    all_conferences: set[str] = set()
    if conf_path.exists():
        with conf_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                team = (row.get("Team") or row.get("team") or "").strip()
                conf = (row.get("Conference") or row.get("conference") or "").strip()
                if team and conf:
                    conf_map[team] = conf
                    all_conferences.add(conf)
        logger.info(
            "Loaded conference CSV: %d teams, %d conferences",
            len(conf_map), len(all_conferences),
        )
    else:
        logger.warning("Conference CSV not found at %s", conf_path)

    region_map: dict[str, str] = {}
    all_regions: set[str] = set()
    if region_path.exists():
        with region_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                team = (row.get("Team") or row.get("team") or "").strip()
                region = (row.get("Region") or row.get("region") or "").strip()
                if team and region:
                    region_map[team] = region
                    all_regions.add(region)
        logger.info(
            "Loaded region CSV: %d teams, %d regions",
            len(region_map), len(all_regions),
        )
    else:
        logger.warning("Region CSV not found at %s", region_path)

    return (
        conf_map,
        region_map,
        sorted(all_conferences),
        sorted(all_regions),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Build athlete roster  (DB query — the only step that touches the DB)
# ─────────────────────────────────────────────────────────────────────────────

# Matches the ordinal class-year CTE in analyze_progression.py exactly so
# class labels are gap-year-safe and consistent across both scripts.
_CLASS_YEAR_CTE = """
WITH athlete_season_rank AS (
    SELECT athlete_id,
           season_year,
           ROW_NUMBER() OVER (
               PARTITION BY athlete_id
               ORDER BY season_year
           ) AS season_rank
    FROM (SELECT DISTINCT athlete_id, season_year FROM results)
),
athlete_classes AS (
    SELECT athlete_id,
           season_year,
           CASE season_rank
               WHEN 1 THEN 'FR'
               WHEN 2 THEN 'SO'
               WHEN 3 THEN 'JR'
               WHEN 4 THEN 'SR'
               WHEN 5 THEN '5TH'
           END AS class_year
    FROM athlete_season_rank
)
"""


def build_athlete_roster(
    conn: sqlite3.Connection,
    conf_map: dict,
    region_map: dict,
    current_season: int = CURRENT_SEASON,
) -> dict:
    """
    Returns {athlete_id: athlete_record} for all athletes who:
      - have results in the current season
      - are FR, SO, or JR (at least one future season remaining)

    Decile is not set here — annotate_athlete_deciles() fills it in after.
    """
    sql = f"""
    {_CLASS_YEAR_CTE}
    SELECT
        a.athlete_id,
        a.athlete_name AS name,
        sc.school_name,
        a.gender,
        ac.class_year,
        r.event_code,
        MIN(r.time_seconds) AS best_time,
        r.season_year
    FROM results r
    JOIN athletes  a  ON a.athlete_id  = r.athlete_id
    JOIN schools   sc ON sc.school_id  = a.school_id
    LEFT JOIN athlete_classes ac
           ON ac.athlete_id  = a.athlete_id
          AND ac.season_year = r.season_year
    WHERE r.season_year       = ?
      AND r.time_seconds IS NOT NULL
      AND r.time_seconds      > 0
      AND {sql_plausible_time_where("r")}
      AND ac.class_year IN ('FR','SO','JR')
    GROUP BY a.athlete_id, r.event_code
    ORDER BY a.athlete_name
    """

    cur  = conn.execute(sql, (current_season,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    athletes: dict = {}
    for row in rows:
        aid    = str(row["athlete_id"])
        school = row["school_name"] or ""

        if aid not in athletes:
            athletes[aid] = {
                "name":       row["name"] or "",
                "school":     school,
                "conference": conf_map.get(school, ""),
                "xc_region":  region_map.get(school, ""),
                "gender":     row["gender"] or "",
                "class_year": row["class_year"] or "",
                "events":     {},
            }

        athletes[aid]["events"][row["event_code"]] = {
            "best_time":   round(float(row["best_time"]), 3),
            "season_year": int(row["season_year"]),
            # decile is filled in by annotate_athlete_deciles()
        }

    logger.info(
        "Built roster: %d eligible athletes (current_season=%d)",
        len(athletes), current_season,
    )
    return athletes


# ─────────────────────────────────────────────────────────────────────────────
# 2. Annotate deciles  (pure lookup against percentile_tables.json — no DB)
# ─────────────────────────────────────────────────────────────────────────────

def _rating_to_decile(rating: float) -> int:
    """Convert a 0–100 percentile rating to a 1–10 decile."""
    return max(1, min(10, int(rating / 10) + 1))


def _time_to_rating(time_sec: float, breakpoints: list[tuple[int, float]]) -> float | None:
    """
    Interpolate a national percentile rating (0–100) from a raw time.

    breakpoints: sorted list of (percentile_int, time_seconds), e.g.
      [(5, 1320.0), (10, 1290.0), ..., (95, 890.0)]
    Lower time = faster = higher percentile.
    Returns None when breakpoints is empty.
    """
    if not breakpoints:
        return None
    if time_sec >= breakpoints[0][1]:    # slower than the p5 anchor
        return 5.0
    if time_sec <= breakpoints[-1][1]:   # faster than the p95 anchor
        return 95.0
    for i in range(len(breakpoints) - 1):
        p_lo, t_lo = breakpoints[i]
        p_hi, t_hi = breakpoints[i + 1]
        if t_hi <= time_sec <= t_lo:
            frac = (t_lo - time_sec) / (t_lo - t_hi) if t_lo != t_hi else 0.5
            return p_lo + frac * (p_hi - p_lo)
    return 50.0


def annotate_athlete_deciles(athletes: dict, percentile_tables: dict) -> None:
    """Add 'decile' (1–10) to every athlete×event entry in-place."""
    for ath in athletes.values():
        gender = ath["gender"]
        for event_code, ev_data in ath["events"].items():
            tbl = percentile_tables.get(event_code, {}).get(gender, {})
            breakpoints = sorted(
                [(int(k[1:]), v) for k, v in tbl.items() if k.startswith("p")],
                key=lambda x: x[0],
            )
            rating = _time_to_rating(ev_data["best_time"], breakpoints)
            ev_data["decile"] = _rating_to_decile(rating) if rating is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reformat improvement distributions  (from rating_transitions.json — no DB)
# ─────────────────────────────────────────────────────────────────────────────

def _prune_cell_map(by_cell: dict) -> dict:
    """Drop sparse start→end decile cells; keep structure for JS fallbacks."""
    pruned: dict = {}
    for fd, to_map in by_cell.items():
        row: dict = {}
        for td, imps in to_map.items():
            if len(imps) >= MIN_CELL_SAMPLES:
                row[td] = imps
        if row:
            pruned[fd] = row
    return pruned


def _prune_from_decile_map(by_from: dict) -> dict:
    pruned: dict = {}
    for fd, imps in by_from.items():
        if len(imps) >= MIN_DECILE_SAMPLES:
            pruned[fd] = imps
    return pruned


def _cells_from_triples(triples: list) -> dict:
    by_cell: dict = {}
    for fd, td, imp in triples:
        fd_s, td_s = str(fd), str(td)
        by_cell.setdefault(fd_s, {}).setdefault(td_s, []).append(float(imp))
    return by_cell


def _from_decile_from_triples(triples: list) -> dict:
    by_from: dict = {}
    for fd, _td, imp in triples:
        by_from.setdefault(str(fd), []).append(float(imp))
    return by_from


def _from_decile_from_improvements(pairs: list) -> dict:
    """Legacy fallback when rating_transitions predates cell bucketing."""
    by_from: dict = {}
    for rating, imp in pairs:
        d = str(_rating_to_decile(float(rating)))
        by_from.setdefault(d, []).append(round(float(imp), 4))
    return _prune_from_decile_map(by_from)


def build_transition_distributions(
    rating_transitions: dict,
) -> dict:
    """
    Package matrix + per-cell improvement arrays for the JS Monte Carlo engine.

    Each transition block mirrors rating_transitions.json but only includes
    fields the simulator needs:
      {event: {gender: {transition: {
          matrix, improvements_by_cell, improvements_by_from_decile
      }}}}
    """
    out: dict = {}
    valid_trans = {f"{f}_to_{t}" for f, t in TRANSITIONS}

    for event_code, genders in rating_transitions.items():
        for gender, transitions in genders.items():
            for trans_key, block in transitions.items():
                if trans_key not in valid_trans:
                    continue
                matrix  = block.get("matrix")
                by_cell = block.get("improvements_by_cell")
                by_from = block.get("improvements_by_from_decile")
                triples = block.get("improvement_triples", [])

                if not by_cell and triples:
                    by_cell = _cells_from_triples(triples)
                if not by_from and triples:
                    by_from = _from_decile_from_triples(triples)
                if not by_from:
                    by_from = _from_decile_from_improvements(block.get("improvements", []))

                by_cell = _prune_cell_map(by_cell or {})
                by_from = _prune_from_decile_map(by_from or {})
                if not matrix or not by_from:
                    continue
                out.setdefault(event_code, {}).setdefault(gender, {})[trans_key] = {
                    "matrix":                      matrix,
                    "improvements_by_cell":        by_cell,
                    "improvements_by_from_decile": by_from,
                }

    n_blocks = sum(
        1 for ev in out.values() for g in ev.values() for _ in g.values()
    )
    logger.info(
        "Built transition distributions: %d blocks (matrix + decile cells)",
        n_blocks,
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--conf-map",
        default=str(DEFAULT_CONF_CSV),
        help="Path to ncaa_d1_xc_teams.csv (Team, Conference columns)",
    )
    parser.add_argument(
        "--region-map",
        default=str(DEFAULT_REGION_CSV),
        help="Path to ncaa_d1_xc_teams_by_region.csv (Team, Region columns)",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=CURRENT_SEASON,
        help="Current season year for roster building",
    )
    args = parser.parse_args()

    conf_map, region_map, all_conferences, all_regions = _load_team_maps_from_csv(
        Path(args.conf_map), Path(args.region_map)
    )

    # Load pre-computed outputs from analyze_progression.py — no recomputation.
    logger.info("Loading pre-computed outputs from analyze_progression.py …")
    percentile_tables  = _load_json("percentile_tables.json")
    rating_transitions = _load_json("rating_transitions.json")

    # 1. Roster — the only step that opens the DB.
    logger.info("Building athlete roster (DB query) …")
    with get_connection() as conn:
        athletes = build_athlete_roster(conn, conf_map, region_map, args.season)

    # 2. Decile annotation — pure dict lookup, no DB.
    logger.info("Annotating athlete deciles …")
    annotate_athlete_deciles(athletes, percentile_tables)

    # 3. Transition distributions — matrix + decile cells from rating_transitions.json.
    logger.info("Building transition distributions from rating_transitions.json …")
    transition_distributions = build_transition_distributions(rating_transitions)

    conferences = all_conferences
    xc_regions  = all_regions

    bundle = {
        "athletes":                   athletes,
        "transition_distributions":   transition_distributions,
        "percentile_benchmarks":      percentile_tables,
        "conferences":                conferences,
        "xc_regions":                 xc_regions,
        "current_season":             args.season,
    }

    _write_json("montecarlo_data.json", bundle)

    n_with_conf = sum(1 for a in athletes.values() if a["conference"])
    n_with_reg  = sum(1 for a in athletes.values() if a["xc_region"])
    logger.info("Done.")
    logger.info("  Athletes:        %d", len(athletes))
    logger.info("  With conference: %d", n_with_conf)
    logger.info("  With XC region:  %d", n_with_reg)
    logger.info("  Conferences:     %d", len(conferences))
    logger.info("  XC regions:      %d", len(xc_regions))


if __name__ == "__main__":
    main()