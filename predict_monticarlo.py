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
import sqlite3
from pathlib import Path

from database import get_connection, DB_PATH

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Must stay in sync with analyze_progression.py
TRANSITIONS    = [("FR", "SO"), ("SO", "JR"), ("JR", "SR"), ("SR", "5TH")]
ELIGIBLE_CLASSES = {"FR", "SO", "JR"}
CURRENT_SEASON = 2025
MIN_DECILE_SAMPLES = 5   # smaller buckets are dropped; JS falls back to "all"

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


def _load_conference_maps(conf_path: Path, region_path: Path) -> tuple[dict, dict]:
    conf_map, region_map = {}, {}
    if conf_path.exists():
        conf_map = json.loads(conf_path.read_text())
        logger.info("Loaded conference map: %d schools", len(conf_map))
    else:
        logger.warning("conference_map.json not found at %s", conf_path)
    if region_path.exists():
        region_map = json.loads(region_path.read_text())
        logger.info("Loaded region map: %d schools", len(region_map))
    else:
        logger.warning("region_map.json not found at %s", region_path)
    return conf_map, region_map


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
        a.name,
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
      AND ac.class_year IN ('FR','SO','JR')
    GROUP BY a.athlete_id, r.event_code
    ORDER BY a.name
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

def _bucket_pairs(pairs: list[list]) -> dict[str, list]:
    """
    Convert a list of [from_rating, imp_pct] pairs from rating_transitions.json
    into {"all": [...], "1": [...], ..., "10": [...]} buckets.

    Buckets with fewer than MIN_DECILE_SAMPLES entries are dropped so the JS
    sampler falls back to "all" gracefully rather than over-fitting tiny groups.
    """
    buckets: dict[str, list] = {"all": [], **{str(d): [] for d in range(1, 11)}}

    for rating, imp in pairs:
        decile = _rating_to_decile(float(rating))
        imp    = round(float(imp), 4)
        buckets["all"].append(imp)
        buckets[str(decile)].append(imp)

    pruned: dict[str, list] = {"all": buckets["all"]}
    for d in range(1, 11):
        if len(buckets[str(d)]) >= MIN_DECILE_SAMPLES:
            pruned[str(d)] = buckets[str(d)]

    return pruned


def build_improvement_distributions(
    rating_transitions: dict,
) -> tuple[dict, dict]:
    """
    Reformat rating_transitions.json into per-decile improvement arrays.

    analyze_progression.py already stores every athlete's paired
    (from_rating, improvement_pct) in:
      rating_transitions[event][gender][transition]["improvements"]
      rating_transitions[event][gender][transition]["improvements_discounted"]

    We bucket those pre-computed values by decile — no DB queries, no re-pairing.

    Returns:
      (raw_distributions, discounted_distributions)

    Both share the JS-compatible shape:
      {event_code: {gender: {transition: {"all": [...], "1": [...], ..., "10": [...]}}}}
    """
    raw_out:  dict = {}
    disc_out: dict = {}
    valid_trans = {f"{f}_to_{t}" for f, t in TRANSITIONS}

    for event_code, genders in rating_transitions.items():
        for gender, transitions in genders.items():
            for trans_key, block in transitions.items():
                if trans_key not in valid_trans:
                    continue  # skip FR_to_SR career block — not used by predictor

                raw_pairs  = block.get("improvements", [])
                disc_pairs = block.get("improvements_discounted", [])

                if raw_pairs:
                    bucketed = _bucket_pairs(raw_pairs)
                    if len(bucketed["all"]) >= 10:
                        raw_out.setdefault(event_code, {}).setdefault(gender, {})[trans_key] = bucketed

                if disc_pairs:
                    bucketed = _bucket_pairs(disc_pairs)
                    if len(bucketed["all"]) >= 10:
                        disc_out.setdefault(event_code, {}).setdefault(gender, {})[trans_key] = bucketed

    n_blocks = sum(
        1
        for ev in raw_out.values()
        for g in ev.values()
        for t in g.values()
        if t.get("all")
    )
    logger.info(
        "Built improvement distributions: %d blocks (from rating_transitions.json — no DB query)",
        n_blocks,
    )
    return raw_out, disc_out


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--conf-map",
        default=r"output\conference_map.json",
        help="Path to conference_map.json (output of extract_conferences.py)",
    )
    parser.add_argument(
        "--region-map",
        default="output/region_map.json",
        help="Path to region_map.json",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=CURRENT_SEASON,
        help="Current season year for roster building",
    )
    args = parser.parse_args()

    conf_map, region_map = _load_conference_maps(
        Path(args.conf_map), Path(args.region_map)
    )

    # Load pre-computed outputs from analyze_progression.py — no recomputation.
    logger.info("Loading pre-computed outputs from analyze_progression.py …")
    percentile_tables  = _load_json("percentile_tables.json")
    rating_transitions = _load_json("rating_transitions.json")

    # 1. Roster — the only step that opens the DB.
    logger.info("Building athlete roster (DB query) …")
    conn     = get_connection()
    athletes = build_athlete_roster(conn, conf_map, region_map, args.season)
    conn.close()

    # 2. Decile annotation — pure dict lookup, no DB.
    logger.info("Annotating athlete deciles …")
    annotate_athlete_deciles(athletes, percentile_tables)

    # 3. Improvement distributions — reformats rating_transitions.json, no DB.
    logger.info("Building improvement distributions from rating_transitions.json …")
    improvement_distributions, improvement_distributions_discounted = (
        build_improvement_distributions(rating_transitions)
    )

    conferences = sorted({a["conference"] for a in athletes.values() if a["conference"]})
    xc_regions  = sorted({a["xc_region"]  for a in athletes.values() if a["xc_region"]})

    bundle = {
        "athletes":                              athletes,
        "improvement_distributions":             improvement_distributions,
        "improvement_distributions_discounted":  improvement_distributions_discounted,
        "percentile_benchmarks":                 percentile_tables,   # passed through as-is
        "conferences":                           conferences,
        "xc_regions":                            xc_regions,
        "current_season":                        args.season,
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