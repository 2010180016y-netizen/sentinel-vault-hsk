"""Backtest over the pool's *recorded* swaps, plus a labelled synthetic stress scenario.

    python scripts/backtest.py --bars 40

Panel A (real) is the honest one: it walks actual HSKSwap trade bars and asks whether the signal
would have cleared its cost hurdle. Panel B (synthetic) injects a dislocation regime to prove the
execution/accounting machinery works end to end. The report never mixes the two.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import MARKETS, RiskPolicy, Settings, Strategy  # noqa: E402
from agent.ledger import Ledger  # noqa: E402
from agent.market import Bar, DexClient  # noqa: E402
from agent.rpc import Rpc  # noqa: E402
from agent.signals import build_features, decide  # noqa: E402


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / len(values[-n:])


def run_series(closes: list[float], *, policy: RiskPolicy, strategy: Strategy, fee_bps: float,
               impact_bps: float, ts0: int = 1_760_000_000, bar_seconds: int = 600) -> tuple[Ledger, list[dict]]:
    led = Ledger()
    led.open_with(1_000.0)
    decimals = MARKETS["WHSK"].decimals
    rows: list[dict] = []
    for i in range(20, len(closes)):
        price = closes[i]
        window = closes[max(0, i - 20) : i]
        twap = sma(window, 20)
        dev_bps = (price / twap - 1) * 10_000
        bars = [
            Bar(t=i - len(window) + k, open=p, high=p, low=p, close=p, volume_quote=1.0, buys=1, sells=(k % 2))
            for k, p in enumerate(window)
        ]
        pos = led.positions.get("WHSK")
        pos_value = (pos.qty / 10**decimals * price) if pos else 0.0
        f = build_features(
            bars, twap_dev_bps=dev_bps, fee_bps=fee_bps, impact_bps_buy=impact_bps,
            impact_bps_sell=impact_bps, liquidity_quote=400_000, strategy=strategy,
        )
        d = decide(f, policy, strategy, has_position=bool(pos and pos.qty > 0), position_quote=pos_value)
        ts = ts0 + i * bar_seconds
        executed = False
        size = min(d.size_quote, led.quote_balance * 0.95)
        if d.action == "BUY" and size >= 10 and d.edge_bps >= max(policy.min_edge_bps, f.round_trip_cost_bps + 20):
            qty = int(size / price * 10**decimals)
            # taker cost: half of the round-trip (fee + impact) is paid on the way in
            cost = size * (fee_bps + impact_bps / 2) / 10_000
            # a claim is only settled when the position is closed; nothing is 'realised' on entry
            led.buy(ts, "WHSK", size, qty, price, cost, d.edge_bps, 0.0, d.signature_reason(), decimals=decimals)
            executed = True
        elif d.action == "SELL" and pos and pos.qty > 0:
            out = pos.qty / 10**decimals * price
            cost = out * (fee_bps + impact_bps / 2) / 10_000
            basis = pos.cost_quote
            captured = (out - cost - basis) / basis * 10_000 if basis else 0.0
            led.sell(ts, "WHSK", pos.qty, out - cost, price, cost, pos.declared_edge_bps, captured,
                     d.signature_reason())
            executed = True
        eq = led.mark(ts, {"WHSK": price})
        rows.append({"i": i, "price": price, "twap": twap, "dev_bps": round(dev_bps, 1), "action": d.action,
                     "edge_bps": d.edge_bps, "hurdle_bps": d.hurdle_bps, "executed": executed,
                     "equity": round(eq, 6), "reasons": d.reasons})
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

    bars = dex.bars(market, n_bars=a.bars, swaps_per_bar=2)
    closes = [b.close for b in bars]
    if len(closes) < 25:
        print("not enough recorded bars to backtest")
        return 1

    # impact measured with the real quoter at the size the strategy would use
    from agent.config import LIQUID_MARKETS  # noqa: F401
    size = int(s.policy.max_trade_notional * s.strategy.target_position_frac)
    q = dex.quote_buy(market, size)
    state = dex.pool_state(market, s.policy.twap_window)
    fair = dex.fair_out(market, size, True, state.twap_quote_per_base)
    impact_bps = max(0.0, (fair - q.amount_out) / fair * 10_000) if fair else 0.0

    real_led, real_rows = run_series(closes, policy=s.policy, strategy=s.strategy,
                                     fee_bps=market.fee / 100.0, impact_bps=impact_bps)

    # Synthetic panels. (1) is the regime the strategy is *for*: repeated dislocations that revert.
    # (2) is the regime that kills mean reversion: a steady slide. The engine must trade (1) and refuse (2).
    mean_rev: list[float] = []
    base = closes[-1]
    for i in range(24):
        base *= (1.0 - 0.025) if i % 4 == 0 else (1.0 + 0.0142 if i % 4 in (1, 2) else 1.0005)
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
        "block_span_bars": [bars[0].t, bars[-1].t],
        "impact_bps_at_size": round(impact_bps, 1),
        "size_quote": size / 1e6,
        "real": {"metrics": real_led.metrics(), "rows": real_rows, "equity": real_led.equity_curve,
                 "closes": closes},
        "synthetic_mean_reversion": {"metrics": mr_led.metrics(), "rows": mr_rows, "equity": mr_led.equity_curve},
        "synthetic_downtrend": {"metrics": dt_led.metrics(), "rows": dt_rows, "equity": dt_led.equity_curve},
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1)

    print(f"bars={len(bars)} impact={impact_bps:.1f}bp size={size/1e6:.0f} USDT")
    print("REAL      ", json.dumps(real_led.metrics(), ensure_ascii=False))
    print("MR SYNTH  ", json.dumps(mr_led.metrics(), ensure_ascii=False))
    print("DOWN SYNTH", json.dumps(dt_led.metrics(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
