"""The paying side: an agent that buys a resource and can prove it paid.

The client keeps its own budget (x402 places the spending limit on the wallet, not on the server), and
signs an EIP-3009 authorization for exactly the amount the challenge asks. A challenge it cannot
afford, or does not understand, is refused before anything is signed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from eth_account import Account

from .eip3009 import Authorization, sign_authorization
from .facilitator import Budget
from .service import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_header,
    encode_header,
)
from .types import PaymentPayload, PaymentRequirements, Resource, Settlement


class X402Error(Exception):
    pass


@dataclass
class FetchResult:
    ok: bool
    status: int
    data: dict | None = None
    settlement: Settlement | None = None
    paid: int = 0
    error: str = ""
    headers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "paid": self.paid,
            "settlement": self.settlement.as_dict() if self.settlement else None,
            "error": self.error,
            "data": self.data,
        }


@dataclass
class X402Client:
    private_key: str
    chain_id: int = 177
    budget: Budget | None = None
    timeout_s: int = 15
    payer: str = field(default="")

    def __post_init__(self) -> None:
        self.payer = Account.from_key(self.private_key).address

    # ------------------------------------------------------------------ api

    def pay_and_fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        max_amount: int | None = None,
        headers: dict | None = None,
    ) -> FetchResult:
        first = self._request(url, method, body, headers or {}, None)
        if first.status != 402:
            return first

        challenge = self._challenge(first)
        if challenge is None:
            return FetchResult(False, 402, error="402 without a decodable PAYMENT-REQUIRED header")

        accepted = self._select(challenge, max_amount)
        payload = self._sign(accepted, challenge)
        paid = int(accepted.amount)
        if self.budget is not None:
            self.budget.charge(paid)

        second = self._request(
            url,
            method,
            body,
            headers or {},
            encode_header(payload.as_dict()),
        )
        if second.status == 200:
            second.paid = paid
            return second
        # Settled-but-refused: report the server's reason, and never claim success because we signed.
        return FetchResult(
            False,
            second.status,
            data=second.data,
            settlement=second.settlement,
            paid=paid,
            error=second.error or "payment not accepted",
        )

    # ------------------------------------------------------------------ internals

    def _request(self, url: str, method: str, body: dict | None, headers: dict, payment: str | None) -> FetchResult:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        if payment:
            req.add_header(PAYMENT_SIGNATURE_HEADER, payment)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = _read_json(resp.read())
                return FetchResult(
                    ok=200 <= resp.status < 300,
                    status=resp.status,
                    data=payload,
                    settlement=self._settlement(resp.headers.get(PAYMENT_RESPONSE_HEADER, "")),
                    headers={k.upper(): v for k, v in resp.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            payload = _read_json(exc.read())
            return FetchResult(
                ok=False,
                status=exc.code,
                data=payload,
                settlement=self._settlement(exc.headers.get(PAYMENT_RESPONSE_HEADER, "") if exc.headers else ""),
                error=str((payload or {}).get("error", "")) if isinstance(payload, dict) else "",
                headers={k.upper(): v for k, v in (exc.headers.items() if exc.headers else [])},
            )
        except urllib.error.URLError as exc:
            return FetchResult(False, 0, error=f"network: {exc.reason}")

    @staticmethod
    def _challenge(result: FetchResult) -> dict | None:
        """The canonical location is the `PAYMENT-REQUIRED` header; a JSON body copy is a fallback."""
        header = result.headers.get(PAYMENT_REQUIRED_HEADER) if result.headers else None
        if header:
            try:
                return decode_header(header)
            except ValueError:
                pass
        if isinstance(result.data, dict) and "accepts" in result.data:
            return result.data
        return None

    def _select(self, challenge: dict, max_amount: int | None) -> PaymentRequirements:
        options = [PaymentRequirements.from_dict(a) for a in challenge.get("accepts", [])]
        for opt in options:
            if opt.chain_id != self.chain_id:
                continue
            amount = int(opt.amount)
            if max_amount is not None and amount > max_amount:
                continue
            if self.budget is not None and not self.budget.can_spend(amount):
                continue
            return opt
        raise X402Error("no acceptable payment option for this network/budget")

    def _sign(self, req: PaymentRequirements, challenge: dict) -> PaymentPayload:
        extra = req.extra or {}
        name = str(extra.get("name", ""))
        version = str(extra.get("version", ""))
        if not name or not version:
            raise X402Error("server did not send accepts[].extra.name/version (EIP-712 domain of the token)")
        now = int(time.time())
        auth = Authorization(
            from_=self.payer,
            to=req.pay_to,
            value=int(req.amount),
            valid_after=now - 60,
            valid_before=now + max(30, int(req.max_timeout_seconds)),
            nonce="0x" + os.urandom(32).hex(),
        )
        signature = sign_authorization(
            auth, self.private_key, name=name, version=version, chain_id=req.chain_id, token=req.asset
        )
        resource = Resource.from_dict(challenge.get("resource"))
        return PaymentPayload(accepted=req, authorization=auth.as_dict(), signature=signature, resource=resource)

    @staticmethod
    def _settlement(value: str) -> Settlement | None:
        if not value:
            return None
        try:
            blob = decode_header(value)
        except ValueError:
            return None
        return Settlement(
            success=bool(blob.get("success")),
            transaction=str(blob.get("transaction", "")),
            network=str(blob.get("network", "")),
            payer=str(blob.get("payer", "")),
            error_reason=str(blob.get("errorReason", "")),
        )


def _read_json(raw: bytes) -> dict | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
