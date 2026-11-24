"""Read-only view of the deployed vault — the on-chain side of the agent's own claims.

The agent's ledger is the off-chain twin of the vault's book. When a vault address is configured
(`SENTINEL_VAULT`), every chat answer that talks about performance is checked against the contract, so
the agent cannot report a number the chain disagrees with. That check is the reason the project exists:
claims are not settled by the agent's own book-keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

VAULT_ABI = [
    {
        "name": "stats",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "name": "s",
                "type": "tuple",
                "components": [
                    {"name": "tradeCount", "type": "uint256"},
                    {"name": "winCount", "type": "uint256"},
                    {"name": "realizedPnlQuote", "type": "int256"},
                    {"name": "feesPaidQuote", "type": "uint256"},
                    {"name": "declaredEdgeSumBps", "type": "int256"},
                    {"name": "realizedEdgeSumBps", "type": "int256"},
                    {"name": "equity", "type": "uint256"},
                    {"name": "highWater", "type": "uint256"},
                    {"name": "dailyNotional", "type": "uint256"},
                    {"name": "oracleHealthy", "type": "bool"},
                    {"name": "executionEdgeSumBps", "type": "int256"},
                ],
            }
        ],
    },
    {"name": "edgeCredibilityBps", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "int256"}]},
    {"name": "avgExecutionEdgeBps", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "int256"}]},
    {"name": "paused", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "bool"}]},
]

STATS_FIELDS = (
    "trade_count",
    "win_count",
    "realised_pnl_quote",
    "fees_paid_quote",
    "declared_edge_sum_bps",
    "realised_edge_sum_bps",
    "equity",
    "high_water",
    "daily_notional",
    "oracle_healthy",
    "execution_edge_sum_bps",
)


class ContractLike(Protocol):  # pragma: no cover - typing only
    def functions(self): ...


@dataclass
class VaultView:
    rpc: Any
    address: str

    def stats(self) -> dict:
        c = self.rpc.contract(self.address, VAULT_ABI)
        raw = c.functions.stats().call()
        values = raw[0] if isinstance(raw, (list, tuple)) and len(raw) == 1 and isinstance(raw[0], (list, tuple)) else raw
        out = {name: values[i] for i, name in enumerate(STATS_FIELDS)}
        out["edge_credibility_bps"] = c.functions.edgeCredibilityBps().call()
        out["avg_execution_edge_bps"] = c.functions.avgExecutionEdgeBps().call()
        out["paused"] = c.functions.paused().call()
        return out

    def summary(self) -> dict:
        """Never raises: a dashboard must survive an RPC hiccup, and the caller decides how to say it."""
        try:
            s = self.stats()
            return {"ok": True, "address": self.address, **s}
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return {"ok": False, "address": self.address, "error": f"{type(exc).__name__}: {exc}"}

    def sentence(self) -> str:
        s = self.summary()
        if not s["ok"]:
            return f"온체인 조회 실패({s['error']}) — 컨트랙트 주소·RPC를 확인하세요."
        return (
            f"온체인 볼트: 거래 {s['trade_count']}건 · 실현손익 {s['realised_pnl_quote'] / 1e6:+.4f} USDT · "
            f"선언 엣지 {s['declared_edge_sum_bps']}bp → 실현 {s['realised_edge_sum_bps']}bp "
            f"(신뢰도 {s['edge_credibility_bps']}bp) · 다리당 실행 {s['avg_execution_edge_bps']}bp · "
            f"평가액 {s['equity'] / 1e6:.2f} USDT"
            + ("" if s["oracle_healthy"] else " · ⚠ 오라클 미가용(평가액 일부 원가 기준)")
            + (" · ⏸ 정지 상태" if s["paused"] else "")
        )
