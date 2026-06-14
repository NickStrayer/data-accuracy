"""
parse_athletes.py  —  TFRRS athlete-page parser (fixed & parallelized)
========================================================================

Bug fixes vs original
─────────────────────
1.  TABLE LAYOUT TAXONOMY (Retained)
    Restricts parsing to the first tab-pane-custom div only (Tab 1).

2.  INDOOR / OUTDOOR DETECTION (Retained)
    Infers event type from result date using the standard collegiate track calendar.

3.  DATE PARSING — RANGE DATES (Retained)
    Uses a plain capture group regex that always takes the start date of a range.

4.  ACADEMIC YEAR & CLASS CALCULATION (Fixed)
    - `season_year_from_date` now returns the collegiate academic season year.
    - Fixed the calendar overlap bug where Fall overwrote Spring classes.
    - Fixed "Unknown" class assignments flattening Juniors/Seniors into Sophomores.
    - Fixed gap-year calculations breaking historical class assumptions.

Speedups
────────
1.  PRE-COMPILED REGEXES: Avoids recompiling on every function call.
2.  MULTIPROCESSING: CPU-heavy BeautifulSoup parsing is distributed across cores.
3.  BATCH DATABASE WRITES: Process pool returns dictionaries, which are inserted 
    sequentially in a single large SQLite transaction, avoiding disk-lock overhead.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Tag

from database import (
    DB_PATH, bulk_insert_results, get_connection,
    mark_url, upsert_athlete, upsert_school, upsert_season,
)

logger = logging.getLogger(__name__)
RAW_HTML_DIR = Path(__file__).parent / "raw_html" / "athletes"

# ── Pre-compiled Regexes ──────────────────────────────────────────────────────
_TIME_RE = re.compile(
    r'(?P<hours>\d+):(?P<minutes>\d{1,2}):(?P<seconds>\d{2}(?:\.\d+)?)'   # HH:MM:SS
    r'|(?P<min2>\d{1,2}):(?P<sec2>\d{2}(?:\.\d+)?)'                        # MM:SS
    r'|(?P<flat>\d{1,3}(?:\.\d+)?)'                                       # flat seconds
)
_XC_DIST_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(k|km)\b')
_XC_NUM_RE = re.compile(r'(\d+)')
_CLASS_RE = re.compile(r'\b(fr|so|jr|sr|5th|freshman|sophomore|junior|senior)\b', re.I)
_NAME_CLASS_RE = re.compile(r'^(.+?)\s*\(')
_STATE_RE = re.compile(r'/teams/(?:tf|xc)/([A-Z]{2})_')
_YEAR_ONLY_RE = re.compile(r'(20\d{2})')
_RANGE_DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*\d{1,2},\s*(\d{4})')


# ── Time parser ───────────────────────────────────────────────────────────────

def parse_time_to_seconds(raw: str) -> Optional[float]:
    if not raw:
        return None
    raw = raw.strip().replace(',', '.')
    m = _TIME_RE.match(raw)
    if not m:
        return None
    if m.group('hours') is not None:
        h  = float(m.group('hours'))
        mi = float(m.group('minutes'))
        s  = float(m.group('seconds'))
        total = h * 3600 + mi * 60 + s
        return total if 1.0 <= total <= 18_000 else None
    if m.group('min2') is not None:
        mi = float(m.group('min2'))
        s  = float(m.group('sec2'))
        total = mi * 60 + s
        return total if 1.0 <= total <= 18_000 else None
    flat = float(m.group('flat'))
    return flat if flat >= 0.5 else None


# ── Event normaliser ──────────────────────────────────────────────────────────

_EVENT_DIST: dict[str, Optional[float]] = {
    '60': 60, '100': 100, '200': 200, '400': 400, '500': 500,
    '600': 600, '800': 800, '1000': 1000, '1500': 1500,
    'mile': 1609.34, '1 mile': 1609.34,
    '3000': 3000, '3000sc': 3000, 'steeplechase': 3000,
    '5000': 5000, '5k': 5000,
    '6000': 6000, '6k': 6000,
    '8000': 8000, '8k': 8000,
    '10000': 10_000, '10k': 10_000,
    '5k (xc)': 5000, '6k (xc)': 6000,
    '8k (xc)': 8000, '10k (xc)': 10_000,
    '4k (xc)': 4000, '3 mile': 4828,
    '2 mile': 3218, '4 mile': 6437,
    '4x400': None, '4x800': None, '4x1500': None, '4x1mile': None,
    'dmr': None, 'smr': None,
}

def normalise_event(
    raw: str,
    result_url: str = '',
    result_date: Optional[str] = None,
) -> tuple[str, Optional[float], str]:
    ev = raw.strip().lower()
    is_xc = '/results/xc/' in result_url or 'xc' in ev or 'cross country' in ev

    if is_xc:
        dm = _XC_DIST_RE.search(ev)
        if dm:
            dist = float(dm.group(1)) * 1000
        else:
            nm = _XC_NUM_RE.search(ev)
            dist = float(nm.group(1)) * 1000 if nm else None
        label = f"{int(dist / 1000)}K_XC" if dist else 'XC'
        return label.upper(), dist, 'XC'

    if 'indoor' in result_url.lower():
        etype = 'INDOOR'
    elif result_date:
        try:
            d = datetime.strptime(result_date[:10], '%Y-%m-%d')
            etype = 'INDOOR' if (d.month < 3 or (d.month == 3 and d.day < 15)) else 'OUTDOOR'
        except ValueError:
            etype = 'OUTDOOR'
    else:
        etype = 'OUTDOOR'

    for key, dist in sorted(_EVENT_DIST.items(), key=lambda x: -len(x[0])):
        if key in ev:
            code = re.sub(r'\s+', '', key.upper())
            if code and code[-1].isdigit():
                code += 'M'
            return code, dist, etype

    code = re.sub(r'[^A-Z0-9]', '_', raw.upper())[:20]
    return code, None, etype


# ── Date parser ───────────────────────────────────────────────────────────────

def parse_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in (
        '%b %d, %Y', '%b  %d, %Y', '%B %d, %Y', '%B  %d, %Y',
        '%m/%d/%Y', '%Y-%m-%d',
    ):
        try:
            return datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass

    m = _RANGE_DATE_RE.match(raw)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", '%b %d, %Y').strftime('%Y-%m-%d')
        except ValueError:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", '%B %d, %Y').strftime('%Y-%m-%d')
            except ValueError:
                pass

    ym = _YEAR_ONLY_RE.search(raw)
    if ym:
        return f"{ym.group(1)}-01-01"
    return None

def season_year_from_date(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d')
        return d.year + 1 if d.month >= 8 else d.year
    except ValueError:
        return None


# ── Class year extraction ─────────────────────────────────────────────────────

_CLASS_MAP: dict[str, str] = {
    'fr': 'FR', 'so': 'SO', 'jr': 'JR', 'sr': 'SR', '5th': '5TH',
    'freshman': 'FR', 'sophomore': 'SO', 'junior': 'JR', 'senior': 'SR',
}

def extract_class_year(text: str) -> str:
    m = _CLASS_RE.search(text)
    return _CLASS_MAP.get(m.group(1).lower(), 'Unknown') if m else 'Unknown'


# ── Table layout classifier ───────────────────────────────────────────────────

def _classify_table(table: Tag) -> str:
    data_rows = [r for r in table.find_all('tr') if r.find_all('td')]
    if not data_rows:
        return 'EMPTY'

    cells = data_rows[0].find_all('td')
    if len(cells) < 2:
        return 'EMPTY'

    c0 = cells[0].get_text(strip=True)

    if re.match(r'^20\d{2}$', c0):
        return 'D_PROGRESSION'

    if re.match(r'^\d+:\d+|\d+\.\d{1,2}$', c0):
        return 'B_TOP_BEST'

    return 'A_MEET_RESULT'


# ── Season inference ──────────────────────────────────────────────────────────

def _infer_seasons(results: list[dict], current_class: str) -> list[dict]:
    class_order = ['FR', 'SO', 'JR', 'SR', '5TH']
    
    season_years = {r.get('season_year') for r in results if r.get('season_year') is not None}
    if not season_years:
        return []

    active_years = sorted(list(season_years))
    latest_year = max(active_years)

    try:
        current_idx = class_order.index(current_class)
    except ValueError:
        current_idx = min(len(active_years) - 1, 4)

    seasons = []
    for sy in active_years:
        year_offset = latest_year - sy
        class_idx = current_idx - year_offset
        class_idx = max(0, min(class_idx, 4))
        
        seasons.append({
            'season_year': sy, 
            'class_year': class_order[class_idx]
        })

    return seasons


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_athlete_html(html: str, athlete_id: str) -> dict:
    soup = BeautifulSoup(html, 'lxml')
    data: dict = {
        'athlete_id':   athlete_id,
        'name':         f'Athlete_{athlete_id}',
        'gender':       'M',
        'school':       '',
        'school_state': '',
        'class_year':   'Unknown',
        'results':      [],
    }

    title_tag = soup.find('h3', class_='panel-title large-title')
    if title_tag:
        full_text = title_tag.get_text(' ', strip=True)
        nm = _NAME_CLASS_RE.match(full_text)
        if nm:
            data['name'] = nm.group(1).strip().title()
        data['class_year'] = extract_class_year(full_text)
    else:
        title = soup.find('title')
        if title:
            t = title.get_text()
            m = re.search(r'TFRRS\s*\|\s*(.+?)\s*[–-]', t)
            if m:
                data['name'] = m.group(1).strip().title()

    for a in soup.find_all('a', href=True):
        if '/teams/' in a['href']:
            school_tag = a.find('h3', class_='panel-title')
            if school_tag:
                data['school'] = school_tag.get_text(strip=True).title()

            href = a['href']
            if '_m_' in href:
                data['gender'] = 'M'
            elif '_f_' in href:
                data['gender'] = 'F'

            state_m = _STATE_RE.search(href)
            if state_m:
                data['school_state'] = state_m.group(1)
            break 

    tab_panes = soup.find_all('div', class_=lambda c: c and 'tab-pane-custom' in c)
    if tab_panes:
        search_scope = tab_panes[0]
    else:
        search_scope = soup

    for table in search_scope.find_all('table', class_=lambda c: c and 'table-hover' in c):
        if _classify_table(table) != 'A_MEET_RESULT':
            continue

        thead = table.find('thead')
        if not thead:
            continue
        th = thead.find('th')
        if not th:
            continue

        meet_link = th.find('a')
        meet_name = meet_link.get_text(strip=True) if meet_link else ''
        meet_url  = meet_link['href'] if meet_link else ''

        th_text  = th.get_text(' ', strip=True)
        date_raw = th_text.replace(meet_name, '').strip()
        result_date = parse_date(date_raw)
        season_year = season_year_from_date(result_date)

        is_xc = '/results/xc/' in meet_url

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 2:
                continue

            raw_event = cells[0].get_text(strip=True)
            if not raw_event:
                continue

            mark_cell = cells[1]
            mark_link = mark_cell.find('a')
            raw_mark   = mark_link.get_text(strip=True) if mark_link else mark_cell.get_text(strip=True)
            source_url = mark_link['href'] if mark_link else meet_url

            time_sec = parse_time_to_seconds(raw_mark)
            if time_sec is None:
                continue

            place: Optional[int] = None
            if len(cells) >= 3:
                pm = re.search(r'(\d+)', cells[2].get_text(strip=True))
                if pm:
                    place = int(pm.group(1))

            event_code, dist_m, etype = normalise_event(
                raw_event,
                result_url=source_url or meet_url,
                result_date=result_date,
            )

            if is_xc and etype != 'XC':
                etype = 'XC'

            data['results'].append({
                'athlete_id':      athlete_id,
                'season_year':     season_year,
                'result_date':     result_date,
                'meet_name':       meet_name,
                'event_code':      event_code,
                'event_type':      etype,
                'distance_meters': dist_m,
                'time_seconds':    time_sec,
                'place':           place,
                'wind_mps':        None,
                'source_url':      source_url or meet_url,
            })

    return data


# ── Multiprocessing Worker ────────────────────────────────────────────────────

def _parse_single_file(path: Path) -> tuple[str, Optional[dict], Optional[Exception]]:
    """Worker function to parse an HTML file in an isolated process."""
    m = re.match(r'^(\d+)', path.stem)
    if not m:
        return path.stem, None, ValueError(f"Invalid filename format: {path.name}")
    
    athlete_id = m.group(1)
    
    try:
        html = path.read_text(encoding='utf-8', errors='replace')
        data = parse_athlete_html(html, athlete_id)
        data['seasons'] = _infer_seasons(data['results'], data['class_year'])
        return athlete_id, data, None
    except Exception as exc:
        return athlete_id, None, exc


# ── Batch Runner ──────────────────────────────────────────────────────────────

def parse_all_cached_athletes(db_path: str | Path = DB_PATH) -> dict:
    from tqdm import tqdm

    stats = {'parsed': 0, 'errors': 0, 'results_stored': 0}
    html_files = list(RAW_HTML_DIR.glob('*.html'))
    logger.info('Found %d cached athlete pages', len(html_files))

    # Identify files that still need parsing
    with get_connection(db_path) as conn:
        done_urls = {
            r['url'] for r in conn.execute(
                "SELECT url FROM scrape_queue WHERE status='parsed'"
            ).fetchall()
        }

    files_to_process = []
    for path in html_files:
        m = re.match(r'^(\d+)', path.stem)
        if m and f'https://www.tfrrs.org/athletes/{m.group(1)}' not in done_urls:
            files_to_process.append(path)
            
    logger.info('Need to parse %d files', len(files_to_process))
    if not files_to_process:
        return stats

    parsed_records = []

    # Parse in parallel
    with ProcessPoolExecutor() as executor:
        futures = {executor.submit(_parse_single_file, p): p for p in files_to_process}
        
        for future in tqdm(as_completed(futures), total=len(futures), desc='Parsing HTML (Parallel)', unit='pg'):
            athlete_id, data, exc = future.result()
            
            if exc:
                logger.error('Error parsing athlete %s: %s', athlete_id, exc, exc_info=False)
                stats['errors'] += 1
            elif data:
                parsed_records.append((athlete_id, data))
                stats['parsed'] += 1

    # Write to database sequentially in one massive transaction
    logger.info("Writing %d parsed records to SQLite...", len(parsed_records))
    
    with get_connection(db_path) as conn:
        conn.execute("BEGIN TRANSACTION") 
        
        try:
            for athlete_id, data in tqdm(parsed_records, desc="Writing to DB", unit="ath"):
                school_id: Optional[int] = None
                if data['school']:
                    school_id = upsert_school(
                        conn,
                        data['school'],
                        state=data.get('school_state') or None,
                    )

                upsert_athlete(
                    conn,
                    athlete_id=athlete_id,
                    name=data['name'],
                    gender=data['gender'],
                    school_id=school_id,
                    profile_url=f'https://www.tfrrs.org/athletes/{athlete_id}',
                )

                for s in data['seasons']:
                    upsert_season(
                        conn,
                        athlete_id=athlete_id,
                        season_year=s['season_year'],
                        school_id=school_id,
                        class_year=s['class_year'],
                    )

                n = bulk_insert_results(conn, data['results'])
                stats['results_stored'] += n
                
                mark_url(conn, f'https://www.tfrrs.org/athletes/{athlete_id}.html', 'parsed')
                
            conn.commit()
            
        except Exception as e:
            conn.rollback()
            logger.error("Database write failed during batch insert: %s", e)
            raise

    logger.info('Parse complete: %s', stats)
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    LOG_DIR = Path(__file__).parent / 'logs'
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / 'parse_athletes.log', encoding='utf-8'),
        ],
    )
    results = parse_all_cached_athletes()
    print(f'\nDone: {results}')