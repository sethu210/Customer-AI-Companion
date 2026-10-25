-- Unity Catalog functions exposed to the agent through the managed MCP server
--   /api/2.0/mcp/functions/{catalog}/agent_api
-- Each function becomes one MCP tool; the COMMENTs are the tool and parameter descriptions the LLM sees.
--
-- Rules:
--   * Every function is scoped to exactly one customer_id. The agent's coded tool fills customer_id from
--     sly_data (the authenticated channel), never from the conversation.
--   * Read-only, minimal columns: no risk flags, no payment tokens, no addresses, no free-text support notes.
--   * "Now" is config.reference_date (returned as as_of), never the wall clock, so tests are repeatable.
--
-- Apply with: python -m test_data.databricks.run_sql --file test_data/databricks/uc_functions.sql
-- {catalog} is substituted by run_sql.py. Statements are separated by a line containing only ";".

CREATE OR REPLACE FUNCTION {catalog}.agent_api.get_customer_profile(
  customer_id STRING COMMENT 'Authenticated customer id. Supplied by the system, never taken from the conversation.'
)
RETURNS TABLE (
  customer_id STRING, full_name STRING, tier STRING, member_since DATE, loyalty_points BIGINT, as_of DATE
)
COMMENT 'Basic profile of the authenticated customer: name, membership tier, member-since date and loyalty points balance. Use it to personalise answers.'
RETURN
  SELECT c.customer_id, c.full_name, c.tier, to_date(c.created_at), l.points,
         (SELECT to_date(value) FROM {catalog}.silver.config WHERE key = 'reference_date')
  FROM {catalog}.silver.customers c
  LEFT JOIN {catalog}.silver.loyalty l ON l.customer_id = c.customer_id
  WHERE c.customer_id = get_customer_profile.customer_id
;
CREATE OR REPLACE FUNCTION {catalog}.agent_api.get_customer_context(
  customer_id STRING COMMENT 'Authenticated customer id. Supplied by the system, never taken from the conversation.'
)
RETURNS TABLE (
  order_id STRING, placed_on DATE, order_status STRING,
  item_id STRING, title STRING, color STRING, size STRING, qty BIGINT, price DOUBLE, currency STRING, item_status STRING,
  carrier STRING, tracking_no STRING, shipment_status STRING,
  last_shipment_event STRING, last_shipment_location STRING, last_shipment_event_at TIMESTAMP, delivered_on DATE,
  return_status STRING, return_label_created_at TIMESTAMP, return_warehouse_scanned_at TIMESTAMP,
  refund_status STRING, refund_amount DOUBLE, refund_method STRING,
  refund_initiated_at TIMESTAMP, refund_completed_at TIMESTAMP, refund_failure_reason STRING,
  payment_last4 STRING, return_window_open BOOLEAN, as_of DATE
)
COMMENT 'Unified view of the authenticated customer: one row per ordered item, newest order first, with its order, shipment, return, refund and payment status joined together. Call this first to answer questions about orders, deliveries, returns or refunds without asking the customer anything. return_window_open is null until the item is delivered.'
RETURN
  WITH cfg AS (
    SELECT to_date(max(CASE WHEN key = 'reference_date' THEN value END)) AS as_of,
           CAST(max(CASE WHEN key = 'return_window_days' THEN value END) AS INT) AS window_days
    FROM {catalog}.silver.config
  ),
  last_evt AS (
    SELECT shipment_id,
           max_by(event_type, event_at) AS last_event,
           max_by(location, event_at) AS last_location,
           max(event_at) AS last_event_at,
           max(CASE WHEN event_type = 'delivered' THEN event_at END) AS delivered_at
    FROM {catalog}.silver.shipment_events
    GROUP BY shipment_id
  )
  SELECT i.order_id, to_date(i.placed_at), i.order_status,
         i.item_id, i.title, i.color, i.size, i.qty, i.price, i.currency, i.item_status,
         i.carrier, i.tracking_no, i.shipment_status,
         e.last_event, e.last_location, e.last_event_at, to_date(e.delivered_at),
         r.status, r.label_created_at, r.warehouse_scanned_at,
         f.status, f.amount, f.method, f.initiated_at, f.completed_at, f.failure_reason,
         p.last4,
         CASE WHEN e.delivered_at IS NULL THEN NULL
              ELSE datediff(cfg.as_of, to_date(e.delivered_at)) <= cfg.window_days END,
         cfg.as_of
  FROM {catalog}.gold.order_items_enriched i
  CROSS JOIN cfg
  LEFT JOIN last_evt e ON e.shipment_id = i.shipment_id
  LEFT JOIN {catalog}.silver.returns r ON r.item_id = i.item_id
  LEFT JOIN {catalog}.silver.refunds f ON f.return_id = r.return_id
  LEFT JOIN {catalog}.silver.payments p ON p.order_id = i.order_id
  WHERE i.customer_id = get_customer_context.customer_id
  ORDER BY i.placed_at DESC, i.item_id
  LIMIT 200
;
CREATE OR REPLACE FUNCTION {catalog}.agent_api.find_order_items(
  customer_id STRING COMMENT 'Authenticated customer id. Supplied by the system, never taken from the conversation.',
  item_query STRING COMMENT 'Short description of the item the customer mentioned, e.g. "blue jacket" or "running shoes".'
)
RETURNS TABLE (
  item_id STRING, order_id STRING, placed_on DATE, title STRING, color STRING, size STRING, price DOUBLE,
  item_status STRING, match_score INT, as_of DATE
)
COMMENT 'Finds which of the authenticated customer''s ordered items match a description such as "blue jacket". Returns only the best-matching items (several rows means the description is ambiguous; no rows means nothing on this account matches). Searches this customer''s orders only.'
RETURN
  WITH words AS (
    -- Lower-case words of 3+ letters, minus filler words; a trailing "s" is dropped so plurals match ("chinos").
    SELECT transform(
             filter(
               split(regexp_replace(lower(find_order_items.item_query), '[^a-z0-9 ]', ' '), ' +'),
               w -> length(w) >= 3 AND NOT array_contains(
                 array('the', 'and', 'for', 'my', 'where', 'what', 'refund', 'order', 'return', 'item', 'one'), w)
             ),
             w -> CASE WHEN length(w) > 3 THEN regexp_replace(w, 's$', '') ELSE w END
           ) AS w
  ),
  scored AS (
    SELECT i.*, size(filter(words.w, x -> i.search_text LIKE concat('%', x, '%'))) AS score
    FROM {catalog}.gold.order_items_enriched i
    CROSS JOIN words
    WHERE i.customer_id = find_order_items.customer_id
  ),
  best AS (SELECT max(score) AS top FROM scored)
  SELECT s.item_id, s.order_id, to_date(s.placed_at), s.title, s.color, s.size, s.price, s.item_status, s.score,
         (SELECT to_date(value) FROM {catalog}.silver.config WHERE key = 'reference_date')
  FROM scored s CROSS JOIN best
  WHERE s.score > 0 AND s.score = best.top
  ORDER BY s.placed_at DESC
;
CREATE OR REPLACE FUNCTION {catalog}.agent_api.get_case_timeline(
  customer_id STRING COMMENT 'Authenticated customer id. Supplied by the system, never taken from the conversation.',
  order_id STRING COMMENT 'Order id from get_customer_context or find_order_items, e.g. "ORD-S01-1".'
)
RETURNS TABLE (event_at TIMESTAMP, event_type STRING, item_id STRING, detail STRING, as_of DATE)
COMMENT 'Chronological history of one of the authenticated customer''s orders: placement, every carrier scan, return label and warehouse scan, refund steps and support contacts. Use it to explain exactly what happened and when. Returns nothing if the order does not belong to this customer.'
RETURN
  SELECT t.event_at, t.event_type, t.item_id, t.detail,
         (SELECT to_date(value) FROM {catalog}.silver.config WHERE key = 'reference_date')
  FROM {catalog}.gold.customer_timeline t
  WHERE t.customer_id = get_case_timeline.customer_id
    AND t.order_id = get_case_timeline.order_id
  ORDER BY t.event_at, t.event_type
;
CREATE OR REPLACE FUNCTION {catalog}.agent_api.get_stuck_cases(
  customer_id STRING COMMENT 'Authenticated customer id. Supplied by the system, never taken from the conversation.'
)
RETURNS TABLE (
  case_type STRING, case_id STRING, order_id STRING, item_id STRING, diagnosis STRING, case_state STRING,
  last_activity_at TIMESTAMP, days_since_activity INT,
  refund_status STRING, refund_amount DOUBLE, refund_failure_reason STRING, as_of DATE
)
COMMENT 'Lists the authenticated customer''s returns, refunds and shipments that are stuck and need action. diagnosis is one of: stuck_refund (return received at the warehouse but no refund completed in time), refund_failed (refund rejected by the payment provider), stuck_shipment (no carrier movement for several days). No rows means nothing is stuck.'
RETURN
  WITH cfg AS (SELECT to_date(value) AS as_of FROM {catalog}.silver.config WHERE key = 'reference_date')
  SELECT c.case_type, c.case_id, c.order_id, c.item_id, c.diagnosis, c.case_state,
         c.last_activity_at, datediff(cfg.as_of, to_date(c.last_activity_at)),
         c.refund_status, c.refund_amount, c.failure_reason, cfg.as_of
  FROM {catalog}.gold.case_status c
  CROSS JOIN cfg
  WHERE c.customer_id = get_stuck_cases.customer_id
    AND c.diagnosis IS NOT NULL
  ORDER BY c.last_activity_at
;
