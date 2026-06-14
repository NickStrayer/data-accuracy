"""
database.py - SQLite schema creation, connection management, and batch insert helpers.
"""

import sqlite3
import logging
import contextlib
from pathlib import Path
from typing import Generator

DB_PATH = Path(__file__).parent / "tfrrs.db"
logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA cache_size=-64000;   -- 64 MB page cache

-- ──────────────────────────────────────────────
--  SCHOOLS
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schools (
    school_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    school_name TEXT    NOT NULL UNIQUE,
    division    TEXT    CHECK(division IN ('I','II','III','NAIA','Unknown'))
                        DEFAULT 'Unknown',
    tfrrs_id    TEXT,
    state       TEXT,
    conference  TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_schools_name ON schools(school_name);

-- ──────────────────────────────────────────────
--  ATHLETES
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id   TEXT PRIMARY KEY,           -- TFRRS numeric ID as text
    athlete_name TEXT NOT NULL,
    gender       TEXT CHECK(gender IN ('M','F','Unknown')) DEFAULT 'Unknown',
    school_id    INTEGER REFERENCES schools(school_id) ON DELETE SET NULL,
    profile_url  TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_athletes_name   ON athletes(athlete_name);
CREATE INDEX IF NOT EXISTS idx_athletes_school ON athletes(school_id);

-- ──────────────────────────────────────────────
--  SEASONS  (one row per athlete × academic year)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS seasons (
    season_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id  TEXT    NOT NULL REFERENCES athletes(athlete_id) ON DELETE CASCADE,
    season_year INTEGER NOT NULL,            -- e.g. 2023 = 2022-23 academic year
    school_id   INTEGER REFERENCES schools(school_id) ON DELETE SET NULL,
    class_year  TEXT    CHECK(class_year IN ('FR','SO','JR','SR','5TH','Unknown'))
                        DEFAULT 'Unknown',
    is_redshirt INTEGER DEFAULT 0,
    UNIQUE(athlete_id, season_year)
);
CREATE INDEX IF NOT EXISTS idx_seasons_athlete ON seasons(athlete_id);
CREATE INDEX IF NOT EXISTS idx_seasons_year    ON seasons(season_year);

-- ──────────────────────────────────────────────
--  RESULTS
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS results (
    result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id      TEXT    NOT NULL REFERENCES athletes(athlete_id) ON DELETE CASCADE,
    season_year     INTEGER NOT NULL,
    result_date     DATE,
    meet_name       TEXT,
    event_code      TEXT    NOT NULL,        -- e.g. '6K_XC', '5000m', '1500m'
    event_type      TEXT    CHECK(event_type IN ('XC','INDOOR','OUTDOOR','Unknown'))
                            DEFAULT 'Unknown',
    distance_meters REAL,
    time_seconds    REAL,
    place           INTEGER,
    wind_mps        REAL,                    -- wind reading (outdoor sprints/jumps)
    source_url      TEXT,
    UNIQUE(athlete_id, season_year, result_date, event_code, time_seconds)
);
CREATE INDEX IF NOT EXISTS idx_results_athlete  ON results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_results_season   ON results(season_year);
CREATE INDEX IF NOT EXISTS idx_results_event    ON results(event_code);
CREATE INDEX IF NOT EXISTS idx_results_time     ON results(event_code, time_seconds);

-- ──────────────────────────────────────────────
--  SCRAPE QUEUE / CHECKPOINT
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_queue (
    url         TEXT    PRIMARY KEY,
    entity_type TEXT    NOT NULL,            -- 'school_list', 'team', 'athlete'
    status      TEXT    CHECK(status IN ('pending','fetched','parsed','error'))
                        DEFAULT 'pending',
    attempts    INTEGER DEFAULT 0,
    last_error  TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON scrape_queue(status, entity_type);

-- ──────────────────────────────────────────────
--  ANALYTICS CACHE
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS progression_stats (
    stat_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_code      TEXT    NOT NULL,
    from_class      TEXT    NOT NULL,
    to_class        TEXT    NOT NULL,
    gender          TEXT    NOT NULL,
    n               INTEGER,
    mean_improvement_pct  REAL,
    median_improvement_pct REAL,
    std_improvement_pct   REAL,
    p10             REAL,
    p25             REAL,
    p75             REAL,
    p90             REAL,
    computed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_code, from_class, to_class, gender)
);

CREATE TABLE IF NOT EXISTS rating_transitions (
    trans_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_code  TEXT    NOT NULL,
    gender      TEXT    NOT NULL,
    from_class  TEXT    NOT NULL,
    from_decile INTEGER NOT NULL,   -- 1-10
    to_decile   INTEGER NOT NULL,
    probability REAL    NOT NULL,
    n           INTEGER,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_code, gender, from_class, from_decile, to_decile)
);
"""


@contextlib.contextmanager
def get_connection(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a WAL-mode SQLite connection."""
    conn = sqlite3.connect(db_path, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables if they do not yet exist."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info("Database initialised at %s", db_path)


def upsert_school(conn: sqlite3.Connection, name: str, division: str = "Unknown",
                  tfrrs_id: str = None, state: str = None, conference: str = None) -> int:
    cur = conn.execute(
        """INSERT INTO schools(school_name, division, tfrrs_id, state, conference)
           VALUES(?,?,?,?,?)
           ON CONFLICT(school_name) DO UPDATE SET
               division   = excluded.division,
               tfrrs_id   = COALESCE(excluded.tfrrs_id,   schools.tfrrs_id),
               state      = COALESCE(excluded.state,      schools.state),
               conference = COALESCE(excluded.conference, schools.conference)
           RETURNING school_id""",
        (name, division, tfrrs_id, state, conference),
    )
    row = cur.fetchone()
    return row[0]


def upsert_athlete(conn: sqlite3.Connection, athlete_id: str, name: str,
                   gender: str = "Unknown", school_id: int = None,
                   profile_url: str = None) -> None:
    conn.execute(
        """INSERT INTO athletes(athlete_id, athlete_name, gender, school_id, profile_url)
           VALUES(?,?,?,?,?)
           ON CONFLICT(athlete_id) DO UPDATE SET
               athlete_name = excluded.athlete_name,
               gender       = CASE WHEN excluded.gender != 'Unknown'
                                   THEN excluded.gender
                                   ELSE athletes.gender END,
               school_id    = COALESCE(excluded.school_id, athletes.school_id),
               profile_url  = COALESCE(excluded.profile_url, athletes.profile_url),
               updated_at   = CURRENT_TIMESTAMP""",
        (athlete_id, name, gender, school_id, profile_url),
    )


def upsert_season(conn: sqlite3.Connection, athlete_id: str, season_year: int,
                  school_id: int = None, class_year: str = "Unknown",
                  is_redshirt: int = 0) -> None:
    conn.execute(
        """INSERT INTO seasons(athlete_id, season_year, school_id, class_year, is_redshirt)
           VALUES(?,?,?,?,?)
           ON CONFLICT(athlete_id, season_year) DO UPDATE SET
               school_id  = COALESCE(excluded.school_id, seasons.school_id),
               class_year = CASE WHEN excluded.class_year != 'Unknown'
                                 THEN excluded.class_year
                                 ELSE seasons.class_year END,
               is_redshirt= excluded.is_redshirt""",
        (athlete_id, season_year, school_id, class_year, is_redshirt),
    )


def bulk_insert_results(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert a batch of result dicts; silently ignore exact duplicates."""
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR IGNORE INTO results
           (athlete_id, season_year, result_date, meet_name, event_code,
            event_type, distance_meters, time_seconds, place, wind_mps, source_url)
           VALUES(:athlete_id, :season_year, :result_date, :meet_name, :event_code,
                  :event_type, :distance_meters, :time_seconds, :place,
                  :wind_mps, :source_url)""",
        rows,
    )
    return conn.execute("SELECT changes()").fetchone()[0]


def queue_urls(conn: sqlite3.Connection, urls: list[str], entity_type: str) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO scrape_queue(url, entity_type) VALUES(?,?)""",
        [(u, entity_type) for u in urls],
    )


def next_pending_urls(conn: sqlite3.Connection, entity_type: str,
                      limit: int = 200) -> list[str]:
    rows = conn.execute(
        """SELECT url FROM scrape_queue
           WHERE entity_type=? AND status='pending' AND attempts<5
           ORDER BY created_at
           LIMIT ?""",
        (entity_type, limit),
    ).fetchall()
    return [r["url"] for r in rows]


def mark_url(conn: sqlite3.Connection, url: str, status: str,
             error: str = None) -> None:
    conn.execute(
        """UPDATE scrape_queue
           SET status=?, attempts=attempts+1, last_error=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE url=?""",
        (status, error, url),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"✓ Database ready at {DB_PATH}")
