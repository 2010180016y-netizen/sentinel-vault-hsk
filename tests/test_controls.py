"""The conversational controls: what a chat may change, and what it may never change."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import DEFAULT_MARKET, MARKETS, RiskPolicy, Settings  # noqa: E402
from agent.control import Controls, apply_adjustment  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.ledger import Ledger  # noqa: E402

POLICY = RiskPolicy()


def _controls() -> Controls:
    return Controls.from_settings(Settings(policy=POLICY))


def test_size_can_be_lowered_and_raised_inside_the_chain_cap():
    c = _controls()
    adj = apply_adjustment("1회 한도 100으로 바꿔", c, POLICY)
    assert adj and adj.ok and adj.after == 100
    adj = apply_adjustment("per trade size 150", c, POLICY)
    assert adj and adj.ok and c.size_quote == 150


def test_size_above_the_on_chain_cap_is_refused_not_clamped():
    c = _controls()
    before = c.size_quote
    adj = apply_adjustment("1회 한도를 500으로 올려", c, POLICY)
    assert adj and not adj.ok
    assert "200" in adj.message  # the actual on-chain number is quoted back
    assert c.size_quote == before, "a refusal must not silently change the setting"


def test_frequency_changes_stay_above_the_chain_floor():
    c = _controls()
    adj = apply_adjustment("거래 빈도 줄여", c, POLICY)
    assert adj and adj.ok and c.cooldown_s == POLICY.cooldown_s * 2
    adj = apply_adjustment("쿨다운 0", c, POLICY)
    assert adj and adj.ok and c.cooldown_s == POLICY.cooldown_s
    assert "하한" in adj.message


def test_risk_appetite_moves_only_agent_side_knobs():
    c = _controls()
    adj = apply_adjustment("리스크 낮춰", c, POLICY)
    assert adj and adj.ok and c.risk_appetite == "low"
    assert c.min_conviction > 0.55 and c.target_position_frac < 0.6
    # the chain policy is untouched by construction (it is a frozen value passed in)
    assert POLICY.max_drawdown_bps == 1500


def test_asset_switch_is_offered_with_its_measured_cost():
    c = _controls()
    adj = apply_adjustment("WETH로 바꿔", c, POLICY)
    assert adj and adj.ok and c.asset == "WETH"
    assert "비쌉니다" in adj.message  # measured impact is quoted, not hidden


def test_unknown_market_is_not_switched():
    c = _controls()
    assert apply_adjustment("DOGE로 바꿔", c, POLICY) is None
    assert c.asset == DEFAULT_MARKET
    assert "DOGE" not in MARKETS


def test_plain_question_is_not_an_adjustment():
    c = _controls()
    assert apply_adjustment("지금 상태 어때?", c, POLICY) is None


class _NoNetwork:
    """SentinelAgent only needs an Rpc for the optional vault view; nothing here touches the network."""


def test_adjusting_a_strategy_parameter_rehashes_the_strategy():
    agent = SentinelAgent(Settings(), rpc=_NoNetwork(), ledger=Ledger(), dex=object())
    before = agent.strategy_hash
    msg = agent.answer("리스크 높여")
    assert agent.controls.risk_appetite == "high"
    assert agent.strategy_hash != before
    assert "registerStrategy" in msg, "a hash change must be surfaced, not applied silently"
    assert agent.ledger.decisions[-1]["action"] == "ADJUST"


def test_refusal_does_not_touch_the_strategy():
    agent = SentinelAgent(Settings(), rpc=_NoNetwork(), ledger=Ledger(), dex=object())
    before = agent.strategy_hash
    msg = agent.answer("1회 한도를 900으로")
    assert agent.strategy_hash == before
    assert msg.startswith("⛔")
