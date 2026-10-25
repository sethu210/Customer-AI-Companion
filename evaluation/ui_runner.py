r"""
Runs the evaluation conversations through the nsflow front end (the chat UI at http://localhost:4173) the way a
person using the UI does, and records them in the same format as evaluation.runner, so evaluation.score and
evaluation.review_doc work on the result.

Per conversation it does what the browser does after a page reload: a new session id, the chat, internal-chat
(agent messages and tool calls), logs (token accounting) and sly-data WebSockets of
/api/v1/ws/<channel>/<network>/<session>, then each customer message as {"message": ..., "sly_data": ...} with
"Use Edited Sly Data" semantics: the channel identity on every turn, plus the case_id returned by the previous turn.

Needs "ns run" (server AND UI) started with clean stores for the label, as for evaluation.runner:

    $env:ACTION_MODE='live'
    $env:ACTION_STORE_DIR='runtime/eval/<label>/multi/actions'
    $env:CASE_STORE_PATH='runtime/eval/<label>/multi/cases.db'
    .\.venv\Scripts\ns.exe run

    .\.venv\Scripts\python.exe -m evaluation.ui_runner --network multi --label <label>   (same three variables)
"""

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import websockets

from coded_tools.customer_service import gateway_auth
from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.case_store import turn_start
from coded_tools.customer_service.mock_adapter import MockAdapter
from evaluation.expectations import ROOT
from evaluation.expectations import Conversation
from evaluation.expectations import conversations
from evaluation.runner import ERROR_MARKERS
from evaluation.runner import NETWORKS
from evaluation.runner import _finish_turn
from evaluation.runner import _line_counts
from evaluation.runner import _new_lines
from evaluation.runner import gateway_sly_data
from evaluation.runner import preflight

CHANNELS = ("chat", "internalchat", "logs", "slydata")


def new_session_id() -> str:
    """The same shape the UI uses: session_<epoch ms>_<random hex>, 36 characters."""
    stamp = str(int(time.time() * 1000))
    return f"session_{stamp}_{secrets.token_hex(16)[:36 - len('session_') - 1 - len(stamp)]}"


class UiSession:
    """One browser tab on one network: the four WebSockets the chat page opens, read in the background."""

    def __init__(self, ui_url: str, network_path: str):
        self.base = ui_url.replace("http", "ws", 1) + "/api/v1/ws"
        self.network_path = network_path
        self.session_id = new_session_id()
        self.sockets: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []  # everything received on internalchat/logs/slydata, in order
        self.answers: asyncio.Queue = asyncio.Queue()
        self._readers: list[asyncio.Task] = []

    async def __aenter__(self) -> "UiSession":
        for channel in CHANNELS:
            url = f"{self.base}/{channel}/{self.network_path}/{self.session_id}"
            self.sockets[channel] = await websockets.connect(url, max_size=None, ping_interval=None)
            self._readers.append(asyncio.create_task(self._read(channel)))
        await asyncio.sleep(1.0)  # let the backend register the connections, as the page does on load
        return self

    async def __aexit__(self, *exc) -> None:
        for task in self._readers:
            task.cancel()
        for ws in self.sockets.values():
            await ws.close()

    async def _read(self, channel: str) -> None:
        try:
            async for raw in self.sockets[channel]:
                data = json.loads(raw)
                if channel == "chat":
                    await self.answers.put(data)
                else:
                    self.events.append({"channel": channel, "at": time.time(), "data": data})
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def send(self, text: str, sly_data: dict[str, Any], timeout_s: float) -> str:
        """Types the message and presses Send; returns the reply shown in the chat panel."""
        signed = gateway_auth.sign(sly_data, customer_message=text)  # as a channel gateway would (gateway_auth.py)
        await self.sockets["chat"].send(json.dumps({"message": text, "sly_data": signed}))
        reply = await asyncio.wait_for(self.answers.get(), timeout=timeout_s)
        await asyncio.sleep(1.5)  # the sly_data and last log events follow the reply
        return (reply.get("message") or {}).get("text") or ""


def summarise_turn(events: list[dict[str, Any]], front: str) -> dict[str, Any]:
    """Agents/tools used, tool errors, token accounting and returned sly_data from one turn's events."""
    turn = {"agents_and_tools": [], "tool_errors": [], "accounting": {}, "sly_data": {}}
    for event in events:
        message = event["data"].get("message")
        if event["channel"] == "internalchat" and isinstance(message, dict):
            for name in message.get("otrace") or []:
                if name and name != front and name not in turn["agents_and_tools"]:
                    turn["agents_and_tools"].append(name)
            text = message.get("text") or ""
            turn["tool_errors"] += [m for m in ERROR_MARKERS if f"Error: {m}" in text and m not in turn["tool_errors"]]
        elif event["channel"] == "logs" and "token_accounting" in str(event["data"].get("message", "")):
            try:
                accounting = json.loads(event["data"]["message"])["token_accounting"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            # Several accounting messages arrive (sub-networks, then the request total); keep the largest.
            if accounting.get("total_tokens", 0) >= turn["accounting"].get("total_tokens", -1):
                turn["accounting"] = {k: accounting.get(k) for k in ("total_tokens", "prompt_tokens",
                                                                      "completion_tokens", "successful_requests",
                                                                      "total_cost", "time_taken_in_seconds")}
        elif event["channel"] == "slydata" and isinstance(message, dict) and isinstance(message.get("text"), dict):
            turn["sly_data"] = message["text"]
    return turn


def latest_case_for(customer_id: str, since_iso: str) -> str | None:
    """Fallback when the sly_data panel does not show a case_id: the case opened for this customer in this run."""
    path = os.environ.get("CASE_STORE_PATH")
    if not path or not Path(path).exists():
        return None
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT case_id FROM cases WHERE customer_id = ? AND created_at >= ? "
                         "ORDER BY created_at DESC LIMIT 1", (customer_id, since_iso)).fetchone()
    return row[0] if row else None


async def run_conversation(network: str, conv: Conversation, ui_url: str, identities: list[dict],
                           store: ActionStore, timeout_s: float, streams_dir: Path) -> dict[str, Any]:
    path, front = NETWORKS[network]
    identity = gateway_sly_data(network, conv, identities)
    sly = dict(identity)
    before = _line_counts(store)
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record: dict[str, Any] = {"id": conv.id, "network": network, "via": "nsflow-ui", "sly_data_sent": identity,
                              "turns": [], "error": None}
    streams_dir.mkdir(parents=True, exist_ok=True)
    stream_path = streams_dir / f"{conv.id}.jsonl"
    record["stream_file"] = str(stream_path)
    try:
        async with UiSession(ui_url, path) as ui:
            record["ui_session"] = ui.session_id
            with stream_path.open("w", encoding="utf-8") as stream:
                for n, text in enumerate(conv.messages, start=1):
                    first_event = len(ui.events)
                    t0 = time.monotonic()
                    since = turn_start()
                    try:
                        answer = await ui.send(text, dict(sly), timeout_s)
                    except Exception as e:  # noqa: BLE001 - recorded, scored as a failed conversation
                        record["error"] = f"{type(e).__name__}: {e}"[:300]
                        break
                    events = ui.events[first_event:]
                    for event in events:
                        stream.write(json.dumps({"turn": n, "question": text, **event}, default=str) + "\n")
                    turn = summarise_turn(events, front)
                    returned = turn.pop("sly_data")
                    case_id = returned.get("case_id") or latest_case_for(conv.customer, started_iso)
                    if case_id:
                        sly["case_id"] = case_id  # what the UI user sends back on the next turn
                    record["turns"].append({"q": text, "a": answer, **turn,
                                            "seconds": round(time.monotonic() - t0, 1)})
                    if network in ("multi", "lean"):  # what a gateway would do; nsflow itself still shows the raw reply
                        _finish_turn(record["turns"][-1], case_id, since, text)
    except Exception as e:  # noqa: BLE001 - e.g. the UI is not running
        record["error"] = record["error"] or f"{type(e).__name__}: {e}"[:300]
    record["store"] = _new_lines(store, before)
    record["case_id"] = sly.get("case_id")
    if network in ("multi", "lean") and sly.get("case_id"):
        record["case"] = CaseStore().case_view(sly["case_id"])
    return record


async def main_async(args: argparse.Namespace) -> None:
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
        f.write(json.dumps({"run": {"label": args.label, "network": args.network, "via": "nsflow-ui",
                                    "ui_url": args.ui, "env_of_runner": env,
                                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}}) + "\n")
        for conv in convs:
            record = await run_conversation(args.network, conv, args.ui, identities, store, args.timeout,
                                            out_dir / f"{args.network}_streams")
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            calls = sum((t["accounting"].get("successful_requests") or 0) for t in record["turns"])
            secs = sum(t["seconds"] for t in record["turns"])
            print(f"{conv.id:4} {secs:6.0f}s {calls:3} LLM calls  {record['error'] or ''}", flush=True)
    print(f"results: {out_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--label", required=True, help="run name; results go to runtime/eval/<label>/")
    parser.add_argument("--ids", nargs="*", help="only these conversation ids, e.g. S01 S09 M1")
    parser.add_argument("--ui", default="http://localhost:4173", help="the nsflow UI address")
    parser.add_argument("--timeout", type=float, default=960, help="seconds per turn")
    parser.add_argument("--allow-dirty", action="store_true")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
