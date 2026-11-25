"""EIP-712 intent construction and dual signing.

The type string is byte-identical to SentinelVault.INTENT_TYPEHASH. If the two ever drift, the
contract's signature check fails closed, which is the safe direction; tests/test_intent_parity.py
compares the locally computed digest with vault.hashIntent() on a fork.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any

from eth_account import Account
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

INTENT_TYPE = (
    "Intent(address tokenIn,address tokenOut,uint24 fee,uint128 amountIn,uint128 minAmountOut,"
    "uint16 declaredEdgeBps,bytes32 strategyHash,bytes32 parentId,uint64 nonce,uint64 deadline,"
    "uint64 maxNotionalQuote)"
)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
DOMAIN_NAME = "SentinelVault"
DOMAIN_VERSION = "1"


@dataclass
class Intent:
    token_in: str
    token_out: str
    fee: int
    amount_in: int
    min_amount_out: int
    declared_edge_bps: int
    strategy_hash: str
    parent_id: str = "0x" + "00" * 32
    nonce: int = 0
    deadline: int = 0
    max_notional_quote: int = 0

    def as_tuple(self) -> tuple:
        """Order matters: it is the Solidity struct order. bytes32 fields become bytes for eth_abi."""
        return (
            to_checksum_address(self.token_in),
            to_checksum_address(self.token_out),
            self.fee,
            self.amount_in,
            self.min_amount_out,
            self.declared_edge_bps,
            _b32(self.strategy_hash),
            _b32(self.parent_id),
            self.nonce,
            self.deadline,
            self.max_notional_quote,
        )

    def as_dict(self) -> dict:
        return asdict(self)


def _b32(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = str(value)
    if text.startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text.rjust(64, "0"))


def strategy_id(spec: dict) -> str:
    """Deterministic id for a strategy spec: sha-free canonical JSON -> keccak256 hex."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + keccak(canonical).hex()


def domain_separator(vault: str, chain_id: int) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                keccak(text=DOMAIN_TYPE),
                keccak(text=DOMAIN_NAME),
                keccak(text=DOMAIN_VERSION),
                chain_id,
                to_checksum_address(vault),
            ],
        )
    )


def struct_hash(it: Intent) -> bytes:
    return keccak(
        abi_encode(
            [
                "bytes32", "address", "address", "uint24", "uint128", "uint128", "uint16",
                "bytes32", "bytes32", "uint64", "uint64", "uint64",
            ],
            [keccak(text=INTENT_TYPE), *it.as_tuple()],
        )
    )


def digest(it: Intent, vault: str, chain_id: int) -> bytes:
    return keccak(b"\x19\x01" + domain_separator(vault, chain_id) + struct_hash(it))


def _hex_signature(signature) -> str:
    """Always `0x`-prefixed. `HexBytes.hex()` dropped the prefix in eth-account 0.13, and every caller
    that did `sig[2:]` then chopped the first byte of the signature — a 64-byte blob that recovers to
    nobody, so the live executor reverted `BadStrategySignature`. The unit tests signed and recovered
    with the same helper and never noticed; the fork run did."""
    text = signature.hex() if hasattr(signature, "hex") else str(signature)
    return text if text.startswith("0x") else "0x" + text


def sign(it: Intent, private_key: str, vault: str, chain_id: int) -> str:
    signed = Account._sign_hash(digest(it, vault, chain_id), private_key=private_key)
    return _hex_signature(signed.signature)


def recover(it: Intent, signature: str, vault: str, chain_id: int) -> str:
    return Account._recover_hash(digest(it, vault, chain_id), signature=signature)


def dual_sign(it: Intent, strategy_key: str, risk_key: str, vault: str, chain_id: int) -> tuple[str, str]:
    """Two independent keys must sign the same digest. Neither can move funds alone."""
    return sign(it, strategy_key, vault, chain_id), sign(it, risk_key, vault, chain_id)


def encode_execute_calldata(intent: Intent, strategy_sig: str, risk_sig: str) -> bytes:
    """ABI-encode `executeIntent(Intent,(...))` calldata without needing the full contract ABI."""
    from eth_utils import function_signature_to_4byte_selector

    sig = (
        "executeIntent((address,address,uint24,uint128,uint128,uint16,bytes32,bytes32,uint64,uint64,uint64),"
        "bytes,bytes)"
    )
    selector = keccak(text=sig)[:4]
    args = abi_encode(
        [
            "(address,address,uint24,uint128,uint128,uint16,bytes32,bytes32,uint64,uint64,uint64)",
            "bytes",
            "bytes",
        ],
        [intent.as_tuple(), bytes.fromhex(strategy_sig.removeprefix("0x")), bytes.fromhex(risk_sig.removeprefix("0x"))],
    )
    return selector + args
