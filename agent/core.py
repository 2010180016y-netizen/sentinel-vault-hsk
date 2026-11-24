"""SentinelAgent: observe -> decide -> self-check -> execute(or refuse) -> account -> explain.

This is the loop a judge can follow in one sitting:

  1. OBSERVE   pool TWAP + spot, real quotes for the size we would trade, trade bars.
  2. DECIDE    deterministic signal with an explicit expected-edge number in bps.
  3. SELF-CHECK the same predicates the vault will apply, so a rejection costs nothing.
  4. ACT       paper fill or dual-signed on-chain intent; a refusal is recorded on-chain too.
  5. ACCOUNT   fees, realised PnL, high-water mark, per-leg execution and claimed-vs-realised edge.
  6. EXPLAIN   a sentence a human can audit against the numbers above (and against the vault's views).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import BPS, DEFAULT_MARKET, MARKETS, MARKET_STATS, Market, RiskPolicy, Settings, Strategy
from .control import Adjustment, Controls, apply_adjustment
from .executor import ChainExecutor, PaperExecutor
from .intent import Intent, strategy_id
from .ledger import Ledger
from .market import Bar, DexClient, PoolState
from .onchain import VaultView
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
            "round_trip_cost_bps": round(2 * (self.market.fee / 100.0) + self.impact_bps_buy + self.impact_bps_sell, 1),
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
        vault_view: VaultView | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.rpc = rpc or Rpc(self.settings.chain.rpc_url, self.settings.chain.chain_id)
        self.dex = dex or DexClient(self.rpc, self.settings.addresses)
        self.ledger = ledger or Ledger()
        self.policy: RiskPolicy = self.settings.policy
        self.controls: Controls = Controls.from_settings(self.settings)
        self.strategy: Strategy = self.controls.strategy(self.settings.strategy)
        self.strategy_hash = strategy_id(self.strategy.spec())
        self.executor = executor or PaperExecutor(self.dex, self.ledger, self.settings.addresses)
        self.market: Market = MARKETS[self.controls.asset]
        self.last_observation: Observation | None = None
        self.last_decision: Decision | None = None
        self.vault_view = vault_view
        if self.vault_view is None and self.settings.vault:
            self.vault_view = VaultView(self.rpc, self.settings.vault)

    # ------------------------------------------------------------------ observe

    def observe(self) -> Observation:
        m = self.market
        state = self.dex.pool_state(m, twap_window=self.policy.twap_window)
        bars = self.dex.bars(m, n_bars=40, swaps_per_bar=2)

        size_quote = int(self.controls.size_quote * 1e6)
        qb = self.dex.quote_buy(m, size_quote)
        fair_buy = self.dex.fair_out(m, size_quote, True, state.twap_quote_per_base)

        pos_qty = self.ledger.positions.get(m.asset)
        if pos_qty and pos_qty.qty > 0:
            sell_size = min(pos_qty.qty, int(self.policy.max_trade_notional))
        else:
            # Flat book: estimate the *exit* we would have to make at the same size, so the round-trip cost
            # in the decision is the real one. Quoting only the entry leg understates the hurdle by a whole
            # impact and is how a bot talks itself into trades that cannot pay for themselves.
            sell_size = fair_buy
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
        d = decide(
            f,
            self.policy,
            self.strategy,
            has_position=bool(pos and pos.qty > 0),
            position_quote=pos_value,
            size_cap_quote=self.controls.size_quote,
        )
        self.last_decision = d
        return d

    # ------------------------------------------------------------------ act

    def _market_state(self, obs: Observation, fair_out: int, quoted_out: int) -> MarketState:
        return MarketState(
            spot_vs_twap_bps=obs.state.twap_deviation_bps, fair_out=fair_out, quoted_out=quoted_out
        )

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
            "dislocation_bps": d.dislocation_bps,
            "size_quote": d.size_quote,
            "reasons": d.reasons,
            "features": d.features.as_dict() if d.features else {},
            "observation": obs.as_dict(),
            "strategy_hash": self.strategy_hash,
            "controls": self.controls.as_dict(),
        }

        if not d.is_trade:
            self.ledger.record_decision(record)
            self.ledger.mark(ts, {m.asset: obs.state.twap_quote_per_base})
            record["executed"] = False
            record["explain"] = self.explain(d, obs)
            record["onchain_refusal"] = self._record_refusal(d)
            return record

        is_buy = d.action == "BUY"
        declared_edge = int(round(d.edge_bps)) if is_buy else 0  # an exit claims nothing
        if is_buy:
            amount_in = int(round(d.size_quote * 1e6))
            min_out = build_min_out(obs.quote_buy, obs.fair_buy, self.policy.max_slippage_bps)
            notional_quote = d.size_quote
            fair_out = obs.fair_buy
        else:
            amount_in = acct.position_qty
            min_out = build_min_out(obs.quote_sell, obs.fair_sell, self.policy.max_slippage_bps) if obs.quote_sell else 0
            notional_quote = obs.fair_sell / 1e6
            fair_out = obs.fair_sell

        verdict = check(
            is_buy=is_buy,
            notional_quote=notional_quote,
            declared_edge_bps=declared_edge,
            min_out=min_out,
            fair_out=fair_out,
            max_notional_quote=(self.policy.max_trade_notional / 1e6) if is_buy else 1e18,
            policy=self.policy,
            account=acct,
            market=self._market_state(obs, fair_out, obs.quote_buy if is_buy else obs.quote_sell),
            now_ts=ts,
        )
        record["policy"] = {"ok": verdict.ok, "reason": verdict.reason, "detail": verdict.detail, "notes": verdict.notes}

        if not verdict.ok:
            record["executed"] = False
            self.ledger.record_decision(record)
            self.ledger.mark(ts, {m.asset: obs.state.twap_quote_per_base})
            record["explain"] = f"refused before spending gas: {verdict.reason} — {verdict.detail}"
            record["onchain_refusal"] = self._record_refusal(d)
            return record

        # --- execute ---
        tx = ""
        if isinstance(self.executor, PaperExecutor):
            fill = (
                self.executor.buy(m, d.size_quote, obs.state)
                if is_buy
                else self.executor.sell(m, amount_in, obs.state)
            )
            tx = ""
            price = fill.price
            amount_out = fill.amount_out
            fee_quote = fill.fee_quote
        else:
            it = Intent(
                token_in=self.settings.addresses.quote if is_buy else m.asset,
                token_out=m.asset if is_buy else self.settings.addresses.quote,
                fee=m.fee,
                amount_in=amount_in,
                min_amount_out=min_out,
                declared_edge_bps=declared_edge,
                strategy_hash=self.strategy_hash,
                nonce=self.ledger.nonce,
                deadline=ts + 300,
                max_notional_quote=int(self.policy.max_trade_notional if is_buy else 2**63 - 1),
            )
            assert isinstance(self.executor, ChainExecutor)
            res = self.executor.execute(it)
            tx = res["tx"]
            amount_out = int(res["amount_out"])  # read from the receipt's IntentExecuted log
            price = (
                (amount_in / 1e6) / (amount_out / 10**m.decimals)
                if is_buy
                else (amount_out / 1e6) / (amount_in / 10**m.decimals)
            )
            fee_quote = (amount_in if is_buy else amount_out) / 1e6 * m.fee / 1_000_000

        # Per-leg execution result against the pool's own TWAP (the vault records the same number).
        leg_edge = ((amount_out - fair_out) / fair_out * BPS) if fair_out else 0.0
        # The round trip is only settled on the exit: an entry has not earned anything yet.
        realised_edge = 0.0
        if not is_buy:
            pos = self.ledger.positions.get(m.asset)
            basis = pos.cost_quote if pos else 0.0
            realised_edge = ((amount_out / 1e6 - basis) / basis * BPS) if basis else 0.0

        if is_buy:
            trade = self.ledger.buy(
                ts, m.asset, d.size_quote, amount_out, price, fee_quote, declared_edge, 0.0,
                d.signature_reason(), tx, decimals=m.decimals, leg_edge_bps=leg_edge,
            )
        else:
            trade = self.ledger.sell(
                ts, m.asset, amount_in, amount_out / 1e6, price, fee_quote, 0, realised_edge,
                d.signature_reason(), tx, leg_edge_bps=leg_edge,
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
                "leg_edge_bps": round(leg_edge, 1),
                "realised_edge_bps": round(realised_edge, 1),
                "fee_quote": round(fee_quote, 6),
                "equity": equity,
                "explain": self.explain(d, obs, executed=True, realised_edge=leg_edge),
            }
        )
        self.ledger.record_decision(record)
        return record

    def _record_refusal(self, d: Decision) -> str:
        """Write a refusal where a trade would have been written, when running live."""
        if not isinstance(self.executor, ChainExecutor) or not self.settings.record_no_trade:
            return ""
        reason = d.reasons[0] if d.reasons else "no reason recorded"
        try:
            res = self.executor.record_no_trade(
                strategy_hash=self.strategy_hash,
                edge_bps=int(round(d.edge_bps)),
                hurdle_bps=int(round(d.hurdle_bps)),
                reason=reason,
            )
            self.ledger.nonce = int(res["nonce"])
            return res["tx"]
        except Exception as exc:  # noqa: BLE001 - the refusal log must not break the loop
            return f"record_failed: {type(exc).__name__}: {exc}"

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
            bits.append(f"filled {realised_edge:+.0f}bp vs TWAP")
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
                "impact_bps_at_size": round(obs.impact_bps_buy, 1) if obs else 0,
                "decimals": m.decimals,
            },
            "position": {
                "qty": pos.qty if pos else 0,
                "qty_whole": round(pos.qty / 10**m.decimals, 6) if pos else 0.0,
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
                "cooldown_s": self.policy.cooldown_s,
            },
            "controls": self.controls.as_dict(),
            "market_menu": {k: {"quote_depth_usd": v["quote_depth_usd"], "impact_bps_at_120": v["impact_bps_at_120"]} for k, v in MARKET_STATS.items()},
            "metrics": self.ledger.metrics(),
            "onchain": self.vault_view.summary() if self.vault_view else {"ok": False, "error": "SENTINEL_VAULT not set"},
            "equity_curve": self.ledger.equity_curve[-240:],
            "trades": [t.as_dict() for t in self.ledger.trades[-20:]],
            "decisions": self.ledger.decisions[-20:],
            "last": self.last_decision.features.as_dict() if self.last_decision and self.last_decision.features else {},
        }

    def adjust(self, text: str) -> Adjustment | None:
        """Apply a conversational adjustment, re-derive the strategy hash, and log the change."""
        adj = apply_adjustment(text, self.controls, self.policy)
        if adj is None:
            return None
        if adj.ok:
            before_hash = self.strategy_hash
            self.strategy = self.controls.strategy(self.settings.strategy)
            self.strategy_hash = strategy_id(self.strategy.spec())
            self.market = MARKETS[self.controls.asset]
            if self.strategy_hash != before_hash:
                adj.message += (
                    f" 전략 파라미터가 바뀌어 strategyHash가 {self.strategy_hash[:10]}…로 바뀌었습니다. "
                    f"라이브 실행 전에 소유자 키로 registerStrategy(0x{self.strategy_hash[2:][:8]}…) 등록이 필요합니다."
                )
            self.ledger.record_decision(
                {
                    "ts": int(time.time()),
                    "action": "ADJUST",
                    "field": adj.field,
                    "before": adj.before,
                    "after": adj.after,
                    "explain": adj.message,
                    "strategy_hash": self.strategy_hash,
                }
            )
        return adj

    def answer(self, question: str) -> str:
        """The conversational surface. Plain language in, numbers from the ledger (and the vault) out."""
        q = question.strip().lower()
        m = self.market

        adj = self.adjust(question)
        if adj is not None:
            return ("✅ " if adj.ok else "⛔ ") + adj.message

        if any(k in q for k in ("온체인", "onchain", "on-chain", "컨트랙트", "vault")):
            if not self.vault_view:
                return "볼트 주소가 설정되지 않았습니다(SENTINEL_VAULT)."
            return self.vault_view.sentence()

        if any(k in q for k in ("상태", "status", "어때", "지금")):
            snap = self.snapshot()
            mt = snap["metrics"]
            return (
                f"{snap['market']['symbol']} {snap['market']['price']:.6f} USDT "
                f"(TWAP 대비 {snap['market']['twap_dev_bps']:+.0f}bp) · 포지션 "
                f"{snap['position']['value_quote']:.2f} USDT · 평가액 {mt['equity']:.2f} USDT · "
                f"거래 {mt['trades']}건 · 1회 한도 {snap['controls']['size_quote']:.0f} USDT"
            )
        if any(k in q for k in ("손익", "pnl", "수익", "성과", "performance")):
            mt = self.ledger.metrics()
            return (
                f"실현손익 {mt['realised_pnl_quote']:+.4f} USDT, 수수료 {mt['fees_paid_quote']:.4f} USDT, "
                f"최대낙폭 {mt['max_drawdown_bps']:.0f}bp, 승률 {mt['hit_rate']*100:.0f}%, "
                f"선언 엣지 {mt['declared_edge_sum_bps']:.0f}bp → 실현 {mt['realised_edge_sum_bps']:.0f}bp "
                f"(신뢰도 {mt['edge_credibility_bps']:.0f}bp) · 다리당 실행 {mt['avg_execution_edge_bps']:+.1f}bp"
            )
        if any(k in q for k in ("리스크", "risk", "한도", "정책", "policy")):
            p = self.policy
            c = self.controls
            return (
                f"온체인 한도: 1회 {p.max_trade_notional/1e6:.0f} USDT · 일일 {p.max_daily_notional/1e6:.0f} USDT · "
                f"낙폭 {p.max_drawdown_bps}bp · 슬리피지 {p.max_slippage_bps}bp · TWAP {p.twap_window}s · "
                f"엣지 하한 {p.min_edge_bps}bp | 에이전트 설정: 1회 {c.size_quote:.0f} USDT, 간격 {c.cooldown_s}s, "
                f"성향 {c.risk_appetite}"
            )
        if any(k in q for k in ("왜", "why", "설명", "근거")):
            if self.last_decision:
                assert self.last_observation is not None
                return self.explain(self.last_decision, self.last_observation)
            return "아직 판단 기록이 없습니다."
        if any(k in q for k in ("거래", "trades", "내역")):
            if not self.ledger.trades:
                return "거래 없음 — 허들을 넘은 이탈이 아직 없었습니다."
            lines = [
                f"{t.side} {t.notional_quote:.2f} USDT @ {t.price:.6f} (다리 {t.leg_edge_bps:+.0f}bp, "
                f"라운드트립 {t.realised_edge_bps:+.0f}bp)"
                for t in self.ledger.trades[-5:]
            ]
            return "\n".join(lines)
        if any(k in q for k in ("시장", "자산", "market")):
            rows = [
                f"{k}: 깊이 ${v['quote_depth_usd']:,} / 120 USDT 임팩트 {v['impact_bps_at_120']:.0f}bp"
                for k, v in MARKET_STATS.items()
            ]
            return f"현재 {m.symbol}/USDT · 후보: " + " · ".join(rows) + " (예: 'WETH로 바꿔')"
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
