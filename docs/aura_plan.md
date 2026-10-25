# Aura — "New In" affinity stylist (E5) — plan

Status: **superseded** (2026-10-21) by `docs/companion_plan.md`: Aura is now the companion's Personal Stylist
Mode, open to every customer who opts in. The scoring, gates, data and evaluation below still apply there.

## 1. What Aura does

When a new product is launched, Aura:

1. takes the **premium customers** (definition: decision D1);
2. for each one, scores how well the new item **goes with what they already own and kept** (their wardrobe);
3. sends a personal "New In, picked for you" invitation **only when the affinity score is 80 or more** (out of 100);
4. writes in the invite *why* it suits them, e.g. "goes with the navy wool overcoat you bought in March, in your
   usual size M";
5. answers in the chat when the customer comes back about the invite, like a personal stylist.

Most premium customers will get nothing for most launches. That is the point.

## 2. Design principles (same as Guardian and the recommendations engine)

- **Code decides; the model only talks.** Who is eligible, the score, the 80 threshold, consent and limits are all
  deterministic code. No LLM is involved in choosing who gets an invite.
- **Explainable score.** Every invite stores its score breakdown and the owned items it is based on. A reviewer can
  see why a customer was invited.
- **Templates for the invite text.** Facts filled in by code. No invented prices, stock or discounts.
- **Read-only on Databricks.** Any new data goes into a new schema or into local files (decision D2). The existing
  bronze, silver, gold and catalog tables stay as they are.
- **Reuse:** the existing outbox and Inbox tab (from the Guardian), the catalog and relations (from the
  recommendations engine), the usual-size logic, quiet hours.

## 3. How the affinity score works (proposed)

Score 0–100, computed per (customer, new product). It starts with **hard gates**: if any one fails, there is no
score and no invite.

| Gate | Rule |
|---|---|
| Premium | tier in the premium set (D1) |
| Consent | the customer has opted in to marketing / New In messages (data needed, §5) |
| Risk | no risk flags (same rule as recommendations) |
| No open trouble | no open case with a person, and no escalated or failed Guardian watch: never market to someone waiting on a complaint |
| Size available | the new item is in stock in the customer's usual size for that category (or it is one-size) |
| Not owned | they don't already have this product |
| Not a rejected category | no returns of this category as `not_as_described`, and fewer than 2 returns of the category |
| Frequency | no Aura invite in the last N days (D5), and not already invited for this product |

It then adds up **weighted signals**, each 0–1. The weights live in `service_policy.hocon` under `aura`:

| Signal | Weight | Meaning | Source |
|---|---|---|---|
| Wardrobe complement | 35 | strongest `complement` relation between the new item and any kept item | product relations of the new item (new data) |
| Style match | 20 | style tags shared with what they own and kept (e.g. smart-casual, minimal, outdoor) | style tags per product (new data) |
| Colour harmony | 15 | the new item's colours go with the colours they own (colour-family rules) | colour family per variant + a small harmony table (new data) |
| Category affinity | 10 | how much they buy in this or adjacent categories, and kept, not returned | order_items (exists) |
| Price comfort | 10 | list price within the range they usually pay | order_items.price (exists) |
| Season fit | 10 | item season matches the launch date and what they buy | products.season (exists) |

Score = sum of weight × signal. **Invite if the score is 80 or more** (`aura.min_affinity: 80`).

**Note on "80%":** this is a rule-based score, not a probability that the customer will like the item. It is honest
and explainable, but it can only be *calibrated* once there is feedback data (opens, clicks, purchases; §5
"later"). Until then, the evaluation checks that the threshold gives sensible numbers of invites and that every
invite has a clear reason (§7).

## 4. Components

| # | Component | What |
|---|---|---|
| 1 | `coded_tools/customer_service/aura.py` | the affinity engine (gates, signals, score, breakdown), pure code with unit tests |
| 2 | Launch runner (`aura run --launch <product_id>`) | reads the launch, lists premium customers, scores each one, writes invites. Idempotent: running a launch twice sends nothing new |
| 3 | Aura store (case-store DB) | `aura_launches`, `aura_scores` (every score with its breakdown, invited or not), `aura_invites` (dedupe on customer + product) |
| 4 | Delivery | invites go to the existing **outbox** with `source: "aura"`. Quiet hours and a daily cap shared with the Guardian, so a customer never gets more than 2 messages a day in total. Opt-out from the invite ("Stop New In picks") |
| 5 | Aura agent (`new_in_stylist`, display name **Aura**) | a new specialist in the network. When the customer asks about an invite ("tell me about the new jacket you sent"), the orchestrator routes to Aura. Aura calls `get_new_in_invite` (code returns the invite, the score reasons and options in their size) and presents it. It cannot order or change anything. The model never sees other customers or scores below the threshold |
| 6 | Demo console | a **"Launch"** panel: pick a staged new product → *Launch* → a table of premium customers with score, gates and breakdown (green ≥ 80). Invited customers see the invite in their **Inbox** tab with a "Look at it" button that opens a chat with Aura |
| 7 | Read-only MCP (E2) | `case_summary` / a new `new_in_picks` view, so a customer's assistant can see their invites |
| 8 | Evaluation (`evaluation/aura_eval.py`) | scenarios A1–A10 (§7), scripted model first |

## 5. Data — what exists and what is needed

**Already available (no change needed):**

- `customers.tier`: standard 341, silver 107, **gold 57, platinum 18**.
- `customers.risk_flags`.
- `order_items`: SKU, title, colour, category, size, price and status, i.e. the wardrobe and what was kept or
  returned.
- `returns.reason`.
- The catalog: `products` (category, material, fit, season, price, active), `product_variants` (colour, size,
  stock) and `product_relations` (complement / alternative / upgrade, strength).
- Case store and Guardian watches (for the "no open trouble" gate).

**Additional data needed:**

| # | Data | Why | Must / nice | Proposed source for the demo |
|---|---|---|---|---|
| N1 | **New products to launch:** product rows with `launched_at` (or `status: new`) and their variants with stock | the trigger; there are no "new" products today, all 40 are existing | Must | 4–6 staged new products (e.g. a cashmere scarf, a waxed field jacket, suede loafers, a linen overshirt), with variants and stock |
| N2 | **Relations for the new products:** which existing products each new item complements, with strength | wardrobe complement signal, the main reason for an invite | Must | hand-set rows like the existing 50 relations (merchandiser-style) |
| N3 | **Style tags per product** (2–4 from a fixed list: smart, casual, minimal, outdoor, sport, classic, statement…) | style match signal | Must | tags for the 40 existing + the new products |
| N4 | **Colour family per colour** (navy → blue/neutral, camel → earth…) + a small **harmony table** (which families go together) | colour harmony signal | Must | one small lookup table (~20 colours, ~8 families) |
| N5 | **Marketing consent per customer** (opted in to New In / personal picks, date, channel) | legally required before any promotional message (GDPR / PECR in the UK); service updates from the Guardian do not need it, marketing does | Must | a consent table with a mix of yes/no for premium customers; demo toggle |
| N6 | **Launch event:** product id + launch time | starts the run | Must | the console's Launch button / `aura launch` CLI, recorded in the Aura store (like the Guardian's simulator) |
| N7 | Customer **stated style preferences / sizes profile** (from a style quiz or the account) | better scores for customers with few purchases | Nice | skip for v1; usual size is derived from kept items |
| N8 | **Engagement feedback:** invite opened, product viewed, bought within 30 days | to calibrate the 80 threshold and the weights; to measure precision | Later (production) | demo: simulated "opened / bought" buttons only |
| N9 | Wishlist / browsing history | strong intent signal | Nice | not in v1 |
| N10 | Product images | nicer invite cards | Nice | placeholder colour swatches, as the current product cards do |

**Where N1–N5 live (decision D2):**
- **(a) Recommended:** new tables in the existing **`catalog` schema** (`new_products`, `product_style`,
  `colour_families`, plus `relations` rows for the new products), and a new **`marketing` schema** for consent.
  This is the same kind of change you approved for the recommendations catalog. Nothing in bronze, silver or gold
  changes. The mock backend gets the same data.
- **(b) Local files only** (`test_data/aura/*.yaml`) read by both backends. Zero Databricks change, but the data
  then doesn't come from the "system of record" in the demo.

## 6. Delivery phases

| Phase | Scope | Size |
|---|---|---|
| 1 Data | generator + validation for N1–N5, mock adapter reads, Databricks SQL (if D2 = a, run once Databricks is back) | M |
| 2 Engine | `aura.py`: gates, signals, score, breakdown, reason sentences; policy block `aura`; unit tests incl. edge cases (no purchases, all returned, one-size items, out of stock in size) | M |
| 3 Launch runner + delivery | Aura store, idempotent run, outbox, shared daily cap with the Guardian, quiet hours, opt-out, CLI `aura` and `scripts/aura.ps1` | M |
| 4 Aura agent | `new_in_stylist` specialist + `get_new_in_invite` tool, orchestrator routing rule, lean network regenerated, display name Aura in the console | S–M |
| 5 Demo console | Launch panel with the score table, invite cards in the Inbox, "Look at it" → chat with Aura | M |
| 6 Evaluation + docs | `aura_eval.py` (scripted, then real model), `docs/aura.md`, enhancements plan + backlog updated | S–M |

## 7. Evaluation (planned)

| Id | Case | Expected |
|---|---|---|
| A1 | platinum customer who owns the navy overcoat; launch the cashmere scarf (complement, matching colours) | invited, score ≥ 80, reason names the overcoat |
| A2 | gold customer with only activewear; launch the suede loafers | not invited (score < 80), score stored with its breakdown |
| A3 | standard / silver customer with a perfect match | not considered (not premium) |
| A4 | premium, perfect match, **no marketing consent** | not invited (gate: consent) |
| A5 | premium with risk flags | not invited, reason never shown to the customer |
| A6 | premium with an escalated Guardian watch / open case with a person | not invited |
| A7 | perfect match but out of stock in their usual size | not invited |
| A8 | same launch run twice, and a second launch the same week | no duplicate; the frequency cap holds |
| A9 | invite generated at 22:00 | held until 08:00; counts towards the shared daily cap |
| A10 | customer opens a chat: "Tell me about the scarf you sent me" | routed to Aura; presents the invite, reasons, sizes/colours; no invented price or stock; nothing ordered |

Also measured: invites per launch (out of 75 premium customers). Every invite must have at least one owned-item
reason. There must be no invite below 80, and no invite that breaks a gate.

## 8. Decisions needed from you

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Who is "premium" | platinum only (18) / **gold + platinum (75)** | gold + platinum |
| D2 | Where the new data lives | **new tables in the `catalog` schema + a `marketing` schema** / local files only | new tables (same pattern as the recommendations catalog). Databricks is down, so build on mock first and load later |
| D3 | Invite wording | **code templates** / LLM-written within guard rails | templates (key actions controlled by code); Aura's chat replies use the model |
| D4 | Threshold | **80, in policy** / per tier | 80, single value, in `service_policy.hocon` |
| D5 | Frequency cap | **max 1 Aura invite per customer per 7 days**, shared 2/day with the Guardian / other | as proposed |
| D6 | Can Aura reserve or add to basket? | **no (read-only stylist)** / later with the E4 confirm link | no for now |

## 9. Risks and limits

- The 80 threshold is a business rule, not a measured probability. Calibrating it needs real engagement data (N8).
- With 40 products and generated orders, wardrobes are small. A few staged new products need hand-set relations
  and tags so the demo shows a clear "yes" and a clear "no".
- Marketing consent is a legal requirement in production. The demo consent data is synthetic.
- A production version needs a real launch event (from the product information system), a scheduler, and an email
  or app-push channel instead of the outbox.
