"""
scrape_teams.py - Discovers all NCAA D1 Men's schools using only the
9 confirmed NCAA DI Regional XC Championship results pages (2025).

These are the ONLY meet URLs used - all 9 were verified from live search
results. No guessed IDs. Conference meet IDs removed entirely.
"""

import logging
import re
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from database import get_connection, init_db, upsert_school, queue_urls, DB_PATH

logger = logging.getLogger(__name__)
BASE_URL = "https://www.tfrrs.org"

# These 9 URLs were confirmed in live search results June 2026.
# Every DI men's team that qualified for regionals appears here.
DI_REGIONAL_MEETS = {
    "Midwest":      "/results/xc/27293/NCAA_Division_I_Midwest_Region_Cross_Country_Championships",
    "Mountain":     "/results/xc/27023/NCAA_Division_I_Mountain_Region_Cross_Country_Championships",
    "Northeast":    "/results/xc/27024/NCAA_Division_I_Northeast_Region_Cross_Country_Championships",
    "Southeast":    "/results/xc/27027/NCAA_Division_I_Southeast_Region_Cross_Country_Championships",
    "West":         "/results/xc/27028/NCAA_Division_I_West_Region_Cross_Country_Championships",
    "Great_Lakes":  "/results/xc/27020/NCAA_Division_I_Great_Lakes_Region_Cross_Country_Championships",
    "Mid_Atlantic": "/results/xc/27021/NCAA_Division_I_Mid_Atlantic_Region_Cross_Country_Championships",
    "South_Central":"/results/xc/27025/NCAA_Division_I_South_Central_Region_Cross_Country_Championships",
    "South":        "/results/xc/27294/NCAA_Division_I_South_Region_Cross_Country_Championships",
}
    


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


def _get_with_retry(url: str, max_attempts: int = 4, backoff: float = 2.0):
    for attempt in range(1, max_attempts + 1):
        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                time.sleep(backoff ** attempt)
            elif resp.status_code == 404:
                logger.error("404 for %s", url)
                return None
            elif resp.status_code >= 500:
                time.sleep(backoff * attempt)
            else:
                logger.error("HTTP %d for %s", resp.status_code, url)
                return None
        except requests.RequestException as exc:
            logger.warning("Request error attempt %d: %s", attempt, exc)
            time.sleep(backoff * attempt)
    return None


def _extract_mens_team_urls(html: str) -> list[str]:
    """Extract only men's team URLs - must contain /teams/ and _m_ in the path."""
    soup = BeautifulSoup(html, "lxml")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/teams/" in href and "_m_" in href and href not in seen:
            seen.add(href)
            full = BASE_URL + href if href.startswith("/") else href
            urls.append(full)
    return urls


def _school_name_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1].replace(".html", "")
    parts = slug.split("_")
    try:
        idx = next(i for i, p in enumerate(parts)
                   if p in ("college", "jcollege", "univ")) + 2
    except StopIteration:
        idx = 2
    name = " ".join(parts[idx:])
    return name.title() if name else slug


def discover_schools(cache_dir: Path = None) -> list[dict]:
    """Scrape all 9 regional championship pages to get every DI men's team."""
    if cache_dir is None:
        cache_dir = Path(__file__).parent / "raw_html" / "listings"
    cache_dir.mkdir(parents=True, exist_ok=True)

    all_schools: dict[str, dict] = {}

    for region_name, path in DI_REGIONAL_MEETS.items():
        url = BASE_URL + path
        cache_file = cache_dir / f"regional_{region_name}.html"

        if cache_file.exists():
            logger.info("Cached: %s", region_name)
            html = cache_file.read_text(encoding="utf-8", errors="replace")
        else:
            logger.info("Fetching: %-20s %s", region_name, url)
            resp = _get_with_retry(url)
            if resp is None:
                logger.error("Could not fetch %s - skipping", region_name)
                continue
            html = resp.text
            cache_file.write_text(html, encoding="utf-8")
            time.sleep(0.8)

        team_urls = _extract_mens_team_urls(html)
        new = sum(1 for u in team_urls if u not in all_schools)
        for team_url in team_urls:
            if team_url not in all_schools:
                all_schools[team_url] = {
                    "name":       _school_name_from_url(team_url),
                    "roster_url": team_url,
                    "division":   "I",
                    "tfrrs_id":   team_url.split("/")[-1].replace(".html", ""),
                }
        logger.info("  %-20s -> %d teams (%d new, %d total)",
                    region_name, len(team_urls), new, len(all_schools))

    result = list(all_schools.values())
    logger.info("Total unique D1 men's XC teams: %d", len(result))
    return result


def save_schools_to_db(schools: list[dict]) -> None:
    with get_connection() as conn:
        roster_urls = []
        for s in schools:
            upsert_school(conn, name=s["name"], division=s["division"],
                          tfrrs_id=s.get("tfrrs_id"))
            roster_urls.append(s["roster_url"])
        queue_urls(conn, roster_urls, "team")
    logger.info("Saved %d schools, queued %d roster URLs", len(schools), len(roster_urls))


def _parse_athlete_links_from_roster(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    athlete_urls = []
    seen = set()
    pattern = re.compile(r'/athletes/\d+', re.I)
    for a in soup.select("a[href]"):
        href = a["href"]
        if pattern.search(href) and href not in seen:
            seen.add(href)
            full = BASE_URL + href if href.startswith("/") else href
            athlete_urls.append(full)
    return athlete_urls


def scrape_rosters(cache_dir: Path = None, delay: float = 1.0,
                   hnd_filter: str = None) -> int:
    """
    Scrape pending team roster pages.
    If hnd_filter is given, only process URLs containing that config_hnd value,
    preventing stale historical URLs from bleeding into the wrong season's run.
    """
    if cache_dir is None:
        cache_dir = Path(__file__).parent / "raw_html" / "rosters"
    cache_dir.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        if hnd_filter:
            rows = conn.execute(
                "SELECT url FROM scrape_queue WHERE entity_type='team' "
                "AND status='pending' AND attempts<5 "
                "AND url LIKE ?",
                (f"%config_hnd={hnd_filter}",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT url FROM scrape_queue WHERE entity_type='team' "
                "AND status='pending' AND attempts<5"
            ).fetchall()
    team_urls = [r["url"] for r in rows]
    logger.info("Processing %d pending roster pages", len(team_urls))

    import urllib.parse as _urlparse
    _SEASON_SUFFIXES = {
        "348": "_2024", "303": "_2023", "264": "_2022", "222": "_2021",
        "195": "_2020", "167": "_2019", "138": "_2018", "115": "_2017",
        "96": "_2016", "78": "_2015", "60": "_2014", "43": "_2013", "25": "_2012",
    }

    total_queued = 0
    for url in team_urls:
        # Parse URL properly so query string never bleeds into the filename
        _parsed = _urlparse.urlparse(url)
        _qs     = _urlparse.parse_qs(_parsed.query)
        _hnd    = _qs.get("config_hnd", [None])[0]
        # slug = bare filename without .html, guaranteed no query string
        slug    = _parsed.path.rstrip("/").split("/")[-1].replace(".html", "")
        _suffix = _SEASON_SUFFIXES.get(_hnd, f"_{_hnd}") if _hnd else ""
        cache_file_name = slug + _suffix + ".html"
        cache_file = cache_dir / cache_file_name

        if cache_file.exists():
            html = cache_file.read_text(encoding="utf-8", errors="replace")
        else:
            resp = _get_with_retry(url)
            if resp is None:
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE scrape_queue SET status='error', "
                        "attempts=attempts+1 WHERE url=?", (url,)
                    )
                continue
            html = resp.text
            cache_file.write_text(html, encoding="utf-8")
            time.sleep(delay)

        athlete_urls = _parse_athlete_links_from_roster(html)
        with get_connection() as conn:
            queue_urls(conn, athlete_urls, "athlete")
            conn.execute(
                "UPDATE scrape_queue SET status='fetched', "
                "updated_at=CURRENT_TIMESTAMP WHERE url=?", (url,)
            )
        total_queued += len(athlete_urls)
        logger.info("  %-50s -> %d athletes", slug[:50], len(athlete_urls))

    logger.info("Total athlete URLs queued: %d", total_queued)
    return total_queued


if __name__ == "__main__":
    import sys
    Path(__file__).parent.joinpath("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                Path(__file__).parent / "logs" / "scrape_teams.log",
                encoding="utf-8"
            ),
        ],
    )
    init_db()
    schools = discover_schools()
    save_schools_to_db(schools)
    scrape_rosters()