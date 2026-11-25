"""Dashboard API: every panel reads a real endpoint, and every endpoint reads the real agent.

The dashboard is the part a judge actually touches, so it gets the same treatment as the trade path:
one HTTP server started from the real `agent.server`, a real `SentinelAgent` on a fake DEX, and
assertions on the JSON the browser receives. No panel is allowed to be a mock-up.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_runtime  # noqa: E402  (its FakeDex/FakeRpc are the deterministic venue)
from agent import server as server_mod  # noqa: E402


@pytest.fixture()
def live_server(monkeypatch, tmp_path):
    monkeypatch.setattr(server_mod, "_agent", test_runtime.agent())
    monkeypatch.setattr(server_mod, "_premium", None)
    monkeypatch.setattr(server_mod, "_last_demo_payment", 0.0)
    monkeypatch.delenv("SENTINEL_X402_PAY_TO", raising=False)
    monkeypatch.delenv("SENTINEL_OWNER", raising=False)
    monkeypatch.delenv("SENTINEL_VAULT", raising=False)
    server_mod.Handler.state_path = str(tmp_path / "ledger.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return json.loads(r.read())


def test_health_and_state(live_server):
    assert get(live_server, "/health") == {"ok": True}
    s = get(live_server, "/api/state")
    assert s["market"]["symbol"] == "WHSK/USDT"
    assert s["policy"]["min_edge_bps"] == 146
    assert s["guards"]["mode"] in ("confirm", "autonomous")
    assert "actions" in s["runtime"] and len(s["runtime"]["actions"]) == 9
    assert s["llm"]["configured"] is False
    assert s["onchain"]["ok"] is False


def test_step_then_trace_exposes_the_gates(live_server):
    rec = get(live_server, "/api/step")["record"]
    trace = get(live_server, "/api/trace")
    assert trace["decision"]["action"] == rec["action"]
    assert trace["gates"], "every decision carries the gates it was judged by"
    assert {g["code"] for g in trace["gates"]} >= {"ORACLE", "EDGE_BELOW_HURDLE", "SPOT_DEVIATION"}
    assert trace["observation"]["pool"] == "0xpool"


def test_simulate_is_driven_by_query_parameters(live_server):
    quiet = get(live_server, "/api/simulate?dev=0")
    dump = get(live_server, "/api/simulate?dev=-400&strategy=dislocation")
    drawdown = get(live_server, "/api/simulate?dev=-400&strategy=dislocation&equity=800&high=1000")
    assert quiet["decision"]["action"] == "HOLD"
    assert dump["decision"]["action"] == "BUY" and dump["outcome"]["would_execute"] is True
    assert drawdown["requested"]["blocked_by"] == "policy:DRAWDOWN"
    oversized = get(live_server, "/api/simulate?dev=-400&strategy=dislocation&size=400")
    assert oversized["requested"]["blocked_by"] == "policy:TRADE_TOO_LARGE"


def test_presets_and_catalog(live_server):
    p = get(live_server, "/api/presets")["presets"]
    assert "dump" in p and p["dump"]["label"]
    c = get(live_server, "/api/catalog")
    assert "sentinel-hsk" in c["plugin"]
    assert "market" in c["providers"]
    assert any(a["name"] == "TRADE_ONCE" for a in c["actions"])


def test_chat_refuses_what_only_the_owner_key_can_move(live_server):
    refused = get(live_server, "/api/ask?q=" + urllib.parse.quote("1회 한도를 500으로 올려줘"))["answer"]
    assert "500" in refused and "200" in refused
    state = get(live_server, "/api/state")
    assert state["controls"]["size_quote"] == 120.0, "a refused request must not move the agent either"
    moved = get(live_server, "/api/ask?q=" + urllib.parse.quote("리스크 낮춰"))["answer"]
    assert "low" in moved or "낮" in moved
    assert "strategyHash" in moved, "the reply says the new hash must be registered before it can trade"


def test_narrate_returns_the_deterministic_sentence_without_a_model(live_server):
    get(live_server, "/api/step")
    out = get(live_server, "/api/narrate")
    assert out["source"] == "deterministic"
    assert out["text"], "a decision always has a sentence even with no LLM configured"


def test_x402_info_reports_itself_unconfigured(live_server):
    info = get(live_server, "/api/x402/info")
    assert info["configured"] is False and info["reason"]
    demo = get(live_server, "/api/x402/demo")
    assert demo["ok"] is False and "SENTINEL_X402" in demo["error"]


def test_erc8004_without_configuration_still_answers(live_server):
    out = get(live_server, "/api/erc8004")
    assert out["configured"] is False
    assert out["registries"]["identity"] == ""


def test_reconcile_without_a_vault_says_so(live_server):
    out = get(live_server, "/api/reconcile")
    assert out["ok"] is False and "SENTINEL_VAULT" in out["error"]


def test_reconcile_compares_the_two_books(live_server, monkeypatch):
    class FakeVault:
        def summary(self):
            return {
                "ok": True,
                "address": "0xvault",
                "trade_count": 0,
                "declared_edge_sum_bps": 0,
                "realised_edge_sum_bps": 0,
                "realised_pnl_quote": 0,
                "avg_execution_edge_bps": 0,
            }

    server_mod._agent.vault_view = FakeVault()
    out = get(live_server, "/api/reconcile")
    assert out["ok"] is True and out["all_match"] is True
    assert {row["field"] for row in out["rows"]} >= {"trades", "declared_edge_sum_bps"}


def test_unknown_path_serves_the_dashboard(live_server):
    with urllib.request.urlopen(live_server + "/", timeout=10) as r:
        html = r.read().decode()
    assert "Sentinel HSK" in html and "/api/simulate" in html
