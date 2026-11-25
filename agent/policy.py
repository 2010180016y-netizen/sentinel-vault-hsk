"""Off-chain mirror of SentinelVault's policy kernel.

Why duplicate the rules: a policy rejection on-chain costs gas and tells us nothing new. The agent
therefore evaluates the *same* predicates locally, in the same order, and refuses to build an intent
that would revert. The Vorlage is the Solidity contract; `test/fixtures/policy_scenarios.json` is a
single table read by BOTH implementations (`test/SentinelVault.t.sol::test_SharedPolicyTable` and
`tests/test_policy_mirror.py`), so the two cannot drift apart without one of the two test suites failing.

Two consumers read `evaluate()`:

  * `check()` — the decision path. It returns the first failing gate, which is the verdict the agent
    acts on (and the reason an intent is never signed).
  * `agent/lab.py` (the dashboard's scenario lab) — it shows the same gates, in the same order, with
    the thresholds and the actual numbers, so a judge can see *which* rule refused a trade instead of
    taking the agent's word for it. Because both read one function, the picture on screen cannot be
    prettier than the rule the agent obeys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import BPS, RiskPolicy

Reason = Literal[
    "EDGE_BELOW_HURDLE",
    "SPOT_DEVIATION",
    "EDGE_CLAIM_ABOVE_DISLOCATION",
    "FAIR_VALUE_ZERO",
    "MIN_OUT_ABOVE_FAIR",
    "SLIPPAGE_WINDOW",
    "TRADE_TOO_LARGE",
    "EXCEEDS_INTENT_CAP",
    "DAILY_LIMIT",
    "DRAWDOWN",
    "COOLDOWN",
    "ORACLE",
    "OK",
]


@dataclass
class AccountState:
    equity_quote: float = 0.0
    high_water_quote: float = 0.0
    daily_notional_quote: float = 0.0
    last_trade_ts: int = 0
    position_qty: int = 0
    position_quote_value: float = 0.0

    @property
    def drawdown_bps(self) -> float:
        if self.high_water_quote <= 0:
            return 0.0
        return max(0.0, (self.high_water_quote - self.equity_quote) / self.high_water_quote * BPS)


@dataclass
class MarketState:
    spot_vs_twap_bps: float = 0.0
    fair_out: int = 0
    quoted_out: int = 0
    oracle_ok: bool = True


@dataclass
class Gate:
    """One predicate, with the number that decided it.

    `code` is the contract-level reason name (the same string `Verdict.reason` uses); `limit` and
    `actual` are the two sides of the comparison, formatted for a human. `applies` is False for a
    gate that this trade type is exempt from (an exit is not judged by the entry hurdle), which is
    information too: it shows the exemption is a decision, not an omission.
    """

    code: str
    ok: bool
    detail: str = ""
    limit: str = ""
    actual: str = ""
    applies: bool = True
    rule: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "ok": self.ok,
            "detail": self.detail,
            "limit": self.limit,
            "actual": self.actual,
            "applies": self.applies,
            "rule": self.rule,
        }


@dataclass
class Verdict:
    ok: bool
    reason: Reason
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "notes": self.notes,
            "gates": [g.as_dict() for g in self.gates],
        }


def in_slippage_band(min_out: int, fair_out: int, max_slippage_bps: int) -> bool:
    """The vault requires minAmountOut in [fair*(1-slip), fair]."""
    if fair_out <= 0:
        return False
    floor = fair_out * (BPS - max_slippage_bps) // BPS
    return floor <= min_out <= fair_out


def evaluate(
    *,
    is_buy: bool,
    notional_quote: float,
    declared_edge_bps: float,
    min_out: int,
    fair_out: int,
    max_notional_quote: float,
    policy: RiskPolicy,
    account: AccountState,
    market: MarketState,
    now_ts: int = 0,
) -> list[Gate]:
    """Every predicate in the contract's order, stopping at the first failure.

    The list is *not* exhaustive after a failure on purpose: the contract reverts at the first failing
    predicate, so showing "pass" for a rule that would never have been reached would be a lie of a
    different kind. The trace therefore reports exactly what the EVM would have evaluated.
    """
    gates: list[Gate] = []
    dislocation = abs(market.spot_vs_twap_bps)

    def add(code: str, ok: bool, *, detail: str = "", limit: str = "", actual: str = "",
            applies: bool = True, rule: str = "") -> bool:
        gates.append(Gate(code, ok, detail, limit, actual, applies, rule))
        return ok

    # 0. oracle health — the vault fails closed when it cannot price the book
    if not add(
        "ORACLE", market.oracle_ok,
        detail="" if market.oracle_ok else "pool oracle cannot serve the TWAP window",
        rule="equity() reverts OracleUnavailable when any held asset cannot be priced",
    ):
        return gates

    # 1. the entry claim hurdle (the differentiator). Exits declare 0 on purpose.
    if is_buy:
        if not add(
            "EDGE_BELOW_HURDLE", declared_edge_bps >= policy.min_edge_bps,
            detail=f"{declared_edge_bps:.0f}bp < {policy.min_edge_bps}bp",
            limit=f"≥ {policy.min_edge_bps}bp", actual=f"{declared_edge_bps:.1f}bp",
            rule="isBuy && declaredEdgeBps < minEdgeBps → EdgeBelowHurdle",
        ):
            return gates
    else:
        add("EDGE_BELOW_HURDLE", True, detail="exit claims no edge", applies=False,
            rule="the hurdle gates entries only: refusing to reduce risk is a risk")

    # 2. spot manipulation breaker
    if not add(
        "SPOT_DEVIATION", dislocation <= policy.max_spot_twap_deviation_bps,
        detail=f"spot is {market.spot_vs_twap_bps:+.0f}bp from TWAP (cap {policy.max_spot_twap_deviation_bps})",
        limit=f"≤ {policy.max_spot_twap_deviation_bps}bp", actual=f"{dislocation:.1f}bp",
        rule="|spot − twap| > maxSpotTwapDeviationBps → PriceDeviated",
    ):
        return gates

    # 3. a claim cannot exceed the move the venue is showing
    if not add(
        "EDGE_CLAIM_ABOVE_DISLOCATION", declared_edge_bps <= dislocation,
        detail=f"claim {declared_edge_bps:.0f}bp > observed dislocation {dislocation:.0f}bp",
        limit=f"≤ {dislocation:.1f}bp (observed)", actual=f"{declared_edge_bps:.1f}bp",
        rule="declaredEdgeBps > |spot − twap| → EdgeClaimAboveDislocation",
    ):
        return gates

    # 4. the vault needs a fair value to bound the fill
    if not add(
        "FAIR_VALUE_ZERO", fair_out > 0,
        detail="TWAP fair value is zero" if fair_out <= 0 else "",
        limit="> 0", actual=f"{fair_out}",
        rule="fairOut == 0 → FairValueZero",
    ):
        return gates

    # 5. never demand a better-than-TWAP fill
    if not add(
        "MIN_OUT_ABOVE_FAIR", min_out <= fair_out,
        detail=f"minOut {min_out} > fair {fair_out}: demands better than TWAP",
        limit=f"≤ {fair_out}", actual=f"{min_out}",
        rule="minAmountOut > fairOut → MinOutAboveFair",
    ):
        return gates

    # 6. the floor must sit inside the slippage band around fair value
    floor = fair_out * (BPS - policy.max_slippage_bps) // BPS
    if not add(
        "SLIPPAGE_WINDOW", in_slippage_band(min_out, fair_out, policy.max_slippage_bps),
        detail=f"minOut {min_out} not inside [fair*(1-{policy.max_slippage_bps}bp), fair={fair_out}]",
        limit=f"[{floor}, {fair_out}]", actual=f"{min_out}",
        rule="minAmountOut < fairOut*(1−maxSlippageBps) → SlippageTooWide",
    ):
        return gates

    # 7. per-trade size cap (exits get maxSlices× headroom so a grown position can always leave)
    cap = policy.max_trade_notional / 1e6 * (1 if is_buy else policy.max_slices)
    if not add(
        "TRADE_TOO_LARGE", notional_quote <= cap,
        detail=f"{notional_quote:.2f} > {cap:.2f} (quote units)",
        limit=f"≤ {cap:.2f} USDT", actual=f"{notional_quote:.2f} USDT",
        rule="notionalQuote > maxTradeNotional (× maxSlices for an exit) → TradeTooLarge",
    ):
        return gates

    # 8. the intent's own ceiling
    if not add(
        "EXCEEDS_INTENT_CAP", notional_quote <= max_notional_quote,
        detail=f"{notional_quote:.2f} > intent cap {max_notional_quote:.2f}",
        limit=f"≤ {max_notional_quote:.2f}", actual=f"{notional_quote:.2f}",
        rule="notionalQuote > intent.maxNotionalQuote → ExceedsIntentCap",
    ):
        return gates

    # 9. rolling 24h budget (capital deployed, i.e. buys)
    if is_buy:
        daily_cap = policy.max_daily_notional / 1e6
        if not add(
            "DAILY_LIMIT", account.daily_notional_quote + notional_quote <= daily_cap,
            detail=f"daily {account.daily_notional_quote + notional_quote:.2f} > cap {daily_cap:.2f}",
            limit=f"≤ {daily_cap:.2f} USDT/24h",
            actual=f"{account.daily_notional_quote + notional_quote:.2f} USDT",
            rule="buys only: metering exits could block the trade that removes risk",
        ):
            return gates
    else:
        add("DAILY_LIMIT", True, detail="exits do not consume the daily budget", applies=False,
            rule="dailyNotional meters buys")

    # 10. drawdown: under stress only risk-reducing trades pass
    if is_buy:
        if not add(
            "DRAWDOWN", account.drawdown_bps <= policy.max_drawdown_bps,
            detail=f"drawdown {account.drawdown_bps:.0f}bp > {policy.max_drawdown_bps}bp; only exits allowed",
            limit=f"≤ {policy.max_drawdown_bps}bp", actual=f"{account.drawdown_bps:.0f}bp",
            rule="equity < highWater*(1−maxDrawdownBps) → DrawdownBreach on buys",
        ):
            return gates
    else:
        add("DRAWDOWN", True, detail="risk-reducing trade", applies=False,
            rule="a drawdown blocks buying, never selling")

    # 11. cooldown between new entries
    if is_buy:
        cooldown_ok = not (
            account.last_trade_ts and now_ts and now_ts < account.last_trade_ts + policy.cooldown_s
        )
        if not add(
            "COOLDOWN", cooldown_ok,
            detail=f"{max(0, now_ts - account.last_trade_ts)}s since the last entry, cooldown {policy.cooldown_s}s",
            limit=f"≥ {policy.cooldown_s}s since last entry",
            actual=f"{max(0, now_ts - account.last_trade_ts) if account.last_trade_ts else 'no prior entry'}s",
            rule="a standalone entry inside cooldown → CooldownActive",
        ):
            return gates
    else:
        add("COOLDOWN", True, detail="an exit is never delayed by the entry cooldown", applies=False,
            rule="cooldown meters how fast new capital is deployed")

    return gates


def check(
    *,
    is_buy: bool,
    notional_quote: float,
    declared_edge_bps: float,
    min_out: int,
    fair_out: int,
    max_notional_quote: float,
    policy: RiskPolicy,
    account: AccountState,
    market: MarketState,
    now_ts: int = 0,
) -> Verdict:
    """Same predicates, same order as SentinelVault (`_authorise` -> `_fairValueBand` -> `_enforceBudget`)."""
    notes: list[str] = []
    gates = evaluate(
        is_buy=is_buy,
        notional_quote=notional_quote,
        declared_edge_bps=declared_edge_bps,
        min_out=min_out,
        fair_out=fair_out,
        max_notional_quote=max_notional_quote,
        policy=policy,
        account=account,
        market=market,
        now_ts=now_ts,
    )
    failed = next((g for g in gates if not g.ok), None)
    if failed is not None:
        return Verdict(False, failed.code, failed.detail, notes, gates)  # type: ignore[arg-type]

    if not is_buy:
        notes.append("risk-reducing trade: notional, daily and cooldown gates do not apply")
    return Verdict(True, "OK", "all predicates satisfied", notes, gates)


def build_min_out(
    quoted_out: int, fair_out: int, max_slippage_bps: int, safety_bps: int = 20, headroom_bps: int = 5
) -> int:
    """Pick a floor that is *tighter* than the quote, so a worse-than-quoted fill is refused.

    Clamped into the vault's accepted window: never above fair (that would demand a better-than-TWAP
    fill) and never below fair*(1-max_slippage). The ceiling sits `headroom_bps` under fair: an exit
    into a premium quotes *above* TWAP, and pinning the floor to fair to the last unit turns a two-second
    TWAP drift between signing and execution into `MinOutAboveFair`. Five bps of headroom is still inside
    the band the contract enforces.
    """
    floor = fair_out * (BPS - max_slippage_bps) // BPS
    candidate = quoted_out - quoted_out * safety_bps // BPS
    ceiling = fair_out - max(1, fair_out * headroom_bps // BPS)
    return max(floor, min(candidate, ceiling))
