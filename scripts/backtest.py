"""Backtest over the pool's *recorded* swaps, plus labelled synthetic stress scenarios.

    python scripts/backtest.py --bars 40

Panel A (real) is the honest one: it walks actual HSKSwap trade bars and asks whether the signal would
have cleared its cost hurdle. Panels B and C inject a dislocation regime to prove the execution and
accounting machinery works end to end. The report never mixes the two.

Every decision goes through the same three modules the live loop uses — `signals.build_features` ->
`signals.decide` -> `policy.check` — and every decision (including the refusals) is written to the ledger,
so the "no-trade" counts in the output are counted decisions, not absences.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import MARKETS, RiskPolicy, Settings, Strategy  # noqa: E402
from agent.ledger import Ledger  # noqa: E402
from agent.market import Bar, DexClient  # noqa: E402
from agent.policy import AccountState, MarketState, build_min_out, check  # noqa: E402
from agent.rpc import Rpc  # noqa: E402
from agent.signals import build_features, decide  # noqa: E402

QUOTE_UNIT = Decimal(10) ** 6


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / len(values[-n:])


def _raw_base(quote_amount: float, price: float, decimals: int) -> int:
    """Base-token raw units bought with `quote_amount` at `price` (quote per whole base)."""
    if price <= 0:
        return 0
    return int(Decimal(str(quote_amount)) / Decimal(str(price)) * (Decimal(10) ** decimals))


def _raw_quote(base_raw: int, price: float, decimals: int) -> int:
    return int(Decimal(base_raw) / (Decimal(10) ** decimals) * Decimal(str(price)) * QUOTE_UNIT)


def run_series(
    closes: list[float],
    *,
    policy: RiskPolicy,
    strategy: Strategy,
    fee_bps: float,
    impact_bps: float,
    ts0: int = 1_760_000_000,
    bar_seconds: int = 600,
    market=MARKETS["WHSK"],
) -> tuple[Ledger, list[dict]]:
    led = Ledger()
    led.open_with(1_000.0)
    decimals = market.decimals
    size_cap = policy.max_trade_notional / 1e6 * strategy.target_position_frac
    # The measured impact already contains the pool fee (the quote is the real output), so the per-leg
    # haircut is the impact alone; adding the fee again would tax the backtest twice.
    taker_haircut = impact_bps / 10_000
    rows: list[dict] = []

    for i in range(20, len(closes)):
        price = closes[i]
        window = closes[max(0, i - 20) : i]
        twap = sma(window, 20)
        dev_bps = (price / twap - 1) * 10_000 if twap else 0.0
        bars = [
            Bar(t=i - len(window) + k, open=p, high=p, low=p, close=p, volume_quote=1.0, buys=1, sells=(k % 2))
            for k, p in enumerate(window)
        ]
        pos = led.positions.get(market.asset)
        pos_value = (pos.qty / 10**decimals * price) if pos else 0.0
        f = build_features(
            bars, twap_dev_bps=dev_bps, fee_bps=fee_bps, impact_bps_buy=impact_bps,
            impact_bps_sell=impact_bps, liquidity_quote=419_000.0, strategy=strategy,
        )
        d = decide(
            f, policy, strategy, has_position=bool(pos and pos.qty > 0), position_quote=pos_value,
            size_cap_quote=size_cap,
        )
        ts = ts0 + i * bar_seconds
        is_buy = d.action == "BUY"
        executed = False

        if d.is_trade:
            if is_buy:
                size = min(d.size_quote, led.quote_balance * 0.95)
                quoted_out = int(_raw_base(size, price, decimals) * (1 - taker_haircut))
                fair_out = _raw_base(size, twap, decimals)
            else:
                amount_in = pos.qty if pos else 0
                quoted_out = int(_raw_quote(amount_in, price, decimals) * (1 - taker_haircut))
                fair_out = _raw_quote(amount_in, twap, decimals)
                size = quoted_out / 1e6

            min_out = build_min_out(quoted_out, fair_out, policy.max_slippage_bps)
            acct = AccountState(
                equity_quote=led.quote_balance + pos_value,
                high_water_quote=max(led.high_water, led.quote_balance + pos_value),
                daily_notional_quote=sum(
                    t.notional_quote for t in led.trades if t.side == "BUY" and t.ts >= ts - 86_400
                ),
                last_trade_ts=led.trades[-1].ts if led.trades else 0,
                position_qty=pos.qty if pos else 0,
                position_quote_value=pos_value,
            )
            verdict = check(
                is_buy=is_buy,
                notional_quote=max(size, 0.0),
                declared_edge_bps=d.edge_bps if is_buy else 0,
                min_out=min_out,
                fair_out=fair_out,
                max_notional_quote=(policy.max_trade_notional / 1e6) if is_buy else 1e18,
                policy=policy,
                account=acct,
                market=MarketState(spot_vs_twap_bps=dev_bps, fair_out=fair_out, quoted_out=quoted_out),
                now_ts=ts,
            )
            if not verdict.ok:
                rows.append({"i": i, "price": price, "twap": twap, "dev_bps": round(dev_bps, 1),
                             "action": d.action, "edge_bps": d.edge_bps, "hurdle_bps": d.hurdle_bps,
                             "executed": False, "blocked_by": verdict.reason, "reasons": d.reasons})
                led.record_decision({"ts": ts, "action": "HOLD", "policy_reject": verdict.reason,
                                     "edge_bps": d.edge_bps, "hurdle_bps": d.hurdle_bps,
                                     "reasons": d.reasons + [verdict.detail]})
                led.mark(ts, {market.asset: price})
                continue

            leg_edge = ((quoted_out - fair_out) / fair_out * 10_000) if fair_out else 0.0
            if is_buy:
                led.buy(ts, market.asset, size, quoted_out, price, size * fee_bps / 10_000,
                        d.edge_bps, 0.0, d.signature_reason(), decimals=decimals, leg_edge_bps=leg_edge)
            else:
                basis = pos.cost_quote if pos else 0.0
                round_trip = ((quoted_out / 1e6 - basis) / basis * 10_000) if basis else 0.0
                led.sell(ts, market.asset, pos.qty, quoted_out / 1e6, price,
                         quoted_out / 1e6 * fee_bps / 10_000, 0, round_trip,
                         d.signature_reason(), leg_edge_bps=leg_edge)
            executed = True
            ledger_action = d.action
        else:
            ledger_action = "HOLD"

        eq = led.mark(ts, {market.asset: price})
        rows.append({
            "i": i, "price": price, "twap": twap, "dev_bps": round(dev_bps, 1), "action": d.action,
            "edge_bps": d.edge_bps, "hurdle_bps": d.hurdle_bps, "executed": executed,
            "blocked_by": "", "equity": round(eq, 6), "reasons": d.reasons,
        })
        led.record_decision({
            "ts": ts, "action": ledger_action, "edge_bps": d.edge_bps, "hurdle_bps": d.hurdle_bps,
            "executed": executed, "reasons": d.reasons, "equity": round(eq, 6),
        })

    return led, rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=40)
    ap.add_argument("--out", default="evidence/backtest.json")
    a = ap.parse_args()

    s = Settings.from_env()
    rpc = Rpc(s.chain.rpc_url, s.chain.chain_id)
    dex = DexClient(rpc, s.addresses)
    market = MARKETS["WHSK"]

    # The pool's swap rate changes day to day (it was ~10 swaps/3h when this was written, ~45 per
    # 40k blocks on 2026-11-25), so a fixed block window silently stops producing a backtest. Widen
    # the window until there are enough bars, and record which window the numbers came from.
    bars, lookback_used = [], 0
    for lookback in (40_000, 80_000, 160_000, 320_000):
        bars = dex.bars(market, n_bars=a.bars, swaps_per_bar=2, lookback_blocks=lookback)
        lookback_used = lookback
        if len(bars) >= 25:
            break
    closes = [b.close for b in bars]
    if len(closes) < 25:
        print(f"not enough recorded bars to backtest (best window {lookback_used:,} blocks -> {len(closes)} bars)")
        return 1
    print(f"bars={len(bars)} from a {lookback_used:,}-block window")

    # impact measured with the real quoter at the size the strategy would use, then the same figure applied
    # to both legs (entry and exit are the same size, so the cost is symmetric by construction)
    size = int(s.policy.max_trade_notional * s.strategy.target_position_frac)
    q = dex.quote_buy(market, size)
    state = dex.pool_state(market, s.policy.twap_window)
    fair = dex.fair_out(market, size, True, state.twap_quote_per_base)
    impact_bps = max(0.0, (fair - q.amount_out) / fair * 10_000) if fair else 0.0
    qs = dex.quote_sell(market, fair)
    fair_s = dex.fair_out(market, fair, False, state.twap_quote_per_base)
    impact_sell_bps = max(0.0, (fair_s - qs.amount_out) / fair_s * 10_000) if fair_s else 0.0

    real_led, real_rows = run_series(closes, policy=s.policy, strategy=s.strategy,
                                     fee_bps=market.fee / 100.0, impact_bps=impact_bps)

    # Synthetic panels. (1) is the regime the strategy is *for*: repeated dislocations that revert, sized so
    # the dislocation clears the full round-trip hurdle (a smaller dip would prove nothing, because the honest
    # answer for a small dip is "do not trade"). (2) is the regime that kills mean reversion: a steady slide.
    mean_rev: list[float] = []
    base = closes[-1]
    for i in range(28):
        base *= (1.0 - 0.042) if i % 4 == 0 else (1.0 + 0.0225 if i % 4 in (1, 2) else 1.0010)
        mean_rev.append(base)
    mean_rev = closes[-20:] + mean_rev

    downtrend: list[float] = []
    d = closes[-1]
    for i in range(44):
        d *= 0.994
        downtrend.append(d)
    downtrend = closes[-20:] + downtrend

    mr_led, mr_rows = run_series(mean_rev, policy=s.policy, strategy=s.strategy,
                                 fee_bps=market.fee / 100.0, impact_bps=impact_bps)
    dt_led, dt_rows = run_series(downtrend, policy=s.policy, strategy=s.strategy,
                                 fee_bps=market.fee / 100.0, impact_bps=impact_bps)

    blob = {
        "market": market.symbol,
        "pool": state.address,
        "bars": len(bars),
        "lookback_blocks": lookback_used,
        "block_span_bars": [bars[0].t, bars[-1].t],
        "impact_bps_at_size": round(impact_bps, 1),
        "impact_bps_exit": round(impact_sell_bps, 1),
        "fee_bps_per_leg": market.fee / 100.0,
        "full_round_trip_cost_bps": round(impact_bps + impact_sell_bps, 1),
        "size_quote": size / 1e6,
        "policy": s.policy.as_dict(),
        "real": {"metrics": real_led.metrics(), "rows": real_rows, "equity": real_led.equity_curve,
                 "closes": closes, "label": "REAL recorded HSKSwap bars"},
        "synthetic_mean_reversion": {"metrics": mr_led.metrics(), "rows": mr_rows, "equity": mr_led.equity_curve,
                                     "label": "SYNTHETIC mean-reversion"},
        "synthetic_downtrend": {"metrics": dt_led.metrics(), "rows": dt_rows, "equity": dt_led.equity_curve,
                                "label": "SYNTHETIC downtrend"},
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1)

    print(f"bars={len(bars)} impact_in={impact_bps:.1f}bp impact_out={impact_sell_bps:.1f}bp "
          f"round_trip={impact_bps + impact_sell_bps:.1f}bp (fee inside each quote) size={size/1e6:.0f} USDT")
    print("REAL      ", json.dumps(real_led.metrics(), ensure_ascii=False))
    print("MR SYNTH  ", json.dumps(mr_led.metrics(), ensure_ascii=False))
    print("DOWN SYNTH", json.dumps(dt_led.metrics(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
