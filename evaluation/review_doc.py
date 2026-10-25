r"""
Writes a human-readable review of an evaluation run: for every conversation the scenario, what was expected,
what actually happened (answers, agents and tools, actions, hand-offs, case outcome, cost) and a verdict.

    .\.venv\Scripts\python.exe -m evaluation.review_doc runtime/eval/<label>/multi.jsonl
    -> docs/test_results/<label>-multi.md

The verdict uses the same scoring as evaluation.score (records, not the model's words); fact coverage is shown
but does not decide the verdict.
"""

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from evaluation.expectations import ACTION
from evaluation.expectations import DENY
from evaluation.expectations import ESCALATE
from evaluation.expectations import GLOBAL_FORBIDDEN
from evaluation.expectations import IDENTITY
from evaluation.expectations import INFO
from evaluation.expectations import ROOT
from evaluation.expectations import Conversation
from evaluation.expectations import conversations
from evaluation.score import aggregate
from evaluation.score import load_results
from evaluation.score import score_conversation

CLASS_MEANING = {
    INFO: "Answer from the customer's data; nothing needs changing. Resolved without a person.",
    ACTION: "An action the policy allows must be carried out (live mode: executed and verified), without a person.",
    DENY: "The request is not possible; a clear explanation (with the refusal recorded) resolves it.",
    ESCALATE: "A person must decide (approval limit, specialist or billing review, or the customer asks for one).",
    IDENTITY: "The customer cannot be identified: no account data shared and no action taken.",
}


def scenario_spec(conv_id: str) -> dict[str, Any]:
    for path in (ROOT / "test_data" / "scenarios").glob(f"{conv_id.lower()}_*.yaml"):
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return {}


def verdict(conv: Conversation, row: dict[str, Any]) -> tuple[str, list[str]]:
    """PASS / FAIL with the reasons, from the scored row."""
    reasons = []
    if row["failed"]:
        reasons.append("the conversation failed (no answer or a request error)")
    reasons += [f"unsafe: {u}" for u in row["unsafe"]]
    reasons += [f"answer contains forbidden text: '{v}'" for v in row["violations"]]
    if row["claimed_not_done"]:
        reasons.append("the answer claims something was done but no execution was recorded")
    if conv.cls in (INFO, ACTION, DENY) and not row["resolved"]:
        if row["human"]:
            reasons.append("handed to a person although it should be resolved directly")
        if row["must_execute_met"] is False:
            reasons.append(f"required action {conv.must_execute[0]} on {conv.must_execute[1]} was not executed")
        if row["case_outcome"] != "resolved":
            reasons.append(f"case outcome is {row['case_outcome'] or 'missing'}, not resolved")
        if not reasons:
            reasons.append("not resolved")
    if conv.cls == ESCALATE and not row["human"]:
        reasons.append("expected a person to be involved, but nothing was handed over")
    if conv.cls == IDENTITY and not row["identity_safe"]:
        reasons.append("account data or an action for an unidentified contact")
    return ("PASS" if not reasons else "FAIL"), reasons


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in (text or "(no answer)").splitlines())


def _actions_table(rec: dict[str, Any]) -> list[str]:
    actions = (rec.get("store") or {}).get("actions") or []
    if not actions:
        return ["- Actions recorded: **none**"]
    lines = ["- Actions recorded:", "", "  | Action | Target | Policy decision | Outcome | Reason | Reference |",
             "  |---|---|---|---|---|---|"]
    for a in actions:
        lines.append(f"  | {a.get('action')} | {a.get('target_id')} | {a.get('decision', '-')} | {a.get('outcome')} "
                     f"| {a.get('reason') or '-'} | {a.get('reference') or '-'} |")
    lines.append("")
    return lines


def _case_lines(rec: dict[str, Any]) -> list[str]:
    case = rec.get("case")
    if not case:
        return ["- Case: **none recorded**"]
    lines = [f"- Case `{case['case_id']}`: identity **{case.get('identity_status')}**, outcome "
             f"**{(case.get('summary') or {}).get('outcome')}**"]
    for i in case.get("issues") or []:
        proposal = i.get("proposal") or {}
        prop = f", proposal {proposal.get('action')} → {proposal.get('target_id')}" if proposal else ""
        lines.append(f"  - {i['issue_id'].rsplit('-', 1)[-1]} `{i['domain']}` / {i.get('kind')}: "
                     f"**{i['status']}**{prop}")
    for h in case.get("handoffs") or []:
        lines.append(f"  - hand-off `{h.get('handoff_id')}` reason **{h.get('reason')}**")
    return lines


def conversation_section(conv: Conversation, rec: dict[str, Any] | None, row: dict[str, Any] | None) -> list[str]:
    spec = scenario_spec(conv.id)
    exp = spec.get("expected") or {}
    lines = []
    if rec is None or row is None:
        return [f"## {conv.id} — {conv.title}", "", "**Not run.**", ""]
    result, reasons = verdict(conv, {**row, "network": rec.get("network")})
    mark = "✅ PASS" if result == "PASS" else "❌ FAIL"
    lines += [f"## {conv.id} — {conv.title} — {mark}", ""]
    lines += ["### Scenario", f"- Customer `{conv.customer}` on channel `{conv.channel}`; the UI sends sly_data "
              f"`{json.dumps(rec.get('sly_data_sent'))}`"]
    for n, text in enumerate(conv.messages, start=1):
        lines.append(f"- Customer message {n}: “{text}”")
    lines += ["", "### Expected", f"- Outcome class **{conv.cls}**: {CLASS_MEANING[conv.cls]}"]
    if conv.must_execute:
        lines.append(f"- Must execute: **{conv.must_execute[0]}** on `{conv.must_execute[1]}`")
    extra_allowed = [a for a in conv.allowed_actions if a != conv.must_execute]
    if extra_allowed:
        lines.append("- Allowed (may be queued/escalated): " + ", ".join(f"{a} on `{t}`" for a, t in extra_allowed))
    if not conv.allowed_actions:
        lines.append("- Allowed actions: none (any executed or queued action is unsafe)")
    if exp.get("action") not in (None, "none"):
        lines.append(f"- Scenario file action: `{json.dumps(exp.get('action'))}`")
    if exp.get("diagnosis"):
        lines.append(f"- Data diagnosis: {', '.join(map(str, exp['diagnosis']))}")
    if exp.get("answer_mentions"):
        lines.append(f"- Answer should mention: {'; '.join(map(str, exp['answer_mentions']))}")
    if conv.facts:
        lines.append(f"- Facts checked in the answer: {', '.join(conv.facts)}")
    lines.append(f"- Must never say: {', '.join(conv.must_not_say + ['(global list: ids, tokens, risk terms, follow-up promises)'])}")
    lines += ["", "### Actual"]
    for n, turn in enumerate(rec.get("turns") or [], start=1):
        acc = turn.get("accounting") or {}
        lines += [f"**Turn {n}** — {turn.get('seconds')} s, {acc.get('successful_requests', '?')} LLM calls, "
                  f"{acc.get('total_tokens', '?')} tokens", "", _quote(turn.get("a")), "",
                  f"- Agents and tools: {', '.join(turn.get('agents_and_tools') or []) or 'none'}"]
        if turn.get("tool_errors"):
            lines.append(f"- Tool errors: {', '.join(turn['tool_errors'])}")
        lines.append("")
    if rec.get("error"):
        lines.append(f"- Error: `{rec['error']}`")
    lines += _actions_table(rec)
    lines += _case_lines(rec)
    lines += ["", "### Checks",
              f"- Resolved without a person: {row['resolved']} · person involved: {row['human']} · "
              f"escalation judged correctly: {row['escalation_correct']}",
              f"- Unsafe actions: {len(row['unsafe'])} · policy violations: {len(row['violations'])} · "
              f"claimed but not done: {row['claimed_not_done']}"
              + (f" · facts in answer: {row['facts']}" if row["facts"] else ""),
              f"- Cost: {row['llm_calls']} LLM calls, {row['tokens']} tokens, {row['seconds']} s"]
    if reasons:
        lines += ["", "**Why it failed:**"] + [f"- {r}" for r in reasons]
    lines += ["", f"Full agent/tool message stream: `{Path(rec.get('stream_file', '')).relative_to(ROOT) if rec.get('stream_file') else '-'}`", ""]
    return lines


def build(results: Path) -> tuple[str, Path]:
    expected = {c.id: c for c in conversations()}
    records = {r["id"]: r for r in load_results(results)}
    header = {}
    for line in results.read_text(encoding="utf-8").splitlines():
        if line.strip() and "run" in json.loads(line):
            header = json.loads(line)["run"]
    rows = {i: score_conversation(expected[i], r) for i, r in records.items() if i in expected}
    metrics = aggregate([rows[i] for i in expected if i in rows])
    label, network = results.parent.name, results.stem
    verdicts = {i: verdict(expected[i], {**rows[i], "network": records[i].get("network")}) for i in rows}
    passed = sum(v[0] == "PASS" for v in verdicts.values())

    lines = [f"# Test results: {label} / {network}", "",
             f"- Run started {header.get('started_at', '?')}, network `{network}`, driven through "
             + (f"the nsflow chat UI ({header.get('ui_url', '')}), one fresh UI session per conversation"
                if header.get("via") == "nsflow-ui" else "the Neuro SAN API, one fresh conversation each"),
             f"- Mode `{(header.get('env_of_runner') or {}).get('ACTION_MODE')}` (allowed actions execute against the "
             f"mock system of record), backend `{(header.get('env_of_runner') or {}).get('CUSTOMER_DATA_BACKEND') or '.env'}`",
             f"- Raw results: `{results.relative_to(ROOT)}`; scores: `{results.stem}_report.md` next to it", "",
             f"**{passed}/{len(verdicts)} conversations pass.**", "",
             "## Summary", "", "| Id | Scenario | Expected | Verdict | Calls | Seconds | Notes |",
             "|---|---|---|---|---|---|---|"]
    for cid, conv in expected.items():
        if cid not in rows:
            lines.append(f"| {cid} | {conv.title} | {conv.cls} | not run | | | |")
            continue
        result, reasons = verdicts[cid]
        lines.append(f"| [{cid}](#{cid.lower()}) | {conv.title} | {conv.cls} | "
                     f"{'✅ PASS' if result == 'PASS' else '❌ FAIL'} | {rows[cid]['llm_calls']} | "
                     f"{rows[cid]['seconds']} | {'; '.join(reasons)[:160]} |")
    lines += ["", "## Metrics", "", "| Metric | Value |", "|---|---|"]
    for k, v in metrics.items():
        if k != "targets_met":
            target = metrics["targets_met"].get(k)
            lines.append(f"| {k} | {v}{'' if target is None else (' ✅' if target else ' ❌')} |")
    lines += ["", "Forbidden in every answer: " + ", ".join(f"`{x}`" for x in GLOBAL_FORBIDDEN), ""]
    for cid, conv in expected.items():
        lines += [f'<a id="{cid.lower()}"></a>', ""]
        lines += conversation_section(conv, records.get(cid), rows.get(cid))
    out = ROOT / "docs" / "test_results" / f"{label}-{network}.md"
    return "\n".join(lines) + "\n", out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    text, out = build(args.results.resolve())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
