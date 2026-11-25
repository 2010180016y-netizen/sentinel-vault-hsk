"""Scenario lab: the policy kernel, made playable.

A judge (or the owner) can move one number — the pool's dislocation, the order size, the book's
drawdown, the kill switch — and see exactly which rule decides the outcome, with the threshold and the
actual value side by side. Nothing here writes to a chain, signs anything, or invents a price: the lab
feeds the *same* `signals.decide`, `policy.evaluate` and `guards.preflight` the live loop runs, and
reports their verdicts.

Three questions are answered separately, because conflating them is the usual way a trading demo lies:

  * **What did the strategy decide?** (`decision`) — the signal engine's judgement, including its
    refusals (noise band, adverse trend, edge below the cost hurdle).
  * **What would it sign?** (`order`) — the exact numbers the strategy's own order would carry, judged
    by the policy mirror and the Uniswap-skill guards.
  * **What if someone asked for a different size?** (`requested`) — the same evaluation at the size the
    slider (or a chat message) asked for. This is the answer to "your bot said no, but would the
    contract have let it?" and to "what happens if I ask for 400 USDT?".

The dashboard renders all three; `tests/test_lab.py` pins the boundaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import BPS, Addresses, RiskPolicy, Strategy
from .guards import GuardRequest, GuardState, period_key, preflight
from .policy import AccountState, MarketState, build_min_out, check
from .signals import Decision, Features, decide

# Default market geometry (measured on HSKSwap WHSK/USDT, docs/00_FACTS.md §7).
DEFAULT_PRICE = 0.0808
DEFAULT_DEPTH_QUOTE = 419_423.0
DEFAULT_FEE_BPS = 30.0
DEFAULT_IMPACT_BUY_BPS = 33.0
DEFAULT_IMPACT_SELL_BPS = 32.7


@dataclass
class Scenario:
    """One hypothetical moment. Every field is a number a human can move."""

    dev_bps: float = 0.0  # spot vs the pool's TWAP, signed
    size_quote: float = 120.0  # the size the strategy may use, and what a request asks for
    price: float = DEFAULT_PRICE  # quote per whole base token
    decimals: int = 18
    has_position: bool = False
    position_quote: float = 0.0
    equity: float = 1_000.0
    high_water: float = 1_000.0
    daily_spent: float = 0.0
    last_entry_age_s: int | None = None
    kill_switch: bool = False
    mode: str = "confirm"
    depth_quote: float = DEFAULT_DEPTH_QUOTE
    fee_bps: float = DEFAULT_FEE_BPS
    impact_buy_bps: float = DEFAULT_IMPACT_BUY_BPS
    impact_sell_bps: float = DEFAULT_IMPACT_SELL_BPS
    # Derived from `dev_bps` when left as None: a dislocation usually arrives with momentum, and a dump
    # lowers RSI. Explicit values let the lab show a pure reversion (flat trend) too.
    trend_bps: float | None = None
    vol_bps: float = 40.0
    rsi: float | None = None
    flow: float = 0.0
    bars: int = 40
    strategy_mode: str = "trend_filtered"  # trend_filtered | dislocation
    now_ts: int = 1_800_000_000

    def resolved(self) -> "Scenario":
        s = replace(self)
        if s.trend_bps is None:
            s.trend_bps = max(-1_500.0, min(1_500.0, s.dev_bps * 0.6))
        if s.rsi is None:
            s.rsi = max(5.0, min(95.0, 50.0 + s.dev_bps / 10.0))
        if s.has_position and s.position_quote <= 0:
            s.position_quote = s.size_quote
        return s


def _features(s: Scenario) -> Features:
    return Features(
        bars=s.bars,
        trend_bps=float(s.trend_bps or 0.0),
        rsi=float(s.rsi or 50.0),
        vol_bps=s.vol_bps,
        twap_dev_bps=s.dev_bps,
        flow_imbalance=s.flow,
        fee_bps=s.fee_bps,
        impact_bps_buy=s.impact_buy_bps,
        impact_bps_sell=s.impact_sell_bps,
        round_trip_cost_bps=2 * s.fee_bps + s.impact_buy_bps + s.impact_sell_bps,
        liquidity_quote=s.depth_quote,
    )


def _account(s: Scenario) -> AccountState:
    return AccountState(
        equity_quote=s.equity,
        high_water_quote=max(s.high_water, s.equity),
        daily_notional_quote=s.daily_spent,
        last_trade_ts=(s.now_ts - s.last_entry_age_s) if s.last_entry_age_s is not None else 0,
        position_qty=int(round(s.position_quote / s.price * (10**s.decimals))) if s.has_position else 0,
        position_quote_value=s.position_quote if s.has_position else 0.0,
    )


def plan_order(s: Scenario, *, is_buy: bool, notional_quote: float, declared_edge_bps: float) -> dict:
    """The numbers an intent would carry, derived the way `core.step` derives them."""
    if is_buy:
        amount_in = int(round(notional_quote * 1e6))
        gross = notional_quote / s.price * (10**s.decimals) if s.price > 0 else 0
        fair_out = int(gross)
        quoted_out = int(gross * (1 - (s.fee_bps + s.impact_buy_bps) / BPS))
        token_in, token_out = "USDT", "WHSK"
    else:
        amount_in = int(round(notional_quote / s.price * (10**s.decimals))) if s.price > 0 else 0
        fair_out = int(round(notional_quote * 1e6))
        quoted_out = int(round(notional_quote * 1e6 * (1 - (s.fee_bps + s.impact_sell_bps) / BPS)))
        token_in, token_out = "WHSK", "USDT"
    min_out = build_min_out(quoted_out, fair_out, RiskPolicy().max_slippage_bps) if fair_out else 0
    return {
        "side": "BUY" if is_buy else "SELL",
        "token_in": token_in,
        "token_out": token_out,
        "notional_quote": round(notional_quote, 6),
        "amount_in_raw": amount_in,
        "fair_out_raw": fair_out,
        "quoted_out_raw": quoted_out,
        "min_amount_out_raw": min_out,
        "declared_edge_bps": round(float(declared_edge_bps), 2),
    }


def judge_order(s: Scenario, plan: dict, policy: RiskPolicy, *, is_buy: bool) -> dict:
    """Run the policy mirror and the swap guards over one plan."""
    market = MarketState(
        spot_vs_twap_bps=s.dev_bps,
        fair_out=plan["fair_out_raw"],
        quoted_out=plan["quoted_out_raw"],
    )
    verdict = check(
        is_buy=is_buy,
        notional_quote=plan["notional_quote"],
        declared_edge_bps=plan["declared_edge_bps"],
        min_out=plan["min_amount_out_raw"],
        fair_out=plan["fair_out_raw"],
        max_notional_quote=policy.max_trade_notional / 1e6 if is_buy else 9.22e18,
        policy=policy,
        account=_account(s),
        market=market,
        now_ts=s.now_ts,
    )

    state = GuardState.for_market(
        [Addresses.quote, Addresses.whsk],
        per_run_cap_quote=policy.max_trade_notional / 1e6,
        per_period_cap_quote=policy.max_daily_notional / 1e6,
        mode=s.mode,
    )
    state.kill_switch = s.kill_switch
    period = period_key(s.now_ts)
    req = GuardRequest(
        token_in=Addresses.quote if is_buy else Addresses.whsk,
        token_out=Addresses.whsk if is_buy else Addresses.quote,
        amount_in=int(plan["amount_in_raw"]),
        notional_quote=plan["notional_quote"],
        quoted_out=int(plan["quoted_out_raw"]),
        fair_out=int(plan["fair_out_raw"]),
        quote_depth_quote=s.depth_quote,
        sell_quote=int(plan["quoted_out_raw"]) if is_buy else 1,
        quote_age_s=0,
        slippage_bps=policy.max_slippage_bps,
        period_spent_quote=s.daily_spent,
        is_buy=is_buy,
    )
    guards = preflight(req, state.limits_for(period)).as_dict()
    blocked = f"policy:{verdict.reason}" if not verdict.ok else ("" if guards["ok"] else f"guard:{guards['code']}")
    return {
        "plan": plan,
        "policy": {
            "ok": verdict.ok,
            "reason": verdict.reason,
            "detail": verdict.detail,
            "notes": verdict.notes,
            "gates": [g.as_dict() for g in verdict.gates],
        },
        "guards": guards,
        "allowed": verdict.ok and guards["ok"],
        "blocked_by": blocked,
    }


def run(
    scenario: Scenario | None = None,
    *,
    policy: RiskPolicy | None = None,
    strategy: Strategy | None = None,
) -> dict[str, Any]:
    """Evaluate one scenario through the real strategy, policy and guard stack."""
    base_strategy = strategy or Strategy()
    s = (scenario or Scenario()).resolved()
    policy = policy or RiskPolicy()
    strat = replace(base_strategy, name=f"sentinel/{s.strategy_mode}/v1", mode=s.strategy_mode)

    feats = _features(s)
    decision: Decision = decide(
        feats,
        policy,
        strat,
        has_position=s.has_position,
        position_quote=s.position_quote,
        size_cap_quote=s.size_quote,
    )

    order = None
    if decision.is_trade:
        is_buy = decision.action == "BUY"
        notional = decision.size_quote if is_buy else s.position_quote
        claim = decision.edge_bps if is_buy else 0.0
        order = judge_order(s, plan_order(s, is_buy=is_buy, notional_quote=notional, declared_edge_bps=claim), policy, is_buy=is_buy)
        order["side"] = decision.action

    # The counterfactual: an entry of the requested size, with the claim the strategy formula would make.
    req_buy = not decision.is_trade or decision.action == "BUY"
    req_notional = s.size_quote if req_buy else s.position_quote
    req_claim = min(abs(s.dev_bps) * 0.45, abs(s.dev_bps)) if req_buy else 0.0
    requested = judge_order(
        s, plan_order(s, is_buy=req_buy, notional_quote=req_notional, declared_edge_bps=req_claim), policy, is_buy=req_buy
    )
    requested["side"] = "BUY" if req_buy else "SELL"

    if not decision.is_trade:
        blocked = "strategy"
    elif order is not None and not order["allowed"]:
        blocked = order["blocked_by"]
    else:
        blocked = ""
    would_execute = bool(decision.is_trade and order is not None and order["allowed"])

    return {
        "inputs": {
            **asdict(s),
            "round_trip_cost_bps": round(feats.round_trip_cost_bps, 1),
            "hurdle_bps": decision.hurdle_bps,
        },
        "features": feats.as_dict(),
        "strategy": {"name": strat.name, "mode": strat.mode},
        "decision": {
            "action": decision.action,
            "is_trade": decision.is_trade,
            "conviction": decision.conviction,
            "edge_bps": decision.edge_bps,
            "size_quote": decision.size_quote,
            "hurdle_bps": decision.hurdle_bps,
            "dislocation_bps": decision.dislocation_bps,
            "reasons": decision.reasons,
        },
        "order": order,
        "requested": requested,
        "outcome": {
            "would_execute": would_execute,
            "blocked_by": blocked,
            "explain": _sentence(decision, order, requested),
        },
    }


def _sentence(decision: Decision, order: dict | None, requested: dict) -> str:
    head = (
        f"전략 {decision.action} {decision.size_quote:.2f} USDT "
        f"(엣지 {decision.edge_bps:.0f}bp / 허들 {decision.hurdle_bps:.0f}bp, 확신 {decision.conviction:.2f})"
    )
    if order is None:
        chain = "체인에 낼 주문이 없다"
    elif order["allowed"]:
        chain = "체인 정책·가드 통과"
    else:
        chain = f"체인 거부 — {order['blocked_by']} ({order['policy']['detail'] or order['guards']['detail']})"
    req = (
        f"요청 {requested['plan']['notional_quote']:.0f} USDT는 "
        + ("가능" if requested["allowed"] else f"불가 — {requested['blocked_by']}")
    )
    return f"{head} | {chain} | {req} | 근거: {'; '.join(decision.reasons[:2])}"


PRESETS: dict[str, dict] = {
    "quiet": {"label": "조용한 시장 (이탈 0bp)", "scenario": {"dev_bps": 0}},
    "dump": {"label": "−400bp 덤프 · 추세 필터", "scenario": {"dev_bps": -400}},
    "dump_taker": {
        "label": "−400bp 덤프 · 이탈 매수 전략",
        "scenario": {"dev_bps": -400, "strategy_mode": "dislocation"},
    },
    "pump_exit": {
        "label": "+260bp 반등 · 보유 중 (청산)",
        "scenario": {"dev_bps": 260, "has_position": True, "position_quote": 120},
    },
    "drawdown": {
        "label": "낙폭 20% · 매수 시도",
        "scenario": {"dev_bps": -400, "equity": 800, "high_water": 1_000, "strategy_mode": "dislocation"},
    },
    "oversize": {
        "label": "1회 400 USDT 요청",
        "scenario": {"dev_bps": -400, "size_quote": 400, "strategy_mode": "dislocation"},
    },
    "kill": {
        "label": "킬 스위치 ON",
        "scenario": {"dev_bps": -400, "kill_switch": True, "strategy_mode": "dislocation"},
    },
}


def presets() -> dict:
    return {key: {"label": spec["label"], "result": run(Scenario(**spec["scenario"]))} for key, spec in PRESETS.items()}
