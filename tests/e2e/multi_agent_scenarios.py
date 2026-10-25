r"""
Conversation tests for the support_multi_agent network (needs the server running and an LLM quota).

The gateway is simulated the way the multi-agent design expects: sly_data carries the raw channel identity
(channel + identifier, looked up from the local test data), and the case_id returned in sly_data after each
turn is sent back on the next turn together with chat_context.

    .\.venv\Scripts\python.exe tests\e2e\multi_agent_scenarios.py            # everything
    .\.venv\Scripts\python.exe tests\e2e\multi_agent_scenarios.py S09 M1    # selected ids

Prints each turn's answer, the agents and tools used, and the case outcome from the SQLite case store.
For scored runs use the evaluation harness instead: python -m evaluation.runner (see evaluation/runner.py).
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from coded_tools.customer_service import gateway_auth  # noqa: E402
from coded_tools.customer_service.case_store import CaseStore  # noqa: E402
from coded_tools.customer_service.mock_adapter import MockAdapter  # noqa: E402
from evaluation.expectations import conversations  # noqa: E402

URL = "http://localhost:8080/api/v1/customer_service/support_multi_agent/streaming_chat"
FRONT = "support_orchestrator"
IDENTITIES = MockAdapter.from_env().identities



def identifier(customer_id: str, channel: str) -> str:
    return next(i["identifier"] for i in IDENTITIES if i["customer_id"] == customer_id and i["channel"] == channel)


def turn(sly: dict, text: str, chat_context: dict | None) -> tuple[str, list[str], dict | None, dict, float]:
    body = {"user_message": {"type": "HUMAN", "text": text}, "sly_data": gateway_auth.sign(sly, customer_message=text),
            "chat_filter": {"chat_filter_type": "MAXIMAL"}}
    if chat_context:
        body["chat_context"] = chat_context
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=600) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    answer, used, ctx, returned = "", [], chat_context, {}
    for line in lines:
        if not line.strip():
            continue
        msg = json.loads(line).get("response", {})
        for origin in msg.get("origin") or []:
            name = origin.get("tool")
            if name and name != FRONT and name not in used:
                used.append(name)
        if msg.get("type") == "AGENT_FRAMEWORK":
            answer = msg.get("text") or answer
            ctx = msg.get("chat_context") or ctx
            returned = msg.get("sly_data") or returned
    return answer, used, ctx, returned, time.monotonic() - started


def scenario_cases() -> list[tuple[str, str, str, list[str]]]:
    """The evaluation conversations (evaluation/expectations.py): 20 scenarios + multi-issue scripts."""
    return [(f"{c.id} {c.title[:50]}", c.customer, c.channel, c.messages) for c in conversations()]


def main() -> None:
    only = sys.argv[1:]
    store = CaseStore()
    for name, customer, channel, messages in scenario_cases():
        if only and not any(name.startswith(o) for o in only):
            continue
        sly = {"channel": channel, "identifier": identifier(customer, channel)}
        ctx = None
        print(f"=== {name}")
        for text in messages:
            try:
                answer, used, ctx, returned, secs = turn(dict(sly), text, ctx)
            except Exception as e:  # noqa: BLE001
                print(f"Q: {text}\n<request failed: {e}>\n")
                break
            if returned.get("case_id"):
                sly["case_id"] = returned["case_id"]  # what a real gateway would keep per conversation
            print(f"Q: {text}\n   ({secs:.0f}s; {', '.join(used) or 'no tools'})\nA: {answer.strip()}\n")
        if sly.get("case_id"):
            view = store.case_view(sly["case_id"])
            statuses = ", ".join(f"{i['domain']}:{i['status']}" for i in view["issues"])
            print(f"   case {sly['case_id']} {view['status']} -> {view['summary']['outcome']} [{statuses}]\n")


if __name__ == "__main__":
    main()
