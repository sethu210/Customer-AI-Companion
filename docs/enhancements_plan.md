# Everly enhancements — plan

_Written 2026-10-20. Status: **E1 done** (2026-10-20), **A2, E2 and E3 done** (2026-10-21, scripted model); E4 not
started — each part starts on the user's go-ahead._

**Vision:** Customer360 Agent knows the customer, supports both humans and AI assistants, takes ownership of
issues, gives proactive updates, and stays engaged until the problem is fully resolved.

**Principle (unchanged):** code owns identity, decisions, case state and guarantees; agents talk and route. Each
enhancement extends what exists — the gateway contract (sly_data), case store, action layer, end-of-turn check
(`gateway.finish_turn`), review console, outbox, evaluation harness — rather than adding a parallel system.

## Decisions (2026-10-20)

| # | Decision | Chosen |
|---|---|---|
| 1 | Assistant protocol | **MCP first** (A2A agent card possibly later) |
| 2 | Assistant authorization | **OAuth consent with scopes**; test tokens in the demo |
| 3 | Actions through assistants | **Read-only first**, then actions with a **confirm link in ACME Retail's own channel** |
| 4 | Guardian events in the demo | **Local event simulator** (no Databricks data changes) |
| 5 | Guardian automatic actions | **Updates and hand-offs only** (no automatic re-tries) — confirmed 2026-10-21 |
| 6 | Update channels | **Outbox + an "Inbox" panel in the demo console** — confirmed 2026-10-21 (assistant webhook in E4) |
| 7 | Slimming the front agent | **Product advisor agent now; measure before moving anything else** |

## Order of work

| Step | Part | Why this order | Rough effort |
|---|---|---|---|
| E1 | Product advisor agent (+ measurement) | small; makes the front agent lighter first | 1 day (+1–2 days measuring) |
| E2 | Headless, read-only (MCP) | assistants can see status safely before anything can change | 3–4 days |
| E3 | Resolution Guardian | cases owned until resolved; its status and update machinery serve E4 | 7–9 days |
| E4 | Headless, actions + updates | needs the confirm link and the Guardian's updates | 3–4 days |

Every step ships with scripted-model tests first (no LLM quota); real-model runs depend on a faster model, given
the free model's 1–4 min per call on 2026-10-20.

---

## E1 — Product advisor agent; lighter front agent

**Goal:** the orchestrator routes and writes the reply; specialists do the work.

**E1a: `product_advisor` specialist (≈ 1 day)**
- New agent in `registries/customer_service/support_multi_agent.hocon`; `get_recommendations` moves from the
  orchestrator's tools to it; the orchestrator routes `shopping` issues to it like any other domain.
- Guarantees stay in code, unchanged (`recommendation_tools.py`): shopping issues only, the customer's other
  requests first, eligibility (identified, strong/medium sign-in, no case with a person, no risk flags), no SKUs to
  the model.
- The specialist returns the tool's result as is (products, prices, reasons, refusal hint); the orchestrator
  relays it and closes the issue.
- Update: scripted model (`evaluation/scripted_llm.py`), demo console agent list and map (new node; cards keep
  coming from the case evidence), lean network (regenerate), network tests.
- **Acceptance:** R1–R7 pass unchanged (scripted run), suite green; calls per shopping conversation recorded.

**E1b: measure before slimming further (≈ 1–2 days)**
- Each delegation adds ≥ 2 LLM calls (20 s – 4 min each on the free model): a lighter front agent means more hops.
- Candidates, compared with the existing harness (scripted first, then the real model when available) on calls
  per conversation, seconds and FCR — the way the lean variant was evaluated:
  - an **action coordinator** agent taking the propose → policy → submit flow off the orchestrator;
  - specialists **closing their own issues** (`update_issue`), so the orchestrator stops managing statuses.
- **Output:** a short decision note (adopt / not) with the numbers; no change is made without it.

### E1 results (2026-10-20)

**E1a delivered:**
- `product_advisor` agent in `support_multi_agent.hocon`: calls `get_recommendations` once and returns its result
  unchanged (or the tool's error, e.g. "other requests come first"); the orchestrator routes `shopping` issues to it
  and no longer has `get_recommendations` itself. The code guarantees in `recommendation_tools.py` are unchanged.
- **Bug fixed on the way:** the `add_issues` schema the model sees still listed the domains without `shopping`
  (Phase 3), so a real model could never have recorded a shopping issue; the scripted model bypasses the schema, so
  no test caught it. Fixed, with a test that keeps the schema's list equal to `case_store.DOMAINS`.
- Scripted model: new `_advisor` role; the orchestrator delegates to it. Lean network regenerated (builder anchor
  updated). Demo console: "Product Advisor" agent on the map (own colour), plain-English summaries for
  `get_recommendations` and the advisor's reply; the map now sizes itself to the number of specialists (checked:
  7 specialists, viewBox 770×312, nothing clipped).
- Suite 360 passed, 2 skipped. Scripted run `runtime/eval/e1-scripted/` (all 30): R1–R7 all pass with the same
  suggestions and refusals; recommendation_accuracy 1.00, 0 invalid / invented / unsafe; FCR 23/24 as before.

**Measured cost of the extra hop (scripted, `recs-scripted2` → `e1-scripted`):** +2 LLM calls per shopping
conversation (R1 9 → 11, R2 15 → 17, R7 18 → 20); the other 23 conversations unchanged call for call (info 14.3,
action 17.2, deny 15.0, escalate 19.2). Average over all 30: 14.47 → 14.93.

### E1b decision note (for the user)

A delegation costs the specialist's own 2 calls (decide the tool call, then answer); the orchestrator spends the
same one step either way. With that measured unit:

| Candidate | Change in calls (estimate from the measured unit) | Effect on safety | Recommendation |
|---|---|---|---|
| **Specialists close their own info-only answers** (`update_issue resolved_info_only` in the same step as `record_findings`) | **−1 call per conversation with an answered issue** (the orchestrator's separate closing step goes; the specialist adds no step when it calls both tools together) — about −7 % on info conversations (14.3 → ~13.3), and the dropped call is on the orchestrator, whose prompt is the largest | Unchanged: the G3 guard still refuses closing a proposal, an action request or a stuck case; G4 still hands off anything left open | **Adopt next** — it makes the front agent lighter *and* cheaper |
| **Action coordinator agent** (takes policy agent + submit + outcome off the orchestrator) | **≈ +2 calls per action conversation** (orchestrator −1 step, coordinator +3: policy, submit, answer) — about +12 % (17.2 → ~19.2), plus one more level of nesting (latency) | Unchanged (the action layer decides) | **Not now** — revisit with a faster model; on the free model (20 s – 4 min per call) it costs 1–8 min per action conversation |

Both estimates are to be confirmed by a scripted run of the variant and, when a faster model is available, a real
one. No change is made until the user chooses.

### A2 results (2026-10-21) — "specialists close their own info-only answers" adopted

- **How:** `record_findings` takes `answered: true`. With no proposal it closes the issue as `resolved_info_only`
  in the same tool call. That makes it one atomic step, so there is no ordering race with a separate
  `update_issue` call. The guard is the same `_needs_decision` that `update_issue` uses (action requests, unsubmitted
  proposals, stuck or failed cases, pending confirmations, no tool evidence). Loyalty issues also refuse it for
  `missing_points` and for a tier mismatch (`_needs_person`). A refused close keeps the findings and returns
  `closed: false` with `not_closed_because`. The orchestrator then follows its usual path (submit, hand off or
  `update_issue`).
- Instructions: the four specialists set `answered` and return `closed`. The orchestrator's step 4 now skips
  issues a specialist has closed. The scripted model follows suit. Lean network regenerated.
- Tests: 8 new (close, no evidence, stuck refund S08, action request S16, while proposing, loyalty balance /
  missing points / tier mismatch); suite green.
- **Scripted run `runtime/eval/a2-scripted/` vs `e1-scripted` (all 30):** average calls 14.93 → **14.57**. −1 call in
  each of the 11 conversations that end with an answered issue (S01–S04, S06, S07, S11, S17, S19, M2, R2; e.g. 12 → 11,
  about −8 %). The other 19 are unchanged call for call (actions, denials, hand-offs, suggestions-only). All scores are
  unchanged: FCR 23/24, escalation accuracy 0.966, 0 unsafe / policy violations / claimed-not-done, recommendation
  accuracy 1.00.
- Still to confirm with a real model (B-list): that it sets `answered` only when the facts really answer the question.

---

## E2 — Headless agent support, read-only (MCP)

**Principle:** a personal AI assistant (ChatGPT, Copilot, Gemini, Claude, …) is **another channel**. It never asserts
who the customer is: an OAuth token proves it, and the gateway turns that into sly_data, exactly as today.

**MCP server (≈ 3–4 days)**
- Endpoint: first check whether Neuro SAN's own MCP support can serve the network; otherwise a thin MCP server in
  front of the HTTP API (`streaming_chat`).
- **Deterministic tools (no LLM; fast and cheap for machine callers):**
  - `get_case_status(case_id?)` — issues, statuses, references, next steps, straight from the case store;
  - `get_order_status(order_ref?)` — the customer's orders, shipments, returns, refunds (existing reads).
- **Conversational tool:** `ask_support(message, case_id?)` — asynchronous, because turns take minutes: returns a
  request id at once; `get_answer(request_id)` polls. Results are **structured** (JSON from the case record:
  issues, outcomes, references, suggested products) plus the reply text.
- In E2 the scope is read-only: requests that would change something are answered and recorded, but any action is
  refused with "confirm in ACME Retail" (E4).

**Identity and consent**
- OAuth 2.1 authorization-code flow with customer consent (as in the MCP authorization spec); the token maps to
  `customer_id`, an auth level and **scopes**: `read_status` (E2), `request_changes` (E4).
- New channel `assistant` in `config/service_policy.hocon` with its own rules (e.g. which scopes may read what).
- Gateway: token → sly_data `{customer_id, channel: "assistant", auth_level, scopes}` (the existing
  gateway-authenticated path); the model never sees the token.
- **Demo:** a consent page in the demo console ("Allow <assistant> to see your orders and cases"), issuing test
  tokens for the test customers; tokens revocable from the same page.

**Safety (as today)**
- Assistant text is untrusted data; other customers' data is unreachable; every call audited with the assistant's
  client id; rate limits per token; no internal ids, risk flags or stock numbers in results.

**Tests and evaluation**
- Unit: token → sly_data, scope enforcement, revoked/expired tokens, cross-customer access refused.
- Eval: conversations on the `assistant` channel driven by a scripted MCP client; the scorer also checks the
  structured output against the case record.
- Demo: Claude Desktop / Claude Code connected to the local MCP server, asking about a refund for "its" customer.

### E2 results (2026-10-21)

Guide: `docs/assistant_gateway.md`.

- **Neuro SAN's own MCP server: not used.** The MCP client supplies `sly_data`, so the assistant would assert the
  customer. Calls also wait for the whole turn and skip `finish_turn`. The support networks stay off its MCP list.
  Built instead: a thin gateway, `assistant_gateway/` (MCP SDK 1.30, FastMCP, streamable HTTP on :8096).
- **Identity:** OAuth 2.1 (authorization code + PKCE, client registration, revocation) with a consent page and a
  connected-assistants page (Disconnect). Each token maps to a grant: customer, sign-in level, scopes. Only token
  hashes are stored. Test tokens come from `assistant_gateway.ps1 token`.
- **Read-only, enforced in the policy:** `read_only_channels: ["assistant"]` → `decide()` refuses every change with
  `confirm_in_channel`, before any other rule. The policy agent's dry run shows it as a failed check.
- **Tools:**
  - `get_case_status` and `get_order_status`: deterministic allow-lists; no model wording, evidence or ids.
  - `ask_support` and `get_answer`: asynchronous, through `stream_turn` and `finish_turn`. The case continues with
    its chat context, scoped to the grant.
- **Safety:** scopes, rate limits (30 calls/min; 20 asks/hour; one ask at a time), ownership checks on case and
  request ids, and an audit row per call. Answers are deleted after 24 h. Requests cut off by a restart are failed.
- **Tests:** 19 unit tests and 4 end-to-end tests over HTTP (OAuth with PKCE and consent → MCP client tool calls;
  refusals of missing, wrong and revoked tokens; the grants page). Suite: 391 passed, 2 skipped.
- **Evaluation `runtime/eval/e2-scripted/`:** a scripted MCP client, scripted model and mock data on
  S01, S02, S08, S13, S14, M2, R1 and D1. Result: **8/8 pass**, 0 conversations changed anything, and S08, S13 and
  M2 were refused with `confirm_in_channel`. Every structured case equals the case store's record, and no customer
  ids appear in any output.
- **Not done yet:**
  - a real-model run (`assistant_gateway.ps1 eval -Live`), part of the B-list;
  - trying Claude Code as the client;
  - a public HTTPS URL for hosted assistants such as Claude Desktop.

---

## E3 — Resolution Guardian

**Principle:** the Guardian is **persistent state plus a deterministic watcher**, not a long-running LLM process.
Code detects events, applies rules, escalates and closes; customer updates come from templates filled with facts
(the model at most words them). Cheaper, reliable on a slow model, and consistent with "decisions in code".

**Data (local SQLite case store; nothing in Databricks changes)**
- A `guardian` record per unresolved issue: what it waits for (refund completed, shipment delivered, review
  decision, replacement shipped), SLA deadline, next check time, updates sent, and its state
  `watching → waiting_on_business | waiting_on_customer → escalated → resolved → closed`.
- **Created by code at the end of a turn** (`gateway.finish_turn` / `end_of_turn`) for refund, return, delivery,
  complaint and replacement issues left in a non-final business state: queued for review, executed but awaiting the
  business outcome (refund initiated, not completed), handed off, delivery or return in progress.

**Local event simulator (decision 4) (≈ 1.5 days)**
- The test data is static (fixed `reference_date`), so nothing "happens" by itself. A local overlay advances a
  simulated clock and injects business events — refund completed / failed, parcel scanned / delivered, return
  received, review approved / rejected — that the reads see on top of the base data.
- Lives under `runtime/` and is read by the mock adapter (and layered over Databricks reads in the demo);
  Databricks data is never changed. A small CLI and a demo-console control ("advance 2 days", "refund completes").

**Watcher (≈ 4–5 days)**
- Scheduled worker: Neuro SAN's periodic events (the server already starts a `PeriodicEventInitiator`) or a small
  Python service; a Databricks job in production.
- Detection: re-read the watched records (existing reads, stuck-case rules); review-console decisions hooked in
  directly; production would use change data feed / streaming.
- Rules (deterministic, settings in `config/service_policy.hocon`):
  - progress event seen → update the customer;
  - past a delay threshold → explain using the existing diagnosis codes (stuck refund, stuck shipment) and the same
    safe wording (no promises of time or outcome);
  - SLA breached → hand off with the case packet;
  - resolved and **verified by read-back** → close the issue, notify, complete the case (the customer can reopen:
    F2);
  - automatic re-tries: **not** in scope (decision 5, assumed) — the Guardian updates and hands off only.
- Notifications (decision 6, assumed): existing `outbox.jsonl` + an **Inbox** panel in the demo console; throttle
  (at most N per day), quiet hours, opt-out. Assistant webhook added in E4.
- Continuity: `case_state` shows the Guardian's state, so a returning customer (or assistant) is told what is being
  watched and when it was last checked.

**Evaluation (≈ 2 days)**
- Time-travel scenarios with the simulator: refund completes → auto-close + one update; stuck shipment →
  explanation, then hand-off at SLA; review rejected → customer notified; throttle respected; worker restart loses
  nothing; a resolved-looking event that read-back does not confirm → no closure.
- Scored from records: updates sent and when, closures verified, **0 false "resolved"**, 0 promises.

### E3 results (2026-10-21)

Guide: `docs/resolution_guardian.md`.

- **Built as planned:** persistent state + a deterministic watcher; no LLM. Watches, notices and preferences live
  in the case store's SQLite file. `gateway.finish_turn` starts watches for every gateway (website, evaluation,
  nsflow runner, assistant gateway). A watcher failure never changes the reply.
- **What is watched:** queued reviews, hand-offs, executed refunds / traces / returns with an outcome still to
  come, and answered refund, return and delivery questions still in progress (decided at the first check from
  fresh data). The same item, order or hand-off is never watched twice per customer. An item known only by the
  customer's words is found in code (`find_order_items`, unique match only).
- **Rules:** progress → update; no movement past the stuck thresholds → one delay update; target (SLA) passed →
  hand-off with the case packet; refund failed → hand-off, **no re-try** (decision 5); a completion is closed only
  when the order timeline confirms it (read-back), else a person after 2 checks.
- **Updates (decision 6):** fixed templates → `outbox.jsonl` (`source: guardian`) + the demo console's **Inbox**
  tab. At most 2 per customer per day, none 21:00–08:00 (they wait), opt-out per customer. A newer waiting update
  of the same issue replaces an older one; every update has a dedupe key.
- **Local event simulator:** a clock and business events in one JSON file, shown on top of the reads
  (`EVENT_SIMULATOR=1`). The data is never changed. Demo controls are in the Inbox tab; the command line is
  `scripts/guardian.ps1`.
- **Continuity:** `being_watched` in `open_case` and `case_state` (one prompt line: say what it waits for and when
  last checked, never when it finishes). The assistant gateway shows a `follow_up` per request.
- **Side fix:** the review console's approval message for a refund said "has been completed" when the refund had
  only been started. It now says "approved and started"; the Guardian reports "completed" from the records.
- **Tests:** 24 unit tests (`tests/test_guardian.py`), plus demo-console and assistant-gateway tests. Suite:
  417 passed, 2 skipped. The E2 assistant evaluation still passes 8/8. The scripted multi-agent evaluation
  (S01, S08, S10, S13, M2, R1, mock) is unchanged: FCR 5/5, escalations correct, 0 unsafe.
- **Evaluation `runtime/eval/e3-scripted/`:** 9 time-travel scenarios (G1–G9) on scripted conversations.
  Result: **9/9 pass, 0 false "resolved", 0 promises**, daily limit and quiet hours respected, nothing changed
  after the conversations except by the (simulated) reviewer.
- **Not done yet:**
  - a real-model run (`guardian.ps1 eval -Live`), part of the B-list;
  - the data's own "today" does not move with the simulator's clock (agents still see the reference date);
  - production: a scheduled job, the wall clock, and change data feed instead of re-reading;
  - updates to assistants (E4).

---

## E4 — Headless actions and updates

**Confirm link (decision 3) (≈ 2 days)**
- An assistant's "yes" is not the customer's yes. Actions that need confirmation (policy
  `requires_customer_confirmation`), and every action requested through `assistant`, create a pending
  confirmation; the customer approves it in ACME Retail's own channel (demo: a confirm page in the console reached by
  a one-time link). Only then may `submit_action` run with `customer_confirmed=true` — the same code check as G1,
  with the confirmation recorded by the confirm page instead of an earlier chat turn.
- Scope `request_changes` required; links expire and are single-use; audited.

**Updates to assistants (≈ 1–2 days)**
- The Guardian's updates also go to a subscribed assistant (webhook, or an MCP notification where supported).
- Optional later: an A2A agent card for assistants that use A2A rather than MCP.

**Evaluation:** assistant-channel conversations with actions: no action without the confirm link; expired and
replayed links refused; Guardian updates delivered to the subscribed assistant.

---

## Open questions

- Which assistants to demo first (Claude Desktop is the simplest MCP client to show).
- Production identity provider for OAuth (the demo issues test tokens itself).

---

## Backlog — everything not done yet (as of end of 2026-10-20)

**Resume here tomorrow.** Nothing below has been started unless it says so. Status: ⏳ not started · 🟡 waiting for
a decision or a resource · ✅ done (kept for context).

### A. Enhancements (this plan)

| # | Item | Status | Next step |
|---|---|---|---|
| A0 | **Customer AI Companion** re-architecture (persistent companion + Personal Stylist Mode); supersedes the Aura plan | 🟡 C1–C8 confirmed, P0–P7 and P9 done (2026-10-22; journeys 10/10 scripted): app named **Everly** | `docs/companion_plan.md`: P8/E4 next; real-model journey run (`journey_eval --live`) and real-model check of the P2/P3/P6 prompt changes (incl. P1, P2, N1); stylist data loaded on Databricks 2026-10-22 (parity tests pass) |
| A1 | E1a product advisor agent | ✅ 2026-10-20 | — |
| A2 | E1b "specialists close their own info-only answers" (≈ −1 call) | ✅ 2026-10-21 | scripted: 14.93 → 14.57 calls avg; real-model check pending (B) |
| A3 | E1b action coordinator agent (≈ +2 calls) | 🟡 recommended "not now" | revisit with a faster model |
| A4 | E2 headless read-only via MCP (OAuth consent + scopes, test tokens) | ✅ 2026-10-21 (scripted) | real-model eval and a Claude Code connection still to do |
| A5 | E3 Resolution Guardian (state + deterministic watcher + local event simulator) | ✅ 2026-10-21 (scripted) | eval 9/9; real-model eval still to do; decisions 5 and 6 confirmed |
| A6 | E4 headless actions (confirm link in ACME Retail) + updates to assistants | ⏳ | after E2 and E3 |

### B. Real-model runs (blocked by the free model)

The free model took 1–4 min per call on 2026-10-20 and one demo turn hit the 900 s limit (`max_execution_seconds`);
the OpenRouter key ($2 cap) **expires 2026-10-22**. Options: run when the free model is faster, or use a faster
paid model from the $10 credit (only `config/llm_config.hocon` changes).

| # | Item | Status | Command / note |
|---|---|---|---|
| B1 | Re-run the 6 real failures after fixes F1–F4, G1–G4 | ⏳ | `.\scripts\ui_eval.ps1 -Network multi -Label ui-fix1 -Backend mock -Ids S05,S06,S07,S09,S12,M3 -Trace` |
| B2 | Recommendations R1–R7 with a real model | ⏳ | `.\scripts\eval.ps1 -Network multi -Label recs-llm -Backend mock -Ids R1,R2,R3,R4,R5,R6,R7` |
| B3 | Confirmation re-run of a few passing scenarios on Databricks (S01, S08, M2) | ⏳ | after B1 |
| B4 | User's manual retest in the demo console (timed out on 2026-10-20 because of model latency) | 🟡 user | demo at http://127.0.0.1:8095 (restarted with all of today's code) |
| B5 | New OpenRouter key before 2026-10-22 (separate test and demo keys with their own caps was suggested) | 🟡 user | — |

### C. Demo

| # | Item | Status | Note |
|---|---|---|---|
| C1 | Record the remaining reel scenarios (S01, S13, S14, and R1 for suggestions); only M2 is recorded | ⏳ | `.\scripts\record_demo.ps1 capture`, then `pick` / `reel` |
| C1b | Two-scenario video D1 + R1 (`demo/reel-two.yaml`), agent display names, skill `record-demo-video` | 🟡 Databricks | set up 2026-10-21; live takes blocked because the Free Edition warehouse would not start. Run the skill when it is back |
| C2 | British female offline voice for the voice-over | 🟡 user | not installed (Zira fallback): Settings > Time & Language > Speech > Add voices > English (United Kingdom), then re-render |

### D. Known limitations (accepted for now)

| # | Item | Note |
|---|---|---|
| D1 | S05 fails in scripted runs | the scripted model cannot read the clarification ("The puffer one."); not a product issue |
| D2 | "Bought together" is random in the test data | background orders pick products at random; meaningful only with real orders |
| D3 | Gateway checks (end-of-turn hand-off, unreadable-reply fallback) don't run in manual nsflow chats | they run in the demo console and both test runners |
| D4 | Unreadable model reply → fallback apology + hand-off, no retry | user's choice (S09); a retry could be added later |
| D5 | Review console has no sign-in | local use only; production needs SSO |
| D6 | Systems of record are simulated (`executed.jsonl`), case state in local SQLite | real integrations are a production item |
| D7 | Product catalog and stock are generated test data | production needs real catalog and stock feeds |

### E. Production readiness (from the 2026-10-20 "connect to prod Databricks" answer; nothing changed yet)

| # | Item |
|---|---|
| E-1 | Read-only views in prod mapping real tables onto the silver contract (`test_data/schema/tables.yaml`); schema names `silver` / `agent_api` / `policy_api` / `catalog` are fixed in code (or make them configurable) |
| E-2 | Run `uc_functions.sql`, `policy_functions.sql`, `catalog_tables.sql` against the prod catalog |
| E-3 | Reference date = real "today" and thresholds agreeing between `service_policy.hocon` and the data's `config` table |
| E-4 | Service principal, read-only grants; token from a secret store (machine-to-machine OAuth needs a small adapter change) |
| E-5 | Approved model provider (customer data must not go to a free model); treat Phoenix traces as sensitive |
| E-6 | Prod smoke checks on known customers (the parity test only works with the test data) |
| E-7 | `ACTION_MODE=shadow` until real action integrations exist |

### F. Housekeeping

| # | Item | Status |
|---|---|---|
| F-1 | `README.md` is empty | ✅ 2026-10-22 (Everly README, P0) |
| F-2 | `registries/generated/unhcr_back_office_agent_network.hocon` (designer test from day 1) — delete if unwanted | 🟡 user |
| F-3 | `databricks_adapter.py` uses the deprecated `streamablehttp_client` (2 uses) → `streamable_http_client` | ⏳ |
| F-4 | Single-agent network still has Neuro SAN's default `max_execution_seconds=300` (multi-agent has 900) | ⏳ |
| F-5 | Plan page artifact still shows day-1 progress | ⏳ |
| F-6 | Agentforce comparison in the key-features doc is unverified against current Agentforce docs (comment left on the doc) | 🟡 user |
| F-7 | Optional: create a dedicated `customer_service` catalog in the Databricks UI (data lives in `workspace`) | ⏳ |
