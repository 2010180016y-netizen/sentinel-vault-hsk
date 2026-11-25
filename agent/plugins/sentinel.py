"""`sentinel-hsk` plugin: the agent's capabilities as elizaOS components.

The split is the point. **Providers** are what the agent knows before it decides (market, risk,
portfolio, identity, payments). **Actions** are what it may do, each with a `validate` gate and a
declared parameter schema. **Evaluators** run afterwards and write the outcome back, which is where
the ERC-8004 reputation numbers eventually come from.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..runtime import Action, ActionResult, Evaluator, Parameter, Plugin, ProviderSpec, State
from ..x402 import Budget, X402Client, X402Error


def build_plugin(agent: Any) -> Plugin:
    """Bind the runtime components to a concrete `SentinelAgent`."""

    # ------------------------------------------------------------------ providers

    def market_provider(runtime, message, state: State) -> dict:
        obs = agent.last_observation
        if obs is None:
            return {"text": "시장: 아직 관측 없음(첫 판단 때 조회)", "data": {"market": None}}
        age = max(0, int(time.time()) - agent.last_observed_at)
        text = (
            f"시장 {obs.market.symbol}/USDT {obs.state.spot_quote_per_base:.6f} "
            f"(TWAP 대비 {obs.state.twap_deviation_bps:+.0f}bp, {age}s 전 관측), "
            f"깊이 {obs.state.depth_quote():,.0f} USDT, 120 USDT 임팩트 {obs.impact_bps_buy:.0f}bp"
        )
        return {"text": text, "data": {"market": obs.as_dict(), "observation_age_s": age}}

    def risk_provider(runtime, message, state: State) -> dict:
        p = agent.policy
        g = agent.guards_summary()
        text = (
            f"리스크: 1회 {p.max_trade_notional/1e6:.0f} / 일일 {p.max_daily_notional/1e6:.0f} USDT, "
            f"낙폭 {p.max_drawdown_bps}bp, 엣지 하한 {p.min_edge_bps}bp | 가드 {g['mode']} "
            f"(킬스위치 {'ON' if g['kill_switch'] else 'OFF'}, 오늘 {g['period_spent_quote']:.2f} USDT 사용)"
        )
        return {"text": text, "data": {"policy": p.as_dict(), "guards": g}}

    def portfolio_provider(runtime, message, state: State) -> dict:
        m = agent.ledger.metrics()
        pos = agent.ledger.positions.get(agent.market.asset)
        text = (
            f"포트폴리오: 평가액 {m['equity']:.2f} USDT, 실현손익 {m['realised_pnl_quote']:+.4f}, "
            f"거래 {m['trades']}건, 보유 {pos.qty if pos else 0} {agent.market.symbol} raw"
        )
        return {"text": text, "data": {"metrics": m, "position": pos.as_dict() if pos else None}}

    def identity_provider(runtime, message, state: State) -> dict:
        ident = _identity_config()
        if not ident.get("agent_id"):
            return {
                "text": "ERC-8004 신원: 미등록 (SENTINEL_8004_AGENT_ID 미설정)",
                "data": {"erc8004": ident},
            }
        return {
            "text": f"ERC-8004 에이전트 #{ident['agent_id']} · identity {ident.get('identity', '?')}",
            "data": {"erc8004": ident},
        }

    def payments_provider(runtime, message, state: State) -> dict:
        payee = _payee()
        budget = _budget_cap()
        text = (
            f"x402 결제: payTo {payee or '미설정'} · 회당 상한 {budget} (atomic)"
            if payee
            else "x402 결제: payTo 미설정(SENTINEL_X402_PAY_TO)"
        )
        return {"text": text, "data": {"x402": {"pay_to": payee, "budget_cap": budget}}}

    def recent_provider(runtime, message, state: State) -> dict:
        items = runtime.recent(3, role="agent")
        if not items:
            return {"text": "", "data": {"recent": []}}
        return {"text": "최근: " + " / ".join(m.text[:60] for m in items), "data": {"recent": [m.meta for m in items]}}

    # ------------------------------------------------------------------ actions

    def read_market(runtime, message, state: State, params: dict) -> ActionResult:
        age = int(time.time()) - agent.last_observed_at
        if agent.last_observation is None or age > 5:
            obs = agent.observe()
        else:
            obs = agent.last_observation
        return ActionResult(
            True,
            text=(
                f"{obs.market.symbol}/USDT {obs.state.spot_quote_per_base:.6f} USDT, "
                f"TWAP 대비 {obs.state.twap_deviation_bps:+.0f}bp, 왕복비용 {obs.as_dict()['round_trip_cost_bps']:.0f}bp"
            ),
            data=obs.as_dict(),
        )

    def report_performance(runtime, message, state: State, params: dict) -> ActionResult:
        m = agent.ledger.metrics()
        return ActionResult(
            True,
            text=(
                f"실현손익 {m['realised_pnl_quote']:+.4f} USDT · 수수료 {m['fees_paid_quote']:.4f} · "
                f"승률 {m['hit_rate']*100:.0f}% · 선언 {m['declared_edge_sum_bps']:.0f}bp → 실현 "
                f"{m['realised_edge_sum_bps']:.0f}bp (신뢰도 {m['edge_credibility_bps']:.0f}bp)"
            ),
            data={"metrics": m},
        )

    def check_risk(runtime, message, state: State, params: dict) -> ActionResult:
        g = agent.guards_summary()
        p = agent.policy
        return ActionResult(
            True,
            text=(
                f"온체인 한도 1회 {p.max_trade_notional/1e6:.0f} / 일일 {p.max_daily_notional/1e6:.0f} USDT · "
                f"가드 {g['mode']} · 킬스위치 {'ON' if g['kill_switch'] else 'OFF'} · "
                f"임팩트≤{g['max_price_impact_bps']}bp · 깊이≥${g['min_quote_depth_usd']:,} · "
                f"오늘 사용 {g['period_spent_quote']:.2f} USDT / {g['per_period_cap_quote']:.0f}"
            ),
            data={"policy": p.as_dict(), "guards": g},
        )

    def explain_decision(runtime, message, state: State, params: dict) -> ActionResult:
        if agent.last_decision is None or agent.last_observation is None:
            return ActionResult(False, error="아직 판단 기록이 없습니다")
        text = agent.explain(agent.last_decision, agent.last_observation)
        return ActionResult(True, text=text, data={"decision": agent.last_decision.as_dict() if hasattr(agent.last_decision, "as_dict") else {}})

    def trade_once(runtime, message, state: State, params: dict) -> ActionResult:
        record = agent.step()
        return ActionResult(
            bool(record.get("executed")) or record.get("action") == "NO_TRADE",
            text=record.get("explain", ""),
            data=record,
        )

    def adjust_agent(runtime, message, state: State, params: dict) -> ActionResult:
        adj = agent.adjust(message.text)
        if adj is None:
            return ActionResult(False, error="조정할 항목을 찾지 못했습니다(예: '1회 한도 80으로', '거래 빈도 줄여', 'WETH로 바꿔')")
        return ActionResult(adj.ok, text=adj.message, data={"field": adj.field, "before": adj.before, "after": adj.after})

    def set_kill_switch(runtime, message, state: State, params: dict) -> ActionResult:
        text = message.text.lower()
        want_on = True
        if any(k in text for k in ("재개", "풀어", "resume", "start", "켜")):
            want_on = False
        if "on" in params:
            want_on = bool(params["on"])
        agent.guard_state.engage_kill_switch(want_on)
        note = (
            "에이전트 가드를 잠갔습니다: 이제 어떤 인텐트도 만들지 않습니다(온체인 pause는 소유자 키 전용)."
            if want_on
            else "에이전트 가드를 풀었습니다."
        )
        return ActionResult(True, text=note, data={"kill_switch": want_on})

    def buy_premium_data(runtime, message, state: State, params: dict) -> ActionResult:
        url = os.environ.get("SENTINEL_DATA_URL", "").strip()
        key = os.environ.get("SENTINEL_STRATEGY_KEY", "").strip()
        if not url or not key:
            return ActionResult(
                False,
                error="x402 소비자 미설정: SENTINEL_DATA_URL 과 SENTINEL_STRATEGY_KEY 가 필요합니다",
            )
        cap = _budget_cap()
        client = X402Client(key, chain_id=agent.settings.chain.chain_id, budget=Budget(cap=cap))
        try:
            res = client.pay_and_fetch(url)
        except X402Error as exc:
            return ActionResult(False, error=f"x402: {exc}")
        if not res.ok:
            return ActionResult(False, error=f"x402 실패({res.status}): {res.error}", data=res.as_dict())
        return ActionResult(
            True,
            text=f"프리미엄 데이터 수신 완료 · {res.paid} atomic USDT 지불 · 정산 {res.settlement.transaction if res.settlement else 'n/a'}",
            data=res.as_dict(),
        )

    def report_reputation(runtime, message, state: State, params: dict) -> ActionResult:
        ident = _identity_config()
        if not ident.get("agent_id"):
            return ActionResult(
                False,
                error="ERC-8004 미등록: SENTINEL_8004_IDENTITY / SENTINEL_8004_AGENT_ID 를 설정하세요",
            )
        m = agent.ledger.metrics()
        return ActionResult(
            True,
            text=(
                f"에이전트 #{ident['agent_id']} 평판 근거: 실현/선언 엣지 {m['edge_credibility_bps']:.0f}bp, "
                f"거래 {m['trades']}건 · 어댑터가 vault.stats()를 읽어 피드백을 올립니다"
            ),
            data={"erc8004": ident, "credibility_bps": m["edge_credibility_bps"], "trades": m["trades"]},
        )

    # ------------------------------------------------------------------ evaluator

    def execution_quality(runtime, message, state: State, result: ActionResult) -> dict | None:
        if result.action != "TRADE_ONCE":
            return None
        record = result.data or {}
        if record.get("executed"):
            return {
                "evaluator": "execution-quality",
                "leg_edge_bps": record.get("leg_edge_bps"),
                "price": record.get("price"),
                "tx": record.get("tx", ""),
            }
        if record.get("guards"):
            return {"evaluator": "execution-quality", "decision": "no_trade", "guard": record["guards"].get("code")}
        return {"evaluator": "execution-quality", "decision": "no_trade", "reason": (record.get("policy") or {}).get("reason")}

    # ------------------------------------------------------------------ assembly

    return Plugin(
        name="sentinel-hsk",
        description="Policy-bound HSKSwap trading agent: market, risk, execution, x402 data purchase, ERC-8004 identity",
        providers=(
            ProviderSpec("market", "pool TWAP/spot, depth, impact", market_provider, position=10),
            ProviderSpec("risk", "vault policy and swap guards", risk_provider, position=20),
            ProviderSpec("portfolio", "ledger metrics and position", portfolio_provider, position=30),
            ProviderSpec("identity", "ERC-8004 agent identity", identity_provider, position=40),
            ProviderSpec("payments", "x402 payee and budget", payments_provider, position=50),
            ProviderSpec("recent", "recent agent outcomes", recent_provider, position=60),
        ),
        actions=(
            Action(
                "READ_MARKET",
                "Read the live pool: price, TWAP dislocation, depth, impact, round-trip cost",
                read_market,
                lambda rt, m, s: _hit(m.text, ("시장", "가격", "상태", "status", "market", "price", "얼마")),
                similes=("시장", "가격", "상태", "status", "market", "price"),
                tags=("read",),
            ),
            Action(
                "REPORT_PERFORMANCE",
                "Report realised PnL, fees, hit rate and claimed-vs-realised edge",
                report_performance,
                lambda rt, m, s: _hit(m.text, ("손익", "pnl", "수익", "성과", "performance")),
                similes=("손익", "pnl", "수익", "성과", "performance"),
                tags=("read",),
            ),
            Action(
                "CHECK_RISK",
                "Report the on-chain policy and the off-chain swap guards, including the kill switch",
                check_risk,
                lambda rt, m, s: _hit(m.text, ("리스크", "risk", "한도", "정책", "policy", "가드", "guard")),
                similes=("리스크", "risk", "한도", "정책", "policy", "가드", "guard"),
                tags=("read",),
            ),
            Action(
                "EXPLAIN_DECISION",
                "Explain the last decision against the numbers behind it",
                explain_decision,
                lambda rt, m, s: _hit(m.text, ("왜", "근거", "why", "explain", "설명")),
                similes=("왜", "근거", "why", "explain"),
                tags=("read",),
            ),
            Action(
                "TRADE_ONCE",
                "Run one observe -> decide -> guard -> execute(or refuse) -> account cycle",
                trade_once,
                lambda rt, m, s: _hit(m.text, ("1회", "판단", "실행", "step", "거래해", "매매")),
                similes=("1회", "판단", "실행", "step", "매매"),
                parameters=(Parameter("dry_run", "record only, never broadcast", "boolean"),),
                tags=("act",),
            ),
            Action(
                "ADJUST_AGENT",
                "Change agent-side sizing/frequency/appetite/asset inside the vault's limits",
                adjust_agent,
                lambda rt, m, s: _hit(
                    m.text, ("한도", "빈도", "성향", "바꿔", "줄여", "늘려", "리스크 낮", "리스크 높")
                ),
                similes=(
                    "한도", "빈도", "성향", "바꿔", "줄여", "늘려", "리스크 낮", "리스크 높", "로 바꿔"
                ),
                priority=5,
                tags=("configure",),
            ),
            Action(
                "SET_KILL_SWITCH",
                "Engage or release the agent-side kill switch (all intents stop)",
                set_kill_switch,
                lambda rt, m, s: _hit(m.text, ("정지", "멈춰", "중단", "재개", "kill", "stop", "resume", "가드 잠")),
                similes=("정지", "멈춰", "중단", "재개", "kill", "stop", "resume"),
                parameters=(Parameter("on", "true engages the switch", "boolean"),),
                tags=("configure",),
            ),
            Action(
                "BUY_PREMIUM_DATA",
                "Buy the premium risk feed over x402 within the wallet budget",
                buy_premium_data,
                lambda rt, m, s: _hit(m.text, ("프리미엄", "데이터 사", "x402", "결제", "pay")),
                similes=("프리미엄", "x402", "데이터 구매", "결제"),
                tags=("pay",),
            ),
            Action(
                "REPORT_REPUTATION",
                "Report the ERC-8004 identity and the numbers an attestor would post",
                report_reputation,
                lambda rt, m, s: _hit(m.text, ("평판", "신뢰", "8004", "reputation", "attest")),
                similes=("평판", "신뢰", "8004", "reputation"),
                tags=("read",),
            ),
        ),
        evaluators=(
            Evaluator(
                "execution-quality",
                "Records the per-leg execution result (or the guard that refused) after a trade attempt",
                execution_quality,
            ),
        ),
    )


def _hit(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(w.lower() in lowered for w in words)


def _identity_config() -> dict:
    return {
        "identity": os.environ.get("SENTINEL_8004_IDENTITY", ""),
        "reputation": os.environ.get("SENTINEL_8004_REPUTATION", ""),
        "validation": os.environ.get("SENTINEL_8004_VALIDATION", ""),
        "agent_id": os.environ.get("SENTINEL_8004_AGENT_ID", ""),
    }


def _payee() -> str:
    return os.environ.get("SENTINEL_X402_PAY_TO", "") or os.environ.get("SENTINEL_OWNER", "")


def _budget_cap() -> int:
    try:
        return int(os.environ.get("SENTINEL_X402_BUDGET", "10000"))
    except ValueError:
        return 10_000
