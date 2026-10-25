# Everly — Your AI Companion, Always by Your Side

**Everly from ACME Retail.** Everly is the AI companion that ACME Retail, an online clothing store, gives each of its
customers. Each customer has one companion for life. It answers and resolves support requests without asking needless
questions, follows open issues until they are done (Resolution Guardian), remembers what the customer asks it to, and
offers an opt-in **Personal Stylist Mode** that invites them to new products that suit what they already own. The
customer can also connect their own AI assistant (read-only, over MCP). It is built on
[Neuro SAN](https://github.com/cognizant-ai-lab/neuro-san). The data layer is Databricks (read-only customer data, plus
our own catalog schema) or a local mock.

**Code decides, the model talks:** identity, actions, policy checks, follow-up rules, invites and message limits are
deterministic code; the language model writes the conversation.

## Status

Plan and decisions: [`docs/companion_plan.md`](docs/companion_plan.md) (phases P0–P9; P0–P7 and P9 done, P8 next).

## What is in the repo

Folder and module names come from the project's start (`neurosan`, `customer_service`). They are kept so nothing
breaks; the product is Everly.

| Path | What it is |
|---|---|
| `registries/customer_service/` | Agent networks: `support_multi_agent` (front agent + specialists), its lean variant, the original single agent |
| `coded_tools/customer_service/` | The agents' tools: case store, policy engine, actions, recommendations, data adapters |
| `coded_tools/customer_service/companion*.py` | Everly's companion: record, consents, profile from the data, thread, memory (stated and confirmed preferences), the notes for the front agent |
| `coded_tools/customer_service/stylist.py`, `messaging.py`, `stylist/` | Personal Stylist Mode: launch scoring and invites, and the message limits shared with the Guardian; command line `python -m stylist` |
| `guardian/` | Resolution Guardian: follows open issues and sends updates ([`docs/resolution_guardian.md`](docs/resolution_guardian.md)) |
| `assistant_gateway/` | Read-only MCP gateway for the customer's own AI assistant, with OAuth consent ([`docs/assistant_gateway.md`](docs/assistant_gateway.md)) |
| `demo_console/`, `demo/` | Everly demo console: simulated website sign-in, "My Companion", live agent view, launches ([`docs/demo_console.md`](docs/demo_console.md)) |
| `review_console/` | Console for people who pick up handed-off cases |
| `evaluation/`, `tests/` | Scenario, Guardian, assistant and journey evals (scripted and real model) and the unit-test suite |
| `config/` | Service policy and LLM configuration |
| `test_data/` | Generated test data and its schema |

The agents' display names in the demo (Orbit, Anchor, Swift, Echo, Vault, Halo, Pulse, Aura, Sentinel) are
internal. To the customer, the whole team is Everly.

## Running

PowerShell, from the project root (dependencies: `uv sync`). Copy `.env.example` to `.env` and fill it in first.

```powershell
.\scripts\demo.ps1                 # agent server + Everly demo console at http://127.0.0.1:8095
.\scripts\eval.ps1 -Scripted       # scenario evals with the scripted test model (no LLM calls)
.\scripts\assistant_gateway.ps1    # MCP gateway for the customer's own AI assistant (:8096)
.\scripts\guardian.ps1             # Resolution Guardian
.\scripts\stylist.ps1              # Personal Stylist Mode: staged launches, launch, scores, invites
uv run pytest                      # unit tests
```

Data backend: `CUSTOMER_DATA_BACKEND` in `.env` (`databricks` or `mock`).
