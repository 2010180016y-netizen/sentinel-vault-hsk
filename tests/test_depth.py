"""Cost is measured where it is paid: at the current spot, at the size actually traded.

The bug this file pins: the old cost model priced the exit leg against the TWAP, so a pool that was
400bp *cheap* looked like it charged 400bp to leave — the bot refused every dislocation it existed to
trade. And the size was fixed at the cap, so a thin pool looked infinitely expensive even when a
one-tenth order would have cleared. Both are numbers, so both are tested here.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import MARKETS, Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.ledger import Ledger, Position  # noqa: E402
from agent.market import Bar, PoolState, spot_impact_bps  # noqa: E402

SPOT = 0.0808
SCALE = 12.38e12  # raw WHSK per raw USDT at this price


class CurveDex:
    """A pool whose price impact grows linearly with size: impact_bps(s) = s / (2*depth) * 1e4."""

    def __init__(self, dev_bps: float = 0.0, depth_quote: float = 3_000.0) -> None:
        self.dev = dev_bps
        self.depth = depth_quote

    def pool_state(self, market, twap_window: int = 600):
        twap = SPOT
        spot = SPOT * (1 + self.dev / 10_000)
        return PoolState(
            address="0xcurve",
            token0=MARKETS["WHSK"].asset,
            token1="0xUSDT",
            fee=3000,
            sqrt_price_x96=1 << 96,
            tick=-301_487 + int(self.dev),
            liquidity=1,
            twap_tick=-301_487,
            twap_window=twap_window,
            spot_quote_per_base=spot,
            twap_quote_per_base=twap,
            base_is_token0=True,
        )

    def bars(self, market, n_bars: int = 30, swaps_per_bar: int = 2):
        return [Bar(t=i, open=SPOT, high=SPOT, low=SPOT, close=SPOT, volume_quote=10.0, buys=1, sells=1)
                for i in range(30)]

    def _spot(self) -> float:
        return SPOT * (1 + self.dev / 10_000)

    def quote_buy(self, market, amount_quote: int):
        usdt = amount_quote / 1e6
        effective = self._spot() * (1 + usdt / (2 * self.depth))

        class Q:
            amount_out = int(usdt / effective * 1e18)

        return Q()

    def quote_sell(self, market, amount_asset: int):
        whole = amount_asset / 1e18
        effective = self._spot() * (1 - whole * self._spot() / (2 * self.depth))

        class Q:
            amount_out = int(whole * effective * 1e6)

        return Q()

    def fair_out(self, market, amount_in: int, buy: bool, twap_quote_per_base: float) -> int:
        if buy:
            return int((amount_in / 1e6) / twap_quote_per_base * 1e18)
        return int((amount_in / 1e18) * twap_quote_per_base * 1e6)


def _agent(dex, ledger=None) -> SentinelAgent:
    ledger = ledger or Ledger()
    ledger.open_with(1_000.0)
    return SentinelAgent(Settings(), rpc=object(), dex=dex, ledger=ledger)


def test_spot_impact_does_not_charge_the_dislocation():
    # 400bp cheap: a quote 30bp worse than spot is 30bp of cost, not 430bp
    cheap_spot = SPOT * 0.96
    out = int((120 / 1e6 * 0 + 120) / cheap_spot * 1e18 * 0.997) if False else int(120 / cheap_spot * 1e18 * 0.997)
    assert spot_impact_bps(cheap_spot, 120 * 10**6, out, 18, is_buy=True) == pytest.approx(30, abs=1)
    # measured against the TWAP instead, the same fill would look ~400bp expensive — the old model
    fair = int(120 / SPOT * 1e18)
    assert abs((fair - out) / fair * 10_000) > 350


def test_the_fit_shrinks_the_order_until_the_cost_clears():
    agent = _agent(CurveDex(dev_bps=-400, depth_quote=3_000))
    obs = agent.observe()
    d = agent.decide(obs)
    assert d.action == "BUY", d.reasons
    assert 0 < d.size_quote < 120, "the cap size costs more than the edge; the order must shrink"
    assert d.hurdle_bps <= d.edge_bps + 1, "the hurdle is measured at the size that will be signed"
    assert any("re-measured" in r for r in d.reasons)


def test_no_size_clears_means_hold_with_the_measurement():
    agent = _agent(CurveDex(dev_bps=-400, depth_quote=300))
    d = agent.decide(agent.observe())
    assert d.action == "HOLD"
    assert any("not even 5 USDT clears" in r for r in d.reasons)


def test_a_deep_pool_keeps_the_strategy_size():
    agent = _agent(CurveDex(dev_bps=-400, depth_quote=419_000))
    d = agent.decide(agent.observe())
    assert d.action == "BUY"
    assert d.size_quote == pytest.approx(120 * 0.6 * d.conviction, rel=0.02)


def test_an_exit_is_never_shrunk_or_held_by_the_fit():
    led = Ledger()
    led.open_with(900.0)
    led.positions[MARKETS["WHSK"].asset] = Position(qty=int(100 / SPOT * 1e18), cost_quote=100.0, decimals=18)
    # a deep pool, so the exit's profit target (dev ≥ hurdle) is genuinely met; a thin pool would make
    # HOLD the economically correct answer and the test would be measuring the wrong thing
    agent = _agent(CurveDex(dev_bps=260, depth_quote=419_000), ledger=led)
    d = agent.decide(agent.observe())
    assert d.action == "SELL", d.reasons
    assert not any("re-measured" in r for r in d.reasons)


def test_the_guard_benchmark_is_spot_not_twap():
    """An exit into a dump must not look like a 10,000bp price impact."""
    led = Ledger()
    led.open_with(900.0)
    led.positions[MARKETS["WHSK"].asset] = Position(qty=int(100 / SPOT * 1e18), cost_quote=100.0, decimals=18)
    agent = _agent(CurveDex(dev_bps=-300, depth_quote=419_000), ledger=led)
    obs = agent.observe()
    req = agent._guard_request(obs, is_buy=False, notional_quote=100.0, quoted_out=obs.quote_sell,
                               fair_out=obs.fair_sell, ts=agent.last_observed_at)
    impact = (req.fair_out - req.quoted_out) / req.fair_out * 10_000 if req.fair_out else 0
    assert abs(impact) < 100, f"benchmark must be spot, got {impact:.0f}bp"
