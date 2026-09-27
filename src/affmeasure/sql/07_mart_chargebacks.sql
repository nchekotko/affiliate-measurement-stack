-- Chargeback rates by affiliate (count- and amount-based, all observed deposits).
CREATE OR REPLACE TABLE mart_chargebacks AS
SELECT
    p.affiliate_id,
    COUNT(DISTINCT dep.deposit_id) AS n_deposits,
    SUM(dep.amount) AS deposit_amount,
    COUNT(cb.deposit_id) AS n_chargebacks,
    COALESCE(SUM(cb.amount), 0) AS chargeback_amount,
    COUNT(cb.deposit_id) / COUNT(DISTINCT dep.deposit_id) AS cb_rate_count,
    COALESCE(SUM(cb.amount), 0) / SUM(dep.amount) AS cb_rate_amount
FROM deposits dep
JOIN fct_player p USING (player_id)
LEFT JOIN chargebacks cb USING (deposit_id)
GROUP BY 1
ORDER BY 1;
