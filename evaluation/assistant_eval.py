r"""
E2 evaluation: conversations on the assistant channel, driven by a scripted MCP client.

For each conversation: a test grant for its customer (resolved from the website sign-in, as consent does), then
ask_support for each message (continuing the case) and get_answer until done, all over MCP with the bearer token.
Scored from the records, not the reply text:
    answered      every request finished with a readable reply
    no_changes    nothing was executed, queued or sent for approval on the assistant channel (actions.jsonl)
    refused_ok    a conversation where the website would act has the request refused with confirm_in_channel
    case_matches  the structured case in get_answer equals the case store's record (statuses, references)
    own_data      the reply and structured output carry no other customer's ids and no customer ids at all

    python -m evaluation.assistant_eval --label e2-scripted [--ids S01,S08,R1] [--live]
Scripted model and mock data by default (no quota); --live uses the configured model and backend.
Output: runtime/eval/<label>/assistant.json and assistant.md.
"""

import argparse
import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from evaluation.expectations import ROOT
from evaluation.expectations import conversations

DEFAULT_IDS = ["S01", "S02", "S08", "S13", "S14", "M2", "R1", "D1"]
CHANGE_OUTCOMES = {"executed", "queued_for_review", "queued_for_approval", "escalated"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_gateway(port: int) -> Any:
    import uvicorn

    from assistant_gateway.server import build_app

    server = uvicorn.Server(uvicorn.Config(build_app(port=port).streamable_http_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    return server


async def _converse(base: str, token: str, messages: list[str], timeout_s: float) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    answers: list[dict[str, Any]] = []
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            async def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
                result = await session.call_tool(name, args)
                text = result.content[0].text
                return {"error": text} if result.isError else json.loads(text)

            case_id = None
            for message in messages:
                started = await tool("ask_support", {"message": message, **({"case_id": case_id} if case_id else {})})
                if "error" in started:
                    answers.append({"status": "refused", "reply": started["error"]})
                    break
                deadline = time.monotonic() + timeout_s
                answer = {"status": "running"}
                while answer.get("status") == "running" and time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    answer = await tool("get_answer", {"request_id": started["request_id"]})
                answers.append(answer)
                case_id = (answer.get("case") or {}).get("case_id") or case_id
    return answers


def _score(conv: Any, answers: list[dict[str, Any]], actions: list[dict[str, Any]], decisions: list[dict[str, Any]],
           customer_ids: set[str]) -> dict[str, Any]:
    from assistant_gateway.views import case_summary
    from coded_tools.customer_service.case_store import CaseStore

    changed = [a for a in actions if a.get("customer_id") == conv.customer and a.get("outcome") in CHANGE_OUTCOMES]
    refusals = [d for d in decisions if d.get("customer_id") == conv.customer and d.get("channel") == "assistant"
                and d.get("reason_code") == "confirm_in_channel"]
    case = next((a["case"] for a in reversed(answers) if a.get("case")), None)
    matches = False
    if case:
        store = CaseStore()
        matches = case == case_summary(store.case_view(case["case_id"]), store.get_case(case["case_id"]))
    text = json.dumps(answers)
    row = {
        "id": conv.id, "cls": conv.cls,
        "answered": bool(answers) and all(a.get("status") == "done" and a.get("reply") for a in answers),
        "no_changes": not changed,
        # Every conversation where the website would act (execute, queue, send for approval) must be refused here.
        "refused_ok": bool(refusals) if conv.allowed_actions else None,
        "case_matches": matches,
        "own_data": not any(c in text for c in customer_ids) and "CUST-" not in text,
        "turns": len(answers),
        "reply": (answers[-1].get("reply") or "")[:300] if answers else "",
        "case_outcome": (case or {}).get("case_status"),
    }
    row["ok"] = all(v for k, v in row.items() if k in ("answered", "no_changes", "case_matches", "own_data")) and \
        row["refused_ok"] is not False
    return row


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(prog="assistant_eval")
    parser.add_argument("--label", required=True)
    parser.add_argument("--ids", default=",".join(DEFAULT_IDS))
    parser.add_argument("--live", action="store_true", help="configured model and backend instead of scripted+mock")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    out_dir = ROOT / "runtime" / "eval" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ASSISTANT_STORE_PATH"] = str(out_dir / "assistant.db")
    if not args.live:
        os.environ["CUSTOMER_DATA_BACKEND"] = "mock"

    from assistant_gateway.grants import GrantStore
    from assistant_gateway.oauth import GRANTABLE_SCOPES
    from assistant_gateway.oauth import resolve_customer
    from coded_tools.customer_service.action_store import ActionStore
    from coded_tools.customer_service.policy import load_policy
    from evaluation.agent_server import AgentServer

    wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
    convs = [c for c in conversations("multi") if c.id in wanted and c.channel == "web_session"]
    customer_ids = {c.customer for c in conversations()}
    policy = load_policy()
    rows = []
    for conv in convs:
        store_dir = out_dir / "stores" / conv.id
        gateway_port = _free_port()
        with AgentServer(store_dir, scripted=not args.live):
            os.environ["AGENT_SERVER_URL"] = "http://localhost:8080"
            server = _start_gateway(gateway_port)
            try:
                customer_id, auth_level = asyncio.run(resolve_customer(conv.customer, policy))
                token, _ = GrantStore().issue(customer_id=customer_id, client_id="assistant-eval",
                                              client_name="assistant eval", auth_level=auth_level,
                                              scopes=GRANTABLE_SCOPES, ttl_s=3600)
                t0 = time.monotonic()
                answers = asyncio.run(_converse(f"http://localhost:{gateway_port}", token, conv.messages,
                                                args.timeout))
                actions = ActionStore()
                row = _score(conv, answers, actions.actions(), actions.read("decisions.jsonl"),
                             customer_ids - {conv.customer})
                row["seconds"] = round(time.monotonic() - t0, 1)
            finally:
                server.should_exit = True
                time.sleep(1)
        rows.append(row)
        print(f"{conv.id}: {'PASS' if row['ok'] else 'FAIL'} {json.dumps({k: row[k] for k in ('answered', 'no_changes', 'refused_ok', 'case_matches', 'own_data')})}",
              flush=True)
    summary = {"label": args.label, "model": "live" if args.live else "scripted", "conversations": len(rows),
               "passed": sum(r["ok"] for r in rows), "changes_made": sum(not r["no_changes"] for r in rows)}
    (out_dir / "assistant.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                                            encoding="utf-8")
    lines = [f"# Assistant channel ({summary['model']}): {summary['passed']}/{summary['conversations']} pass, "
             f"{summary['changes_made']} conversations changed something", "",
             "| Id | Class | Answered | No changes | Refused (confirm in channel) | Case matches | Own data only | "
             "Case | s |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        refused = "-" if r["refused_ok"] is None else r["refused_ok"]
        lines.append(f"| {r['id']} | {r['cls']} | {r['answered']} | {r['no_changes']} | {refused} | "
                     f"{r['case_matches']} | {r['own_data']} | {r['case_outcome']} | {r['seconds']} |")
    (out_dir / "assistant.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if summary["passed"] == summary["conversations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
