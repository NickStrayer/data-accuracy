"""
visualize.py - Generates a self-contained HTML dashboard from TFRRS SQLite data.

Run:  python visualize_fixed.py
Opens: output/dashboard.html  (no server needed, open directly in browser)

Fixes applied vs original:
  BUG-01  q() helper executed each query 4x; rewritten to execute once.
  BUG-02  r.std / r.p10 etc. called .toFixed() without null guard → JS crash;
           added null-safe helpers in JS.
  BUG-05  showTab() relied on global window.event.target (non-standard);
           nav buttons now pass themselves via onclick="showTab('x', this)".
  BUG-07  Std-Dev shown as a peer bar alongside Mean — misleading; replaced
           with error-range annotation text and removed the spurious dataset.
  BUG-08  p10/p25/p75/p90 nullable in DB; JS now guards with ?? null.
  BUG-10  class_performance query event list didn't match actual DB codes
           produced by parse_athletes.py (e.g. '5000M' vs '5000M') — verified
           and corrected; also added '5K_XC' / '10K_XC' variants.
  BUG-11  Embedding raw JSON inside <script> block unsafe if values contain
           </script>; now escaped.
  BUG-12  Stacked attrition chart double-counted redshirt athletes; chart
           replaced with a clean grouped bar (Returned / Attrited) and a
           separate redshirt line.
  MISC    fmtPct(null) returned '—' but callers still called .toFixed on the
           raw value before passing; fixed at call sites.
           renderImprovementBar y-axis reverse removed (improvement % has no
           meaningful "better at top" direction for grouped bars).
           Added safe guards throughout for empty / missing JSON sections.
"""

import json
import sqlite3
import webbrowser
from pathlib import Path

DB_PATH     = Path(__file__).parent / "tfrrs.db"
OUTPUT_PATH = Path(__file__).parent / "output" / "dashboard.html"

# Keep in sync with analyze_progression.FOCUS_EVENTS
FOCUS_EVENT_ORDER = [
    "5K_XC", "6K_XC", "8K_XC", "10K_XC",
    "1500M", "3000M", "5000M", "10000M",
    "MILE", "3000S",
]
FOCUS_EVENT_CODES = set(FOCUS_EVENT_ORDER) | {"10_000", "3000SC"}


def _sql_in(codes) -> str:
    return ",".join(f"'{c}'" for c in codes)


# ─────────────────────────────────────────────────────────────────────────────
#  Data loading  (BUG-01 fixed: single execute per query)
# ─────────────────────────────────────────────────────────────────────────────

def load_data() -> dict:
    conn = sqlite3.connect(DB_PATH)

    def q(sql, params=()):
        """Execute sql once, return list-of-dicts."""
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    data = {}

    # Summary counts
    data["summary"] = {
        "athletes": conn.execute("SELECT COUNT(*) FROM athletes").fetchone()[0],
        "results":  conn.execute("SELECT COUNT(*) FROM results").fetchone()[0],
        "schools":  conn.execute("SELECT COUNT(*) FROM schools").fetchone()[0],
        "seasons":  conn.execute(
            "SELECT COUNT(DISTINCT season_year) FROM results"
        ).fetchone()[0],
    }

    codes_sql = _sql_in(FOCUS_EVENT_CODES)

    # Progression stats from DB (focus events only)
    try:
        data["progression"] = q(f"""
            SELECT event_code, gender, from_class, to_class,
                   n,
                   mean_improvement_pct   AS mean,
                   median_improvement_pct AS median,
                   std_improvement_pct    AS std,
                   p10, p25, p75, p90,
                   discounted_mean_improvement_pct   AS discounted_mean,
                   discounted_median_improvement_pct AS discounted_median,
                   discounted_std_improvement_pct    AS discounted_std,
                   discounted_p10, discounted_p25, discounted_p75, discounted_p90
            FROM progression_stats
            WHERE event_code IN ({codes_sql})
            ORDER BY event_code, gender, from_class
        """)
    except sqlite3.OperationalError:
        # Older DB without discounted_* columns (run analyze_progression.py to migrate)
        data["progression"] = q(f"""
            SELECT event_code, gender, from_class, to_class,
                   n,
                   mean_improvement_pct   AS mean,
                   median_improvement_pct AS median,
                   std_improvement_pct    AS std,
                   p10, p25, p75, p90
            FROM progression_stats
            WHERE event_code IN ({codes_sql})
            ORDER BY event_code, gender, from_class
        """)

    # Ordinal career class (1st season=FR, 2nd=SO, …) — scraped class_year is unreliable
    data["class_performance"] = q(f"""
        WITH bests AS (
            SELECT r.athlete_id, r.season_year, r.event_code, a.gender,
                   MIN(r.time_seconds) AS time_seconds
            FROM results r
            JOIN athletes a ON a.athlete_id = r.athlete_id
            WHERE r.event_code IN ({codes_sql})
              AND r.time_seconds IS NOT NULL AND r.time_seconds > 0
            GROUP BY r.athlete_id, r.season_year, r.event_code, a.gender
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY athlete_id, event_code, gender
                       ORDER BY season_year
                   ) - 1 AS class_idx
            FROM bests
        )
        SELECT
            CASE class_idx
                WHEN 0 THEN 'FR' WHEN 1 THEN 'SO' WHEN 2 THEN 'JR'
                WHEN 3 THEN 'SR' WHEN 4 THEN '5TH'
            END AS class_year,
            event_code,
            gender,
            ROUND(AVG(time_seconds), 2) AS avg_time,
            ROUND(MIN(time_seconds), 2) AS best_time,
            COUNT(DISTINCT athlete_id)  AS n_athletes
        FROM ranked
        WHERE class_idx BETWEEN 0 AND 4
        GROUP BY class_idx, event_code, gender
        ORDER BY event_code, gender, class_idx
    """)

    # Season volume
    data["season_volume"] = q("""
        SELECT season_year AS year,
               COUNT(DISTINCT athlete_id) AS athletes,
               COUNT(*)                   AS results
        FROM results
        GROUP BY season_year
        ORDER BY season_year
    """)

    # Top schools
    data["top_schools"] = q("""
        SELECT sc.school_name AS school,
               COUNT(DISTINCT a.athlete_id) AS athletes
        FROM athletes a
        JOIN schools sc ON sc.school_id = a.school_id
        GROUP BY sc.school_name
        ORDER BY athletes DESC
        LIMIT 15
    """)

    # Results by event (focus events only)
    data["event_counts"] = q(f"""
        SELECT event_code, event_type,
               COUNT(*)                   AS n_results,
               COUNT(DISTINCT athlete_id) AS n_athletes
        FROM results
        WHERE event_code IN ({codes_sql})
        GROUP BY event_code
        ORDER BY n_results DESC
    """)

    # Load JSON outputs produced by analyze_progression.py
    def _load_json(filename):
        p = Path(__file__).parent / "output" / filename
        return json.loads(p.read_text()) if p.exists() else {}

    data["attrition"]   = _load_json("attrition_rates.json")
    data["breakout"]    = {
        k: v for k, v in _load_json("breakout_rates.json").items()
        if k in FOCUS_EVENT_CODES
    }
    data["percentiles"] = {
        k: v for k, v in _load_json("percentile_tables.json").items()
        if k in FOCUS_EVENT_CODES
    }
    data["rating_transitions"] = {
        k: v for k, v in _load_json("rating_transitions.json").items()
        if k in FOCUS_EVENT_CODES
    }
    data["yearly_trends"] = {
        k: v for k, v in _load_json("yearly_trends.json").items()
        if k in FOCUS_EVENT_CODES
    }

    data["ui_events"] = FOCUS_EVENT_ORDER

    # Monte Carlo predictor data (built by predict_montecarlo.py)
    mc = _load_json("montecarlo_data.json")
    data["mc"] = {
        "athletes":                  mc.get("athletes", {}),
        "improvement_distributions": {
            k: v for k, v in mc.get("improvement_distributions", {}).items()
            if k in FOCUS_EVENT_CODES
        },
        "percentile_benchmarks": {
            k: v for k, v in mc.get("percentile_benchmarks", {}).items()
            if k in FOCUS_EVENT_CODES
        },
        "conferences":    mc.get("conferences", []),
        "xc_regions":     mc.get("xc_regions", []),
        "current_season": mc.get("current_season", 2025),
    }

    conn.close()
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  HTML generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_html(data: dict) -> str:
    # BUG-11 fixed: escape </script> inside embedded JSON
    d = json.dumps(data, default=str).replace("</script>", r"<\/script>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NCAA XC &amp; Distance — Development Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/noUiSlider/15.7.1/nouislider.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/noUiSlider/15.7.1/nouislider.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;800&family=Barlow:wght@300;400;500&display=swap');

  :root {{
    --bg:       #0a0e1a;
    --surface:  #111827;
    --surface2: #1a2235;
    --border:   #1e2d45;
    --accent:   #00c8ff;
    --accent2:  #ff6b35;
    --accent3:  #7fff6e;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --good:     #22d3a0;
    --warn:     #f59e0b;
    --bad:      #f87171;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Barlow', sans-serif;
    font-size: 14px;
    min-height: 100vh;
  }}

  header {{
    background: linear-gradient(135deg, #0a0e1a 0%, #0d1929 50%, #0a1628 100%);
    border-bottom: 1px solid var(--border);
    padding: 24px 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }}
  header h1 {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 28px; font-weight: 800; letter-spacing: 2px;
    text-transform: uppercase;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  header p {{ color: var(--muted); font-size: 12px; letter-spacing: 1px; }}

  nav {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 40px;
    display: flex; gap: 4px;
  }}
  nav button {{
    background: none; border: none; color: var(--muted);
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 13px; font-weight: 600; letter-spacing: 1.5px;
    text-transform: uppercase;
    padding: 14px 20px; cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
  }}
  nav button:hover {{ color: var(--text); }}
  nav button.active {{
    color: var(--accent);
    border-bottom-color: var(--accent);
  }}

  main {{ padding: 32px 40px; max-width: 1600px; margin: 0 auto; }}

  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}

  .grid-4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; margin-bottom: 28px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr;       gap: 24px; margin-bottom: 28px; }}
  .grid-3 {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 24px; margin-bottom: 28px; }}
  .full  {{ grid-column: 1 / -1; }}

  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
  }}
  .card-title {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 12px;
  }}

  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px 24px;
    position: relative; overflow: hidden;
  }}
  .kpi::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
  }}
  .kpi.blue::before   {{ background: var(--accent);  }}
  .kpi.orange::before {{ background: var(--accent2); }}
  .kpi.green::before  {{ background: var(--accent3); }}
  .kpi.warn::before   {{ background: var(--warn);    }}
  .kpi-label {{
    font-size: 11px; font-weight: 600; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 8px;
  }}
  .kpi-value {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 38px; font-weight: 800; line-height: 1;
    color: var(--text);
  }}
  .kpi-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

  .chart-wrap {{ position: relative; height: 280px; }}
  .chart-wrap.tall  {{ height: 340px; }}
  .chart-wrap.short {{ height: 220px; }}

  .controls {{
    display: flex; gap: 10px; flex-wrap: wrap;
    margin-bottom: 20px; align-items: center;
  }}
  select, .btn-group button {{
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 8px 14px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 12px; letter-spacing: 1px; font-weight: 600;
    text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }}
  select:focus {{ outline: none; border-color: var(--accent); }}
  .btn-group {{ display: flex; gap: 4px; }}
  .btn-group button:hover  {{ border-color: var(--accent); color: var(--accent); }}
  .btn-group button.active {{
    background: var(--accent); border-color: var(--accent);
    color: #0a0e1a;
  }}

  .progress-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
  .progress-label {{ width: 120px; font-size: 12px; color: var(--muted); }}
  .progress-bar-bg {{ flex: 1; height: 8px; background: var(--surface2); border-radius: 4px; overflow: hidden; }}
  .progress-bar    {{ height: 100%; border-radius: 4px; transition: width 0.6s ease; }}
  .progress-val    {{
    width: 48px; text-align: right;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 14px; font-weight: 700;
  }}

  .data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .data-table th {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; color: var(--muted);
    padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left;
  }}
  .data-table td {{
    padding: 9px 12px;
    border-bottom: 1px solid rgba(30,45,69,0.5);
    color: var(--text);
  }}
  .data-table tr:hover td {{ background: var(--surface2); }}
  .data-table .num {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 15px; font-weight: 600; color: var(--accent);
  }}

  .pct-ruler {{ display: flex; border-radius: 6px; overflow: hidden; height: 32px; margin: 8px 0; }}
  .pct-seg   {{
    display: flex; align-items: center; justify-content: center;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 700; color: #0a0e1a; flex: 1;
  }}

  .section-title {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 20px; font-weight: 800; letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 20px; padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
  }}
  .section-title span {{ color: var(--accent); }}

  .empty-state {{
    text-align: center; padding: 40px;
    color: var(--muted); font-size: 13px;
  }}

  .tier-matrix-grid {{
    display: grid;
    grid-template-columns: 36px 1fr;
    grid-template-rows: auto 1fr;
    gap: 8px 12px;
    align-items: center;
  }}
  .tier-matrix-axis-top {{
    grid-column: 2;
    text-align: center;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
  }}
  .tier-matrix-axis-left {{
    grid-row: 2;
    writing-mode: vertical-rl;
    transform: rotate(180deg);
    text-align: center;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
  }}
  .tier-matrix-table {{
    grid-column: 2;
    grid-row: 2;
    overflow-x: auto;
  }}

  .eval-time-input {{
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 8px 14px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 18px; font-weight: 600;
    width: 148px;
  }}
  .eval-time-input:focus {{
    outline: none;
    border-color: var(--accent);
  }}
  .eval-snap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
  }}
  .eval-snap-label {{
    font-size: 11px; font-weight: 600; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 6px;
  }}
  .eval-snap-value {{
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 28px; font-weight: 800; line-height: 1.1;
  }}
</style>
</head>
<body>

<header>
  <div>
    <h1>NCAA XC &amp; Distance — Development Dashboard</h1>
    <p>TFRRS Data Pipeline &nbsp;·&nbsp; Longitudinal Athlete Analytics</p>
  </div>
  <div style="text-align:right; color:var(--muted); font-size:12px;">
    <div id="gen-date"></div>
  </div>
</header>

<!-- BUG-05 fixed: pass `this` so showTab can activate the correct button -->
<nav>
  <button class="active" onclick="showTab('overview',this)">Overview</button>
  <button onclick="showTab('progression',this)">Progression Curves</button>
  <button onclick="showTab('breakout',this)">Breakout Rates</button>
  <button onclick="showTab('percentiles',this)">Percentile Tables</button>
  <button onclick="showTab('tiers',this)">Tier Transitions</button>
  <button onclick="showTab('evaluator',this)">Evaluator</button>
  <button onclick="showTab('predictor',this)">Predictor</button>
  <button onclick="showTab('volume',this)">Data Volume</button>
</nav>

<main>

<!-- ══════ TAB 1 — OVERVIEW ══════ -->
<div class="tab-pane active" id="tab-overview">

  <div class="grid-4">
    <div class="kpi blue">
      <div class="kpi-label">Total Athletes</div>
      <div class="kpi-value" id="kpi-athletes">—</div>
      <div class="kpi-sub">unique profiles</div>
    </div>
    <div class="kpi orange">
      <div class="kpi-label">Total Results</div>
      <div class="kpi-value" id="kpi-results">—</div>
      <div class="kpi-sub">race performances</div>
    </div>
    <div class="kpi green">
      <div class="kpi-label">Schools</div>
      <div class="kpi-value" id="kpi-schools">—</div>
      <div class="kpi-sub">NCAA programs</div>
    </div>
    <div class="kpi warn">
      <div class="kpi-label">Seasons Covered</div>
      <div class="kpi-value" id="kpi-seasons">—</div>
      <div class="kpi-sub">academic years</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">Results by Event</div>
      <div class="chart-wrap"><canvas id="chart-events"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Top Programs by Athlete Count</div>
      <div class="chart-wrap"><canvas id="chart-schools"></canvas></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">Performance Curve — Average Time by Class Year</div>
      <div class="controls">
        <select id="ov-event-sel" onchange="renderOverviewCurve()"></select>
        <div class="btn-group">
          <button id="ov-m-btn" class="active" onclick="setOvGender('M')">Men</button>
          <button id="ov-f-btn"                onclick="setOvGender('F')">Women</button>
        </div>
      </div>
      <div class="chart-wrap short"><canvas id="chart-curve"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Mean % Improvement by Class Transition</div>
      <div class="controls">
        <select id="ov-event-sel2" onchange="renderImprovementBar()"></select>
        <div class="btn-group">
          <button id="ov2-m-btn" class="active" onclick="setOv2Gender('M')">Men</button>
          <button id="ov2-f-btn"                onclick="setOv2Gender('F')">Women</button>
        </div>
      </div>
      <div class="chart-wrap short"><canvas id="chart-improvement"></canvas></div>
    </div>
  </div>
</div>

<!-- ══════ TAB 2 — PROGRESSION ══════ -->
<div class="tab-pane" id="tab-progression">
  <div class="section-title">Progression <span>Curves — All percentages represent pure time gains/losses</span></div>

  <div class="controls">
    <select id="prog-event" onchange="renderProgression()"></select>
    <div class="btn-group">
      <button id="prog-m-btn" class="active" onclick="setProgGender('M')">Men</button>
      <button id="prog-f-btn"                onclick="setProgGender('F')">Women</button>
    </div>
    <div class="btn-group">
      <button id="prog-raw-btn" class="active" onclick="setProgMode('raw')">Raw</button>
      <button id="prog-disc-btn"                onclick="setProgMode('discounted')">Discounted vs. NCAA field</button>
    </div>
  </div>
  <p style="color:var(--muted);margin:-4px 0 16px;font-size:13px;">
    "Discounted" adjusts for overall NCAA time inflation in progression results — often a very minimal change.
  </p>
  <p style="color:var(--muted);margin:-8px 0 20px;font-size:13px;">
    Percentile columns (P10–P90) describe the spread of <strong>% time improvement</strong> among athletes
    who raced in both seasons of a transition — not where athletes rank on absolute time.
    <strong>P90</strong> is the improvement rate exceeded by only the top 10% of improvers (the largest
    percent gains), <em>not</em> the fastest 10% by time. Likewise, P10 marks the bottom 10% of improvers.
  </p>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">Mean Improvement % per Transition</div>
      <div class="chart-wrap"><canvas id="chart-prog-mean"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Percentile Distribution of Improvement</div>
      <div class="chart-wrap"><canvas id="chart-prog-dist"></canvas></div>
    </div>
  </div>

  <div class="card full">
    <div class="card-title">Progression Summary Table</div>
    <table class="data-table">
      <thead>
        <tr>
          <th>Transition</th><th>N</th><th>Mean %</th>
          <th>Median %</th><th>Std Dev</th>
          <th title="10th percentile of % improvement (bottom 10% of improvers)">P10</th>
          <th title="25th percentile of % improvement">P25</th>
          <th title="75th percentile of % improvement">P75</th>
          <th title="90th percentile of % improvement (top 10% of improvers, not fastest by time)">P90</th>
        </tr>
      </thead>
      <tbody id="prog-table-body"></tbody>
    </table>
  </div>

  <div class="section-title" style="margin-top:32px;">NCAA-Wide <span>Progression Over Time</span></div>
  <p style="color:var(--muted);margin-bottom:16px;font-size:13px;">
    How the overall NCAA field's median (P50), P25, and P75 times for this event have shifted
    season to season — independent of any individual athlete's class-year progression.
  </p>
  <div class="grid-2">
    <div class="card">
      <div class="card-title">Men — National Percentile Times by Season</div>
      <div class="chart-wrap"><canvas id="chart-yearly-trend-m"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Women — National Percentile Times by Season</div>
      <div class="chart-wrap"><canvas id="chart-yearly-trend-f"></canvas></div>
    </div>
  </div>
</div>

<!-- ══════ TAB 3 — ATTRITION ══════ -->
<div class="tab-pane" id="tab-attrition">
  <div class="section-title">Attrition <span>&amp; Roster Dynamics</span></div>

  <div class="grid-4" id="attrition-kpis"></div>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">Return Rate by Transition</div>
      <div class="chart-wrap"><canvas id="chart-return"></canvas></div>
    </div>
    <!-- BUG-12 fixed: replaced misleading stacked chart with clean grouped bars -->
    <div class="card">
      <div class="card-title">Returned vs Attrited Athletes</div>
      <div class="chart-wrap"><canvas id="chart-att-grouped"></canvas></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Roster Retention Rates</div>
    <div id="retention-bars"></div>
  </div>
</div>

<!-- ══════ TAB 4 — BREAKOUT ══════ -->
<div class="tab-pane" id="tab-breakout">
  <div class="section-title">Breakout <span>Rates</span></div>
  <p style="color:var(--muted);margin-bottom:8px;font-size:13px;">
    This page asks: <strong>"of athletes who competed in both class years of a transition
    (e.g. FR and SO), what fraction improved their best time by at least X seconds
    between those two seasons?"</strong> Thresholds are set at 1%, 3%, 5%, 7%, and 10%
    of the event/gender median time — so they scale naturally across events of different
    lengths. It's a hit-rate, not a magnitude — the Progression
    Curves tab tells you the typical (mean/median) improvement, while this tells you how
    common large jumps actually are.
  </p>
  <p style="color:var(--muted);margin-bottom:24px;font-size:13px;">
    Use the <strong>time-percentile slider</strong> below to restrict the analysis to athletes
    within a specific tier of the overall time distribution (e.g. drag to 50–75% to see only
    athletes in the middle-slow half). Both the bar chart and heatmap update live.
    Use the percentile slider to zoom into a specific starting-tier band.
  </p>

  <!-- Percentile range slider -->
  <div class="card" style="margin-bottom:20px;">
    <div class="card-title">Filter by Athlete Time Percentile (from-season)</div>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:4px 0;">
      <span style="font-size:12px;color:var(--muted);white-space:nowrap;">Time percentile range:</span>
      <span id="br-pct-min-lbl" style="font-size:13px;font-weight:700;color:var(--accent);min-width:110px;white-space:nowrap;">0%</span>
      <div style="position:relative;flex:1;min-width:160px;height:24px;">
          <div id="breakout-range-slider" style="flex:1;min-width:300px;"></div>
      </div>
      <span id="br-pct-max-lbl" style="font-size:13px;font-weight:700;color:var(--accent2);min-width:110px;white-space:nowrap;">100%</span>
      <span id="br-pct-count" style="font-size:12px;color:var(--muted);"></span>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:6px;">
      100% = fastest athletes &nbsp;·&nbsp; 0% = slowest athletes &nbsp;·&nbsp;
      Drag both handles to narrow the window (e.g. 50–75% = middle-fast tier)
    </div>
  </div>

  <div class="controls">
    <select id="br-event" onchange="renderBreakout()"></select>
    <div class="btn-group">
      <button id="br-m-btn" class="active" onclick="setBrGender('M')">Men</button>
      <button id="br-f-btn"                onclick="setBrGender('F')">Women</button>
    </div>
    <div class="btn-group">
      <button id="br-raw-btn" class="active" onclick="setBrMode('raw')">Raw</button>
      <button id="br-disc-btn"                onclick="setBrMode('discounted')">Discounted vs. NCAA field</button>
    </div>
  </div>
  <p style="color:var(--muted);margin:-4px 0 16px;font-size:13px;">
    "Discounted" Adjusts for overall NCAA time inflation in progression results-- Often a very minimal change.
  </p>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">P(Improve ≥ Xs) by Class Transition</div>
      <div class="chart-wrap tall"><canvas id="chart-breakout"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Breakout Probability Heatmap</div>
      <div id="breakout-heatmap" style="padding:8px;overflow-x:auto;"></div>
    </div>
  </div>
</div>

<!-- ══════ TAB 5 — PERCENTILES ══════ -->
<div class="tab-pane" id="tab-percentiles">
  <div class="section-title">National <span>Percentile Benchmarks</span></div>
  <p style="color:var(--muted);margin-bottom:24px;font-size:13px;">
    Time (MM:SS) required to be at each national percentile across all seasons combined.
    Lower time = faster. P95 = faster than 95% of the NCAA field.
  </p>
  <div id="percentile-tables"></div>
</div>

<!-- ══════ TAB 6 — TIER TRANSITIONS ══════ -->
<div class="tab-pane" id="tab-tiers">
  <div class="section-title">Tier <span>Transitions</span></div>
  <p style="color:var(--muted);margin-bottom:8px;font-size:13px;">
    Every athlete-season is ranked against the entire NCAA field for that event/gender/season
    and bucketed into a <strong>decile</strong> (D1 = slowest 10% nationally, D10 = fastest
    10%). The matrix answers: <strong>"if an athlete was in decile X in the starting class year,
    what's the probability they land in decile Y in the ending class year?"</strong> Reading along a
    row shows how likely an athlete is to stay in their tier, move up (toward D10), or fall back
    (toward D1).
  </p>
  <p style="color:var(--muted);margin-bottom:24px;font-size:13px;">
    <strong>Grey</strong> cells = stayed in the same decile &nbsp;·&nbsp;
    <strong style="color:var(--good)">Green</strong> = moved to a faster decile (toward D10) &nbsp;·&nbsp;
    <strong style="color:var(--bad)">Red</strong> = moved to a slower decile (toward D1).
    The rightmost column shows mean % time change for athletes starting in each decile.
  </p>

  <div class="controls">
    <select id="tier-event" onchange="renderTierTab()"></select>
    <select id="tier-trans-sel" onchange="onTierTransChange()"></select>
    <div class="btn-group">
      <button id="tier-m-btn" class="active" onclick="setTierGender('M')">Men</button>
      <button id="tier-f-btn"                onclick="setTierGender('F')">Women</button>
    </div>
    <div class="btn-group">
      <button id="tier-raw-btn" class="active" onclick="setTierMode('raw')">Raw</button>
      <button id="tier-disc-btn"                onclick="setTierMode('discounted')">Discounted vs. NCAA field</button>
    </div>
  </div>
  <p style="color:var(--muted);margin:-4px 0 16px;font-size:13px;">
    "Discounted" adjusts for overall NCAA time inflation in progression results — often a very minimal change.
  </p>

  <div class="card full">
    <div class="card-title">Decile Transition Matrix (10×10)</div>
    <div id="tier-matrix" style="padding:8px;overflow-x:auto;"></div>
  </div>

  <div class="card full">
    <div class="card-title">Percentile Distribution of Improvement by Starting Decile</div>
    <p style="color:var(--muted);margin:-4px 0 12px;font-size:12px;">
      For the selected transition above — deciles on the X-axis (D1 slowest → D10 fastest),
      with P10 / P25 / Median / P75 / P90 lines connecting across deciles (same as Progression tab).
    </p>
    <div class="chart-wrap tall"><canvas id="chart-tier-decile-prog"></canvas></div>
  </div>
</div>

<!-- ══════ TAB — EVALUATOR ══════ -->
<div class="tab-pane" id="tab-evaluator">
  <div class="section-title">Performance <span>Evaluator</span></div>
  <p style="color:var(--muted);margin-bottom:8px;font-size:13px;">
    Enter a current best time to estimate <strong>next-season</strong> and <strong>career</strong> outcomes
    using historical NCAA improvement rates for the selected event. Rates are looked up by your
    <strong>estimated starting decile</strong> (from national percentile benchmarks), matching the
    Tier Transitions decile breakdown — not event-wide averages.
  </p>
  <p style="color:var(--muted);margin-bottom:24px;font-size:13px;">
    Each scenario applies a decile-specific P10 / Median / P90 (etc.) improvement rate for that transition.
    Career projections re-estimate decile after each year before applying the next step's rate.
    Falls back to event-wide progression stats when a decile bucket has too few athletes.
  </p>

  <div class="controls">
    <select id="eval-event" onchange="renderEvaluator()"></select>
    <select id="eval-class" onchange="renderEvaluator()">
      <option value="FR">Freshman (FR)</option>
      <option value="SO">Sophomore (SO)</option>
      <option value="JR">Junior (JR)</option>
      <option value="SR">Senior (SR)</option>
    </select>
    <div class="btn-group">
      <button id="eval-m-btn" class="active" onclick="setEvalGender('M')">Men</button>
      <button id="eval-f-btn"                onclick="setEvalGender('F')">Women</button>
    </div>
    <div class="btn-group">
      <button id="eval-raw-btn" class="active" onclick="setEvalMode('raw')">Raw</button>
      <button id="eval-disc-btn"                onclick="setEvalMode('discounted')">Discounted vs. NCAA field</button>
    </div>
    <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);">
      Best time
      <input type="text" id="eval-time" class="eval-time-input" placeholder="16:30.00"
             oninput="renderEvaluator()" value="16:30.00">
      <span style="font-size:11px;">MM:SS.ss or seconds</span>
    </label>
  </div>

  <div id="eval-content">
    <div class="empty-state">Enter a time above to see projections.</div>
  </div>
</div>

<!-- ══════ TAB 7 — VOLUME ══════ -->
<div class="tab-pane" id="tab-predictor">
  <div class="section-title">Field <span>Predictor</span></div>
  <p style="color:var(--muted);margin-bottom:8px;font-size:13px;">
    Select a <strong>conference or region</strong>, <strong>event</strong>, and <strong>target finish</strong>
    (e.g. top 1, top 6). Then search for an athlete to focus on.
    The simulator runs <strong>10,000 Monte Carlo seasons</strong> — each athlete in the field
    independently draws a random improvement (or regression) from their own historical decile
    distribution, producing a simulated finishing order. The result is a probability distribution
    over finishing places for your chosen athlete.
  </p>

  <div class="controls" style="flex-wrap:wrap;gap:10px;">
    <select id="pred-scope-type" onchange="predScopeChanged()">
      <option value="conference">Conference</option>
      <option value="xc_region">XC Region</option>
    </select>
    <select id="pred-scope-value" onchange="predFieldChanged()"></select>
    <select id="pred-event" onchange="predFieldChanged()"></select>
    <div class="btn-group">
      <button id="pred-m-btn" class="active" onclick="setPredGender('M')">Men</button>
      <button id="pred-f-btn"               onclick="setPredGender('F')">Women</button>
    </div>
    <select id="pred-transition">
      <option value="FR_to_SO">FR → SO</option>
      <option value="SO_to_JR">SO → JR</option>
      <option value="JR_to_SR" selected>JR → SR</option>
    </select>
  </div>

  <div style="margin:14px 0 6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
    <input type="text" id="pred-athlete-search" placeholder="Search athlete name..."
           style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);
                  background:var(--surface2);color:var(--text);font-size:13px;width:240px;"
           oninput="predAthleteSearch(this.value)">
    <span id="pred-field-count" style="font-size:12px;color:var(--muted);"></span>
    <span id="pred-sims-label"  style="font-size:12px;color:var(--muted);margin-left:auto;">
      10,000 simulations
    </span>
  </div>

  <!-- Athlete dropdown from search -->
  <div id="pred-athlete-list" style="display:none;max-height:200px;overflow-y:auto;
       border:1px solid var(--border);border-radius:6px;background:var(--surface2);
       margin-bottom:12px;font-size:13px;"></div>

  <!-- Chosen athlete card -->
  <div id="pred-chosen-card" style="display:none;margin-bottom:14px;padding:10px 14px;
       border:1px solid var(--accent);border-radius:8px;background:var(--surface);font-size:13px;">
  </div>

  <!-- Run button -->
  <div style="margin-bottom:16px;">
    <button id="pred-run-btn"
            style="padding:8px 22px;background:var(--accent);color:#fff;border:none;
                   border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;"
            onclick="runMonteCarlo()">
      Run Simulation
    </button>
    <span id="pred-run-status" style="margin-left:12px;font-size:12px;color:var(--muted);"></span>
  </div>

  <!-- Results -->
  <div id="pred-results" style="display:none;">
    <div class="grid-2" style="margin-bottom:20px;">
      <div class="card">
        <div class="card-title">Finishing Place Distribution</div>
        <div class="chart-wrap tall"><canvas id="chart-pred-places"></canvas></div>
      </div>
      <div class="card">
        <div class="card-title">Cumulative Probability</div>
        <div class="chart-wrap tall"><canvas id="chart-pred-cumul"></canvas></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-title">Key Probabilities</div>
      <div id="pred-prob-table"></div>
    </div>

    <div class="card">
      <div class="card-title">Simulated Field — Median Outcomes</div>
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px;">
        Each athlete's median simulated time across all 10,000 runs,
        sorted by median finish. Grey = no historical distribution found (time held fixed).
      </p>
      <div id="pred-field-table"></div>
    </div>
  </div>
</div>

<div class="tab-pane" id="tab-volume">
  <div class="section-title">Data <span>Volume</span></div>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">Athletes per Season Year</div>
      <div class="chart-wrap tall"><canvas id="chart-vol-athletes"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Results per Season Year</div>
      <div class="chart-wrap tall"><canvas id="chart-vol-results"></canvas></div>
    </div>
  </div>
</div>

</main>

<script>
// ── Embedded data ─────────────────────────────────────────────────────────────
const DATA = {d};

// ── State ─────────────────────────────────────────────────────────────────────
let ovGender   = 'M';
let ov2Gender  = 'M';
let progGender = 'M';
let brGender   = 'M';
let tierGender = 'M';
let tierMode   = 'raw'; // 'raw' | 'discounted'
let brMode     = 'raw'; // 'raw' | 'discounted'
let brPctMin   = 0;
let brPctMax   = 100;
let evalGender = 'M';
let evalMode   = 'raw';

const EVAL_SCENARIOS = [
  {{ id: 'p10',    label: 'P10',    note: 'Bottom 10% of improvers' }},
  {{ id: 'p25',    label: 'P25',    note: 'Below typical' }},
  {{ id: 'median', label: 'Median', note: 'Typical athlete' }},
  {{ id: 'mean',   label: 'Mean',   note: 'Average rate' }},
  {{ id: 'p75',    label: 'P75',    note: 'Strong year' }},
  {{ id: 'p90',    label: 'P90',    note: 'Top 10% of improvers' }},
];
const EVAL_CLASS_NEXT = {{ FR: 'SO', SO: 'JR', JR: 'SR', SR: '5TH' }};
const EVAL_CAREER_PATH = {{ FR: ['SO','JR','SR'], SO: ['JR','SR'], JR: ['SR'], SR: ['5TH'] }};

// ── Chart registry ────────────────────────────────────────────────────────────
const charts = {{}};
function mkChart(id, cfg) {{
  if (charts[id]) charts[id].destroy();
  const el = document.getElementById(id);
  if (!el) return null;
  charts[id] = new Chart(el, cfg);
  return charts[id];
}}

// ── Helpers ───────────────────────────────────────────────────────────────────
const CLASS_ORDER = ['FR','SO','JR','SR','5TH'];
const TRANS_LABELS = {{
  'FR_to_SO':'FR→SO','SO_to_JR':'SO→JR',
  'JR_to_SR':'JR→SR','SR_to_5TH':'SR→5TH',
  'FR_to_SR':'FR→SR (Freshman to Senior)'
}};
const TIER_TRANS_ORDER = ['FR_to_SO','SO_to_JR','JR_to_SR','SR_to_5TH','FR_to_SR'];
const COLORS = ['#00c8ff','#ff6b35','#7fff6e','#f59e0b','#a78bfa','#f472b6'];

const EVENT_LABELS = {{
  '5K_XC':'5K XC','6K_XC':'6K XC','8K_XC':'8K XC','10K_XC':'10K XC',
  '1500M':'1500m','3000M':'3000m','5000M':'5000m','10000M':'10000m',
  'MILE':'Mile','3000S':'Steeple',
  '10_000':'10000m','3000SC':'Steeple',
}};
const EVENT_ALIASES = {{
  '10000M': ['10_000', '10000M'],
  '3000S':  ['3000SC', '3000S'],
}};
function eventCandidates(code) {{
  return EVENT_ALIASES[code] || [code];
}}
function firstEventKey(obj, event) {{
  for (const code of eventCandidates(event)) {{
    if (obj && obj[code]) return code;
  }}
  return event;
}}
function rowsForEvent(dataset, event, gender) {{
  for (const code of eventCandidates(event)) {{
    const rows = (dataset || []).filter(r => r.event_code === code && r.gender === gender);
    if (rows.length) return rows;
  }}
  return [];
}}
function populateEventSelect(id) {{
  const sel = document.getElementById(id);
  if (!sel) return;
  const events = DATA.ui_events || [];
  sel.innerHTML = events.map(e =>
    `<option value="${{e}}">${{EVENT_LABELS[e] || e}}</option>`
  ).join('');
}}

function fmtTime(s) {{
  if (s == null || isNaN(s)) return 'N/A';
  const m = Math.floor(s / 60), sec = s % 60;
  return m + ':' + sec.toFixed(2).padStart(5, '0');
}}

// BUG-02 / BUG-08 fixed: null-safe percentage formatter
function fmtPct(v) {{
  if (v == null || isNaN(v)) return '—';
  return (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
}}

// Safe toFixed that won't throw on null/undefined
function safe2(v) {{
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(2);
}}

const gridColor = 'rgba(30,45,69,0.8)';
const tickColor = '#64748b';
const baseFont  = {{ family: 'Barlow, sans-serif', size: 11 }};

function baseOpts(showLegend = true) {{
  return {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: showLegend, labels: {{ color: tickColor, font: baseFont }} }},
      tooltip: {{ backgroundColor: '#1a2235', titleColor: '#e2e8f0', bodyColor: '#94a3b8' }}
    }},
    scales: {{
      x: {{ grid: {{ color: gridColor }}, ticks: {{ color: tickColor, font: baseFont }} }},
      y: {{ grid: {{ color: gridColor }}, ticks: {{ color: tickColor, font: baseFont }} }}
    }}
  }};
}}

// ── BUG-05 fixed: tab switching uses passed button reference ──────────────────
function showTab(name, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  // Lazy render on first reveal
  if (name === 'progression') renderProgression();
  if (name === 'attrition')   renderAttrition();
  if (name === 'breakout')    renderBreakout();
  if (name === 'percentiles') renderPercentiles();
  if (name === 'tiers')       renderTierTab();
  if (name === 'evaluator')   renderEvaluator();
  if (name === 'volume')      renderVolume();
  if (name === 'predictor')   initPredictor();
}}

// ══════════════════════════════════════════════════════════════════════════════
//  PREDICTOR  (Monte Carlo field simulator)
// ══════════════════════════════════════════════════════════════════════════════

// ── State ─────────────────────────────────────────────────────────────────────
let predGender      = 'M';
let predInitialized = false;
let predChosenId    = null;   // athlete_id string
let predLastResult  = null;   // most recent simulation output

const N_SIMS = 10_000;

const PRED_TRANS_NEXT = {{ FR: 'SO', SO: 'JR', JR: 'SR' }};

// ── Initialise tab (called once on first reveal) ──────────────────────────────
function initPredictor() {{
  if (predInitialized) return;
  predInitialized = true;

  const mc = DATA.mc || {{}};

  // Populate scope dropdowns
  predScopeChanged();

  // Populate event dropdown
  const evSel = document.getElementById('pred-event');
  evSel.innerHTML = '';
  (DATA.ui_events || []).forEach(code => {{
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = EVENT_LABELS[code] || code;
    evSel.appendChild(opt);
  }});

  predFieldChanged();
}}

// ── Scope (conference vs region) changed ──────────────────────────────────────
function predScopeChanged() {{
  const scope = document.getElementById('pred-scope-type').value;
  const mc    = DATA.mc || {{}};
  const items = scope === 'conference' ? (mc.conferences || []) : (mc.xc_regions || []);

  const sel = document.getElementById('pred-scope-value');
  sel.innerHTML = items.map(v => `<option value="${{v}}">${{v}}</option>`).join('');
  predFieldChanged();
}}

function setPredGender(g) {{
  predGender = g;
  document.getElementById('pred-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('pred-f-btn').classList.toggle('active', g === 'F');
  predFieldChanged();
}}

// ── Build current field from DB roster ────────────────────────────────────────
function predGetField() {{
  const mc         = DATA.mc || {{}};
  const athletes   = mc.athletes || {{}};
  const scope      = document.getElementById('pred-scope-type').value;
  const scopeVal   = document.getElementById('pred-scope-value').value;
  const event_code = document.getElementById('pred-event').value;
  const transition = document.getElementById('pred-transition').value;
  const fromClass  = transition.split('_to_')[0];

  return Object.entries(athletes)
    .filter(([id, a]) => {
      if (a.gender !== predGender)         return false;
      if (a.class_year !== fromClass)      return false;
      if (!a.events[event_code])           return false;
      if (scope === 'conference' && a.conference !== scopeVal) return false;
      if (scope === 'xc_region'  && a.xc_region  !== scopeVal) return false;
      return true;
    })
    .map(([id, a]) => ({{
      id,
      name:       a.name,
      school:     a.school,
      class_year: a.class_year,
      best_time:  a.events[event_code].best_time,
      decile:     a.events[event_code].decile ?? null,
    }}))
    .sort((a, b) => a.best_time - b.best_time);
}}

function predFieldChanged() {{
  const field = predGetField();
  const countEl = document.getElementById('pred-field-count');
  countEl.textContent = field.length ? `${{field.length}} athletes in field` : 'No athletes found for this selection';

  // Clear prior results
  document.getElementById('pred-results').style.display = 'none';
  predLastResult = null;
  predChosenId = null;
  document.getElementById('pred-chosen-card').style.display = 'none';
  document.getElementById('pred-athlete-search').value = '';
  document.getElementById('pred-athlete-list').style.display = 'none';
}}

// ── Athlete search ────────────────────────────────────────────────────────────
function predAthleteSearch(q) {{
  const listEl = document.getElementById('pred-athlete-list');
  if (!q || q.length < 2) {{ listEl.style.display = 'none'; return; }}

  const field = predGetField();
  const ql    = q.toLowerCase();
  const hits  = field.filter(a => a.name.toLowerCase().includes(ql)).slice(0, 10);

  if (!hits.length) {{ listEl.style.display = 'none'; return; }}

  listEl.innerHTML = hits.map(a =>
    `<div onclick="predSelectAthlete('${{a.id}}')"
          style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);"
          onmouseover="this.style.background='var(--surface)'"
          onmouseout="this.style.background=''">
      <strong>${{a.name}}</strong>
      <span style="color:var(--muted);font-size:11px;margin-left:8px;">
        ${{a.school}} · ${{a.class_year}} · ${{secToMMSS(a.best_time)}}
      </span>
    </div>`
  ).join('');
  listEl.style.display = 'block';
}}

function predSelectAthlete(id) {{
  predChosenId = id;
  document.getElementById('pred-athlete-list').style.display = 'none';

  const mc  = DATA.mc || {{}};
  const ath = mc.athletes[id];
  const event_code = document.getElementById('pred-event').value;
  const ev  = ath.events[event_code];

  const card = document.getElementById('pred-chosen-card');
  card.innerHTML = `
    <strong style="font-size:14px;">${{ath.name}}</strong>
    <span style="color:var(--muted);font-size:12px;margin-left:10px;">
      ${{ath.school}} · ${{ath.class_year}} · ${{ath.conference || ath.xc_region || ''}}
    </span><br>
    <span style="font-size:13px;margin-top:4px;display:inline-block;">
      Current best (${{EVENT_LABELS[event_code] || event_code}}):
      <strong>${{secToMMSS(ev.best_time)}}</strong>
      · National decile: <strong>D${{ev.decile ?? '?'}}</strong>
    </span>`;
  card.style.display = 'block';
}}

// ── Monte Carlo engine ────────────────────────────────────────────────────────
function getImprovementDistribution(event_code, transition) {{
  const dists = (DATA.mc || {{}}).improvement_distributions || {{}};
  const block = dists[event_code]?.[predGender]?.[transition];
  return block || null;
}}

function sampleImprovement(decile, block) {{
  // Pick the per-decile array if it has enough samples, else fall back to "all"
  const arr = (decile && block[String(decile)] && block[String(decile)].length >= 5)
    ? block[String(decile)]
    : block['all'];
  if (!arr || !arr.length) return 0;
  // Uniform random draw from the empirical distribution
  return arr[Math.floor(Math.random() * arr.length)];
}}

function applyImprovement(time_sec, imp_pct) {{
  // imp_pct positive = faster; time decreases
  return time_sec * (1 - imp_pct / 100);
}}

function runMonteCarlo() {{
  const field      = predGetField();
  const transition = document.getElementById('pred-transition').value;
  const event_code = document.getElementById('pred-event').value;
  const statusEl   = document.getElementById('pred-run-status');

  if (!predChosenId) {{
    statusEl.textContent = 'Search for and select an athlete first.';
    return;
  }}
  if (!field.length) {{
    statusEl.textContent = 'No athletes in field.';
    return;
  }}
  if (!field.find(a => a.id === predChosenId)) {{
    statusEl.textContent = 'Selected athlete is not in the current field.';
    return;
  }}

  statusEl.textContent = 'Simulating…';
  document.getElementById('pred-run-btn').disabled = true;

  // Small timeout so the UI can repaint before the compute loop
  setTimeout(() => {{
    const block = getImprovementDistribution(event_code, transition);

    // place_counts[i] = number of sims where chosen athlete finished in place i+1
    const placeCounts = new Array(field.length).fill(0);

    // Also track each athlete's simulated times for the median field table
    const allSimTimes = field.map(() => []);

    for (let sim = 0; sim < N_SIMS; sim++) {{
      const simTimes = field.map(ath => {{
        if (!block) return ath.best_time; // no dist → hold fixed
        const imp = sampleImprovement(ath.decile, block);
        return applyImprovement(ath.best_time, imp);
      }});

      // Rank (lower time = better)
      const chosenSimTime = simTimes[field.findIndex(a => a.id === predChosenId)];
      let place = 1;
      simTimes.forEach((t, i) => {{
        if (field[i].id !== predChosenId && t < chosenSimTime) place++;
      }});
      placeCounts[place - 1]++;

      simTimes.forEach((t, i) => allSimTimes[i].push(t));
    }}

    // Compute median simulated time per athlete for the field table
    const medianTimes = allSimTimes.map(times => {{
      const s = [...times].sort((a, b) => a - b);
      return s[Math.floor(s.length / 2)];
    }});

    predLastResult = {{ placeCounts, field, medianTimes, event_code, transition, block }};
    renderPredictorResults(predLastResult);

    statusEl.textContent = `Done — ${{N_SIMS.toLocaleString()}} simulations`;
    document.getElementById('pred-run-btn').disabled = false;
  }}, 30);
}}

// ── Render results ────────────────────────────────────────────────────────────
function renderPredictorResults({{ placeCounts, field, medianTimes, event_code }}) {{
  const chosenIdx = field.findIndex(a => a.id === predChosenId);
  const chosenAth = field[chosenIdx];
  const n         = field.length;

  document.getElementById('pred-results').style.display = 'block';

  // ── Finishing place bar chart ──
  mkChart('chart-pred-places', {{
    type: 'bar',
    data: {{
      labels:   Array.from({{length: n}}, (_, i) => `P${{i+1}}`),
      datasets: [{{
        label: 'Probability (%)',
        data:  placeCounts.map(c => +(c / N_SIMS * 100).toFixed(2)),
        backgroundColor: placeCounts.map((_, i) =>
          i === 0 ? '#f59e0b' : i < 3 ? '#00c8ff' : i < 6 ? '#7fff6e' : '#444'
        ),
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => `${{ctx.raw.toFixed(1)}}%` }} }} }},
      scales: {{
        x: {{ title: {{ display: true, text: 'Finishing Place' }} }},
        y: {{ title: {{ display: true, text: '% of simulations' }},
              ticks: {{ callback: v => v + '%' }} }},
      }},
    }},
  }});

  // ── Cumulative chart ──
  let cumul = 0;
  const cumulData = placeCounts.map(c => {{ cumul += c / N_SIMS * 100; return +cumul.toFixed(2); }});
  mkChart('chart-pred-cumul', {{
    type: 'line',
    data: {{
      labels:   Array.from({{length: n}}, (_, i) => `Top ${{i+1}}`),
      datasets: [{{
        label: 'Cumulative P(%)',
        data:  cumulData,
        borderColor: '#00c8ff',
        backgroundColor: 'rgba(0,200,255,0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => `${{ctx.raw.toFixed(1)}}%` }} }} }},
      scales: {{
        x: {{ title: {{ display: true, text: 'Finish ≤ Place' }} }},
        y: {{ min: 0, max: 100,
              title: {{ display: true, text: 'Cumulative probability (%)' }},
              ticks: {{ callback: v => v + '%' }} }},
      }},
    }},
  }});

  // ── Key probability table ──
  const targets = [1, 3, 6, 10, Math.ceil(n / 2)].filter((v, i, a) => v <= n && a.indexOf(v) === i);
  const probTableEl = document.getElementById('pred-prob-table');
  probTableEl.innerHTML = `
    <table class="data-table">
      <thead><tr>
        ${{targets.map(t => `<th style="text-align:center;">Top ${{t}}</th>`).join('')}}
        <th style="text-align:center;">Win</th>
      </tr></thead>
      <tbody><tr>
        ${{targets.map(t => {{
          const p = cumulData[t - 1] || 0;
          return `<td style="text-align:center;font-weight:600;color:${{p > 50 ? 'var(--good)' : p > 20 ? 'var(--text)' : 'var(--muted)'}}">${{p.toFixed(1)}}%</td>`;
        }}).join('')}}
        <td style="text-align:center;font-weight:600;color:var(--accent)">
          ${{(placeCounts[0] / N_SIMS * 100).toFixed(1)}}%
        </td>
      </tr></tbody>
    </table>`;

  // ── Median field table ──
  const ranked = field
    .map((ath, i) => ({{ ...ath, medianTime: medianTimes[i], isChosen: ath.id === predChosenId }}))
    .sort((a, b) => a.medianTime - b.medianTime);

  const fieldTableEl = document.getElementById('pred-field-table');
  fieldTableEl.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th>#</th><th>Athlete</th><th>School</th><th>Yr</th>
        <th class="num">Current Best</th><th class="num">Median Sim</th>
        <th class="num">Δ</th><th class="num">Decile</th>
      </tr></thead>
      <tbody>
        ${{ranked.map((ath, idx) => {{
          const delta  = ath.best_time - ath.medianTime;
          const deltaS = delta >= 0 ? `-${{secToMMSS(Math.abs(delta))}`
                                    : `+${{secToMMSS(Math.abs(delta))}}`; // + = slower
          const rowStyle = ath.isChosen
            ? 'background:rgba(0,200,255,0.10);font-weight:600;'
            : '';
          return `<tr style="${{rowStyle}}">
            <td>${{idx + 1}}</td>
            <td>${{ath.name}}${{ath.isChosen ? ' ★' : ''}}</td>
            <td style="font-size:11px;color:var(--muted)">${{ath.school}}</td>
            <td>${{ath.class_year}}</td>
            <td class="num">${{secToMMSS(ath.best_time)}}</td>
            <td class="num">${{secToMMSS(ath.medianTime)}}</td>
            <td class="num" style="color:${{delta >= 0 ? 'var(--good)' : 'var(--bad)}}">${{deltaS}}</td>
            <td class="num">${{ath.decile ? 'D' + ath.decile : '—'}}</td>
          </tr>`;
        }}).join('')}}
      </tbody>
    </table>`;
}}

// ── Utility: seconds → MM:SS.xx ──────────────────────────────────────────────
function secToMMSS(s) {{
  if (s == null || isNaN(s)) return '—';
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m > 0
    ? `${{m}}:${{r.toFixed(2).padStart(5, '0')}}`
    : `${{r.toFixed(2)}}s`;
}}

// ══════════════════════════════════════════════════════════════════════════════
//  OVERVIEW
// ══════════════════════════════════════════════════════════════════════════════
function initOverview() {{
  ['ov-event-sel','ov-event-sel2','prog-event','br-event','tier-event','eval-event'].forEach(populateEventSelect);

  const s = DATA.summary;
  document.getElementById('kpi-athletes').textContent = s.athletes.toLocaleString();
  document.getElementById('kpi-results').textContent  = s.results.toLocaleString();
  document.getElementById('kpi-schools').textContent  = s.schools.toLocaleString();
  document.getElementById('kpi-seasons').textContent  = s.seasons.toLocaleString();
  document.getElementById('gen-date').textContent =
    new Date().toLocaleDateString('en-US', {{ dateStyle: 'long' }});

  // Results by event
  const ev = DATA.event_counts;
  if (ev && ev.length) {{
    mkChart('chart-events', {{
      type: 'bar',
      data: {{
        labels: ev.map(r => r.event_code),
        datasets: [{{
          label: 'Results',
          data: ev.map(r => r.n_results),
          backgroundColor: ev.map((_, i) => COLORS[i % COLORS.length] + 'bb'),
          borderColor:     ev.map((_, i) => COLORS[i % COLORS.length]),
          borderWidth: 1, borderRadius: 4
        }}]
      }},
      options: baseOpts(false)
    }});
  }}

  // Top schools (horizontal bar)
  const sc = DATA.top_schools;
  if (sc && sc.length) {{
    mkChart('chart-schools', {{
      type: 'bar',
      data: {{
        labels: sc.map(r => r.school),
        datasets: [{{
          label: 'Athletes',
          data: sc.map(r => r.athletes),
          backgroundColor: '#00c8ffbb', borderColor: '#00c8ff',
          borderWidth: 1, borderRadius: 4
        }}]
      }},
      options: {{ ...baseOpts(false), indexAxis: 'y' }}
    }});
  }}

  renderOverviewCurve();
  renderImprovementBar();
}}

function setOvGender(g) {{
  ovGender = g;
  document.getElementById('ov-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('ov-f-btn').classList.toggle('active', g === 'F');
  renderOverviewCurve();
}}
function setOv2Gender(g) {{
  ov2Gender = g;
  document.getElementById('ov2-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('ov2-f-btn').classList.toggle('active', g === 'F');
  renderImprovementBar();
}}

function renderOverviewCurve() {{
  const event = document.getElementById('ov-event-sel').value;
  const rows  = rowsForEvent(DATA.class_performance, event, ovGender);
  rows.sort((a, b) => CLASS_ORDER.indexOf(a.class_year) - CLASS_ORDER.indexOf(b.class_year));

  if (!rows.length) {{
    mkChart('chart-curve', {{
      type: 'bar',
      data: {{ labels: ['No data'], datasets: [{{ data: [0] }}] }},
      options: {{ ...baseOpts(false), plugins: {{ legend: {{ display: false }}, title: {{
        display: true, text: `No class-year data for ${{EVENT_LABELS[event] || event}} (${{ovGender === 'M' ? 'Men' : 'Women'}})`,
        color: tickColor, font: baseFont
      }} }} }}
    }});
    return;
  }}

  mkChart('chart-curve', {{
    type: 'line',
    data: {{
      labels: rows.map(r => r.class_year),
      datasets: [
        {{
          label: 'Avg Time',
          data: rows.map(r => r.avg_time),
          borderColor: '#00c8ff', backgroundColor: 'rgba(0,200,255,0.1)',
          tension: 0.4, fill: true, pointRadius: 5, pointBackgroundColor: '#00c8ff'
        }},
        {{
          label: 'Best Time',
          data: rows.map(r => r.best_time),
          borderColor: '#7fff6e', backgroundColor: 'rgba(127,255,110,0.05)',
          tension: 0.4, borderDash: [4,4], pointRadius: 4, pointBackgroundColor: '#7fff6e'
        }}
      ]
    }},
    options: {{
      ...baseOpts(true),
      scales: {{
        x: {{ grid: {{ color: gridColor }}, ticks: {{ color: tickColor, font: baseFont }} }},
        y: {{
          grid: {{ color: gridColor }},
          // reverse: true → lowest time (fastest) at top — correct for running
          reverse: true,
          ticks: {{ color: tickColor, font: baseFont, callback: v => fmtTime(v) }}
        }}
      }}
    }}
  }});
}}

// BUG-07 fixed: removed misleading Std Dev bar; show mean only with annotation note
function renderImprovementBar() {{
  const event = document.getElementById('ov-event-sel2').value;
  const rows  = rowsForEvent(DATA.progression, event, ov2Gender);
  rows.sort((a, b) => CLASS_ORDER.indexOf(a.from_class) - CLASS_ORDER.indexOf(b.from_class));

  if (!rows.length) {{
    mkChart('chart-improvement', {{
      type: 'bar',
      data: {{ labels: ['No data'], datasets: [{{ data: [0] }}] }},
      options: {{ ...baseOpts(false), plugins: {{ legend: {{ display: false }}, title: {{
        display: true, text: `No progression data for ${{EVENT_LABELS[event] || event}} (${{ov2Gender === 'M' ? 'Men' : 'Women'}})`,
        color: tickColor, font: baseFont
      }} }} }}
    }});
    return;
  }}

  const labels = rows.map(r =>
    TRANS_LABELS[r.from_class + '_to_' + r.to_class] || r.from_class + '→' + r.to_class
  );
  const means = rows.map(r => r.mean != null ? parseFloat(Number(r.mean).toFixed(2)) : null);

  mkChart('chart-improvement', {{
    type: 'bar',
    data: {{
      labels,
      datasets: [{{
        label: 'Mean Improvement %',
        data: means,
        backgroundColor: means.map(v => v != null && v >= 0
          ? 'rgba(34,211,160,0.7)' : 'rgba(248,113,113,0.7)'),
        borderColor: means.map(v => v != null && v >= 0 ? '#22d3a0' : '#f87171'),
        borderWidth: 1, borderRadius: 4
      }}]
    }},
    options: {{
      ...baseOpts(false),
      plugins: {{
        ...baseOpts(false).plugins,
        tooltip: {{
          callbacks: {{
            afterLabel: ctx => {{
              const r = rows[ctx.dataIndex];
              return r && r.std != null ? `Std Dev: ±${{Number(r.std).toFixed(2)}}%` : '';
            }}
          }}
        }}
      }}
    }}
  }});
}}

// ══════════════════════════════════════════════════════════════════════════════
//  PROGRESSION
// ══════════════════════════════════════════════════════════════════════════════
function setProgGender(g) {{
  progGender = g;
  document.getElementById('prog-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('prog-f-btn').classList.toggle('active', g === 'F');
  renderProgression();
}}

let progMode = 'raw'; // 'raw' | 'discounted'
function setProgMode(mode) {{
  progMode = mode;
  document.getElementById('prog-raw-btn').classList.toggle('active', mode === 'raw');
  document.getElementById('prog-disc-btn').classList.toggle('active', mode === 'discounted');
  renderProgression();
}}

// Pull a stat field, falling back to the raw value if a discounted
// variant isn't present (e.g. older DBs before this metric existed).
function progVal(r, field) {{
  if (progMode === 'discounted') {{
    const v = r['discounted_' + field];
    return v != null ? v : r[field];
  }}
  return r[field];
}}

function renderProgression() {{
  const event = document.getElementById('prog-event').value;
  const rows  = rowsForEvent(DATA.progression, event, progGender);
  rows.sort((a, b) => CLASS_ORDER.indexOf(a.from_class) - CLASS_ORDER.indexOf(b.from_class));

  const tbody = document.getElementById('prog-table-body');

  renderYearlyTrends(event);

  if (!rows.length) {{
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No data for this selection</td></tr>';
    return;
  }}

  const labels = rows.map(r =>
    TRANS_LABELS[r.from_class + '_to_' + r.to_class] || r.from_class + '→' + r.to_class
  );

  mkChart('chart-prog-mean', {{
    type: 'bar',
    data: {{
      labels,
      datasets: [{{
        label: progMode === 'discounted' ? 'Mean % Improvement (vs. NCAA field)' : 'Mean % Improvement',
        data: rows.map(r => progVal(r, 'mean') != null ? +Number(progVal(r, 'mean')).toFixed(3) : null),
        backgroundColor: 'rgba(0,200,255,0.6)', borderColor: '#00c8ff',
        borderWidth: 1, borderRadius: 6
      }}]
    }},
    options: {{
      ...baseOpts(false),
      plugins: {{
        ...baseOpts(false).plugins,
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.raw != null ? (ctx.raw > 0 ? '+' : '') + ctx.raw.toFixed(2) : '—'}}%`
          }}
        }}
      }}
    }}
  }});

  // BUG-08 fixed: guard nullable p-values with null-safe access
  mkChart('chart-prog-dist', {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{ label:'P10',    data: rows.map(r => progVal(r,'p10')    != null ? +Number(progVal(r,'p10')).toFixed(2)    : null), backgroundColor:'rgba(248,113,113,0.5)', borderColor:'#f87171', borderWidth:1, borderRadius:3 }},
        {{ label:'P25',    data: rows.map(r => progVal(r,'p25')    != null ? +Number(progVal(r,'p25')).toFixed(2)    : null), backgroundColor:'rgba(251,191,36,0.5)',  borderColor:'#fbbf24', borderWidth:1, borderRadius:3 }},
        {{ label:'Median', data: rows.map(r => progVal(r,'median') != null ? +Number(progVal(r,'median')).toFixed(2) : null), backgroundColor:'rgba(0,200,255,0.7)',   borderColor:'#00c8ff', borderWidth:1, borderRadius:3 }},
        {{ label:'P75',    data: rows.map(r => progVal(r,'p75')    != null ? +Number(progVal(r,'p75')).toFixed(2)    : null), backgroundColor:'rgba(127,255,110,0.5)', borderColor:'#7fff6e', borderWidth:1, borderRadius:3 }},
        {{ label:'P90 (top 10% improvers)', data: rows.map(r => progVal(r,'p90') != null ? +Number(progVal(r,'p90')).toFixed(2) : null), backgroundColor:'rgba(167,139,250,0.5)', borderColor:'#a78bfa', borderWidth:1, borderRadius:3 }},
      ]
    }},
    options: baseOpts(true)
  }});

  // Table
  tbody.innerHTML = rows.map(r => {{
    const key = r.from_class + '_to_' + r.to_class;
    const lbl = TRANS_LABELS[key] || key;
    const mean = progVal(r, 'mean'), median = progVal(r, 'median'), std = progVal(r, 'std');
    return `<tr>
      <td><strong>${{lbl}}</strong></td>
      <td class="num">${{r.n != null ? Number(r.n).toLocaleString() : '—'}}</td>
      <td class="num" style="color:${{mean != null && mean >= 0 ? 'var(--good)' : 'var(--bad)'}}">
        ${{fmtPct(mean)}}</td>
      <td class="num">${{fmtPct(median)}}</td>
      <td style="color:var(--muted)">${{std != null ? Number(std).toFixed(2) + '%' : '—'}}</td>
      <td style="color:var(--bad)">${{fmtPct(progVal(r,'p10'))}}</td>
      <td style="color:var(--warn)">${{fmtPct(progVal(r,'p25'))}}</td>
      <td style="color:var(--good)">${{fmtPct(progVal(r,'p75'))}}</td>
      <td style="color:var(--accent)">${{fmtPct(progVal(r,'p90'))}}</td>
    </tr>`;
  }}).join('');
}}

// NCAA-wide percentile time trends by season for the selected event.
function renderYearlyTrends(event) {{
  ['m', 'f'].forEach(g => {{
    const gender = g === 'm' ? 'M' : 'F';
    const yt = DATA.yearly_trends || {{}};
    const code = firstEventKey(yt, event);
    const rows = (yt[code] && yt[code][gender]) || [];
    const canvasId = `chart-yearly-trend-${{g}}`;

    if (!rows.length) {{
      mkChart(canvasId, {{
        type: 'line',
        data: {{ labels: ['No data'], datasets: [{{ data: [null] }}] }},
        options: {{ ...baseOpts(false), plugins: {{ legend: {{ display: false }}, title: {{
          display: true, text: `No yearly data for ${{EVENT_LABELS[event] || event}} (${{gender === 'M' ? 'Men' : 'Women'}})`,
          color: tickColor, font: baseFont
        }} }} }}
      }});
      return;
    }}

    mkChart(canvasId, {{
      type: 'line',
      data: {{
        labels: rows.map(r => r.year),
        datasets: [
          {{ label: 'P25 (slower quartile)', data: rows.map(r => r.p25 ?? null), borderColor: '#7fff6e', backgroundColor: 'rgba(127,255,110,0.08)', tension: 0.3, pointRadius: 3 }},
          {{ label: 'Median (P50)',          data: rows.map(r => r.p50 ?? null), borderColor: '#00c8ff', backgroundColor: 'rgba(0,200,255,0.1)',   tension: 0.3, pointRadius: 4, borderWidth: 2 }},
          {{ label: 'P75 (faster quartile)', data: rows.map(r => r.p75 ?? null), borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.08)', tension: 0.3, pointRadius: 3 }},
        ]
      }},
      options: {{
        ...baseOpts(true),
        plugins: {{
          ...baseOpts(true).plugins,
          tooltip: {{
            backgroundColor: '#1a2235', titleColor: '#e2e8f0', bodyColor: '#94a3b8',
            callbacks: {{ label: ctx => ` ${{ctx.dataset.label}}: ${{fmtTime(ctx.raw)}}` }}
          }}
        }},
        scales: {{
          x: {{ grid: {{ color: gridColor }}, ticks: {{ color: tickColor, font: baseFont }} }},
          y: {{
            grid: {{ color: gridColor }},
            reverse: true,
            ticks: {{ color: tickColor, font: baseFont, callback: v => fmtTime(v) }}
          }}
        }}
      }}
    }});
  }});
}}

// ══════════════════════════════════════════════════════════════════════════════
//  BREAKOUT
// ══════════════════════════════════════════════════════════════════════════════
function setBrGender(g) {{
  brGender = g;
  document.getElementById('br-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('br-f-btn').classList.toggle('active', g === 'F');
  renderBreakout();
}}

function setBrMode(mode) {{
  brMode = mode;
  document.getElementById('br-raw-btn').classList.toggle('active', mode === 'raw');
  document.getElementById('br-disc-btn').classList.toggle('active', mode === 'discounted');
  renderBreakout();
}}

// Map athlete time percentile (0=slowest … 100=fastest) to a benchmark time
// using the national percentile table for the selected event/gender.
function timeAtPercentile(event, gender, pct) {{
  const tables = DATA.percentiles || {{}};
  const code = firstEventKey(tables, event);
  const v = tables[code]?.[gender];
  if (!v) return null;

  const anchors = [
    [5, v.p5], [10, v.p10], [25, v.p25], [50, v.p50],
    [75, v.p75], [90, v.p90], [95, v.p95]
  ];
  pct = Math.max(0, Math.min(100, pct));

  if (pct <= anchors[0][0]) {{
    const [p0, t0] = anchors[0], [p1, t1] = anchors[1];
    return t0 + (t1 - t0) * (pct - p0) / (p1 - p0);
  }}
  if (pct >= anchors[anchors.length - 1][0]) {{
    const [p0, t0] = anchors[anchors.length - 2];
    const [p1, t1] = anchors[anchors.length - 1];
    return t0 + (t1 - t0) * (pct - p0) / (p1 - p0);
  }}
  for (let i = 0; i < anchors.length - 1; i++) {{
    const [p0, t0] = anchors[i], [p1, t1] = anchors[i + 1];
    if (pct >= p0 && pct <= p1) {{
      return t0 + (t1 - t0) * (pct - p0) / (p1 - p0);
    }}
  }}
  return null;
}}

function updateBrPctLabels() {{
  const event = document.getElementById('br-event')?.value;
  const minEl = document.getElementById('br-pct-min-lbl');
  const maxEl = document.getElementById('br-pct-max-lbl');
  if (!minEl || !maxEl) return;

  const fmtLbl = pct => {{
    const t = event ? timeAtPercentile(event, brGender, pct) : null;
    return t != null ? `${{pct}}% (${{fmtTime(t)}})` : `${{pct}}%`;
  }};

  minEl.textContent = fmtLbl(brPctMin);
  maxEl.textContent = fmtLbl(brPctMax);
}}

function initBreakoutSlider() {{
  const slider = document.getElementById('breakout-range-slider');
  if (!slider) return;

  noUiSlider.create(slider, {{
    start: [0, 100],
    connect: true,
    step: 1,
    range: {{
      min: 0,
      max: 100
    }}
  }});

  slider.noUiSlider.on('update', values => {{
    brPctMin = Math.round(+values[0]);
    brPctMax = Math.round(+values[1]);
    renderBreakout();
  }});
}}

// Format seconds as "45.0s" or "1m 30.0s"
function fmtSec(s) {{
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60), sec = (s % 60).toFixed(1);
  return m + 'm ' + sec + 's';
}}

function renderBreakout() {{
  const event  = document.getElementById('br-event').value;
  updateBrPctLabels();
  const br     = DATA.breakout || {{}};
  const heatEl = document.getElementById('breakout-heatmap');
  const brKey  = firstEventKey(br, event);

  const minPct = brPctMin;
  const maxPct = brPctMax;

  if (!br[brKey] || !br[brKey][brGender]) {{
    heatEl.innerHTML = '<div class="empty-state">No breakout data for this selection</div>';
    document.getElementById('br-pct-count').textContent = '';
    return;
  }}

  const gData = br[brKey][brGender];
  const thresholds_s = gData['_thresholds_seconds'] || [];
  const keys = Object.keys(gData).filter(k =>
    k !== '_thresholds_seconds' && gData[k] && gData[k].n != null
  );

  if (!keys.length || !thresholds_s.length) {{
    heatEl.innerHTML = '<div class="empty-state">Insufficient data</div>';
    return;
  }}

  // ── Per-athlete filtering via athlete_points ──────────────────────────────
  // athlete_points: array of [from_time_percentile (0–100), improvement_pct]
  // Filter to athletes whose from_time_percentile falls in [brPctMin, brPctMax].
  // Then recompute hit-rates from the filtered subset.
  const isFiltered = minPct > 0 || maxPct < 100;
  const ptsKey = brMode === 'discounted' ? 'athlete_points_discounted' : 'athlete_points';

  function filteredHitRates(k) {{
    const pts = gData[k][ptsKey] || gData[k]['athlete_points'] || null;
    if (!pts || !pts.length) return null; // fall back to pre-aggregated values
    const sub = pts.filter(([pct]) => pct >= minPct && pct <= maxPct);
    if (!sub.length) return {{ n: 0, rates: thresholds_s.map(() => 0) }};
    // For each threshold, convert seconds → equivalent % using the event P50
    // which was already used to derive thresholds_s — so pct_equiv = thr_s / median * 100
    // But improvement_pct in athlete_points is already a raw % — compare directly.
    // thresholds_s[i] is X seconds; the stored improvement is in % of from_time.
    // The fraction stored is (from_time - to_time)/from_time * 100.
    // We need: did athlete improve by >= thr_s seconds?
    // We don't store from_time in athlete_points, only the pct.
    // Use the same approximation as the Python backend: thr_pct = thr_s / P50 * 100.
    // P50 is not directly in the JSON but thresholds_s = fractions * P50, so:
    //   P50 ≈ thresholds_s[2] / 0.05  (the 5% threshold, index 2)
    const p50_approx = thresholds_s.length >= 3 ? thresholds_s[2] / 0.05 : null;
    const rates = thresholds_s.map(thr_s => {{
      if (!p50_approx) return 0;
      const thr_pct = (thr_s / p50_approx) * 100;
      return sub.filter(([, imp]) => imp >= thr_pct).length / sub.length;
    }});
    return {{ n: sub.length, rates }};
  }}

  // Update athlete count label
  const totalFiltered = keys.reduce((sum, k) => {{
    const pts = gData[k][ptsKey] || gData[k]['athlete_points'] || [];
    return sum + pts.filter(([pct]) => pct >= minPct && pct <= maxPct).length;
  }}, 0);
  

  const labels  = keys.map(k => TRANS_LABELS[k] || k);
  const tColors = ['#00c8ff','#7fff6e','#f59e0b','#f87171','#a78bfa'];

  // Build per-threshold, per-transition hit-rate data
  const datasets = thresholds_s.map((thr_s, i) => {{
    const data = keys.map(k => {{
      const fr = filteredHitRates(k);
      if (fr) {{
        return fr.n > 0 ? +(fr.rates[i] * 100).toFixed(1) : 0;
      }}
      // Fallback to pre-aggregated value (no slider or old JSON without athlete_points)
      const suffix = brMode === 'discounted' ? '_discounted' : '';
      const val    = gData[k][`p_improve_${{thr_s}}s${{suffix}}`]
                  ?? gData[k][`p_improve_${{thr_s}}s`] ?? 0;
      return +(val * 100).toFixed(1);
    }});
    return {{
      label: `≥${{fmtSec(thr_s)}}`,
      data,
      backgroundColor: (tColors[i] || '#94a3b8') + '99',
      borderColor: tColors[i] || '#94a3b8',
      borderWidth: 1, borderRadius: 4
    }};
  }});

  // Y-axis max = highest bar + 15%, rounded up to nearest 5
  const allVals = datasets.flatMap(ds => ds.data).filter(v => v != null);
  const rawMax  = allVals.length ? Math.max(...allVals) : 100;
  const yMax    = Math.min(100, Math.ceil((rawMax * 1.15) / 5) * 5);

  mkChart('chart-breakout', {{
    type: 'bar',
    data: {{ labels, datasets }},
    options: {{
      ...baseOpts(true),
      plugins: {{
        ...baseOpts(true).plugins,
        title: {{
          display: true,
          text: isFiltered
            ? `Percentile range: ${{minPct}}–${{maxPct}}% · ${{totalFiltered.toLocaleString()}} athletes`
            : `All athletes · ${{brGender === 'M' ? "Men's" : "Women's"}} median ${{EVENT_LABELS[event] || event}} thresholds`,
          color: tickColor, font: baseFont
        }}
      }},
      scales: {{
        ...baseOpts(true).scales,
        y: {{
          grid: {{ color: gridColor }},
          ticks: {{ color: tickColor, font: baseFont, callback: v => v + '%' }},
          max: yMax
        }}
      }}
    }}
  }});

  // ── Heatmap ───────────────────────────────────────────────────────────────
  const cellStyle = v => {{
    const alpha = Math.min(v * 4, 1);
    const bg = v >= 0.3 ? `rgba(34,211,160,${{alpha}})` :
               v >= 0.1 ? `rgba(245,158,11,${{alpha}})` :
                          `rgba(248,113,113,${{alpha}})`;
    return `background:${{bg}};color:#0a0e1a;font-weight:700;`;
  }};

  heatEl.innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead><tr>
        <th style="color:var(--muted);padding:8px;text-align:left;">Transition</th>
        ${{thresholds_s.map(t => `<th style="color:var(--muted);padding:8px;text-align:center;">≥${{fmtSec(t)}}</th>`).join('')}}
        <th style="color:var(--muted);padding:8px;" title="Athletes in selected range">Athletes</th>
      </tr></thead>
      <tbody>
        ${{keys.map((k, ki) => {{
          const r  = gData[k];
          const fr = filteredHitRates(k);
          return `<tr>
            <td style="padding:8px;color:var(--text);font-weight:600;">${{TRANS_LABELS[k] || k}}</td>
            ${{thresholds_s.map((t, ti) => {{
              const v = fr
                ? (fr.n > 0 ? fr.rates[ti] : 0)
                : (r[`p_improve_${{t}}s`] || 0);
              return `<td style="padding:8px;text-align:center;border-radius:4px;${{cellStyle(v)}}">${{(v*100).toFixed(1)}}%</td>`;
            }}).join('')}}
            <td style="padding:8px;color:var(--muted)">${{fr ? fr.n.toLocaleString() : (r.n != null ? Number(r.n).toLocaleString() : '—')}}</td>
          </tr>`;
        }}).join('')}}
      </tbody>
    </table>
    <div style="margin-top:8px;font-size:11px;color:var(--muted);">
      Thresholds: ${{thresholds_s.map(fmtSec).join(' · ')}} — derived from the ${{brGender === 'M' ? "men's" : "women's"}} P50 median time for ${{EVENT_LABELS[event] || event}}.
      ${{isFiltered ? ` · Filtered to percentile ${{minPct}}–${{maxPct}}%.` : ''}}
    </div>`;
}}

// ══════════════════════════════════════════════════════════════════════════════
//  PERCENTILES
// ══════════════════════════════════════════════════════════════════════════════
function renderPercentiles() {{
  const pct    = DATA.percentiles || {{}};
  const el     = document.getElementById('percentile-tables');
  const pctKeys   = ['p5','p10','p25','p50','p75','p90','p95'];
  const pctLabels = ['P5 (slow)','P10','P25','Median','P75','P90','P95 (elite)'];
  const pctColors = ['#f87171','#fb923c','#fbbf24','#94a3b8','#34d399','#22d3a0','#00c8ff'];

  if (!Object.keys(pct).length) {{
    el.innerHTML = '<div class="empty-state">No percentile data available</div>';
    return;
  }}

  el.innerHTML = Object.entries(pct).map(([event, genders]) => {{
    return `<div class="card" style="margin-bottom:24px;">
      <div class="card-title">${{event}}</div>
      ${{Object.entries(genders).map(([gender, vals]) => {{
        const ruler = pctKeys.map((k, i) => {{
          const t = vals[k];
          return `<div class="pct-seg" style="background:${{pctColors[i]}};font-size:10px;">${{fmtTime(t)}}</div>`;
        }}).join('');
        return `
          <div style="margin-bottom:16px;">
            <div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;letter-spacing:1px;">
              ${{gender === 'M' ? 'MEN' : 'WOMEN'}}
            </div>
            <div class="pct-ruler">${{ruler}}</div>
            <div style="display:flex;">
              ${{pctLabels.map((l, i) =>
                `<div style="flex:1;text-align:center;font-size:9px;color:${{pctColors[i]}};letter-spacing:0.5px;">${{l}}</div>`
              ).join('')}}
            </div>
          </div>`;
      }}).join('')}}
      <table class="data-table" style="margin-top:16px;">
        <thead><tr>
          <th>Gender</th>
          ${{pctLabels.map((l, i) => `<th style="color:${{pctColors[i]}}">${{l}}</th>`).join('')}}
        </tr></thead>
        <tbody>
          ${{Object.entries(genders).map(([gender, vals]) => `
            <tr>
              <td><strong>${{gender === 'M' ? 'Men' : 'Women'}}</strong></td>
              ${{pctKeys.map((k, i) =>
                `<td class="num" style="color:${{pctColors[i]}}">${{fmtTime(vals[k])}}</td>`
              ).join('')}}
            </tr>`).join('')}}
        </tbody>
      </table>
    </div>`;
  }}).join('');
}}

// ══════════════════════════════════════════════════════════════════════════════
//  TIER TRANSITIONS
// ══════════════════════════════════════════════════════════════════════════════
function setTierGender(g) {{
  tierGender = g;
  document.getElementById('tier-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('tier-f-btn').classList.toggle('active', g === 'F');
  renderTierTab();
}}

function setTierMode(mode) {{
  tierMode = mode;
  document.getElementById('tier-raw-btn').classList.toggle('active', mode === 'raw');
  document.getElementById('tier-disc-btn').classList.toggle('active', mode === 'discounted');
  renderTierMatrix();
  renderTierDecileProgression();
}}

function populateTierTransitionSelect() {{
  const sel   = document.getElementById('tier-trans-sel');
  const event = document.getElementById('tier-event').value;
  const rt    = DATA.rating_transitions || {{}};
  const rtKey = firstEventKey(rt, event);
  const gData = (rt[rtKey] || {{}})[tierGender] || {{}};
  const keys  = Object.keys(gData).filter(k => gData[k] && gData[k].matrix);
  if (!keys.length) {{
    sel.innerHTML = '<option value="">No data</option>';
    return;
  }}
  keys.sort((a, b) => {{
    const ia = TIER_TRANS_ORDER.indexOf(a);
    const ib = TIER_TRANS_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  }});
  sel.innerHTML = keys.map(k =>
    `<option value="${{k}}">${{TRANS_LABELS[k] || k}}</option>`
  ).join('');
}}

function onTierTransChange() {{
  renderTierMatrix();
  renderTierDecileProgression();
}}

function renderTierTab() {{
  const sel  = document.getElementById('tier-trans-sel');
  const prev = sel ? sel.value : '';
  populateTierTransitionSelect();
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  renderTierMatrix();
  renderTierDecileProgression();
}}

// Data decile keys: 1 = slowest … 10 = fastest (displayed D1 top-left → D10 bottom-right).
function tierDecileMidPctile(d) {{
  return d * 10 - 5;
}}

function tierDecileLabel(event, gender, d) {{
  const pct = tierDecileMidPctile(d);
  const t = event ? timeAtPercentile(event, gender, pct) : null;
  return t != null ? `D${{d}} (${{fmtTime(t)}})` : `D${{d}}`;
}}

function tierImprovements(block) {{
  return (tierMode === 'discounted' && block.improvements_discounted?.length)
    ? block.improvements_discounted
    : (block.improvements || []);
}}

function ratingToDataDecile(rating) {{
  return Math.min(10, Math.max(1, Math.ceil(Number(rating) / 10)));
}}

function meanImprovementByDataDecile(block) {{
  const means = {{}};
  const counts = {{}};
  tierImprovements(block).forEach(([rating, imp]) => {{
    const key = String(ratingToDataDecile(rating));
    means[key] = (means[key] || 0) + imp;
    counts[key] = (counts[key] || 0) + 1;
  }});
  Object.keys(means).forEach(k => {{
    means[k] = counts[k] ? means[k] / counts[k] : null;
  }});
  return means;
}}

function tierCellStyle(v, fromKey, toKey) {{
  if (v <= 0) {{
    return 'background:var(--surface2);color:var(--muted);font-weight:500;';
  }}

  const fd = Number(fromKey), td = Number(toKey);
  const alpha = Math.min(v * 3.2, 1);

  if (td === fd) {{
    return `background:rgba(100,116,139,${{0.18 + alpha * 0.55}});color:${{alpha >= 0.35 ? '#e2e8f0' : 'var(--text)'}};font-weight:600;`;
  }}
  if (td > fd) {{
    return `background:rgba(34,211,160,${{0.14 + alpha * 0.72}});color:${{alpha >= 0.35 ? '#0a0e1a' : 'var(--text)'}};font-weight:600;`;
  }}
  return `background:rgba(248,113,113,${{0.14 + alpha * 0.72}});color:${{alpha >= 0.35 ? '#0a0e1a' : 'var(--text)'}};font-weight:600;`;
}}

function tierImpStyle(v) {{
  if (v == null || isNaN(v)) return 'color:var(--muted);font-weight:600;';
  if (Math.abs(v) < 0.05) {{
    return 'background:rgba(100,116,139,0.25);color:var(--text);font-weight:700;';
  }}
  if (v > 0) {{
    const alpha = Math.min(Math.abs(v) / 8, 1);
    return `background:rgba(34,211,160,${{0.18 + alpha * 0.55}});color:${{alpha >= 0.4 ? '#0a0e1a' : 'var(--good)'}};font-weight:700;`;
  }}
  const alpha = Math.min(Math.abs(v) / 8, 1);
  return `background:rgba(248,113,113,${{0.18 + alpha * 0.55}});color:${{alpha >= 0.4 ? '#0a0e1a' : 'var(--bad)'}};font-weight:700;`;
}}

function renderTierMatrix() {{
  const event = document.getElementById('tier-event').value;
  const trans = document.getElementById('tier-trans-sel').value;
  const el    = document.getElementById('tier-matrix');
  const rt    = DATA.rating_transitions || {{}};
  const rtKey = firstEventKey(rt, event);
  const block = ((rt[rtKey] || {{}})[tierGender] || {{}})[trans];

  if (!block || !block.matrix) {{
    const hint = trans === 'FR_to_SR'
      ? 'No FR→SR data — re-run analyze_progression.py to generate career transitions.'
      : 'No transition matrix for this selection';
    el.innerHTML = `<div class="empty-state">${{hint}}</div>`;
    return;
  }}

  const matrix  = block.matrix;
  const deciles = Array.from({{ length: 10 }}, (_, i) => i + 1);
  const meanImp = meanImprovementByDataDecile(block);

  el.innerHTML = `
    <div class="tier-matrix-grid">
      <div class="tier-matrix-axis-top">Ending year</div>
      <div class="tier-matrix-axis-left">Starting year</div>
      <div class="tier-matrix-table">
        <table class="data-table tier-matrix-table" style="font-size:12px;">
          <thead><tr>
            <th style="min-width:24px;"></th>
            ${{deciles.map(d =>
              `<th style="text-align:center;min-width:72px;" title="Ending decile benchmark time">${{tierDecileLabel(event, tierGender, d)}}</th>`
            ).join('')}}
            <th style="text-align:center;min-width:88px;" title="Mean % time change for athletes starting in this decile">Avg Δ%</th>
          </tr></thead>
          <tbody>
            ${{deciles.map(fromD => {{
              const fromKey = String(fromD);
              const rowMean = meanImp[fromKey];
              return `<tr>
                <td style="font-weight:700;white-space:nowrap;">${{tierDecileLabel(event, tierGender, fromD)}}</td>
                ${{deciles.map(toD => {{
                  const toKey = String(toD);
                  const v = (matrix[fromKey] || {{}})[toKey] || 0;
                  return `<td style="text-align:center;border-radius:4px;${{tierCellStyle(v, fromKey, toKey)}}" title="P(to D${{toD}} | from D${{fromD}})">${{v > 0 ? (v * 100).toFixed(1) + '%' : '—'}}</td>`;
                }}).join('')}}
                <td style="text-align:center;border-radius:4px;${{tierImpStyle(rowMean)}}">${{rowMean != null ? fmtPct(rowMean) : '—'}}</td>
              </tr>`;
            }}).join('')}}
          </tbody>
        </table>
      </div>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--muted);">
      n = ${{block.n != null ? Number(block.n).toLocaleString() : '—'}} athletes
      · Decile times are national benchmarks for the selected event/gender
      · D1 = slowest 10%, D10 = fastest 10%
    </div>`;
}}

function tierPctile(arr, p) {{
  if (!arr || !arr.length) return null;
  const sorted = [...arr].sort((a, b) => a - b);
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  return lo === hi ? sorted[lo] : sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}}

function improvementsByDataDecile(block) {{
  const buckets = {{}};
  tierImprovements(block).forEach(([rating, imp]) => {{
    const key = String(ratingToDataDecile(rating));
    (buckets[key] = buckets[key] || []).push(imp);
  }});
  return buckets;
}}

function percentileStatsByDecile(block) {{
  const buckets = improvementsByDataDecile(block);
  const stats = {{}};
  for (let d = 1; d <= 10; d++) {{
    const arr = buckets[String(d)] || [];
    stats[String(d)] = {{
      p10:    tierPctile(arr, 0.10),
      p25:    tierPctile(arr, 0.25),
      median: tierPctile(arr, 0.50),
      p75:    tierPctile(arr, 0.75),
      p90:    tierPctile(arr, 0.90),
    }};
  }}
  return stats;
}}

function renderTierDecileProgression() {{
  const event = document.getElementById('tier-event').value;
  const trans = document.getElementById('tier-trans-sel').value;
  const rt    = DATA.rating_transitions || {{}};
  const rtKey = firstEventKey(rt, event);
  const block = ((rt[rtKey] || {{}})[tierGender] || {{}})[trans];

  if (!block || !tierImprovements(block).length) {{
    mkChart('chart-tier-decile-prog', {{
      type: 'line',
      data: {{ labels: ['No data'], datasets: [{{ data: [null] }}] }},
      options: {{ ...baseOpts(false), plugins: {{ legend: {{ display: false }}, title: {{
        display: true,
        text: 'No decile progression data for this selection',
        color: tickColor, font: baseFont
      }} }} }}
    }});
    return;
  }}

  const deciles = Array.from({{ length: 10 }}, (_, i) => i + 1);
  const labels  = deciles.map(d => tierDecileLabel(event, tierGender, d));
  const stats   = percentileStatsByDecile(block);

  const series = [
    {{ key: 'p10',    label: 'P10',    border: '#f87171', fill: 'rgba(248,113,113,0.08)' }},
    {{ key: 'p25',    label: 'P25',    border: '#fbbf24', fill: 'rgba(251,191,36,0.08)' }},
    {{ key: 'median', label: 'Median', border: '#00c8ff', fill: 'rgba(0,200,255,0.12)' }},
    {{ key: 'p75',    label: 'P75',    border: '#7fff6e', fill: 'rgba(127,255,110,0.08)' }},
    {{ key: 'p90',    label: 'P90 (top 10% improvers)', border: '#a78bfa', fill: 'rgba(167,139,250,0.08)' }},
  ];

  const datasets = series.map(s => ({{
    label: s.label,
    data: deciles.map(d => {{
      const v = stats[String(d)][s.key];
      return v != null ? +Number(v).toFixed(2) : null;
    }}),
    borderColor: s.border,
    backgroundColor: s.fill,
    tension: 0.35,
    fill: false,
    pointRadius: 4,
    pointBackgroundColor: s.border,
    spanGaps: true
  }}));

  mkChart('chart-tier-decile-prog', {{
    type: 'line',
    data: {{ labels, datasets }},
    options: {{
      ...baseOpts(true),
      plugins: {{
        ...baseOpts(true).plugins,
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.raw != null ? (ctx.raw > 0 ? '+' : '') + ctx.raw.toFixed(2) : '—'}}%`
          }}
        }}
      }},
      scales: {{
        x: {{ grid: {{ color: gridColor }}, ticks: {{ color: tickColor, font: baseFont, maxRotation: 45, minRotation: 45 }} }},
        y: {{
          grid: {{ color: gridColor }},
          ticks: {{ color: tickColor, font: baseFont, callback: v => v + '%' }}
        }}
      }}
    }}
  }});
}}

// ══════════════════════════════════════════════════════════════════════════════
//  EVALUATOR
// ══════════════════════════════════════════════════════════════════════════════
function setEvalGender(g) {{
  evalGender = g;
  document.getElementById('eval-m-btn').classList.toggle('active', g === 'M');
  document.getElementById('eval-f-btn').classList.toggle('active', g === 'F');
  renderEvaluator();
}}

function setEvalMode(mode) {{
  evalMode = mode;
  document.getElementById('eval-raw-btn').classList.toggle('active', mode === 'raw');
  document.getElementById('eval-disc-btn').classList.toggle('active', mode === 'discounted');
  renderEvaluator();
}}

function parseTimeInput(raw) {{
  const s = (raw || '').trim();
  if (!s) return null;
  if (s.includes(':')) {{
    const parts = s.split(':');
    if (parts.length < 2) return null;
    const mins = parseFloat(parts[0]);
    const secs = parseFloat(parts[1]);
    if (isNaN(mins) || isNaN(secs) || mins < 0 || secs < 0) return null;
    return mins * 60 + secs;
  }}
  const v = parseFloat(s);
  return (isNaN(v) || v <= 0) ? null : v;
}}

function timeToPercentile(event, gender, timeSec) {{
  const tables = DATA.percentiles || {{}};
  const code = firstEventKey(tables, event);
  const v = tables[code]?.[gender];
  if (!v || timeSec == null) return null;

  const anchors = [
    [5, v.p5], [10, v.p10], [25, v.p25], [50, v.p50],
    [75, v.p75], [90, v.p90], [95, v.p95]
  ];

  if (timeSec >= anchors[0][1]) return Math.max(0, 5 - (timeSec - anchors[0][1]) / anchors[0][1] * 5);
  if (timeSec <= anchors[anchors.length - 1][1]) return Math.min(100, 95 + (anchors[anchors.length - 1][1] - timeSec) / anchors[anchors.length - 1][1] * 5);

  for (let i = 0; i < anchors.length - 1; i++) {{
    const [p0, t0] = anchors[i], [p1, t1] = anchors[i + 1];
    if (timeSec <= t0 && timeSec >= t1) {{
      return p0 + (p1 - p0) * (t0 - timeSec) / (t0 - t1);
    }}
  }}
  return null;
}}

function fmtNationalPct(pct) {{
  if (pct == null || isNaN(pct)) return '—';
  if (pct >= 95) return '95+';
  return pct.toFixed(1) + '%';
}}

function pctToDecile(pct) {{
  if (pct == null || isNaN(pct)) return null;
  return Math.min(10, Math.max(1, Math.ceil(pct / 10)));
}}

function evalTransitionRow(event, gender, fromClass, toClass) {{
  return rowsForEvent(DATA.progression, event, gender)
    .find(r => r.from_class === fromClass && r.to_class === toClass);
}}

function evalTransitionBlock(event, gender, fromClass, toClass) {{
  const rt    = DATA.rating_transitions || {{}};
  const rtKey = firstEventKey(rt, event);
  const tk    = fromClass + '_to_' + toClass;
  return ((rt[rtKey] || {{}})[gender] || {{}})[tk] || null;
}}

function evalImprovements(block) {{
  if (!block) return [];
  return (evalMode === 'discounted' && block.improvements_discounted?.length)
    ? block.improvements_discounted
    : (block.improvements || []);
}}

function evalPercentileStatsByDecile(block) {{
  const buckets = {{}};
  evalImprovements(block).forEach(([rating, imp]) => {{
    const key = String(ratingToDataDecile(rating));
    (buckets[key] = buckets[key] || []).push(imp);
  }});
  const stats = {{}};
  for (let d = 1; d <= 10; d++) {{
    const arr = buckets[String(d)] || [];
    stats[String(d)] = {{
      n:      arr.length,
      p10:    tierPctile(arr, 0.10),
      p25:    tierPctile(arr, 0.25),
      median: tierPctile(arr, 0.50),
      p75:    tierPctile(arr, 0.75),
      p90:    tierPctile(arr, 0.90),
      mean:   arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : null,
    }};
  }}
  return stats;
}}

function evalDecileRate(event, gender, fromClass, toClass, decile, rateKey) {{
  const block = evalTransitionBlock(event, gender, fromClass, toClass);
  const row   = evalTransitionRow(event, gender, fromClass, toClass);
  if (decile == null) return evalRate(row, rateKey);

  if (block) {{
    const stats = evalPercentileStatsByDecile(block)[String(decile)];
    if (stats && stats.n >= 5 && stats[rateKey] != null) {{
      return Number(stats[rateKey]);
    }}
  }}
  return evalRate(row, rateKey);
}}

function evalDecileN(event, gender, fromClass, toClass, decile) {{
  const block = evalTransitionBlock(event, gender, fromClass, toClass);
  if (!block || decile == null) {{
    const row = evalTransitionRow(event, gender, fromClass, toClass);
    return row?.n ?? null;
  }}
  const stats = evalPercentileStatsByDecile(block)[String(decile)];
  return stats?.n ? stats.n : (evalTransitionRow(event, gender, fromClass, toClass)?.n ?? null);
}}

function evalRate(row, rateKey) {{
  if (!row) return null;
  if (evalMode === 'discounted') {{
    const dk = rateKey === 'mean' || rateKey === 'median'
      ? 'discounted_' + rateKey
      : 'discounted_' + rateKey;
    const v = row[dk];
    return v != null ? Number(v) : (row[rateKey] != null ? Number(row[rateKey]) : null);
  }}
  return row[rateKey] != null ? Number(row[rateKey]) : null;
}}

function applyImpPct(timeSec, impPct) {{
  if (timeSec == null || impPct == null || isNaN(impPct)) return null;
  return timeSec * (1 - impPct / 100);
}}

function evalCareerSteps(startTime, startClass, event, gender, rateKey) {{
  const path = EVAL_CAREER_PATH[startClass] || [];
  const steps = [];
  let t = startTime;
  let from = startClass;
  let decile = pctToDecile(timeToPercentile(event, gender, startTime));

  for (const to of path) {{
    const imp = evalDecileRate(event, gender, from, to, decile, rateKey);
    if (imp == null) break;
    t = applyImpPct(t, imp);
    if (t == null) break;
    steps.push({{ from, to, time: t, impPct: imp, decile }});
    from = to;
    decile = pctToDecile(timeToPercentile(event, gender, t));
  }}
  return steps;
}}

function evalTotalImpPct(startTime, endTime) {{
  if (!startTime || !endTime) return null;
  return (startTime - endTime) / startTime * 100;
}}

function renderEvaluator() {{
  const el = document.getElementById('eval-content');
  if (!el) return;

  const event = document.getElementById('eval-event')?.value;
  const startClass = document.getElementById('eval-class')?.value || 'FR';
  const timeSec = parseTimeInput(document.getElementById('eval-time')?.value);

  if (!event || timeSec == null) {{
    el.innerHTML = '<div class="empty-state">Enter a valid time (MM:SS.ss or seconds) to see projections.</div>';
    return;
  }}

  const startPct = timeToPercentile(event, evalGender, timeSec);
  const startDecile = pctToDecile(startPct);
  const nextClass = EVAL_CLASS_NEXT[startClass];
  const nextRow = nextClass ? evalTransitionRow(event, evalGender, startClass, nextClass) : null;
  const nextDecileN = nextClass
    ? evalDecileN(event, evalGender, startClass, nextClass, startDecile)
    : null;
  const careerCols = EVAL_CAREER_PATH[startClass] || [];

  const snapPct = fmtNationalPct(startPct);
  const snapDecileTime = startDecile != null
    ? tierDecileLabel(event, evalGender, startDecile)
    : null;

  let nextTable = '';
  if (!nextClass || !nextRow) {{
    nextTable = '<div class="empty-state">No historical transition data for the next class year.</div>';
  }} else {{
    nextTable = `
      <table class="data-table">
        <thead><tr>
          <th>Scenario</th>
          <th>D${{startDecile}} rate</th>
          <th>${{nextClass}} time</th>
          <th>1-yr Δ%</th>
          <th>Est. national pct</th>
        </tr></thead>
        <tbody>
          ${{EVAL_SCENARIOS.map(sc => {{
            const imp = evalDecileRate(event, evalGender, startClass, nextClass, startDecile, sc.id);
            const proj = applyImpPct(timeSec, imp);
            const pct = proj != null ? timeToPercentile(event, evalGender, proj) : null;
            const impColor = imp == null ? 'var(--muted)' : imp >= 0 ? 'var(--good)' : 'var(--bad)';
            return `<tr>
              <td><strong>${{sc.label}}</strong><div style="font-size:11px;color:var(--muted);">${{sc.note}}</div></td>
              <td class="num" style="color:${{impColor}}">${{imp != null ? fmtPct(imp) : '—'}}</td>
              <td class="num">${{proj != null ? fmtTime(proj) : '—'}}</td>
              <td class="num" style="color:${{impColor}}">${{imp != null ? fmtPct(imp) : '—'}}</td>
              <td class="num">${{fmtNationalPct(pct)}}</td>
            </tr>`;
          }}).join('')}}
        </tbody>
      </table>
      <div style="margin-top:8px;font-size:11px;color:var(--muted);">
        Rates for athletes starting in decile D${{startDecile}} on ${{startClass}}→${{nextClass}}
        · n=${{nextDecileN != null ? Number(nextDecileN).toLocaleString() : '—'}} in this decile bucket
        · ${{evalMode === 'discounted' ? 'field-adjusted' : 'raw'}} improvement %
      </div>`;
  }}

  let careerTable = '';
  if (!careerCols.length) {{
    careerTable = '<div class="empty-state">No further class transitions available from this year.</div>';
  }} else {{
    careerTable = `
      <table class="data-table">
        <thead><tr>
          <th>Scenario</th>
          ${{careerCols.map(c => `<th>${{c}} time</th>`).join('')}}
          <th>Career Δ%</th>
          <th>Final est. pct</th>
        </tr></thead>
        <tbody>
          ${{EVAL_SCENARIOS.map(sc => {{
            const steps = evalCareerSteps(timeSec, startClass, event, evalGender, sc.id);
            const finalTime = steps.length ? steps[steps.length - 1].time : null;
            const totalImp = evalTotalImpPct(timeSec, finalTime);
            const finalPct = finalTime != null ? timeToPercentile(event, evalGender, finalTime) : null;
            const totalColor = totalImp == null ? 'var(--muted)' : totalImp >= 0 ? 'var(--good)' : 'var(--bad)';
            return `<tr>
              <td><strong>${{sc.label}}</strong><div style="font-size:11px;color:var(--muted);">${{sc.note}}</div></td>
              ${{careerCols.map(c => {{
                const step = steps.find(s => s.to === c);
                return `<td class="num">${{step ? fmtTime(step.time) : '—'}}</td>`;
              }}).join('')}}
              <td class="num" style="color:${{totalColor}}">${{totalImp != null ? fmtPct(totalImp) : '—'}}</td>
              <td class="num">${{fmtNationalPct(finalPct)}}</td>
            </tr>`;
          }}).join('')}}
        </tbody>
      </table>
      <div style="margin-top:8px;font-size:11px;color:var(--muted);">
        Career path: ${{startClass}}→${{careerCols.join('→')}} · decile-specific rates per step (re-estimated after each year)
        · ${{evalMode === 'discounted' ? 'field-adjusted' : 'raw'}} improvement %
      </div>`;
  }}

  el.innerHTML = `
    <div class="grid-4" style="margin-bottom:24px;">
      <div class="eval-snap">
        <div class="eval-snap-label">Your time (${{startClass}})</div>
        <div class="eval-snap-value" style="color:var(--accent);">${{fmtTime(timeSec)}}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px;">${{EVENT_LABELS[event] || event}} · ${{evalGender === 'M' ? 'Men' : 'Women'}}</div>
      </div>
      <div class="eval-snap">
        <div class="eval-snap-label">Est. national percentile</div>
        <div class="eval-snap-value">${{snapPct}}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px;">vs. all-season NCAA benchmark</div>
      </div>
      <div class="eval-snap">
        <div class="eval-snap-label">Est. decile</div>
        <div class="eval-snap-value" style="font-size:22px;">${{startDecile != null ? 'D' + startDecile : '—'}}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px;">${{snapDecileTime || 'D1=slowest · D10=fastest'}}</div>
      </div>
      <div class="eval-snap">
        <div class="eval-snap-label">Next transition</div>
        <div class="eval-snap-value" style="font-size:22px;">${{startClass}}→${{nextClass || '—'}}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px;">${{nextRow ? 'D' + startDecile + ' n=' + (nextDecileN != null ? Number(nextDecileN).toLocaleString() : '—') : 'No data'}}</div>
      </div>
    </div>

    <div class="card" style="margin-bottom:24px;">
      <div class="card-title">Next Season — ${{startClass}} to ${{nextClass || '?'}}</div>
      ${{nextTable}}
    </div>

    <div class="card">
      <div class="card-title">Career Path — ${{startClass}} through ${{careerCols[careerCols.length - 1] || startClass}}</div>
      ${{careerTable}}
    </div>`;
}}

// ══════════════════════════════════════════════════════════════════════════════
//  VOLUME
// ══════════════════════════════════════════════════════════════════════════════
function renderVolume() {{
  const vol = DATA.season_volume || [];
  if (!vol.length) return;
  const labels = vol.map(r => r.year);

  mkChart('chart-vol-athletes', {{
    type: 'bar',
    data: {{
      labels,
      datasets: [{{
        label: 'Unique Athletes',
        data: vol.map(r => r.athletes),
        backgroundColor: 'rgba(0,200,255,0.6)', borderColor: '#00c8ff',
        borderWidth: 1, borderRadius: 4
      }}]
    }},
    options: baseOpts(false)
  }});

  mkChart('chart-vol-results', {{
    type: 'bar',
    data: {{
      labels,
      datasets: [{{
        label: 'Total Results',
        data: vol.map(r => r.results),
        backgroundColor: 'rgba(255,107,53,0.6)', borderColor: '#ff6b35',
        borderWidth: 1, borderRadius: 4
      }}]
    }},
    options: baseOpts(false)
  }});
}}

// ── Boot ──────────────────────────────────────────────────────────────────────
initOverview();
initBreakoutSlider();
</script>
</body>
</html>"""


def main():
    print("Loading data from database...")
    data = load_data()

    print(f"  Athletes:  {data['summary']['athletes']:,}")
    print(f"  Results:   {data['summary']['results']:,}")
    print(f"  Schools:   {data['summary']['schools']:,}")

    print("Generating dashboard HTML...")
    html = generate_html(data)

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    size_kb = OUTPUT_PATH.stat().st_size // 1024
    print(f"Dashboard written to: {OUTPUT_PATH}  ({size_kb} KB)")
    print("Opening in browser...")
    try:
        webbrowser.open(OUTPUT_PATH.as_uri())
    except Exception:
        print(f"Open manually: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()