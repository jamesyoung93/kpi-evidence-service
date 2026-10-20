CREATE OR REPLACE VIEW ${gov}.gv_rx_account_monthly AS
SELECT
  'KESTREL' AS brand,
  account_id,
  CAST(npi AS STRING) AS npi,
  month,
  CAST(month || '-01' AS DATE) AS month_start,
  last_day(CAST(month || '-01' AS DATE)) AS month_end,
  territory, district, region, specialty, payer_channel,
  trx_units, trx_dollars, trx_normalized, nrx, nbrx,
  market_trx, market_nrx, il17_class_trx, advanced_therapy_trx,
  calls, call_plan, samples, speaker_attendance, new_writers,
  CASE WHEN trx_units > 0 THEN 1 ELSE 0 END AS is_writer
FROM ${platform}.rx_account_monthly;

CREATE OR REPLACE VIEW ${gov}.gv_rx_panel_monthly AS
SELECT
  brand,
  month,
  CAST(month || '-01' AS DATE) AS month_start,
  last_day(CAST(month || '-01' AS DATE)) AS month_end,
  territory, district, region, specialty, payer_channel,
  trx_units, trx_dollars, trx_normalized, nrx, nbrx,
  market_trx, market_nrx, il17_class_trx, advanced_therapy_trx,
  load_id, loaded_at
FROM (
  SELECT
    loads.*,
    ROW_NUMBER() OVER (PARTITION BY brand, month, territory, payer_channel
                       ORDER BY loaded_at DESC, load_id DESC) AS load_rank
  FROM (
    SELECT
      'KESTREL' AS brand,
      month, territory, district, region, specialty, payer_channel,
      trx_units, trx_dollars, trx_normalized, nrx, nbrx,
      market_trx, market_nrx, il17_class_trx, advanced_therapy_trx,
      load_id, loaded_at
    FROM ${platform}.rx_panel_monthly
  ) loads
) ranked_loads
WHERE load_rank = 1;

CREATE OR REPLACE VIEW ${gov}.gv_referrals_monthly AS
SELECT
  'KESTREL' AS brand,
  account_id,
  month,
  CAST(month || '-01' AS DATE) AS month_start,
  last_day(CAST(month || '-01' AS DATE)) AS month_end,
  territory, district, region, specialty, payer_channel,
  referrals_in, active_referrers
FROM ${platform}.referrals_monthly;
