# Assistant gateway (E2): personal AI assistants over MCP, read-only

A customer's own AI assistant (Claude, ChatGPT, Copilot, Gemini, …) is **another channel**. It can read the
customer's orders and support cases and ask support questions for them. In E2 it **cannot change anything**.

Plan: `docs/enhancements_plan.md` (E2). Code: `assistant_gateway/`.

## Why not Neuro SAN's built-in MCP server

Neuro SAN can serve a network as one MCP tool (opt-in per manifest entry with `"mcp"`). For this project it has
three problems:
- **The MCP client sends `sly_data` itself.** The assistant would say who the customer is. The core rule is that
  identity comes from a token checked in code.
- **The call waits for the whole turn.** That takes minutes on the free model, longer than MCP clients wait.
- **No gateway checks.** `finish_turn` would not run.

So the support networks stay off its MCP list (no `"mcp"` key in `registries/manifest.hocon`). A thin gateway does
the job instead.

## How it works

```
assistant ──MCP + Bearer token──▶ assistant gateway :8096 ──HTTP streaming_chat──▶ agent server :8080
                                   │ token → grant (customer, sign-in level, scopes) in code
                                   │ scope check, rate limits, audit
                                   └ case store / data layer (status tools, no LLM)
```

- **Identity:** the token maps to a grant in `GrantStore`. A grant holds the customer, the sign-in level and the
  scopes. The gateway builds `sly_data {customer_id, channel: "assistant", auth_level}`, as the website gateway
  does. The model never sees the token, and tool arguments cannot name a customer. Only a SHA-256 hash of each
  token is stored. Tokens start with `acme_` (before the ACME rebrand on 2026-10-22: `ula_`, no longer valid).
- **Read-only:** `config/service_policy.hocon` lists `read_only_channels: ["assistant"]`. `policy.decide()`
  refuses every change on that channel with the reason `confirm_in_channel`, before any other rule. The request is
  still investigated, answered and recorded. The customer is told to do it in the ACME Retail app or website.
- **Scopes:** `read_status` allows the four tools. `request_changes` is reserved for E4: consent narrows any request
  for it to `read_status`.
- **Limits** (settings under `assistant` in the policy file):

  | Setting | Value |
  |---|---|
  | Calls per minute per token | 30 |
  | `ask_support` calls per hour per token | 20 |
  | `ask_support` requests running at once per token | 1 |
  | `ask_support` timeout | 900 s |
  | Answers kept | 24 h |
  | Token lifetime | 30 days, no refresh tokens |

- **Audit:** every call is recorded with the assistant's client id and the outcome (the `audit` table in
  `runtime/assistant/assistant.db`).
- **Demo map** (`docs/assistant_lane_plan.md`): each `ask_support` turn (running, then done or failed, with the
  agents' steps) and each status read is also appended to `assistant_activity.jsonl` in the action store
  (`assistant_gateway/activity.py`; no token, grant or sly_data). The demo console shows a signed-in customer their
  own assistant's box on the map and its turns in the background lane. `scripts\demo.ps1 -Assistant` starts the
  gateway with the demo's stores.

## Tools

| Tool | What | Speed |
|---|---|---|
| `get_case_status(case_id?)` | The customer's cases: each request with its status, reference and next step, plus any suggested products. Facts recorded by code only (no model wording, no evidence, no internal ids). | instant |
| `get_order_status(order_ref?)` | Orders with items, shipment tracking, returns and refunds. `order_ref` is an order number or words like "blue jacket". | instant |
| `ask_support(message, case_id?)` | One support turn through the agents, in the background. Returns a `request_id` at once. | minutes |
| `get_answer(request_id)` | `running` / `done` / `failed`. When done: the reply (after `finish_turn`'s checks) and the structured case record. | instant |

Pass the `case_id` from an answer to continue the case. The conversation so far is kept for that case and grant.

## Run it

```powershell
.\scripts\demo.ps1                       # agent server :8080 (+ demo console); or: ns run
.\scripts\assistant_gateway.ps1          # gateway: http://localhost:8096/mcp
```

Customer data comes from `CUSTOMER_DATA_BACKEND` in `.env`. While Databricks is unavailable, set
`$env:CUSTOMER_DATA_BACKEND = 'mock'` in the shell before starting both.

### Connect Claude Code (simplest local client)

```powershell
claude mcp add --transport http everly http://localhost:8096/mcp
```

Then run `/mcp` in Claude Code and choose to authenticate:
1. The browser opens the **consent page**. It sends you to **sign in** first.
2. Sign in with the customer's email address: a 6-digit code goes to that address (in the demo: the outbox, shown
   in the demo console's Inbox tab). Started with `scripts\demo.ps1`, the page also offers "choose a test customer".
3. The consent page shows the assistant's name (marked "not verified by ACME Retail" unless its client id is in
   `assistant.verified_clients` in the policy) and where you go back to. Choose **Allow**.

### Sign-in and consent (review findings S2, S3)

- The customer comes only from a **signed session cookie** (HttpOnly, SameSite=Lax, Secure on https, 30 minutes),
  never from a form or the URL. `/consent` and `/grants` need it; every form that changes something carries a CSRF
  token bound to the session. Code: `assistant_gateway/signin.py`.
- **Sign-in codes:** 6 digits, 10 minutes, 5 tries, single use. The page reads the same whether or not the address has
  an account. The code is a *private* outbox message: it never reaches the companion's thread or notes, or the review
  console.
- **Demo sign-in** (pick a test customer): only with `ASSISTANT_DEMO_SIGNIN=1` (set by `scripts\demo.ps1`) and only on
  a loopback address. `build_app` refuses to start with it on any other host or public URL.
- **Registration:** redirect URIs must be https, or http on a loopback address; the client's name is cut to plain
  words (40 characters) before it is ever shown.

Then ask, for example, "Where is my refund for the blue jacket?".

The OAuth flow (registration, PKCE, consent, token, revocation) is covered by `tests/test_assistant_mcp.py` with an
HTTP client that follows the MCP authorization spec. Connecting Claude Code itself has not been tried yet.

**Without the OAuth flow:** issue a test token and pass it as a header.

```powershell
.\scripts\assistant_gateway.ps1 token -Customer CUST-S01
claude mcp add --transport http everly http://localhost:8096/mcp --header "Authorization: Bearer <token>"
```

**Claude Desktop and other hosted assistants** connect to remote MCP servers from their own infrastructure. They
need a public HTTPS URL, for example a tunnel, started with `--public-url https://…` so the OAuth metadata
advertises that address. This was not tested here.

### Disconnect

- **The customer:** http://localhost:8096/grants → sign in → **Disconnect**.
- **The assistant:** OAuth revocation at `/revoke`.
- **From the shell:** `python -m assistant_gateway revoke --customer CUST-S01 --grant GRANT-…`.

## Evaluation

```powershell
.\scripts\assistant_gateway.ps1 eval -Label e2-scripted        # scripted model + mock data, ~1 min
.\scripts\assistant_gateway.ps1 eval -Label e2-live -Live      # configured model and backend
```

For each conversation, a scripted MCP client gets a grant, calls `ask_support` and `get_answer` over MCP, and is
scored from the records:
- **answered:** every request finished with a readable reply.
- **no changes:** nothing was executed, queued or sent for approval (`actions.jsonl`).
- **refused:** wherever the website would have acted, the request was refused with `confirm_in_channel`
  (`decisions.jsonl`).
- **case matches:** the structured case equals the case store's record.
- **own data only:** no customer ids appear in the output.

Results go to `runtime/eval/<label>/assistant.md`.

## Demo limitations

- **Sign-in is simulated.** The consent and grants pages use a simulated sign-in (pick a test customer), like the
  demo console. The identity is still resolved in code from the website sign-in. Production uses the real
  ACME Retail sign-in and identity provider.
- **Local HTTP only.** No TLS. A public deployment needs HTTPS and `--public-url`.

## Files

| Path | What |
|---|---|
| `assistant_gateway/grants.py` | Grants (hashed tokens), registered clients, one-time codes, `ask_support` requests, audit (SQLite) |
| `assistant_gateway/views.py` | Case and order status allow-lists |
| `assistant_gateway/support.py` | `ask_support` turn: `sly_data` from the grant, `stream_turn`, `finish_turn` |
| `assistant_gateway/oauth.py` | OAuth provider for the MCP SDK, consent and grants pages |
| `assistant_gateway/server.py` | `Gateway` (rules) and the FastMCP app |
| `evaluation/assistant_eval.py` | Assistant-channel evaluation |
| `tests/test_assistant_gateway.py`, `tests/test_assistant_mcp.py` | Unit tests and end-to-end tests (OAuth + MCP over HTTP) |
