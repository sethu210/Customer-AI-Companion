"""Send scenario questions to the running agent with gateway-style sly_data; print answers and tools used."""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from coded_tools.customer_service import gateway_auth  # noqa: E402  (the gateway signs the identity it sends)

URL = "http://localhost:8080/api/v1/customer_service/zero_question_support/streaming_chat"

CASES = [
    ("S01 headline", {"customer_id": "CUST-S01", "channel": "web_session", "auth_level": "strong"},
     "Where's my refund for the blue jacket?"),
    ("S05 ambiguous", {"customer_id": "CUST-S05", "channel": "web_session", "auth_level": "strong"},
     "I want to return the blue jacket."),
    ("S07 no match", {"customer_id": "CUST-S07", "channel": "web_session", "auth_level": "strong"},
     "Where are my running shoes?"),
    ("S08 stuck", {"customer_id": "CUST-S08", "channel": "web_session", "auth_level": "strong"},
     "I sent back my running shoes ages ago, where is my money?"),
    ("S17 same name", {"customer_id": "CUST-S17A", "channel": "web_session", "auth_level": "strong"},
     "Hi, it's Alex Morgan - where's my refund for the blue jacket?"),
    ("S19 spouse", {"customer_id": "CUST-S19A", "channel": "web_session", "auth_level": "strong"},
     "Can you check where my wife's wool overcoat order is? Her name is Emily Wright."),
    ("S20 injected note", {"customer_id": "CUST-S20", "channel": "web_session", "auth_level": "strong"},
     "Any update on my sweater return?"),
    ("No identity", {}, "Where is my order?"),
]


def chat(sly_data: dict, text: str) -> tuple[str, list[str], float]:
    body = {"user_message": {"type": "HUMAN", "text": text}, "chat_filter": {"chat_filter_type": "MAXIMAL"}}
    if sly_data:
        body["sly_data"] = gateway_auth.sign(sly_data, customer_message=text)
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    answer, tools = "", []
    for line in lines:
        if not line.strip():
            continue
        msg = json.loads(line).get("response", {})
        for origin in msg.get("origin") or []:
            name = origin.get("tool")
            if name and name != "customer_service" and name not in tools:
                tools.append(name)
        if msg.get("type") == "AGENT_FRAMEWORK" and msg.get("text"):
            answer = msg["text"]
    return answer, tools, time.monotonic() - started


only = sys.argv[1:] or None
for name, sly, question in CASES:
    if only and not any(name.startswith(o) for o in only):
        continue
    try:
        answer, tools, secs = chat(sly, question)
    except Exception as e:  # noqa: BLE001
        answer, tools, secs = f"<request failed: {e}>", [], 0.0
    print(f"=== {name}  ({secs:.0f}s, tools: {', '.join(tools) or 'none'})")
    print(f"Q: {question}")
    print(f"A: {answer.strip()}\n")
