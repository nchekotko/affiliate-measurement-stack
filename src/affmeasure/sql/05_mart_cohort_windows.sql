-- FTD-month cohorts with FIXED windows (D7 / D30 / D90 since each player's FTD).
-- A window value is NULL unless every player of the cohort has had the full window
-- observed (cohort month end + W days <= obs_end), so young cohorts never look worse.
CREATE OR REPLACE TABLE mart_cohort_windows AS
WITH pd AS (
    SELECT p.affiliate_id, p.ftd_month, p.ftd_month_start, p.player_id,
           date_diff('day', CAST(p.ftd_ts AS DATE), d.activity_date) AS age_d,
           d.ngr, d.deposits, d.stake, d.n_deposits, d.n_chargebacks
    FROM fct_player p JOIN fct_player_day d USING (player_id)
    WHERE p.ftd_ts IS NOT NULL
),
per_player AS (
    SELECT affiliate_id, ftd_month, ftd_month_start, player_id,
           SUM(ngr) FILTER (WHERE age_d < 7) AS ngr_d7,
           SUM(ngr) FILTER (WHERE age_d < 30) AS ngr_d30,
           SUM(ngr) FILTER (WHERE age_d < 90) AS ngr_d90,
           SUM(deposits) FILTER (WHERE age_d < 30) AS dep_d30,
           MAX(CASE WHEN age_d BETWEEN 7 AND 29 AND stake > 0 THEN 1 ELSE 0 END) AS active_d8_30,
           MAX(CASE WHEN age_d BETWEEN 30 AND 89 AND stake > 0 THEN 1 ELSE 0 END) AS active_d31_90,
           SUM(n_deposits) FILTER (WHERE age_d < 90) AS n_dep_d90,
           SUM(n_chargebacks) FILTER (WHERE age_d < 90) AS n_cb_d90
    FROM pd GROUP BY ALL
),
agg AS (
    SELECT affiliate_id, ftd_month, ftd_month_start,
           COUNT(*) AS n_ftd,
           AVG(COALESCE(ngr_d7, 0)) AS ngr_d7,
           AVG(COALESCE(ngr_d30, 0)) AS ngr_d30,
           AVG(COALESCE(ngr_d90, 0)) AS ngr_d90,
           AVG(COALESCE(dep_d30, 0)) AS deposits_d30,
           AVG(active_d8_30) AS retention_d8_30,
           AVG(active_d31_90) AS retention_d31_90,
           SUM(n_cb_d90) / NULLIF(SUM(n_dep_d90), 0) AS chargeback_rate_d90
    FROM per_player GROUP BY ALL
)
SELECT
    a.affiliate_id, a.ftd_month, a.n_ftd,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 7 DAY <= m.obs_end THEN a.ngr_d7 END AS ngr_d7,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 30 DAY <= m.obs_end THEN a.ngr_d30 END AS ngr_d30,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 90 DAY <= m.obs_end THEN a.ngr_d90 END AS ngr_d90,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 30 DAY <= m.obs_end THEN a.deposits_d30 END AS deposits_d30,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 30 DAY <= m.obs_end THEN a.retention_d8_30 END AS retention_d8_30,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 90 DAY <= m.obs_end THEN a.retention_d31_90 END AS retention_d31_90,
    CASE WHEN a.ftd_month_start + INTERVAL 1 MONTH + INTERVAL 90 DAY <= m.obs_end THEN a.chargeback_rate_d90 END AS chargeback_rate_d90
FROM agg a CROSS JOIN sim_meta m
ORDER BY a.affiliate_id, a.ftd_month;
