"""The LLM's leash: it may narrate, it may propose, it may not move a number or an order.

These tests are the difference between "an LLM is integrated" and "an LLM is in the loop under the same
discipline as the agent's own claim":

  * a narration that invents a number is discarded, visibly;
  * a proposal above the on-chain cap, with no signal, or with nothing to sell is refused by the same
    mirror the chain uses;
  * the deterministic decision is identical whether or not a model is configured.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.lab import Scenario, run as run_scenario  # noqa: E402
from agent.narrator import (  # noqa: E402
    GuardedNarrator,
    HttpNarrator,
    NullNarrator,
    Proposal,
    ScriptedNarrator,
    build_narrator,
    evaluate_proposal,
    facts_from_record,
    numbers_are_grounded,
    parse_proposal,
)

FACTS = {"action": "BUY", "edge_bps": 166.0, "hurdle_bps": 146.0, "size_quote": 60.989, "executed": True}


def test_grounded_numbers_pass():
    ok, stray = numbers_are_grounded("엣지 166bp가 허들 146bp를 넘어 60.99 USDT를 매수했습니다.", FACTS)
    assert ok and stray == []


def test_invented_numbers_are_caught():
    ok, stray = numbers_are_grounded("엣지 999bp로 500 USDT를 매수하면 12% 수익입니다.", FACTS)
    assert not ok
    assert set(stray) >= {"999", "500", "12"}


def test_protocol_names_are_not_quantities():
    ok, stray = numbers_are_grounded("ERC-8004 신원과 x402 결제, EIP-712 서명(체인 eip155:177)입니다.", FACTS)
    assert ok, stray


def test_a_liar_is_replaced_by_the_deterministic_sentence():
    g = GuardedNarrator(ScriptedNarrator("엣지 999bp, 500 USDT 매수."))
    out = g.narrate(FACTS, "fallback sentence")
    assert out["text"] == "fallback sentence"
    assert out["rejected"] is True and "999" in out["reason"]


def test_an_honest_reply_is_kept_and_labelled():
    g = GuardedNarrator(ScriptedNarrator("엣지 166bp가 허들 146bp를 넘어 60.99 USDT를 매수했습니다."))
    out = g.narrate(FACTS, "fallback")
    assert out["source"] == "llm" and out["rejected"] is False


def test_no_narrator_configured_is_not_a_failure():
    out = GuardedNarrator(NullNarrator()).narrate(FACTS, "fallback")
    assert out == {"text": "fallback", "source": "deterministic", "rejected": False, "reason": "no narrator configured"}
    assert build_narrator({}).name == "null"
    assert build_narrator({"SENTINEL_LLM_SCRIPT": "hi"}).name == "scripted"


def test_a_broken_endpoint_falls_back_instead_of_raising():
    g = GuardedNarrator(HttpNarrator("http://127.0.0.1:9", timeout_s=0.2))
    out = g.narrate(FACTS, "fallback")
    assert out["text"] == "fallback" and out["rejected"] is True
    assert "narrator failed" in out["reason"]


class _OpenAIish(BaseHTTPRequestHandler):
    reply = "엣지 166bp가 허들 146bp를 넘었습니다."
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps({"choices": [{"message": {"content": type(self).reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_http_narrator_speaks_the_openai_wire_format():
    srv = HTTPServer(("127.0.0.1", 0), _OpenAIish)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        n = HttpNarrator(f"http://127.0.0.1:{srv.server_port}", model="m", api_key="k")
        out = GuardedNarrator(n).narrate(FACTS, "fallback")
        assert out["source"] == "llm"
        seen = _OpenAIish.seen[-1]
        assert seen["path"] == "/chat/completions"
        assert seen["auth"] == "Bearer k"
        assert seen["body"]["model"] == "m"
        assert "may not invent numbers" in seen["body"]["messages"][0]["content"]
    finally:
        srv.shutdown()


def test_proposal_parsing_is_strict():
    p, why = parse_proposal('sure! {"action":"buy","size_quote":60,"confidence":1.4,"rationale":"dip"}')
    assert p == Proposal("BUY", 60.0, 1.0, "dip") and why == ""
    assert parse_proposal("buy now")[0] is None
    assert parse_proposal('{"action":"YOLO","size_quote":1}')[0] is None
    assert parse_proposal('{"action":"BUY","size_quote":-5}')[0] is None


def test_a_proposal_above_the_cap_is_refused_by_the_chain():
    r = evaluate_proposal(Proposal("BUY", 500, 0.9, "all in"), dev_bps=-400, has_position=False,
                          position_quote=0, price=0.0808)
    assert r["allowed"] is False and r["blocked_by"] == "policy:TRADE_TOO_LARGE"


def test_a_proposal_without_a_signal_is_refused():
    r = evaluate_proposal(Proposal("BUY", 60, 0.9, "vibes"), dev_bps=0, has_position=False,
                          position_quote=0, price=0.0808)
    assert r["allowed"] is False and r["blocked_by"] == "policy:EDGE_BELOW_HURDLE"


def test_a_proposal_to_sell_nothing_is_refused():
    r = evaluate_proposal(Proposal("SELL", 60, 0.9, ""), dev_bps=300, has_position=False,
                          position_quote=0, price=0.0808)
    assert r["allowed"] is False and r["blocked_by"] == "policy:NOTHING_TO_SELL"


def test_a_hold_asks_for_nothing():
    r = evaluate_proposal(Proposal("HOLD", 0), dev_bps=0, has_position=False, position_quote=0, price=0.0808)
    assert r["allowed"] is True and r["evaluation"] is None


def test_narration_never_changes_the_decision():
    """The same scenario, with and without a model, must produce the same numbers."""
    before = run_scenario(Scenario(dev_bps=-400, strategy_mode="dislocation"))
    g = GuardedNarrator(ScriptedNarrator("완전히 다른 이야기를 합니다 999bp 500 USDT."))
    out = g.narrate(facts_from_record(before["decision"] | {"executed": True}), "fallback")
    after = run_scenario(Scenario(dev_bps=-400, strategy_mode="dislocation"))
    assert out["rejected"] is True
    assert before["decision"] == after["decision"]
    assert before["order"]["plan"] == after["order"]["plan"]


def test_facts_only_carry_numbers_the_agent_already_produced():
    record = {
        "action": "BUY",
        "edge_bps": 166.0,
        "size_quote": 60.9,
        "features": {"twap_dev_bps": -381.0},
        "policy": {"ok": True, "reason": "OK", "gates": []},
        "explain": "long prose that is not a number source",
        "secret": "0xdeadbeef",
    }
    facts = facts_from_record(record)
    assert facts["edge_bps"] == 166.0 and facts["features"]["twap_dev_bps"] == -381.0
    assert "explain" not in facts and "secret" not in facts
