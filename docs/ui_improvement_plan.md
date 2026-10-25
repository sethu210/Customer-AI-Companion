# Demo console: plan for a user-friendly UI that shows the agents at work

Status: **built 2026-10-20**; see `docs/demo_console.md` for how to use it. Later decisions: "Not signed in" is off,
there is no review-console step in videos, and the voice is a British English female offline voice.

**Decisions (2026-10-20):**
- The prototype layout is fine.
- Channel: **website sign-in only**; phone is out for now. "Not signed in" is kept behind a switch, off by default.
- The timeline **shows tool calls**.
- Branding: **ACME Retail**, tagline **"Everyday Essentials, Expertly Curated"**.
- Data: **Databricks only**. Model: **the free model only**.
- A **recording capability** (§7) is built but stays **off unless switched on**. Recordings are made when the user
  chooses, and later combine into **one full video of selected scenarios**, with an optional **voice-over**.
  Installing Playwright + Chromium and ffmpeg is approved.

Original proposal follows. A clickable prototype is in
`prototype/demo_ui/index.html` (open it in a browser; no server needed). It uses the real test customers and replays
the recorded M2 conversation (Daniel Okafor: stuck refund + loyalty points, 19 LLM calls).

## 1. What we want

1. **Pick a customer and "sign in" instead of importing sly_data.** The person running the demo chooses a customer
   and a channel (website / email / phone, or "not signed in"). The backend builds the sly_data a real channel
   gateway would send, so the agents' identity check runs exactly as in production.
2. **A friendly chat for the customer side**, and next to it a **"Behind the scenes" view of the Neuro SAN
   communication**: which agent is talking to which, which tools they use, what came back, how the case changes.
3. Keep **nsflow** as the developer tool. The demo console is a separate, simpler page for demos and testers.

## 2. Recommended approach: a small "demo console" app (like the review console)

Build it the same way as `review_console/` (FastAPI + one HTML page, local only), as `demo_console/` on
`http://127.0.0.1:8095`. It talks to the running Neuro SAN server (`ns run --server-only` is enough; nsflow is not
needed for it).

Why not change nsflow: it is a packaged React build (nsflow 0.7.4 in `.venv`); customising it means forking the
front end and redoing it on every upgrade, and its screens are built for developers (sly_data editor, raw logs).

```
 Browser (demo page)                demo_console (FastAPI, 127.0.0.1:8095)            Neuro SAN (8080)
 ─────────────────────              ─────────────────────────────────────            ────────────────
 pick customer + channel ──POST /api/session──▶ looks up the channel identity
                                                (adapter: mock or Databricks)
                                                keeps sly_data SERVER-SIDE
 type message ───────────WS /api/session/{id}/chat──▶ POST streaming_chat ─────────▶ support_multi_agent
                                                       sly_data + chat_context        (MAXIMAL filter: every
 ◀── events: agent→agent, tool call/result, ◀───────── normalises each streamed ◀──── agent message)
     case updates, final answer, cost                  message; reads case store
```

### Backend (demo_console/app.py)

| Endpoint | What it does |
|---|---|
| `GET /api/customers?query=&kind=scenario\|all&page=` | Customers from the active backend (`customers` + `channel_identities`; read-only, the adapters already have this data). The 23 scenario customers are pinned on top with their title, outcome class and suggested questions from `evaluation/expectations.py`. Paged, so the 500 background customers work too. |
| `POST /api/session {customer_id, channel}` | **Simulated sign-in.** Picks that customer's identity row for the channel and builds the gateway sly_data `{"channel": ..., "identifier": ...}`, exactly what `evaluation.runner.gateway_sly_data` sends today. Option `"not_signed_in"` sends `{"channel": "web_session"}` only. Returns a session id; **the browser never sends or edits sly_data.** |
| `WS /api/session/{id}/chat` | Sends each customer message to `http://localhost:8080/api/v1/customer_service/support_multi_agent/streaming_chat` with the session's sly_data, the `chat_context` from the previous turn, and `chat_filter: MAXIMAL`. Streams back normalised events (below). Keeps the returned `case_id` (the only thing `allow.to_upstream` returns) for the next turn. |
| `GET /api/case/{case_id}` | Case, issues, findings, proposals, policy decisions, actions and hand-offs from the case and action stores (`CaseStore().case_view`, `ActionStore`). |
| `POST /api/session/{id}/reset` | New conversation (new chat context) for the same customer. |
| `GET /api/replays`, `GET /api/replays/{label}/{id}` | Optional: plays back a recorded run from `runtime/eval/<label>/multi_streams/<id>.jsonl` (see §5). |

Normalised event (one per streamed agent message; parsing reuses `evaluation.runner.parse_stream` and
`evaluation.ui_runner.summarise_turn`):

```json
{"t": 54.9, "kind": "delegate|tool_call|tool_result|agent_reply|answer|case|cost",
 "from": "returns_refunds", "to": "find_order_items", "path": ["support_orchestrator", "returns_refunds", "find_order_items"],
 "summary": "Finds the item: orange Trail Running Shoes, $119.", "detail": {"args": {...}, "result": {...}}}
```

The path comes from each message's `origin`/`otrace` (Neuro SAN already sends it). "Invoking `x` with" means a
call to x, "Got result" or "result from x" is the reply. Plain-English summaries come from a small table per tool,
with the technical text as a fallback (for example "find_order_items → unique: Trail Running Shoes").

### Safety for the simulated sign-in

- It is a **demo feature**, switched on only with `DEMO_LOGIN=1`, bound to 127.0.0.1, and labelled "Simulated
  sign-in" on screen. A production deployment replaces it with the real channel gateway.
- Identity stays as designed: the console sends only the raw channel identity, and `intake_identity` /
  `ensure_identity` resolve it in code. The model never sees or picks the customer id. The ambiguous shared phone
  (S18), unverified identities and "not signed in" can all be shown honestly.
- No changes to the agent network, its tools, or the identity contract are needed.

## 3. Screens (as in the prototype)

**Sign-in screen**
- Search box, plus filter chips: All / Demo scenarios / Other customers.
- Customer cards showing name, id, tier, what the scenario demonstrates, and the expected outcome (Answer / Fix it /
  Explain no / Hand to a person / Not identified).
- Side panel: the channels this customer really has, each with its sign-in strength and what it allows, plus
  "Not signed in", and a preview of the exact sly_data the gateway will send.

**Console**
- **Chat (left):** the customer header shows how they signed in. Suggested questions come from the scenario, and a
  live status line ("Returns & Refunds is using get_stuck_cases… 55 s") makes the 1–5 minute free-model wait
  understandable. Under each answer: LLM calls, time, issues resolved and actions executed, plus "See how the agents
  got here".
- **Behind the scenes (right),** a panel the presenter can hide with "Show agents":
  - **Agent activity:**
    - A network map of the customer, the orchestrator and the 6 specialists. The active link animates and the
      tool in use is labelled.
    - Below it, the **conversation between agents** as a timeline. Each step shows from → to and a plain-English
      line, with the arguments and result expandable.
    - "Technical view" switches to raw tool names and arguments. "Tool calls" hides or shows tool steps.
    - Parallel calls are marked ∥.
  - **Case:** the case id, identity, and each issue with its specialist and status (open → investigating → fix
    proposed → fixed and verified / answered / with a person). It also shows the policy decisions (ALLOW / DENY /
    ESCALATE with the reason) and the actions with outcome and reference. Items waiting for review link to the
    review console.
  - **Session data:** the sly_data sent, the sly_data after the turn (customer_id, auth_level, identity_status,
    case_id; never shown to the model), what is returned upstream, and the cost of the turn.
- Header: signed-in pill, New conversation, Switch customer, light/dark theme.

## 4. Build phases (after approval)

| Phase | Scope | Size |
|---|---|---|
| 1. Sign-in + chat | `demo_console` app: customers, sessions, chat proxy to Neuro SAN, final answers only. Unit tests: the gateway sly_data per channel, "not signed in", and that the browser cannot set sly_data. | ~1 day |
| 2. Agent activity | Stream normaliser (reusing the runner parsers), timeline, network map, plain-English summaries per tool. Tests against the saved stream files. | ~1–1.5 days |
| 3. Case panel + review link | Case/action store view refreshed on case events; link to the review console; Session data tab. | ~0.5 day |
| 4. Replay mode + polish | Play recorded runs at 1×/4×/15× (demos without waiting for the free model), mobile layout, a collapsible map for small screens, accessibility pass, README and a `scripts/demo.ps1` that starts server + console. | ~1 day |

No change to the agent network, specialist tools or policy is needed in any phase.

## 5. Decisions taken (2026-10-20)

| Question | Answer |
|---|---|
| Separate demo console or inside nsflow | Separate demo console (prototype approved) |
| Channels | Website sign-in only; phone ignored for now; "Not signed in" behind a switch, off by default |
| Tool calls in the timeline | Shown |
| Branding | ACME Retail, "Everyday Essentials, Expertly Curated" |
| Recordings | Build the capability now, off by default; use it later on demand |
| Video | One full video of scenarios chosen later (not one video per scenario) |
| Data / model | Databricks only; the free model only |
| Voice-over | Build the capability; used only when recording is chosen |
| Installs | Playwright + Chromium and ffmpeg approved |

## 6. Recording capability

Nothing records unless it is switched on. Recordings are made when you choose, kept in a local library, and combined
into **one full video** of the scenarios you pick later, with an optional **voice-over**.

### 6.1 How it works: record live, then render compressed

Recording the screen in real time and cutting out the idle parts would be fragile, so the work is split in two steps.

Recording the screen in real time and cutting out the idle parts would be fragile, so the work is split in two steps:

1. **Capture (live, real agents).** The recorder signs in as the scenario customer through the demo console API
   and posts the real question(s). It saves everything the page received to a **session recording**
   (`runtime/demo/recordings/<scenario>-<timestamp>.json`):
   - every agent event, with its real timestamp;
   - the final answers and the case/action snapshots;
   - the cost and the real elapsed time.

   This is exactly the stream the live UI shows, so nothing is staged.
2. **Render (video).** A headless Chromium (Playwright) opens the demo console in **replay mode** on that recording.
   It plays the recording with a *presentation clock* instead of the real one and records the browser to video:
   - **Kept and held:**
     - the title card;
     - sign-in, with the customer card and "Website, signed in";
     - the question being typed and sent;
     - each agent hand-off (orchestrator → specialist and its reply), at about 1.5 s each;
     - the key tool results, at about 1 s each: the stuck-case diagnosis, the policy decision ALLOW/DENY/ESCALATE,
       and the action executed with its reference;
     - the case panel at the end;
     - the final answer, held for about 6 s;
     - the end card.
   - **Compressed:** routine reads (profile, timeline, findings), shown at about 0.4 s each and still visible in the
     timeline.
   - **Removed:** the LLM waiting time. A small chip ("⏩ 24 s of model thinking skipped") appears where a gap
     was cut, and a corner clock shows "real time 2:08 · shown in 0:42", so the video stays honest.
   - Each scenario becomes a **segment**. Segments are only joined into a video by the reel step (§6.4).

Because rendering only uses a saved recording, it can be re-run as often as needed (other timing, scenario choice
or voice-over) without calling the model again. Capturing uses the live model; rendering is free.

### 6.2 Switching recording on

| Mode | How | What is saved |
|---|---|---|
| **Off (default)** | `scripts/demo.ps1` | Nothing. The demo console works normally. |
| **Record a manual demo** | `scripts/demo.ps1 -Record` adds a **● Record** toggle to the console header | Each conversation you have with the toggle on becomes a take in the library. |
| **Record scenarios unattended** | `scripts/record_demo.ps1 capture -Ids M2,S13,S14,S01` | For each scenario: website sign-in as its customer, the question(s) typed as in the scenario file (multi-turn too, e.g. S05, S09, M3), and a wait for each answer. |

**Library:** `runtime/demo/recordings/<scenario>/<take>/`
- `recording.json`: every event with its real timestamp, the answers, case and action snapshots, cost and elapsed time;
- `meta.json`: date, model, backend, the agent network's file date, and the **verdict** from the existing scoring
  (`evaluation.score`).

`record_demo.ps1 list` shows the takes, and `record_demo.ps1 pick <scenario> <take>` marks the one to use.

**Before each capture run** (Databricks only, free model only):
- Check that the Databricks CLI login has more than 15 minutes left; if not, ask you to run `databricks auth login`.
- Wake the SQL warehouse with a tiny query.
- Check the OpenRouter key: still valid (it currently expires 2026-10-22) and enough free requests left for the
  chosen scenarios (about 20 per scenario).

**Free-model takes:** a take that errors or gets the wrong outcome is marked **failed**. The run retries it up to
`-Retakes 2` times. Failed takes are never used in a video unless you pick them explicitly.

### 6.3 Pieces to build

| Piece | What it does |
|---|---|
| Recorder hook in the demo console | When recording is on, the chat proxy also writes every normalised event to the take. It is the same stream the page shows, so what is recorded is what was seen. |
| `demo/recorder.py capture / list / pick` | Unattended scenario runs, preflight checks, retakes, verdicts, and the library. |
| Replay mode in the demo console | `?replay=<take>&clock=presentation`: the same page, fed from the file instead of the live socket. The pacing rules live in one file (`demo/timing.yaml`) that is easy to tune. |
| Overlays | Scenario title card, short captions on key steps ("Policy check: refund allowed"), skipped-time chips, real-vs-shown clock, and a scenario end card (outcome, LLM calls, real time). |
| `demo/recorder.py render` | Playwright drives the replay page and records each segment. |

### 6.4 One full video from selected scenarios

You choose the scenarios later in **`demo/reel.yaml`**:

```yaml
title: "ACME Retail — AI customer support"
tagline: "Everyday Essentials, Expertly Curated"
resolution: 1920x1080
scenarios:            # order in the video; take: "picked" (default) or a take id
  - id: S01
  - id: M2
  - id: S13
  - id: S14
voice_over: true      # see §6.5
```

`scripts/record_demo.ps1 reel demo/reel.yaml` builds the video:
1. An intro card with ACME Retail, the tagline, and a short "what you will see" slide showing the 7-agent
   map.
2. Each selected scenario's segment, rendered from its picked take.
3. An outro card: the scenarios shown, how many were resolved without a person, and the total real time against the
   time shown.

The parts are joined with ffmpeg into **`runtime/demo/videos/<title>-<date>.mp4`** (1080p, H.264). The reel step
only uses saved recordings, so it never calls the model and can be rebuilt any time.

### 6.5 Voice-over (only when you choose it)

Off unless `voice_over: true` is set in `reel.yaml` or `-VoiceOver` is passed.

1. **Narration script, drafted automatically.**
   - `record_demo.ps1 narrate demo/reel.yaml` writes `demo/narration/<reel>.yaml`.
   - The draft gives one short line per key moment, made from fixed templates over the recorded events, for
     example "Daniel asks where his refund is. The orchestrator hands the question to the Returns and Refunds
     specialist."
   - Each line is tied to an **event**, not to a time, so it stays in sync if the pacing changes.
   - You can edit, delete or add lines freely before rendering.
   - Polishing the wording with the free model is available on request, never by default.
2. **Speech.** Pluggable voices:
   - `windows` (default): the built-in Windows voices. Offline and free, but a bit robotic.
   - `edge`: Microsoft's neural voices through the `edge-tts` package. Free and more natural, but it needs
     internet and is an unofficial service.
   - `file`: your own recordings, one audio file per line (`demo/narration/audio/<line-id>.wav`), so you can
     narrate in your own voice.
3. **Sync.**
   - The renderer measures each spoken line and holds that step on screen until the line ends, so speech never
     runs over the next step.
   - ffmpeg places each clip at its cue and mixes in an optional background music file at low volume (yours; none
     is bundled).
4. **Subtitles.** A `.srt` file is written from the same script and can be burned into the video if wanted.

### 6.6 Commands (summary)

```powershell
.\scripts\demo.ps1                                         # demo console, recording off
.\scripts\demo.ps1 -Record                                 # demo console with the ● Record toggle
.\scripts\record_demo.ps1 capture -Ids M2,S13,S14,S01      # unattended takes (Databricks, free model)
.\scripts\record_demo.ps1 list                             # takes with verdicts
.\scripts\record_demo.ps1 pick M2 2026-10-21T1012
.\scripts\record_demo.ps1 narrate demo\reel.yaml           # draft the voice-over script (edit it after)
.\scripts\record_demo.ps1 reel demo\reel.yaml [-VoiceOver] # one full video
```

### 6.7 Dependencies and resources

- **New Python packages:** `playwright` (plus `playwright install chromium`, about 150 MB), `imageio-ffmpeg`
  (ffmpeg, about 30 MB), `pyttsx3` (Windows voices), and `edge-tts` (only if that voice engine is used).
- **Memory:** headless Chromium (about 300 MB) runs only while rendering, never during capture. Capture needs only
  the server and console, as now.
- **Data stays local:** recordings hold test data and identifiers only, under `runtime/demo/`.

### 6.8 Build order and size (after the demo console)

| Step | Scope | Size |
|---|---|---|
| A | Demo console, phases 1–3 (§4) with ACME Retail branding and website-only sign-in | ~3–3.5 days |
| B | Recording on/off, recorder hook, unattended capture, preflight, retakes, library | ~0.5–1 day |
| C | Replay mode, presentation clock, overlays, segment rendering | ~1 day |
| D | Reel builder (`reel.yaml`, intro/outro, joining) | ~0.5 day |
| E | Voice-over: narration draft, voice engines, sync, subtitles, mixing | ~1–1.5 days |

Each step ends with tests and a short check: B and C on a recorded M2 take, D on a two-scenario reel, and E on a
narrated reel with the Windows voice.

## 7. Known limits of the prototype

- It replays one recorded conversation (Daniel Okafor). Other customers can sign in, but their messages get a
  placeholder reply.
- The case panel is driven by the recording, not the case store.
- It lists 15 sample customers; the real app pages through all 523.
- On short screens (about 600 px high) the map leaves little room for the timeline. The build should let the map
  collapse.
