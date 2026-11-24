"""SentinelAgent: observe -> decide -> self-check -> execute(or refuse) -> account -> explain.

This is the loop a judge can follow in one sitting:

  1. OBSERVE   pool TWAP + spot, real quotes for the size we would trade, trade bars.
  2. DECIDE    deterministic signal with an explicit expected-edge number in bps.
  3. SELF-CHECK the same predicates the vault will apply, so a rejection costs nothing.
  4. ACT       paper fill or dual-signed on-chain intent.
  5. ACCOUNT   fees, realised PnL, high-water mark, and declared-vs-realised edge.
  6. EXPLAIN   a sentence a human can audit against the numbers above.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import BPS, LIQUID_MARKETS, MARKETS, Market, RiskPolicy, Settings, Strategy
from .executor import ChainExecutor, PaperExecutor
from .intent import Intent, strategy_id
from .ledger import Ledger
from .market import Bar, DexClient, PoolState
from .policy import AccountState, MarketState, build_min_out, check
from .rpc import Rpc
from .signals import Decision, build_features, decide


@dataclass
class Observation:
    market: Market
    state: PoolState
    bars: list[Bar]
    impact_bps_buy: float
    impact_bps_sell: float
    quote_buy: int
    quote_sell: int
    fair_buy: int
    fair_sell: int

    def as_dict(self) -> dict:
        return {
            "pool": self.state.address,
            "tick": self.state.tick,
            "twap_tick": self.state.twap_tick,
            "twap_window_s": self.state.twap_window,
            "twap_dev_bps": round(self.state.twap_deviation_bps, 1),
            "price_quote_per_base": round(self.state.spot_quote_per_base, 8),
            "twap_price_quote_per_base": round(self.state.twap_quote_per_base, 8),
            "liquidity": self.state.liquidity,
            "depth_quote": round(self.state.depth_quote(), 2),
            "bars": len(self.bars),
            "impact_bps_buy": round(self.impact_bps_buy, 1),
            "impact_bps_sell": round(self.impact_bps_sell, 1),
        }


class SentinelAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dex: DexClient | None = None,
        rpc: Rpc | None = None,
        ledger: Ledger | None = None,
        executor=None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.rpc = rpc or Rpc(self.settings.chain.rpc_url, self.settings.chain.chain_id)
        self.dex = dex or DexClient(self.rpc, self.settings.addresses)
        self.ledger = ledger or Ledger()
        self.policy: RiskPolicy = self.settings.policy
        self.strategy: Strategy = self.settings.strategy
        self.strategy_hash = strategy_id(self.strategy.spec())
        self.executor = executor or PaperExecutor(self.dex, self.ledger, self.settings.addresses)
        self.market: Market = MARKETS[LIQUID_MARKETS[0]]
        self.last_observation: Observation | None = None
        self.last_decision: Decision | None = None

    # ------------------------------------------------------------------ observe

    def observe(self) -> Observation:
        m = self.market
        state = self.dex.pool_state(m, twap_window=self.policy.twap_window)
        bars = self.dex.bars(m, n_bars=40, swaps_per_bar=2)

        size_quote = int(self.policy.max_trade_notional * self.strategy.target_position_frac)
        qb = self.dex.quote_buy(m, size_quote)
        fair_buy = self.dex.fair_out(m, size_quote, True, state.twap_quote_per_base)

        pos_qty = self.ledger.positions.get(m.asset)
        sell_size = min(pos_qty.qty, int(self.policy.max_trade_notional * 1.0)) if pos_qty else 0
        qs = self.dex.quote_sell(m, sell_size) if sell_size > 0 else None
        fair_sell = self.dex.fair_out(m, sell_size, False, state.twap_quote_per_base) if sell_size > 0 else 0

        def _impact(quoted: int, fair: int) -> float:
            if fair <= 0 or quoted <= 0:
                return 0.0
            return max(0.0, (fair - quoted) / fair * BPS)

        obs = Observation(
            market=m,
            state=state,
            bars=bars,
            impact_bps_buy=_impact(qb.amount_out, fair_buy),
            impact_bps_sell=_impact(qs.amount_out if qs else 0, fair_sell),
            quote_buy=qb.amount_out,
            quote_sell=qs.amount_out if qs else 0,
            fair_buy=fair_buy,
            fair_sell=fair_sell,
        )
        self.last_observation = obs
        return obs

    # ------------------------------------------------------------------ decide

    def decide(self, obs: Observation | None = None) -> Decision:
        obs = obs or self.observe()
        m = self.market
        pos = self.ledger.positions.get(m.asset)
        price = obs.state.twap_quote_per_base
        pos_value = (pos.qty / 10**m.decimals * price) if pos else 0.0

        f = build_features(
            obs.bars,
            twap_dev_bps=obs.state.twap_deviation_bps,
            fee_bps=m.fee / 100.0,
            impact_bps_buy=obs.impact_bps_buy,
            impact_bps_sell=obs.impact_bps_sell,
            liquidity_quote=obs.state.depth_quote(),
            strategy=self.strategy,
        )
        d = decide(f, self.policy, self.strategy, has_position=bool(pos and pos.qty > 0), position_quote=pos_value)
        self.last_decision = d
        return d

    # ------------------------------------------------------------------ act

    def _account_state(self, obs: Observation) -> AccountState:
        m = self.market
        pos = self.ledger.positions.get(m.asset)
        price = obs.state.twap_quote_per_base
        pos_value = (pos.qty / 10**m.decimals * price) if pos else 0.0
        equity = self.ledger.quote_balance + pos_value
        day_ago = int(time.time()) - 86_400
        daily = sum(t.notional_quote for t in self.ledger.trades if t.side == "BUY" and t.ts >= day_ago)
        last_ts = self.ledger.trades[-1].ts if self.ledger.trades else 0
        return AccountState(
            equity_quote=equity,
            high_water_quote=max(self.ledger.high_water, equity),
            daily_notional_quote=daily,
            last_trade_ts=last_ts,
            position_qty=pos.qty if pos else 0,
            position_quote_value=pos_value,
        )

    def step(self) -> dict[str, Any]:
        ts = int(time.time())
        obs = self.observe()
        d = self.decide(obs)
        m = self.market
        acct = self._account_state(obs)

        record: dict[str, Any] = {
            "ts": ts,
            "action": d.action,
            "conviction": d.conviction,
            "edge_bps": d.edge_bps,
            "hurdle_bps": d.hurdle_bps,
            "size_quote": d.size_quote,
            "reasons": d.reasons,
            "features": d.features.as_dict() if d.features else {},
            "observation": obs.as_dict(),
            "strategy_hash": self.strategy_hash,
        }

        if not d.is_trade:
            self.ledger.record_decision(record)
            self.ledger.mark(ts, {m.asset: obs.state.twap_quote_per_base})
            record["executed"] = False
            record["explain"] = self.explain(d, obs)
            return record

        is_buy = d.action == "BUY"
        if is_buy:
            amount_in = int(round(d.size_quote * 1e6))
            min_out = build_min_out(obs.quote_buy, obs.fair_buy, self.policy.max_slippage_bps)
            notional_quote = d.size_quote
        else:
            amount_in = acct.position_qty
            min_out = build_min_out(obs.quote_sell, obs.fair_sell, self.policy.max_slippage_bps) if obs.quote_sell else 0
            notional_quote = obs.fair_sell / 1e6

        verdict = check(
            is_buy=is_buy,
            notional_quote=notional_quote,
            declared_edge_bps=d.edge_bps,
            min_out=min_out,
            fair_out=obs.fair_buy if is_buy else obs.fair_sell,
            max_notional_quote=(self.policy.max_trade_notional / 1e6) if is_buy else 1e18,
            policy=self.policy,
            account=acct,
            market=MarketState(
                spot_vs_twap_bps=obs.state.twap_deviation_bps,
                fair_out=obs.fair_buy if is_buy else obs.fair_sell,
                quoted_out=obs.quote_buy if is_buy else obs.quote_sell,
            ),
        )
        record["policy"] = {"ok": verdict.ok, "reason": verdict.reason, "detail": verdict.detail, "notes": verdict.notes}

        if not verdict.ok:
            record["executed"] = False
            self.ledger.record_decision(record)
            self.ledger.mark(ts, {m.asset: obs.state.twap_quote_per_base})
            record["explain"] = f"refused before spending gas: {verdict.reason} — {verdict.detail}"
            return record

        # --- execute ---
        if isinstance(self.executor, PaperExecutor):
            fill = (
                self.executor.buy(m, d.size_quote, obs.state)
                if is_buy
                else self.executor.sell(m, amount_in, obs.state)
            )
            tx = ""
            price = fill.price
            realised_edge = (
                (fill.amount_out - obs.fair_buy) / obs.fair_buy * BPS
                if is_buy and obs.fair_buy
                else (fill.amount_out - obs.fair_sell) / obs.fair_sell * BPS
                if obs.fair_sell
                else 0.0
            )
            fee_quote = fill.fee_quote
            amount_out = fill.amount_out
        else:
            it = Intent(
                token_in=self.settings.addresses.quote if is_buy else m.asset,
                token_out=m.asset if is_buy else self.settings.addresses.quote,
                fee=m.fee,
                amount_in=amount_in,
                min_amount_out=min_out,
                declared_edge_bps=int(round(d.edge_bps)),
                strategy_hash=self.strategy_hash,
                nonce=self.ledger.nonce,
                deadline=ts + 300,
                max_notional_quote=int(self.policy.max_trade_notional if is_buy else 2**63 - 1),
            )
            assert isinstance(self.executor, ChainExecutor)
            res = self.executor.execute(it)
            tx = res["tx"]
            amount_out = 0  # filled in from the receipt by the caller / next observation
            price = obs.state.twap_quote_per_base
            realised_edge = 0.0
            fee_quote = notional_quote * m.fee / 1_000_000

        if is_buy:
            trade = self.ledger.buy(
                ts, m.asset, d.size_quote, amount_out, price, fee_quote, d.edge_bps, realised_edge,
                d.signature_reason(), tx, decimals=m.decimals,
            )
        else:
            trade = self.ledger.sell(
                ts, m.asset, amount_in, amount_out / 1e6, price, fee_quote, d.edge_bps, realised_edge,
                d.signature_reason(), tx,
            )
        equity = self.ledger.mark(ts, {m.asset: obs.state.twap_quote_per_base})
        trade.equity_after = equity

        record.update(
            {
                "executed": True,
                "tx": tx,
                "amount_in": amount_in,
                "amount_out": amount_out,
                "price": price,
                "realised_edge_bps": round(realised_edge, 1),
                "fee_quote": round(fee_quote, 6),
                "equity": equity,
                "explain": self.explain(d, obs, executed=True, realised_edge=realised_edge),
            }
        )
        self.ledger.record_decision(record)
        return record

    # ------------------------------------------------------------------ explain

    def explain(self, d: Decision, obs: Observation, *, executed: bool = False, realised_edge: float = 0.0) -> str:
        f = d.features
        head = (
            f"{d.action} {d.size_quote:.2f} USDT at conviction {d.conviction:.2f}"
            if d.is_trade
            else "no trade"
        )
        bits = [head, f"edge {d.edge_bps:.0f}bp vs hurdle {d.hurdle_bps:.0f}bp"]
        if f:
            bits.append(
                f"spot {f.twap_dev_bps:+.0f}bp vs {obs.state.twap_window}s TWAP, RSI {f.rsi:.0f}, "
                f"trend {f.trend_bps:+.0f}bp, cost {f.round_trip_cost_bps:.0f}bp"
            )
        if executed:
            bits.append(f"filled {realised_edge:+.0f}bp vs fair")
        bits.append("why: " + "; ".join(d.reasons))
        return " | ".join(bits)

    # ------------------------------------------------------------------ interface

    def snapshot(self) -> dict[str, Any]:
        m = self.market
        obs = self.last_observation
        pos = self.ledger.positions.get(m.asset)
        price = obs.state.twap_quote_per_base if obs else 0.0
        return {
            "strategy": self.strategy.name,
            "strategy_hash": self.strategy_hash,
            "mode": "live" if isinstance(self.executor, ChainExecutor) else "paper",
            "market": {
                "symbol": f"{m.symbol}/USDT",
                "pool": obs.state.address if obs else "",
                "price": round(price, 8),
                "twap_dev_bps": round(obs.state.twap_deviation_bps, 1) if obs else 0,
                "depth_quote": round(obs.state.depth_quote(), 2) if obs else 0,
            },
            "position": {
                "qty": pos.qty if pos else 0,
                "value_quote": round((pos.qty / 10**m.decimals * price) if pos else 0.0, 4),
                "avg_price": round(pos.avg_price, 8) if pos else 0,
            },
            "policy": {
                "min_edge_bps": self.policy.min_edge_bps,
                "max_slippage_bps": self.policy.max_slippage_bps,
                "max_trade_notional": self.policy.max_trade_notional / 1e6,
                "max_daily_notional": self.policy.max_daily_notional / 1e6,
                "max_drawdown_bps": self.policy.max_drawdown_bps,
                "twap_window": self.policy.twap_window,
            },
            "metrics": self.ledger.metrics(),
            "equity_curve": self.ledger.equity_curve[-240:],
            "trades": [t.as_dict() for t in self.ledger.trades[-20:]],
            "decisions": self.ledger.decisions[-20:],
            "last": self.last_decision.features.as_dict() if self.last_decision and self.last_decision.features else {},
        }

    def answer(self, question: str) -> str:
        """The conversational surface. Plain language in, numbers from the ledger out."""
        q = question.strip().lower()
        m = self.market
        if any(k in q for k in ("상태", "status", "어때", "지금")):
            snap = self.snapshot()
            return (
                f"{snap['market']['symbol']} {snap['market']['price']:.6f} USDT "
                f"(TWAP 대비 {snap['market']['twap_dev_bps']:+.0f}bp) · 포지션 "
                f"{snap['position']['value_quote']:.2f} USDT · 평가액 {snap['metrics']['equity']:.2f} USDT · "
                f"거래 {snap['metrics']['trades']}건"
            )
        if any(k in q for k in ("손익", "pnl", "수익", "성과", "performance")):
            mt = self.ledger.metrics()
            return (
                f"실현손익 {mt['realised_pnl_quote']:+.4f} USDT, 수수료 {mt['fees_paid_quote']:.4f} USDT, "
                f"최대낙폭 {mt['max_drawdown_bps']:.0f}bp, 승률 {mt['hit_rate']*100:.0f}%, "
                f"주장 엣지 {mt['declared_edge_sum_bps']:.0f}bp → 실현 {mt['realised_edge_sum_bps']:.0f}bp "
                f"(신뢰도 {mt['edge_credibility_bps']:.0f}bp)"
            )
        if any(k in q for k in ("리스크", "risk", "한도", "정책")):
            p = self.policy
            return (
                f"1회 {p.max_trade_notional/1e6:.0f} USDT · 일일 {p.max_daily_notional/1e6:.0f} USDT · "
                f"낙폭 {p.max_drawdown_bps}bp · 슬리피지 {p.max_slippage_bps}bp · TWAP {p.twap_window}s · "
                f"엣지 허들 {p.min_edge_bps}bp"
            )
        if any(k in q for k in ("왜", "why", "설명", "근거")):
            if self.last_decision:
                assert self.last_observation is not None
                return self.explain(self.last_decision, self.last_observation)
            return "아직 판단 기록이 없습니다."
        if any(k in q for k in ("거래", "trades", "내역")):
            if not self.ledger.trades:
                return "거래 없음 — 허들을 넘은 신호가 아직 없습니다."
            lines = [
                f"{t.side} {t.notional_quote:.2f} USDT @ {t.price:.6f} (엣지 {t.realised_edge_bps:+.0f}bp)"
                for t in self.ledger.trades[-5:]
            ]
            return "\n".join(lines)
        if any(k in q for k in ("정지", "멈춰", "pause", "중단")):
            return "정지는 컨트랙트의 pause()로만 가능합니다(소유자·리스크 키). 페이퍼 모드에서는 실행을 멈추려면 루프를 종료하세요."
        # default: run one step and narrate
        rec = self.step()
        return rec.get("explain", "")

    # convenience for scripts
    def run(self, cycles: int = 1, sleep_s: float = 0.0, on_step=None) -> list[dict]:
        out = []
        for i in range(cycles):
            rec = self.step()
            out.append(rec)
            if on_step:
                on_step(rec)
            if sleep_s and i < cycles - 1:
                time.sleep(sleep_s)
        return out
