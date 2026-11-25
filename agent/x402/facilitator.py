"""Settlement backends.

The x402 split we keep: verification decides whether a payment *would* settle, settlement is the
irreversible part. On HSK mainnet the irreversible part is forbidden for this project (the hackathon
brief says no mainnet gas spend, and a facilitator that spends it would be spending the operator's
money), so:

  * `PaperFacilitator` verifies for real (EIP-3009 signature, window, nonce) and writes a settlement
    receipt into a local ledger. `transaction` is prefixed `paper:` so nobody can mistake it for a
    chain hash.
  * `ChainFacilitator` broadcasts the signed `transferWithAuthorization` and returns the real
    transaction hash. It refuses chain 177 unless one of these is true: (a) both `allow_mainnet` and
    `SENTINEL_ALLOW_MAINNET_SPEND=1` are set (a deliberate opt-in), or (b) the RPC is a loopback
    address, where a transaction cannot spend real funds. That is a code guard, not a comment: the
    demo cannot leak gas by being misconfigured, and a local fork can still settle for real.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .types import PaymentPayload, PaymentRequirements, Settlement
from .verify import NonceStore, PaymentVerifier, VerifyResult

MAINNET_CHAIN_ID = 177


def hex_hash(value) -> str:
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else "0x" + text
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


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
    """The real broadcaster, unreachable on mainnet unless deliberately armed."""

    requirements: PaymentRequirements
    web3: object
    token: object  # web3 contract with `transferWithAuthorization`
    sender: str
    nonces: NonceStore = field(default_factory=NonceStore)
    allow_mainnet: bool = False
    private_key: str = ""

    def __post_init__(self) -> None:
        self.verifier = PaymentVerifier(self.requirements, self.nonces)

    # ------------------------------------------------------------------ guards

    def rpc_is_local(self) -> bool:
        try:
            provider = self.web3.provider  # type: ignore[attr-defined]
            uri = getattr(provider, "endpoint_uri", "") or ""
        except Exception:  # noqa: BLE001 - an unknown provider is treated as remote
            return False
        host = (urlparse(uri).hostname or "").lower()
        return host in _LOCAL_HOSTS

    def _refuse_mainnet(self) -> None:
        chain_id = int(self.web3.eth.chain_id)  # type: ignore[attr-defined]
        if chain_id != MAINNET_CHAIN_ID:
            return
        if self.allow_mainnet and os.environ.get("SENTINEL_ALLOW_MAINNET_SPEND") == "1":
            return
        if self.rpc_is_local():
            return  # a loopback node cannot spend real funds
        raise MainnetSpendForbidden(
            "chain 177 settlement requires allow_mainnet=True and SENTINEL_ALLOW_MAINNET_SPEND=1 "
            "(or a loopback RPC)"
        )

    # ------------------------------------------------------------------ settle

    def _broadcast(self, fn):
        if not self.private_key:
            return fn.transact({"from": self.sender})
        w3 = self.web3  # type: ignore[attr-defined]
        account = w3.eth.account.from_key(self.private_key)
        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 200_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": int(w3.eth.chain_id),
            }
        )
        signed = account.sign_transaction(tx)
        return w3.eth.send_raw_transaction(signed.raw_transaction)

    def settle(self, payload, resource: str = "") -> Settlement:
        self._refuse_mainnet()
        p = payload if isinstance(payload, PaymentPayload) else PaymentPayload.from_dict(payload)
        result = self.verifier.verify(p)
        if not result.ok:
            return Settlement(False, "", self.requirements.network, result.payer, result.reason)
        auth = result.authorization
        assert auth is not None
        # Mark first: a revert below must still not allow the same authorization to be retried forever.
        self.nonces.mark(auth.nonce)
        fn = self.token.functions.transferWithAuthorization(  # type: ignore[attr-defined]
            auth.from_,
            auth.to,
            int(auth.value),
            int(auth.valid_after),
            int(auth.valid_before),
            bytes.fromhex(auth.nonce.removeprefix("0x")),
            bytes.fromhex(str(p.signature).removeprefix("0x")),
        )
        tx = self._broadcast(fn)
        receipt = self.web3.eth.wait_for_transaction_receipt(tx, timeout=120)  # type: ignore[attr-defined]
        if int(receipt.get("status", 0)) != 1:
            return Settlement(False, hex_hash(tx), self.requirements.network, result.payer, "settlement_reverted")
        return Settlement(True, hex_hash(tx), self.requirements.network, result.payer)
