"""
Companion plan P4 (no LLM): the stylist data in use today. The profile shows the style tags and colour families of
what the customer kept, and a stated preference for a colour family ("earth tones") stands for its colours.
"""

import asyncio

from coded_tools.customer_service import companion as cp
from coded_tools.customer_service import recommendations
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.results import TIMEOUT
from coded_tools.customer_service.results import ToolResult
from test_companion_memory import SHOP
from test_companion_memory import remember_confirmed
from test_multi_agent_foundation import isolated  # noqa: F401 - autouse fixture: mock data, temp stores
from test_multi_agent_foundation import mock  # noqa: F401
from test_multi_agent_foundation import start
from test_multi_agent_foundation import web

POLICY = load_policy()
EARTH = ["brown", "camel", "khaki", "olive", "sand", "tan"]


def run(coro):
    return asyncio.run(coro)


def setup_function():
    cp._DATA_CACHE.clear()
    cp._STYLE_CACHE.clear()
    recommendations._CATALOG_CACHE.clear()


def test_profile_shows_kept_styles_and_colour_families(mock):
    view = run(cp.load_view("CUST-B0025", adapter=mock, policy=POLICY))
    assert view["profile"]["styles"] == ["classic", "smart", "casual"]
    assert view["profile"]["colour_families"] == ["dark_neutral", "earth"]
    assert "Styles they keep: classic, smart, casual; mostly dark neutrals and earth tones." in view["digest"]
    assert view["profile"]["sources"]["styles"] == "the styles of items you kept"


def test_without_the_stylist_data_the_profile_says_so(mock, monkeypatch):
    real = mock.fetch

    async def fetch(fn, *args, policy=None):
        if fn == "get_product_styles":
            return ToolResult(status=TIMEOUT)
        return await real(fn, *args, policy=policy)

    monkeypatch.setattr(mock, "fetch", fetch)
    view = run(cp.load_view("CUST-B0025", adapter=mock, policy=POLICY))
    assert "styles" in view["profile"]["unavailable"] and "Styles they keep" not in view["digest"]
    assert ("mock", "CUST-B0025") not in cp._DATA_CACHE  # tried again next time


def test_a_colour_family_stands_for_its_colours(mock):
    families = run(cp.load_style_data(mock, POLICY))["families"]
    signals = cp.preference_signals([{"kind": "likes", "value": "earth tones"},
                                     {"kind": "avoids", "value": "bright colours"}], families)
    assert [w for w in signals["likes"] if w in families] == EARTH
    assert {"red", "orange", "yellow", "pink", "burgundy"} <= set(signals["avoids"])
    assert cp.preference_signals([{"kind": "likes", "value": "earth tones"}])["likes"] == ["earth", "tone"]


def test_suggestions_favour_earth_tones_once_remembered(mock):
    from coded_tools.customer_service.recommendation_tools import GetRecommendations

    remember_confirmed(mock, "CUST-S11", "likes", "earth tones")
    sly = web(mock, "CUST-S11")
    (iid,) = start(sly, SHOP)
    out = run(GetRecommendations().async_invoke({"issue_id": iid, "product_query": "chinos"}, sly))
    liked = [r for r in out["recommendations"] if r.get("in_colours_you_like")]
    assert liked and all(set(r["in_colours_you_like"]) <= set(EARTH) for r in liked)
    assert all(r["available"][0]["color"].lower() in EARTH for r in liked)


def test_staged_products_are_never_suggested_before_launch(mock):
    staged = {r["product_id"] for r in run(mock.get_product_launches())}
    catalog = run(recommendations.load_catalog(mock, POLICY))
    assert staged and all(not catalog.products[pid]["active"] for pid in staged)
    history = run(mock.get_purchase_history("CUST-S13"))  # owns the wool coat: the cashmere scarf complements it
    out = recommendations.recommend(catalog, history, POLICY["recommendations"], product_query="wool overcoat")
    assert not {r["product_id"] for r in out["recommendations"]} & staged
