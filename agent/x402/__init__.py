"""x402 v2 (HTTP 402 agent payments) — adopted from `coinbase/x402`.

Only the parts we can make true here are implemented: the wire format of the HTTP transport
(`PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE`), the EIP-3009 authorization and its
EIP-712 signature, and the verify-then-settle order with single-use nonces. Settlement on HSK mainnet
is deliberately *not* available in this project — the hackathon rules forbid mainnet gas spend — so
the facilitator's settlement backend is a paper ledger and the chain backend refuses chain 177 unless
an operator explicitly overrides it (`agent/x402/facilitator.py`).

    spec:  oss/coinbase_x402/specs/x402-specification-v2.md
           oss/coinbase_x402/specs/schemes/exact/scheme_exact_evm.md
           oss/coinbase_x402/specs/transports-v2/http.md
"""

from .client import FetchResult, X402Client, X402Error
from .eip3009 import Authorization, authorization_digest, recover_authorization, sign_authorization
from .facilitator import Budget, ChainFacilitator, MainnetSpendForbidden, PaperFacilitator, SettlementError
from .service import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_header,
    encode_header,
    payment_required,
)
from .types import X402_VERSION, PaymentPayload, PaymentRequirements, Resource, Settlement
from .verify import NONCE_REUSE, NonceStore, PaymentVerifier, VerifyResult

__all__ = [
    "X402_VERSION",
    "Authorization",
    "Budget",
    "ChainFacilitator",
    "FetchResult",
    "MainnetSpendForbidden",
    "NONCE_REUSE",
    "NonceStore",
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "PAYMENT_SIGNATURE_HEADER",
    "PaperFacilitator",
    "PaymentPayload",
    "PaymentRequirements",
    "PaymentVerifier",
    "Resource",
    "Settlement",
    "SettlementError",
    "VerifyResult",
    "X402Client",
    "X402Error",
    "authorization_digest",
    "decode_header",
    "encode_header",
    "payment_required",
    "recover_authorization",
    "sign_authorization",
]
