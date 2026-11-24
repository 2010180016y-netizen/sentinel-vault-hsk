"""Signal engine: turns pool facts into an explainable, cost-aware decision.

The engine is deliberately deterministic. An LLM may narrate the decision (see agent_core.explain),
but it never chooses the numbers: a language model cannot be made accountable for a bps claim,
and this project's whole premise is that claims must be checkable. Every decision carries the
features that produced it, so the same inputs always reproduce the same trade.

Cost model (corrected 2026-11-24): a taker pays the fee **and** the price impact twice — once getting
in, once getting out. The earlier model averaged the two impacts (2*fee + (impact_in + impact_out)/2),
which understated the hurdle by roughly one impact and therefore let the bot trade moves that could not
pay for themselves. The hurdle now uses the full round trip, and the claim is capped by the dislocation
the venue is actually showing, which is the same bound the vault enforces on-chain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .config import BPS, RiskPolicy, Strategy
from .market import Bar

Action = Literal["BUY", "SELL", "HOLD"]


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def stdev_bps(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    m = sum(returns) / len(returns)
    var = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var)


@dataclass
class Features:
    bars: int
    trend_bps: float
    rsi: float
    vol_bps: float
    twap_dev_bps: float
    flow_imbalance: float
    fee_bps: float
    impact_bps_buy: float
    impact_bps_sell: float
    round_trip_cost_bps: float
    liquidity_quote: float

    def as_dict(self) -> dict:
        return {
            "bars": self.bars,
            "trend_bps": round(self.trend_bps, 2),
            "rsi": round(self.rsi, 2),
            "vol_bps": round(self.vol_bps, 2),
            "twap_dev_bps": round(self.twap_dev_bps, 2),
            "flow_imbalance": round(self.flow_imbalance, 3),
            "fee_bps": round(self.fee_bps, 2),
            "impact_bps_buy": round(self.impact_bps_buy, 2),
            "impact_bps_sell": round(self.impact_bps_sell, 2),
            "round_trip_cost_bps": round(self.round_trip_cost_bps, 2),
            "liquidity_quote": round(self.liquidity_quote, 2),
        }


@dataclass
class Decision:
    action: Action
    conviction: float
    edge_bps: float
    size_quote: float
    reasons: list[str] = field(default_factory=list)
    features: Features | None = None
    hurdle_bps: float = 0.0
    dislocation_bps: float = 0.0

    @property
    def is_trade(self) -> bool:
        return self.action != "HOLD"

    def signature_reason(self) -> str:
        return self.reasons[0] if self.reasons else "no reason recorded"


def build_features(
    bars: list[Bar],
    *,
    twap_dev_bps: float,
    fee_bps: float,
    impact_bps_buy: float,
    impact_bps_sell: float,
    liquidity_quote: float,
    strategy: Strategy,
) -> Features:
    closes = [b.close for b in bars]
    fast = ema(closes[-max(strategy.ema_fast + 1, 2) :], strategy.ema_fast)
    slow = ema(closes[-max(strategy.ema_slow + 1, 2) :], strategy.ema_slow)
    trend_bps = (fast / slow - 1) * BPS if slow else 0.0
    rets = [b.ret_bps for b in bars[1:]]
    buys = sum(b.buys for b in bars[-20:])
    sells = sum(b.sells for b in bars[-20:])
    flow = (buys - sells) / (buys + sells) if (buys + sells) else 0.0
    return Features(
        bars=len(bars),
        trend_bps=trend_bps,
        rsi=rsi(closes, strategy.rsi_period),
        vol_bps=stdev_bps(rets),
        twap_dev_bps=twap_dev_bps,
        flow_imbalance=flow,
        fee_bps=fee_bps,
        impact_bps_buy=impact_bps_buy,
        impact_bps_sell=impact_bps_sell,
        # A taker pays fee + impact on the way in AND on the way out. Both impacts are counted.
        round_trip_cost_bps=2 * fee_bps + impact_bps_buy + impact_bps_sell,
        liquidity_quote=liquidity_quote,
    )


def decide(
    f: Features,
    policy: RiskPolicy,
    strategy: Strategy,
    *,
    has_position: bool,
    position_quote: float,
    size_cap_quote: float | None = None,
) -> Decision:
    """Mean reversion against the pool's own TWAP, confirmed by trend/flow, gated by the cost hurdle."""
    hurdle = max(float(policy.min_edge_bps), f.round_trip_cost_bps + 20.0)  # cost + a margin to be worth it

    # Edge is the *expected* favourable move, discounted heavily: we never claim the full dislocation. It is
    # then capped at the dislocation itself, because a claim about a move larger than the move on offer is a
    # claim the chain can (and does) refuse: SentinelVault reverts `EdgeClaimAboveDislocation`.
    reversion = abs(f.twap_dev_bps) * 0.45
    continuation = abs(f.trend_bps) * 0.35 if (f.trend_bps * -f.twap_dev_bps) > 0 else 0.0
    edge = min(reversion + continuation, abs(f.twap_dev_bps))

    conviction = 0.0
    conviction += min(abs(f.twap_dev_bps) / 400.0, 1.0) * 0.5
    conviction += min(abs(f.trend_bps) / 150.0, 1.0) * 0.2
    conviction += min(abs(f.flow_imbalance), 1.0) * 0.15
    conviction += 0.15 if f.bars >= 20 else 0.0

    # Risk-adjusted sanity: a dislocation smaller than 1.5 bar-vols is noise, not signal.
    noisy = f.vol_bps > 0 and abs(f.twap_dev_bps) < 1.5 * f.vol_bps
    # A mean-reversion entry into a strong adverse trend is the classic way this strategy dies, so it
    # is blocked explicitly rather than by hoping conviction falls short.
    adverse_trend = f.trend_bps < -max(25.0, 1.5 * f.vol_bps)

    reasons: list[str] = []
    action: Action = "HOLD"

    wants_buy = f.twap_dev_bps <= -hurdle and f.rsi <= strategy.rsi_overbought
    wants_sell = has_position and (f.twap_dev_bps >= hurdle or f.rsi >= strategy.rsi_overbought)

    if adverse_trend:
        reasons.append(f"adverse trend {f.trend_bps:+.0f}bp vs bar vol {f.vol_bps:.0f}bp: mean reversion disarmed")
    if noisy:
        reasons.append(f"dislocation {f.twap_dev_bps:+.0f}bp is inside noise band (1.5x bar vol {f.vol_bps:.0f}bp)")
    if conviction < strategy.min_conviction:
        reasons.append(f"conviction {conviction:.2f} below {strategy.min_conviction}")
    if edge < hurdle:
        reasons.append(f"edge {edge:.0f}bp below hurdle {hurdle:.0f}bp (cost {f.round_trip_cost_bps:.0f}bp)")

    if wants_buy and not noisy and not adverse_trend and conviction >= strategy.min_conviction and edge >= hurdle:
        action = "BUY"
        reasons.insert(
            0,
            f"spot {abs(f.twap_dev_bps):.0f}bp below TWAP, reversion edge {edge:.0f}bp > hurdle {hurdle:.0f}bp",
        )
    elif wants_sell:
        # Exits are allowed on a weaker case than entries: refusing to reduce risk is itself a risk. An exit
        # claims no edge on-chain (declaredEdgeBps = 0), so the hurdle does not apply to it.
        action = "SELL"
        why = "spot above TWAP" if f.twap_dev_bps >= hurdle else "RSI overbought"
        reasons.insert(0, f"{why} ({f.twap_dev_bps:+.0f}bp, RSI {f.rsi:.0f}) -> take profit")

    if action == "HOLD" and not reasons:
        reasons.append("no dislocation beyond the cost hurdle; holding is the priced decision")

    size_quote = 0.0
    if action == "BUY":
        cap = size_cap_quote if size_cap_quote is not None else policy.max_trade_notional / 1e6
        size_quote = min(cap, policy.max_trade_notional / 1e6) * strategy.target_position_frac * conviction
    elif action == "SELL":
        size_quote = position_quote

    return Decision(
        action=action,
        conviction=round(conviction, 4),
        edge_bps=round(edge, 2),
        size_quote=round(size_quote, 6),
        reasons=reasons,
        features=f,
        hurdle_bps=round(hurdle, 2),
        dislocation_bps=round(abs(f.twap_dev_bps), 2),
    )
