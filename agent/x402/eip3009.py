"""EIP-3009 `TransferWithAuthorization`, as x402's `exact` scheme uses it on EVM.

`scheme_exact_evm.md` says the client signs `transferWithAuthorization(from,to,value,validAfter,
validBefore,nonce)` in the token's own EIP-712 domain and the facilitator broadcasts it. The token's
domain name/version come from `PaymentRequired.accepts[].extra` because they are properties of the
token contract, not of x402.

Our HSK USDT does not implement EIP-3009, so this module is the *verification* layer: we can check a
client's authorization exactly as a facilitator would, and we refuse to pretend it settled on chain.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

TRANSFER_WITH_AUTHORIZATION_TYPE = (
    "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,"
    "uint256 validBefore,bytes32 nonce)"
)
EIP712_DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"

# The `exact` scheme is one-time-use money: replaying the same authorization must fail everywhere.
_AUTH_TYPEHASH = keccak(text=TRANSFER_WITH_AUTHORIZATION_TYPE)
_DOMAIN_TYPEHASH = keccak(text=EIP712_DOMAIN_TYPE)


@dataclass(frozen=True)
class Authorization:
    from_: str
    to: str
    value: int
    valid_after: int
    valid_before: int
    nonce: str  # 0x + 64 hex

    def as_dict(self) -> dict:
        """Field names are the x402 wire names; they are part of the protocol, not our style choice."""
        return {
            "from": self.from_,
            "to": self.to,
            "value": str(self.value),
            "validAfter": str(self.valid_after),
            "validBefore": str(self.valid_before),
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> "Authorization":
        return cls(
            from_=blob["from"],
            to=blob["to"],
            value=int(blob["value"]),
            valid_after=int(blob["validAfter"]),
            valid_before=int(blob["validBefore"]),
            nonce=_b32(blob["nonce"]),
        )


def _b32(value) -> str:
    text = value.hex() if isinstance(value, (bytes, bytearray)) else str(value)
    text = text.removeprefix("0x")
    if len(text) != 64:
        raise ValueError(f"nonce must be 32 bytes, got {len(text) // 2}")
    return "0x" + text.lower()


def domain_separator(name: str, version: str, chain_id: int, verifying_contract: str) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                _DOMAIN_TYPEHASH,
                keccak(text=name),
                keccak(text=version),
                chain_id,
                to_checksum_address(verifying_contract),
            ],
        )
    )


def struct_hash(auth: Authorization) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
            [
                _AUTH_TYPEHASH,
                to_checksum_address(auth.from_),
                to_checksum_address(auth.to),
                int(auth.value),
                int(auth.valid_after),
                int(auth.valid_before),
                bytes.fromhex(auth.nonce.removeprefix("0x")),
            ],
        )
    )


def authorization_digest(auth: Authorization, *, name: str, version: str, chain_id: int, token: str) -> bytes:
    return keccak(
        b"\x19\x01" + domain_separator(name, version, chain_id, token) + struct_hash(auth)
    )


def sign_authorization(
    auth: Authorization, private_key: str, *, name: str, version: str, chain_id: int, token: str
) -> str:
    signed = Account._sign_hash(
        authorization_digest(auth, name=name, version=version, chain_id=chain_id, token=token),
        private_key=private_key,
    )
    return "0x" + signed.signature.hex().removeprefix("0x")


def recover_authorization(
    auth: Authorization, signature: str, *, name: str, version: str, chain_id: int, token: str
) -> str:
    return Account._recover_hash(
        authorization_digest(auth, name=name, version=version, chain_id=chain_id, token=token),
        signature=signature,
    )
