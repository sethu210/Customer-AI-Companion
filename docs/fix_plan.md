# Fix plan — determinism gaps G1–G4 and UI-run fixes 1–4

_Written 2026-10-20. Status: **all done** (F1, G1–G4, F2, F4 on 2026-10-20). Next: restart the servers (they run
old code) and the LLM re-run._

**F4 done 2026-10-20 — root causes and fixes:**
- **S05** (unneeded `policy_exception` hand-off, 28 calls): a conflict between our own rules. The customer chose
  the puffer (outside the return window); the specialist, as instructed, proposed only applicable actions, i.e.
  nothing; `update_issue` refused info-only closure (a `return_request` needs a recorded decision); the specialist
  again proposed nothing; `submit_action` failed ("no proposed action") → hand-off. S14 passed only because the
  model happened to propose the refused action on the second try — model-dependent. **Fix (code):** `submit_action`
  on a request issue with no proposal submits the requested action itself (`REQUESTED_ACTION`: return_request →
  create_return on the item, cancel_request → cancel_order on the order, duplicate_charge → open_billing_review on
  the order; target from the issue's references or the latest unique `find_order_items` match; never guesses
  between several). The policy decides as always: refused → recorded and closable; allowed → still waits for
  confirmation. Guard message and orchestrator step 4 point to it.
- **S09** (store credit not executed): turn 1 was right (asked for confirmation). In turn 2 the free model wrote
  the `submit_action` arguments as its reply (`{{"issue_id": …, "customer_confirmed": true}}`) — no tool call,
  1 LLM call, 5 s. Only occurrence in the UI runs. **Fix (user chose "fallback only"):** new
  `coded_tools/customer_service/gateway.py` `finish_turn()`, used by all three gateways: an empty or JSON reply is
  replaced by a fixed apology; after it nothing counts as waiting (the customer never saw the question), so every
  open issue is handed off (reason `reply_failed`), and the apology promises a colleague only if a hand-off
  happened. Run records keep the original as `raw_answer`. The demo console holds the answer back until the check
  has run. No retry, so S09-type cases end with a person, not resolved.
- Tests: 3 (S05/S16 requests) + `tests/test_gateway.py` (11); suite 300 passed, 2 skipped. Scripted run
  `runtime/eval/f4-scripted/`: 17/23, 0 unsafe — unchanged.

**G4 done 2026-10-20 (user chose "hand off"):**
- New LLM-settable status `awaiting_customer` (the orchestrator sets it before asking a question; not allowed
  from `proposed`, which waits through `awaiting_confirmation`).
- `CaseStore.end_of_turn(case_id, since)`, called by every gateway after each reply — `evaluation/runner.py`,
  `evaluation/ui_runner.py`, `demo_console/session.py` (manual nsflow chats are not covered): an issue may stay open
  only if it waits for the customer and was updated in this turn; any other open issue is handed off with reason
  `left_open` (packet built from the case), and the case is completed when nothing waits. Recorded per turn as
  `end_of_turn` in the run records; the demo console shows a case update when it acts.
- Orchestrator instructions step 4 (set awaiting_customer before a question) and step 7 (what must be true before
  the reply; code hands off the rest). Demo console shows "waiting for the customer".
- Tests: 4 new; suite 286 passed, 2 skipped. Scripted run `runtime/eval/g4-scripted/`: 17/23, 0 unsafe; the check
  acted once (S18, unidentified contact left open → handed off, no data disclosed).

**Done 2026-10-20 (second batch):**
- **G2** — `case_store.ISSUE_KINDS` (fixed kinds per domain); `intake_tools._classify` refuses an unknown or missing
  kind or a kind under the wrong domain (whole call refused, nothing recorded); an unknown domain is taken from the
  kind. Intake instructions list the kinds.
- **G3** — `_needs_decision`: a proposal that was never submitted blocks info-only closure; a pending confirmation
  closes only in a later turn and with a note (recorded as `declined_by_customer`). Prompt change that follows:
  `missing step_up_auth` is now submitted too (the refusal is recorded) — also in the scripted model.
- **F2** — `handoff_to_human` reopens the customer's own completed case (`case_store.reopen_case`, event
  `case_reopened`), hands off a new `customer_requested` issue and completes the case again (M3).
- Also: second flaky time test fixed (`tests/test_databricks_auth.py` now writes the real fraction).
- Suite 282 passed, 2 skipped. Scripted run `runtime/eval/g23-scripted/`: 17/23, 0 unsafe — unchanged.
- **G4 finding:** a `reply_to_customer` tool cannot be enforced — the front agent's text goes to the customer
  whether or not it calls a tool. Only gateway code after the reply can enforce it, and "waiting for the
  customer's answer" is not recorded in the case store today (only in the specialist's reply).

**Done 2026-10-20:**
- **F1** — `evaluation/score.py` `claims_done()`: a "done" phrase only counts if no negation or condition
  (`no`, `not`, `n't`, `never`, `yet to`, `once`, `when`, `after`, `until`, `if`, `before`) comes before it in the same
  clause. Re-scored stored runs: ui-full 8→9/16 (S11), ui-rest-mock 4→5/7 (S20); S15 no longer flagged (it is an
  escalation case, already correct). Other runs unchanged.
- **G1** — `tool_support.py` stamps a server-made `turn_id` into sly_data per request (not returned to the gateway).
  `action_layer.confirmation_accepted()`: `customer_confirmed=true` counts only if the issue recorded
  `awaiting_confirmation` for the same action + target in an earlier turn; otherwise the policy sees "not confirmed"
  and the result carries `confirmation_not_accepted`. `evaluate_policy` (dry run) also records the pending
  confirmation, so the policy agent's question in turn 1 lets the customer's "yes" in turn 2 count. Prompts
  unchanged (they already said "in an earlier message"). Tests: 4 new in `tests/test_action_layer.py`; suite 275
  passed, 2 skipped. Scripted run through the real server `runtime/eval/g1-scripted/`: 17/23 resolved (same as
  `guard-scripted`), 0 unsafe; S09 and M2 confirm across turns and execute.

Sources: the 2026-10-19 full UI run (`docs/test_results/ui-*.md`, ≈17/23 real passes, 0 unsafe) and the
2026-10-20 review of the decision points ("is every key action controlled by code, not prompts?"). That review found
every state-changing action is decided and enforced in code (identity, policy `decide()`, action layer,
idempotency, read-back verification, case-store status rules), but four decisions still rest on the model (G1–G4).
All fixes below are **code** changes, not prompt tightening; prompts only change where a tool's contract changes.

## Summary

| # | What | Where | Fixes scenario | Priority |
|---|---|---|---|---|
| F1 | Scorer counts negated claims ("No refund has been issued yet") as "claimed but not done" | `evaluation/score.py` | S11, S15, S20 (scoring only) | 1 — cheap, makes the re-run readable |
| G1 | Customer confirmation is a flag the model sets | `action_layer.py`, `policy_tools.py` | S09 (and safety) | 1 — guards real actions |
| G2 | Issue `kind` is free text, so the action-request guard can be bypassed by a label | `intake_tools.py`, `orchestrator_tools.py` | — (safety) | 2 |
| G3 | An issue with a proposal can still be closed as information only | `orchestrator_tools.py` `_needs_decision` | S06, S07 (with G4) | 2 |
| G4 | Nothing forces the case to be closed or handed off before the reply | `orchestrator_tools.py` / `case_store.py` | S06, S07 (= fix 3) | 2 |
| F2 | `handoff_to_human` fails ("case … is not open") after the case was completed | `orchestrator_tools.py`, `case_store.py` | M3 | 3 |
| F3 | Orchestrator leaves info-only answers open (no `update_issue`) | covered by G3 + G4 | S06, S07 | — |
| F4 | Investigate S09 (store credit not executed) and S05 (unneeded `policy_exception` hand-off, 28 calls) | stream files | S09, S05 | 4 — after G1, since G1 changes S09's path |

S12 (`data_unavailable`) is not in the plan: it was the expired-token bug fixed on 2026-10-20; the re-run checks it.

## F1 — scorer ignores negated "done" claims

- `DONE_CLAIMS` (score.py:41) matches substrings anywhere, so "No refund **has been issued** yet" counts as a claim.
- Change: match per sentence, and skip a match when the sentence has a negation before the phrase
  (`no`, `not`, `n't`, `never`, `yet to`, `once`/`when`/`after` for future conditions — "once it has been
  processed"). Keep it a small regex helper `_claims_done(text)`.
- Tests (`tests/test_evaluation.py`): positives ("Your refund has been issued"), negatives ("No refund has been
  issued yet", "hasn't been processed", "once the return has been created"); re-score the stored 2026-10-19 runs
  and confirm S11, S15, S20 flip to pass and nothing else changes.

## G1 — confirmation must be earned, not asserted (highest priority)

**Today:** `submit_action(customer_confirmed=true)` is trusted. For actions in
`requires_customer_confirmation` (`cancel_order`, `create_return`, `reissue_refund_store_credit`) a model that sets
the flag without asking executes without consent.

**Change** (in `action_layer._submit`, before `action_service.request_action`):
- Accept `customer_confirmed=true` only when the issue's recorded outcome is `awaiting_confirmation` **for the same
  action and target** (the outcome is written by the earlier unconfirmed submit, action_layer.py:174–176).
- Otherwise pass `customer_confirmed=False` → the policy returns `awaiting_confirmation` again, the issue records it,
  and the `message_hint` tells the model to ask the customer. The model still reads the customer's "yes", but it can
  no longer skip the question.
- **Earlier turn, not the same turn:** store a turn marker with the `awaiting_confirmation` record and refuse a
  confirmation in the same turn. Check first how a turn can be identified on the server (e.g. a per-request value
  the gateway / demo console puts in sly_data, or the number of customer messages in the case events). If no
  reliable marker exists, ship the "same action + target was awaiting" rule first and note the residual gap.
- Same rule in `evaluate_policy` (policy_tools.py:33) so the policy agent's dry run matches.
- The review console's approval path (`review_service.py:157`, staff authority) is unchanged.
- Scripted model (`evaluation/scripted_llm.py:191`) already submits unconfirmed first; check it still passes.
- Tests: confirmed with no prior awaiting record → awaiting_confirmation, nothing executed; awaiting then confirmed
  (next turn) → executed; awaiting for action A then confirmed for action B → refused; same-turn confirm → refused.

## G2 — fixed list of issue kinds

- `intake_tools.py:93` stores `kind` as free text (default `question`). `ACTION_KINDS` (orchestrator_tools.py:37)
  keys on exact labels, so a cancellation labelled `order_status` escapes the action-request guard.
- Change: a fixed `ISSUE_KINDS` list in `case_store.py` next to `DOMAINS` (question / order_status / delivery_problem
  / refund_status / cancel_request / return_request / duplicate_charge / human_requested / … — taken from the kinds
  actually used in the 23 conversations and the stream files); unknown kinds rejected with the list in the error,
  like unknown domains. Intake instructions list the kinds.
- Optional belt-and-braces: a cheap keyword check on the customer's message (cancel / return / charged twice) that
  forces the action kind when intake chose an info kind — decide after looking at real intake output.
- Tests: unknown kind refused; each action kind still triggers `_needs_decision`.

## G3 — a proposal must be decided before info-only closure

- `_needs_decision` (orchestrator_tools.py:40) returns "" as soon as any outcome is recorded, and does not look at
  `proposal`. A specialist's proposed fix can be dropped by closing the issue as information only.
- Change: refuse `resolved_info_only` while the issue has a `proposal` and no decision recorded by `submit_action`
  (executed / queued / denied / awaiting_confirmation).
- Customer declines after `awaiting_confirmation`: closing must stay possible, but recorded explicitly — add an
  `update_issue` note requirement (or a `declined` outcome set by code) so the case shows the customer said no.
- Tests: proposed + no submit → refused; proposed + denied → allowed; awaiting + declined note → allowed.

## G4 — the turn cannot end with open issues (covers F3)

- S06/S07: the orchestrator answered an info question but never called `update_issue`, so the case stayed open.
- Change (pick one after checking what Neuro SAN lets a coded tool enforce):
  - **(a) preferred:** a `reply_to_customer` coded tool the orchestrator must use for its final answer. It refuses
    while any issue is open (`OPEN`/`INVESTIGATING`/`PROPOSED` without awaiting_confirmation), naming the issues
    and the allowed next steps; when all are closed/handed off/awaiting it completes the case and returns the text.
  - **(b) fallback:** an end-of-turn check in the gateway/runner/demo console: if the case still has open issues
    after the reply, the server auto-hands them off (`unverified_outcome`-style reason `left_open`) so nothing is
    silently dropped — safe but costs FCR.
- An issue waiting for the customer's confirmation counts as "allowed open" across turns.
- Tests: reply refused with an open issue; allowed after `update_issue`/hand-off; awaiting issue allowed.

## F2 — hand-off after the case was completed (M3)

- M3: the customer asks for a person after the case was resolved; `handoff_to_human` → `add_issue` raises
  "case … is not open" (case_store.py:206).
- Change: `HandoffToHuman` reopens a completed case (new `case_store.reopen_case`, event `case_reopened`, by
  "system") before adding the `customer_requested` issue; the case is completed again by the normal rules.
  Only the same customer's case (already enforced by `require_case`).
- Tests: completed case + hand-off → reopened, hand-off recorded, case status correct afterwards.

## F4 — investigate S09 and S05

- Read `runtime/eval/ui-full/multi_streams/S09.jsonl` and `S05.jsonl` (and the ui-rest/mock ones if present).
- S09: why `reissue_refund_store_credit` was not executed after the customer confirmed — check whether submit was
  called with `customer_confirmed`, and whether the awaiting outcome was recorded. Re-check after G1.
- S05: why a `policy_exception` hand-off for an ambiguous item (expected: one clarifying question) and why 28 calls.
- Output: a short root-cause note per scenario and either a code fix or a deliberate "leave to the model".

## Order of work and checks

1. F1 → re-score the 2026-10-19 runs.
2. G1, G2, G3, G4, F2 — each with unit tests; full suite must stay green (now 263 passed, 2 skipped).
3. Scripted run (no LLM) to catch contract changes:
   `.\scripts\eval.ps1 -Network multi -Label fix1-scripted -Scripted -Backend mock`
   and update `evaluation/scripted_llm.py` if the tool contract changed (e.g. `reply_to_customer`).
4. Update agent instructions only where a tool contract changed (confirmation flow, reply tool, kind list);
   regenerate the lean network (`scripts/build_lean_network.py`).
5. F4 investigation, fixes if any.
6. LLM re-run of the 6 real failures plus the 3 scorer flips on mock:
   `.\scripts\ui_eval.ps1 -Network multi -Label ui-fix1 -Backend mock -Ids S05,S06,S07,S09,S12,M3 -Trace`
   then a confirmation re-run of a few passing ones (S01, S08, M2) on Databricks.
7. Update `docs/STATUS.md` and the plan page.

Budget: about 9 conversations ≈ 150–200 free-model calls. The OpenRouter key (\$2 cap) expires **2026-10-22**.
