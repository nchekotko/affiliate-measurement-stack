-- Player x calendar month (invoice grain) with lifetime age since registration.
CREATE OR REPLACE TABLE fct_player_month AS
SELECT
    d.player_id,
    p.affiliate_id,
    strftime(d.activity_date, '%Y-%m') AS month,
    date_diff('month', date_trunc('month', p.reg_ts), date_trunc('month', d.activity_date)) AS months_since_reg,
    SUM(d.deposits) AS deposits,
    SUM(d.stake) AS stake,
    SUM(d.ggr) AS ggr,
    SUM(d.casino_ggr) AS casino_ggr,
    SUM(d.provider_fee) AS provider_fee,
    SUM(d.gaming_tax) AS gaming_tax,
    SUM(d.bonus_cost) AS bonus_cost,
    SUM(d.chargeback_amount) AS chargeback_amount,
    SUM(d.chargeback_cost) AS chargeback_cost,
    SUM(d.payment_cost) AS payment_cost,
    SUM(d.withdrawals) AS withdrawals,
    SUM(d.ngr) AS ngr,
    MAX(CASE WHEN d.stake > 0 THEN 1 ELSE 0 END) AS is_active
FROM fct_player_day d
JOIN fct_player p USING (player_id)
GROUP BY ALL;
