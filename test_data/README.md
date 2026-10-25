# Test data

One seeded generator feeds both the mock adapter (reads `out/parquet/`) and Databricks (loads the same files).

```powershell
.\.venv\Scripts\python.exe -m test_data.generator.generate   # writes out/ (12 tables + 7 catalog tables, ~18.0k rows)
.\.venv\Scripts\python.exe -m test_data.generator.validate   # integrity + scenario checks, summary only
.\.venv\Scripts\python.exe -m pytest                         # same checks + negative tests
```

| Path | What |
|---|---|
| `schema/tables.yaml` | Columns, types, allowed values, foreign keys (single source of truth) |
| `generator/settings.yaml` | Seed, reference date, background size, policy thresholds |
| `scenarios/sNN_*.yaml` | Hand-written test cases with expected outcomes |
| `generator/rules.py` | Reference "stuck" rules; Databricks `gold.case_status` must agree |
| `out/` | Generated: `parquet/`, `csv/`, `manifest.json`, `scenario_expectations.json` |
| `databricks/load_customer_service.py` | Notebook: bronze → silver → gold, then scenario parity check |
| `databricks/uc_functions.sql` | Agent tools as UC functions in `agent_api` (exposed by the managed MCP server) |
| `databricks/run_sql.py` | Applies a SQL file through the Statement API using the CLI login, with per-statement timing |
| `generator/catalog.py` | Product catalog for recommendations: 46 products (38 on sale, 2 discontinued, 6 staged), 358 variants with stock, 84 relations; plus the stylist data (Everly plan P4): 6 staged launches, 92 style tags, 25 colour families, 21 harmony pairs. Own random seed, so the other tables never change |
| `databricks/catalog_tables.sql` | Loads the catalog into its own schema `<catalog>.catalog` (products, product_variants, product_relations, product_launches, product_styles, colour_families, colour_harmony) from `raw/catalog/` |

**Product catalog:** every `order_items.sku` is a catalog variant (validated). Products: 38 on sale, 2 discontinued
(inactive, no stock) and 6 staged for launch (inactive, with stock and a `product_launches` row 2-21 days after the
reference date; recommendations skip them until launched). Every product has 1-3 style tags; every colour a family;
`colour_harmony` holds one row per pair of families (validated by `check_stylist`). Load or reload it without touching
the customer-service tables: upload the seven catalog parquet files (`products`, `product_variants`,
`product_relations`, `product_launches`, `product_styles`, `colour_families`, `colour_harmony`) to
`/Volumes/workspace/bronze/raw/catalog/`, then
`python -m test_data.databricks.run_sql --file test_data/databricks/catalog_tables.sql --cli <path-to-databricks.exe>`
(commands at the top of the SQL file).

**Agent tools** (`/api/2.0/mcp/functions/workspace/agent_api`): `get_customer_profile`, `get_customer_context`,
`find_order_items`, `get_case_timeline`, `get_stuck_cases`. All take `customer_id`, which the agent's coded tool
must fill from `sly_data`, never from the LLM. Re-apply after edits:
`python -m test_data.databricks.run_sql --file test_data/databricks/uc_functions.sql --cli <path-to-databricks.exe>`

**MCP latency on Free Edition:** the first function call of an MCP session can hit a 90 s
"provisioning resources for function execution" timeout; later calls in the same session take 2-6 s.
Keep one long-lived session, warm it up, and fall back to the Statement API (1-5 s) on timeout.

**Adding a test case:** write `scenarios/sNN_name.yaml`, regenerate, validate.
Dates are `*_days_ago` relative to `reference_date`; IDs are derived (`CUST-S05` → `ORD-S05-2` → `ITM-S05-2-1`).

**Loading Databricks:** import the notebook, run Step 1, upload `out/parquet/*.parquet`, `out/manifest.json`
and `out/scenario_expectations.json` to `/Volumes/<catalog>/bronze/raw`, then run all cells.

Free Edition cannot create catalogs from the CLI/SQL, so the data lives in the **`workspace`** catalog
(`workspace.bronze|silver|gold|agent_api`, volume `/Volumes/workspace/bronze/raw`); run the notebook with `catalog=workspace`.
Reload from the CLI:

```powershell
databricks fs cp test_data\out\parquet\<table>.parquet dbfs:/Volumes/workspace/bronze/raw/<table>.parquet --overwrite
databricks jobs submit --json "@submit_load.json"   # notebook_task with base_parameters {catalog: workspace}
```
