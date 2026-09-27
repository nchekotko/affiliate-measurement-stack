-- Player x day money facts. NGR convention of this repo:
--   ngr = GGR - gaming tax - provider/game fees
-- Bonus cost is kept as a separate line so contribution margin subtracts it explicitly
-- (no double counting).
CREATE OR REPLACE TABLE fct_player_day AS
WITH dep AS (
    SELECT player_id, CAST(dep_ts AS DATE) AS d,
           SUM(amount) AS deposits,
           COUNT(*) AS n_deposits,
           SUM(CASE method
                   WHEN 'card' THEN amount * m.card_fee_pct + m.card_fee_fixed
                   WHEN 'ewallet' THEN amount * m.ewallet_fee_pct
                   ELSE m.open_banking_fee_fixed END) AS deposit_fees
    FROM deposits CROSS JOIN sim_meta m
    GROUP BY 1, 2
),
bet AS (
    SELECT b.player_id, CAST(b.bet_date AS DATE) AS d,
           SUM(b.stake) AS stake,
           SUM(b.ggr) AS ggr,
           SUM(b.n_bets) AS n_bets,
           SUM(CASE WHEN g.vertical = 'casino' THEN b.ggr ELSE 0 END) AS casino_ggr,
           SUM(b.ggr * g.provider_fee_rate) AS provider_fee
    FROM bets b JOIN games g USING (game_id)
    GROUP BY 1, 2
),
wd AS (
    SELECT player_id, CAST(wd_ts AS DATE) AS d, SUM(amount) AS withdrawals, COUNT(*) AS n_withdrawals
    FROM withdrawals GROUP BY 1, 2
),
bon AS (
    SELECT player_id, CAST(bonus_ts AS DATE) AS d, SUM(amount) AS bonus_cost
    FROM bonuses GROUP BY 1, 2
),
cb AS (
    SELECT player_id, CAST(cb_ts AS DATE) AS d, SUM(amount) AS chargeback_amount, COUNT(*) AS n_chargebacks
    FROM chargebacks GROUP BY 1, 2
),
keys AS (
    SELECT player_id, d FROM dep UNION
    SELECT player_id, d FROM bet UNION
    SELECT player_id, d FROM wd UNION
    SELECT player_id, d FROM bon UNION
    SELECT player_id, d FROM cb
)
SELECT
    k.player_id,
    k.d AS activity_date,
    COALESCE(dep.deposits, 0) AS deposits,
    COALESCE(dep.n_deposits, 0) AS n_deposits,
    COALESCE(bet.stake, 0) AS stake,
    COALESCE(bet.n_bets, 0) AS n_bets,
    COALESCE(bet.ggr, 0) AS ggr,
    COALESCE(bet.casino_ggr, 0) AS casino_ggr,
    COALESCE(bet.provider_fee, 0) AS provider_fee,
    COALESCE(bet.ggr, 0) * m.gaming_tax_rate AS gaming_tax,
    COALESCE(wd.withdrawals, 0) AS withdrawals,
    COALESCE(bon.bonus_cost, 0) AS bonus_cost,
    COALESCE(cb.chargeback_amount, 0) AS chargeback_amount,
    COALESCE(cb.n_chargebacks, 0) AS n_chargebacks,
    COALESCE(cb.chargeback_amount, 0) + COALESCE(cb.n_chargebacks, 0) * m.chargeback_fee AS chargeback_cost,
    COALESCE(dep.deposit_fees, 0) + COALESCE(wd.n_withdrawals, 0) * m.withdrawal_fee AS payment_cost,
    COALESCE(bet.ggr, 0) * (1 - m.gaming_tax_rate) - COALESCE(bet.provider_fee, 0) AS ngr
FROM keys k
CROSS JOIN sim_meta m
LEFT JOIN dep USING (player_id, d)
LEFT JOIN bet USING (player_id, d)
LEFT JOIN wd USING (player_id, d)
LEFT JOIN bon USING (player_id, d)
LEFT JOIN cb USING (player_id, d);
