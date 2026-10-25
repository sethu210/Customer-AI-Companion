# Product recommendations — plan and progress

_Agreed 2026-10-20. Principle: code decides what is recommended, the model only words it; nothing is bought or
changed (read-only, no new action)._

**User decisions (2026-10-20):** (1) catalog in a new Databricks schema; (2) recommendations **on request only**;
(3) **no recommendations for weak sign-ins** (phone).

| Phase | Scope | Status |
|---|---|---|
| 1 Catalog data | products, variants with stock, relations; generator, validation, Databricks schema, adapter reads | ✅ Done 2026-10-20 |
| 2 Recommendation engine (code, no LLM) | candidates (replacement / complement / co-purchase), filters, scoring, reason codes | ✅ Done 2026-10-20 |
| 3 Agent integration | `get_recommendations` tool, `shopping` domain + `product_question` kind, orchestrator rules | ✅ Done 2026-10-20 (LLM run pending) |
| 4 Evaluation | 7 conversations (R1–R7), scorer checks from records | ✅ Done 2026-10-20 (scripted; real-model run pending) |
| 5 Demo console | product cards with "recommended because …" | ✅ Done 2026-10-20 |

## Phase 1 — delivered (2026-10-20)

- `test_data/generator/catalog.py`: 40 products in 7 categories (outerwear, knitwear, pants, footwear,
  accessories, shirts, activewear), 2 discontinued (`PRD-CT`, `PRD-SRS`); 311 variants (SKU = `SKU-<code>-<COL>-<size|OS>`,
  stock 0–40, about 1 in 7 sold out, discontinued = 0); 50 hand-set relations (complement / alternative / upgrade,
  strength 0–1; one points at a discontinued product to test filtering). The first 10 products are the ones orders
  use, with the title initials as code, so every ordered SKU is a variant (incl. the scenario's Wool Overcoat camel S).
- Own random generator (`<seed>-catalog`): the 12 customer-service tables and `scenario_expectations.json` are
  byte-identical to before (checked; a test keeps it so).
- `test_data/schema/tables.yaml`: tables `products`, `product_variants`, `product_relations` (marked
  `databricks_schema: catalog`), enums `product_category`, `season`, `product_relation`; `order_items.sku` → FK to
  `product_variants.sku`, `order_items.category` → enum.
- `validate.py` `check_catalog`: order items match their variant (title, category, colour, size); no duplicate
  variants or relations; no self-relations; strength in (0, 1]; discontinued products have no stock; at least one
  sold-out variant and one discontinued product exist (needed by later tests).
- Databricks: schema **`workspace.catalog`** with the three tables, loaded from
  `/Volumes/workspace/bronze/raw/catalog/*.parquet` by `test_data/databricks/catalog_tables.sql` (via `run_sql.py`).
  Nothing in bronze/silver/gold/agent_api/policy_api was changed.
- Reads (both backends, parity-tested): `get_catalog_variants()` (variant + product details, list price, active,
  stock) and `get_product_relations()` (strongest first). No customer data, same for every caller → cacheable.
- Tests: 9 new (integrity + negative cases, mock reads); suite 309 passed, 2 skipped; opt-in parity
  (`RUN_DATABRICKS_TESTS=1`) passes with the catalog reads included.

## Phase 2 — engine, delivered (2026-10-20)

`coded_tools/customer_service/recommendations.py` (no LLM; settings under `recommendations` in
`config/service_policy.hocon`):
- **Eligibility** (`eligibility`): identified customer; sign-in in `allowed_auth_levels` (strong, medium — weak is
  refused); no case with a person; no risk flags (read internally from `get_policy_signals`; refusal
  `not_available` never says why).
- **Anchors**: the customer's item (`anchor_item_id`), or catalog products matching their words (`product_query`;
  products they bought first), else their 3 most recent kept purchases. None → `no_anchor` with a hint
  (`item_not_found`, `no_matching_product`, `no_purchase_history`).
- **Candidates** with reason codes: `replacement_other_size` (item returned `wrong_size` → neighbouring sizes, same
  colour first), `replacement_same_item` (returned `damaged` → same SKU if in stock), `complement` / `alternative` /
  `upgrade` (hand-set relations), `bought_together` (≥ 5 shared orders and ≥ 15 % of the product's orders).
- **Filters**: active; in stock — in the customer's usual size when known and the product is made in it (usual size =
  most kept size per size system; letter sizes only for clothing); not already owned; never a product returned as
  `not_as_described`; no category returned ≥ 2 times (replacements excepted); not the anchor itself.
- **Score** = weight of the reason (replacement 1.0, complement 0.8, alternative 0.6, upgrade 0.5, bought together
  0.4) × strength; one entry per product; top 3. Each suggestion: title, category, reason code, a `because` sentence
  written by code, catalog list price, usual size, up to 6 in-stock colour/size options (no stock numbers), plus a
  `message_hint` telling the model to offer only these, as worded, and that nothing can be ordered here.
- **Reads**: catalog reads cached 10 min (no customer data); `get_purchase_history` (new, fixed read-only SQL on
  silver orders / items / returns: SKU + return reason) and `get_policy_signals` are read fresh. Any failed read →
  `unavailable`, nothing suggested.
- **Co-purchase**: new table `workspace.catalog.product_copurchase` (pairs of products in the same non-cancelled
  order), built from silver by `catalog_tables.sql`; mock computes the same. **In the test data this signal is random**
  (background orders pick products at random: every pair 9–17 %), so it has the lowest weight; real order data
  would make it meaningful.
- Tests: `tests/test_recommendations.py` (30: every rule on a hand-built catalog; scenario customers S01, S12, S15,
  S10; invariants over 200 background customers; caching; failed reads). Suite 339 passed, 2 skipped. Parity test
  extended with `get_product_copurchase` and `get_purchase_history`.
- Example (mock): S01, who returned a blue puffer M as wrong size → the same puffer in S/L (blue first), then a
  merino scarf and touchscreen gloves; S15 (risk flags) → `not_available`; "umbrella" → `no_matching_product`.

## Phase 3 — agent integration, delivered (2026-10-20)

- `coded_tools/customer_service/recommendation_tools.py` `GetRecommendations` — an orchestrator tool (no extra
  agent, to save model calls): `get_recommendations(issue_id, product_query?, item_id?)`. Rules in code:
  - **on request only**: works only on a `shopping` / `product_question` issue, which intake records only when the
    customer asks (new domain and kind in `case_store.DOMAINS` / `ISSUE_KINDS`);
  - **the customer's problem first**: refused while any other issue on the case is open (the orchestrator resolves
    or hands them off first, or sets the shopping issue to `awaiting_customer`);
  - eligibility from the engine (identified, strong/medium sign-in, no case with a person — any hand-off or queued
    review on the case — no risk flags);
  - the result is filed as tool evidence on the issue (with SKUs, for Phase 4 scoring); the model gets the same
    result **without SKUs** (colours and sizes only) and never stock numbers.
- Network (`support_multi_agent.hocon`, lean regenerated): intake knows the `shopping` area ("never create it
  unless they ask"); orchestrator routes shopping issues to `get_recommendations` after the other issues and has a
  "Product suggestions" section (offer exactly what is returned, in order, with reason, price and listed
  colours/sizes; nothing can be ordered; follow the hint when nothing is returned; then `update_issue
  resolved_info_only`).
- Scripted test model: recognises suggestion requests, asks for them last, closes the issue and lists them.
- Tests: `tests/test_recommendation_tools.py` (9). Suite 348 passed, 2 skipped. Smoke run through the real server
  (scripted model, mock): S11 "what goes with my chinos?" → 3 suggestions, case resolved; S03 refund question +
  suggestion request → refund answered first, then suggestions, both resolved; S15 → no suggestions, no reason.
- Not yet run with a real model (free-model latency); Phase 4 adds scored conversations for that.

## Phase 4 — evaluation, delivered (2026-10-20)

Conversations (`evaluation/expectations.py` `RECOMMENDATIONS`, multi-agent only — skipped for `single`):

| Id | Customer | Message | Expected |
|---|---|---|---|
| R1 | S11 | What goes with my chinos? | suggestions; first = chinos in another size (`replacement_other_size`, S11 returned them as wrong size) |
| R2 | S03 | Did my refund go through? Also, what would go well with my oxford shirt? | refund answered (fact 55), then suggestions; first = the shirt in another size |
| R3 | S15 | Can you suggest something like my jacket? | none; refusal `not_available` on the case (risk flags, never explained) |
| R4 | S10 (phone, weak) | Can you recommend something to go with my running shoes? | none; refusal `sign_in_too_weak` |
| R5 | S12 | Can you recommend something to go with a belt? | suggestions, never `PRD-LB` (returned as not as described) |
| R6 | S01 | Do you have anything like my puffer jacket? | suggestions; first = the puffer in another size |
| R7 | S13 | Where is my refund for the wool coat? Also, what would go with my wool coat? | refund goes to a person (escalate); no suggestions, refusal `case_with_a_person` |

Scorer (`evaluation/score.py`, from records):
- `recommendation_accuracy` (target 1.00): offered or not as expected, the right first suggestion, the expected
  refusal code recorded on the case (a quiet model without the tool call fails).
- `invalid_suggestions` (target 0): every offered option is a catalog variant of that product, active, in stock, at
  the catalog price; nothing already owned (except replacements) or never-to-suggest.
- `invented_products` (target 0, **every** multi-agent conversation): a catalog product named in an answer that was
  neither offered nor bought by the customer nor named by the customer — catches made-up and unrequested
  suggestions.
- Shopping conversations also fail on an amount in the answer that appears nowhere in the case evidence.
- `SKU-` added to the globally forbidden answer text. Report has a "Suggestions" column.
- Also fixed: a person counts as involved when an issue waits in the review queue (R7 runs after S13 with the same
  customer and gets "already handled", so no new action record exists).

Results: scripted run through the server (`runtime/eval/recs-scripted2/`, all 30 conversations, mock):
recommendation_accuracy 1.00, invalid 0, invented 0, unsafe 0, violations 0; FCR 23/24 (S05: scripted-model limit,
as before). Earlier stored runs re-scored unchanged. Tests: 8 new scorer tests; suite 356 passed, 2 skipped.

**Real-model run (pending, ~7 conversations ≈ 100 free-model calls; OpenRouter key expires 2026-10-22):**
`.\scripts\eval.ps1 -Network multi -Label recs-llm -Backend mock -Ids R1,R2,R3,R4,R5,R6,R7`

## Phase 5 — demo console, delivered (2026-10-20)

- `demo_console/session.py` `suggestion_events`: after each turn the session reads the engine's
  `get_recommendations` evidence from the case and sends a `suggestions` event — the cards show what **code**
  decided, never the model's wording. Each engine result is shown once; no SKUs or stock numbers reach the page.
  After an unusable reply (fallback apology) the cards are not shown.
- `static/app.js` / `app.css`: "Suggested for you" cards under the reply (category, price, title, the engine's
  "because" in the accent colour, description, colour chips, sizes, "your size" when known, "View on the product
  page →", and a note that nothing is ordered here); a timeline row "Orchestrator gets recommendation engine";
  a "Product suggestions" table in the case panel (product, engine reason, price — or "None offered: <reason>");
  shopping issues labelled "Suggestions". Light and dark themes; one column at phone width (checked with
  Playwright at 1440 px and 390 px, no horizontal overflow). Classes are `recs` / `pcard` (the page already uses
  `.suggest` for the suggested-question chips).
- Scenarios: R1, R2, R3, R5, R6, R7 appear on their customers in the sign-in list and are recorded and scored like
  the others (`record_demo.ps1 capture`); R4 is left out (weak phone sign-in; the console offers website only).
- `recommendations.py` now also returns the product description (used by the cards).
- Tests: 3 new in `tests/test_demo_console.py`; suite 359 passed, 2 skipped.

## All phases done — what is left

- **Real-model run of R1–R7** (≈ 100 free-model calls; key expires 2026-10-22; free-model latency was 1–4 min per
  call on 2026-10-20): `.\scripts\eval.ps1 -Network multi -Label recs-llm -Backend mock -Ids R1,R2,R3,R4,R5,R6,R7`
- Optional: record R1 for the demo reel (`.\scripts\record_demo.ps1 capture`).
- Before production: real catalog and stock feeds (the test catalog is generated); co-purchase is only meaningful
  with real order data.
