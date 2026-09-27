-- One row per registered player: attribution (tracking last click) and funnel timestamps.
CREATE OR REPLACE TABLE fct_player AS
WITH kyc_pass AS (
    SELECT player_id,
           MIN(kyc_ts) FILTER (WHERE status = 'passed') AS kyc_ts,
           COUNT(*) AS kyc_attempts
    FROM kyc
    GROUP BY 1
),
ftd AS (
    SELECT player_id, MIN(dep_ts) AS ftd_ts, arg_min(amount, dep_ts) AS ftd_amount
    FROM deposits
    GROUP BY 1
),
first_bet AS (
    SELECT player_id, MIN(bet_date) AS first_bet_date FROM bets GROUP BY 1
)
SELECT
    p.player_id,
    p.reg_ts,
    p.geo,
    p.device_fp,
    COALESCE(c.affiliate_id, 'ORGANIC') AS affiliate_id,
    c.sub_id,
    p.click_id,
    c.click_ts,
    date_diff('second', c.click_ts, p.reg_ts) AS click_to_reg_sec,
    k.kyc_ts,
    COALESCE(k.kyc_attempts, 0) AS kyc_attempts,
    f.ftd_ts,
    f.ftd_amount,
    strftime(f.ftd_ts, '%Y-%m') AS ftd_month,
    date_trunc('month', f.ftd_ts) AS ftd_month_start,
    b.first_bet_date,
    CAST(floor(date_diff('day', m.start_ts, p.reg_ts) / 7) AS INTEGER) AS reg_week
FROM players p
CROSS JOIN sim_meta m
LEFT JOIN clicks c USING (click_id)
LEFT JOIN kyc_pass k USING (player_id)
LEFT JOIN ftd f USING (player_id)
LEFT JOIN first_bet b USING (player_id);
