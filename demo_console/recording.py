"""
The take library: each recorded conversation is one take under runtime/demo/recordings/<scenario>/<take_id>/.

    recording.json   every event the page received, with real timestamps; answers; case snapshots; cost
    meta.json        date, model, backend, customer, verdict (scored with the evaluation code when the take is a
                     scenario), real seconds

picks.json (next to the scenario folders) records which take each scenario uses in videos. A take whose verdict is
FAIL is never picked automatically.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from coded_tools.customer_service.action_store import ActionStore
from evaluation.expectations import ROOT
from evaluation.expectations import conversations
from evaluation.review_doc import verdict as review_verdict
from evaluation.runner import _line_counts
from evaluation.runner import _new_lines
from evaluation.score import score_conversation

LIBRARY = Path(os.environ.get("DEMO_RECORDINGS_DIR", ROOT / "runtime" / "demo" / "recordings"))
LLM_CONFIG = ROOT / "config" / "llm_config.hocon"


def model_name() -> str:
    if os.environ.get("NS_LLM_MODEL"):
        return os.environ["NS_LLM_MODEL"]
    match = re.search(r'"model_name":\s*"([^"]+)"', LLM_CONFIG.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def match_scenario(customer_id: str, first_message: str) -> str | None:
    """A conversation is a scenario take when its customer and first message are a scenario's."""
    for conv in conversations():
        if conv.customer == customer_id and conv.messages[0].strip().lower() == first_message.strip().lower():
            return conv.id
    return None


class Take:
    def __init__(self, customer: dict[str, Any], sly_data: dict[str, Any], scenario: str | None = None,
                 store: ActionStore | None = None) -> None:
        self.store = store or ActionStore()
        self.before = _line_counts(self.store)
        self.take_id = time.strftime("%Y%m%d-%H%M%S")
        self.data: dict[str, Any] = {
            "version": 1, "take_id": self.take_id, "scenario": scenario, "customer": customer,
            "sly_data_sent": sly_data, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "backend": os.environ.get("CUSTOMER_DATA_BACKEND", "mock"), "model": model_name(),
            "turns": [], "case": None, "store": {}, "verdict": None, "error": None,
        }
        self.dir: Path | None = None

    def add_turn(self, question: str, events: list[dict[str, Any]], answer: str, seconds: float,
                 cost: dict[str, Any], error: str | None = None) -> None:
        if not self.data["turns"] and self.data["scenario"] is None:
            self.data["scenario"] = match_scenario(self.data["customer"]["customer_id"], question)
        self.data["turns"].append({"q": question, "events": events, "answer": answer, "seconds": round(seconds, 1),
                                   "cost": cost})
        self.data["error"] = self.data["error"] or error
        self.save()

    def finish(self, case_view: dict[str, Any] | None) -> dict[str, Any]:
        self.data["case"] = case_view
        self.data["store"] = _new_lines(self.store, self.before)
        self.data["verdict"] = score_take(self.data)
        self.save()
        return self.meta()

    def folder(self) -> Path:
        if self.dir is None:
            name = self.data["scenario"] or f"manual-{self.data['customer']['customer_id']}"
            self.dir = LIBRARY / name / self.take_id
            self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def meta(self) -> dict[str, Any]:
        d = self.data
        return {"take_id": d["take_id"], "scenario": d["scenario"], "customer_id": d["customer"]["customer_id"],
                "customer_name": d["customer"]["name"], "started_at": d["started_at"], "backend": d["backend"],
                "model": d["model"], "turns": len(d["turns"]), "verdict": d["verdict"], "error": d["error"],
                "seconds": round(sum(t["seconds"] for t in d["turns"]), 1),
                "llm_calls": sum((t["cost"] or {}).get("llm_calls") or 0 for t in d["turns"])}

    def save(self) -> None:
        if not self.data["turns"]:
            return
        folder = self.folder()
        (folder / "recording.json").write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps(self.meta(), indent=1, default=str), encoding="utf-8")


def as_eval_record(data: dict[str, Any]) -> dict[str, Any]:
    """The take in evaluation.runner's record format, so evaluation.score can judge it."""
    turns = []
    for t in data["turns"]:
        names = []
        for e in t["events"]:
            for n in (e.get("from"), e.get("to")):
                if n and n != "support_orchestrator" and n not in names:
                    names.append(n)
        # "Error: customer_data_unavailable (permission_denied). ..." -> customer_data_unavailable
        errors = sorted({(str(e["detail"]["result"]).removeprefix("Error: ").split() or ["error"])[0].rstrip(".:")
                         for e in t["events"] if e["kind"] == "tool_result" and e.get("error")})
        cost = t.get("cost") or {}
        turns.append({"q": t["q"], "a": t["answer"], "agents_and_tools": names, "tool_errors": errors,
                      "seconds": t["seconds"], "accounting": {"successful_requests": cost.get("llm_calls"),
                                                              "total_tokens": cost.get("tokens")}})
    case = data.get("case") or {}
    return {"id": data["scenario"], "network": "multi", "turns": turns, "store": data.get("store") or {},
            "case": case, "case_id": case.get("case_id"), "error": data.get("error")}


def score_take(data: dict[str, Any]) -> dict[str, Any]:
    conv = next((c for c in conversations() if c.id == data["scenario"]), None)
    if conv is None:
        return {"result": "UNSCORED", "reasons": ["not a scenario conversation"]}
    if [t["q"] for t in data["turns"]] != conv.messages[:len(data["turns"])] or len(data["turns"]) < len(conv.messages):
        return {"result": "UNSCORED", "reasons": ["the messages differ from the scenario script"]}
    record = as_eval_record(data)
    row = score_conversation(conv, record)
    result, reasons = review_verdict(conv, {**row, "network": "multi"})
    return {"result": result, "reasons": reasons}


# ---------------------------------------------------------------- library


def list_takes(scenario: str | None = None) -> list[dict[str, Any]]:
    picks = load_picks()
    out = []
    for meta_file in sorted(LIBRARY.glob("*/*/meta.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if scenario and meta.get("scenario") != scenario:
            continue
        meta["folder"] = meta_file.parent.parent.name
        meta["picked"] = picks.get(meta["folder"]) == meta["take_id"]
        out.append(meta)
    return out


def load_take(folder: str, take_id: str) -> dict[str, Any]:
    path = LIBRARY / folder / take_id / "recording.json"
    if not path.exists():
        raise FileNotFoundError(f"no take {folder}/{take_id} in {LIBRARY}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_picks() -> dict[str, str]:
    path = LIBRARY / "picks.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def pick(folder: str, take_id: str) -> None:
    load_take(folder, take_id)  # must exist
    picks = load_picks()
    picks[folder] = take_id
    LIBRARY.mkdir(parents=True, exist_ok=True)
    (LIBRARY / "picks.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")


def resolve_take(folder: str, take: str = "picked") -> str:
    """'picked' -> the picked take, else the newest PASS take of a real model (scripted pipeline-check takes are
    never chosen automatically); an explicit take id is returned as is."""
    if take != "picked":
        return take
    picks = load_picks()
    if folder in picks:
        return picks[folder]
    passing = [m for m in list_takes(folder) if (m.get("verdict") or {}).get("result") == "PASS"
               and m.get("model") != "scripted"]
    if not passing:
        raise LookupError(f"{folder}: no picked take and no passing take; record one or pick a take explicitly")
    return passing[-1]["take_id"]
