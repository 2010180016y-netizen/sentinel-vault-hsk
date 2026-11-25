"""x402: signature parity, verifier reasons, settlement semantics, and a real HTTP round trip.

The last test matters most: a client and a server that have never shared anything but the spec talk
over a socket, one pays, the other verifies and settles, both sides end up agreeing on the receipt.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eth_account import Account

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.x402 import (  # noqa: E402
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    Authorization,
    Budget,
    ChainFacilitator,
    MainnetSpendForbidden,
    NonceStore,
    PaperFacilitator,
    PaymentRequirements,
    PaymentVerifier,
    Resource,
    X402Client,
    X402Error,
    authorization_digest,
    decode_header,
    encode_header,
    payment_required,
    recover_authorization,
    sign_authorization,
)
from agent.x402.types import PaymentPayload  # noqa: E402

TOKEN = "0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029"
PAY_TO = "0x7B3f005fF9AAF4eAae2CF4c7A3343B2384383a83"
DOMAIN = {"name": "USDT", "version": "1"}
CHAIN = 177
NOW = int(__import__("time").time())


def requirements(amount: int = 1000, network: str = "eip155:177") -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network=network,
        amount=str(amount),
        asset=TOKEN,
        pay_to=PAY_TO,
        max_timeout_seconds=60,
        extra={"assetTransferMethod": "eip3009", **DOMAIN},
    )


def signed_payload(key: str, amount: int = 1000, *, now: int = NOW, valid_before: int | None = None, **over) -> dict:
    auth = Authorization(
        from_=Account.from_key(key).address,
        to=over.get("to", PAY_TO),
        value=over.get("value", amount),
        valid_after=over.get("valid_after", now - 60),
        valid_before=valid_before if valid_before is not None else now + 30,
        nonce=over.get("nonce", "0x" + "11" * 32),
    )
    sig = sign_authorization(
        auth, key, name=DOMAIN["name"], version=DOMAIN["version"], chain_id=over.get("chain_id", CHAIN), token=TOKEN
    )
    return PaymentPayload(
        accepted=over.get("accepted", requirements(amount)),
        authorization=auth.as_dict(),
        signature=sig,
        resource=Resource(url="http://127.0.0.1/premium"),
    ).as_dict()


@pytest.fixture
def key() -> str:
    return "0x" + "42" * 32


# ------------------------------------------------------------------ signatures

def test_eip3009_signature_roundtrip(key):
    auth = Authorization(Account.from_key(key).address, PAY_TO, 1000, NOW - 60, NOW + 30, "0x" + "ab" * 32)
    sig = sign_authorization(auth, key, chain_id=CHAIN, token=TOKEN, **DOMAIN)
    assert recover_authorization(auth, sig, chain_id=CHAIN, token=TOKEN, **DOMAIN).lower() == auth.from_.lower()


def test_digest_matches_an_independent_eip712_implementation(key):
    """eth_account's generic typed-data encoder is implementation #2 for the same bytes."""
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    auth = Authorization(Account.from_key(key).address, PAY_TO, 1000, NOW - 60, NOW + 30, "0x" + "cd" * 32)
    ours = authorization_digest(auth, chain_id=CHAIN, token=TOKEN, **DOMAIN)

    reference = encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {"name": DOMAIN["name"], "version": DOMAIN["version"], "chainId": CHAIN, "verifyingContract": TOKEN},
            "message": {
                "from": auth.from_,
                "to": auth.to,
                "value": auth.value,
                "validAfter": auth.valid_after,
                "validBefore": auth.valid_before,
                "nonce": bytes.fromhex(auth.nonce[2:]),
            },
        }
    )
    # eth_account exposes the domain separator as `header`; the EIP-712 prefix is ours to add.
    assert ours == keccak(b"\x19\x01" + reference.header + reference.body)


def test_tampering_with_the_signature_payload_breaks_it(key):
    """Change a field the requirement check does not look at (the nonce), so only the signature can catch it."""
    payload = signed_payload(key)
    payload["payload"]["authorization"]["nonce"] = "0x" + "77" * 32
    v = PaymentVerifier(requirements(), now=lambda: NOW)
    assert v.verify(payload).reason == "invalid_signature"


# ------------------------------------------------------------------ verifier reasons

def test_verifier_accepts_a_well_formed_payment(key):
    v = PaymentVerifier(requirements(), now=lambda: NOW)
    result = v.verify(signed_payload(key))
    assert result.ok, result.reason
    assert result.payer.lower() == Account.from_key(key).address.lower()


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"value": 5}, "amount_mismatch"),
        ({"to": "0x" + "99" * 20}, "pay_to_mismatch"),
        ({"valid_after": NOW + 100, "valid_before": NOW + 200}, "not_yet_valid"),
        ({"valid_before": NOW - 1}, "expired"),
    ],
)
def test_verifier_names_what_is_wrong(key, over, expected):
    payload = signed_payload(key, now=NOW, **over)
    v = PaymentVerifier(requirements(), now=lambda: NOW)
    assert v.verify(payload).reason == expected


def test_nonce_is_single_use(key):
    payload = signed_payload(key)
    facilitator = PaperFacilitator(requirements())
    first = facilitator.settle(payload, resource="/premium")
    assert first.success
    second = facilitator.settle(payload, resource="/premium")
    assert not second.success and second.error_reason == "nonce_already_used"


def test_failed_verification_does_not_consume_the_nonce(key):
    facilitator = PaperFacilitator(requirements())
    bad = signed_payload(key, value=5)
    assert not facilitator.settle(bad).success
    good = signed_payload(key)  # same nonce, correct amount
    assert facilitator.settle(good).success, "the nonce must still be usable after a failed payment"


def test_paper_settlement_is_labelled_as_paper(key):
    facilitator = PaperFacilitator(requirements())
    settlement = facilitator.settle(signed_payload(key), resource="/premium")
    assert settlement.success and settlement.transaction.startswith("paper:")
    assert facilitator.receipts and facilitator.receipts[0]["amount"] == "1000"


def test_wrong_network_and_scheme_are_rejected(key):
    payload = signed_payload(key)
    payload["accepted"]["network"] = "eip155:1"
    assert PaymentVerifier(requirements(), now=lambda: NOW).verify(payload).reason == "network_mismatch"
    other = signed_payload(key, nonce="0x" + "22" * 32)
    other["accepted"]["scheme"] = "upto"
    assert PaymentVerifier(requirements(), now=lambda: NOW).verify(other).reason == "unsupported_scheme"


def test_unsupported_transfer_method_is_rejected(key):
    payload = signed_payload(key)
    payload["accepted"]["extra"]["assetTransferMethod"] = "permit2"
    assert PaymentVerifier(requirements(), now=lambda: NOW).verify(payload).reason == "unsupported_transfer_method"


def test_nonce_store_survives_a_restart(tmp_path):
    path = str(tmp_path / "nonces.txt")
    store = NonceStore(path=path)
    store.mark("0x" + "33" * 32)
    again = NonceStore(path=path)
    assert again.used("0x" + "33" * 32)


# ------------------------------------------------------------------ mainnet guard

class _FakeEth:
    def __init__(self, chain_id: int):
        self.chain_id = chain_id

    def wait_for_transaction_receipt(self, tx, timeout=120):
        return {"status": 1, "transactionHash": tx}


class _FakeWeb3:
    def __init__(self, chain_id: int):
        self.eth = _FakeEth(chain_id)

    def wait_for_transaction_receipt(self, tx, timeout=120):
        return {"status": 1, "transactionHash": tx}


class _FakeToken:
    def __init__(self):
        self.calls = 0
        self.functions = self

    def transferWithAuthorization(self, *args):
        self.calls += 1

        class _Tx:
            def transact(self, opts):
                return b"\x01" * 32

        return _Tx()


def test_chain_settlement_refuses_hsk_mainnet(key):
    facilitator = ChainFacilitator(requirements(), web3=_FakeWeb3(177), token=_FakeToken(), sender=PAY_TO)
    with pytest.raises(MainnetSpendForbidden):
        facilitator.settle(signed_payload(key))


def test_chain_settlement_can_run_on_a_local_chain(key):
    token = _FakeToken()
    facilitator = ChainFacilitator(requirements(), web3=_FakeWeb3(31337), token=token, sender=PAY_TO)
    settlement = facilitator.settle(signed_payload(key))
    assert settlement.success and token.calls == 1


def test_budget_is_a_client_side_cap():
    budget = Budget(cap=1500)
    assert budget.can_spend(1000)
    budget.charge(1000)
    assert not budget.can_spend(1000)


# ------------------------------------------------------------------ end-to-end over HTTP

class _PaidHandler(BaseHTTPRequestHandler):
    facilitator: PaperFacilitator
    payload_body = {"premium": "risk feed", "hurdle_bps": 146}

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict, headers: dict | None = None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        header = self.headers.get(PAYMENT_SIGNATURE_HEADER, "")
        resource = Resource(url=f"http://{self.headers.get('Host')}/premium", description="premium")
        if not header:
            body = payment_required(self.facilitator.requirements, resource, "PAYMENT-SIGNATURE header is required")
            return self._send(402, body, {PAYMENT_REQUIRED_HEADER: encode_header(body)})
        settlement = self.facilitator.settle(decode_header(header), resource=resource.url)
        ph = {PAYMENT_RESPONSE_HEADER: encode_header(settlement.as_dict())}
        if not settlement.success:
            body = payment_required(self.facilitator.requirements, resource, settlement.error_reason)
            ph[PAYMENT_REQUIRED_HEADER] = encode_header(body)
            return self._send(402, body, ph)
        return self._send(200, self.payload_body, ph)


@pytest.fixture
def paid_server():
    _PaidHandler.facilitator = PaperFacilitator(requirements())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PaidHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/premium"
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_pays_a_server_it_has_never_met(paid_server, key):
    client = X402Client(key, chain_id=CHAIN, budget=Budget(cap=5_000))
    result = client.pay_and_fetch(paid_server)
    assert result.ok and result.status == 200, result.as_dict()
    assert result.data["premium"] == "risk feed"
    assert result.paid == 1000
    assert result.settlement and result.settlement.success
    assert result.settlement.transaction.startswith("paper:")


def test_the_same_payment_header_cannot_be_replayed(paid_server, key):
    client = X402Client(key, chain_id=CHAIN, budget=Budget(cap=50_000))
    payload = client._sign(
        requirements(), {"accepts": [requirements().as_dict()], "resource": {"url": paid_server}}
    )
    first = _PaidHandler.facilitator.settle(payload.as_dict(), resource=paid_server)
    assert first.success
    replay = _PaidHandler.facilitator.settle(payload.as_dict(), resource=paid_server)
    assert not replay.success and replay.error_reason == "nonce_already_used"


def test_client_refuses_a_price_above_its_budget(paid_server, key):
    client = X402Client(key, chain_id=CHAIN, budget=Budget(cap=500))
    with pytest.raises(X402Error):
        client.pay_and_fetch(paid_server)


def test_client_does_not_pay_on_a_free_endpoint(key):
    calls = []

    class _Free(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"free": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            calls.append(1)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Free)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = X402Client(key, chain_id=CHAIN, budget=Budget(cap=5_000))
        res = client.pay_and_fetch(f"http://127.0.0.1:{srv.server_address[1]}/free")
        assert res.ok and res.paid == 0 and res.data == {"free": True}
        assert client.budget.spent == 0
    finally:
        srv.shutdown()
        srv.server_close()
