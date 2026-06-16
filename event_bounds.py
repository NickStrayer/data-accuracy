"""
Plausible time bounds for focus distance events.

Marks outside these ranges are excluded from analysis, visualization, and
Monte Carlo — not deleted from the database.

Improvement % outliers are trimmed per starting decile (within each
event x transition bucket) using the percentiles below.
"""

from __future__ import annotations

import numpy as np

# Fastest plausible mark (seconds) — reject if time_seconds < floor
EVENT_FASTEST_SECONDS: dict[str, float] = {
    "8K_XC":  22 * 60,        # 22:00
    "6K_XC":  16 * 60,        # 16:00
    "5K_XC":  13 * 60,        # 13:00
    "10_000": 26 * 60 + 30,   # 26:30  (track 10k)
    "5000M":  12 * 60 + 50,   # 12:50
    "1500M":  3 * 60 + 29,    # 3:29
}

# Slowest plausible mark (seconds) — reject if time_seconds > cap
EVENT_SLOWEST_SECONDS: dict[str, float] = {
    "MILE": 8 * 60,  # 8:00
}

BOUNDED_EVENTS = set(EVENT_FASTEST_SECONDS) | set(EVENT_SLOWEST_SECONDS)

# Per starting-decile improvement trim (within each transition bucket).
IMPROVEMENT_TRIM_LOW_PCT = 0.5
IMPROVEMENT_TRIM_HIGH_PCT = 99.5
MIN_DECILE_SAMPLES_FOR_IMPROVEMENT_TRIM = 5


def decile_improvement_bounds(improvements: list[float]) -> tuple[float, float] | None:
    """P0.5/P99.5 band for one decile bucket; None if too few samples."""
    if len(improvements) < MIN_DECILE_SAMPLES_FOR_IMPROVEMENT_TRIM:
        return None
    lo, hi = np.percentile(
        improvements,
        [IMPROVEMENT_TRIM_LOW_PCT, IMPROVEMENT_TRIM_HIGH_PCT],
    )
    return float(lo), float(hi)


def trim_improvement_records_by_decile(
    records: list[dict],
    *,
    decile_key: str = "from_decile",
    imp_key: str = "imp",
) -> list[dict]:
    """
    Drop transition pairs whose improvement % falls outside the decile's
    P0.5–P99.5 band. Deciles with fewer than MIN_DECILE_SAMPLES_FOR_IMPROVEMENT_TRIM
    pairs are left untrimmed.
    """
    if not records:
        return records

    by_decile: dict[int, list[dict]] = {}
    for rec in records:
        by_decile.setdefault(int(rec[decile_key]), []).append(rec)

    kept: list[dict] = []
    for decile in range(1, 11):
        bucket = by_decile.get(decile, [])
        if not bucket:
            continue
        bounds = decile_improvement_bounds([float(b[imp_key]) for b in bucket])
        if bounds is None:
            kept.extend(bucket)
            continue
        lo, hi = bounds
        kept.extend(b for b in bucket if lo <= float(b[imp_key]) <= hi)
    return kept


def is_plausible_time(event_code: str, time_seconds: float | None) -> bool:
    if time_seconds is None or time_seconds <= 0:
        return False
    floor = EVENT_FASTEST_SECONDS.get(event_code)
    cap   = EVENT_SLOWEST_SECONDS.get(event_code)
    if floor is not None and time_seconds < floor:
        return False
    if cap is not None and time_seconds > cap:
        return False
    return True


def _col(alias: str, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def sql_plausible_time_where(alias: str = "r", focus_codes: tuple[str, ...] | None = None) -> str:
    """
    SQL fragment: true when a result row passes bounds for its event_code.
    Unbounded events pass through (still require caller's time_seconds > 0).
  Pass alias='' when the results table has no alias.
    """
    ec = _col(alias, "event_code")
    ts = _col(alias, "time_seconds")
    clauses: list[str] = []
    for event in sorted(BOUNDED_EVENTS):
        parts = [f"{ec} = '{event}'"]
        if event in EVENT_FASTEST_SECONDS:
            parts.append(f"{ts} >= {EVENT_FASTEST_SECONDS[event]}")
        if event in EVENT_SLOWEST_SECONDS:
            parts.append(f"{ts} <= {EVENT_SLOWEST_SECONDS[event]}")
        clauses.append("(" + " AND ".join(parts) + ")")

    if focus_codes:
        unbounded = [c for c in focus_codes if c not in BOUNDED_EVENTS]
        if unbounded:
            ub = ",".join(f"'{c}'" for c in unbounded)
            clauses.append(f"({ec} IN ({ub}))")
    else:
        bounded_in = ",".join(f"'{e}'" for e in sorted(BOUNDED_EVENTS))
        clauses.append(f"({ec} NOT IN ({bounded_in}))")

    return "(" + " OR ".join(clauses) + ")"
