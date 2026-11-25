"""Server side of the x402 HTTP transport: headers in, headers out.

`transports-v2/http.md`: the 402 carries a base64 `PaymentRequired` in `PAYMENT-REQUIRED`, the client
answers with a base64 `PaymentPayload` in `PAYMENT-SIGNATURE`, and the server reports the outcome in
`PAYMENT-RESPONSE`. Bodies are the server's business; the protocol lives in the headers.
"""

from __future__ import annotations

import base64
import binascii
import json

from .types import PaymentRequired, PaymentRequirements, Resource, Settlement

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


def encode_header(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(raw).decode()


def decode_header(value: str) -> dict:
    value = (value or "").strip()
    if not value:
        raise ValueError("empty x402 header")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"x402 header is not valid base64: {exc}") from exc
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"x402 header is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("x402 header must decode to an object")
    return obj


def payment_required(
    requirements: PaymentRequirements | list[PaymentRequirements],
    resource: Resource,
    error: str = "",
) -> dict:
    accepts = [requirements] if isinstance(requirements, PaymentRequirements) else list(requirements)
    return PaymentRequired(resource=resource, accepts=accepts, error=error).as_dict()


def settlement_header(settlement: Settlement) -> str:
    return encode_header(settlement.as_dict())
