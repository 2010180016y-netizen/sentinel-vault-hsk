"""Cross-language parity: the *same* table, two implementations.

`test/fixtures/policy_scenarios.json` is read by the Solidity kernel
(`test/SentinelVault.t.sol::test_SharedPolicyTable`) and by this test. If a predicate is added to one side,
or its order changes in one side, one of the two suites fails — which is the only way a hand-written mirror
stays honest.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import RiskPolicy  # noqa: E402
from agent.policy import AccountState, MarketState, check  # noqa: E402

FIXTURE = os.path.join(ROOT, "test", "fixtures", "policy_scenarios.json")


def load_table() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_fixture_is_the_one_the_contract_reads():
    """The Solidity test must read the same path, or 'parity' is a story rather than a fact."""
    sol = open(os.path.join(ROOT, "test", "SentinelVault.t.sol"), encoding="utf-8").read()
    assert "test/fixtures/policy_scenarios.json" in sol


@pytest.mark.parametrize("scenario", load_table()["scenarios"], ids=lambda s: s["id"])
def test_mirror_reaches_the_same_verdict_as_the_contract(scenario):
    table = load_table()
    pol = table["policy"]
    policy = RiskPolicy(
        min_edge_bps=pol["minEdgeBps"],
        max_slippage_bps=pol["maxSlippageBps"],
        max_spot_twap_deviation_bps=pol["maxSpotTwapDeviationBps"],
        max_drawdown_bps=pol["maxDrawdownBps"],
        twap_window=pol["twapWindow"],
        cooldown_s=pol["cooldown"],
        max_slices=pol["maxSlices"],
        max_trade_notional=pol["maxTradeNotional"],
        max_daily_notional=scenario["maxDailyNotional"],
    )

    # The contract computes fair value from the pool's TWAP; here the same integer is used on both sides of
    # every comparison, expressed as a ratio so the two TWAP implementations need not agree bit for bit.
    fair_out = 1_000_000_000_000_000_000_000  # any positive integer; only ratios matter
    min_out = fair_out * scenario["minOutBpsOfFair"] // 10_000
    amount_in = scenario["amountIn"]
    notional_quote = amount_in / 1e6
    daily = scenario["preSpendQuote"] / 1e6

    verdict = check(
        is_buy=True,
        notional_quote=notional_quote,
        declared_edge_bps=scenario["declaredEdgeBps"],
        min_out=min_out,
        fair_out=fair_out,
        max_notional_quote=scenario["maxNotionalQuote"] / 1e6,
        policy=policy,
        account=AccountState(equity_quote=5_000.0, high_water_quote=5_000.0, daily_notional_quote=daily),
        market=MarketState(spot_vs_twap_bps=float(scenario["spotOffsetTicks"]), fair_out=fair_out),
        now_ts=0,
    )

    assert verdict.reason == scenario["expect"], verdict.detail


def test_every_contract_expectation_is_mapped():
    """A typo in the table would otherwise fail as 'OK' vs an unmapped selector at the Foundry side."""
    known = {
        "OK",
        "EDGE_BELOW_HURDLE",
        "SLIPPAGE_WINDOW",
        "MIN_OUT_ABOVE_FAIR",
        "TRADE_TOO_LARGE",
        "EXCEEDS_INTENT_CAP",
        "SPOT_DEVIATION",
        "DAILY_LIMIT",
        "EDGE_CLAIM_ABOVE_DISLOCATION",
        "COOLDOWN",
    }
    for s in load_table()["scenarios"]:
        assert s["expect"] in known, s["id"]
