"""
analyze_progression.py - Computes longitudinal development statistics from
the TFRRS SQLite database and generates simulator-ready JSON outputs.

Outputs written to ./output/:
  development_curves.json
  attrition_rates.json
  breakout_rates.json
  rating_transitions.json
  percentile_tables.json
  progression_full.json
  analysis_summary.json

KEY CHANGE vs previous version:
  Class years are derived by ranking each athlete's active seasons
  chronologically (1st season = FR, 2nd = SO, 3rd = JR, 4th = SR, 5th = 5TH).
  This correctly handles gap years (injury, COVID, redshirt) — a calendar gap
  between seasons still just increments the rank by 1, so class labels are
  never skipped or pushed to NULL due to missing years.

  The previous approach used a calendar offset from first_year, which caused
  gap-year athletes to have their class labels skip (e.g. FR→SO→SR, missing JR)
  and eventually fall off the end into NULL.

  The even older result_acad / acad_year CTE approach was also broken because
  spring results within the same season_year got a different acad_year than
  fall results, producing duplicate class labels per season (e.g. both FR and
  SO for season_year=2016), which caused _iter_transition_pairs to find no
  valid cross-season pairs and produce ~0% progression across the board.
"""

import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from database import get_connection, DB_PATH

logger = logging.getLogger(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

CLASS_ORDER  = ["FR", "SO", "JR", "SR", "5TH"]
TRANSITIONS  = [("FR","SO"), ("SO","JR"), ("JR","SR"), ("SR","5TH")]

# Events to include — must match event_code values actually in the DB
FOCUS_EVENTS = {
    "8K_XC":  "8K XC",
    "6K_XC":  "6K XC",
    "5K_XC":  "5K XC",
    "10K_XC": "10K XC",
    "5000M":  "5000m",
    "10_000": "10000m",
    "1500M":  "1500m",
    "3000M":  "3000m",
    "800M":   "800m",
    "MILE":   "Mile",
}
FOCUS_CODES = tuple(FOCUS_EVENTS.keys())

PERCENTILE_BREAKPOINTS = [5, 10, 25, 50, 75, 90, 95]

from event_bounds import (
    is_plausible_time,
    sql_plausible_time_where,
    trim_improvement_records_by_decile,
)
#  Year-by-year NCAA field percentiles (used to discount overall progression)
# ─────────────────────────────────────────────────────────────────────────────

def compute_yearly_field_stats(df_marks: pd.DataFrame) -> dict:
    """
    For each event_code/gender/season_year, compute the NCAA-wide percentile
    times (5/10/25/50/75/90/95). This captures how the *whole field* moved
    year over year (course changes, shoe tech, depth of competition, etc.)
    independent of any individual athlete's class-year progression.

    Output shape:
      { event_code: { gender: [ {year, n, p5, p10, ..., p95}, ... sorted by year ] } }
    """
    output: dict = {}
    for (event_code, gender), grp in df_marks.groupby(["event_code", "gender"]):
        by_year = {}
        for year, yr_df in grp.groupby("season_year"):
            times = yr_df["best_time"].dropna().values
            if len(times) < 5:
                continue
            row = {"year": int(year), "n": int(len(times))}
            for p in PERCENTILE_BREAKPOINTS:
                # Lower time = faster, so "p95" (top 5%) corresponds to the
                # 5th percentile of the time distribution, matching the
                # convention used in compute_percentile_tables.
                row[f"p{p}"] = round(float(np.percentile(times, 100 - p)), 3)
            by_year[int(year)] = row
        if by_year:
            output.setdefault(event_code, {})[gender] = [
                by_year[y] for y in sorted(by_year)
            ]
    return output


def _build_field_curve(yearly_field_stats: dict, event_code: str, gender: str) -> dict[int, float]:
    """Return {season_year: median_time} for use as a discounting baseline."""
    rows = yearly_field_stats.get(event_code, {}).get(gender, [])
    return {row["year"]: row["p50"] for row in rows if row.get("p50") is not None}

# ─────────────────────────────────────────────────────────────────────────────
#  Shared SQL fragment — infers class year from chronological season rank
#
#  Class year = rank of the athlete's active seasons in calendar order:
#    1st season = FR, 2nd = SO, 3rd = JR, 4th = SR, 5th = 5TH
#
#  Gap years (injury, COVID, redshirt) are handled correctly because rank
#  increments by 1 per active season regardless of calendar gaps between them.
#
#  season_year is already academic-year-anchored by the parser:
#    Aug-Dec results → season_year = calendar_year + 1
#    Jan-Jul results → season_year = calendar_year
#  So season_year=2016 means the 2015-16 academic year for every result in
#  that bucket, fall or spring — one class label per season, no ambiguity.
# ─────────────────────────────────────────────────────────────────────────────

_CLASS_YEAR_CTE = """
    athlete_season_rank AS (
        -- Rank each athlete's active seasons in chronological order.
        -- Uses DISTINCT season_year so multiple results in the same season
        -- don't inflate the rank.
        SELECT athlete_id,
               season_year,
               ROW_NUMBER() OVER (
                   PARTITION BY athlete_id
                   ORDER BY season_year
               ) AS season_rank
        FROM (
            SELECT DISTINCT athlete_id, season_year
            FROM results
        )
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
                   ELSE NULL
               END AS class_year
        FROM athlete_season_rank
    )
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_best_marks(db_path=DB_PATH) -> pd.DataFrame:
    """
    One best time per athlete × season_year × event, focus events only.
    class_year is inferred from chronological season rank via _CLASS_YEAR_CTE.
    """
    placeholders = ",".join(f"'{c}'" for c in FOCUS_CODES)
    sql = f"""
    WITH {_CLASS_YEAR_CTE},
    best_marks AS (
        SELECT r.athlete_id,
               r.season_year,
               r.event_code,
               r.event_type,
               r.distance_meters,
               MIN(r.time_seconds) AS best_time,
               a.gender
        FROM results r
        JOIN athletes a ON a.athlete_id = r.athlete_id
        WHERE r.time_seconds IS NOT NULL
          AND r.time_seconds > 0
          AND r.event_code IN ({placeholders})
          AND {sql_plausible_time_where("r", FOCUS_CODES)}
        GROUP BY r.athlete_id, r.season_year, r.event_code
    )
    SELECT b.athlete_id, b.season_year, b.event_code,
           b.event_type, b.distance_meters, b.best_time,
           b.gender, ac.class_year
    FROM best_marks b
    LEFT JOIN athlete_classes ac
           ON ac.athlete_id  = b.athlete_id
          AND ac.season_year = b.season_year
    """
    with get_connection(db_path) as conn:
        df = pd.read_sql_query(sql, conn)
    logger.info("Loaded %d best-mark rows (focus events)", len(df))
    return df


def load_seasons_df(db_path=DB_PATH) -> pd.DataFrame:
    """
    Season rows with SQL-inferred class years and school info.
    """
    sql = f"""
    WITH {_CLASS_YEAR_CTE}
    SELECT s.athlete_id, s.season_year,
           ac.class_year,
           s.is_redshirt,
           a.gender,
           sc.school_name,
           sc.division
    FROM seasons s
    JOIN athletes a         ON a.athlete_id  = s.athlete_id
    LEFT JOIN schools sc    ON sc.school_id  = s.school_id
    LEFT JOIN athlete_classes ac
           ON ac.athlete_id  = s.athlete_id
          AND ac.season_year = s.season_year
    """
    with get_connection(db_path) as conn:
        df = pd.read_sql_query(sql, conn)
    logger.info("Loaded %d season rows", len(df))
    return df


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def pct_improvement(from_time: float, to_time: float) -> float:
    """Positive = faster (improvement); negative = slower."""
    if from_time <= 0:
        return np.nan
    return (from_time - to_time) / from_time * 100.0


def _discounted_improvement(
    imp: float, fy: int, ty: int, field_curve: dict | None,
) -> float:
    disc = imp
    if field_curve:
        fc_from = field_curve.get(fy)
        fc_to = field_curve.get(ty)
        if fc_from is not None and fc_to is not None:
            field_imp = pct_improvement(fc_from, fc_to)
            if not np.isnan(field_imp):
                disc = imp - field_imp
    return disc


def _add_fixed_deciles(g_df: pd.DataFrame) -> pd.DataFrame:
    """Per season_year cohort: rating + fixed-boundary decile (matches compute_ratings)."""
    parts: list[pd.DataFrame] = []
    for _season_year, grp in g_df.groupby("season_year"):
        if len(grp) < 2:
            continue
        ranked = grp.copy()
        ranked["rank"] = ranked["best_time"].rank(method="min", ascending=True)
        n = len(ranked)
        ranked["rating"] = 100.0 * (1 - (ranked["rank"] - 1) / n)
        ranked["decile"] = ranked["rating"].apply(
            lambda r: max(1, min(10, int(r / 10) + 1))
        )
        parts.append(ranked)
    if not parts:
        return g_df.copy()
    return pd.concat(parts, ignore_index=True)


def _collect_transition_pair_records(
    g_df: pd.DataFrame,
    from_cls: str,
    to_cls: str,
    event_code: str,
    field_curve: dict | None,
) -> list[dict]:
    """
    One record per athlete transition pair after time-plausibility and
    per-decile P0.5/P99.5 improvement trimming.
    """
    keyed = g_df.set_index(["athlete_id", "season_year", "class_year"], drop=False)
    records: list[dict] = []
    for athlete_id, ft, fy, tt, ty in _iter_transition_pairs(g_df, from_cls, to_cls):
        if not _times_plausible_for_event(ft, tt, event_code):
            continue
        try:
            fr = keyed.loc[(athlete_id, fy, from_cls)]
        except KeyError:
            continue
        if isinstance(fr, pd.DataFrame):
            fr = fr.iloc[0]
        if pd.isna(fr.get("decile")):
            continue
        imp = pct_improvement(ft, tt)
        if np.isnan(imp):
            continue
        records.append({
            "from_decile": int(fr["decile"]),
            "imp":         float(imp),
            "disc":        float(_discounted_improvement(imp, fy, ty, field_curve)),
            "from_time":   ft,
        })
    return trim_improvement_records_by_decile(records)


def _trim_merged_improvements_by_decile(merged: pd.DataFrame) -> pd.DataFrame:
    """Apply per-from_decile P0.5/P99.5 trim on a merged transition DataFrame."""
    if merged.empty:
        return merged
    records = [
        {"from_decile": int(row["from_decile"]), "imp": float(row["imp"]), "_idx": idx}
        for idx, row in merged.iterrows()
        if not np.isnan(row["imp"])
    ]
    kept = trim_improvement_records_by_decile(records)
    keep_idx = {r["_idx"] for r in kept}
    return merged.loc[[i for i in merged.index if i in keep_idx]].copy()


def _iter_transition_pairs(g_df: pd.DataFrame, from_cls: str, to_cls: str):
    """
    For each athlete, pair the best mark from their first from-class season
    with the best mark from their first to-class season strictly afterward.

    Both marks come from distinct season_years (guaranteed by the CTE which
    produces exactly one class_year per athlete × season_year).
    Athletes who stayed the same, slowed down, or sped up are all included.
    """
    elig = g_df[g_df["class_year"].isin([from_cls, to_cls])]
    if elig.empty:
        return

    cells = (
        elig.groupby(["athlete_id", "season_year", "class_year"])["best_time"]
        .min()
        .reset_index()
    )
    cells["season_year"] = cells["season_year"].astype(int)

    for athlete_id, grp in cells.groupby("athlete_id"):
        from_rows = grp[grp["class_year"] == from_cls].sort_values("season_year")
        to_rows   = grp[grp["class_year"] == to_cls].sort_values("season_year")
        if from_rows.empty or to_rows.empty:
            continue

        fy = int(from_rows["season_year"].iloc[0])
        ft = float(from_rows.loc[from_rows["season_year"] == fy, "best_time"].iloc[0])

        later = to_rows[to_rows["season_year"] > fy]
        if later.empty:
            continue
        ty = int(later["season_year"].iloc[0])
        tt = float(later.loc[later["season_year"] == ty, "best_time"].iloc[0])

        if ft <= 0:
            continue
        yield athlete_id, ft, fy, tt, ty


def _transition_improvements(
    g_df: pd.DataFrame,
    field_curve: dict[int, float] | None = None,
    event_code: str | None = None,
) -> dict[str, dict[str, np.ndarray]]:
    """
    Per-transition arrays of % improvement for one event × gender.

    Each athlete contributes at most one pair: best time from their first
    from-class season, best time from their first to-class season after that.
    Zero and negative changes are kept; only unpaired or same-season data is
    excluded. Pairs outside each starting decile's P0.5-P99.5 band are dropped.
    """
    if not event_code:
        raise ValueError("event_code is required for transition improvement trimming")
    g_enriched = _add_fixed_deciles(g_df)
    buckets: dict[str, dict[str, np.ndarray]] = {}
    for from_cls, to_cls in TRANSITIONS:
        key = f"{from_cls}_to_{to_cls}"
        records = _collect_transition_pair_records(
            g_enriched, from_cls, to_cls, event_code, field_curve
        )
        if len(records) >= 5:
            buckets[key] = {
                "raw":        np.array([r["imp"] for r in records]),
                "discounted": np.array([r["disc"] for r in records]),
            }

    return buckets


# ─────────────────────────────────────────────────────────────────────────────
#  1. Progression curves
# ─────────────────────────────────────────────────────────────────────────────

def compute_progression(df_marks: pd.DataFrame, yearly_field_stats: dict) -> dict:
    output: dict = {}

    for event_code, ev_df in df_marks.groupby("event_code"):
        output[event_code] = {}
        for gender in ["M", "F"]:
            g_df = ev_df[ev_df["gender"] == gender]
            if len(g_df) < 10:
                continue
            field_curve = _build_field_curve(yearly_field_stats, event_code, gender)
            buckets = _transition_improvements(g_df, field_curve, event_code)
            if not buckets:
                continue
            output[event_code][gender] = {}
            for key, variants in buckets.items():
                entry = {}
                for variant_name, pcts in variants.items():
                    prefix = "" if variant_name == "raw" else "discounted_"
                    entry[f"{prefix}n"]      = int(len(pcts))
                    entry[f"{prefix}mean"]   = float(np.mean(pcts))
                    entry[f"{prefix}median"] = float(np.median(pcts))
                    entry[f"{prefix}std"]    = float(np.std(pcts, ddof=1)) if len(pcts) > 1 else 0.0
                    for p in PERCENTILE_BREAKPOINTS:
                        entry[f"{prefix}p{p}"] = float(np.percentile(pcts, p))
                output[event_code][gender][key] = entry

    return output


# ─────────────────────────────────────────────────────────────────────────────
#  2. Breakout rates  (empirical)
# ─────────────────────────────────────────────────────────────────────────────

# Threshold fractions of the event/gender P50 time used to define a "breakout".
# e.g. 0.01 → athlete must improve by at least 1% of the median event time in seconds.
BREAKOUT_FRACTIONS = [0.01, 0.03, 0.05, 0.07, 0.10]


def _breakout_thresholds_seconds(
    df_marks: pd.DataFrame, event_code: str, gender: str
) -> list[float]:
    """
    Return the absolute-second thresholds for this event/gender by multiplying
    each fraction in BREAKOUT_FRACTIONS by the overall P50 (median) time for
    that event/gender across all seasons.

    Returns a list of floats (seconds), one per fraction, rounded to 2 dp.
    Falls back to an empty list if there is insufficient data.
    """
    times = (
        df_marks[
            (df_marks["event_code"] == event_code) & (df_marks["gender"] == gender)
        ]["best_time"]
        .dropna()
        .values
    )
    if len(times) < 5:
        return []
    median_time = float(np.median(times))
    return [round(median_time * f, 2) for f in BREAKOUT_FRACTIONS]


def compute_breakout_rates_empirical(df_marks: pd.DataFrame, yearly_field_stats: dict) -> dict:
    output: dict = {}

    for event_code, ev_df in df_marks.groupby("event_code"):
        output[event_code] = {}
        for gender in ["M", "F"]:
            g_df = ev_df[ev_df["gender"] == gender]
            thresholds_s = _breakout_thresholds_seconds(df_marks, event_code, gender)
            if not thresholds_s:
                continue

            all_times = g_df["best_time"].dropna().values
            if len(all_times) < 5:
                continue
            median_time = float(np.median(all_times))

            field_curve = _build_field_curve(yearly_field_stats, event_code, gender)
            output[event_code][gender] = {
                "_thresholds_seconds": thresholds_s,
            }

            g_enriched = _add_fixed_deciles(g_df)
            for from_cls, to_cls in TRANSITIONS:
                key = f"{from_cls}_to_{to_cls}"
                records = _collect_transition_pair_records(
                    g_enriched, from_cls, to_cls, event_code, field_curve
                )
                paired = [(r["from_time"], r["imp"], r["disc"]) for r in records]

                if not paired:
                    continue

                from_times_arr = np.array([p[0] for p in paired])
                n_from = len(from_times_arr)
                sorted_idx = np.argsort(from_times_arr)
                pct_by_sorted = np.empty(n_from)
                for rank_i, orig_i in enumerate(sorted_idx):
                    pct_by_sorted[orig_i] = round(100.0 * (1 - rank_i / max(n_from - 1, 1)), 2)

                raw_pts, disc_pts = [], []
                for i, (_ft, imp, disc) in enumerate(paired):
                    raw_pts.append([round(float(pct_by_sorted[i]), 2), round(float(imp), 2)])
                    disc_pts.append([round(float(pct_by_sorted[i]), 2), round(float(disc), 2)])

                row = {
                    "n": int(len(paired)),
                    "athlete_points":            raw_pts,
                    "athlete_points_discounted": disc_pts,
                }
                raw_arr  = np.array([p[1] for p in paired])
                disc_arr = np.array([p[2] for p in paired])
                if median_time and median_time > 0:
                    for thr_s in thresholds_s:
                        thr_pct  = (thr_s / median_time) * 100.0
                        key_name = f"p_improve_{thr_s}s"
                        row[key_name]                 = round(float((raw_arr  >= thr_pct).mean()), 4)
                        row[key_name + "_discounted"] = round(float((disc_arr >= thr_pct).mean()), 4)
                output[event_code][gender][key] = row

    return output


# ─────────────────────────────────────────────────────────────────────────────
#  3. Attrition / return rates
# ─────────────────────────────────────────────────────────────────────────────

def compute_attrition(df_seasons: pd.DataFrame) -> dict:
    output: dict = {}
    df = df_seasons.copy()

    # Exclude athletes whose most recent season IS the global max year —
    # they are currently active and may not have had a chance to return yet.
    max_year = int(df["season_year"].max())

    for from_cls, to_cls in TRANSITIONS:
        from_df = (
            df[(df["class_year"] == from_cls) & (df["season_year"] < max_year)]
            [["athlete_id", "season_year", "school_name"]]
            .rename(columns={"school_name": "from_school", "season_year": "from_year"})
        )

        to_df = (
            df[df["class_year"] == to_cls]
            [["athlete_id", "season_year", "school_name"]]
            .rename(columns={"school_name": "to_school", "season_year": "to_year"})
        )

        merged = from_df.merge(to_df, on="athlete_id", how="left")
        merged = merged[
            merged["to_year"].isna() | (merged["to_year"] > merged["from_year"])
        ]
        merged = merged.sort_values("from_year").drop_duplicates("athlete_id")

        n_total = len(merged)
        if n_total == 0:
            continue

        returned    = int(merged["to_year"].notna().sum())
        transferred = int((
            merged["from_school"].notna()
            & merged["to_school"].notna()
            & (merged["from_school"] != merged["to_school"])
        ).sum())
        redshirt_ids = set(df[df["is_redshirt"] == 1]["athlete_id"])
        redshirt_n   = len(set(merged["athlete_id"]) & redshirt_ids)

        output[f"{from_cls}_to_{to_cls}"] = {
            "n":              int(n_total),
            "return_rate":    round(returned    / n_total, 4),
            "transfer_rate":  round(transferred / n_total, 4),
            "redshirt_rate":  round(redshirt_n  / n_total, 4),
            "attrition_rate": round(1 - returned / n_total, 4),
        }

    # SR → 5TH supplemental stat
    sr_ids    = set(df[(df["class_year"] == "SR") & (df["season_year"] < max_year)]["athlete_id"])
    fifth_ids = set(df[df["class_year"] == "5TH"]["athlete_id"])
    if sr_ids:
        output["SR_graduation"] = {
            "n":               int(len(sr_ids)),
            "fifth_year_rate": round(len(sr_ids & fifth_ids) / len(sr_ids), 4),
        }

    return output


# ─────────────────────────────────────────────────────────────────────────────
#  4. Percentile ratings
# ─────────────────────────────────────────────────────────────────────────────

def compute_ratings(df_marks: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (event_code, gender, season_year), grp in df_marks.groupby(
        ["event_code", "gender", "season_year"]
    ):
        if len(grp) < 2:
            continue
        ranked = grp.assign(rank=grp["best_time"].rank(method="min", ascending=True))
        n = len(ranked)
        ranked = ranked.assign(rating=lambda d: 100.0 * (1 - (d["rank"] - 1) / n))
        rows.append(ranked[["athlete_id","season_year","event_code",
                             "gender","class_year","best_time","rating"]])
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  5. Rating transition matrix
# ─────────────────────────────────────────────────────────────────────────────

def _merge_class_transition(g, from_cls: str, to_cls: str) -> pd.DataFrame:
    """
    One row per athlete: best mark from their first from_cls season paired with
    the best mark from their first to_cls season strictly afterward.
    """
    if g.empty:
        return pd.DataFrame()

    keyed = g.set_index(["athlete_id", "season_year", "class_year"], drop=False)
    rows: list[dict] = []
    for athlete_id, ft, fy, tt, ty in _iter_transition_pairs(g, from_cls, to_cls):
        try:
            fr = keyed.loc[(athlete_id, fy, from_cls)]
            tr = keyed.loc[(athlete_id, ty, to_cls)]
        except KeyError:
            continue
        if isinstance(fr, pd.DataFrame):
            fr, tr = fr.iloc[0], tr.iloc[0]
        rows.append({
            "athlete_id":   athlete_id,
            "from_decile":  fr["decile"],
            "from_year":    fy,
            "from_rating":  fr["rating"],
            "from_time":    ft,
            "to_decile":    tr["decile"],
            "to_year":      ty,
            "to_time":      tt,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).dropna(subset=["from_decile", "to_decile"])


def _time_plausible(time_sec: float, event_code: str | None) -> bool:
    return is_plausible_time(event_code or "", time_sec)


def _times_plausible_for_event(
    from_time: float,
    to_time: float,
    event_code: str | None,
) -> bool:
    return _time_plausible(from_time, event_code) and _time_plausible(to_time, event_code)


def _filter_transition_pairs(
    merged: pd.DataFrame,
    event_code: str | None,
) -> pd.DataFrame:
    """Keep only pairs whose from/to marks are not impossibly fast for the event."""
    if merged.empty:
        return merged
    keep: list[bool] = []
    for _, row in merged.iterrows():
        ft, tt = float(row["from_time"]), float(row["to_time"])
        keep.append(_times_plausible_for_event(ft, tt, event_code))
    return merged[np.array(keep)].copy()


def _build_rating_transition_block(
    merged: pd.DataFrame,
    field_curve: dict,
    event_code: str | None = None,
):
    merged = _filter_transition_pairs(merged, event_code)
    if len(merged) < 5:
        return None

    merged = merged.copy()
    merged["imp"] = [
        pct_improvement(float(ft), float(tt))
        for ft, tt in zip(merged["from_time"], merged["to_time"])
    ]
    merged = _trim_merged_improvements_by_decile(merged)
    if len(merged) < 5:
        return None

    matrix: dict[str, dict[str, float]] = {}
    by_cell: dict[str, dict[str, list]] = {}
    by_from: dict[str, list] = {str(d): [] for d in range(1, 11)}

    for fd in range(1, 11):
        sub   = merged[merged["from_decile"] == fd]
        total = len(sub)
        row_map: dict[str, float] = {}
        for td in range(1, 11):
            p = float((sub["to_decile"] == td).sum() / total) if total > 0 else 0.0
            if p > 0:
                row_map[str(td)] = round(p, 4)
        matrix[str(fd)] = row_map

    improvements: list = []
    improvements_discounted: list = []
    triples: list = []
    for _, row in merged.iterrows():
        fd = str(int(row["from_decile"]))
        td = str(int(row["to_decile"]))
        imp = float(row["imp"])
        if np.isnan(imp):
            continue

        imp_r = round(imp, 2)
        improvements.append([round(float(row["from_rating"]), 2), imp_r])
        triples.append([int(fd), int(td), imp_r])
        by_from.setdefault(fd, []).append(imp_r)
        by_cell.setdefault(fd, {}).setdefault(td, []).append(imp_r)

        disc = imp
        fc_from = field_curve.get(int(row["from_year"]))
        fc_to   = field_curve.get(int(row["to_year"]))
        if fc_from is not None and fc_to is not None:
            field_imp = pct_improvement(fc_from, fc_to)
            if not np.isnan(field_imp):
                disc = imp - field_imp
        improvements_discounted.append(
            [round(float(row["from_rating"]), 2), round(float(disc), 2)]
        )

    return {
        "n":                       int(len(merged)),
        "matrix":                  matrix,
        "improvements_by_cell":    by_cell,
        "improvements_by_from_decile": by_from,
        "improvement_triples":     triples,
        "improvements":            improvements,
        "improvements_discounted": improvements_discounted,
    }


def compute_rating_transitions(
    df_ratings: pd.DataFrame,
    yearly_field_stats: dict,
) -> dict:
    output: dict = {}
    if df_ratings.empty:
        return output

    df = df_ratings.copy()
    # Fixed-boundary decile: rating 0–10 → 1, 10–20 → 2, …, 90–100 → 10.
    # Must match the identical formula in predict_monticarlo.py (_rating_to_decile)
    # so that distribution buckets and athlete lookups use the same decile scale.
    # pd.cut was wrong here because it split the *observed* rating range into 10
    # equal-width bins, so boundaries shifted with each season's data — meaning a
    # rating of 85 could land in decile 8 one run and decile 9 the next.
    df["decile"] = df["rating"].apply(lambda r: max(1, min(10, int(r / 10) + 1)))

    for event_code, ev in df.groupby("event_code"):
        output[event_code] = {}
        for gender in ["M", "F"]:
            g = ev[ev["gender"] == gender]
            output[event_code][gender] = {}
            field_curve = _build_field_curve(yearly_field_stats, event_code, gender)

            for from_cls, to_cls in TRANSITIONS:
                merged = _merge_class_transition(g, from_cls, to_cls)
                block  = _build_rating_transition_block(merged, field_curve, event_code)
                if block:
                    output[event_code][gender][f"{from_cls}_to_{to_cls}"] = block

            merged = _merge_class_transition(g, "FR", "SR")
            block  = _build_rating_transition_block(merged, field_curve, event_code)
            if block:
                output[event_code][gender]["FR_to_SR"] = block

    return output


# ─────────────────────────────────────────────────────────────────────────────
#  6. Percentile tables
# ─────────────────────────────────────────────────────────────────────────────

def compute_percentile_tables(df_marks: pd.DataFrame) -> dict:
    output: dict = {}
    for (event_code, gender), grp in df_marks.groupby(["event_code","gender"]):
        if len(grp) < 20:
            continue
        times = grp["best_time"].dropna().values
        output.setdefault(event_code, {})[gender] = {
            f"p{p}": round(float(np.percentile(times, 100 - p)), 2)
            for p in PERCENTILE_BREAKPOINTS
        }
    return output


# ─────────────────────────────────────────────────────────────────────────────
#  7. Aggregate development curves
# ─────────────────────────────────────────────────────────────────────────────

def compute_aggregate_curves(progression: dict) -> dict:
    output: dict = {}
    for event_code, genders in progression.items():
        output[event_code] = {}
        for gender, transitions in genders.items():
            output[event_code][gender] = {}
            for trans_key, stats in transitions.items():
                from_cls = trans_key.split("_to_")[0]
                output[event_code][gender][from_cls] = {
                    "mean":              round(stats["mean"],   3),
                    "std":               round(stats["std"],    3),
                    "median":            round(stats["median"], 3),
                    "n":                 stats["n"],
                    "discounted_mean":   round(stats.get("discounted_mean",   stats["mean"]),   3),
                    "discounted_median": round(stats.get("discounted_median", stats["median"]), 3),
                    "discounted_std":    round(stats.get("discounted_std",    stats["std"]),    3),
                }
    return output


# ─────────────────────────────────────────────────────────────────────────────
#  Persist progression_stats to DB
# ─────────────────────────────────────────────────────────────────────────────

def persist_progression_stats(progression: dict, db_path=DB_PATH) -> None:
    rows = []
    for event_code, genders in progression.items():
        for gender, transitions in genders.items():
            for trans_key, stats in transitions.items():
                from_cls, to_cls = trans_key.split("_to_")
                rows.append({
                    "event_code":             event_code,
                    "from_class":             from_cls,
                    "to_class":               to_cls,
                    "gender":                 gender,
                    "n":                      stats["n"],
                    "mean_improvement_pct":   stats["mean"],
                    "median_improvement_pct": stats["median"],
                    "std_improvement_pct":    stats["std"],
                    "p10":                    stats.get("p10"),
                    "p25":                    stats.get("p25"),
                    "p75":                    stats.get("p75"),
                    "p90":                    stats.get("p90"),
                    "discounted_mean_improvement_pct":   stats.get("discounted_mean"),
                    "discounted_median_improvement_pct": stats.get("discounted_median"),
                    "discounted_std_improvement_pct":    stats.get("discounted_std"),
                    "discounted_p10": stats.get("discounted_p10"),
                    "discounted_p25": stats.get("discounted_p25"),
                    "discounted_p75": stats.get("discounted_p75"),
                    "discounted_p90": stats.get("discounted_p90"),
                })
    with get_connection(db_path) as conn:
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(progression_stats)").fetchall()
        }
        new_cols = [
            "discounted_mean_improvement_pct",
            "discounted_median_improvement_pct",
            "discounted_std_improvement_pct",
            "discounted_p10", "discounted_p25", "discounted_p75", "discounted_p90",
        ]
        for col in new_cols:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE progression_stats ADD COLUMN {col} REAL")

        conn.execute("DELETE FROM progression_stats")
        conn.executemany(
            """INSERT INTO progression_stats
               (event_code, from_class, to_class, gender, n,
                mean_improvement_pct, median_improvement_pct, std_improvement_pct,
                p10, p25, p75, p90,
                discounted_mean_improvement_pct, discounted_median_improvement_pct,
                discounted_std_improvement_pct,
                discounted_p10, discounted_p25, discounted_p75, discounted_p90)
               VALUES(:event_code,:from_class,:to_class,:gender,:n,
                      :mean_improvement_pct,:median_improvement_pct,
                      :std_improvement_pct,:p10,:p25,:p75,:p90,
                      :discounted_mean_improvement_pct,:discounted_median_improvement_pct,
                      :discounted_std_improvement_pct,
                      :discounted_p10,:discounted_p25,:discounted_p75,:discounted_p90)""",
            rows,
        )
    logger.info("Persisted %d progression_stats rows", len(rows))


# ─────────────────────────────────────────────────────────────────────────────
#  Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis(db_path=DB_PATH) -> dict:
    logger.info("Loading data from %s …", db_path)
    df_marks   = load_best_marks(db_path)
    df_seasons = load_seasons_df(db_path)

    if df_marks.empty:
        logger.warning("No mark data found — run scraping & parsing first.")
        _write_empty_outputs()
        return {}

    logger.info("Computing year-by-year NCAA field percentiles …")
    yearly_field_stats = compute_yearly_field_stats(df_marks)

    logger.info("Computing progression curves …")
    progression = compute_progression(df_marks, yearly_field_stats)
    persist_progression_stats(progression, db_path)

    logger.info("Computing breakout rates …")
    breakout = compute_breakout_rates_empirical(df_marks, yearly_field_stats)

    logger.info("Computing attrition …")
    attrition = compute_attrition(df_seasons)

    logger.info("Computing ratings …")
    df_ratings = compute_ratings(df_marks)

    logger.info("Computing percentile tables …")
    pct_tables = compute_percentile_tables(df_marks)

    logger.info("Computing rating transitions …")
    rating_trans = compute_rating_transitions(df_ratings, yearly_field_stats)

    curves = compute_aggregate_curves(progression)

    _write_json("development_curves.json", curves)
    _write_json("attrition_rates.json",    attrition)
    _write_json("breakout_rates.json",     breakout)
    _write_json("rating_transitions.json", rating_trans)
    _write_json("percentile_tables.json",  pct_tables)
    _write_json("progression_full.json",   progression)
    _write_json("yearly_trends.json",      yearly_field_stats)

    summary = {
        "events_analysed":  len(progression),
        "athletes_rated":   int(df_ratings["athlete_id"].nunique()) if not df_ratings.empty else 0,
        "total_results":    len(df_marks),
        "attrition_keys":   list(attrition.keys()),
    }
    _write_json("analysis_summary.json", summary)

    logger.info("Analysis complete. Outputs in %s", OUTPUT_DIR)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _write_json(filename: str, data) -> None:
    path = OUTPUT_DIR / filename
    path.write_text(json.dumps(data, indent=2, default=_json_default), encoding="utf-8")
    logger.info("  Wrote %s (%d bytes)", filename, path.stat().st_size)


def _json_default(obj):
    if isinstance(obj, np.integer):  return int(obj)
    if isinstance(obj, np.floating): return float(obj)
    if isinstance(obj, np.ndarray):  return obj.tolist()
    raise TypeError(f"Not serialisable: {type(obj)}")


def _write_empty_outputs():
    for fname in ["development_curves.json", "attrition_rates.json",
                  "breakout_rates.json",     "rating_transitions.json",
                  "percentile_tables.json",  "analysis_summary.json",
                  "progression_full.json",   "yearly_trends.json"]:
        _write_json(fname, {})


if __name__ == "__main__":
    import sys

    LOG_DIR = Path(__file__).parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "analyze_progression.log"),
        ],
    )
    summary = run_analysis()
    print("\n✓ Analysis summary:", json.dumps(summary, indent=2))