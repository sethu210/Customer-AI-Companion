r"""
Runs the evaluation conversations against a running Neuro SAN server and records everything needed to score
them: answers, agents/tools used, time, LLM calls, tokens and cost per turn, and the records each conversation
added to the action store (decisions, actions, review cases, executions) and, for the multi-agent network, the
case store. The full message stream of every conversation (each agent's messages and tool calls) is saved to
runtime/eval/<label>/<network>_streams/<id>.jsonl for debugging.

Start the server for an evaluation run with clean stores and live mode, so allowed actions execute against
the mock system of record (shadow mode would hand every allowed action to a person and hide resolutions):

    $env:ACTION_MODE='live'
    $env:ACTION_STORE_DIR='runtime/eval/<label>/actions'
    $env:CASE_STORE_PATH='runtime/eval/<label>/cases.db'
    # optional failure run: $env:CUSTOMER_DATA_FAULTS='get_stuck_cases:timeout:1'
    .\.venv\Scripts\ns.exe run

Then, with the same three variables set in this shell:

    .\.venv\Scripts\python.exe -m evaluation.runner --network multi --label <label>
    .\.venv\Scripts\python.exe -m evaluation.runner --network single --label <label>   (restart with new dirs first)
    .\.venv\Scripts\python.exe -m evaluation.score runtime/eval/<label>/multi.jsonl --compare runtime/eval/<label>/single.jsonl
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from coded_tools.customer_service import gateway_auth
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.companion import CompanionStore
from coded_tools.customer_service.stylist import StylistStore
from coded_tools.customer_service.gateway import finish_turn
from coded_tools.customer_service.mock_adapter import MockAdapter
from evaluation.expectations import ROOT
from evaluation.expectations import Conversation
from evaluation.expectations import conversations

NETWORKS = {
    "multi": ("customer_service/customer-ai-companion", "support_orchestrator"),
    "lean": ("customer_service/customer-ai-companion-lean", "support_orchestrator"),
    "single": ("customer_service/zero_question_support", "customer_service"),
}
STORE_FILES = ("decisions.jsonl", "actions.jsonl", "cases.jsonl", "executed.jsonl")
ERROR_MARKERS = ("customer_data_unavailable", "action_unavailable", "case_unavailable", "policy_unavailable",
                 "handoff_unavailable")


def gateway_sly_data(network: str, conv: Conversation, identities: list[dict]) -> dict[str, Any]:
    """What each network's gateway sends: raw channel identity (multi) or a resolved customer (single)."""
    rows = [i for i in identities if i["customer_id"] == conv.customer and i["channel"] == conv.channel]
    if network in ("multi", "lean"):
        return {"channel": conv.channel, "identifier": rows[0]["identifier"]}
    matches = {i["customer_id"] for i in identities
               if i["channel"] == conv.channel and i["identifier"] == rows[0]["identifier"]}
    if len(matches) != 1 or not rows[0]["verified"]:
        return {"channel": conv.channel}  # the single-agent gateway contract: no customer_id when unresolved
    return {"customer_id": conv.customer, "channel": conv.channel, "auth_level": rows[0]["auth_level"]}


def post_turn(url: str, front: str, sly: dict, text: str, chat_context: dict | None,
              timeout_s: float) -> tuple[dict[str, Any], list[str]]:
    """One turn: the parsed summary, plus the raw streamed lines (every agent's messages) for debugging."""
    body = {"user_message": {"type": "HUMAN", "text": text}, "sly_data": gateway_auth.sign(sly, customer_message=text),
            "chat_filter": {"chat_filter_type": "MAXIMAL"}}
    if chat_context:
        body["chat_context"] = chat_context
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    turn = parse_stream(lines, front, text, chat_context)
    turn["seconds"] = round(time.monotonic() - started, 1)
    return turn, lines


def parse_stream(lines: list[str], front: str, text: str, chat_context: dict | None) -> dict[str, Any]:
    """Answer, agents/tools used, tool errors, accounting, chat_context and returned sly_data of one turn."""
    turn = {"q": text, "a": "", "agents_and_tools": [], "tool_errors": [], "accounting": {},
            "chat_context": chat_context, "sly_data": {}}
    for line in lines:
        if not line.strip():
            continue
        msg = json.loads(line).get("response", {})
        origin = msg.get("origin") or []
        for o in origin:
            name = o.get("tool")
            if name and name != front and name not in turn["agents_and_tools"]:
                turn["agents_and_tools"].append(name)
        text_out = msg.get("text") or ""
        # Only real tool errors ("Error: <marker>"); the agents' instructions mention the same words.
        turn["tool_errors"] += [m for m in ERROR_MARKERS if f"Error: {m}" in text_out and m not in turn["tool_errors"]]
        structure = msg.get("structure") or {}
        if len(origin) == 1 and isinstance(structure, dict) and structure.get("total_tokens") is not None:
            # Two accounting messages arrive (main network, then request total); keep the larger.
            if structure.get("total_tokens", 0) >= turn["accounting"].get("total_tokens", -1):
                turn["accounting"] = {k: structure.get(k) for k in ("total_tokens", "prompt_tokens",
                                                                     "completion_tokens", "successful_requests",
                                                                     "total_cost", "time_taken_in_seconds")}
        if msg.get("type") == "AGENT_FRAMEWORK":
            turn["a"] = msg.get("text") or turn["a"]
            turn["chat_context"] = msg.get("chat_context") or turn["chat_context"]
            turn["sly_data"] = msg.get("sly_data") or turn["sly_data"]
    return turn


def _line_counts(store: ActionStore) -> dict[str, int]:
    return {f: len((store.dir / f).read_text(encoding="utf-8").splitlines()) if (store.dir / f).exists() else 0
            for f in STORE_FILES}


def _new_lines(store: ActionStore, before: dict[str, int]) -> dict[str, list[dict]]:
    out = {}
    for f in STORE_FILES:
        path = store.dir / f
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out[f.removesuffix(".jsonl")] = [json.loads(x) for x in lines[before[f]:] if x.strip()]
    return out


def run_conversation(network: str, conv: Conversation, base_url: str, identities: list[dict],
                     store: ActionStore, timeout_s: float, streams_dir: Path | None = None) -> dict[str, Any]:
    """
    :param streams_dir: when given, every streamed message of every turn is saved to <streams_dir>/<id>.jsonl
                        (what each agent said and which tools it called), for debugging a conversation.
    """
    path, front = NETWORKS[network]
    url = f"{base_url}/api/v1/{path}/streaming_chat"
    sly = gateway_sly_data(network, conv, identities)
    record: dict[str, Any] = {"id": conv.id, "network": network, "sly_data_sent": sly, "turns": [], "error": None}
    if conv.stylist_launch:  # set-up, before the conversation: Stylist Mode on, the products launched
        record["stylist_setup"] = _launch_for(conv)
    before = _line_counts(store)
    ctx = None
    stream_file = None
    if streams_dir is not None:
        streams_dir.mkdir(parents=True, exist_ok=True)
        stream_file = (streams_dir / f"{conv.id}.jsonl").open("w", encoding="utf-8")
        record["stream_file"] = str(streams_dir / f"{conv.id}.jsonl")
    try:
        for n, text in enumerate(conv.messages, start=1):
            since = turn_start()
            try:
                turn, lines = post_turn(url, front, dict(sly), text, ctx, timeout_s)
            except Exception as e:  # noqa: BLE001 - recorded, scored as a failed conversation
                record["error"] = f"{type(e).__name__}: {e}"[:300]
                break
            if stream_file:
                for line in lines:
                    if line.strip():
                        stream_file.write(json.dumps({"turn": n, "question": text,
                                                      "message": json.loads(line).get("response", {})}) + "\n")
            _keep_turn(record, turn, sly)
            ctx = record.pop("_ctx")
            if network in ("multi", "lean"):  # what the gateway does after each reply (gateway.finish_turn)
                _finish_turn(record["turns"][-1], sly.get("case_id"), since, text)
    finally:
        if stream_file:
            stream_file.close()
    record["store"] = _new_lines(store, before)
    record["case_id"] = sly.get("case_id")
    if network in ("multi", "lean") and sly.get("case_id"):
        record["case"] = CaseStore().case_view(sly["case_id"])
    if network in ("multi", "lean"):  # what the customer's companion remembers afterwards (scored: P conversations)
        record["memory"] = [{"kind": m["kind"], "value": m["value"]}
                            for m in CompanionStore().memories(conv.customer)]
        stylist = StylistStore()
        record["stylist"] = {"invites": [{k: i[k] for k in ("product_id", "status")}
                                         for i in stylist.invites(customer_id=conv.customer)],
                             "feedback": [{k: f[k] for k in ("product_id", "kind")}
                                          for f in stylist.feedback(conv.customer)]}
    return record


def _launch_for(conv: Conversation) -> list[dict[str, Any]]:
    """The customer switches Stylist Mode on and the products are launched (only they are opted in by the runner)."""
    import asyncio

    from coded_tools.customer_service import companion as cp
    from coded_tools.customer_service.stylist import Stylist

    CompanionStore().set_consent(conv.customer, cp.STYLIST_MODE, True, channel="evaluation")
    stylist = Stylist()
    out = []
    for product_id in conv.stylist_launch:
        summary = asyncio.run(stylist.launch(product_id, by="evaluation"))
        out.append({"product_id": product_id, "invited": summary.get("invited", [])})
    return out


def _finish_turn(turn: dict[str, Any], case_id: str | None, since: str, message: str | None = None) -> None:
    """Applies the gateway's checks; an unusable reply is kept as raw_answer and replaced by what was shown."""
    shown, checked = finish_turn(case_id, since, turn.get("a"), customer_message=message)
    if checked["reply_problem"]:
        turn["raw_answer"], turn["a"] = turn.get("a"), shown
    turn["end_of_turn"] = checked


def _keep_turn(record: dict[str, Any], turn: dict[str, Any], sly: dict[str, Any]) -> None:
    record["_ctx"] = turn.pop("chat_context")
    returned = turn.pop("sly_data")
    if returned.get("case_id"):
        sly["case_id"] = returned["case_id"]  # what a real gateway keeps per conversation
    record["turns"].append(turn)


def preflight(store: ActionStore, convs: list[Conversation], allow_dirty: bool) -> None:
    """Earlier actions for the same customers would turn into 'already_handled' and skew the scores."""
    path = store.dir / "actions.jsonl"
    if allow_dirty or not path.exists():
        return
    used = {c.customer for c in convs}
    dirty = sorted({json.loads(x)["customer_id"] for x in path.read_text().splitlines() if x.strip()} & used)
    if dirty:
        sys.exit(f"{path} already holds actions for {', '.join(dirty)}. Start the server with fresh ACTION_STORE_DIR "
                 "and CASE_STORE_PATH for this run (see the module docstring), or pass --allow-dirty.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--label", required=True, help="run name; results go to runtime/eval/<label>/")
    parser.add_argument("--ids", nargs="*", help="only these conversation ids, e.g. S01 S09 M1")
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--timeout", type=float, default=900, help="seconds per turn")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    convs = [c for c in conversations(args.network) if not args.ids or c.id in args.ids]
    store = ActionStore()
    preflight(store, convs, args.allow_dirty)
    out_dir = ROOT / "runtime" / "eval" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.network}.jsonl"
    identities = MockAdapter.from_env().identities
    env = {k: os.environ.get(k) for k in ("ACTION_MODE", "ACTION_STORE_DIR", "CASE_STORE_PATH",
                                          "CUSTOMER_DATA_FAULTS", "CUSTOMER_DATA_BACKEND")}
    with out_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run": {"label": args.label, "network": args.network, "env_of_runner": env,
                                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}}) + "\n")
        for conv in convs:
            record = run_conversation(args.network, conv, args.url, identities, store, args.timeout,
                                      streams_dir=out_dir / f"{args.network}_streams")
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            calls = sum((t["accounting"].get("successful_requests") or 0) for t in record["turns"])
            secs = sum(t["seconds"] for t in record["turns"])
            print(f"{conv.id:4} {secs:6.0f}s {calls:3} LLM calls  {record['error'] or ''}")
    print(f"results: {out_file}")


if __name__ == "__main__":
    main()
