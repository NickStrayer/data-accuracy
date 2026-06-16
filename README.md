# TFRRS NCAA Distance Running — Development Pipeline

Scrapes TFRRS (Track & Field Results Reporting System), stores performances in SQLite, computes longitudinal development statistics, and serves an interactive HTML dashboard with a Monte Carlo field predictor.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  INGEST (main.py)                                                       │
│  scrape_teams → scrape_athletes → parse_athletes → tfrrs.db              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  ANALYSIS (analyze_progression.py)                                      │
│  DB marks → JSON stats (progression, transitions, percentiles, …)       │
│  Uses event_bounds.py for plausible times + decile improvement trim     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
┌──────────────────────────────┐    ┌──────────────────────────────────┐
│  MONTE CARLO                 │    │  DASHBOARD                       │
│  predict_monticarlo.py       │    │  visulaize.py                    │
│  → montecarlo_data.json      │    │  → output/dashboard.html         │
│  CSV conference/region maps  │    │  reads DB + all JSON outputs     │
└──────────────────────────────┘    └──────────────────────────────────┘
```

**Class years** are inferred by ranking each athlete's active seasons chronologically (1st = FR, 2nd = SO, …), so gap years do not skip labels.

**Decile ratings** use fixed 0–100 boundaries (rating 85 → decile 9). Transition sampling uses per starting-decile P0.5–P99.5 improvement trimming.

**Conference / XC region** for the Predictor tab come from `ncaa_d1_xc_teams.csv` and `ncaa_d1_xc_teams_by_region.csv`. The `Team` column must match `schools.school_name` in the database exactly.

---

## Project structure

```
data-accuracy/
├── main.py                  # CLI: scrape → parse → analyze
├── database.py              # SQLite schema and helpers
├── scrape_teams.py          # School discovery + roster scraping
├── scrape_athletes.py       # Concurrent athlete HTML downloader
├── parse_athletes.py        # HTML → structured DB rows
├── analyze_progression.py   # Analytics engine → output/*.json
├── event_bounds.py          # Plausible time floors/caps + improvement trim
├── predict_monticarlo.py    # Monte Carlo roster + transition distributions
├── visulaize.py             # Self-contained HTML dashboard generator
├── ncaa_d1_xc_teams.csv     # Team → conference (Predictor scope)
├── ncaa_d1_xc_teams_by_region.csv  # Team → XC region
├── queries.sql              # Ad-hoc analytical SQL (not run automatically)
├── requirements.txt
├── logs/                    # Per-module log files
├── output/                  # Generated JSON + dashboard.html
│   ├── progression_full.json
│   ├── rating_transitions.json
│   ├── breakout_rates.json
│   ├── montecarlo_data.json
│   ├── dashboard.html
│   └── …
└── tfrrs.db                 # SQLite database
```

Cached scrape HTML typically lives under `raw_html/` (rosters, athletes) if configured in the scrape modules.

---

## Setup

**Requirements:** Python 3.12+

```bash
pip install -r requirements.txt
```

---

## Main pipeline (ingest + analysis)

Orchestrated by `main.py`:

```bash
# 1. Initialise database
python main.py init

# 2. Discover NCAA schools
python main.py discover

# 3. Scrape team rosters → queue athlete URLs
python main.py scrape-rosters

# 4. Download athlete HTML (2 workers is safe for TFRRS)
python main.py download --workers 2 --batch 1000

# 5. Parse HTML → SQLite
python main.py parse

# 6. Compute development statistics → output/*.json
python main.py analyze

# Or steps 1–6 in one go:
python main.py all
```

### Add a historical season

```bash
python main.py add-historical --year 2019
# then parse + analyze when downloads finish
python main.py parse
python main.py analyze
```

Supported years and TFRRS `config_hnd` codes are defined in `main.py` (`HISTORICAL_SEASONS`).

---

## Extended pipeline (dashboard + predictor)

After `analyze`, build Monte Carlo data and the dashboard:

```bash
python predict_monticarlo.py
python visulaize.py
```

`predict_monticarlo.py` reads `tfrrs.db`, `rating_transitions.json`, `percentile_tables.json`, and the two NCAA XC CSV files. Default current season is **2026** (`CURRENT_SEASON` in `predict_monticarlo.py`).

Open `output/dashboard.html` in a browser (no server required).

### Optional flags

```bash
python predict_monticarlo.py --season 2026
python predict_monticarlo.py --conf-map ncaa_d1_xc_teams.csv --region-map ncaa_d1_xc_teams_by_region.csv
```

---

## Key modules

| Module | Role |
|--------|------|
| `event_bounds.py` | Event time floors/caps ; SQL filter for analysis/MC; P0.5/P99.5 per-decile improvement trim |
| `analyze_progression.py` | Progression, breakout, attrition, percentile tables, rating transition matrices |
| `predict_monticarlo.py` | Current-season FR/SO/JR roster, decile annotation, transition distributions for JS simulator |
| `visulaize.py` | Single-file dashboard: Overview, Progression, Breakout, Tier Transitions, Percentiles, Evaluator, Predictor |

---

## Output files

| File | Used by |
|------|---------|
| `progression_full.json` | Dashboard Progression tab |
| `breakout_rates.json` | Dashboard Breakout tab |
| `attrition_rates.json` | Dashboard (attrition stats) |
| `rating_transitions.json` | Tier Transitions, Evaluator, Monte Carlo |
| `percentile_tables.json` | Percentiles tab, Evaluator, decile assignment |
| `yearly_trends.json` | Year-over-year field movement charts |
| `development_curves.json` | Aggregate class-year curves |
| `analysis_summary.json` | Run metadata |
| `montecarlo_data.json` | Predictor tab (built by `predict_monticarlo.py`) |
| `dashboard.html` | Browser UI (built by `visulaize.py`) |

---

## Rating system

Each athlete-season gets a national percentile rating (0–100) within event × gender × season:

```
rating = 100 × (1 − (rank − 1) / N)
```

Decile = `floor(rating / 10) + 1`, clamped to 1–10 (fixed boundaries, not cohort-relative bins).

---

## Conference / region CSVs

- **`ncaa_d1_xc_teams.csv`** — columns: `Conference`, `Team`
- **`ncaa_d1_xc_teams_by_region.csv`** — columns: `Region`, `Team`

`Team` must match `schools.school_name` in `tfrrs.db` exactly. Schools not listed are excluded from conference/region filtering in the Predictor (D2/D3/NAIA rosters in the DB are expected to be unmapped).

To add or fix a school: add one row per file with the exact DB name. Re-run `predict_monticarlo.py` and `visulaize.py`.

---

## Database schema (core tables)

```sql
schools        (school_id, school_name, division, tfrrs_id, state, conference)
athletes       (athlete_id, athlete_name, gender, school_id, profile_url)
seasons        (athlete_id, season_year, school_id, class_year, is_redshirt)
results        (athlete_id, season_year, result_date, meet_name,
                event_code, event_type, distance_meters, time_seconds, place, wind_mps)
scrape_queue   (url, entity_type, status, attempts, last_error)
progression_stats  (cached progression rows from analyze)
```

---

## Scraping notes

- Respect rate limits; **2 workers** is confirmed safe for TFRRS downloads.
- HTML is cached locally; `scrape_queue` supports resume after interruption.
- Do not raise `--workers` above 10 without understanding block risk.

---

## Extending

**New event:** add distance mapping in `parse_athletes.py` and entry in `FOCUS_EVENTS` / `FOCUS_CODES` in `analyze_progression.py`. Add time bounds in `event_bounds.py` if needed.

**New conference team:** edit both CSV files; re-run `predict_monticarlo.py`.

---

## License

MIT
