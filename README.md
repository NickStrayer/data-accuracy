# TFRRS NCAA Distance Running Development Pipeline

A production-grade Python pipeline that scrapes TFRRS (Track & Field Results Reporting System) to build a longitudinal database of NCAA cross-country and distance running performances, then computes simulator-ready athlete development statistics.

---

## Features

| Capability | Details |
|---|---|
| **Scraping** | Discovers all NCAA schools → rosters → athlete profiles with retry, caching, and resume |
| **Concurrency** | ThreadPoolExecutor with per-thread rate limiting and exponential backoff |
| **Checkpointing** | `scrape_queue` table tracks every URL status; interrupted jobs resume automatically |
| **Parsing** | Robust multi-layout HTML parser handles TFRRS format changes; all times → seconds, all distances → metres |
| **Database** | Normalised SQLite with WAL mode; supports 10+ years, 100k+ athletes, millions of results |
| **Analytics** | Year-over-year progression, breakout rates, attrition/transfer/redshirt rates, national percentile ratings |
| **Simulator output** | JSON files consumed directly by a college sports simulator engine |
| **Tests** | 52 unit and integration tests covering all modules |

---

## Project Structure

```
tfrrs_pipeline/
├── database.py            # Schema, connection manager, batch helpers
├── scrape_teams.py        # School discovery + roster scraping
├── scrape_athletes.py     # Concurrent athlete HTML downloader
├── parse_athletes.py      # HTML → structured data parser
├── analyze_progression.py # Analytics engine + JSON output
├── main.py                # CLI orchestrator
├── queries.sql            # Useful analytical SQL queries
├── requirements.txt
├── models/                # (Reserved for ML model artefacts)
├── raw_html/
│   ├── listings/          # Cached school listing pages
│   ├── rosters/           # Cached team roster pages
│   └── athletes/          # Cached athlete profile pages
├── logs/                  # Per-module log files
├── output/                # Generated JSON outputs
│   ├── development_curves.json
│   ├── attrition_rates.json
│   ├── breakout_rates.json
│   ├── rating_transitions.json
│   ├── percentile_tables.json
│   └── progression_full.json
├── tests/
│   └── test_pipeline.py
└── tfrrs.db               # SQLite database
```

---

## Setup

### Requirements
- Python 3.12+
- Internet access for scraping (or use `demo` mode offline)

### Install

```bash
git clone https://github.com/your-org/tfrrs-pipeline
cd tfrrs-pipeline
pip install -r requirements.txt
```

---

## Usage

### Quick Start — Demo Mode (no scraping required)

Generates 3,000 synthetic but statistically realistic NCAA athletes and runs the full analysis pipeline in ~3 seconds:

```bash
python main.py demo --athletes 3000
```

### Full Pipeline

```bash
# 1. Initialise database
python main.py init

# 2. Discover all NCAA schools D1 Mens
python main.py discover

# 3. Scrape team rosters → queue athlete URLs
python main.py scrape-rosters

# 4. Download athlete HTML pages (8 workers, batches of 1000)- 2 is confirmed safe for TFFRS
python main.py download --workers 2 --batch 1000

# 5. Parse HTML → SQLite
python main.py parse

# 6. Compute development statistics → JSON
python main.py analyze

# Or run all steps sequentially:
python main.py all
```

### Run Tests

```bash
python -m pytest tests/ -v
# With coverage:
python -m pytest tests/ -v --cov=. --cov-report=term-missing
```

---

## Database Schema

```sql
schools   (school_id, school_name, division, tfrrs_id, state, conference)
athletes  (athlete_id, athlete_name, gender, school_id, profile_url)
seasons   (season_id, athlete_id, season_year, school_id, class_year, is_redshirt)
results   (result_id, athlete_id, season_year, result_date, meet_name,
           event_code, event_type, distance_meters, time_seconds, place, wind_mps)
scrape_queue       (url, entity_type, status, attempts, last_error)
progression_stats  (analytics cache — see analyze_progression.py)
rating_transitions (10×10 decile transition matrices per event/gender/transition)
```

---

## Adding Historical Season Data (e.g. 2021 XC Rosters)
To supplement current roster data with a historical season, first ensure your current scrape is complete and all teams are marked fetched in the queue. Then open sqlite3 and run the following to queue every team's historical roster page: INSERT OR IGNORE INTO scrape_queue (url, entity_type, status) SELECT url || '?config_hnd=222', 'team', 'pending' FROM scrape_queue WHERE entity_type='team' AND status='fetched'; — replacing 222 with the appropriate TFRRS season ID for your target year (222 = 2021 XC, 348 = current season). Then run python main.py scrape-rosters followed by python main.py download --workers 1 --batch 200. Overlapping athletes who appear on both current and historical rosters are automatically skipped at every level — the HTML cache, the scrape queue, and the database all use ignore-on-conflict logic — so there is no risk of duplicate data or redundant downloads. When downloads finish, run python main.py parse and python main.py analyze as normal to incorporate the new athletes into your development statistics.

main discover

INSERT OR IGNORE INTO scrape_queue (url, entity_type, status)
SELECT 
    url || '?config_hnd=138',
    'team',
    'pending'
FROM scrape_queue
WHERE entity_type='team' AND status='fetched';

*nothing will output

main scrape-rosters

python main.py download --workers 2 --batch 200.

## Output Files

### `development_curves.json`

Mean/median/std % improvement per class-year transition, event, and gender.  
Used by the simulator to draw realistic improvement curves for each athlete.

```json
{
  "8K_XC": {
    "M": {
      "FR": { "mean": 2.63, "std": 3.08, "median": 2.70, "n": 887 },
      "SO": { "mean": 2.41, "std": 4.68, "median": 2.53, "n": 662 },
      "JR": { "mean": 2.56, "std": 6.37, "median": 2.80, "n": 438 },
      "SR": { "mean": 2.58, "std": 6.74, "median": 2.81, "n": 208 }
    }
  }
}
```

### `attrition_rates.json`

Return / transfer / redshirt / attrition rates per class-year transition.

```json
{
  "FR_to_SO": {
    "n": 1842,
    "return_rate":   0.832,
    "transfer_rate": 0.071,
    "redshirt_rate": 0.048,
    "attrition_rate": 0.168
  }
}
```

### `breakout_rates.json`

Empirical probability of improving by 5/10/15/20% per transition.

```json
{
  "8K_XC": {
    "M": {
      "FR_to_SO": {
        "n": 887,
        "p_improve_5pct":  0.221,
        "p_improve_10pct": 0.009,
        "p_improve_15pct": 0.0
      }
    }
  }
}
```

### `rating_transitions.json`

10×10 decile Markov transition matrix.  
`matrix["3"]["7"]` = P(athlete in decile 3 moves to decile 7 next year).

### `percentile_tables.json`

Time (seconds) at each national percentile, for use in rating assignment.

```json
{
  "8K_XC": {
    "M": {
      "p5":  1728.0,
      "p25": 1620.0,
      "p50": 1524.0,
      "p75": 1428.0,
      "p95": 1356.0
    }
  }
}
```

---

## Rating System

Each athlete-season receives a national percentile rating (0–100):

```
rating = 100 × (1 − (rank − 1) / N)
```

Where `rank` is the athlete's rank by best time (1 = fastest) among all
NCAA athletes in that event/gender/season. A rating of 95 means the athlete
is faster than 95% of the national field.

---

## Scraping Notes

TFRRS does not provide an official API. This pipeline:

- Respects rate limits with configurable per-worker delays
- Identifies itself with a descriptive User-Agent string
- Caches all HTML locally to avoid redundant requests
- Resumes from checkpoints if interrupted
- Backs off exponentially on HTTP 429 and 5xx errors

Do not set `--workers` above 10 or remove rate-limiting delays, as this may
result in IP blocks.

---

## Extending the Pipeline

### Add a New Event

Add its distance to `_EVENT_DIST_M` in `parse_athletes.py` and to
`FOCUS_EVENTS` in `analyze_progression.py`.

### Add Field Events (jumps, throws)

Replace `time_seconds` logic with `mark_value` and implement a separate
distance/height column. The schema already supports `distance_meters` as a
separate field.

### ML-Based Rating Model

Load `percentile_tables.json` as feature thresholds, train a gradient-boost
model on `progression_stats` data, and save artefacts to `models/`.

---

## Performance

| Dataset | Rows | Time |
|---|---|---|
| 3,000 demo athletes | ~31,000 results | ~3 s |
| 50,000 real athletes | ~500,000 results | ~15 s analysis |
| 300,000 real athletes | ~3M results | ~90 s analysis |

Bulk SQLite inserts with `executemany` + WAL mode handle millions of rows
efficiently. Pandas vectorised operations are used throughout the analytics
engine.

---

## License

MIT
