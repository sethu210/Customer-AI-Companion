# Databricks notebook source
# MAGIC %md
# MAGIC # Load customer-service test data (bronze → silver → gold)
# MAGIC
# MAGIC 1. Run **Step 1** once to create the catalog, schemas and the upload volume.
# MAGIC 2. Upload everything from local `test_data/out/parquet/` **plus** `test_data/out/manifest.json` and
# MAGIC    `test_data/out/scenario_expectations.json` into the volume shown by Step 1 (Catalog Explorer → volume → Upload).
# MAGIC 3. Run all remaining cells. Re-running is safe: every table is fully replaced.
# MAGIC
# MAGIC All "stuck" rules compare against `config.reference_date`, never the wall clock, and must stay in sync with
# MAGIC `test_data/generator/rules.py`. The last cell checks every scenario's expected diagnosis.

# COMMAND ----------

dbutils.widgets.text("catalog", "customer_service")
CATALOG = dbutils.widgets.get("catalog")
spark.conf.set("spark.sql.session.timeZone", "UTC")

TABLES = {
    "customers": "customer_id",
    "channel_identities": "identity_id",
    "orders": "order_id",
    "shipments": "shipment_id",
    "order_items": "item_id",
    "shipment_events": "event_id",
    "returns": "return_id",
    "refunds": "refund_id",
    "payments": "payment_id",
    "loyalty": "customer_id",
    "support_contacts": "contact_id",
    "config": "key",
}

# COMMAND ----------

# MAGIC %md ## Step 1 - catalog, schemas, upload volume

# COMMAND ----------

if CATALOG not in [r[0] for r in spark.sql("SHOW CATALOGS").collect()]:
    try:
        spark.sql(f"CREATE CATALOG {CATALOG}")
    except Exception as e:  # Free Edition only creates catalogs through the UI; use `workspace` instead.
        raise RuntimeError(
            f"Could not create catalog {CATALOG!r}: {e}\nSet the 'catalog' widget to 'workspace' and re-run."
        ) from e

for schema in ("bronze", "silver", "gold", "agent_api"):
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{schema}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.bronze.raw")

RAW = f"/Volumes/{CATALOG}/bronze/raw"
print(f"Upload the parquet files, manifest.json and scenario_expectations.json to: {RAW}")

# COMMAND ----------

# MAGIC %md ## Step 2 - bronze: raw copy of the uploaded files

# COMMAND ----------

import json

uploaded = {f.name for f in dbutils.fs.ls(RAW)}
missing = [f"{t}.parquet" for t in TABLES if f"{t}.parquet" not in uploaded]
missing += [f for f in ("manifest.json", "scenario_expectations.json") if f not in uploaded]
if missing:
    raise RuntimeError(f"Upload these files to {RAW} first: {missing}")

manifest = json.loads(dbutils.fs.head(f"{RAW}/manifest.json", 1_000_000))

from pyspark.sql import functions as F

for table in TABLES:
    (
        spark.read.parquet(f"{RAW}/{table}.parquet")
        .withColumn("_ingested_at", F.current_timestamp())
        .write.mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{CATALOG}.bronze.{table}")
    )
print(f"bronze loaded: {len(TABLES)} tables (seed {manifest['seed']}, reference date {manifest['reference_date']})")

# COMMAND ----------

# MAGIC %md ## Step 3 - silver: typed, trimmed, de-duplicated, row counts checked against the manifest

# COMMAND ----------

problems = []
for table, pk in TABLES.items():
    df = spark.table(f"{CATALOG}.bronze.{table}").drop("_ingested_at")
    for name, dtype in df.dtypes:
        if dtype == "string":
            df = df.withColumn(name, F.trim(F.col(name)))
    if table == "customers":
        df = df.withColumn(
            "risk_flags",
            F.when(F.col("risk_flags").isNull(), F.array().cast("array<string>")).otherwise(F.split("risk_flags", ";")),
        )
    df = df.dropDuplicates([pk])
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CATALOG}.silver.{table}")

    actual = spark.table(f"{CATALOG}.silver.{table}").count()
    expected = manifest["tables"][table]["rows"]
    if actual != expected:
        problems.append(f"{table}: {actual} rows, manifest says {expected}")

if problems:
    raise RuntimeError("Row count mismatch:\n" + "\n".join(problems))
print("silver loaded; all row counts match the manifest")

# COMMAND ----------

# MAGIC %md ## Step 4 - gold

# COMMAND ----------

spark.sql(f"""
CREATE OR REPLACE TABLE {CATALOG}.gold.order_items_enriched AS
SELECT
  o.customer_id, o.order_id, o.placed_at, o.status AS order_status, o.currency,
  i.item_id, i.sku, i.title, i.color, i.category, i.size, i.qty, i.price, i.item_status,
  s.shipment_id, s.carrier, s.tracking_no, s.status AS shipment_status,
  lower(concat_ws(' ', i.color, i.title, i.category)) AS search_text
FROM {CATALOG}.silver.order_items i
JOIN {CATALOG}.silver.orders o ON o.order_id = i.order_id
LEFT JOIN {CATALOG}.silver.shipments s ON s.shipment_id = i.shipment_id
""")

spark.sql(f"""
CREATE OR REPLACE TABLE {CATALOG}.gold.customer_timeline AS
SELECT customer_id, order_id, CAST(NULL AS STRING) AS item_id, 'order_placed' AS event_type, placed_at AS event_at,
       concat('total ', total, ' ', currency) AS detail
FROM {CATALOG}.silver.orders
UNION ALL
SELECT o.customer_id, o.order_id, NULL, concat('shipment_', e.event_type), e.event_at,
       concat(s.carrier, ' ', s.tracking_no, ' @ ', e.location)
FROM {CATALOG}.silver.shipment_events e
JOIN {CATALOG}.silver.shipments s ON s.shipment_id = e.shipment_id
JOIN {CATALOG}.silver.orders o ON o.order_id = s.order_id
UNION ALL
SELECT o.customer_id, o.order_id, r.item_id, 'return_label_created', r.label_created_at, r.reason
FROM {CATALOG}.silver.returns r
JOIN {CATALOG}.silver.order_items i ON i.item_id = r.item_id
JOIN {CATALOG}.silver.orders o ON o.order_id = i.order_id
UNION ALL
SELECT o.customer_id, o.order_id, r.item_id, 'return_warehouse_scanned', r.warehouse_scanned_at, r.status
FROM {CATALOG}.silver.returns r
JOIN {CATALOG}.silver.order_items i ON i.item_id = r.item_id
JOIN {CATALOG}.silver.orders o ON o.order_id = i.order_id
WHERE r.warehouse_scanned_at IS NOT NULL
UNION ALL
SELECT o.customer_id, f.order_id, NULL, 'refund_initiated', f.initiated_at, concat(f.amount, ' via ', f.method)
FROM {CATALOG}.silver.refunds f JOIN {CATALOG}.silver.orders o ON o.order_id = f.order_id
UNION ALL
SELECT o.customer_id, f.order_id, NULL, 'refund_completed', f.completed_at, concat(f.amount, ' via ', f.method)
FROM {CATALOG}.silver.refunds f JOIN {CATALOG}.silver.orders o ON o.order_id = f.order_id
WHERE f.completed_at IS NOT NULL
UNION ALL
SELECT o.customer_id, f.order_id, NULL, 'refund_failed', f.initiated_at, f.failure_reason
FROM {CATALOG}.silver.refunds f JOIN {CATALOG}.silver.orders o ON o.order_id = f.order_id
WHERE f.status = 'failed'
UNION ALL
SELECT customer_id, order_id, NULL, concat('support_', channel), created_at, topic
FROM {CATALOG}.silver.support_contacts
""")

# A view, so the rules always use the current config values. Mirrors test_data/generator/rules.py.
spark.sql(f"""
CREATE OR REPLACE VIEW {CATALOG}.gold.case_status AS
WITH cfg AS (
  SELECT
    to_timestamp(max(CASE WHEN key = 'reference_date' THEN value END)) AS ref,
    CAST(max(CASE WHEN key = 'stuck_refund_days' THEN value END) AS INT) AS stuck_refund_days,
    CAST(max(CASE WHEN key = 'stuck_shipment_days' THEN value END) AS INT) AS stuck_shipment_days
  FROM {CATALOG}.silver.config
),
return_cases AS (
  SELECT
    o.customer_id, o.order_id, r.item_id, 'return' AS case_type, r.return_id AS case_id,
    r.status AS case_state, r.warehouse_scanned_at AS last_activity_at,
    f.refund_id, f.status AS refund_status, f.amount AS refund_amount, f.failure_reason,
    CASE
      WHEN f.status = 'failed' THEN 'refund_failed'
      WHEN r.warehouse_scanned_at IS NOT NULL
       AND r.warehouse_scanned_at <= cfg.ref - make_interval(0, 0, 0, cfg.stuck_refund_days)
       AND (f.status IS NULL OR f.status IN ('pending', 'processing')) THEN 'stuck_refund'
    END AS diagnosis
  FROM {CATALOG}.silver.returns r
  JOIN {CATALOG}.silver.order_items i ON i.item_id = r.item_id
  JOIN {CATALOG}.silver.orders o ON o.order_id = i.order_id
  LEFT JOIN {CATALOG}.silver.refunds f ON f.return_id = r.return_id
  CROSS JOIN cfg
),
shipment_cases AS (
  SELECT
    o.customer_id, o.order_id, CAST(NULL AS STRING) AS item_id, 'shipment' AS case_type, s.shipment_id AS case_id,
    s.status AS case_state, max(e.event_at) AS last_activity_at,
    CAST(NULL AS STRING) AS refund_id, CAST(NULL AS STRING) AS refund_status,
    CAST(NULL AS DOUBLE) AS refund_amount, CAST(NULL AS STRING) AS failure_reason,
    CASE
      WHEN s.status <> 'delivered' AND o.status <> 'cancelled'
       AND max(e.event_at) <= max(cfg.ref) - make_interval(0, 0, 0, max(cfg.stuck_shipment_days)) THEN 'stuck_shipment'
    END AS diagnosis
  FROM {CATALOG}.silver.shipments s
  JOIN {CATALOG}.silver.orders o ON o.order_id = s.order_id
  JOIN {CATALOG}.silver.shipment_events e ON e.shipment_id = s.shipment_id
  CROSS JOIN cfg
  GROUP BY o.customer_id, o.order_id, s.shipment_id, s.status, o.status
)
SELECT * FROM return_cases
UNION ALL
SELECT * FROM shipment_cases
""")
print("gold built: order_items_enriched, customer_timeline, case_status (view)")

# COMMAND ----------

# MAGIC %md ## Step 5 - verify every scenario's expected diagnosis against gold.case_status

# COMMAND ----------

expectations = json.loads(dbutils.fs.head(f"{RAW}/scenario_expectations.json", 1_000_000))
actual = {
    r["customer_id"]: sorted(r["codes"])
    for r in spark.sql(f"""
        SELECT customer_id, collect_set(diagnosis) AS codes
        FROM {CATALOG}.gold.case_status WHERE diagnosis IS NOT NULL GROUP BY customer_id
    """).collect()
}
failures = []
for exp in expectations:
    got = actual.get(exp["customer"], [])
    if got != sorted(exp.get("diagnosis", [])):
        failures.append(f"{exp['id']}: expected {sorted(exp.get('diagnosis', []))}, gold.case_status gives {got}")

background_stuck = [cid for cid in actual if cid.startswith("CUST-B")]
if background_stuck:
    failures.append(f"background customers unexpectedly stuck: {background_stuck[:5]}")

if failures:
    raise AssertionError("Scenario parity failed:\n" + "\n".join(failures))
print(f"All {len(expectations)} scenario diagnoses match gold.case_status")
