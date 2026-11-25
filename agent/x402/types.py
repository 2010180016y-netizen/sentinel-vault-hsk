"""x402 v2 wire types.

Field names and shapes follow `specs/x402-specification-v2.md` exactly; they travel between two
processes that never share a language, so a rename here is a protocol break, not a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

X402_VERSION = 2


def network_for(chain_id: int) -> str:
    """x402 names networks `{namespace}:{chainId}`; HSK Chain is EVM, so `eip155:177`."""
    return f"eip155:{chain_id}"


@dataclass(frozen=True)
class Resource:
    url: str
    description: str = ""
    mime_type: str = "application/json"

    def as_dict(self) -> dict:
        return {"url": self.url, "description": self.description, "mimeType": self.mime_type}

    @classmethod
    def from_dict(cls, blob: dict | None) -> "Resource":
        blob = blob or {}
        return cls(
            url=blob.get("url", ""),
            description=blob.get("description", ""),
            mime_type=blob.get("mimeType", "application/json"),
        )


@dataclass(frozen=True)
class PaymentRequirements:
    scheme: str
    network: str
    amount: str  # atomic units, as a decimal string
    asset: str
    pay_to: str
    max_timeout_seconds: int = 60
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "amount": self.amount,
            "asset": self.asset,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, blob: dict) -> "PaymentRequirements":
        return cls(
            scheme=blob["scheme"],
            network=blob["network"],
            amount=str(blob["amount"]),
            asset=blob["asset"],
            pay_to=blob["payTo"],
            max_timeout_seconds=int(blob.get("maxTimeoutSeconds", 60)),
            extra=dict(blob.get("extra") or {}),
        )

    @property
    def chain_id(self) -> int:
        return int(self.network.split(":")[1])


@dataclass(frozen=True)
class PaymentRequired:
    resource: Resource
    accepts: list[PaymentRequirements]
    error: str = ""
    x402_version: int = X402_VERSION

    def as_dict(self) -> dict:
        out = {
            "x402Version": self.x402_version,
            "resource": self.resource.as_dict(),
            "accepts": [a.as_dict() for a in self.accepts],
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class PaymentPayload:
    accepted: PaymentRequirements
    authorization: dict  # raw x402 authorization dict (keeps from/value/validAfter names)
    signature: str
    resource: Resource = field(default_factory=lambda: Resource(url=""))
    x402_version: int = X402_VERSION
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        body = {
            "x402Version": self.x402_version,
            "resource": self.resource.as_dict(),
            "accepted": self.accepted.as_dict(),
            "payload": {"signature": self.signature, "authorization": dict(self.authorization)},
        }
        if self.extra:
            body["payload"].update(self.extra)
        return body

    @classmethod
    def from_dict(cls, blob: dict) -> "PaymentPayload":
        payload = blob.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        auth = payload.get("authorization") or payload.get("permit2Authorization")
        if not isinstance(auth, dict):
            raise ValueError("payload.authorization is missing")
        return cls(
            accepted=PaymentRequirements.from_dict(blob["accepted"]),
            authorization=auth,
            signature=str(payload.get("signature", "")),
            resource=Resource.from_dict(blob.get("resource")),
            x402_version=int(blob.get("x402Version", 0)),
            extra={k: v for k, v in payload.items() if k not in {"signature", "authorization", "permit2Authorization"}},
        )


@dataclass(frozen=True)
class Settlement:
    success: bool
    transaction: str
    network: str
    payer: str
    error_reason: str = ""

    def as_dict(self) -> dict:
        out = {
            "success": self.success,
            "transaction": self.transaction,
            "network": self.network,
            "payer": self.payer,
        }
        if self.error_reason:
            out["errorReason"] = self.error_reason
        return out
