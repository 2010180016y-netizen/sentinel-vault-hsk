"""Conversational controls: the chat is not read-only.

Track 1 asks the agent to expose a conversational interface for *adjusting* position size, trade
frequency, risk appetite and target asset — not just for reporting. This module turns a plain sentence
("1회 한도를 100으로", "거래 빈도 줄여", "리스크 낮춰", "WETH로 바꿔") into a bounded change of the
agent's own behaviour.

The boundary is the whole point, and it is a boundary in code, not in a prompt:

  * The vault's policy (per-trade cap, daily cap, slippage band, edge floor, drawdown, cooldown floor) is
    set by the human with the owner key and **cannot** be moved from a chat message.
  * What the chat can move lives strictly inside those limits. Asking for more is refused with the actual
    on-chain number, not silently clamped.
  * Changing a strategy parameter changes `strategyHash`, so a live agent must have that hash registered
    by the human before the next trade. The reply says so, with the hash, instead of pretending the change
    is self-authorising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .config import DEFAULT_MARKET, MARKETS, MARKET_STATS, RiskPolicy, Settings, Strategy

# (min_conviction, target_position_frac) — how eagerly the agent acts, inside the same hard limits.
RISK_PROFILES: dict[str, tuple[float, float]] = {
    "low": (0.72, 0.35),
    "normal": (0.55, 0.60),
    "high": (0.40, 0.90),
}

MAX_COOLDOWN_S = 86_400


@dataclass
class Controls:
    """What a conversation may change. Everything here is agent-side and bounded by the vault's policy."""

    asset: str = DEFAULT_MARKET
    size_quote: float = 120.0  # per-entry notional in quote units
    cooldown_s: int = 60
    risk_appetite: str = "normal"
    min_conviction: float = 0.55
    target_position_frac: float = 0.60

    @classmethod
    def from_settings(cls, settings: Settings) -> "Controls":
        p = settings.policy
        return cls(
            asset=DEFAULT_MARKET,
            size_quote=p.max_trade_notional / 1e6 * settings.strategy.target_position_frac,
            cooldown_s=p.cooldown_s,
            risk_appetite="normal",
            min_conviction=settings.strategy.min_conviction,
            target_position_frac=settings.strategy.target_position_frac,
        )

    def strategy(self, base: Strategy) -> Strategy:
        return replace(
            base, min_conviction=self.min_conviction, target_position_frac=self.target_position_frac
        )

    def as_dict(self) -> dict:
        return {
            "asset": self.asset,
            "size_quote": round(self.size_quote, 4),
            "cooldown_s": self.cooldown_s,
            "risk_appetite": self.risk_appetite,
            "min_conviction": round(self.min_conviction, 3),
            "target_position_frac": round(self.target_position_frac, 3),
        }


@dataclass
class Adjustment:
    ok: bool
    field: str
    before: Any
    after: Any
    message: str


_NUM = r"(\d+(?:\.\d+)?)"
_SIZE_WORDS = ("한도", "크기", "사이즈", "주문", "금액", "size", "notional", "position size", "per trade")
_FREQ_WORDS = ("빈도", "쿨다운", "간격", "cooldown", "frequency", "interval")
_RISK_WORDS = ("리스크", "위험", "risk", "성향", "appetite")
_ASSET_WORDS = ("자산", "종목", "코인", "asset", "market", "토큰")


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _number_near(q: str, words: tuple[str, ...]) -> float | None:
    """The number that belongs to the keyword, not the first digit in the sentence.

    "1회 한도 100으로" must read 100, not the "1" of "1회"; "한도를 900으로" must read 900.
    """
    for w in words:
        i = q.find(w)
        if i < 0:
            continue
        for segment in (q[i + len(w) :], q[:i]):
            m = re.search(_NUM, segment)
            if m:
                return float(m.group(1))
    return None


def apply_adjustment(text: str, controls: Controls, policy: RiskPolicy) -> Adjustment | None:
    """Parse one conversational adjustment. Returns None when the sentence is not an adjustment."""
    q = text.strip().lower()
    if not q:
        return None

    # ---- target asset -------------------------------------------------------------------------
    for key, market in MARKETS.items():
        if key.lower() in q:
            if controls.asset == key:
                return Adjustment(True, "asset", key, key, f"이미 {key}/USDT 시장을 보고 있습니다.")
            stats = MARKET_STATS.get(key, {})
            thin = ""
            if key != DEFAULT_MARKET and stats:
                thin = (
                    f" 참고: 이 풀의 120 USDT 임팩트는 실측 {stats['impact_bps_at_120']:.0f}bp로 "
                    f"{DEFAULT_MARKET}({MARKET_STATS[DEFAULT_MARKET]['impact_bps_at_120']:.0f}bp)보다 비쌉니다."
                )
            before = controls.asset
            controls.asset = key
            return Adjustment(
                True,
                "asset",
                before,
                key,
                f"대상 자산을 {before} → {key}/USDT로 바꿨습니다.{thin}",
            )

    # ---- risk appetite ------------------------------------------------------------------------
    if _has(q, _RISK_WORDS):
        if any(w in q for w in ("낮", "줄", "보수", "conservative", "low", "down")):
            return _set_appetite(controls, "low")
        if any(w in q for w in ("높", "늘", "공격", "aggressive", "high", "up")):
            return _set_appetite(controls, "high")
        if any(w in q for w in ("보통", "중립", "normal", "reset", "기본")):
            return _set_appetite(controls, "normal")

    # ---- per-entry size -----------------------------------------------------------------------
    if _has(q, _SIZE_WORDS):
        want = _number_near(q, _SIZE_WORDS)
        if want is not None:
            cap = policy.max_trade_notional / 1e6
            if want <= 0:
                return Adjustment(False, "size_quote", controls.size_quote, want, "크기는 0보다 커야 합니다.")
            if want > cap:
                return Adjustment(
                    False,
                    "size_quote",
                    controls.size_quote,
                    want,
                    f"요청한 {want:.0f} USDT는 온체인 1회 한도 {cap:.0f} USDT를 넘습니다. "
                    f"이 한도는 소유자 키의 정책(정책 교체 트랜잭션)으로만 바뀌고, 대화로는 바꿀 수 없습니다.",
                )
            before = controls.size_quote
            controls.size_quote = want
            return Adjustment(
                True, "size_quote", before, want, f"1회 진입 규모를 {before:.0f} → {want:.0f} USDT로 바꿨습니다."
            )

    # ---- trade frequency ----------------------------------------------------------------------
    if _has(q, _FREQ_WORDS):
        cap = policy.cooldown_s  # the chain's floor: the vault refuses entries inside it
        near = _number_near(q, _FREQ_WORDS)
        if near is not None:
            want = int(near)
        elif any(w in q for w in ("줄", "느리", "낮", "slow", "less")):
            want = min(controls.cooldown_s * 2, MAX_COOLDOWN_S)
        elif any(w in q for w in ("늘", "빠르", "높", "fast", "more")):
            want = max(controls.cooldown_s // 2, cap)
        else:
            return None
        before = controls.cooldown_s
        want = max(cap, min(want, MAX_COOLDOWN_S))
        controls.cooldown_s = want
        note = "" if want > cap else f" (온체인 최소 쿨다운 {cap}초가 하한입니다)"
        return Adjustment(
            True, "cooldown_s", before, want, f"진입 간격을 {before}s → {want}s로 바꿨습니다.{note}"
        )

    return None


def _set_appetite(controls: Controls, level: str) -> Adjustment:
    before = controls.risk_appetite
    conviction, frac = RISK_PROFILES[level]
    controls.risk_appetite = level
    controls.min_conviction = conviction
    controls.target_position_frac = frac
    return Adjustment(
        True,
        "risk_appetite",
        before,
        level,
        f"리스크 성향을 {before} → {level}로 바꿨습니다 "
        f"(최소 확신도 {conviction:.2f}, 목표 비중 {frac:.2f}). "
        f"슬리피지·낙폭·1회 한도 같은 온체인 한도는 그대로입니다.",
    )
