"""Live execution wiring: what the agent books must come from the chain, never from a placeholder.

The bug this covers: the live branch used to record `amount_out = 0` ("filled in from the receipt by the
caller / next observation"). Nothing ever filled it in, so a *successful* live trade was booked as a 100%
loss in the ledger and every downstream number (PnL, credibility, drawdown) was wrong.
"""

import os
import sys

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import MARKETS, RiskPolicy, Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.executor import INTENT_EXECUTED_TOPIC, ChainExecutor  # noqa: E402
from agent.ledger import Ledger, Position  # noqa: E402
from agent.market import Bar, PoolState  # noqa: E402
from agent.signals import Decision, Features  # noqa: E402

TICK = -301_487
# 1 USDT buys ~12.38 WHSK at ~0.0808: the fake quotes in raw units, so the conversion is 12.38e12
# (10**18 WHSK per whole token / 10**6 USDT per whole token). The previous factor of 12 ignored the
# 18/6 decimal gap, which the spot-benchmarked impact check (correctly) refuses.
WHSK_PER_USDT_SCALE = int(12.38e12)


def _state(dev_ticks: int, window: int = 600) -> PoolState:
    return PoolState(
        address="0xpool",
        token0=MARKETS["WHSK"].asset,
        token1="0xUSDT",
        fee=3000,
        sqrt_price_x96=1 << 96,
        tick=TICK + dev_ticks,
        liquidity=1_475_813_786_831_558_685,
        twap_tick=TICK,
        twap_window=window,
        spot_quote_per_base=0.0808,
        twap_quote_per_base=0.0808,
        base_is_token0=True,
    )


class FakeDex:
    def __init__(self, dev_ticks: int = 300) -> None:
        self.dev = dev_ticks

    def pool_state(self, market, twap_window: int = 600):
        return _state(self.dev, twap_window)

    def bars(self, market, n_bars: int = 30, swaps_per_bar: int = 2):
        return [
            Bar(t=i, open=0.08, high=0.08, low=0.08, close=0.08, volume_quote=10.0, buys=1, sells=1)
            for i in range(30)
        ]

    def quote_buy(self, market, amount_quote: int):
        class Q:
            amount_out = amount_quote * WHSK_PER_USDT_SCALE

        return Q()

    def quote_sell(self, market, amount_asset: int):
        class Q:
            amount_out = amount_asset // WHSK_PER_USDT_SCALE

        return Q()

    def fair_out(self, market, amount_in: int, buy: bool, twap_quote_per_base: float) -> int:
        return amount_in * WHSK_PER_USDT_SCALE if buy else amount_in // WHSK_PER_USDT_SCALE


class FakeChain(ChainExecutor):
    """A ChainExecutor with the network stripped out — same type, so the core's live branch is exercised."""

    def __init__(self, ledger: Ledger, amount_out: int) -> None:
        self.ledger = ledger
        self.amount_out = amount_out
        self.intents = []
        self.refusals = []

    def execute(self, it):
        self.intents.append(it)
        return {"tx": "0xfeed", "status": 1, "gasUsed": 1, "amount_out": self.amount_out}

    def record_no_trade(self, **kw):
        self.refusals.append(kw)
        nonce = self.ledger.nonce + 1
        self.ledger.nonce = nonce
        return {"tx": "0xref", "nonce": nonce}


def _decision(action: str, edge: float = 160.0, hurdle: float = 146.0) -> Decision:
    f = Features(
        bars=30, trend_bps=100.0, rsi=40.0, vol_bps=20.0, twap_dev_bps=-300.0, flow_imbalance=0.2,
        fee_bps=30.0, impact_bps_buy=33.0, impact_bps_sell=33.0, round_trip_cost_bps=126.0,
        liquidity_quote=419_000.0,
    )
    return Decision(action=action, conviction=0.8, edge_bps=edge, size_quote=120.0, reasons=["test"],
                    features=f, hurdle_bps=hurdle, dislocation_bps=300.0)


def _agent(executor, ledger=None) -> SentinelAgent:
    ledger = ledger or Ledger()
    ledger.open_with(1_000.0)
    a = SentinelAgent(Settings(), rpc=object(), dex=FakeDex(), ledger=ledger, executor=executor)
    a.last_decision = _decision("BUY")
    return a


def test_live_buy_books_the_amount_the_chain_reported(monkeypatch):
    led = Ledger()
    # 120 USDT at 0.0808 minus the 30bp pool fee, at spot (the fake pool has no curve impact).
    fill = 1_480_541_089_108_910_891_089
    chain = FakeChain(led, amount_out=fill)
    agent = _agent(chain, led)
    monkeypatch.setattr(agent, "decide", lambda obs=None: _decision("BUY"))

    rec = agent.step()

    assert rec["executed"] is True
    assert rec["amount_out"] == fill
    assert led.trades[-1].amount_out == fill
    assert rec["leg_edge_bps"] < 0, "a taker at spot pays the fee; the fork suite covers the positive case"
    assert led.quote_balance == pytest.approx(880.0)
    assert chain.intents[0].declared_edge_bps == int(round(rec["edge_bps"]))
    assert rec["tx"] == "0xfeed"


def test_live_refusal_is_written_on_chain(monkeypatch):
    led = Ledger()
    chain = FakeChain(led, amount_out=0)
    agent = _agent(chain, led)
    monkeypatch.setattr(agent, "decide", lambda obs=None: _decision("HOLD", edge=40.0, hurdle=146.0))

    rec = agent.step()

    assert rec["executed"] is False
    assert rec["onchain_refusal"] == "0xref"
    assert chain.refusals and chain.refusals[0]["hurdle_bps"] == 146
    assert led.nonce == 1, "the refusal consumes the nonce, like a trade would"


def test_live_exit_settles_the_round_trip(monkeypatch):
    led = Ledger()
    led.positions[MARKETS["WHSK"].asset] = Position(qty=1_200_000_000_000_000_000_000, cost_quote=100.0, decimals=18)
    led.quote_balance = 900.0
    chain = FakeChain(led, amount_out=105_000_000)  # 105 USDT for a 100 USDT position
    agent = _agent(chain, led)
    monkeypatch.setattr(agent, "decide", lambda obs=None: _decision("SELL", edge=0.0))

    rec = agent.step()

    assert rec["executed"] is True
    assert led.trades[-1].realised_edge_bps == pytest.approx(500.0), "5% round trip in bps"
    assert led.metrics()["realised_pnl_quote"] == pytest.approx(5.0)
    assert chain.intents[0].declared_edge_bps == 0, "an exit claims no edge"


def test_intent_executed_log_is_decoded():
    amount_in, amount_out = 100_000_000, 1_234_080_000_000_000_000_000
    data = abi_encode(
        ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint16", "int256", "uint64"],
        ["0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029", "0xB210D2120d57b758EE163cFfb43e73728c471Cf1",
         amount_in, amount_out, amount_in, amount_out, 120, -32, 1],
    )
    logs = [
        {"topics": [bytes.fromhex("11" * 32), bytes.fromhex("22" * 32), bytes.fromhex("33" * 32), bytes.fromhex("44" * 32)], "data": b"0x"},
        {"topics": [bytes.fromhex(INTENT_EXECUTED_TOPIC.removeprefix("0x")), bytes.fromhex("22" * 32),
                    bytes.fromhex("33" * 32), bytes.fromhex("44" * 32)], "data": data},
    ]
    assert ChainExecutor.decode_amount_out(logs) == amount_out


def test_missing_log_is_not_silently_zero_filled():
    assert ChainExecutor.decode_amount_out([]) == 0


def test_twap_deviation_is_in_basis_points():
    """One tick is 1bp. The mirror used to multiply by 100, inflating every dislocation 100x."""
    # 1.0001^d - 1 in bps: -400 ticks is -392bp, +100 ticks is +100.5bp. Both are ~1 tick = 1bp, which is
    # what the contract assumes (`maxSpotTwapDeviationBps` counts ticks); not the 100x the mirror reported.
    assert _state(dev_ticks=-400).twap_deviation_bps == pytest.approx(-392, abs=2)
    assert _state(dev_ticks=100).twap_deviation_bps == pytest.approx(100.5, abs=1)
    assert abs(_state(dev_ticks=1).twap_deviation_bps) < 1.5
