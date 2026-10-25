# Resolution Guardian (E3): owning an issue until it is really resolved

A support conversation often ends before the customer's problem does: the refund is pending, the parcel is still
moving, a colleague has to review a refund. The Resolution Guardian follows each of these after the conversation.
It tells the customer when something changes, explains a delay, hands the issue to a person when it takes too long,
and closes it only when a second read of the records confirms the outcome.

Plan: `docs/enhancements_plan.md` (E3). Code: `coded_tools/customer_service/guardian.py` (rules and state),
`coded_tools/customer_service/simulator.py` (local event simulator), `guardian/` (command line).

## Decisions

| # | Decision | Chosen (2026-10-21) |
|---|---|---|
| 4 | Guardian events in the demo | **Local event simulator**: the data never changes |
| 5 | Guardian automatic actions | **Updates and hand-offs only**: never re-tries or changes anything |
| 6 | Update channels | **Outbox** (`outbox.jsonl`) + the **Inbox** tab in the demo console (assistants: E4) |

## How it works

**Persistent state plus a deterministic watcher, not a long-running LLM process.** Code reads the records, applies
the rules, hands off and closes. Updates are fixed templates filled with facts from the records. The model writes
none of them.

```
conversation ─ finish_turn ─▶ watch_case: one watch per issue still in progress  (case store SQLite)
                                          │
Orbit's clock tick (every 60 s) ─▶ Orbit ─▶ guardian agent ─▶ run_follow_ups
                                          │ due watch → fresh reads (+ simulator overlay) → rules
                                          ├─▶ outbox.jsonl (updates: throttle, quiet hours, opt-out)
                                          └─▶ hand-off to the review console (case packet)
```

**Orbit triggers the checks** (since 2026-10-23, `docs/orbit_tick_plan.md`). The Guardian is an agent in the
network (`guardian`, tool `run_follow_ups`). A clock sends Orbit a tick request with a secret token; Orbit's and the
guardian's middleware (`system_events.py`) call the tools in code, so the agent-to-agent calls are in the agent
server's stream and on the demo map, but no model is called. On customer turns no model ever sees the guardian.

### Which issues are watched (`watch_case`, at the end of every turn)

| Issue left as | Watched for (milestones, in order) |
|---|---|
| queued for a person's review | `review_decided`, then the action's outcome (below) |
| handed to a person | `handoff_resolved` |
| executed: `initiate_refund`, `retrigger_refund` | `refund_completed` |
| executed: `open_carrier_trace` | `parcel_delivered` |
| executed: `create_return` | `return_received`, `refund_completed` |
| answered (returns/refunds, order/delivery) | decided at the first check from fresh data: a pending refund → `refund_completed`; a return not yet received → `return_received`, `refund_completed`; a parcel not delivered → `parcel_delivered`; nothing in progress → closed (`nothing_pending`) |

- An issue known only by the customer's words ("blue jacket") is resolved at the first check with
  `find_order_items`, the same matching the agents use. Only a unique match is watched (otherwise `item_unclear`).
- **The same item, order or hand-off is never watched twice for a customer**, so asking again does not double the
  updates (`already_watched`).
- Unidentified contacts are never watched.

### States

`watching` (not checked yet) → `waiting_on_business` | `waiting_on_customer` (a return parcel to send) →
`escalated` (with a person) → `resolved` (outcome confirmed, closing update waiting) → `closed`.

### Rules at each check

Settings are under `guardian` in `config/service_policy.hocon`.

| Seen | Then |
|---|---|
| a milestone reached, or a new carrier scan | an update; for a scan, the targets start again |
| no movement for `delay_hours` (refund 120 h, parcel 96 h: the stuck rules) | one update explaining the delay |
| `sla_hours` passed (review 48 h, hand-off 72 h, refund 168 h, parcel 144 h, return 336 h) | a hand-off with the case packet (`guardian_sla_breached`) and an update |
| refund failed | a hand-off (`guardian_refund_failed`) and an update. **Not re-tried** |
| last milestone reached **and confirmed by read-back** (the order timeline shows it too) | resolved, closing update, closed |
| a completion the timeline does not confirm | not trusted; after 2 checks a person looks (`guardian_unconfirmed`) |
| review approved / rejected | the review console already told the customer; the Guardian moves on or closes without a second message |
| hand-off resolved | closed; an update only if the colleague did not write to the customer |
| data unavailable | nothing is decided; the next check tries again |

The delay is counted from the last activity in the data (for example the warehouse scan), or from the agents'
action if that is later. The target (SLA) is counted from the start of each wait.

### Updates

- **Wording:** fixed templates (`TEMPLATES` in `guardian.py`) with the item, amount, carrier, location and case id.
  They make no promises of time or outcome; a test checks this.
- **Where:** `outbox.jsonl` with `source: guardian`, for the channel gateway to deliver. The demo console's **Inbox**
  tab shows the outbox as the customer would receive it.
- **Throttle:** at most `max_updates_per_day` (2) per customer. More wait until the next day. A newer waiting
  update of the same issue replaces an older one.
- **Quiet hours:** 21:00–08:00 on the Guardian's clock (UTC; customers' time zones are not in the data). Updates
  wait until the morning; a resolved watch closes when its closing update is sent.
- **Opt-out:** per customer (Inbox switch, or `guardian opt-out`). Updates are recorded as suppressed; hand-offs
  still happen.
- **Never twice:** every update has a dedupe key, so a restarted watcher never repeats one.

The review console's approval message for a refund now says "approved and started", not "completed". The money has
not moved yet; the Guardian sends "completed" when the records show it.

### Continuity

- `open_case` (intake) and `case_state` (orchestrator) list `being_watched`: what is followed up for this customer,
  what it waits for and when it was last checked. The orchestrator may tell a returning customer exactly that; it
  still never promises when it will finish.
- The assistant gateway's `get_case_status` shows a `follow_up` per request (state, waiting for, last checked).

## The local event simulator

The test data is static (a fixed `reference_date`), so nothing happens by itself. `simulator.py` keeps a
**simulated clock** and **business events** in one JSON file (`SIMULATOR_STATE`; the demo keeps it with its stores)
and shows them on top of the data reads when `EVENT_SIMULATOR=1`. Mock or Databricks: **the data is never changed**.

- **Clock:** starts at `reference_date` 18:00 UTC (after every event in the data) and moves only when advanced.
- **Events:** `refund_completed`, `refund_failed`, `return_received`, `parcel_scanned`, `parcel_delivered`. They
  change `get_customer_context`, add to `get_case_timeline` and update `get_stuck_cases`.
- **Unconfirmed completion:** the account shows the refund completed but the timeline does not, like a replica
  that lags. It tests the read-back.
- **Review decisions** are not data events: the simulator makes them through the review service, as a person in
  the review console would.
- **Limitation:** the data's own "today" (`as_of`) does not move; only the Guardian's clock does. After advancing
  days, the agents still see the reference date.

## Run it

**Demo.** `.\scripts\demo.ps1` starts the agent server and the console with the simulator on. The Guardian runs
through Orbit: the console sends Orbit a tick every 60 s (`orbit_tick_every_s`). Then:
1. Have a conversation, for example S01 "Where's my refund for the blue jacket?".
2. Open the **Inbox** tab. The watch appears and moves to "waiting on ACME Retail" at its first check.
3. Use **+6 h / +1 day / +3 days** and the event buttons (Refund completes, Refund fails, Carrier scan, Parcel
   delivered, Reviewer approves, ...). The buttons offered are what can happen to that watch now (checked by the
   server).
4. Updates appear under Messages, held ones as "waiting to be sent". Hand-offs appear in the review console.

**Command line** (the newest demo's stores by default, or `-Stores <folder>`):

```powershell
.\scripts\guardian.ps1 status                              # watches, states, updates, what can happen
.\scripts\guardian.ps1 advance -Days 2
.\scripts\guardian.ps1 event -Watch W-... -Event parcel_delivered
.\scripts\guardian.ps1 run                                 # Orbit's clock, when no demo console is running
.\scripts\guardian.ps1 reset
```

`run` sends ticks to the agent server and needs its `ORBIT_TICK_TOKEN` in `.env`. `advance` and `event` are
maintenance commands: they check the watches directly, not through Orbit.

**Production** would send Orbit's tick from a scheduled job (a Databricks job), use the wall clock, and detect
changes with change data feed or streaming instead of re-reading. The rules and state stay the same.

## Evaluation

```powershell
.\scripts\guardian.ps1 eval -Label e3-scripted       # scripted model + mock data, ~5 min
.\scripts\guardian.ps1 eval -Label e3-live -Live     # configured model and backend
```

Each scenario runs one conversation through the agent server, as the website would (`finish_turn` starts the
watches). It then moves the clock and injects events, with a Guardian check after each step. Every check is Orbit's
tick through the same agent server (Orbit → guardian → `run_follow_ups`, no model call), as in the demo; G6's worker
restart restarts the agent server (`evaluation/orbit_client.py`).

| Id | Scenario |
|---|---|
| G1 | S01 refund completes: one update, verified close |
| G2 | S10 stuck shipment after a carrier trace: delay explained, then a person at the target |
| G3 | S13 review rejected: one message, from the reviewer |
| G4 | S13 review approved, then the refund completes |
| G5 | S02 four carrier scans in a morning: the daily limit holds, a newer update replaces a waiting one |
| G6 | S01 worker restart: nothing lost, nothing repeated |
| G7 | S01 completion the timeline does not confirm: never closed, a person after two checks |
| G8 | S02 delivered at 22:00: the closing update waits for 08:00 |
| G9 | S08 refund failed: a person, no re-try |

Scored from the records only: updates sent (kinds, order, time), the watch's final state, hand-off reasons,
**0 false "resolved"** (re-checked against the timeline), **0 promises**, the daily limit and quiet hours, and
**no changes** after the conversation except those a (simulated) reviewer's approval made. Results:
`runtime/eval/<label>/guardian.md`.

## Files

| Path | What |
|---|---|
| `coded_tools/customer_service/guardian.py` | Watches, notices, preferences (SQLite, case store file); rules; templates; `watch_case`; `simulate_event` |
| `coded_tools/customer_service/simulator.py` | Simulated clock and events; `SimulatedAdapter` overlay |
| `coded_tools/customer_service/gateway.py` | `finish_turn` starts the watches |
| `guardian/__main__.py`, `scripts/guardian.ps1` | Command line |
| `coded_tools/customer_service/system_events.py`, `guardian_tools.py` | Orbit's tick: token check, scripted steps (no model), `run_follow_ups` |
| `demo_console/orbit_tick.py` | Orbit's clock: a tick request to the agent server every 60 s, and at once on the clock buttons |
| `demo_console/app.py`, `static/app.js` | Inbox tab, simulator controls |
| `evaluation/guardian_eval.py` | Time-travel evaluation |
| `tests/test_guardian.py` | Unit tests (simulator, rules, limits, read-back, restart) |
