# Everly — Customer AI Companion re-architecture plan

> **Everly** — *Your AI Companion, Always by Your Side.* The app and the companion are both called Everly; the store
> is ACME Retail, and the app is presented as "Everly from ACME Retail".

**Rebrand (2026-10-22):**
- **The store:** UrbanLeaf Retail became **ACME Retail** everywhere: prompts, policy texts, invites, greetings,
  assistant gateway, docs and tests. Assistant tokens now start with `acme_`, so tokens issued before no longer work.
- **The look:** a neutral ACME look (indigo accent, greys) and an Everly companion mark replace the leaf and the green
  colours. The header reads "Everly from ACME Retail".
- **The project's name:** "Everly" now appears in the README, the pyproject description and the doc titles. Folder
  and module names stay (user's choice: names and text only).
- **Stylist data, so launches find someone:**
  - **Catalog:** the Linen Overshirt is now a spring/autumn layer that goes with the slim chinos, and the Trail
    Fleece Jacket goes more strongly with the trail running shoes (0.9). Only the products and relations tables
    changed; the customer tables are byte-identical. With every customer opted in, the six launches now invite 226,
    47, 97, 31, 62 and 3.
  - **Demo set-up:** the demo opts in 15 set-up customers (`stylist_mode_on`, `docs/demo_console.md`).
  - **Databricks:** catalog reloaded.

Status (2026-10-22): decisions C1–C8 **confirmed** as recommended (§9). **P0–P7 and P9 done** (§6); P9 ran with
the scripted model. The real-model journey run and the real-model check of the P2, P3 and P6 prompt changes are still
to do (they need an OpenRouter key). Next: P8/E4 (own AI assistant views, confirm link), on go-ahead.
Supersedes the stand-alone Aura plan (`docs/aura_plan.md`): Aura becomes the companion's **Personal Stylist Mode**.

> **Value proposition:** from a customer *account* to a customer-owned AI *companion* that provides continuous
> support, personalised styling and proactive engagement.

---

## 1. From today's app to the companion

| | Today (Customer360 / zero-question agent) | Companion |
|---|---|---|
| Unit | a **conversation** (sign in → chat → case) | a **customer**: one companion per account, for life |
| Memory | case store per conversation; the gateway keeps only `case_id` | a persistent **companion profile** and **memory** across all conversations and channels |
| Relationship | reactive: answers when asked | reactive **and** proactive: follows issues (Guardian), announces relevant launches (Stylist) |
| Shopping | suggestions only when asked (recommendations engine) | opt-in **Personal Stylist Mode**: new launches scored against the wardrobe, invite only at ≥ 80 |
| Channels | website chat; read-only MCP for assistants (E2) | one companion thread across web account, app and the customer's own AI assistant (E2 + E4) |
| Control | policy controlled by the business | the customer controls their companion: modes, updates, memory, connected assistants, export, delete |

**What stays:** the multi-agent network and its specialists, the rule that code decides key actions, the policy
engine, the review console, the Guardian, the recommendations engine, the event simulator, and the read-only rule on
Databricks customer data.

## 2. Principles

1. **One companion per customer.** It is created with the account and is always the same: same name, same thread,
   same memory, on every channel.
2. **The customer owns it.** They can see everything the companion knows, correct or delete it, switch modes on and
   off, connect their own AI assistant, and export or delete the companion.
3. **Code decides; the model talks.** Identity, actions, eligibility, follow-up rules, stylist scores and
   thresholds, consent, message limits: all deterministic code (as today). The model writes the conversation.
4. **Memory comes from facts, not guesses.** The profile is derived by code from the systems of record. Preferences
   are stored only when the **customer states them and confirms**. The model never writes free-text memories on
   its own.
5. **Service before selling.** A customer with an open problem gets no stylist invites. Service updates always win
   the daily message budget.
6. **Earned attention.** At most 2 proactive messages a day, quiet hours, at most 1 stylist invite a week, and only
   items with affinity ≥ 80.

## 3. Target architecture

```
 Customer channels:  ACME Retail web account · app · customer's own AI assistant (MCP, E2/E4)
                                    │
                       ┌────────────▼─────────────┐
                       │   Companion gateway      │  identity in code, one thread per customer,
                       │   (evolves gateway.py)   │  injects profile digest + open items each turn
                       └────────────┬─────────────┘
          ┌─────────────────────────┼──────────────────────────────┐
          ▼                         ▼                              ▼
 Conversation (Neuro SAN)    Companion core (code, no LLM)   Proactive engines (code, no LLM)
  Companion front agent        companion profile               Guardian  (service follow-up)
  + specialists:               memory (stated prefs)           Stylist   (launch → affinity ≥ 80)
  intake, orders, returns,     settings & consents
  payments, loyalty,           thread (chat + updates)                     │
  product advisor, policy,             ▲                                   │
  **stylist** (new)                    └──────── Companion messaging ◄─────┘
                                                 (consent, quiet hours, daily budget,
                                                  priority service > stylist, channel routing)
 Data: Databricks customer data (read-only) · catalog schema (+ launches, style, colours) · local companion store
```

## 4. Components

### 4.1 Companion core (new, code)
- **`companion` record per customer:** companion id, name (C2), created, settings, connected channels and
  assistants.
- **Companion profile (derived, read-only, rebuilt by code from the data):**
  - usual sizes per category (existing usual-size logic);
  - favourite colours and colour families;
  - categories bought and kept, returns and their reasons;
  - price range;
  - loyalty tier and points;
  - open requests (cases, Guardian watches);
  - style tags of owned items.
- **Memory (customer-stated, confirmed):** preferences such as "prefers navy and earth tones", "no wool", "size 10
  in footwear", "dressing for a wedding in June". Stored only via a `remember_preference` tool **after the
  customer says yes** ("Shall I remember that?"). Each entry has a source, date and expiry, and can be edited or
  deleted.
- **Conversation recall:** a code-built summary per closed case ("Mar 2026: refund for a puffer jacket, completed").
  This uses the case store, not model-written notes.
- **Profile digest:** a compact (≤ ~300 tokens), code-built block given to the front agent each turn: name, tier,
  sizes, preferences, open items. It replaces several lookups and keeps the free model fast.

### 4.2 Conversation layer (changed)
- **Front agent becomes the Companion.** The orchestrator keeps its routing role but speaks as the customer's
  companion: it knows the profile digest and the open items, and it greets returning customers with what changed
  ("Your refund for the jacket went through on Tuesday").
- **New specialist `stylist`** (Aura's role, used in Stylist Mode or when asked):
  - styles new launches and invites using the affinity engine;
  - answers "what goes with…" using the existing recommendations engine plus profile and memory;
  - never orders anything; reserving or adding to basket comes later through E4.
- **The existing specialists don't change.** Intake keeps resolving identity in code.
- **One thread:** a returning customer continues the same thread. A case is still opened per issue and is visible
  in the thread.

### 4.3 Proactive engines (existing + new)
- **Guardian (E3, built):** stays as the service follow-up engine. Its outbox becomes the companion thread.
- **Stylist engine (new; the Aura plan §3–§4):** a launch event → customers with Stylist Mode on (+ C3 tier rule) →
  hard gates → affinity score (wardrobe complement 35, style 20, colour 15, category 10, price 10, season 10) →
  invite only at ≥ 80, with the reason and the owned item named. Every score is stored with its breakdown.
- **Companion messaging (new, shared by both engines):**
  - consent per message type (service updates vs stylist invites);
  - quiet hours;
  - a shared daily budget, with service first;
  - a weekly stylist cap;
  - dedupe;
  - routing to the customer's channels: thread, plus a connected assistant (E4).

### 4.4 Customer control centre (new UI, "My Companion")
- **Chat:** the continuous thread, with proactive messages inline.
- **Updates:** issues being followed, with state, last checked and what it is waiting for (from the Guardian).
- **Stylist:** a toggle for Personal Stylist Mode (with a plain-English explanation), the picks received with "why
  this", and "not for me" feedback, which is stored and lowers future scores in that style or category.
- **What I know about you:** the profile and memory, each item with its source, plus edit and delete. There is
  also "Download my companion data" and "Reset my companion".
- **Settings:** proactive updates on/off, quiet hours, channels, connected assistants (E2 tokens, revoke).

### 4.5 Customer's own AI assistant (E2 built, E4 planned)
- The E2 MCP gateway becomes "**Connect your own AI to your companion**". The same read-only views are extended with
  `companion_profile`, `stylist_picks` and `thread`.
- E4 (confirm link + updates to assistants) stays as planned. Proactive messages can then also reach the connected
  assistant.

### 4.6 Business side (demo)
- **Launch panel:** a merchandiser launches a staged product and sees who is scored, the gates, the breakdown, and
  who was invited. Since 2026-10-23 the launch is an event to Orbit, which has Aura launch it (`launch_new_in`), and
  held invites go out on Orbit's 60 s tick (`release_picks`); code decides, no model call (`docs/orbit_tick_plan.md`).
- **The review console stays.** Stylist invites need no review; they are fully code-gated.

## 5. Data

**Already available:** customers (tier, risk flags), orders and items (size, colour, category, price, kept or
returned), returns (reasons), refunds, payments, loyalty, support contacts, the catalog (products, variants with
stock, relations), the case store and the Guardian store.

**Additional data needed:**

| # | Data | Purpose | Where (proposal) | v1? |
|---|---|---|---|---|
| N1 | **New products with `launched_at`** + variants and stock | Stylist trigger | Databricks `catalog` schema (new table) or staged file (C6) | Must |
| N2 | **Relations for the new products** (goes with which existing products, strength) | wardrobe-complement signal | `catalog.product_relations` (new rows) | Must |
| N3 | **Style tags per product** (fixed list: smart, casual, minimal, outdoor, classic, statement…) | style signal; profile style | `catalog` (new table) | Must |
| N4 | **Colour families + harmony table** | colour signal; profile colours | `catalog` (new small table) | Must |
| N5 | **Companion settings and consents** (stylist mode, proactive updates, marketing consent with date and channel) | legal basis (UK GDPR/PECR) + customer control | **local companion store**, owned by the companion, not Databricks | Must |
| N6 | **Stated preferences / memory** | personalisation | local companion store | Must |
| N7 | **Stylist feedback** (opened, viewed, "not for me", bought within 30 days) | lower scores for disliked styles; later, calibrate the 80 threshold | local companion store (simulated in the demo) | Must (simple) |
| N8 | Style quiz / onboarding answers | better profiles for customers with few purchases | local companion store | Nice |
| N9 | Wishlist, browsing history | intent signal | not in v1 | Later |
| N10 | Product images | nicer cards | placeholders as today | Nice |
| N11 | Real launch feed, consent system, push/email channel | production | — | Production |

Customer data in bronze, silver and gold is unchanged. Only the catalog gets new tables (C6), and everything the
customer owns (settings, consent, memory, feedback) lives in the companion store.

## 6. Delivery phases

| Phase | Scope | Builds on | Size |
|---|---|---|---|
| P0 Naming & doc ✅ | Customer360 → Everly: README, docs, demo branding, agent display names (C2) | — | S |
| P1 Companion core ✅ | companion store, settings/consent, derived profile, profile digest, conversation recall | case store, adapters | M |
| P2 Continuous thread ✅ | gateway keeps one thread per customer; front agent gets the digest; returning-customer greeting; outbox merged into the thread | gateway, Guardian outbox | M |
| P3 Memory ✅ | `remember_preference` / `forget_preference` tools with the customer's confirmation (checked in code, like G1); preferences used by the advisor and stylist | P1 | S–M |
| P4 Stylist data ✅ | N1–N4 generator + validation, mock reads (Databricks load later) | catalog | M |
| P5 Stylist engine + messaging ✅ | affinity engine, launch runner, companion messaging (shared budget with the Guardian), feedback | P1, P4, Guardian | M–L |
| P6 Stylist agent ✅ | `stylist` specialist, `get_stylist_picks` / `explain_pick` tools, routing rules, lean network regenerated | P5 | S–M |
| P7 "My Companion" UI ✅ | Chat / Updates / Stylist / What I know / Settings in the demo console; Launch panel; behind-the-scenes kept | P1–P6 | L |
| P8 Own AI assistant | E2 views extended; then E4 (confirm link + updates to assistants) | E2 | M (+E4) |
| P9 Evaluation ✅ | journey evals (§7), scripted then real model; regression of all existing evals | all | M |

Suggested order: P0 → P1 → P2 → P3 (companion first, visible value with existing data) → P4 → P5 → P6 (stylist) →
P7 → P9 → P8/E4.

**P0 done (2026-10-22).** Demo branding in `demo/demo.yaml`: brand "Everly", tagline "Your AI Companion, Always by
Your Side", new `retailer: "ACME Retail"` field (sent by `/api/meta`, shown in the console header note). Console
title, header, video intro/outro narration (`demo_console/narration.py`), script and skill headers, `docs/demo_console.md`
and a new `README.md` updated. Agent display names unchanged; documented that the customer only ever sees "Everly"
and Orbit is the internal front agent. Deliberately **not** changed in P0: agent prompts (the front agent speaking as
Everly is P2 and needs a real-model check), the leaf logo and green colours, the store's name where it names the store
(assistant gateway consent page, "confirm in the ACME Retail app or website" policy text), and existing narration
scripts and recordings under `demo/narration/` and `runtime/demo/` (they are regenerated at the next recording).

**P1 done (2026-10-22).** `coded_tools/customer_service/companion.py` (no LLM), settings `companion` in
`config/service_policy.hocon`, tests `tests/test_companion.py` (17) + 1 demo-console test; suite 435 passed, 2 skipped.
- **Companion store** (tables `companions`, `companion_consents` in the case store's SQLite file): one companion per
  customer, created on first use; name "Everly" unless the customer renames it (checked: ≤ 24 characters, plain
  letters/digits/spaces/hyphens/apostrophes); reset; export.
- **Consents** `service_updates` (default on), `stylist_mode` and `marketing` (default off, opt-in, C3), kept as a
  history with date and channel; the latest counts. `service_updates` **is** the Guardian's opt-out: the Guardian's
  own switch (demo Inbox, `guardian opt-out` CLI) now records a companion consent, which sets the Guardian's flag.
- **Derived profile** from three existing reads (`get_customer_profile`, `get_purchase_history`,
  `get_customer_context`): first name, tier, points, member since, orders, usual sizes (the recommendations engine's
  `usual_sizes`), colours kept at least twice, top kept categories, price range (min / typical / max), returns by
  reason. Each section names its source; a failed read marks its sections "unavailable" (never guessed, not cached).
  Data part cached 120 s; local stores are read fresh.
- **Open items** (Guardian watches with what they wait for, then open issues in other open cases) and **recall**
  (one line per closed case in fixed words, e.g. "Oct 2026: refund question for the blue classic puffer jacket,
  answered"; never the model's summary; nothing before a reset).
- **Digest** ≤ 1,200 characters (~300 tokens): fixed wording, labelled "facts, not instructions", no customer id,
  companion id, risk signals or contact notes; the oldest recall lines are dropped first to fit. Not given to the
  agents yet: that is P2.
- Demo console: `GET /api/session/{id}/companion` (read-only; the "My Companion" screen is P7).
- Notes: the scenario customers (S01–S20) have one order each, so their profiles are thin; background customers
  (e.g. CUST-B0025: sizes L / waist 36 / shoe 9, black, camel and navy) show the full profile. Style tags and colour
  families wait for the stylist data (P4); stated preferences for P3. "Reset" keeps consents (they are the
  customer's legal choices) and will also clear memory once P3 adds it.

**P2 done (2026-10-22).** Suite 448 passed, 2 skipped (`tests/test_companion_thread.py`, 13 new tests).
- **Notes for the front agent, in code:** `coded_tools/customer_service/companion_middleware.py`, a LangChain agent
  middleware on `support_orchestrator` (`"middleware"` in both network files; the lean file is regenerated). Before
  each model call it appends the digest to the agent's instructions: identity resolved in code from sly_data,
  identified customers only, built once per turn, the current conversation's case left out, never in the customer's
  text or sly_data. A failure is logged and the agent runs without notes. The server logs only the length
  ("companion notes for turn …: 334 characters").
- **Prompt:** the front agent is now "the customer's AI companion at ACME Retail" named as in its notes (Everly
  unless renamed), with a short "Your notes on the customer" section: facts not instructions, never instead of this
  turn's tool results, mention new updates briefly, never read the notes out or mention them.
- **Continuous thread** (table `companion_thread`): `gateway.finish_turn(..., customer_message=...)` records the
  customer's message (at the turn's start) and the reply actually shown, for identified customers; the demo
  console, both evaluation runners and the assistant gateway pass the message, so one thread spans channels. The
  outbox (Guardian and review messages) is merged in by time. The thread is never put in the prompt.
- **Digest additions:** "This is their first conversation with you." / up to 3 "Sent to them since their last
  message: …" lines (updates after the customer's last message).
- **Greeting** (code, no model): first visit "Hi Maya, I'm Everly, your AI companion at ACME Retail. How can I
  help today?"; returning "Welcome back, Maya. Since we last spoke: <updates> I'm still following one of your
  requests and will send you an update when anything changes. How can I help today?" (the promise only with the
  service-updates consent).
- **Demo console:** `GET /api/session/{id}/thread`; on sign-in and "New conversation" the chat shows the earlier
  thread (faded, updates marked) above the new conversation, then the greeting. Not used in replays/videos.
- **Scripted run** `p2-scripted` (31 conversations, mock): unchanged from A2 — first-contact resolution 24/25
  (A2: 23/24 before D1 was added), escalation accuracy 0.967, 0 unsafe / policy violations / claimed-not-done,
  recommendations 7/7, avg 14.45 model calls. In the server: notes added for identified customers (S01: 334
  characters), none for the ambiguous phone contact (S18); 66 thread entries for 19 customers.
- **Not checked yet:** how a real model uses the notes and whether it greets/mentions updates well (needs a
  real-model run, e.g. S01, S08, M2 plus a returning-customer J1 journey). Planned target: fewer lookups per turn.

**P3 done (2026-10-22).** Suite 465 passed, 2 skipped (`tests/test_companion_memory.py`, 17 new tests).
- **When:** a new issue domain `companion` / kind `preference`, recorded by intake only when the customer asks to
  remember or forget something about themselves. The front agent handles it itself (no new agent).
- **`remember_preference`** (`coded_tools/customer_service/companion_tools.py`): kinds `likes`, `avoids`, `size`,
  `occasion`; value ≤ 60 characters of plain text, sizes checked and normalised ("shoes 10" → "footwear 10",
  "clothing m" → "clothing M"). Step 1 stores nothing: code records the question (table `companion_memory`, status
  pending, 24 h) and returns the exact wording ("Shall I remember that you like navy?"); the issue waits for the
  customer. Step 2, only in a **later turn** with `customer_confirmed=true`: the recorded question becomes a
  remembered preference and the issue closes. Refused: a yes in the same turn, for a question never asked, for a
  different value, or after the question expired (the G1 rule). A "no" closes the issue via `update_issue` with a
  note, also only after the customer answered.
- **`forget_preference`**: by id from the notes (P1, P2, …) or ALL; deleted at once; the case evidence keeps the ids,
  never the forgotten values.
- **Who:** identified customers on a sign-in that may change things (`write_auth_levels`), not on read-only channels
  (personal assistants): otherwise "manage it in the app or website".
- **Limits** (`companion.memory` in the policy): at most 12 remembered; likes/avoids/sizes kept 365 days, occasions
  120; a newer stated size replaces the older one for the same area. Reset deletes all memory; export includes it.
- **Used:** the notes show "Stated preferences (their words, confirmed): likes navy [P1]; …". The recommendation
  engine applies them in code: a stated size wins over the usual size; products or colours named in "avoids" are
  never offered (replacements of the customer's own item excepted); colours they like come first and add
  `weights.liked_colour` (0.1); the result carries `in_colours_you_like` and `stated_preferences_applied`. The
  stylist (P5/P6) will use them too.
- **Evaluation:** new conversations P1 (remember "navy", "Yes, please.", then suggestions) and P2 (remember, yes,
  forget), multi-agent only, run last; the runner records the customer's memory afterwards and the scorer checks
  it (`memory_accuracy`, `unrequested_memories`: a conversation that did not ask must leave nothing remembered).
  The scripted model plays the flow and, to forget, reads the id from the notes in its system message.
- **Scripted run** `p3-scripted` (33 conversations, mock): first-contact resolution 26/27, escalation accuracy 0.969,
  0 unsafe / violations / claimed-not-done, recommendations 7/7 (8/8 with P1), memory 2/2, 0 unrequested memories,
  avg 14.88 model calls (P1 23, P2 20 over three turns each). P2 forgot by the id "P1" read from the notes.
- **Not checked yet:** a real model asking the question in its own words, waiting, and not remembering things
  unasked (the prompt forbids it; the code allows memory only on a companion issue and after a later yes).

**P4 done (2026-10-22).** Suite 481 passed, 2 skipped. Stylist data on mock (Databricks load later, C6):
- **N1** six products staged for launch (Cashmere Scarf, Waxed Field Jacket, Suede Loafers, Linen Overshirt, Merino
  Polo, Trail Fleece Jacket): in `products` with active=false and stock ready, plus new table `product_launches`
  (planned 2-21 days after the reference date, with a headline). Recommendations skip them until launched.
- **N2** 34 new rows in `product_relations` (new → existing complements, and a few existing → new).
- **N3** new table `product_styles`: 1-3 tags per product from a fixed list (smart, casual, classic, minimal,
  outdoor, sport, statement, cosy), 92 rows.
- **N4** new tables `colour_families` (25 colours in 6 families: dark/light neutrals, earth, blue, green, warm) and
  `colour_harmony` (one row per pair of families, 21 rows: e.g. blue + earth 0.9, green + warm 0.3).
- Generator (`test_data/generator/catalog.py`), schema (`tables.yaml`, enums `style_tag`, `colour_family`),
  validation `check_stylist` (+ 10 negative tests), reads `get_product_launches|styles|colour_families|harmony` in
  both adapters (mock + Databricks SQL in `catalog_tables.sql`, added to the parity test). The 12 customer tables are
  byte-for-byte unchanged (manifest hashes compared).
- **In use now:** the profile/notes show the styles and colour families of kept items ("Styles they keep: classic,
  smart, casual; mostly dark neutrals and earth tones"); a stated preference naming a family ("earth tones",
  "neutrals", "bright colours") stands for its colours in suggestions.
- Scripted run `p4-scripted`: identical to `p3-scripted` (33 conversations, same products offered everywhere).
- **Databricks: loaded 2026-10-22.** The 7 catalog parquet files were uploaded and `catalog_tables.sql` run (10/10
  statements). Row counts equal the manifest: products 46, variants 358, relations 84, launches 6, styles 92, colour
  families 25, harmony 21, copurchase 90. Only the `catalog` schema changed. The parity tests
  (`RUN_DATABRICKS_TESTS=1`, with the `DATABRICKS_*` settings from `.env` in the shell) pass: mock equals Databricks.

**P5 done (2026-10-22).** Suite 496 passed, 2 skipped (`tests/test_stylist.py`, 15 tests: J4-J8 and every gate);
Guardian scripted eval still 9/9 (`p5-guardian`).
- **Engine** `coded_tools/customer_service/stylist.py` (no LLM), settings `stylist` in the policy. A launch (simulator
  clock) takes every customer whose `stylist_mode` consent is on (opt-in, C3):
  - **gates** (stored by name): `not_available` (risk flags, never said why), `open_trouble` (open hand-off, or a
    follow-up escalated or with a person: service before selling), `already_owned`, `rejected_category`, `avoided`
    (stated avoids), `size_unknown` / `not_in_your_size` (stated or usual size; one-size needs none),
    `already_invited`, `weekly_limit`;
  - **score** 0-100 = complement 35 + style 20 + colour 15 + category 10 + price 10 + season 10 (each signal 0-1,
    from the customer's kept items, the stylist data and their stated preferences), minus a "not for me" penalty
    (same category 25, 2+ shared styles 15, max 40);
  - **invite** only at ≥ 80 **and** with an owned item it goes with; every score is stored with its breakdown.
- **Invite** (fixed template, C5): "New in at ACME Retail: the Cashmere Scarf, $87.99. I picked it for you
  because it goes with your navy wool overcoat and suits the classic and smart style you keep. It's in stock in
  camel, grey and navy. Not for you? Just say so, or switch Personal Stylist Mode off at any time." It goes to the
  outbox (source `stylist`), so it appears in the thread and the demo Inbox ("Personal Stylist").
- **Companion messaging** `messaging.py`, shared with the Guardian (C7): one daily limit for both
  (`guardian.max_updates_per_day` 2); an invite waits while a service update is waiting (`service_first`) and always
  leaves 1 slot for service (`reserve_for_service`); quiet hours 21-08; 1 invite a week; dedupe per product; a held
  invite is sent later (`Stylist.run_once`, also run by the demo console's loop and "advance") or dropped after
  3 days. The Guardian now counts invites in its daily limit.
- **Feedback** (N7, simple): `opened`, `not_for_me`, `bought` per invite (table `stylist_feedback`); "not for me"
  lowers similar launches.
- **Command line** `python -m stylist staged|launch|scores|invites|run|opt-in|feedback` and `scripts\stylist.ps1`
  (on the newest demo's stores).
- **Calibration on the test data** (every customer opted in, all six launches at once): Cashmere Scarf 226 invited
  of 506 scored (it complements the Wool Overcoat at 0.95, which ~200 test customers keep, so ≥ 80 mostly means
  "owns the overcoat"); Merino Polo 16 and Suede Loafers 36 (the rest held back by the weekly limit and unknown shoe
  sizes); Waxed Field Jacket, Linen Overshirt (warm season in October) and Trail Fleece 0. The 80 is a rule, not a
  probability: raise `min_affinity` or lower relation strengths if invites should be rarer; real calibration needs
  feedback data (N7, production).
- Not in P5: launched products do not become active in the recommendation engine (the data never changes; an
  overlay could come with P6/P7); the chat cannot see or explain picks yet (P6); no UI for launches (P7).

**P6 done (2026-10-22).** Suite 506 passed, 2 skipped (`tests/test_stylist_tools.py`, 10 tests).
- **New issue domain** `stylist` (kinds `pick_question`, `pick_feedback`): intake records it when the customer asks
  about a pick that was sent, or says one is not for them. The front agent routes it to the new **`stylist` agent**
  (demo display name **Aura**, "Personal Stylist"); both network files, lean regenerated.
- **Tools** (`coded_tools/customer_service/stylist_tools.py`), everything from code: `get_stylist_picks` (the picks
  really sent, newest first: product, list price, why it was picked, what is in stock in their size now, any feedback;
  or why none: Stylist Mode off / none sent), `explain_pick` (the reasons, built in fixed words from the stored
  breakdown: "It goes very well with your navy wool overcoat. It shares the classic and cosy style of what you
  keep. …", never a score), `pick_not_for_me` (feedback; a change, so only on a sign-in that may change things, not
  through a personal assistant). Answers close the issue as information only. Nothing can be ordered, reserved or
  added to a basket.
- **Evaluation:** conversation **N1** (J9): CUST-B0196 switches Stylist Mode on and the Cashmere Scarf is launched
  before the conversation (runner set-up, on the run's own simulator clock: `eval.ps1` now sets `SIMULATOR_STATE`);
  "Tell me about the scarf you sent me." then "Thanks, but it's not for me." Scored: the pick was sent, the
  feedback recorded (`stylist_accuracy`), facts "Cashmere Scarf" and "87.99", no basket/reservation wording. The
  invented-products check now accepts products from picks that were really sent.
- **Scripted run** `p6-scripted` (34 conversations): first-contact resolution 27/28 (the miss is S05, the known
  scripted-model limitation), escalation 0.97, 0 unsafe / violations / invented products, recommendations, memory and
  stylist all 100%, avg 15.06 model calls (N1: 21 over two turns).
- Demo console: the agent map shows Aura; summaries for the stylist and memory tools in the timeline.

**P7 done (2026-10-22).** Suite 513 passed, 2 skipped (`tests/test_my_companion.py`, 8 tests). Checked in headless
Chromium (light, dark, 390 px phone): every tab renders, the launch → pick → "not for me" flow works, no JavaScript
errors.
- **My Companion** tabs next to the chat (left pane; customer's side): **Chat** (as before, with the thread and the
  greeting) · **Updates** (requests being followed: state, what it waits for, last checked; the messages received,
  newest first) · **Stylist** (Personal Stylist Mode switch with a plain explanation; picks with "why Everly picked
  it", what is in stock in their size, "Not for me") · **What I know** (every fact from the account and orders with
  its source; stated preferences with delete and an "add" form; earlier requests; "Download my companion data" (JSON)
  and "Reset my companion", a two-step button) · **Settings** (rename the companion; proactive updates, Stylist Mode,
  news and offers (not used); the message limits; connected personal AI assistants with "Disconnect"; recent choices).
- **Launches** tab in "Behind the scenes" (business side): the staged products, "Launch now", the result (candidates,
  invited, below 80, not eligible by gate) and the best 15 scores with their breakdown and invite status.
- Server: `demo_console/my_companion.py` (an API router: one view for all tabs, one endpoint per action, each
  returning the refreshed view; the Launch panel). The customer's clicks act on their own companion (channel "web");
  adding a preference here counts as their yes (an explicit action in their account), unlike the chat, where the
  agent must ask and wait.
- Shared code moved for reuse: `stylist.reasons` / `sent_picks` / `pick_view` (also used by the stylist tools),
  `companion.issue_words`.
- Fixed on the way: the invisible video caption and subtitle overlays blocked clicks on a phone-width screen (sign-in
  button); they no longer take pointer events.

**P9 done (2026-10-22), scripted model.** The suite has 526 passed and 2 skipped; `tests/test_journey_eval.py` adds 13 tests for the
evaluation's own rules. The journeys run with `python -m evaluation.journey_eval --label <label> [--ids J1,J4] [--live]`.
Results go to `runtime/eval/<label>/journeys.md|json`, with `stores/<id>/steps.json` for each journey.
- **How it works:**
  - Each journey gets fresh stores and its own agent server (scripted model and mock data unless `--live`).
  - Conversations go through the website gateway (`DemoSession`), and J10 also goes through the assistant gateway.
  - Between conversations the simulator's clock moves. The Guardian and the Stylist do their regular run, business
    events happen, products are launched, and the customer uses "My Companion" (the console's own functions).
  - Scores come from the records.
- **Customers:**
  - CUST-S01 (J1), CUST-S11 (J2), CUST-S02 (J3);
  - CUST-B0196 (J4–J6, J9, J10; scarf 92);
  - CUST-B0091 (J7; can return the chino pants);
  - CUST-B0025 (J8; scarf 90 and loafers 90, which share the classic and smart styles).
- **Every journey is also checked for four rules:**
  - every invite has a stored score of at least 80, names an owned item and passed every gate;
  - at most 2 messages per customer per simulated day, none in quiet hours, and at most 1 invite in 7 days;
  - no promised time or outcome and no forbidden phrase, in a reply or a sent message;
  - remembered preferences only in J2 and J3.

  Here a promise means "within" followed by a time: the stylist's reasons say a price is "within what you usually
  spend", which is not a promise.
- **Run `p9-scripted`: 10/10 journeys pass, 0 rules broken, 8.9 model calls per turn (14 turns).** What each
  journey showed:

  | Journey | Result |
  |---|---|
  | J1 | The refund update went out on day 3. The day-4 greeting starts "Welcome back, Maya. Since we last spoke: Your refund of $129.00 … has been completed …". The notes (510 characters) carry the update. The reply asks nothing again. The thread reads customer, companion, update, customer, companion across two cases. |
  | J2 | Nothing was stored before the yes; afterwards "likes earth tones" was stored. The chino suggestions put the earth colours first (khaki and olive chinos, a brown belt) and record `stated_preferences_applied`. "What I know" shows it. |
  | J3 | Forgetting removed it from the notes and from "What I know". A preference added in "My Companion" is saved at once. Reset clears memory, recall (2 lines before) and the thread, and the greeting is a first-visit one again. |
  | J4 | The whole test population was opted in (523). 226 were invited, every one at 80 or more with an owned item. The customer's invite (92) names the "navy wool overcoat". |
  | J5 | Without consent, and after switching it on and off again, nothing was scored or sent. |
  | J6 | "I'd like to talk to a person" opened a hand-off, and the launch was gated `open_trouble`. |
  | J7 | A return was created after the customer's yes. The Guardian's "we have received your returned … chino pants" update and the launch came the same morning. The invite waited (`daily_limit`, one slot is kept for service updates) and went out the next morning. |
  | J8 | "Not for me" was recorded. Eight days later the Suede Loafers scored 75 (90 without the 15-point penalty), and no invite was sent. |
  | J9 | The question was routed to the stylist. The reply names only the Cashmere Scarf, $87.99, and the owned overcoat. No action was taken and there's no basket wording. |
  | J10 | Through the assistant gateway the same pick came back. "It's not for me" was refused in code (`not_allowed`: no changes through a personal assistant), and no feedback was stored. The thread holds the website turn and both assistant turns, and the case status is visible. |
- **Not measured yet:**
  - **J10 views:** the assistant has no profile, thread or picks views yet, only `ask_support`, `get_answer`,
    `get_case_status` and `get_order_status`. Those views are P8.
  - **"Fewer model calls with the notes":** the scripted model makes the same lookups either way (J1: 12 calls on
    day 1 and 12 on day 4), so this needs the real-model run.
- **Regression (scripted, mock):** all three match P6.
  - Conversations (`p9-regress`): 34 conversations, first-contact resolution 27/28 (S05, the known
    scripted-model limitation), escalation 0.97, 0 unsafe, violations or invented products; recommendations, memory
    and stylist all 100%; 15.06 model calls on average.
  - Guardian (`p9-guardian`): 9/9.
  - Assistant (`p9-assistant`): 8/8, nothing changed through the assistant.
- **Real-model run still to do:** `python -m evaluation.journey_eval --label p9-live --live` needs an OpenRouter key
  and, on Databricks, the stylist tables. With `--live`, J4 opts in only 20 customers to keep the reads down.

## 7. Evaluation — "journeys" across days

The existing evals (S/M/R scenarios, Guardian G1–G9, E2 assistant 8/8) are kept as regression. The new journey
evals use the simulated clock across several conversations with the same customer:

| Id | Journey | Checks |
|---|---|---|
| J1 | Day 1 refund question → day 3 Guardian "refund completed" → day 4 new chat | the companion greets with the update; no repeated questions; thread continuous |
| J2 | Customer says "I prefer earth tones, remember that" → later "what goes with my chinos?" | stored only after confirmation; suggestions respect it; visible in "What I know" |
| J3 | Customer deletes a memory / resets the companion | no longer used or shown; recall is gone |
| J4 | Stylist on, launch with a strong match | invite ≥ 80 naming the owned item; fits the daily and weekly caps |
| J5 | Stylist off (or no consent), perfect match | nothing sent |
| J6 | Open complaint with a person + launch | no invite (service before selling) |
| J7 | Guardian update and stylist invite on the same day, budget 2 | the service update is sent first; the invite waits or is skipped |
| J8 | "Not for me" on a pick, then a similar launch | lower score; not invited |
| J9 | Customer asks about the pick in chat | routed to the stylist; facts from the invite only; nothing ordered |
| J10 | The same customer via their own AI assistant (MCP) | sees the same profile, thread and picks, read-only |

Measured as well: no invite below 80 or past a gate; no memory without confirmation; no promises; LLM calls per
turn with the digest compared with today (target: fewer).

## 8. Risks

- **Privacy and trust:** profiling for styling needs explicit opt-in, a clear explanation, and easy off and delete.
  The plan treats these as features, not afterthoughts.
- **Model memory errors:** avoided by design. Memory is code-derived facts plus customer-confirmed preferences only.
- **Latency on the free model:** the digest should reduce lookups. A persistent thread must not grow the prompt, so
  only the digest and the current case go in, never the whole history.
- **The 80 score is a rule, not a probability.** Calibration needs real feedback (N7, production data).
- **Scope:** this is several weeks of work. P1–P3 give a visible companion with no new data; the stylist needs N1–N4
  first.
- **Databricks is down:** everything is built on mock data first, as with E3.

## 9. Decisions

**Confirmed by the user on 2026-10-22: all recommendations below.** C1 = re-centre the app on the companion. C2 =
one brand name for every customer, **Everly** (the app name too), customer can rename later; Orbit stays the
internal front agent. Tagline: "Your AI Companion, Always by Your Side".

| # | Decision | Options | Recommendation (= decision) |
|---|---|---|---|
| C1 | Scope of the change | **re-centre the app on the companion; keep the support network inside it** / a separate app | re-centre (most of the platform is reused) |
| C2 | Companion name and persona | one brand name for every customer (e.g. "Aura") / the customer names their own / keep "Orbit" | one brand name, customer can rename later; Orbit stays the internal front agent |
| C3 | Who can enable Stylist Mode | **everyone (opt-in)** / premium (gold+platinum) only / premium first | everyone, opt-in (the pasted text says "Customers can optionally enable") |
| C4 | Memory | **code-derived profile + customer-confirmed preferences** / model-written memories | code-derived + confirmed |
| C5 | Invite wording | **code templates** / model-written within guard rails | templates; the chat around them uses the model |
| C6 | Where new catalog data lives | **new tables in the `catalog` schema** (like the recommendations catalog) / staged files | catalog schema, built on mock first, loaded when Databricks is back |
| C7 | Message limits | **2/day total (service first), stylist 1/week, quiet hours 21–08** / other | as proposed |
| C8 | E4 timing | **after the stylist (P8)** / before | after: the companion and stylist give more demo value first |
