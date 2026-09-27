-- click -> registration -> KYC -> FTD -> first bet funnel by affiliate (last-click tracking).
CREATE OR REPLACE TABLE mart_funnel AS
WITH clk AS (SELECT affiliate_id, COUNT(*) AS clicks FROM clicks GROUP BY 1),
f AS (
    SELECT affiliate_id,
           COUNT(*) AS registrations,
           COUNT(kyc_ts) AS kyc_passed,
           COUNT(ftd_ts) AS ftd,
           COUNT(first_bet_date) AS bettors,
           AVG(ftd_amount) AS avg_ftd_amount
    FROM fct_player GROUP BY 1
)
SELECT
    f.affiliate_id,
    clk.clicks,
    f.registrations,
    f.kyc_passed,
    f.ftd,
    f.bettors,
    f.avg_ftd_amount,
    f.registrations / NULLIF(clk.clicks, 0) AS click_to_reg,
    f.kyc_passed / f.registrations AS reg_to_kyc,
    f.ftd / f.registrations AS reg_to_ftd,
    f.bettors / NULLIF(f.ftd, 0) AS ftd_to_bet
FROM f LEFT JOIN clk USING (affiliate_id)
ORDER BY f.affiliate_id;
