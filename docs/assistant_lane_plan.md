# Personal AI assistants on the demo map

Planned 2026-10-23; **built 2026-10-23** with the recommended decisions (play after the turn finishes; status reads
listed only; the box takes the assistant's registered name). Prototype: `docs/screenshots/assistant_lane/
prototype.html` (`prototype_map.png`, `prototype_phone.png`); screenshots of the built version in the same folder
(`01_...` to `07_...`).

## Today

A customer's own AI assistant (for example Claude or ChatGPT) reaches Everly through the **assistant gateway**
(E2, `assistant_gateway/`, port 8096, MCP, read-only; `docs/assistant_gateway.md`):

- `get_case_status`, `get_order_status`: read the stores directly; no model, no agents.
- `ask_support(message)`: one turn to **Orbit through the agent server**, in the background
  (`assistant_gateway/support.py run_turn`: `stream_turn`, then `finish_turn`), fetched with `get_answer`.

The demo console never sees any of it: the gateway is a separate process, and `run_turn` reads the turn's stream
only to the end and keeps no events (`async for _ in stream_turn(...)`). So the map shows the website chat and
Orbit's clock, but not the assistant. `scripts\demo.ps1` does not start the gateway either.

## Goal

When the signed-in customer's assistant asks Everly something, the map shows it the way it shows Orbit's clock:
a **"<client> · AI assistant"** box on the left linked to Orbit, the turn playing on the map between the customer's
website turns (assistant → Orbit → Anchor → Echo … → Orbit → assistant), and a row in the background lane under the
map. Status reads (no agents) get a lane row only. Only the signed-in customer's own assistants are shown.

## Plan

**A1. The gateway records what happened** (`assistant_gateway/support.py`, `server.py`)
- `run_turn` keeps the map events (`delegate`, `agent_reply`, `tool_call`, `tool_result`) that `Normaliser` already
  produces, instead of discarding them.
- Each `ask_support` turn, and each status-tool call, appends one record to an activity file in the shared action
  store (`ACTION_STORE_DIR/assistant_activity.jsonl`): `seq`, customer id, client name (from the grant), tool,
  start and end time, status (`running` / `done` / `failed`), the map events, the case id and a one-line summary.
  A `running` record is written at the start, so the lane can show "asking…" during a turn that takes minutes.
- What is written is what the case record and the stream already hold (no token, no sly_data, no model text beyond
  the reply's first line). The customer id stays server-side: the console filters by it.

**A2. The console serves it to the signed-in customer only** (`demo_console/app.py`)
- `GET /api/session/{id}/clock` (the background-lane endpoint from T4) also returns the activity records of **this
  customer** after a cursor. Other customers' assistant activity is never sent to the page.
- Read straight from the shared file; nothing changes in the gateway's MCP tools or their answers.

**A3. The map and the lane** (`static/app.js`, `app.css`)
- A box on the left, between "Customer" and "Orbit's clock": "<client name> · AI assistant", subtitle
  "<customer's first name>'s · MCP · read-only". Solid, not dashed (it has its own model), own colour
  (`--a-assistant`, light and dark). Linked to Orbit. Shown only once the customer has a connected assistant
  (from My Companion's assistant list); with several, the most recent one's name, the others in the lane.
- A finished `ask_support` turn plays like a tick (assistant → Orbit → agents → Orbit → assistant), compressed to a
  few seconds, between website turns only; "ask_support" labels the link. Its lane row: time, "Assistant", the
  question (shortened), the agents in order, the result, and a "read-only" pill when a change was refused
  (`confirm_in_channel`).
- Status reads: a lane row only ("Claude read the order status (no agents)").
- `layout`: the left column gets a third box, so "Customer" moves up and the assistant box sits at the middle; the
  map height is unchanged (the specialists' column decides it).

**A4. Running it in the demo** (`scripts\demo.ps1`)
- A `-Assistant` switch also starts the gateway with the demo's stores (`CASE_STORE_PATH`, `ACTION_STORE_DIR`,
  `ASSISTANT_STORE_PATH`, `SIMULATOR_STATE`), so a test token from `scripts\assistant_gateway.ps1 token -Customer
  CUST-S01` (new `acme_` token) shows up on Maya's map. Without the switch, nothing changes.

**A5. Tests, checks and docs**
- Unit tests: the gateway writes one record per call (running, then done) with the map events; a failed turn is
  recorded as failed; `/clock` returns only the signed-in customer's records; no token or sly_data in the file.
- The assistant evaluation (`evaluation/assistant_eval.py`, 8/8 scripted) and the unit suite unchanged.
- Playwright: a scripted `ask_support` for CUST-S01 while Maya is signed in plays on the map; light, dark, 390 px.
- Docs: `assistant_gateway.md`, `demo_console.md`.

## Decisions for the user

1. **Play live or after?** Recommended: play the turn **after it finishes**, compressed (like ticks), with an
   "asking…" lane row while it runs. Live playback means reading a file the gateway is still writing; a free model's
   turn takes minutes.
2. **Show status reads?** Recommended: lane row only, no animation (no agents are involved).
3. **Box name:** the assistant's registered client name ("Claude · AI assistant"), recommended, or a fixed
   "Personal AI assistant".

## Not in scope

Changes from an assistant (E4, confirm link) and the gateway's MCP behaviour, limits and audit stay as they are.
