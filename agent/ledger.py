"""Trade ledger and performance accounting.

Kept deliberately parallel to the vault's own book-keeping (cost basis, fees in quote units, high
water mark, win count) so the paper run and the on-chain run can be compared line for line. The three
edge numbers mirror the vault's three exactly:

  * `declared_edge_sum_bps` — claims registered on entries            (SentinelVault.declaredEdgeSumBps)
  * `realised_edge_sum_bps` — round-trip outcome, settled on exits    (SentinelVault.realizedEdgeSumBps)
  * `execution_edge_sum_bps` — per-leg result vs TWAP                 (SentinelVault.executionEdgeSumBps)
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Position:
    qty: int = 0  # raw asset units
    cost_quote: float = 0.0
    decimals: int = 18
    declared_edge_bps: float = 0.0

    @property
    def avg_price(self) -> float:
        """Quote units per whole token (not per raw unit — that was off by 10**decimals)."""
        whole = self.qty / (10 ** self.decimals)
        return self.cost_quote / whole if whole else 0.0


@dataclass
class Trade:
    ts: int
    side: str  # BUY / SELL
    asset: str
    amount_in: int
    amount_out: int
    price: float
    notional_quote: float
    fee_quote: float
    declared_edge_bps: float
    realised_edge_bps: float  # round-trip, bps of cost basis (0 until the position is closed)
    reason: str
    tx: str = ""
    equity_after: float = 0.0
    leg_edge_bps: float = 0.0  # this leg against the pool TWAP (negative = paid to take liquidity)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ledger:
    quote_symbol: str = "USDT"
    quote_balance: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    high_water: float = 0.0
    realised_pnl: float = 0.0
    fees_paid: float = 0.0
    win_count: int = 0
    decisions: list[dict] = field(default_factory=list)  # includes NO-TRADE decisions and adjustments
    nonce: int = 0

    # ---------------- mutations ----------------

    def open_with(self, quote: float) -> None:
        self.quote_balance = quote
        self.high_water = quote

    def mark(self, ts: int, price_by_asset: dict[str, float]) -> float:
        eq = self.quote_balance + sum(
            p.qty * price_by_asset.get(a, 0.0) / (10 ** p.decimals) for a, p in self.positions.items()
        )
        self.equity_curve.append((ts, round(eq, 6)))
        self.high_water = max(self.high_water, eq)
        return eq

    def buy(self, ts: int, asset: str, amount_in_quote: float, amount_out_raw: int, price: float,
            fee_quote: float, declared_edge_bps: float, realised_edge_bps: float, reason: str, tx: str = "",
            decimals: int = 18, leg_edge_bps: float = 0.0) -> Trade:
        if amount_in_quote > self.quote_balance + 1e-9:
            raise ValueError("insufficient quote balance")
        self.quote_balance -= amount_in_quote
        pos = self.positions.setdefault(asset, Position(decimals=decimals))
        pos.qty += amount_out_raw
        pos.cost_quote += amount_in_quote
        pos.declared_edge_bps = declared_edge_bps
        self.fees_paid += fee_quote
        t = Trade(ts, "BUY", asset, int(round(amount_in_quote * 1e6)), amount_out_raw, price,
                  amount_in_quote, fee_quote, declared_edge_bps, realised_edge_bps, reason, tx,
                  leg_edge_bps=leg_edge_bps)
        self.trades.append(t)
        return t

    def sell(self, ts: int, asset: str, amount_in_raw: int, amount_out_quote: float, price: float,
             fee_quote: float, declared_edge_bps: float, realised_edge_bps: float, reason: str, tx: str = "",
             leg_edge_bps: float = 0.0) -> Trade:
        pos = self.positions[asset]
        if amount_in_raw <= 0 or pos.qty < amount_in_raw:
            raise ValueError("insufficient position")
        cost_portion = pos.cost_quote * amount_in_raw / pos.qty
        pnl = amount_out_quote - cost_portion
        self.realised_pnl += pnl
        if pnl > 0:
            self.win_count += 1
        pos.qty -= amount_in_raw
        pos.cost_quote -= cost_portion
        self.quote_balance += amount_out_quote
        self.fees_paid += fee_quote
        t = Trade(ts, "SELL", asset, amount_in_raw, int(round(amount_out_quote * 1e6)), price,
                  amount_out_quote, fee_quote, declared_edge_bps, realised_edge_bps, reason, tx,
                  leg_edge_bps=leg_edge_bps)
        self.trades.append(t)
        return t

    def record_decision(self, record: dict) -> None:
        self.decisions.append(record)

    # ---------------- metrics ----------------

    def metrics(self) -> dict[str, Any]:
        equity = self.equity_curve[-1][1] if self.equity_curve else self.high_water
        peak, max_dd = 0.0, 0.0
        for _, e in self.equity_curve:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak * 10_000)
        rets = [
            (self.equity_curve[i][1] - self.equity_curve[i - 1][1]) / self.equity_curve[i - 1][1]
            for i in range(1, len(self.equity_curve))
            if self.equity_curve[i - 1][1] > 0
        ]
        sharpe = (statistics.mean(rets) / statistics.pstdev(rets) * (len(rets) ** 0.5)) if len(rets) > 2 and statistics.pstdev(rets) > 0 else 0.0
        # Claims are registered on entries, so only buys contribute to the denominator.
        declared = sum(t.declared_edge_bps for t in self.trades if t.side == "BUY")
        realised = sum(t.realised_edge_bps for t in self.trades if t.side == "SELL")
        execution = [t.leg_edge_bps for t in self.trades]
        return {
            "trades": len(self.trades),
            "entries": sum(1 for t in self.trades if t.side == "BUY"),
            "wins": self.win_count,
            "hit_rate": (self.win_count / len(self.trades)) if self.trades else 0.0,
            "realised_pnl_quote": round(self.realised_pnl, 6),
            "fees_paid_quote": round(self.fees_paid, 6),
            "equity": round(equity, 6),
            "high_water": round(self.high_water, 6),
            "max_drawdown_bps": round(max_dd, 1),
            "sharpe": round(sharpe, 3),
            "declared_edge_sum_bps": round(declared, 1),
            "realised_edge_sum_bps": round(realised, 1),
            "edge_credibility_bps": round((realised / declared * 10_000) if declared else 0.0, 1),
            "execution_edge_sum_bps": round(sum(execution), 1),
            "avg_execution_edge_bps": round(sum(execution) / len(execution), 1) if execution else 0.0,
            "no_trade_decisions": sum(1 for d in self.decisions if d.get("action") == "HOLD"),
            "adjustments": sum(1 for d in self.decisions if d.get("action") == "ADJUST"),
        }

    # ---------------- persistence ----------------

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        blob = {
            "quote_symbol": self.quote_symbol,
            "quote_balance": self.quote_balance,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "trades": [t.as_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "high_water": self.high_water,
            "realised_pnl": self.realised_pnl,
            "fees_paid": self.fees_paid,
            "win_count": self.win_count,
            "decisions": self.decisions,
            "nonce": self.nonce,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: str) -> "Ledger":
        """Load a saved ledger. Fields added after a file was written fall back to their defaults, so an
        older state file still opens (a demo must not die because a dataclass gained a field)."""
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        led = cls(quote_symbol=blob.get("quote_symbol", "USDT"))
        led.quote_balance = blob.get("quote_balance", 0.0)
        led.positions = {k: _dataclass_from(Position, v) for k, v in blob.get("positions", {}).items()}
        led.trades = [_dataclass_from(Trade, t) for t in blob.get("trades", [])]
        led.equity_curve = [tuple(x) for x in blob.get("equity_curve", [])]
        led.high_water = blob.get("high_water", 0.0)
        led.realised_pnl = blob.get("realised_pnl", 0.0)
        led.fees_paid = blob.get("fees_paid", 0.0)
        led.win_count = blob.get("win_count", 0)
        led.decisions = blob.get("decisions", [])
        led.nonce = blob.get("nonce", 0)
        return led


def _dataclass_from(cls, blob: dict):
    """Fill missing keys with defaults instead of raising, so old state files stay readable."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in blob.items() if k in names}
    return cls(**kwargs)
