"""ERC-8004 off-chain pieces: the registration file and the read-only registry client.

The Solidity side is tested in `test/ERC8004.t.sol`. What can go wrong here is the document shape
(a registration file that no indexer parses is a silent failure) and the ABI encoding used to read
reputation, so both are pinned to the spec text.
"""

import json
import os
import sys

import pytest
from eth_abi import encode as abi_encode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.registry8004 import (  # noqa: E402
    REGISTRATION_TYPE,
    RegistryReader,
    agent_registry_id,
    decode_get_summary,
    encode_get_summary,
    registration_file,
    registration_uri,
)

IDENTITY = "0x7B3f005fF9AAF4eAae2CF4c7A3343B2384383a83"
REPUTATION = "0x7319aF430CED91CEB56279af86b973B0C67c1B02"


def test_registry_id_follows_the_spec_string():
    assert agent_registry_id(177, IDENTITY) == f"eip155:177:{IDENTITY}"


def test_registration_file_has_every_mandatory_field():
    doc = registration_file(
        agent_id=1,
        identity_registry=IDENTITY,
        chain_id=177,
        name="Sentinel HSK",
        description="Policy-bound trading agent for HSKSwap",
    )
    assert doc["type"] == REGISTRATION_TYPE
    for field in ("name", "description", "image", "services", "x402Support", "active", "registrations"):
        assert field in doc, field
    assert doc["registrations"][0]["agentId"] == 1
    assert doc["registrations"][0]["agentRegistry"] == f"eip155:177:{IDENTITY}"
    assert doc["x402Support"] is True
    assert doc["active"] is True
    assert isinstance(doc["services"], list) and doc["services"]


def test_registration_uri_is_a_data_uri_that_round_trips():
    doc = registration_file(
        agent_id=7, identity_registry=IDENTITY, chain_id=177, name="n", description="d"
    )
    uri = registration_uri(doc)
    assert uri.startswith("data:application/json;base64,")
    import base64

    decoded = json.loads(base64.b64decode(uri.split(",", 1)[1]))
    assert decoded == doc


def test_get_summary_calldata_is_the_contract_selector_plus_arguments():
    from eth_utils import keccak

    data = encode_get_summary(1, [IDENTITY], "edgeCredibility", "")
    assert data.startswith("0x" + keccak(text="getSummary(uint256,address[],string,string)")[:4].hex())
    # decodable back into the same arguments
    from eth_abi import decode as abi_decode

    args = abi_decode(
        ["uint256", "address[]", "string", "string"], bytes.fromhex(data[10:])
    )
    assert args[0] == 1 and args[1][0].lower() == IDENTITY.lower() and args[2] == "edgeCredibility"


def test_decode_summary_reads_the_three_return_values():
    raw = "0x" + abi_encode(["uint64", "int128", "uint8"], [3, 8700, 0]).hex()
    assert decode_get_summary(raw) == (3, 8700, 0)


class _FakeRpc:
    def __init__(self, ret: bytes):
        self.ret = ret
        self.calls = []

    def call(self, to, data):
        self.calls.append((to, data))
        return "0x" + self.ret.hex()


def test_registry_reader_calls_the_reputation_contract_and_scales_the_result():
    rpc = _FakeRpc(abi_encode(["uint64", "int128", "uint8"], [2, 9500, 2]))
    reader = RegistryReader(rpc, IDENTITY, REPUTATION)
    view = reader.summary(1, [IDENTITY], "edgeCredibility")
    assert rpc.calls[0][0].lower() == REPUTATION.lower()
    assert view.count == 2
    assert view.as_dict()["scaled"] == pytest.approx(95.0)


def test_registry_reader_address_is_checksummed():
    rpc = _FakeRpc(abi_encode(["address"], ["0x" + "ab" * 20]))
    reader = RegistryReader(rpc, IDENTITY.lower(), REPUTATION.lower())
    owner = reader.owner_of(1)
    assert owner.lower() == "0x" + "ab" * 20
