"""Settlement backends.

The x402 split we keep: verification decides whether a payment *would* settle, settlement is the
irreversible part. On HSK mainnet the irreversible part is forbidden for this project (the hackathon
brief says no mainnet gas spend, and a facilitator that spends it would be spending the operator's
money), so:

  * `PaperFacilitator` verifies for real (EIP-3009 signature, window, nonce) and writes a settlement
    receipt into a local ledger. `transaction` is prefixed `paper:` so nobody can mistake it for a
    chain hash.
  * `ChainFacilitator` exists as the adapter for a real deployment, and refuses chain 177 unless both
    the constructor flag and `SENTINEL_ALLOW_MAINNET_SPEND=1` are set. That is a code guard, not a
    comment: the demo cannot leak gas by being misconfigured.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from .types import PaymentPayload, PaymentRequirements, Settlement
from .verify import NonceStore, PaymentVerifier, VerifyResult

MAINNET_CHAIN_ID = 177


class SettlementError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class MainnetSpendForbidden(SettlementError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("mainnet_spend_forbidden", detail or "SPEND-01: no mainnet transaction in this project")


@dataclass
class Budget:
    """The client-side spending cap x402 puts on the wallet, not on the facilitator."""

    cap: int  # atomic units of the payment asset
    spent: int = 0

    def can_spend(self, amount: int) -> bool:
        return self.spent + amount <= self.cap

    def charge(self, amount: int) -> None:
        if not self.can_spend(amount):
            raise SettlementError("budget_exceeded", f"{self.spent}+{amount} > {self.cap}")
        self.spent += amount


@dataclass
class PaperFacilitator:
    requirements: PaymentRequirements
    nonces: NonceStore = field(default_factory=NonceStore)
    state_path: str | None = None
    receipts: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.verifier = PaymentVerifier(self.requirements, self.nonces)
        if self.state_path:
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    self.receipts = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError):
                self.receipts = []

    def _persist(self) -> None:
        if self.state_path:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(self.receipts, fh, indent=2)

    def verify(self, payload) -> VerifyResult:
        return self.verifier.verify(payload)

    def settle(self, payload, resource: str = "") -> Settlement:
        """Verify, then settle. A failed verification must not consume the nonce."""
        result = self.verifier.verify(payload)
        if not result.ok:
            return Settlement(
                success=False,
                transaction="",
                network=self.requirements.network,
                payer=result.payer,
                error_reason=result.reason,
            )
        auth = result.authorization
        assert auth is not None
        self.nonces.mark(auth.nonce)
        seed = f"{auth.from_.lower()}:{auth.nonce}:{auth.value}:{resource}:{time.time_ns()}".encode()
        receipt = {
            "id": "paper:" + hashlib.sha256(seed).hexdigest()[:40],
            "payer": result.payer,
            "payTo": self.requirements.pay_to,
            "amount": str(auth.value),
            "asset": self.requirements.asset,
            "network": self.requirements.network,
            "resource": resource,
            "settledAt": int(time.time()),
            "nonce": auth.nonce,
        }
        self.receipts.append(receipt)
        self._persist()
        return Settlement(
            success=True,
            transaction=receipt["id"],
            network=self.requirements.network,
            payer=result.payer,
        )


@dataclass
class ChainFacilitator:
    """The real broadcaster, kept unreachable on mainnet by design."""

    requirements: PaymentRequirements
    web3: object
    token: object  # web3 contract with `transferWithAuthorization`
    sender: str
    nonces: NonceStore = field(default_factory=NonceStore)
    allow_mainnet: bool = False

    def __post_init__(self) -> None:
        self.verifier = PaymentVerifier(self.requirements, self.nonces)

    def _refuse_mainnet(self) -> None:
        chain_id = int(self.web3.eth.chain_id)  # type: ignore[attr-defined]
        if chain_id != MAINNET_CHAIN_ID:
            return
        if self.allow_mainnet and os.environ.get("SENTINEL_ALLOW_MAINNET_SPEND") == "1":
            return
        raise MainnetSpendForbidden(
            "chain 177 settlement requires allow_mainnet=True and SENTINEL_ALLOW_MAINNET_SPEND=1"
        )

    def settle(self, payload, resource: str = "") -> Settlement:
        self._refuse_mainnet()
        p = payload if isinstance(payload, PaymentPayload) else PaymentPayload.from_dict(payload)
        result = self.verifier.verify(p)
        if not result.ok:
            return Settlement(False, "", self.requirements.network, result.payer, result.reason)
        auth = result.authorization
        assert auth is not None
        self.nonces.mark(auth.nonce)
        tx = self.token.functions.transferWithAuthorization(  # type: ignore[attr-defined]
            auth.from_,
            auth.to,
            int(auth.value),
            int(auth.valid_after),
            int(auth.valid_before),
            bytes.fromhex(auth.nonce.removeprefix("0x")),
            p.signature,
        ).transact({"from": self.sender})
        return Settlement(True, tx.hex(), self.requirements.network, result.payer)
