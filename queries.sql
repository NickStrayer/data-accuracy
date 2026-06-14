-- ============================================================
--  queries.sql  –  Useful analytical queries for the TFRRS DB
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. Best ever mark per athlete per event
-- ─────────────────────────────────────────────────────────────
SELECT
    a.athlete_name,
    a.gender,
    sc.school_name,
    r.event_code,
    MIN(r.time_seconds)              AS best_time_seconds,
    MIN(r.time_seconds) / 60.0      AS best_time_minutes
FROM results r
JOIN athletes a  ON a.athlete_id  = r.athlete_id
LEFT JOIN schools sc ON sc.school_id = a.school_id
WHERE r.event_code = '8K_XC'
  AND a.gender = 'M'
GROUP BY a.athlete_id, r.event_code
ORDER BY best_time_seconds
LIMIT 100;


-- ─────────────────────────────────────────────────────────────
-- 2. Year-over-year progression for a single athlete
-- ─────────────────────────────────────────────────────────────
WITH bests AS (
    SELECT
        athlete_id,
        season_year,
        event_code,
        MIN(time_seconds) AS best_time
    FROM results
    WHERE event_code = '6K_XC'
    GROUP BY athlete_id, season_year
),
lagged AS (
    SELECT
        b.*,
        s.class_year,
        LAG(b.best_time) OVER (
            PARTITION BY b.athlete_id ORDER BY b.season_year
        ) AS prev_time
    FROM bests b
    LEFT JOIN seasons s
           ON s.athlete_id = b.athlete_id
          AND s.season_year = b.season_year
)
SELECT
    l.athlete_id,
    a.athlete_name,
    l.season_year,
    l.class_year,
    ROUND(l.best_time, 2)                                       AS best_time,
    ROUND(l.prev_time, 2)                                       AS prev_time,
    ROUND((l.prev_time - l.best_time) / l.prev_time * 100, 2)  AS pct_improvement
FROM lagged l
JOIN athletes a ON a.athlete_id = l.athlete_id
WHERE l.prev_time IS NOT NULL
ORDER BY pct_improvement DESC
LIMIT 50;


-- ─────────────────────────────────────────────────────────────
-- 3. National percentile ranking by event (current season)
-- ─────────────────────────────────────────────────────────────
WITH season_bests AS (
    SELECT
        r.athlete_id,
        r.event_code,
        MIN(r.time_seconds) AS best_time
    FROM results r
    WHERE r.season_year = (SELECT MAX(season_year) FROM results)
    GROUP BY r.athlete_id, r.event_code
),
ranked AS (
    SELECT *,
        PERCENT_RANK() OVER (
            PARTITION BY event_code ORDER BY best_time DESC
        ) * 100 AS national_pct
    FROM season_bests
)
SELECT
    a.athlete_name,
    sc.school_name,
    r.event_code,
    ROUND(r.best_time / 60.0, 2)  AS time_min,
    ROUND(r.national_pct, 1)      AS national_percentile
FROM ranked r
JOIN athletes a  ON a.athlete_id = r.athlete_id
LEFT JOIN schools sc ON sc.school_id = a.school_id
WHERE r.event_code = '5000M'
ORDER BY national_pct DESC
LIMIT 200;


-- ─────────────────────────────────────────────────────────────
-- 4. Transfer detection (different school in consecutive seasons)
-- ─────────────────────────────────────────────────────────────
SELECT
    s1.athlete_id,
    a.athlete_name,
    s1.season_year        AS from_year,
    sc1.school_name       AS from_school,
    s2.season_year        AS to_year,
    sc2.school_name       AS to_school
FROM seasons s1
JOIN seasons s2  ON  s2.athlete_id  = s1.athlete_id
                 AND s2.season_year = s1.season_year + 1
JOIN athletes a  ON  a.athlete_id   = s1.athlete_id
LEFT JOIN schools sc1 ON sc1.school_id = s1.school_id
LEFT JOIN schools sc2 ON sc2.school_id = s2.school_id
WHERE s1.school_id IS NOT NULL
  AND s2.school_id IS NOT NULL
  AND s1.school_id != s2.school_id
ORDER BY s1.season_year DESC
LIMIT 200;


-- ─────────────────────────────────────────────────────────────
-- 5. Attrition summary – who never came back after their FR year?
-- ─────────────────────────────────────────────────────────────
SELECT
    COUNT(DISTINCT fr.athlete_id)       AS total_freshmen,
    COUNT(DISTINCT so.athlete_id)       AS returned_sophomores,
    ROUND(
        1.0 * COUNT(DISTINCT so.athlete_id) /
        NULLIF(COUNT(DISTINCT fr.athlete_id), 0), 3
    )                                   AS return_rate
FROM seasons fr
LEFT JOIN seasons so
       ON  so.athlete_id  = fr.athlete_id
       AND so.class_year  = 'SO'
WHERE fr.class_year = 'FR';


-- ─────────────────────────────────────────────────────────────
-- 6. Top-improving athletes FR→SO (breakout candidates)
-- ─────────────────────────────────────────────────────────────
WITH fr_marks AS (
    SELECT r.athlete_id, MIN(r.time_seconds) AS fr_time
    FROM results r
    JOIN seasons s ON s.athlete_id = r.athlete_id
                  AND s.season_year = r.season_year
    WHERE s.class_year = 'FR'
      AND r.event_code = '8K_XC'
    GROUP BY r.athlete_id
),
so_marks AS (
    SELECT r.athlete_id, MIN(r.time_seconds) AS so_time
    FROM results r
    JOIN seasons s ON s.athlete_id = r.athlete_id
                  AND s.season_year = r.season_year
    WHERE s.class_year = 'SO'
      AND r.event_code = '8K_XC'
    GROUP BY r.athlete_id
)
SELECT
    a.athlete_name,
    sc.school_name,
    ROUND(f.fr_time / 60.0, 2)                             AS fr_min,
    ROUND(s.so_time / 60.0, 2)                             AS so_min,
    ROUND((f.fr_time - s.so_time) / f.fr_time * 100, 2)   AS pct_improvement
FROM fr_marks f
JOIN so_marks s USING (athlete_id)
JOIN athletes a  ON a.athlete_id = f.athlete_id
LEFT JOIN schools sc ON sc.school_id = a.school_id
ORDER BY pct_improvement DESC
LIMIT 50;


-- ─────────────────────────────────────────────────────────────
-- 7. Division-level performance benchmarks
-- ─────────────────────────────────────────────────────────────
SELECT
    sc.division,
    a.gender,
    r.event_code,
    COUNT(*)                         AS n_results,
    ROUND(AVG(r.time_seconds)/60, 2) AS avg_min,
    ROUND(MIN(r.time_seconds)/60, 2) AS best_min,
    ROUND(MAX(r.time_seconds)/60, 2) AS slowest_min
FROM results r
JOIN athletes a  ON a.athlete_id  = r.athlete_id
LEFT JOIN schools sc ON sc.school_id = a.school_id
WHERE r.event_code IN ('5000M', '6K_XC', '8K_XC', '10000M')
GROUP BY sc.division, a.gender, r.event_code
ORDER BY sc.division, a.gender, r.event_code;


-- ─────────────────────────────────────────────────────────────
-- 8. Scrape queue health check
-- ─────────────────────────────────────────────────────────────
SELECT
    entity_type,
    status,
    COUNT(*) AS n,
    MAX(updated_at) AS last_update
FROM scrape_queue
GROUP BY entity_type, status
ORDER BY entity_type, status;


-- ─────────────────────────────────────────────────────────────
-- 9. Precomputed progression stats (from analytics cache)
-- ─────────────────────────────────────────────────────────────
SELECT
    event_code,
    gender,
    from_class || '→' || to_class   AS transition,
    n,
    ROUND(mean_improvement_pct, 2)  AS mean_pct,
    ROUND(std_improvement_pct,  2)  AS std_pct,
    ROUND(p25, 2)                   AS p25,
    ROUND(p75, 2)                   AS p75
FROM progression_stats
ORDER BY event_code, gender, from_class;


-- ─────────────────────────────────────────────────────────────
-- 10. Coach recruiting target: DI athletes ≥95th percentile
--     in 5k who are still FR/SO
-- ─────────────────────────────────────────────────────────────
WITH season_bests AS (
    SELECT r.athlete_id, r.season_year,
           MIN(r.time_seconds) AS best_time
    FROM results r
    WHERE r.event_code = '5000M'
    GROUP BY r.athlete_id, r.season_year
),
ranked AS (
    SELECT *,
        PERCENT_RANK() OVER (
            PARTITION BY season_year ORDER BY best_time DESC
        ) AS pct_rank
    FROM season_bests
)
SELECT
    a.athlete_name,
    sc.school_name,
    r.season_year,
    s.class_year,
    ROUND(r.best_time / 60.0, 2) AS time_min,
    ROUND(r.pct_rank * 100, 1)   AS national_pct
FROM ranked r
JOIN athletes  a  ON a.athlete_id  = r.athlete_id
LEFT JOIN schools sc ON sc.school_id = a.school_id
LEFT JOIN seasons s  ON s.athlete_id  = r.athlete_id
                    AND s.season_year  = r.season_year
WHERE sc.division = 'I'
  AND s.class_year IN ('FR', 'SO')
  AND r.pct_rank >= 0.95
ORDER BY r.pct_rank DESC;
