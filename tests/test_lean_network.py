"""The lean network is generated from the main one and must never drift from it."""

import importlib.util
from pathlib import Path

from pyhocon import ConfigFactory

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("build_lean_network", ROOT / "scripts" / "build_lean_network.py")
build_lean_network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_lean_network)


def test_lean_network_is_up_to_date():
    expected = build_lean_network.build(build_lean_network.source_text())
    assert build_lean_network.TARGET.read_bytes().decode("utf-8") == expected, \
        "run: .venv\\Scripts\\python.exe scripts\\build_lean_network.py"


def test_lean_network_differs_only_by_the_policy_agent():
    main = {t["name"]: t for t in ConfigFactory.parse_file(str(build_lean_network.SOURCE), resolve=False)["tools"]}
    lean = {t["name"]: t for t in ConfigFactory.parse_file(str(build_lean_network.TARGET), resolve=False)["tools"]}
    assert set(main) - set(lean) == {"policy_risk_escalation", "evaluate_policy", "get_policy_config"}
    assert set(lean) <= set(main)
    for name in lean:
        if name != "support_orchestrator":  # every other agent and tool is identical
            assert lean[name].as_plain_ordered_dict() == main[name].as_plain_ordered_dict(), name
    instructions = lean["support_orchestrator"]["instructions"]
    assert "policy_risk_escalation" not in instructions and "awaiting_confirmation" in instructions
    assert list(lean["support_orchestrator"]["tools"]) == [t for t in main["support_orchestrator"]["tools"]
                                                          if t != "policy_risk_escalation"]


def test_lean_network_is_served():
    assert "customer_service/support_multi_agent_lean.hocon" in (ROOT / "registries" / "manifest.hocon").read_text()
