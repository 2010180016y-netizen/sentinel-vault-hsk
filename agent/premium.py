"""The paid endpoint: premium risk analytics behind an x402 challenge.

Why a trading agent would pay for data: the interesting thing a Track 1 agent can sell (or consume) is
not a quote — quotes are free on chain — but a *second opinion* about whether a trade should exist at
all (hurdle vs realised edge, guard state, drawdown). That is what this endpoint returns, and it is
priced at 0.001 USDT so the payment path is exercised without moving meaningful value.

Two settlement backends, one wire protocol:

  * `paper` (default) verifies the EIP-3009 authorization for real (signature, window, amount, payTo,
    single-use nonce) and writes a `paper:` receipt. No gas, no chain write.
  * `chain` broadcasts the signed `transferWithAuthorization` through `ChainFacilitator`. It refuses
    HSK mainnet (chain 177) unless both `allow_mainnet` and `SENTINEL_ALLOW_MAINNET_SPEND=1` are set,
    so a misconfiguration cannot spend the operator's gas; on a local fork/test chain it settles for
    real. `scripts/fork_demo.py` uses this to prove the settlement leg is not a stub.

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

X402_TOKEN_ABI = [
    {
        "name": "transferWithAuthorization",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "signature", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "authorizationState",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "authorizer", "type": "address"}, {"name": "nonce", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "a", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


@dataclass
class PremiumConfig:
    requirements: PaymentRequirements | None
    reason: str = ""
    settlement: str = "paper"  # paper | chain
    rpc_url: str = ""
    facilitator_key: str = ""
    asset: str = Addresses.quote
    token_name: str = "USDT"
    token_version: str = "1"
    price: int = int(PREMIUM_PRICE_ATOMIC)

    @property
    def configured(self) -> bool:
        return self.requirements is not None

    def public(self) -> dict:
        """What the dashboard may show. Never the facilitator key."""
        if self.requirements is None:
            return {"configured": False, "reason": self.reason}
        return {
            "configured": True,
            "settlement": self.settlement,
            "network": self.requirements.network,
            "amount_atomic": self.requirements.amount,
            "amount_human": int(self.requirements.amount) / 1e6,
            "asset": self.requirements.asset,
            "pay_to": self.requirements.pay_to,
            "token": {"name": self.token_name, "version": self.token_version},
            "chain_settlement_ready": bool(self.settlement == "chain" and self.rpc_url and self.facilitator_key),
        }

    @classmethod
    def from_env(cls, settings: Settings | None = None, *, price: int | None = None) -> "PremiumConfig":
        settings = settings or Settings.from_env()
        pay_to = (
            os.environ.get("SENTINEL_X402_PAY_TO", "")
            or os.environ.get("SENTINEL_OWNER", "")
            or os.environ.get("SENTINEL_VAULT", "")
        ).strip()
        amount = price if price is not None else int(os.environ.get("SENTINEL_X402_PRICE", PREMIUM_PRICE_ATOMIC))
        asset = os.environ.get("SENTINEL_X402_ASSET", "").strip() or Addresses.quote
        name = os.environ.get("SENTINEL_X402_TOKEN_NAME", "").strip() or "USDT"
        version = os.environ.get("SENTINEL_X402_TOKEN_VERSION", "").strip() or "1"
        settlement = (os.environ.get("SENTINEL_X402_SETTLEMENT", "paper") or "paper").strip().lower()
        base = dict(
            settlement=settlement,
            rpc_url=os.environ.get("SENTINEL_X402_RPC", "").strip() or settings.chain.rpc_url,
            facilitator_key=os.environ.get("SENTINEL_X402_FACILITATOR_KEY", "").strip(),
            asset=asset,
            token_name=name,
            token_version=version,
            price=amount,
        )
        if not pay_to:
            return cls(None, "set SENTINEL_X402_PAY_TO (or SENTINEL_OWNER / SENTINEL_VAULT) to accept payments", **base)
        return cls(
            PaymentRequirements(
                scheme="exact",
                network=network_for(settings.chain.chain_id),
                amount=str(amount),
                asset=asset,
                pay_to=pay_to,
                max_timeout_seconds=60,
                # Our HSK USDT does not implement EIP-3009; naming the domain honestly keeps the
                # signature meaningful. On a fork with the mock token the domain matches the token.
                extra={"assetTransferMethod": "eip3009", "name": name, "version": version},
            ),
            **base,
        )


class PremiumService:
    """Server-side handler for one paid resource."""

    def __init__(self, agent, config: PremiumConfig, *, state_path: str | None = None) -> None:
        if config.requirements is None:
            raise ValueError(config.reason)
        self.agent = agent
        self.config = config
        self.requirements = config.requirements
        if state_path:
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        nonces = NonceStore(path=None if state_path is None else f"{state_path}.nonces")
        self.settlement_mode = "paper"
        if config.settlement == "chain":
            self.facilitator = self._chain_facilitator(nonces)
            self.settlement_mode = "chain"
        else:
            self.facilitator = PaperFacilitator(
                self.requirements,
                nonces=nonces,
                state_path=state_path,
            )

    def _chain_facilitator(self, nonces: NonceStore):
        """Build the broadcaster. Raises when the operator asked for chain mode without a sender."""
        if not self.config.facilitator_key:
            raise ValueError("SENTINEL_X402_SETTLEMENT=chain requires SENTINEL_X402_FACILITATOR_KEY")
        from web3 import Web3

        from .x402.facilitator import ChainFacilitator

        w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 20}))
        token = w3.eth.contract(
            address=Web3.to_checksum_address(self.requirements.asset), abi=X402_TOKEN_ABI
        )
        sender = w3.eth.account.from_key(self.config.facilitator_key).address
        facilitator = ChainFacilitator(
            self.requirements,
            web3=w3,
            token=token,
            sender=sender,
            nonces=nonces,
        )
        # A chain-connected verifier can also check that the payer holds the money; paper mode cannot.
        facilitator.verifier.balance_of = lambda who, asset: int(
            token.functions.balanceOf(Web3.to_checksum_address(who)).call()
        )
        # A local fork/test chain has an unlocked or key-funded sender; the transaction is signed here.
        facilitator.private_key = self.config.facilitator_key
        return facilitator

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
            "settlement_mode": self.settlement_mode,
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
