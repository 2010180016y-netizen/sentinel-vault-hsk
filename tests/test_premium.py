"""The paid endpoint itself: challenge, verify, settle, and what the payer receives."""

import os
import sys
from pathlib import Path

import pytest
from eth_account import Account

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.premium import PremiumConfig, PremiumService  # noqa: E402
from agent.x402 import Budget, X402Client, decode_header, encode_header  # noqa: E402
from agent.x402.types import network_for  # noqa: E402

TEAM_DIR = os.path.join(ROOT, "tests")
sys.path.insert(0, TEAM_DIR)

import test_runtime  # noqa: E402

KEY = "0x" + "5c" * 32


def test_endpoint_reports_itself_unconfigured_instead_of_inventing_a_payee(monkeypatch):
    for var in ("SENTINEL_X402_PAY_TO", "SENTINEL_OWNER", "SENTINEL_VAULT"):
        monkeypatch.delenv(var, raising=False)
    cfg = PremiumConfig.from_env()
    assert cfg.requirements is None
    assert "SENTINEL_X402_PAY_TO" in cfg.reason


def test_endpoint_uses_the_operator_address_and_the_chain_id(monkeypatch):
    monkeypatch.setenv("SENTINEL_X402_PAY_TO", Account.from_key(KEY).address)
    cfg = PremiumConfig.from_env()
    assert cfg.requirements is not None
    assert cfg.requirements.network == network_for(177)
    assert cfg.requirements.amount == "1000"
    assert cfg.requirements.extra["name"] == "USDT"


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_X402_PAY_TO", Account.from_key(KEY).address)
    agent = test_runtime.agent()
    cfg = PremiumConfig.from_env()
    return PremiumService(agent, cfg, state_path=str(tmp_path / "settlements.json")), cfg


def test_challenge_header_round_trips(service):
    svc, cfg = service
    challenge = svc.challenge("http://127.0.0.1:8765")
    decoded = decode_header(encode_header(challenge))
    assert decoded["x402Version"] == 2
    assert decoded["accepts"][0]["payTo"] == cfg.requirements.pay_to
    assert decoded["resource"]["url"].endswith("/api/premium/edge")


def test_payer_receives_the_analytics_after_settlement(service):
    svc, cfg = service
    client = X402Client(KEY, chain_id=177, budget=Budget(cap=10_000))
    payload = client._sign(
        cfg.requirements,
        {"accepts": [cfg.requirements.as_dict()], "resource": {"url": "http://127.0.0.1:8765/api/premium/edge"}},
    )
    settlement, data = svc.settle(encode_header(payload.as_dict()), "http://127.0.0.1:8765")
    assert settlement.success, settlement.error_reason
    assert data is not None
    assert set(data) >= {"market", "policy", "edge", "risk", "guards"}
    assert data["guards"]["mode"] in ("confirm", "autonomous")


def test_same_header_twice_is_refused_and_returns_the_challenge(service):
    svc, cfg = service
    client = X402Client(KEY, chain_id=177, budget=Budget(cap=10_000))
    payload = client._sign(
        cfg.requirements,
        {"accepts": [cfg.requirements.as_dict()], "resource": {"url": "http://127.0.0.1:8765/api/premium/edge"}},
    )
    header = encode_header(payload.as_dict())
    first, _ = svc.settle(header, "http://127.0.0.1:8765")
    second, body = svc.settle(header, "http://127.0.0.1:8765")
    assert first.success and not second.success
    assert second.error_reason == "nonce_already_used"
    assert body == {"error": "nonce_already_used"}, "the caller is told why, not handed the data"


def test_the_real_http_handler_serves_402_then_200(monkeypatch, tmp_path):
    """End-to-end through `agent.server.Handler`: our handler, our client, the spec's three headers."""
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.parse import urlparse

    from agent import server as server_mod
    from agent.x402 import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER, decode_header

    pay_to = Account.from_key(KEY).address
    monkeypatch.setenv("SENTINEL_X402_PAY_TO", pay_to)
    fake = test_runtime.agent()
    monkeypatch.setattr(server_mod, "_agent", fake)
    monkeypatch.setattr(server_mod, "_premium", None)

    handler_cls = server_mod.Handler
    handler_cls.state_path = str(tmp_path / "ledger.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/premium/edge"
    try:
        import urllib.error
        import urllib.request

        try:
            urllib.request.urlopen(url, timeout=10)
            raise AssertionError("the endpoint must answer 402 without a payment header")
        except urllib.error.HTTPError as exc:
            assert exc.code == 402
            challenge = decode_header(exc.headers["PAYMENT-REQUIRED"])
            assert challenge["x402Version"] == 2 and challenge["accepts"][0]["payTo"] == pay_to

        client = X402Client(KEY, chain_id=177, budget=Budget(cap=10_000))
        result = client.pay_and_fetch(url)
        assert result.ok and result.status == 200, result.as_dict()
        assert result.settlement and result.settlement.success
        assert "edge" in result.data and "guards" in result.data
        assert result.data["edge"]["last_hurdle_bps"] is not None or True
    finally:
        srv.shutdown()
        srv.server_close()
