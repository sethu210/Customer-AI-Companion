"""
Review finding S1: the agent server trusts only identity a gateway signed (gateway_auth.py). These tests run with
unsigned identity NOT trusted, as the agent server always does.
"""

import asyncio
import base64
import json
import time

import pytest

from coded_tools.customer_service import gateway_auth
from coded_tools.customer_service.identity import IdentityError
from coded_tools.customer_service.identity import authenticated_customer_id
from coded_tools.customer_service.identity import ensure_identity
from coded_tools.customer_service.mock_adapter import MockAdapter
from coded_tools.customer_service.policy import load_policy


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def mock() -> MockAdapter:
    return MockAdapter.from_env()


@pytest.fixture(autouse=True)
def strict(monkeypatch, tmp_path):
    monkeypatch.delenv(gateway_auth.TRUST_UNSIGNED_ENV, raising=False)
    monkeypatch.setenv(gateway_auth.KEY_ENV, "test-key-" + "0" * 32)
    monkeypatch.setattr(gateway_auth, "_KEY", None)


def web(mock: MockAdapter, customer_id: str) -> dict:
    ident = next(i["identifier"] for i in mock.identities
                 if i["customer_id"] == customer_id and i["channel"] == "web_session")
    return {"channel": "web_session", "identifier": ident}


def status(sly: dict, adapter) -> str:
    return run(ensure_identity(sly, adapter, load_policy()))["identity_status"]


def test_signed_web_identity_is_resolved(mock):
    sly = gateway_auth.sign(web(mock, "CUST-S01"))
    assert status(sly, mock) == "verified" and sly["customer_id"] == "CUST-S01" and sly["auth_level"] == "strong"


def test_forged_customer_id_is_not_trusted(mock):
    sly = {"customer_id": "CUST-S08", "channel": "web_session", "auth_level": "strong"}
    assert status(sly, mock) == "missing"
    assert "customer_id" not in sly and "auth_level" not in sly


def test_forged_identity_status_is_not_trusted(mock):
    sly = {"customer_id": "CUST-S08", "channel": "web_session", "auth_level": "strong", "identity_status": "verified"}
    assert status(sly, mock) == "missing" and "customer_id" not in sly


def test_unsigned_fields_next_to_a_valid_token_are_replaced_by_the_signed_ones(mock):
    sly = gateway_auth.sign({"customer_id": "CUST-S10", "channel": "phone", "auth_level": "weak"})
    sly.update(customer_id="CUST-S01", auth_level="strong", channel="web_session")  # tampered after signing
    assert status(sly, mock) == "gateway_authenticated"
    assert (sly["customer_id"], sly["auth_level"], sly["channel"]) == ("CUST-S10", "weak", "phone")


def test_tampered_token_is_refused(mock):
    sly = gateway_auth.sign({"customer_id": "CUST-S10", "channel": "web_session", "auth_level": "weak"})
    body, mac = sly["identity_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged = base64.urlsafe_b64encode(json.dumps({**claims, "auth_level": "strong"}).encode()).rstrip(b"=").decode()
    sly["identity_token"] = f"{forged}.{mac}"
    assert status(sly, mock) == "missing" and "customer_id" not in sly


def test_expired_token_is_refused(mock, monkeypatch):
    sly = gateway_auth.sign(web(mock, "CUST-S01"), ttl_s=60)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert status(sly, mock) == "missing"


def test_token_of_another_key_is_refused(mock, monkeypatch):
    sly = gateway_auth.sign(web(mock, "CUST-S01"))
    monkeypatch.setenv(gateway_auth.KEY_ENV, "another-key-" + "1" * 32)
    monkeypatch.setattr(gateway_auth, "_KEY", None)
    assert status(sly, mock) == "missing"


def test_resolved_identity_is_reused_in_the_request_and_a_forged_seal_is_not(mock):
    class Counting(MockAdapter):
        calls = 0

        def __init__(self, base):
            self.__dict__.update(base.__dict__)

        async def resolve_channel_identity(self, channel, identifier):
            Counting.calls += 1
            return await super().resolve_channel_identity(channel, identifier)

    adapter = Counting(mock)
    sly = gateway_auth.sign(web(mock, "CUST-S01"))
    assert status(sly, adapter) == "verified" and status(sly, adapter) == "verified"
    assert Counting.calls == 1  # sealed by the server: not resolved again
    forged = {"channel": "web_session", "customer_id": "CUST-S08", "auth_level": "strong",
              "identity_status": "verified", "identity_seal": sly["identity_seal"]}
    assert status(forged, adapter) == "missing"


def test_single_agent_tools_need_a_signed_customer_too():
    with pytest.raises(IdentityError):
        authenticated_customer_id({"customer_id": "CUST-S08", "channel": "web_session", "auth_level": "strong"})
    signed = gateway_auth.sign({"customer_id": "CUST-S08", "channel": "web_session", "auth_level": "strong"})
    assert authenticated_customer_id(signed) == "CUST-S08"


def test_system_requests_carry_no_identity_token():
    sly = gateway_auth.sign({"channel": "system_tick", "tick_token": "x"})
    assert "identity_token" not in sly


def test_sign_adds_the_customer_message_and_drops_a_callers_seal():
    sly = gateway_auth.sign({"channel": "web_session", "identifier": "a", "identity_seal": "x"},
                            customer_message="Yes please")
    assert sly["customer_message"] == "Yes please" and "identity_seal" not in sly
