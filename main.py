"""
main.py - Orchestrates the full TFRRS NCAA scraping -> analysis pipeline.

Usage:
  python main.py init
  python main.py discover              # find all schools
  python main.py scrape-rosters        # get athlete URLs from team pages
  python main.py download              # fetch athlete HTML
  python main.py parse                 # parse HTML into DB
  python main.py analyze               # compute development statistics
  python main.py all                   # run full pipeline end-to-end
  python main.py demo                  # generate synthetic demo data + analyze
  python main.py add-historical --year 2019   # download one historical season by year
  python main.py add-historical --hnd 138     # or by raw config_hnd code
"""

import argparse
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

# ── Windows UTF-8 fix (must happen before logging is configured) ──────────────
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "main.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ── Historical season lookup: year -> config_hnd ──────────────────────────────
HISTORICAL_SEASONS = {
    2024: "348",
    2023: "303",
    2022: "264",
    2021: "222",
    2020: "195",
    2019: "167",
    2018: "138",
    2017: "115",
    2016: "96",
    2015: "78",
    2014: "60",
    2013: "43",
    2012: "25",
}

# ── Pipeline steps ─────────────────────────────────────────────────────────────

def step_init():
    from database import init_db
    init_db()
    logger.info("[OK] Database initialised")


def step_discover():
    from scrape_teams import discover_schools, save_schools_to_db
    schools = discover_schools()
    save_schools_to_db(schools)
    logger.info("[OK] Discovered %d schools", len(schools))


def step_scrape_rosters():
    from scrape_teams import scrape_rosters
    n = scrape_rosters()
    logger.info("[OK] Queued %d athlete URLs", n)


def step_download(workers: int = 8, batch: int = 1000):
    from scrape_athletes import download_all_athletes
    stats = download_all_athletes(max_workers=workers, batch_size=batch)
    logger.info("[OK] Download stats: %s", stats)


def step_parse():
    from parse_athletes import parse_all_cached_athletes
    stats = parse_all_cached_athletes()
    logger.info("[OK] Parse stats: %s", stats)


def step_analyze():
    from analyze_progression import run_analysis
    summary = run_analysis()
    logger.info("[OK] Analysis summary:\n%s", json.dumps(summary, indent=2))
    return summary


def step_add_historical(hnd: str, year: int = None, workers: int = 2, batch: int = 200):
    """
    Full self-contained flow for adding one historical season:
      1. Ensures DB + schools exist (safe to re-run on existing DB)
      2. Queues historical roster URLs using config_hnd=<hnd>
      3. Scrapes those rosters to discover athlete URLs (network only for new pages)
      4. Downloads new athlete HTML (skips anything already cached locally)
    """
    from database import get_connection

    label = f"{year} (hnd={hnd})" if year else f"hnd={hnd}"

    # Step 1 — ensure DB and schools are present
    logger.info("[add-historical] Initialising DB and schools (safe no-op if already done)")
    step_init()
    step_discover()

    # Step 2 — queue historical roster URLs derived from current team URLs
    logger.info("[add-historical] Queuing historical roster URLs for %s", label)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT url FROM scrape_queue WHERE entity_type='team' AND status IN ('pending','fetched')"
        ).fetchall()

        if not rows:
            logger.error(
                "No team URLs found in scrape_queue. "
                "Run 'python main.py discover' first."
            )
            return

        base_urls = [r["url"] for r in rows if "?" not in r["url"]]
        historical_urls = [u + f"?config_hnd={hnd}" for u in base_urls]
        conn.executemany(
            "INSERT OR IGNORE INTO scrape_queue(url, entity_type, status) VALUES(?,?,?)",
            [(u, "team", "pending") for u in historical_urls],
        )
        logger.info("[add-historical] Queued %d historical roster URLs for %s",
                    len(historical_urls), label)

    # Step 3 — scrape ONLY this season's roster pages by passing the hnd filter
    logger.info("[add-historical] Scraping historical rosters for %s", label)
    from scrape_teams import scrape_rosters
    scrape_rosters(hnd_filter=hnd)

    # Step 4 — download new athlete pages (already-cached athletes are skipped)
    logger.info("[add-historical] Downloading new athletes (workers=%d, batch=%d)",
                workers, batch)
    step_download(workers=workers, batch=batch)

    logger.info("[add-historical] Done for %s. Run 'python main.py parse' and "
                "'python main.py analyze' whenever you are ready.", label)


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TFRRS NCAA Distance Running Pipeline")
    p.add_argument("command",
                   choices=["init", "discover", "scrape-rosters", "download",
                            "parse", "analyze", "all", "demo", "add-historical"])
    p.add_argument("--year", type=int, default=None,
                   help=("Historical season year for add-historical "
                         f"(supported: {', '.join(str(y) for y in sorted(HISTORICAL_SEASONS))})"))
    p.add_argument("--hnd", type=str, default=None,
                   help="Raw TFRRS config_hnd code (alternative to --year)")
    p.add_argument("--workers",  type=int, default=8)
    p.add_argument("--batch",    type=int, default=1000)
    p.add_argument("--athletes", type=int, default=2000)
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    t0 = time.time()

    if args.command == "init":
        step_init()
    elif args.command == "discover":
        step_init()
        step_discover()
    elif args.command == "scrape-rosters":
        step_scrape_rosters()
    elif args.command == "download":
        step_download(args.workers, args.batch)
    elif args.command == "parse":
        step_parse()
    elif args.command == "analyze":
        step_analyze()
    elif args.command == "all":
        step_init()
        step_discover()
        step_scrape_rosters()
        step_download(args.workers, args.batch)
        step_parse()
        step_analyze()
    elif args.command == "add-historical":
        # Resolve hnd from --year or --hnd
        hnd = None
        year = None
        if args.year:
            year = args.year
            hnd = HISTORICAL_SEASONS.get(year)
            if not hnd:
                valid = ", ".join(str(y) for y in sorted(HISTORICAL_SEASONS))
                logger.error("Unknown year %d. Supported years: %s", year, valid)
                sys.exit(1)
        elif args.hnd:
            hnd = args.hnd
            # Reverse lookup year for logging
            year = next((y for y, h in HISTORICAL_SEASONS.items() if h == hnd), None)
        else:
            logger.error("add-historical requires --year <year> or --hnd <code>  "
                         "e.g. --year 2019  or  --hnd 138")
            sys.exit(1)

        step_add_historical(hnd=hnd, year=year, workers=2, batch=args.batch)

    logger.info("Done in %.1f s", time.time() - t0)


if __name__ == "__main__":
    main()