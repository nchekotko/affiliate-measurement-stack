-- NGR curve by FTD-month cohort and 30-day age since FTD. Only fully observed ages are kept.
-- segment = affiliate_id, plus an 'ALL' roll-up. active_share doubles as the retention curve.
CREATE OR REPLACE TABLE mart_ltv_curve AS
WITH base AS (
    SELECT p.affiliate_id, p.ftd_month, p.player_id,
           CAST(floor(date_diff('day', CAST(p.ftd_ts AS DATE), d.activity_date) / 30) AS INTEGER) AS age,
           d.ngr, d.stake
    FROM fct_player p JOIN fct_player_day d USING (player_id)
    WHERE p.ftd_ts IS NOT NULL
),
sizes AS (
    SELECT affiliate_id AS segment, ftd_month, ANY_VALUE(ftd_month_start) AS ftd_month_start, COUNT(*) AS n_ftd
    FROM fct_player WHERE ftd_ts IS NOT NULL GROUP BY 1, 2
    UNION ALL
    SELECT 'ALL' AS segment, ftd_month, ANY_VALUE(ftd_month_start), COUNT(*)
    FROM fct_player WHERE ftd_ts IS NOT NULL GROUP BY 2
),
cells AS (
    SELECT affiliate_id AS segment, ftd_month, age,
           SUM(ngr) AS ngr, COUNT(DISTINCT player_id) FILTER (WHERE stake > 0) AS active
    FROM base GROUP BY 1, 2, 3
    UNION ALL
    SELECT 'ALL' AS segment, ftd_month, age, SUM(ngr), COUNT(DISTINCT player_id) FILTER (WHERE stake > 0)
    FROM base GROUP BY 2, 3
),
grid AS (
    SELECT s.segment, s.ftd_month, s.n_ftd, a.age
    FROM sizes s
    CROSS JOIN range(0, 24) a(age)
    CROSS JOIN sim_meta m
    WHERE s.ftd_month_start + INTERVAL 1 MONTH + to_days(CAST(30 * (a.age + 1) AS INTEGER)) <= m.obs_end
)
SELECT
    g.segment, g.ftd_month, g.age, g.n_ftd,
    COALESCE(c.ngr, 0) AS ngr,
    COALESCE(c.ngr, 0) / g.n_ftd AS ngr_per_ftd,
    SUM(COALESCE(c.ngr, 0)) OVER (PARTITION BY g.segment, g.ftd_month ORDER BY g.age) / g.n_ftd AS cum_ngr_per_ftd,
    COALESCE(c.active, 0) / g.n_ftd AS active_share
FROM grid g
LEFT JOIN cells c USING (segment, ftd_month, age)
ORDER BY g.segment, g.ftd_month, g.age;
