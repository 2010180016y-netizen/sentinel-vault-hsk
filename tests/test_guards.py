"""Guard parity and behaviour: the Python side of `test/fixtures/guard_scenarios.json`.

`test/SwapGuard.t.sol::test_SharedGuardTable` runs the EVM implementation over the same table. If this
file and that test disagree with the table, one of the two implementations changed its mind alone.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.guards import (  # noqa: E402
    CODES,
    GuardLimits,
    GuardRequest,
    GuardState,
    depth_share_bps,
    is_address,
    is_amount,
    period_key,
    preflight,
    price_impact_bps,
)

FIXTURE = os.path.join(ROOT, "test", "fixtures", "guard_scenarios.json")


def table() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def limits_from(t: dict, s: dict) -> GuardLimits:
    lim = t["limits"]
    allow = set()
    if s["allowedIn"]:
        allow.add(s["tokenIn"])
    if s["allowedOut"]:
        allow.add(s["tokenOut"])
    return GuardLimits(
        max_slippage_bps=lim["maxSlippageBps"],
        max_price_impact_bps=lim["maxPriceImpactBps"],
        max_depth_share_bps=lim["maxDepthShareBps"],
        min_quote_depth_usd=lim["minDepthQuote"] / 1e6,
        max_quote_age_s=lim["maxQuoteAge"],
        token_allowlist=frozenset(allow),
        per_run_cap_quote=(s["perRunCap"] / 1e6) if s["hasPerRunCap"] else None,
        per_period_cap_quote=(s["perPeriodCap"] / 1e6) if s["hasPerPeriodCap"] else None,
        kill_switch=s["killSwitch"],
        mode="autonomous" if s["autonomous"] else "confirm",
        dry_run_done=s["dryRunDone"],
    )


def request_from(s: dict) -> GuardRequest:
    return GuardRequest(
        token_in=s["tokenIn"],
        token_out=s["tokenOut"],
        amount_in=s["amountIn"],
        notional_quote=s["notionalQuote"] / 1e6,
        quoted_out=s["quotedOut"],
        fair_out=s["fairOut"],
        quote_depth_quote=s["quoteDepthQuote"] / 1e6,
        sell_quote=s["sellQuote"],
        quote_age_s=int(s["nowTs"] - s["quotedAt"]),
        slippage_bps=s["slippageBps"],
        period_spent_quote=s["periodSpentQuote"] / 1e6,
        is_buy=s["isBuy"],
    )


def test_the_solidity_suite_reads_the_same_table():
    sol = open(os.path.join(ROOT, "test", "SwapGuard.t.sol"), encoding="utf-8").read()
    assert "test/fixtures/guard_scenarios.json" in sol
    assert "test_SharedGuardTable" in sol


@pytest.mark.parametrize("scenario", table()["scenarios"], ids=lambda s: s["id"])
def test_mirror_reaches_the_same_verdict_as_the_evm(scenario):
    t = table()
    verdict = preflight(request_from(scenario), limits_from(t, scenario))
    assert verdict.code == scenario["expect"], verdict.detail


def test_every_reason_code_is_exercised_by_the_table():
    used = {s["expect"] for s in table()["scenarios"]}
    missing = set(CODES) - used
    assert not missing, f"fixture never reaches {sorted(missing)}; the predicate is untested"


def test_predicate_order_is_documented_and_matches_the_library():
    """First failure wins: a kill switch must be reported before an allowlist problem, not after."""
    t = table()
    s = dict(next(x for x in t["scenarios"] if x["id"] == "legal_swap_passes"))
    s["killSwitch"] = True
    s["allowedIn"] = False
    s["amountIn"] = 0
    assert preflight(request_from(s), limits_from(t, s)).code == "AMOUNT_INVALID"
    s["amountIn"] = 1
    assert preflight(request_from(s), limits_from(t, s)).code == "KILL_SWITCH"


def test_checks_trace_is_kept_for_the_report():
    t = table()
    s = next(x for x in t["scenarios"] if x["id"] == "legal_swap_passes")
    v = preflight(request_from(s), limits_from(t, s))
    names = [c for c, _ in v.checks]
    assert names[0] == "addresses" and names[-1] == "mode"
    assert all(passed for _, passed in v.checks)
    assert v.as_dict()["price_impact_bps"] >= 0


def test_price_impact_and_depth_share_math():
    assert price_impact_bps(1000, 1100) == 0.0, "a quote better than TWAP is not negative impact"
    assert price_impact_bps(1000, 990) == pytest.approx(100.0)
    assert depth_share_bps(120, 0) == 10_000, "an empty side is entirely consumed"
    assert depth_share_bps(120, 419_423) == pytest.approx(2.86, abs=0.01)


def test_input_validation_rules_from_the_skills():
    assert is_address("0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029")
    assert not is_address("F1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029")
    assert not is_address("0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029; rm -rf /")
    assert is_amount("120.5") and is_amount(120) and is_amount(0)
    assert not is_amount("-1") and not is_amount("1; drop") and not is_amount(True)


def test_guard_state_tracks_period_spend_and_caps():
    st = GuardState.for_market(
        ["0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029", "0xB210D2120d57b758EE163cFfb43e73728c471Cf1"],
        per_run_cap_quote=200,
        per_period_cap_quote=300,
        mode="autonomous",
    )
    period = period_key(1_700_000_000)
    assert st.spent(period) == 0
    st.charge(period, 150)
    assert st.spent(period) == 150

    req = GuardRequest(
        token_in="0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029",
        token_out="0xB210D2120d57b758EE163cFfb43e73728c471Cf1",
        amount_in=140_000_000,
        notional_quote=140,
        quoted_out=1_000,
        fair_out=1_000,
        quote_depth_quote=500_000,
        sell_quote=1,
        quote_age_s=1,
        slippage_bps=100,
        period_spent_quote=st.spent(period),
        is_buy=True,
    )
    assert preflight(req, st.limits_for(period)).code == "AUTONOMY_UNARMED"
    st.mark_dry_run()
    assert preflight(req, st.limits_for(period)).code == "OK"
    st.charge(period, 100)  # 250 spent: +140 would cross the 300 period cap
    req.period_spent_quote = st.spent(period)
    assert preflight(req, st.limits_for(period)).code == "PER_PERIOD_CAP"
    st.engage_kill_switch()
    assert preflight(req, st.limits_for(period)).code == "KILL_SWITCH"


def test_period_key_is_a_utc_day():
    assert period_key(0) == "1970-01-01"
    assert period_key(1_700_000_000) == "2023-11-14"
