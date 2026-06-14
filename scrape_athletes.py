"""
scrape_athletes.py - Downloads TFRRS athlete profile pages with:
  * concurrent fetching (ThreadPoolExecutor)
  * exponential-backoff retry
  * local HTML cache (skip existing files)
  * checkpoint / resume via scrape_queue table
  * progress bar (tqdm)
  * per-worker rate limiting
"""

import logging
import re
import time
import threading
import hashlib
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm

from database import get_connection, init_db, mark_url, DB_PATH

logger = logging.getLogger(__name__)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.tfrrs.org"
RAW_HTML_DIR = Path(__file__).parent / "raw_html" / "athletes"
LOG_DIR = Path(__file__).parent / "logs"

# ── thread-local session so each worker has its own TCP pool ──────────────────
_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; TFRRS-NCAA-Research-Bot/1.0; "
                "+https://github.com/research/tfrrs-pipeline)"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        _local.session = s
    return _local.session


# ── per-thread rate limiter ───────────────────────────────────────────────────
_rate_lock = threading.Lock()
_last_request_time: dict[int, float] = {}   # thread_id → timestamp


def _rate_limit(min_interval: float = 3.0) -> None:
    tid = threading.get_ident()
    with _rate_lock:
        last = _last_request_time.get(tid, 0)
        delta = time.monotonic() - last
        if delta < min_interval:
            time.sleep(min_interval - delta)
        _last_request_time[tid] = time.monotonic()


# ── URL helpers ───────────────────────────────────────────────────────────────
ATHLETE_ID_RE = re.compile(r'/athletes/(\d+)', re.I)

def extract_athlete_id(url: str) -> Optional[str]:
    m = ATHLETE_ID_RE.search(url)
    return m.group(1) if m else None


def url_to_cache_path(url: str) -> Path:
    """Stable file path for a URL: raw_html/athletes/<id>.html or <md5>.html"""
    aid = extract_athlete_id(url)
    if aid:
        return RAW_HTML_DIR / f"{aid}.html"
    h = hashlib.md5(url.encode()).hexdigest()
    return RAW_HTML_DIR / f"unk_{h}.html"


# ── core fetch ────────────────────────────────────────────────────────────────
def fetch_athlete_page(url: str,
                       max_attempts: int = 5,
                       base_backoff: float = 2.0) -> Optional[str]:
    """
    Download one athlete page.  Returns HTML string or None on permanent failure.
    Implements exponential backoff for 429 / 5xx.
    """
    cache_path = url_to_cache_path(url)
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    session = _get_session()

    for attempt in range(1, max_attempts + 1):
        time.sleep(random.uniform(2.0, 3.0))
        try:
            resp = session.get(url, timeout=25)
        except requests.RequestException as exc:
            wait = base_backoff ** attempt
            logger.debug("RequestException attempt %d: %s | sleep %.1fs", attempt, exc, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            html = resp.text
            cache_path.write_text(html, encoding="utf-8")
            return html

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", base_backoff ** attempt))
            logger.warning("429 on %s — sleeping %.1fs", url, retry_after)
            time.sleep(retry_after)
            continue

        if resp.status_code in (301, 302, 303, 307, 308):
            redir = resp.headers.get("Location", "")
            if redir:
                url = redir if redir.startswith("http") else BASE_URL + redir
            continue

        if resp.status_code == 404:
            logger.debug("404 for %s — skipping", url)
            return None

        if resp.status_code >= 500:
            wait = base_backoff * attempt
            logger.warning("HTTP %d on %s — retry in %.1fs", resp.status_code, url, wait)
            time.sleep(wait)
            continue

        logger.error("Unhandled HTTP %d for %s", resp.status_code, url)
        return None

    logger.error("Exhausted retries for %s", url)
    return None


# ── worker ────────────────────────────────────────────────────────────────────
def _worker_fetch(url: str) -> tuple[str, bool, Optional[str]]:
    """Returns (url, success, error_message)."""
    try:
        html = fetch_athlete_page(url)
        if html:
            return url, True, None
        return url, False, "no content"
    except Exception as exc:
        return url, False, str(exc)


# ── batch downloader ──────────────────────────────────────────────────────────
def download_all_athletes(
    max_workers: int = 6,
    batch_size: int = 500,
    db_path = DB_PATH,
) -> dict:
    """
    Reads pending athlete URLs from scrape_queue and downloads them
    concurrently.  Updates queue status after each batch.

    Returns summary dict { fetched, failed, skipped }.
    """
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"fetched": 0, "failed": 0, "skipped": 0}

    while True:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT url FROM scrape_queue
                   WHERE entity_type='athlete' AND status='pending' AND attempts<5
                   ORDER BY created_at
                   LIMIT ?""",
                (batch_size,),
            ).fetchall()

        urls = [r["url"] for r in rows]
        if not urls:
            logger.info("No more pending athlete URLs.")
            break

        logger.info("Downloading batch of %d athlete pages (workers=%d)",
                    len(urls), max_workers)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_worker_fetch, u): u for u in urls}
            with tqdm(total=len(futures), desc="Fetching athletes", unit="pg") as pbar:
                for future in as_completed(futures):
                    url, ok, err = future.result()
                    with get_connection(db_path) as conn:
                        if ok:
                            mark_url(conn, url, "fetched")
                            stats["fetched"] += 1
                        else:
                            mark_url(conn, url, "error", error=err)
                            stats["failed"] += 1
                    pbar.update(1)
                    pbar.set_postfix(
                        ok=stats["fetched"], fail=stats["failed"]
                    )

        logger.info("Batch done — fetched=%d failed=%d",
                    stats["fetched"], stats["failed"])

    logger.info("Download complete: %s", stats)
    return stats


# ── ad-hoc single-URL download ────────────────────────────────────────────────
def download_athlete(athlete_id: str) -> Optional[str]:
    """Convenience wrapper to fetch one athlete by numeric ID."""
    url = f"{BASE_URL}/athletes/{athlete_id}.html"
    return fetch_athlete_page(url)


# ── queue population helper ───────────────────────────────────────────────────
def queue_athletes_from_url_list(urls: list[str]) -> None:
    """Add a list of athlete URLs directly to the queue (for testing)."""
    from database import queue_urls
    with get_connection() as conn:
        queue_urls(conn, urls, "athlete")
    logger.info("Queued %d athlete URLs", len(urls))


if __name__ == "__main__":
    import sys

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "scrape_athletes.log"),
        ],
    )

    init_db()
    stats = download_all_athletes(max_workers=8, batch_size=1000)
    print(f"\n✓ Done: {stats}")
