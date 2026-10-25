r"""
    python -m assistant_gateway [serve] [--port 8096]               the MCP server: http://localhost:8096/mcp
    python -m assistant_gateway token --customer CUST-S01 [--days 1]  a test token (demo), as consent would issue
    python -m assistant_gateway revoke --customer CUST-S01 --grant GRANT-...

The agent server (port 8080, or AGENT_SERVER_URL) must be running for ask_support. Pages: /consent (OAuth consent)
and /grants (connected assistants, revoke).
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()  # same .env as the agent server: backend choice and Databricks settings
    parser = argparse.ArgumentParser(prog="assistant_gateway")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8096)
    serve.add_argument("--public-url", default=None)
    token = sub.add_parser("token")
    token.add_argument("--customer", required=True)
    token.add_argument("--client", default="test-client")
    token.add_argument("--days", type=float, default=1)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--customer", required=True)
    revoke.add_argument("--grant", required=True)
    args = parser.parse_args()

    from assistant_gateway.grants import GrantStore
    from coded_tools.customer_service.policy import load_policy

    if args.command == "token":
        from assistant_gateway.oauth import GRANTABLE_SCOPES
        from assistant_gateway.oauth import resolve_customer

        customer_id, auth_level = asyncio.run(resolve_customer(args.customer, load_policy()))
        token, grant = GrantStore().issue(customer_id=customer_id, client_id=args.client, client_name=args.client,
                                          auth_level=auth_level, scopes=GRANTABLE_SCOPES, ttl_s=args.days * 86400)
        print(f"{grant['grant_id']} for {customer_id} ({auth_level} sign-in), scopes {grant['scopes']}, "
              f"{args.days:g} day(s)\n{token}")
        return 0
    if args.command == "revoke":
        print("revoked" if GrantStore().revoke(args.grant, args.customer) else "not found or already revoked")
        return 0
    from assistant_gateway.server import build_app

    port = getattr(args, "port", 8096)
    build_app(port=port, public_url=getattr(args, "public_url", None)).run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
