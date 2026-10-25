# Orbit in control: the Guardian and Aura as agents, a 60 s clock and launch events

Plan agreed 2026-10-23. Option C: Orbit (support_orchestrator) is triggered by a clock tick every 60 s and by
new-in launches, and in turn calls the Guardian and Aura through the agent network. Code decides every step:
Orbit's and the called agents' middleware answer in place of the model, so the delegation is real (it is in the
agent server's message stream, the demo map, nsflow and traces) but no model call is made.

## Why

- Today the Guardian runs outside the network: `finish_turn` starts watches after Orbit's reply, and the demo
  console's `_guardian_loop` calls `Guardian.run_once()` directly every 15 s. The Launch panel calls
  `Stylist.launch()` directly, and the same loop calls `Stylist.run_once()` for held invites. None of it is in the
  agent server's stream, so the map never shows it.
- Agent-to-agent calls in Neuro SAN are tool calls made by an agent's model; `demo_console/stream.py` turns them
  into `delegate` / `agent_reply` / `tool_call` events for the map.
- Orbit's real model on every tick (option B) would cost ~2 calls per tick (~2,900 a day at 60 s, above the free
  model's 1,000) and let the model decide when updates go out. Option C costs no model calls.

## Decisions (2026-10-23)

| # | Decision | Chosen |
|---|---|---|
| 1 | How Orbit is triggered | **C**: tick/event requests to Orbit; middleware answers in place of the model |
| 2 | Clock | **Every 60 s** (`orbit_tick_every_s`); the simulated clock still moves only with the buttons |
| 3 | Where watches start | In code at the end of each turn (`finish_turn`), drawn as Orbit → Guardian "start watch" |
| 4 | Stylist invites | Through the network: launch → Orbit → Aura; held invites on each tick Orbit → Aura |
| 5 | Name on the map | "Guardian" |

## Phases

**T0. Check that it works (first)**
- Orbit's middleware returns a fixed `AIMessage` with a tool call to an agent instead of calling the model; Neuro
  SAN runs that agent; the called agent's middleware does the same for its coded tool; the stream shows the
  delegation; no model is called. Both paths: Orbit → Guardian and Orbit → Aura.
- If it does not work: option D (Orbit's real model, only on ticks that have work).

**T1. Guardian agent** (multi and lean networks)
- `guardian` agent ("Guardian", "Follow-ups"), tools `run_follow_ups` (`Guardian.run_once()`, returns the summary)
  and `follow_up_status` (read-only). Its middleware scripts the one step. Rules, templates, outbox unchanged.

**T2. Orbit decides on ticks in code**
- Tick request: sly_data `{"channel": "system_tick", "tick_token": <secret from .env>}`.
- Orbit's middleware checks the token and scripts: `guardian` → Aura (`release_picks`) → fixed final answer.
- On customer turns the middleware removes `guardian` from the tools the model sees.
- A tick never opens a case, runs intake, uses companion notes or touches a thread; `finish_turn` is not called.

**T3. The 60 s clock**
- The demo console's ticker (`orbit_tick_every_s: 60`) replaces `_guardian_loop`; it sends a tick to Orbit
  through the agent server. Clock and event buttons move the simulated clock, then tick at once.
- The agent server gets `CASE_STORE_PATH`, `ACTION_STORE_DIR`, `SIMULATOR_STATE`, `EVENT_SIMULATOR=1` (`demo.ps1`).
- Only Orbit's ticks run checks (one process: the Guardian's run lock is per process).

**L1. Launch event**
- The Launch button sends Orbit sly_data `{"channel": "system_event", "event": "launch", "product_id", "tick_token"}`.

**L2. Orbit → Aura**
- Orbit's middleware scripts a call to `stylist` (Aura), then a fixed answer. Aura's new middleware reads the event
  from sly_data and scripts one call to `launch_new_in` (`Stylist.launch()`). Scoring, gates, the 80 threshold and
  the messaging limits are unchanged.

**L3. Held invites**
- Each tick: Orbit → Guardian, then Orbit → Aura `release_picks` (`Stylist.run_once()`); service updates first.
  The console's direct `Stylist().run_once()` calls (`app.py:66`, `app.py:254`) go.

**L4. Customer turns unchanged**
- Aura's middleware hides `launch_new_in` and `release_picks` from its model on customer turns.

**T4 + L5. The picture**
- `agents.py`: `guardian` → ("Guardian", "Follow-ups · code"), summaries for the new tools.
- Left: a "Triggers: clock 60 s · new-in launch" box linked to Orbit; the Guardian in the specialists' column.
- Tick and launch streams go through `stream.py` into a background lane (not the customer's chat): Triggers → Orbit
  → Guardian / Aura, and → Customer when the signed-in customer gets an update or invite.
- End of a customer turn: a dashed Orbit → Guardian "start watch" link. nsflow shows the Guardian from the hocon.

**T5 + L6. Tests, evaluations, docs**
- Unit tests: token check, hidden tools on customer turns, ticks never touch cases or threads, the scripted paths,
  launch through Aura gives the same invites as `Stylist.launch()` directly.
- Scripted runs unchanged: guardian G1–G9 9/9, journeys J1–J10 10/10, N1 stylist accuracy, `eval.ps1` 0 unsafe.
- Map checked with headless Playwright (light, dark, 390 px).
- Docs: `resolution_guardian.md`, `demo_console.md`, `companion_plan.md`, README, the hocon header.

## Order of work

T0 (both paths) → T1–T3 → L1–L4 → T4/L5 → T5/L6.

## Progress

| Phase | State |
|---|---|
| T0 | **passed** (2026-10-23), see below |
| T1 | done (2026-10-23): `guardian` agent + `run_follow_ups` (`guardian_tools.py`) in both networks. `follow_up_status` dropped: nothing would call it (`case_state` already lists `being_watched`) |
| T2 | done (2026-10-23): `system_events.py` (`ScriptedEventMiddleware` on Orbit, guardian, stylist; token check); tick = Orbit → guardian → Orbit → stylist (`release_picks`, so L3 is done too) |
| T3 | done (2026-10-23): `demo_console/orbit_tick.py` (`OrbitClock`), `orbit_tick_every_s: 60`, buttons tick at once (`check_customer` for simulated events), token from `demo.ps1`, `guardian run` ticks Orbit |
| L1 | done (2026-10-23): launch event (channel `system_event`, `event: launch`, `product_id`, `launched_by` in sly_data; unknown events refused); `OrbitClock.launch()`; the Launch panel's button (`POST /api/launches/{id}`) goes through it |
| L2 | done (2026-10-23): Orbit → Aura → `launch_new_in` (`stylist_tools.LaunchNewIn`, product from sly_data, returns counts only; `Stylist.launch()` unchanged) |
| L3, L4 | done with T2: `release_picks` on each tick; `launch_new_in`/`release_picks` hidden from Aura's model on customer turns |
| T5, L6 | done (2026-10-23): `evaluation/orbit_client.py`; `guardian_eval` and `journey_eval` check, release and launch only through Orbit (ticks, launch events) on their agent server, which now stays up for the steps; G6's worker restart restarts the agent server (`AgentServer.restart`). Scripted: guardian **9/9** (`runtime/eval/t5-guardian`), journeys **10/10**, 0 invariant breaks, 8.93 model calls per turn (`runtime/eval/t5-journeys`), both unchanged. Unit tests 539 passed, 2 skipped |
| T4, L5 | done (2026-10-23): map has "Orbit's clock" (triggers, bottom left) → Orbit, and the Guardian; code-only boxes dashed. `OrbitClock` keeps the last 20 requests with who got a message; `GET /api/session/{id}/clock?after=seq` (this customer's `for_you` only). Ticks and launches play on the map between turns (never in the timeline) and are listed under it; "update" / "new-in pick" link to the customer when they got one; "start watch" Orbit → Guardian at the end of a turn (`turn_end.watching`). Inbox: "Simulated time" and Orbit's tick note |

### T0 result (2026-10-23)

A throwaway network (Orbit, Guardian, Aura; a model class that raises if called) ran in process through Neuro SAN's
`DirectAgentSessionFactory`, with mock data and temporary stores, its stream fed through `demo_console/stream.py`:

| Request | Stream (as the demo map sees it) | Result |
|---|---|---|
| tick | Orbit → Guardian → `run_follow_ups` → Orbit → Aura → `release_picks` → Orbit | real `Guardian.run_once()` and `Stylist.run_once()` ran |
| launch PRD-CSC | Orbit → Aura → `launch_new_in` → Orbit | real `Stylist.launch()`: 15 candidates, 13 scored, 4 invited (outbox written) |
| customer turn | Orbit → Aura | Orbit's model would see only `stylist` (no `guardian`); Aura's only `get_stylist_picks` |

- **0 model calls** in all three: middleware returning an `AIMessage` (with or without tool calls) replaces the
  model call (LangChain `ModelCallResult`), and Neuro SAN runs the agent and coded tools for those calls as usual.
- sly_data reaches the called agents' middleware and tools, so the event and product id travel in sly_data,
  never in model-written arguments.
- Filtering `request.tools` in the middleware hides tools from the model on customer turns.
- Notes for T1: the in-process stream has enum message types (the HTTP stream has strings; `stream.py` expects
  strings). A tool result reaches the next step as a Python-literal string, which `stream.parse_text` already reads.
  Each agent still needs a constructible LLM (keys present), even though it is not called.
