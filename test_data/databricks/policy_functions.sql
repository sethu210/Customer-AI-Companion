-- Policy signals for the deterministic policy engine (coded_tools/customer_service/policy.py).
-- Lives in its own schema, policy_api, so it is NOT exposed by the agent's MCP endpoint (.../functions/{catalog}/agent_api):
-- risk flags must never reach the LLM. The adapter calls it through the SQL Statement API only.
--
-- Apply with: python -m test_data.databricks.run_sql --file test_data/databricks/policy_functions.sql

CREATE SCHEMA IF NOT EXISTS {catalog}.policy_api
;
CREATE OR REPLACE FUNCTION {catalog}.policy_api.get_policy_signals(
  customer_id STRING COMMENT 'Authenticated customer id.'
)
RETURNS TABLE (
  customer_id STRING, risk_flags STRING, account_age_days INT, refunds_365d INT, refund_total_365d DOUBLE, as_of DATE
)
COMMENT 'Risk and history signals for policy decisions. Internal only; never shown to the agent or the customer.'
RETURN
  WITH cfg AS (SELECT to_date(value) AS as_of FROM {catalog}.silver.config WHERE key = 'reference_date'),
  refunds AS (
    SELECT count(*) AS n, coalesce(sum(f.amount), 0.0) AS total
    FROM {catalog}.silver.refunds f
    JOIN {catalog}.silver.orders o ON o.order_id = f.order_id
    CROSS JOIN cfg
    WHERE o.customer_id = get_policy_signals.customer_id
      AND f.status = 'completed'
      AND to_date(f.completed_at) > date_sub(cfg.as_of, 365)
  )
  SELECT c.customer_id, array_join(array_sort(c.risk_flags), ';'),
         datediff(cfg.as_of, to_date(c.created_at)), CAST(refunds.n AS INT), refunds.total, cfg.as_of
  FROM {catalog}.silver.customers c
  CROSS JOIN cfg
  CROSS JOIN refunds
  WHERE c.customer_id = get_policy_signals.customer_id
;
