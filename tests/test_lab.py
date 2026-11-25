"""The scenario lab: the same gates the chain and the agent use, driven from a hypothetical.

These tests pin the boundaries the dashboard's sliders cross, so a pretty colour cannot disagree with
the rule: every assertion here is about a number a judge can reproduce with the same call.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import RiskPolicy  # noqa: E402
from agent.lab import PRESETS, Scenario, presets, run  # noqa: E402


def _gates(result, side="order"):
    block = result[side]["policy"]["gates"] if result[side] else []
    return {g["code"]: g for g in block}


def test_quiet_market_holds_and_the_chain_would_refuse_a_claim():
    r = run(Scenario(dev_bps=0))
    assert r["decision"]["action"] == "HOLD"
    assert r["order"] is None, "no order is signed when the strategy holds"
    assert r["requested"]["allowed"] is False
    assert r["requested"]["blocked_by"] == "policy:EDGE_BELOW_HURDLE"
    gate = _gates(r, "requested")["EDGE_BELOW_HURDLE"]
    assert gate["ok"] is False and gate["limit"] == "≥ 146bp"


def test_the_trend_filter_refuses_a_dump_the_vault_would_allow():
    """The two gates answer different questions; the lab must not blur them."""
    r = run(Scenario(dev_bps=-400))  # default strategy
    assert r["decision"]["action"] == "HOLD"
    assert any("adverse trend" in reason for reason in r["decision"]["reasons"])
    assert r["requested"]["allowed"] is True, "the chain would have allowed the order; the strategy said no"


def test_the_dislocation_strategy_takes_the_same_dump():
    r = run(Scenario(dev_bps=-400, strategy_mode="dislocation"))
    assert r["decision"]["action"] == "BUY"
    assert r["outcome"]["would_execute"] is True
    assert 0 < r["decision"]["size_quote"] <= 120
    assert r["order"]["policy"]["ok"] and r["order"]["guards"]["ok"]
    claim = r["order"]["plan"]["declared_edge_bps"]
    assert claim <= abs(400) and claim >= RiskPolicy().min_edge_bps


def test_an_oversized_request_is_refused_by_the_chain_even_when_the_strategy_sizes_down():
    r = run(Scenario(dev_bps=-400, size_quote=400, strategy_mode="dislocation"))
    assert r["decision"]["action"] == "BUY"
    assert r["decision"]["size_quote"] <= 120, "the strategy caps itself at max_trade_notional before signing"
    assert r["requested"]["allowed"] is False
    assert r["requested"]["blocked_by"] == "policy:TRADE_TOO_LARGE"
    assert _gates(r, "requested")["TRADE_TOO_LARGE"]["actual"] == "400.00 USDT"


def test_a_drawdown_blocks_buys_and_leaves_the_exit_alone():
    buying = run(Scenario(dev_bps=-400, equity=800, high_water=1_000, strategy_mode="dislocation"))
    assert buying["order"]["blocked_by"] == "policy:DRAWDOWN"
    selling = run(Scenario(dev_bps=260, has_position=True, position_quote=120, equity=800, high_water=1_000))
    assert selling["decision"]["action"] == "SELL"
    assert selling["order"]["allowed"] is True
    assert _gates(selling, "order")["DRAWDOWN"]["applies"] is False


def test_the_kill_switch_outranks_the_economics():
    r = run(Scenario(dev_bps=-400, kill_switch=True, strategy_mode="dislocation"))
    assert r["requested"]["guards"]["ok"] is False
    assert r["requested"]["guards"]["code"] == "KILL_SWITCH"
    assert r["requested"]["policy"]["ok"] is True, "the policy is fine; the operator's brake is what stopped it"


def test_exit_gates_are_exempt_not_failed():
    r = run(Scenario(dev_bps=260, has_position=True, position_quote=120))
    gates = _gates(r, "order")
    for code in ("EDGE_BELOW_HURDLE", "DAILY_LIMIT", "COOLDOWN"):
        assert gates[code]["applies"] is False and gates[code]["ok"] is True


def test_gate_order_matches_the_contract():
    """The trace must stop where the EVM would stop, in the same order."""
    r = run(Scenario(dev_bps=-400, size_quote=400, strategy_mode="dislocation"))
    codes = [g["code"] for g in r["requested"]["policy"]["gates"]]
    assert codes == [
        "ORACLE",
        "EDGE_BELOW_HURDLE",
        "SPOT_DEVIATION",
        "EDGE_CLAIM_ABOVE_DISLOCATION",
        "FAIR_VALUE_ZERO",
        "MIN_OUT_ABOVE_FAIR",
        "SLIPPAGE_WINDOW",
        "TRADE_TOO_LARGE",
    ]
    assert codes[-1] == r["requested"]["policy"]["reason"]


def test_a_spot_move_beyond_the_manipulation_breaker_is_refused():
    r = run(Scenario(dev_bps=-600, strategy_mode="dislocation"))
    assert r["requested"]["blocked_by"] == "policy:SPOT_DEVIATION"


def test_every_preset_runs_and_reports_an_outcome():
    out = presets()
    assert set(out) == set(PRESETS)
    for key, entry in out.items():
        assert entry["label"]
        assert entry["result"]["outcome"]["explain"]
        assert isinstance(entry["result"]["outcome"]["would_execute"], bool)


def test_lab_is_deterministic():
    a = run(Scenario(dev_bps=-377.5, size_quote=87.5, strategy_mode="dislocation"))
    b = run(Scenario(dev_bps=-377.5, size_quote=87.5, strategy_mode="dislocation"))
    assert a == b
