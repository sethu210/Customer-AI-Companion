"""
Assistant gateway (E2): personal AI assistants (Claude, ChatGPT, Copilot, Gemini, ...) as another channel, over MCP.

An assistant never says who the customer is. The customer grants it access with OAuth consent; the token maps to
the customer, a sign-in level and scopes in code (grants.py), and the gateway turns that into sly_data
{customer_id, channel: "assistant", auth_level} exactly as the website gateway does. The model never sees the token.

    grants.py   grants (tokens, stored hashed), consent codes, asynchronous requests, audit log (SQLite)
    views.py    case and order status built from the case store and the data layer: deterministic, no LLM
    support.py  ask_support: one agent turn in the background through the same gateway checks (finish_turn)
    server.py   the MCP server (streamable HTTP) with token checks, scopes and rate limits

Read-only in E2: the policy refuses every change on the assistant channel (read_only_channels, confirm_in_channel).
"""
