"""Trade guards adopted from Uniswap's own agent skills (`Uniswap/uniswap-ai`).

Why a second gate when the vault already has a policy kernel: the vault answers "may this *signed
intent* execute?" and nothing else. The Uniswap skills answer a different set of questions that a
swap agent gets wrong before it ever signs:

  * `swap-planner`  — is the pool deep enough that this size is not the price? (TVL < $100k is a
    warning, and price impact must be shown before the trade.)
  * `dca-bot`       — per-run and per-period spend caps, a token allowlist, a dry run, a kill switch;
    without all four the operator may not hand the bot "autonomous" mode.
  * `copy-trade`    — a mirror that cannot be sold back is a honeypot, so the exit leg must quote.
  * `swap-integration` — default 0.5% slippage, and a hard ceiling (5%) on what a user may set.

Those rules live off-chain on purpose. They are the agent's *self*-restraint and its refusal is
recorded like any other no-trade decision; the vault remains the only thing that can move funds.

`src/libraries/SwapGuardLib.sol` implements the same predicates in the same order for the same
reason the policy kernel and its Python mirror are kept honest: `test/fixtures/guard_scenarios.json`
is read by both, so the EVM implementation and this one cannot drift without a test failing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Iterable

BPS = 10_000

# Reason codes are strings here and `bytes32` in Solidity; the shared fixture compares them literally.
CODES = (
    "OK",
    "ADDRESS_INVALID",
    "AMOUNT_INVALID",
    "KILL_SWITCH",
    "TOKEN_NOT_ALLOWED",
    "SELL_SIDE_MISSING",
    "DEPTH_FLOOR",
    "DEPTH_SHARE",
    "PRICE_IMPACT",
    "SLIPPAGE_TOLERANCE",
    "PER_RUN_CAP",
    "PER_PERIOD_CAP",
    "QUOTE_STALE",
    "AUTONOMY_UNARMED",
)

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
AMOUNT_RE = re.compile(r"^[0-9]+\.?[0-9]*$")


def is_address(value: object) -> bool:
    return isinstance(value, str) and bool(ADDRESS_RE.match(value))


def is_zero_address(value: object) -> bool:
    return is_address(value) and value.lower() == "0x" + "0" * 40


def is_amount(value: object) -> bool:
    """Skill rule: amounts MUST be non-negative numeric values matching `^[0-9]+\\.?[0-9]*$`."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value >= 0
    return isinstance(value, str) and bool(AMOUNT_RE.match(value))


@dataclass(frozen=True)
class GuardLimits:
    """Operator-set guardrails. Defaults are the strictest reading of the Uniswap skills."""

    # `swap-integration`: default 0.5%, hard ceiling 5% on user-chosen slippage.
    max_slippage_bps: int = 500
    # `swap-planner`: show impact and refuse when it is not noise.
    max_price_impact_bps: int = 100
    # Never let one order be the pool: at most 20% of quoted depth on one side.
    max_depth_share_bps: int = 2_000
    # `swap-planner`: "Pool TVL < $100k" is a high-slippage warning; below that we do not trade.
    min_quote_depth_usd: int = 100_000
    # A quote older than this is not a quote.
    max_quote_age_s: int = 30
    # Per-run and per-period spend caps in quote units (human units, e.g. USDT).
    per_run_cap_quote: float | None = None
    per_period_cap_quote: float | None = None
    token_allowlist: frozenset[str] = field(default_factory=frozenset)
    kill_switch: bool = False
    # `dca-bot`/`copy-trade`: autonomous mode requires caps + allowlist + dry run + kill switch.
    mode: str = "confirm"  # "confirm" | "autonomous"
    dry_run_done: bool = False

    def normalised_allowlist(self) -> frozenset[str]:
        return frozenset(a.lower() for a in self.token_allowlist)

    def allows(self, token: str) -> bool:
        return is_address(token) and token.lower() in self.normalised_allowlist()


@dataclass
class GuardRequest:
    token_in: str
    token_out: str
    amount_in: int
    notional_quote: float
    quoted_out: int
    fair_out: int
    quote_depth_quote: float
    sell_quote: int
    quote_age_s: int
    slippage_bps: int
    period_spent_quote: float = 0.0
    is_buy: bool = True


@dataclass
class GuardVerdict:
    ok: bool
    code: str
    detail: str = ""
    checks: list[tuple[str, bool]] = field(default_factory=list)
    price_impact_bps: float = 0.0
    depth_share_bps: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
            "price_impact_bps": round(self.price_impact_bps, 1),
            "depth_share_bps": round(self.depth_share_bps, 1),
            "checks": [{"check": c, "pass": p} for c, p in self.checks],
        }


def price_impact_bps(fair_out: int, quoted_out: int) -> float:
    """How much worse than the pool's own TWAP the quote is, in bps. A better-than-TWAP quote is 0."""
    if fair_out <= 0 or quoted_out <= 0 or quoted_out >= fair_out:
        return 0.0
    return (fair_out - quoted_out) / fair_out * BPS


def depth_share_bps(notional_quote: float, depth_quote: float) -> float:
    if depth_quote <= 0:
        return float(BPS)  # an empty side is entirely consumed by any order
    return notional_quote / depth_quote * BPS


def preflight(req: GuardRequest, limits: GuardLimits) -> GuardVerdict:
    """Same predicate order as `SwapGuardLib.check`. First failure wins, and is named."""
    checks: list[tuple[str, bool]] = []

    def fail(code: str, detail: str) -> GuardVerdict:
        checks.append((code, False))
        return GuardVerdict(
            False,
            code,
            detail,
            checks,
            price_impact_bps=price_impact_bps(req.fair_out, req.quoted_out),
            depth_share_bps=depth_share_bps(req.notional_quote, req.quote_depth_quote),
        )

    def ok(check: str) -> None:
        checks.append((check, True))

    # 1. input validation — addresses and amounts are never interpolated unchecked into a call
    if (
        not (is_address(req.token_in) and is_address(req.token_out))
        or is_zero_address(req.token_in)
        or is_zero_address(req.token_out)
        or req.token_in.lower() == req.token_out.lower()
    ):
        return fail("ADDRESS_INVALID", f"tokenIn={req.token_in!r} tokenOut={req.token_out!r}")
    ok("addresses")
    if not is_amount(req.amount_in) or req.amount_in <= 0:
        return fail("AMOUNT_INVALID", f"amountIn={req.amount_in!r}")
    ok("amount")

    # 2. kill switch outranks everything: it is the operator's hand on the brake
    if limits.kill_switch:
        return fail("KILL_SWITCH", "operator kill switch is engaged")
    ok("kill_switch")

    # 3. token allowlist, both legs (`dca-bot`: no autonomous trading outside the list)
    if not (limits.allows(req.token_in) and limits.allows(req.token_out)):
        return fail("TOKEN_NOT_ALLOWED", f"{req.token_in} / {req.token_out} not both allowlisted")
    ok("allowlist")

    # 4. honeypot: a buy whose exit cannot quote is a position we cannot leave
    if req.is_buy and req.sell_quote <= 0:
        return fail("SELL_SIDE_MISSING", "the exit leg returned no quote")
    ok("sell_side")

    # 5. depth floor (skill: TVL < $100k is a high-slippage pool)
    if req.quote_depth_quote < limits.min_quote_depth_usd:
        return fail(
            "DEPTH_FLOOR", f"quote depth {req.quote_depth_quote:,.0f} < {limits.min_quote_depth_usd:,} USDT"
        )
    ok("depth_floor")

    # 6. size relative to depth
    share = depth_share_bps(req.notional_quote, req.quote_depth_quote)
    if share > limits.max_depth_share_bps:
        return fail("DEPTH_SHARE", f"order is {share:.0f}bp of depth (cap {limits.max_depth_share_bps}bp)")
    ok("depth_share")

    # 7. price impact vs the pool's own TWAP
    impact = price_impact_bps(req.fair_out, req.quoted_out)
    if impact > limits.max_price_impact_bps:
        return fail("PRICE_IMPACT", f"{impact:.0f}bp > {limits.max_price_impact_bps}bp")
    ok("price_impact")

    # 8. slippage tolerance ceiling
    if req.slippage_bps > limits.max_slippage_bps:
        return fail("SLIPPAGE_TOLERANCE", f"{req.slippage_bps}bp > ceiling {limits.max_slippage_bps}bp")
    ok("slippage")

    # 9-10. spend caps (entries only: an exit spends nothing)
    if req.is_buy:
        if limits.per_run_cap_quote is not None and req.notional_quote > limits.per_run_cap_quote:
            return fail("PER_RUN_CAP", f"{req.notional_quote:.2f} > {limits.per_run_cap_quote:.2f}")
        ok("per_run_cap")
        if (
            limits.per_period_cap_quote is not None
            and req.period_spent_quote + req.notional_quote > limits.per_period_cap_quote
        ):
            return fail(
                "PER_PERIOD_CAP",
                f"spent {req.period_spent_quote:.2f} + {req.notional_quote:.2f} > {limits.per_period_cap_quote:.2f}",
            )
        ok("per_period_cap")

    # 11. quote freshness
    if req.quote_age_s > limits.max_quote_age_s:
        return fail("QUOTE_STALE", f"quote is {req.quote_age_s}s old (max {limits.max_quote_age_s}s)")
    ok("quote_fresh")

    # 12. autonomous mode is an award for having every guardrail, not a default
    if limits.mode == "autonomous":
        armed = (
            limits.per_run_cap_quote is not None
            and limits.per_period_cap_quote is not None
            and bool(limits.token_allowlist)
            and limits.dry_run_done
        )
        if not armed:
            return fail("AUTONOMY_UNARMED", "caps, allowlist and a dry run are required before autonomous mode")
    ok("mode")

    return GuardVerdict(
        True,
        "OK",
        "all guards satisfied",
        checks,
        price_impact_bps=impact,
        depth_share_bps=share,
    )


@dataclass
class GuardState:
    """Spend accounting and the dry-run/kill-switch flags that `preflight` needs.

    Kept out of `GuardLimits` because limits are operator configuration and this is run state.
    """

    limits: GuardLimits
    spend_by_period: dict[str, float] = field(default_factory=dict)
    dry_run_done: bool = False
    kill_switch: bool = False
    refusals: list[dict] = field(default_factory=list)

    def limits_for(self, period: str) -> GuardLimits:
        return replace(
            self.limits,
            dry_run_done=self.dry_run_done,
            kill_switch=self.kill_switch,
            mode=self.limits.mode,
        )

    def spent(self, period: str) -> float:
        return self.spend_by_period.get(period, 0.0)

    def charge(self, period: str, notional_quote: float) -> None:
        self.spend_by_period[period] = self.spent(period) + notional_quote

    def engage_kill_switch(self, on: bool = True) -> None:
        self.kill_switch = on

    def mark_dry_run(self) -> None:
        self.dry_run_done = True

    def record_refusal(self, period: str, verdict: GuardVerdict, context: dict) -> None:
        self.refusals.append({"period": period, "code": verdict.code, "detail": verdict.detail, **context})

    @classmethod
    def for_market(
        cls,
        allowlist: Iterable[str],
        *,
        per_run_cap_quote: float | None = None,
        per_period_cap_quote: float | None = None,
        mode: str = "confirm",
    ) -> "GuardState":
        limits = GuardLimits(
            token_allowlist=frozenset(allowlist),
            per_run_cap_quote=per_run_cap_quote,
            per_period_cap_quote=per_period_cap_quote,
            mode=mode,
        )
        return cls(limits=limits)


def period_key(ts: int, cadence: str = "utc_day") -> str:
    """Idempotency/period key for the spend cap. Matches the DCA skill's 'current period' idea."""
    import time as _time

    t = _time.gmtime(ts)
    if cadence == "utc_day":
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
    if cadence == "utc_hour":
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}"
    raise ValueError(f"unsupported cadence {cadence!r}")


def load_fixture(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
