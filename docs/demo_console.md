# Everly demo console

**Everly from ACME Retail** — *Your AI Companion, Always by Your Side* — is the AI companion that ACME Retail (the
store) gives its customers. The header reads "Everly from **ACME** Retail": Everly's companion mark (a speech bubble
with a spark) on the indigo accent, and the retailer's first word as a capitalised wordmark. Until 2026-10-22 the
store was called UrbanLeaf Retail, with a leaf logo and green colours. Brand, tagline and retailer are set in
`demo/demo.yaml`. A friendly front end for the multi-agent support network
(`customer-ai-companion`), built for demos. It has:
- a **simulated website sign-in** that replaces importing sly_data;
- the **customer chat**;
- a live **"Behind the scenes"** view of the Neuro SAN communication: the agent map, a timeline of agent messages and
  tool calls, the case, and the session data.

Recording conversations (takes) is optional and **off unless you switch it on**.

**My Companion** (Everly plan P7): next to the chat, the signed-in customer's tabs — **Updates** (requests being
followed and messages received), **Stylist** (Personal Stylist Mode switch, picks with the reasons, "Not for me"),
**What I know** (facts with their sources, stated preferences to add or delete, earlier requests, download and
reset), **Settings** (companion name, message choices and limits, connected AI assistants). The business side's
**Launches** tab (under "Behind the scenes") launches a staged product and shows the scores. "Launch now" sends Orbit
a launch event through the agent server; Orbit has Aura launch it (no model call, `docs/orbit_tick_plan.md`).

**Stylist Mode set-up:** at start-up the console switches Personal Stylist Mode on for the 15 customers listed under
`stylist_mode_on` in `demo/demo.yaml`. It is recorded as their consent with channel "demo set-up", and a choice
already recorded is kept. Each launch therefore finds customers at 80 or more:

| Launch | Customers |
|---|---|
| Cashmere Scarf | B0196, B0477 |
| Waxed Field Jacket | B0165, B0400 |
| Suede Loafers | B0265, B0419 |
| Linen Overshirt | B0490, B0096 |
| Merino Polo | B0499, B0346 |
| Trail Fleece Jacket | B0195, B0109 |

B0009, B0014 and B0015 score below 80 everywhere. The field-jacket customers also match the scarf, so a field-jacket
launch within a week of the scarf meets the weekly limit (one invite a week). The group is small on purpose: on
Databricks every candidate costs three reads per launch.

**A quick demo:**
1. Open Launches and press "Launch now" on the Cashmere Scarf.
2. Sign in as CUST-B0196 (scenario N1).
3. Look at Stylist, Updates and the Inbox.

**Agent names in the demo** (display only; the network's agent ids are unchanged; `demo_console/agents.py`):
Orbit (orchestrator), Anchor (intake & identity), Swift (orders & delivery), Echo (returns & refunds), Vault
(payments & billing), Halo (loyalty & account), Pulse (product advisor), Aura (personal stylist), Sentinel
(policy & risk). The customer
never sees these names: to them the whole team is **Everly**; Orbit is the internal name of the front agent.

Plan and decisions: `docs/ui_improvement_plan.md`. Design prototype: `prototype/demo_ui/index.html`.

## 1. Run the demo console

```powershell
.\scripts\demo.ps1            # agent server (live mode, fresh stores) + console at http://127.0.0.1:8095
.\scripts\demo.ps1 -Record    # the same, plus a ● Record toggle in the console header
```

- **Customer data:** read from `CUSTOMER_DATA_BACKEND` in `.env`; it is `databricks` for demos. The list holds
  all 523 customers, with the scenario customers pinned on top.
- **Fresh stores:** each start uses new case and action stores (`runtime\demo\stores\console-<time>\`). Each demo
  therefore starts from the same data; otherwise an action done in one demo would be "already handled" in the
  next. `-KeepStores` reuses one folder instead.
- **Ports:** 8080 (agent server) and 8095 (console) must be free. Stop nsflow (`ns run`) first.
- **Stopping:** Ctrl+C stops both.

### Using it

1. **Pick a customer.** Search or filter (Demo scenarios / Other customers), then select a card. The panel shows
   "Website, signed in" and the exact sly_data the website gateway will send
   (`{"channel": "web_session", "identifier": "sess_…"}`).
2. **Sign in and open chat.** The console keeps the sly_data on the server; the browser never sends it. The agents
   resolve the customer **in code** (`intake_identity`), so the model never sees or chooses the customer id.
3. **Ask a question.** Use a suggested question (the scenario's own messages) or type one. While the agents work, a
   status line says who is doing what.
4. **Behind the scenes:**
   - **Agent activity:**
     - The map highlights the active link and labels the tool in use.
     - The timeline lists every agent request and reply, and every tool call with its result in plain English;
       each row expands to the full arguments and result.
     - "Technical view" switches the rows to raw tool names and arguments. "Tool calls" and "Map" hide or show
       those parts.
   - **Case:** the case with its issues and their status, the policy decisions (ALLOW / DENY / ESCALATE), the
     actions with their reference, and any hand-offs, read from the case store.
   - **Inbox** (E3): the updates the customer receives (the outbox), the Resolution Guardian's watches with their
     state, and the event simulator: advance the clock and make things happen (refund completes, parcel
     delivered, reviewer approves, ...). A counter on the tab shows new messages. Guide:
     `docs/resolution_guardian.md`.
   - **Session data:** the sly_data sent, what the gateway keeps after the turn (`case_id` only), the identity
     result, and the turn's LLM calls, tokens and time.
5. **Show agents** hides the right-hand panel for a customer-only view. **New conversation** keeps the customer
   and starts a fresh chat. **Switch customer** returns to the sign-in page.

Settings are in `demo/demo.yaml`:
- brand and tagline;
- `not_signed_in` (off; set it to `true` to offer an anonymous visitor);
- the agent server URL and the per-message timeout;
- `orbit_tick_every_s` (default 60): Orbit's clock (on the map: the dashed "Orbit's clock" box; its ticks and
  launches play on the map between turns and are listed under it, with "update" / "new-in pick" to the customer when
  the signed-in customer got one).
- Personal AI assistants (`scripts\demo.ps1 -Assistant`, `docs/assistant_lane_plan.md`): when the signed-in customer
  has connected one, its box ("Claude · AI assistant") appears on the left; its `ask_support` turns play on the map
  once finished (assistant → Orbit → agents → assistant) and are listed under it ("is asking…" while running, a
  "read-only" pill when a change was refused); its status reads are listed only. Only the customer's own. Every tick the console sends Orbit a request through the agent
  server; Orbit calls the Guardian (follow-up checks) and Aura (held picks), decided in code with no model call
  (`docs/orbit_tick_plan.md`). The Inbox tab's clock and event buttons tick at once. `scripts\demo.ps1` gives both
  processes the ticks' secret (`ORBIT_TICK_TOKEN`, from `.env` or new per start).

## 2. Recordings (takes)

A **take** is one recorded conversation: every event the page received, with real timestamps, plus the answers,
case snapshots and cost. Takes are stored in `runtime\demo\recordings\<scenario>\<take id>\`.

**A take's verdict** is scored with the evaluation code, the same rules as the test reports:
- **PASS / FAIL** when the customer and messages match a scenario;
- **UNSCORED** for free-form conversations.

To record during a manual demo: `.\scripts\demo.ps1 -Record`, then switch **● Record** on. Each conversation
becomes a take when you switch it off, start a new conversation or switch customer.

## 3. Files

| Path | What |
|---|---|
| `demo_console/` | App (`app.py`), catalog, sessions, stream normaliser (`stream.py`), agent summaries (`agents.py`), takes (`recording.py`), page (`static/`) |
| `demo/demo.yaml` | Console settings |
| `scripts/demo.ps1` | Start the console |
| `runtime/demo/` | Stores and takes (local only) |
| `tests/test_demo_console.py` | Offline tests (normaliser, sign-in, scoring) |

**Changes outside `demo_console/`:** one read-only customer-list query (`list_customers`) in
`databricks_adapter.py` and `mock_adapter.py`, used only by the console's customer picker.

The agent network, its tools and the policy are unchanged.
