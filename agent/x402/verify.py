"""Payment verification: the five checks `scheme_exact_evm.md` lists, in order.

Nothing here touches the chain. Balance and simulation checks (steps 2 and 5 of the spec) are exposed
as callbacks so a chain-connected facilitator can supply them, and omitted in paper mode rather than
guessed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .eip3009 import Authorization, recover_authorization
from .types import PaymentPayload, PaymentRequirements

# The reason strings are part of our own error contract (returned to the client in `errorReason`).
MALFORMED = "malformed_payload"
VERSION = "unsupported_version"
SCHEME = "unsupported_scheme"
NETWORK = "network_mismatch"
ASSET = "asset_mismatch"
PAY_TO = "pay_to_mismatch"
AMOUNT = "amount_mismatch"
SIGNATURE = "invalid_signature"
NOT_YET_VALID = "not_yet_valid"
EXPIRED = "expired"
NONCE_REUSE = "nonce_already_used"
INSUFFICIENT_FUNDS = "insufficient_funds"
UNSUPPORTED_TRANSFER_METHOD = "unsupported_transfer_method"


@dataclass
class NonceStore:
    """Single-use nonces. In memory by default; `path` makes the record survive a restart."""

    seen: set[str] = field(default_factory=set)
    path: str | None = None

    def __post_init__(self) -> None:
        if self.path:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            self.seen.add(line)
            except FileNotFoundError:
                pass

    def used(self, nonce: str) -> bool:
        return nonce.lower() in self.seen

    def mark(self, nonce: str) -> None:
        key = nonce.lower()
        self.seen.add(key)
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    payer: str = ""
    authorization: Authorization | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "payer": self.payer}


class PaymentVerifier:
    """Checks one `PaymentPayload` against the requirement the server is asking for.

    `verify` is intentionally side-effect free (it does not consume the nonce); `settle` consumes it.
    That split is what "verify-then-settle" means: a payload that fails verification must leave the
    nonce usable, and a payload that succeeds must not be usable twice.
    """

    def __init__(
        self,
        requirements: PaymentRequirements,
        nonce_store: NonceStore | None = None,
        *,
        now: Callable[[], float] = time.time,
        balance_of: Callable[[str, str], int] | None = None,
        allowed_transfer_methods: tuple[str, ...] = ("eip3009",),
    ) -> None:
        self.requirements = requirements
        self.nonces = nonce_store or NonceStore()
        self.now = now
        self.balance_of = balance_of
        self.allowed_transfer_methods = allowed_transfer_methods

    # ------------------------------------------------------------------ helpers

    def _token_domain(self, accepted: PaymentRequirements) -> tuple[str, str]:
        extra = accepted.extra or {}
        name = str(extra.get("name", ""))
        version = str(extra.get("version", ""))
        if not name or not version:
            raise ValueError("accepts.extra.name and .version are required for eip3009")
        return name, version

    # ------------------------------------------------------------------ verify

    def verify(self, payload: PaymentPayload | dict) -> VerifyResult:
        try:
            p = payload if isinstance(payload, PaymentPayload) else PaymentPayload.from_dict(payload)
        except (KeyError, ValueError, TypeError):
            return VerifyResult(False, MALFORMED)

        req = self.requirements
        acc = p.accepted

        if p.x402_version != 2:
            return VerifyResult(False, VERSION)
        if acc.scheme != "exact":
            return VerifyResult(False, SCHEME)
        method = str((acc.extra or {}).get("assetTransferMethod", "eip3009"))
        if method not in self.allowed_transfer_methods:
            return VerifyResult(False, UNSUPPORTED_TRANSFER_METHOD)
        if acc.network != req.network:
            return VerifyResult(False, NETWORK)
        if acc.asset.lower() != req.asset.lower():
            return VerifyResult(False, ASSET)
        if acc.pay_to.lower() != req.pay_to.lower():
            return VerifyResult(False, PAY_TO)

        try:
            auth = Authorization.from_dict(p.authorization)
        except (KeyError, ValueError, TypeError):
            return VerifyResult(False, MALFORMED)
        if auth.to.lower() != req.pay_to.lower():
            return VerifyResult(False, PAY_TO)

        # `exact` means exact: the signed value is the price, not an upper bound.
        if int(auth.value) != int(req.amount) or int(acc.amount) != int(req.amount):
            return VerifyResult(False, AMOUNT)

        try:
            name, version = self._token_domain(acc)
            signer = recover_authorization(
                auth,
                p.signature,
                name=name,
                version=version,
                chain_id=acc.chain_id,
                token=acc.asset,
            )
        except (ValueError, TypeError):
            return VerifyResult(False, SIGNATURE)
        if signer.lower() != auth.from_.lower():
            return VerifyResult(False, SIGNATURE)

        now = int(self.now())
        if now < int(auth.valid_after):
            return VerifyResult(False, NOT_YET_VALID, signer, auth)
        if now >= int(auth.valid_before):
            return VerifyResult(False, EXPIRED, signer, auth)
        if self.nonces.used(auth.nonce):
            return VerifyResult(False, NONCE_REUSE, signer, auth)

        if self.balance_of is not None:
            try:
                balance = self.balance_of(auth.from_, acc.asset)
            except Exception:  # noqa: BLE001 - an unavailable balance must not become "paid"
                return VerifyResult(False, INSUFFICIENT_FUNDS, signer, auth)
            if balance < int(auth.value):
                return VerifyResult(False, INSUFFICIENT_FUNDS, signer, auth)

        return VerifyResult(True, "", signer, auth)
