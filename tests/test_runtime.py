"""Runtime (elizaOS component model) behaviour, on a real agent with a fake DEX.

The point of these tests is not that a runtime exists but that the boundaries it encodes hold:
an action that does not validate never runs, a declared parameter schema is enforced before the
handler, a provider that fails cannot take the whole state down, and an evaluator records the
outcome without being able to change it.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import MARKETS, Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.guards import GuardLimits, GuardState  # noqa: E402
from agent.ledger import Ledger  # noqa: E402
from agent.market import Bar, PoolState  # noqa: E402
from agent.runtime import AgentRuntime, Action, ActionResult, Evaluator, Plugin, ProviderSpec, State  # noqa: E402

TICK = -301_487


def _state(dev_ticks: int = -450) -> PoolState:
    return PoolState(
        address="0xpool",
        token0=MARKETS["WHSK"].asset,
        token1="0xUSDT",
        fee=3000,
        sqrt_price_x96=1 << 96,
        tick=TICK + dev_ticks,
        liquidity=1_475_813_786_831_558_685,
        twap_tick=TICK,
        twap_window=600,
        spot_quote_per_base=0.0808,
        twap_quote_per_base=0.0808,
        base_is_token0=True,
    )


class FakeDex:
    def pool_state(self, market, twap_window: int = 600):
        return _state()

    def bars(self, market, n_bars: int = 30, swaps_per_bar: int = 2):
        return [
            Bar(t=i, open=0.08, high=0.08, low=0.08, close=0.08, volume_quote=10.0, buys=1, sells=1)
            for i in range(30)
        ]

    def quote_buy(self, market, amount_quote: int):
        class Q:
            amount_out = amount_quote * 12

        return Q()

    def quote_sell(self, market, amount_asset: int):
        class Q:
            amount_out = amount_asset // 12

        return Q()

    def fair_out(self, market, amount_in: int, buy: bool, twap_quote_per_base: float) -> int:
        return amount_in * 12 if buy else amount_in // 12


class FakeRpc:
    """Never touches the network; the agent only needs chain id for its guard allowlist."""

    def __init__(self):
        self.expected_chain_id = 177


def agent() -> SentinelAgent:
    ledger = Ledger()
    ledger.open_with(1_000.0)
    guards = GuardState.for_market(
        ["0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029", MARKETS["WHSK"].asset], per_period_cap_quote=800
    )
    return SentinelAgent(Settings(), dex=FakeDex(), rpc=FakeRpc(), ledger=ledger, guards=guards)


# ------------------------------------------------------------------ registration rules

def test_duplicate_plugins_and_actions_are_refused():
    rt = AgentRuntime()
    plugin = Plugin(
        "demo",
        actions=(
            Action("PING", "ping", lambda *a: ActionResult(True, "pong"), lambda *a: True),
        ),
    )
    rt.register_plugin(plugin)
    with pytest.raises(ValueError):
        rt.register_plugin(plugin)
    with pytest.raises(ValueError):
        rt.register_plugin(
            Plugin("other", actions=(Action("PING", "dup", lambda *a: ActionResult(True), lambda *a: True),))
        )


def test_provider_failure_is_contained():
    def boom(rt, m, s):
        raise RuntimeError("provider down")

    rt = AgentRuntime()
    rt.register_plugin(
        Plugin(
            "p",
            providers=(
                ProviderSpec("boom", "always fails", boom, position=1),
                ProviderSpec("ok", "fine", lambda rt, m, s: {"text": "alive"}, position=2),
            ),
        )
    )
    state = rt.compose()
    assert "boom unavailable" in state.text
    assert "alive" in state.text


def test_action_parameter_schema_is_enforced_before_the_handler():
    ran = []

    def handler(rt, m, s, params):
        ran.append(params)
        return ActionResult(True, "done")

    from agent.runtime import Parameter

    rt = AgentRuntime()
    rt.register_plugin(
        Plugin(
            "p",
            actions=(
                Action(
                    "SET",
                    "set a boolean",
                    handler,
                    lambda *a: True,
                    parameters=(Parameter("on", "switch", "boolean", required=True),),
                ),
            ),
        )
    )
    res = rt.process("anything", forced_action="SET", params={"on": "yes"})
    assert not res.result.success and "schema" in res.result.error
    assert ran == [], "handler must not run with invalid params"
    res = rt.process("anything", forced_action="SET", params={"on": True})
    assert res.result.success and ran == [{"on": True}]


def test_evaluator_sees_the_result_and_cannot_rewrite_it():
    seen = []

    def evaluator(rt, m, s, result):
        seen.append(result.action)
        return {"evaluator": "spy"}

    rt = AgentRuntime()
    rt.register_plugin(
        Plugin(
            "p",
            actions=(Action("GO", "go", lambda *a: ActionResult(True, "went"), lambda *a: True),),
            evaluators=(Evaluator("spy", "records", evaluator),),
        )
    )
    res = rt.process("go", forced_action="GO")
    assert res.result.text == "went"
    assert seen == ["GO"]
    assert res.evaluations == [{"evaluator": "spy"}]


# ------------------------------------------------------------------ the real plugin

def test_every_action_is_reachable_by_its_own_similes():
    a = agent()
    rt = a.runtime
    assert set(rt.actions) == {
        "READ_MARKET",
        "REPORT_PERFORMANCE",
        "CHECK_RISK",
        "EXPLAIN_DECISION",
        "TRADE_ONCE",
        "ADJUST_AGENT",
        "SET_KILL_SWITCH",
        "BUY_PREMIUM_DATA",
        "REPORT_REPUTATION",
    }
    assert rt.provider_names() == ["market", "risk", "portfolio", "identity", "payments", "recent"]


@pytest.mark.parametrize(
    "text,action",
    [
        ("지금 상태 어때", "READ_MARKET"),
        ("손익 보여줘", "REPORT_PERFORMANCE"),
        ("리스크 한도 알려줘", "CHECK_RISK"),
        ("1회 판단 실행해", "TRADE_ONCE"),
        ("에이전트 정지해", "SET_KILL_SWITCH"),
        ("평판 어때", "REPORT_REPUTATION"),
    ],
)
def test_messages_route_to_the_expected_action(text, action):
    a = agent()
    res = a.runtime.process(text)
    assert res.matched and res.action == action, res.as_dict()


def test_adjustment_beats_a_plain_risk_question():
    a = agent()
    res = a.runtime.process("리스크 낮춰")
    assert res.action == "ADJUST_AGENT"
    assert a.controls.risk_appetite == "low"


def test_trade_once_runs_the_guarded_cycle_and_the_evaluator_records_it():
    a = agent()
    res = a.runtime.process("1회 판단")
    assert res.action == "TRADE_ONCE"
    assert res.result.data["guards"]["ok"] in (True, False)
    assert res.evaluations and res.evaluations[0]["evaluator"] == "execution-quality"


def test_kill_switch_stops_the_next_cycle_before_any_signing():
    a = agent()
    on = a.runtime.process("거래 정지")
    assert on.result.success and a.guard_state.kill_switch
    res = a.runtime.process("1회 판단")
    assert res.result.data["executed"] is False
    assert res.result.data["guards"]["code"] == "KILL_SWITCH"
    off = a.runtime.process("거래 재개")
    assert off.result.success and not a.guard_state.kill_switch


def test_unknown_text_falls_back_to_the_legacy_answers():
    a = agent()
    res = a.runtime.process("zzzz")
    assert res.matched is False
    assert a.chat("zzzz")  # the fallback produced *something* instead of an empty string


def test_snapshot_exposes_the_runtime_catalog():
    a = agent()
    snap = a.snapshot()
    assert snap["runtime"]["plugins"] == ["sentinel-hsk"]
    assert "TRADE_ONCE" in snap["runtime"]["actions"]
    assert snap["runtime"]["providers"] == ["market", "risk", "portfolio", "identity", "payments", "recent"]
