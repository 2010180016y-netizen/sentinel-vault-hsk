"""Off-chain mirror of SentinelVault's policy kernel.

Why duplicate the rules: a policy rejection on-chain costs gas and tells us nothing new. The agent
therefore evaluates the *same* predicates locally, in the same order, and refuses to build an intent
that would revert. The Vorlage is the Solidity contract; `test/fixtures/policy_scenarios.json` is a
single table read by BOTH implementations (`test/SentinelVault.t.sol::test_SharedPolicyTable` and
`tests/test_policy_mirror.py`), so the two cannot drift apart without one of the two test suites failing.
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
class Verdict:
    ok: bool
    reason: Reason
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def in_slippage_band(min_out: int, fair_out: int, max_slippage_bps: int) -> bool:
    """The vault requires minAmountOut in [fair*(1-slip), fair]."""
    if fair_out <= 0:
        return False
    floor = fair_out * (BPS - max_slippage_bps) // BPS
    return floor <= min_out <= fair_out


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
    dislocation = abs(market.spot_vs_twap_bps)

    if not market.oracle_ok:
        return Verdict(False, "ORACLE", "pool oracle cannot serve the TWAP window")

    # Claim gates apply to entries. An exit declares no edge: refusing to reduce risk because an expected
    # move is small would turn a risk limit into a trap (mirror of the contract's `isBuy &&` guard).
    if is_buy and declared_edge_bps < policy.min_edge_bps:
        return Verdict(False, "EDGE_BELOW_HURDLE", f"{declared_edge_bps:.0f}bp < {policy.min_edge_bps}bp")

    if dislocation > policy.max_spot_twap_deviation_bps:
        return Verdict(
            False,
            "SPOT_DEVIATION",
            f"spot is {market.spot_vs_twap_bps:+.0f}bp from TWAP (cap {policy.max_spot_twap_deviation_bps})",
        )

    if declared_edge_bps > dislocation:
        return Verdict(
            False,
            "EDGE_CLAIM_ABOVE_DISLOCATION",
            f"claim {declared_edge_bps:.0f}bp > observed dislocation {dislocation:.0f}bp",
        )

    if fair_out <= 0:
        return Verdict(False, "FAIR_VALUE_ZERO", "TWAP fair value is zero")

    if min_out > fair_out:
        return Verdict(False, "MIN_OUT_ABOVE_FAIR", f"minOut {min_out} > fair {fair_out}: demands better than TWAP")

    if not in_slippage_band(min_out, fair_out, policy.max_slippage_bps):
        return Verdict(
            False,
            "SLIPPAGE_WINDOW",
            f"minOut {min_out} not inside [fair*(1-{policy.max_slippage_bps}bp), fair={fair_out}]",
        )

    cap = policy.max_trade_notional / 1e6 * (1 if is_buy else policy.max_slices)
    if notional_quote > cap:
        return Verdict(False, "TRADE_TOO_LARGE", f"{notional_quote:.2f} > {cap:.2f} (quote units)")

    if notional_quote > max_notional_quote:
        return Verdict(False, "EXCEEDS_INTENT_CAP", f"{notional_quote:.2f} > intent cap {max_notional_quote:.2f}")

    if is_buy and account.daily_notional_quote + notional_quote > policy.max_daily_notional / 1e6:
        return Verdict(
            False, "DAILY_LIMIT", f"daily {account.daily_notional_quote + notional_quote:.2f} > cap"
        )

    if is_buy and account.drawdown_bps > policy.max_drawdown_bps:
        return Verdict(
            False,
            "DRAWDOWN",
            f"drawdown {account.drawdown_bps:.0f}bp > {policy.max_drawdown_bps}bp; only exits allowed",
        )

    if is_buy and account.last_trade_ts and now_ts and now_ts < account.last_trade_ts + policy.cooldown_s:
        return Verdict(
            False,
            "COOLDOWN",
            f"{now_ts - account.last_trade_ts}s since the last entry, cooldown {policy.cooldown_s}s",
        )

    if not is_buy:
        notes.append("risk-reducing trade: notional, daily and cooldown gates do not apply")

    return Verdict(True, "OK", "all predicates satisfied", notes)


def build_min_out(quoted_out: int, fair_out: int, max_slippage_bps: int, safety_bps: int = 20) -> int:
    """Pick a floor that is *tighter* than the quote, so a worse-than-quoted fill is refused.

    Clamped into the vault's accepted window: never above fair (that would demand a better-than-TWAP
    fill) and never below fair*(1-max_slippage).
    """
    floor = fair_out * (BPS - max_slippage_bps) // BPS
    candidate = quoted_out - quoted_out * safety_bps // BPS
    return max(floor, min(candidate, fair_out))
