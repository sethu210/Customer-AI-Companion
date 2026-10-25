"""
Companion plan P7 (no LLM): the "My Companion" endpoints of the demo console and the Launch panel. Every tab's data
comes from the stores and the data; the customer's clicks change their own companion only.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import data_adapter
from coded_tools.customer_service import recommendations
from coded_tools.customer_service import stylist as sty
from demo_console import recording

FAN = "CUST-B0196"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CUSTOMER_DATA_BACKEND", "mock")
    monkeypatch.setenv("CASE_STORE_PATH", str(tmp_path / "cases.db"))
    monkeypatch.setenv("ACTION_STORE_DIR", str(tmp_path / "actions"))
    monkeypatch.setenv("SIMULATOR_STATE", str(tmp_path / "simulator.json"))
    monkeypatch.setenv("ASSISTANT_STORE_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.delenv("DEMO_RECORDING", raising=False)
    monkeypatch.setattr(data_adapter, "_ADAPTER", None)
    monkeypatch.setattr(recording, "LIBRARY", tmp_path / "recordings")
    cp._DATA_CACHE.clear()
    cp._STYLE_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()
    from demo_console import app as app_module

    # the demo's Stylist Mode set-up (demo.yaml stylist_mode_on) is tested on its own below
    monkeypatch.setitem(app_module.STATE, "config", {**app_module.STATE["config"], "stylist_mode_on": []})
    from coded_tools.customer_service.system_events import TOKEN_ENV
    from test_orbit_tick import TOKEN
    from test_orbit_tick import orbit_in_process  # launches go through Orbit (test_orbit_tick: the real network)

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with TestClient(app_module.app) as c:
        app_module.STATE["clock"].send = orbit_in_process
        yield c
    data_adapter._ADAPTER = None


def test_the_demo_starts_with_stylist_mode_on_for_its_set_up_customers(client):
    """Each staged launch finds at least one customer at 80 or more among them (the weekly limit aside)."""
    from demo_console import app as app_module

    cohort = app_module.load_config()["stylist_mode_on"]
    assert app_module.seed_stylist_mode(cohort) == len(cohort) == 15
    assert app_module.seed_stylist_mode(cohort) == 0  # a recorded choice is kept, never overwritten
    history = cp.CompanionStore().consent_history(cohort[0])
    assert history[-1]["channel"] == "demo set-up"
    stylist = sty.Stylist()
    data = asyncio.run(stylist._data())
    for product in ("PRD-CSC", "PRD-WFJ", "PRD-SLF", "PRD-LOS", "PRD-MPL", "PRD-TFJ"):
        best = 0
        for cid in cohort:
            facts = asyncio.run(stylist._facts(cid, stylist.clock.now()))
            result = sty.score(facts, product, data, stylist.settings, month=stylist.clock.now().month,
                               skip_category_after_returns=2)
            best = max(best, result["score"] or 0)
        assert best >= 80, product


def sign_in(client, customer=FAN):
    return client.post("/api/session", json={"customer_id": customer}).json()["session_id"]


def test_the_view_has_every_tab(client):
    sid = sign_in(client)
    v = client.get(f"/api/session/{sid}/my-companion").json()
    assert v["companion"]["name"] == "Everly" and v["profile"]["customer"]["first_name"]
    assert v["updates"] == {"followed": [], "messages": []}
    assert v["stylist"] == {"on": False, "picks": []} and v["assistants"] == []
    assert v["limits"] == {"quiet_hours": [21, 8], "per_day": 2, "invites_per_week": 1}


def test_settings_change_the_customers_own_consents_and_name(client):
    sid = sign_in(client)
    v = client.post(f"/api/session/{sid}/my-companion/consent", json={"consent": "stylist_mode", "granted": True}).json()
    assert v["stylist"]["on"] and v["consent_history"][0]["channel"] == "web"
    assert client.post(f"/api/session/{sid}/my-companion/consent",
                       json={"consent": "sell_my_data", "granted": True}).status_code == 400
    v = client.post(f"/api/session/{sid}/my-companion/name", json={"name": "Juniper"}).json()
    assert v["companion"]["name"] == "Juniper"
    assert client.post(f"/api/session/{sid}/my-companion/name", json={"name": "<b>x</b>"}).status_code == 400


def test_preferences_added_and_deleted_by_the_customer(client):
    sid = sign_in(client)
    v = client.post(f"/api/session/{sid}/my-companion/memory", json={"kind": "size", "value": "shoes 10"}).json()
    (pref,) = v["profile"]["preferences"]
    assert (pref["kind"], pref["value"]) == ("size", "footwear 10")
    assert client.post(f"/api/session/{sid}/my-companion/memory",
                       json={"kind": "size", "value": "hats 7"}).status_code == 400
    v = client.delete(f"/api/session/{sid}/my-companion/memory/{pref['memory_id']}").json()
    assert v["profile"]["preferences"] == []
    assert client.delete(f"/api/session/{sid}/my-companion/memory/P99").status_code == 404


def test_export_and_reset(client):
    sid = sign_in(client)
    client.post(f"/api/session/{sid}/my-companion/memory", json={"kind": "likes", "value": "navy"})
    res = client.get(f"/api/session/{sid}/my-companion/export")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    data = res.json()
    assert data["preferences"][0]["value"] == "navy" and "profile" in data and "thread" in data
    v = client.post(f"/api/session/{sid}/my-companion/reset").json()
    assert v["profile"]["preferences"] == [] and v["companion"]["name"] == "Everly"


def test_launch_panel_launches_and_the_pick_reaches_the_customer(client):
    sid = sign_in(client)
    client.post(f"/api/session/{sid}/my-companion/consent", json={"consent": "stylist_mode", "granted": True})
    panel = client.get("/api/launches").json()
    assert panel["opted_in"] == 1 and panel["launches"][0]["product_id"] == "PRD-CSC"
    assert panel["launches"][0]["summary"] is None
    after = client.post("/api/launches/PRD-CSC").json()
    summary = after["launches"][0]["summary"]
    assert summary["candidates"] == 1 and summary["invited"][0]["customer_id"] == FAN
    scores = client.get("/api/launches/PRD-CSC/scores").json()
    assert scores["scores"][0]["customer_id"] == FAN and scores["scores"][0]["invite"]["status"] == "sent"
    assert client.post("/api/launches/PRD-WO").status_code == 400  # not staged

    v = client.get(f"/api/session/{sid}/my-companion").json()
    (pick,) = v["stylist"]["picks"]
    assert pick["title"] == "Cashmere Scarf" and pick["picked_because"]
    assert v["updates"]["messages"][0]["source"] == "stylist"  # the invite is in the customer's messages
    v = client.post(f"/api/session/{sid}/my-companion/pick-feedback",
                    json={"product_id": "PRD-CSC", "kind": "not_for_me"}).json()
    assert v["stylist"]["picks"][0]["your_feedback"] == "not_for_me"
    assert client.post(f"/api/session/{sid}/my-companion/pick-feedback",
                       json={"product_id": "PRD-WFJ", "kind": "not_for_me"}).status_code == 400


def test_followed_requests_and_assistants_are_the_customers_own(client):
    from assistant_gateway.grants import GrantStore

    sid = sign_in(client, "CUST-S01")
    token, grant = GrantStore().issue(customer_id="CUST-S01", client_id="c1", client_name="Claude",
                                      auth_level="strong", scopes=["read_status"], ttl_s=3600)
    GrantStore().issue(customer_id="CUST-S02", client_id="c1", client_name="Other", auth_level="strong",
                       scopes=["read_status"], ttl_s=3600)
    v = client.get(f"/api/session/{sid}/my-companion").json()
    assert [a["client_name"] for a in v["assistants"]] == ["Claude"]
    v = client.post(f"/api/session/{sid}/my-companion/assistants/{grant['grant_id']}/revoke").json()
    assert v["assistants"] == [] and GrantStore().verify(token) is None
    other = GrantStore().customer_grants("CUST-S02")[0]["grant_id"]
    assert client.post(f"/api/session/{sid}/my-companion/assistants/{other}/revoke").status_code == 404


def test_not_signed_in_or_unknown_session(client):
    assert client.get("/api/session/nope/my-companion").status_code == 404


def test_page_has_the_my_companion_tabs_and_the_launch_panel(client):
    html = client.get("/").text
    for tab in ("chat", "updates", "stylist", "know", "settings"):
        assert f'data-ctab="{tab}"' in html
    assert 'data-tab="launches"' in html and 'id="launchbox"' in html
    js = client.get("/static/app.js").text
    assert "function renderMC" in js and "function renderLaunches" in js
    assert "confirm(" not in js and "alert(" not in js  # no browser dialogs: two-step buttons instead
