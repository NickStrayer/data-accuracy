"""
extract_conferences.py

Scans raw_html/rosters/ for base roster files (no year suffix),
extracts conference and XC region from the TFRRS league links,
and writes conference_map.json + region_map.json.

Usage:
    python extract_conferences.py
    python extract_conferences.py --roster-dir path/to/rosters --out-dir path/to/output
"""

import os
import re
import json
import argparse
from pathlib import Path
from bs4 import BeautifulSoup
from collections import defaultdict

# ---------------------------------------------------------------------------
# Patterns that indicate a versioned/historical file — skip these
# e.g. AL_college_m_Alabama_2021.html, AL_college_m_Alabama_2015.html
# ---------------------------------------------------------------------------
YEAR_SUFFIX_RE = re.compile(r'_\d{4}(?:_\d+)?\.html$', re.IGNORECASE)

# XC region league names contain "Region" (e.g. "DI South Region", "DI Midwest Region")
REGION_RE = re.compile(r'region', re.IGNORECASE)


def is_base_file(filename: str) -> bool:
    """Return True if this is a base roster file (not a year-suffixed historical copy)."""
    if YEAR_SUFFIX_RE.search(filename):
        return False
    if not filename.lower().endswith('.html'):
        return False
    return True


def school_name_from_filename(filename: str) -> str:
    """
    Convert filename like 'AL_college_m_Alabama.html' → 'Alabama'
    or 'AL_college_f_North_Carolina_State.html' → 'North Carolina State'
    
    Filename pattern: {STATE}_{type}_{gender}_{School_Name}.html
    We split on '_' and drop the first 3 tokens (state, type, gender).
    """
    stem = Path(filename).stem  # strip .html
    parts = stem.split('_')
    if len(parts) < 4:
        return stem  # fallback — return whole stem
    school_parts = parts[3:]  # everything after state_type_gender
    return ' '.join(school_parts)


def extract_leagues(html_content: str) -> tuple[str | None, str | None]:
    """
    Parse a TFRRS team HTML page and return (conference, xc_region).
    
    The leagues appear in a <span class="panel-heading-normal-text"> inside
    .panel-second-title, as <a> tags linking to /leagues/{id}.html
    
    Example HTML:
        <a href="/leagues/72.html">SEC</a>, 
        <a href="/leagues/1619.html">DI South Region</a>
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    conference = None
    xc_region = None

    # Find the panel-second-title section
    second_title = soup.find(class_='panel-second-title')
    if not second_title:
        return None, None

    # All league anchor tags inside that section
    league_links = second_title.find_all('a', href=re.compile(r'/leagues/\d+'))
    
    for link in league_links:
        name = link.get_text(strip=True)
        if not name:
            continue
        if REGION_RE.search(name):
            xc_region = name
        else:
            # First non-region league = primary athletic conference
            if conference is None:
                conference = name

    return conference, xc_region


def extract_gender(filename: str) -> str:
    """Extract gender from filename pattern: STATE_type_GENDER_School.html"""
    parts = Path(filename).stem.split('_')
    if len(parts) >= 3:
        g = parts[2].upper()
        if g in ('M', 'F'):
            return g
    return 'U'  # unknown


def main():
    parser = argparse.ArgumentParser(description='Extract conference/region from TFRRS roster HTML files')
    parser.add_argument('--roster-dir', default='data-accuracy/raw_html/rosters',
                        help='Directory containing roster HTML files')
    parser.add_argument('--out-dir', default='.',
                        help='Output directory for JSON files')
    args = parser.parse_args()

    roster_dir = Path(args.roster_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not roster_dir.exists():
        print(f"ERROR: Roster directory not found: {roster_dir}")
        print("Pass the correct path with --roster-dir")
        return

    # Collect all base HTML files
    all_files = sorted(roster_dir.glob('*.html'))
    base_files = [f for f in all_files if is_base_file(f.name)]
    skipped_year = len(all_files) - len(base_files)

    print(f"Found {len(all_files)} total HTML files")
    print(f"  Skipping {skipped_year} year-suffixed historical files")
    print(f"  Processing {len(base_files)} base roster files\n")

    # --- Output structures ---
    # conference_map: school_name → conference  (e.g. "Alabama" → "SEC")
    # region_map:     school_name → xc_region   (e.g. "Alabama" → "DI South Region")
    # Both are further split by gender: _m_ files → men's, _f_ → women's
    # Since conference is usually the same for M/W we also produce a merged version.

    conf_by_school_gender: dict[tuple[str, str], str] = {}
    region_by_school_gender: dict[tuple[str, str], str] = {}

    errors = []
    no_league = []

    for filepath in base_files:
        school = school_name_from_filename(filepath.name)
        gender = extract_gender(filepath.name)

        try:
            html = filepath.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            errors.append((filepath.name, str(e)))
            continue

        conference, xc_region = extract_leagues(html)

        if conference is None and xc_region is None:
            no_league.append(filepath.name)

        key = (school, gender)
        if conference:
            conf_by_school_gender[key] = conference
        if xc_region:
            region_by_school_gender[key] = xc_region

    # --- Build flat maps (school → value, preferring M then F if both exist) ---
    all_schools = set(s for s, _ in conf_by_school_gender) | set(s for s, _ in region_by_school_gender)

    conference_map: dict[str, str] = {}
    region_map: dict[str, str] = {}

    # Also produce gendered maps for cases where M/W are in different conferences (rare but happens)
    conference_map_m: dict[str, str] = {}
    conference_map_f: dict[str, str] = {}
    region_map_m: dict[str, str] = {}
    region_map_f: dict[str, str] = {}

    for school in sorted(all_schools):
        conf_m = conf_by_school_gender.get((school, 'M'))
        conf_f = conf_by_school_gender.get((school, 'F'))
        reg_m  = region_by_school_gender.get((school, 'M'))
        reg_f  = region_by_school_gender.get((school, 'F'))

        # Merged (school → conf): prefer M, fall back to F
        conference_map[school] = conf_m or conf_f or ''
        region_map[school]     = reg_m  or reg_f  or ''

        if conf_m: conference_map_m[school] = conf_m
        if conf_f: conference_map_f[school] = conf_f
        if reg_m:  region_map_m[school] = reg_m
        if reg_f:  region_map_f[school] = reg_f

    # --- Write outputs ---
    def write_json(data, filename):
        path = out_dir / filename
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Wrote {path}  ({len(data)} entries)")

    write_json(conference_map,   'conference_map.json')
    write_json(region_map,       'region_map.json')
    write_json(conference_map_m, 'conference_map_men.json')
    write_json(conference_map_f, 'conference_map_women.json')
    write_json(region_map_m,     'region_map_men.json')
    write_json(region_map_f,     'region_map_women.json')

    # --- Summary stats ---
    print(f"\n--- Summary ---")
    print(f"Schools with conference: {len([v for v in conference_map.values() if v])}")
    print(f"Schools with XC region:  {len([v for v in region_map.values() if v])}")
    print(f"Schools with neither:    {len([v for v in conference_map.values() if not v])}")

    # Conference breakdown
    conf_counts: dict[str, int] = defaultdict(int)
    for v in conference_map.values():
        if v:
            conf_counts[v] += 1
    print(f"\nConferences found ({len(conf_counts)}):")
    for conf, count in sorted(conf_counts.items(), key=lambda x: -x[1]):
        print(f"  {conf:40s} {count:3d} schools")

    region_counts: dict[str, int] = defaultdict(int)
    for v in region_map.values():
        if v:
            region_counts[v] += 1
    print(f"\nXC Regions found ({len(region_counts)}):")
    for reg, count in sorted(region_counts.items(), key=lambda x: -x[1]):
        print(f"  {reg:40s} {count:3d} schools")

    if errors:
        print(f"\nRead errors ({len(errors)}):")
        for fname, err in errors:
            print(f"  {fname}: {err}")

    if no_league:
        print(f"\nFiles with no league links found ({len(no_league)}):")
        for fname in no_league[:20]:
            print(f"  {fname}")
        if len(no_league) > 20:
            print(f"  ... and {len(no_league) - 20} more")


if __name__ == '__main__':
    main()