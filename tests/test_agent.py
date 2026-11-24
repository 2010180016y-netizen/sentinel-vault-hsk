import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import intent as intent_mod  # noqa: E402
from agent.config import RiskPolicy, Strategy  # noqa: E402
from agent.ledger import Ledger, Position  # noqa: E402
from agent.market import Bar, tick_to_price  # noqa: E402
from agent.policy import AccountState, MarketState, build_min_out, check, in_slippage_band  # noqa: E402
from agent.signals import build_features, decide, ema, rsi  # noqa: E402


# --------------------------------------------------------------------------- intent / EIP-712

def test_intent_type_string_matches_solidity():
    """The off-chain type string must be byte-identical to the contract's, or every signature fails."""
    src = open(os.path.join(ROOT, "src", "SentinelVault.sol"), encoding="utf-8").read()
    m = re.search(r'keccak256\(\s*"(Intent\([^"]+\))"', src)
    assert m, "INTENT_TYPEHASH literal not found in the contract"
    assert m.group(1) == intent_mod.INTENT_TYPE


def test_signing_roundtrip_recovers_both_signers():
    from eth_account import Account

    a, b = Account.create(), Account.create()
    it = intent_mod.Intent(
        token_in="0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029",
        token_out="0xB210D2120d57b758EE163cFfb43e73728c471Cf1",
        fee=3000,
        amount_in=100_000_000,
        min_amount_out=1,
        declared_edge_bps=120,
        strategy_hash=intent_mod.strategy_id({"x": 1}),
        nonce=7,
        deadline=2**40,
        max_notional_quote=200_000_000,
    )
    vault = "0x000000000000000000000000000000000000dEaD"
    s1, s2 = intent_mod.dual_sign(it, a.key.hex(), b.key.hex(), vault, 177)
    assert intent_mod.recover(it, s1, vault, 177).lower() == a.address.lower()
    assert intent_mod.recover(it, s2, vault, 177).lower() == b.address.lower()


def test_strategy_id_is_deterministic_and_sensitive():
    spec = Strategy().spec()
    assert intent_mod.strategy_id(spec) == intent_mod.strategy_id(json.loads(json.dumps(spec, sort_keys=True)))
    spec2 = dict(spec)
    spec2["min_conviction"] = 0.9
    assert intent_mod.strategy_id(spec) != intent_mod.strategy_id(spec2)


def test_calldata_selector_matches_signature():
    from eth_utils import keccak

    sig = (
        "executeIntent((address,address,uint24,uint128,uint128,uint16,bytes32,bytes32,uint64,uint64,uint64),"
        "bytes,bytes)"
    )
    it = intent_mod.Intent(
        token_in="0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029",
        token_out="0xB210D2120d57b758EE163cFfb43e73728c471Cf1",
        fee=3000, amount_in=1, min_amount_out=1, declared_edge_bps=100,
        strategy_hash="0x" + "11" * 32, nonce=1, deadline=2, max_notional_quote=1,
    )
    data = intent_mod.encode_execute_calldata(it, "0x" + "aa" * 65, "0x" + "bb" * 65)
    assert data[:4] == keccak(text=sig)[:4]


# --------------------------------------------------------------------------- signals / policy

def _bars(prices):
    return [Bar(t=i, open=p, high=p, low=p, close=p, volume_quote=1.0, buys=1, sells=0) for i, p in enumerate(prices)]


def test_ema_and_rsi_bounds():
    assert ema([1, 1, 1, 1], 3) == pytest.approx(1.0)
    assert 0 <= rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]) <= 100
    assert rsi([5] * 20) == 50.0


def test_round_trip_cost_counts_both_legs():
    """A taker pays fee + impact twice. The earlier model averaged the impacts and understated the hurdle."""
    f = build_features(
        _bars([1.0, 1.001, 1.0, 0.999] * 6),
        twap_dev_bps=-200, fee_bps=30, impact_bps_buy=33, impact_bps_sell=33,
        liquidity_quote=419_000, strategy=Strategy(),
    )
    assert f.round_trip_cost_bps == pytest.approx(2 * 30 + 33 + 33)
    d = decide(f, RiskPolicy(), Strategy(), has_position=False, position_quote=0)
    assert d.hurdle_bps == pytest.approx(max(RiskPolicy().min_edge_bps, f.round_trip_cost_bps + 20))


def test_decision_never_claims_more_than_the_dislocation():
    """The claim is capped at the dislocation of the venue: the vault refuses anything above it."""
    f = build_features(
        _bars([1.0, 1.02, 1.05, 1.09] * 6),  # strong trend, moderate dislocation
        twap_dev_bps=-150, fee_bps=30, impact_bps_buy=33, impact_bps_sell=33,
        liquidity_quote=419_000, strategy=Strategy(),
    )
    d = decide(f, RiskPolicy(), Strategy(), has_position=False, position_quote=0)
    assert d.edge_bps <= abs(f.twap_dev_bps)
    assert d.dislocation_bps == pytest.approx(150)


def test_decision_refuses_a_dislocation_inside_costs():
    f = build_features(
        _bars([1.0, 1.001, 1.002, 1.003] * 6),
        twap_dev_bps=-40, fee_bps=30, impact_bps_buy=33, impact_bps_sell=33,
        liquidity_quote=419_000, strategy=Strategy(),
    )
    d = decide(f, RiskPolicy(), Strategy(), has_position=False, position_quote=0)
    assert d.action == "HOLD"
    assert d.edge_bps < d.hurdle_bps
    assert any("hurdle" in r for r in d.reasons)


def test_decision_trades_a_large_dislocation():
    f = build_features(
        _bars([1.0, 0.999, 0.998, 1.0] * 6),
        twap_dev_bps=-400, fee_bps=30, impact_bps_buy=33, impact_bps_sell=33,
        liquidity_quote=419_000, strategy=Strategy(),
    )
    d = decide(f, RiskPolicy(), Strategy(), has_position=False, position_quote=0)
    assert d.action == "BUY"
    assert d.edge_bps >= d.hurdle_bps
    assert 0 < d.size_quote <= RiskPolicy().max_trade_notional / 1e6


def test_decision_sells_an_overbought_book():
    f = build_features(
        _bars([1.0] * 10 + [1.01] * 12),
        twap_dev_bps=60, fee_bps=30, impact_bps_buy=5, impact_bps_sell=5,
        liquidity_quote=419_000, strategy=Strategy(),
    )
    d = decide(f, RiskPolicy(), Strategy(), has_position=True, position_quote=90.0)
    assert d.action == "SELL"
    assert d.size_quote == 90.0


def test_policy_blocks_drawdown_buys():
    v = check(
        is_buy=True, notional_quote=100, declared_edge_bps=200, min_out=99, fair_out=100,
        max_notional_quote=200, policy=RiskPolicy(),
        account=AccountState(equity_quote=800, high_water_quote=1000),
        market=MarketState(spot_vs_twap_bps=200),
    )
    assert not v.ok and v.reason == "DRAWDOWN"


def test_policy_allows_exits_in_drawdown():
    v = check(
        is_buy=False, notional_quote=500, declared_edge_bps=0, min_out=99, fair_out=100,
        max_notional_quote=1e18, policy=RiskPolicy(),
        account=AccountState(equity_quote=800, high_water_quote=1000, position_qty=10),
        market=MarketState(spot_vs_twap_bps=10),
    )
    assert v.ok, v.detail


def test_policy_rejects_wide_slippage_and_small_edge():
    p = RiskPolicy()
    assert check(is_buy=True, notional_quote=10, declared_edge_bps=50, min_out=99, fair_out=100,
                 max_notional_quote=200, policy=p, account=AccountState(), market=MarketState()).reason == "EDGE_BELOW_HURDLE"
    assert check(is_buy=True, notional_quote=10, declared_edge_bps=200, min_out=90, fair_out=100,
                 max_notional_quote=200, policy=p, account=AccountState(),
                 market=MarketState(spot_vs_twap_bps=200)).reason == "SLIPPAGE_WINDOW"
    assert check(is_buy=True, notional_quote=10, declared_edge_bps=200, min_out=99, fair_out=100,
                 max_notional_quote=200, policy=p, account=AccountState(),
                 market=MarketState(spot_vs_twap_bps=900)).reason == "SPOT_DEVIATION"


def test_policy_rejects_a_claim_above_the_dislocation():
    v = check(is_buy=True, notional_quote=10, declared_edge_bps=200, min_out=99, fair_out=100,
              max_notional_quote=200, policy=RiskPolicy(), account=AccountState(),
              market=MarketState(spot_vs_twap_bps=120))
    assert v.reason == "EDGE_CLAIM_ABOVE_DISLOCATION"


def test_policy_rejects_an_entry_inside_the_cooldown():
    v = check(is_buy=True, notional_quote=10, declared_edge_bps=200, min_out=99, fair_out=100,
              max_notional_quote=200, policy=RiskPolicy(), account=AccountState(last_trade_ts=1_000),
              market=MarketState(spot_vs_twap_bps=200), now_ts=1_030)
    assert v.reason == "COOLDOWN"
    # ... and an exit is never held back by it
    v = check(is_buy=False, notional_quote=10, declared_edge_bps=0, min_out=99, fair_out=100,
              max_notional_quote=1e18, policy=RiskPolicy(), account=AccountState(last_trade_ts=1_000),
              market=MarketState(spot_vs_twap_bps=0), now_ts=1_030)
    assert v.ok, v.detail


def test_min_out_is_always_inside_the_contract_band():
    p = RiskPolicy()
    for quoted, fair in [(100, 100), (98, 100), (100.5, 100), (95, 100), (0, 100)]:
        m = build_min_out(int(quoted), int(fair), p.max_slippage_bps)
        assert in_slippage_band(m, int(fair), p.max_slippage_bps), (quoted, fair, m)


# --------------------------------------------------------------------------- ledger

def test_ledger_pnl_matches_vault_semantics():
    led = Ledger()
    led.open_with(1_000.0)
    led.buy(1, "WHSK", 100.0, 1_230_000_000_000_000_000_000, 0.0813, 0.3, 150, 0.0, "buy",
            decimals=18, leg_edge_bps=-32)
    eq = led.mark(2, {"WHSK": 0.0813})
    assert eq == pytest.approx(999.999, abs=1e-6)
    led.sell(3, "WHSK", 1_230_000_000_000_000_000_000, 105.0, 0.0854, 0.3, 0, 500.0, "sell", leg_edge_bps=-30)
    m = led.metrics()
    assert m["trades"] == 2 and m["wins"] == 1
    assert m["realised_pnl_quote"] == pytest.approx(5.0, abs=1e-9)
    assert m["fees_paid_quote"] == pytest.approx(0.6, abs=1e-9)
    # claims come from entries, outcomes from exits, per-leg execution from both
    assert m["declared_edge_sum_bps"] == pytest.approx(150)
    assert m["realised_edge_sum_bps"] == pytest.approx(500)
    assert m["avg_execution_edge_bps"] == pytest.approx(-31)
    assert m["edge_credibility_bps"] == pytest.approx(500 / 150 * 10_000, rel=1e-6)


def test_ledger_avg_price_is_per_whole_token():
    led = Ledger()
    led.open_with(100.0)
    led.buy(1, "WHSK", 100.0, 1_234_000_000_000_000_000_000, 0.081, 0.3, 150, 0.0, "buy", decimals=18)
    assert led.positions["WHSK"].avg_price == pytest.approx(100 / 1234, rel=1e-9)


def test_ledger_rejects_oversell():
    led = Ledger()
    led.open_with(100.0)
    with pytest.raises(ValueError):
        led.buy(1, "WHSK", 500.0, 1, 0.08, 0.1, 100, 0, "x")


def test_ledger_persistence(tmp_path):
    led = Ledger()
    led.open_with(500.0)
    led.buy(1, "WHSK", 50.0, 600_000_000_000_000_000_000, 0.083, 0.15, 100, 0, "buy", decimals=18)
    led.record_decision({"action": "HOLD", "reasons": ["x"]})
    p = tmp_path / "ledger.json"
    led.save(str(p))
    back = Ledger.load(str(p))
    assert back.quote_balance == pytest.approx(led.quote_balance)
    assert back.positions["WHSK"].qty == led.positions["WHSK"].qty
    assert back.decisions == led.decisions


def test_ledger_load_tolerates_older_state_files(tmp_path):
    """A state file written before a field existed must still open (a demo must not die on a schema bump)."""
    p = tmp_path / "old.json"
    p.write_text(json.dumps({
        "quote_balance": 10.0,
        "positions": {"WHSK": {"qty": 1, "cost_quote": 1.0, "decimals": 18}},
        "trades": [{"ts": 1, "side": "BUY", "asset": "WHSK", "amount_in": 1, "amount_out": 1,
                    "price": 1.0, "notional_quote": 1.0, "fee_quote": 0.0, "declared_edge_bps": 150,
                    "realised_edge_bps": 0.0, "reason": "x"}],
    }), encoding="utf-8")
    led = Ledger.load(str(p))
    assert led.trades[0].leg_edge_bps == 0.0


# --------------------------------------------------------------------------- market helpers

def test_tick_to_price_matches_chain_observation():
    # WHSK(18)/USDT(6) at tick -301487 is 0.0808 USDT per WHSK on mainnet (measured 2026-11-24).
    raw = tick_to_price(-301_487)
    human = raw * 1e18 / 1e6
    assert human == pytest.approx(0.0808, abs=1e-4)
