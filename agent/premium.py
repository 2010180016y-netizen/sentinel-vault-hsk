"""The paid endpoint: premium risk analytics behind an x402 challenge.

Why a trading agent would pay for data: the interesting thing a Track 1 agent can sell (or consume) is
not a quote — quotes are free on chain — but a *second opinion* about whether a trade should exist at
all (hurdle vs realised edge, guard state, drawdown). That is what this endpoint returns, and it is
priced at 0.001 USDT so the payment path is exercised without moving meaningful value.

The payee is never invented: it comes from `SENTINEL_X402_PAY_TO`, then `SENTINEL_OWNER`, then
`SENTINEL_VAULT`. If none is set the endpoint reports itself unconfigured instead of asking to be paid
somewhere arbitrary.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .config import Addresses, Settings
from .x402 import (
    NonceStore,
    PaperFacilitator,
    PaymentRequirements,
    Resource,
    Settlement,
    decode_header,
    payment_required,
)
from .x402.types import network_for

PREMIUM_PRICE_ATOMIC = "1000"  # 0.001 USDT (6dp)
DEFAULT_PATH = "/api/premium/edge"


@dataclass
class PremiumConfig:
    requirements: PaymentRequirements | None
    reason: str = ""

    @classmethod
    def from_env(cls, settings: Settings | None = None, *, price: int | None = None) -> "PremiumConfig":
        settings = settings or Settings.from_env()
        pay_to = (
            os.environ.get("SENTINEL_X402_PAY_TO", "")
            or os.environ.get("SENTINEL_OWNER", "")
            or os.environ.get("SENTINEL_VAULT", "")
        ).strip()
        if not pay_to:
            return cls(None, "set SENTINEL_X402_PAY_TO (or SENTINEL_OWNER / SENTINEL_VAULT) to accept payments")
        return cls(
            PaymentRequirements(
                scheme="exact",
                network=network_for(settings.chain.chain_id),
                amount=str(price if price is not None else os.environ.get("SENTINEL_X402_PRICE", PREMIUM_PRICE_ATOMIC)),
                asset=Addresses.quote,
                pay_to=pay_to,
                max_timeout_seconds=60,
                # Our HSK USDT does not implement EIP-3009; naming the domain honestly keeps the
                # signature meaningful even though settlement stays paper in this project.
                extra={"assetTransferMethod": "eip3009", "name": "USDT", "version": "1"},
            )
        )


class PremiumService:
    """Server-side handler for one paid resource. Verification is real; settlement is a receipt."""

    def __init__(self, agent, config: PremiumConfig, *, state_path: str | None = None) -> None:
        if config.requirements is None:
            raise ValueError(config.reason)
        self.agent = agent
        self.requirements = config.requirements
        if state_path:
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        self.facilitator = PaperFacilitator(
            self.requirements,
            nonces=NonceStore(path=None if state_path is None else f"{state_path}.nonces"),
            state_path=state_path,
        )

    def resource(self, base_url: str) -> Resource:
        return Resource(
            url=base_url.rstrip("/") + DEFAULT_PATH,
            description="Sentinel premium risk analytics: hurdle vs realised edge, guard state, drawdown",
        )

    def challenge(self, base_url: str, error: str = "") -> dict:
        return payment_required(self.requirements, self.resource(base_url), error)

    def settle(self, payment_header: str, base_url: str) -> tuple[Settlement, dict | None]:
        try:
            blob = decode_header(payment_header)
        except ValueError as exc:
            return (
                Settlement(False, "", self.requirements.network, "", "malformed_payload"),
                {"error": str(exc)},
            )
        settlement = self.facilitator.settle(blob, resource=self.resource(base_url).url)
        if not settlement.success:
            return settlement, {"error": settlement.error_reason}
        return settlement, self.payload()

    def payload(self) -> dict:
        """What the payer gets. Everything here is derived from the agent's own book, not invented."""
        agent = self.agent
        snap = agent.snapshot()
        metrics = snap["metrics"]
        guards = snap["guards"]
        decision = agent.last_decision
        return {
            "ts": int(time.time()),
            "resource": DEFAULT_PATH,
            "market": snap["market"],
            "policy": snap["policy"],
            "edge": {
                "declared_sum_bps": metrics.get("declared_edge_sum_bps"),
                "realised_sum_bps": metrics.get("realised_edge_sum_bps"),
                "credibility_bps": metrics.get("edge_credibility_bps"),
                "avg_execution_edge_bps": metrics.get("avg_execution_edge_bps"),
                "last_hurdle_bps": getattr(decision, "hurdle_bps", None),
                "last_dislocation_bps": getattr(decision, "dislocation_bps", None),
            },
            "risk": {
                "equity_quote": metrics.get("equity"),
                "max_drawdown_bps": metrics.get("max_drawdown_bps"),
                "trades": metrics.get("trades"),
            },
            "guards": guards,
        }
