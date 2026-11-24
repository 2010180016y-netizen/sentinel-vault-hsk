"""EIP-712 parity against the real contract, on a local chain.

`agent/intent.py` builds the digest off-chain; `SentinelVault.hashIntent` builds it on-chain from the
Solidity type string. If the two ever drift (a field added on one side, a type widened, an address
checksummed differently) every signature fails — the safe direction, but a silent one. This test deploys
the compiled contract to a throwaway `anvil` instance, calls `hashIntent`, and compares it with the
Python digest byte for byte. Skips (loudly) when forge/anvil or the build artifacts are unavailable.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import intent as intent_mod  # noqa: E402

FORGE = shutil.which("forge") or os.path.expanduser("~/.foundry/bin/forge")
ANVIL = shutil.which("anvil") or os.path.expanduser("~/.foundry/bin/anvil")
ARTIFACTS = {
    "vault": os.path.join(ROOT, "out", "SentinelVault.sol", "SentinelVault.json"),
    "erc20": os.path.join(ROOT, "out", "MockERC20.sol", "MockERC20.json"),
    "factory": os.path.join(ROOT, "out", "MockFactory.sol", "MockFactory.json"),
    "router": os.path.join(ROOT, "out", "MockSwapRouter02.sol", "MockSwapRouter02.json"),
}
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # anvil #0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _art(name: str) -> dict:
    with open(ARTIFACTS[name], encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def chain():
    if not (os.path.exists(FORGE) and os.path.exists(ANVIL)):
        pytest.skip("forge/anvil not installed")
    if not all(os.path.exists(p) for p in ARTIFACTS.values()):
        pytest.skip("build artifacts missing (run `forge build` first)")

    from web3 import Web3

    port = _free_port()
    proc = subprocess.Popen(
        [ANVIL, "--port", str(port), "--chain-id", "177", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
    for _ in range(100):
        try:
            w3.eth.block_number
            break
        except Exception:  # noqa: BLE001 - anvil is still starting
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("anvil did not start")
    try:
        yield w3
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _deploy(w3, art: dict, args: list | None = None) -> str:
    acct = w3.eth.account.from_key(DEPLOYER_KEY)
    data = art["bytecode"]["object"]
    if args:
        data += w3.codec.encode([a[0] for a in args], [a[1] for a in args]).hex()
    tx = {
        "from": acct.address,
        "data": data,
        "gas": 6_000_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": 177,
    }
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=30)
    assert rcpt["status"] == 1
    return rcpt["contractAddress"]


def _policy_tuple() -> tuple:
    return (100, 146, 500, 1500, 600, 60, 4, 200_000_000, 800_000_000)


def test_python_digest_equals_vault_hashintent(chain):
    w3 = chain
    quote = _deploy(w3, _art("erc20"), [("string", "Tether USD"), ("string", "USDT"), ("uint8", 6)])
    asset = _deploy(w3, _art("erc20"), [("string", "Wrapped HSK"), ("string", "WHSK"), ("uint8", 18)])
    factory = _deploy(w3, _art("factory"))
    router = _deploy(w3, _art("router"))
    owner = "0x000000000000000000000000000000000000FEE1"
    strat = "0x00000000000000000000000000000000000A11CE"
    risk = "0x0000000000000000000000000000000000000B0B"
    vault = _deploy(
        w3,
        _art("vault"),
        [
            ("address", owner),
            ("address", router),
            ("address", factory),
            ("address", quote),
            ("address", strat),
            ("address", risk),
            ("(uint16,uint16,uint16,uint16,uint32,uint32,uint8,uint64,uint64)", _policy_tuple()),
        ],
    )

    it = intent_mod.Intent(
        token_in=quote,
        token_out=asset,
        fee=3000,
        amount_in=100_000_000,
        min_amount_out=1_234_080_000_000_000_000_000,
        declared_edge_bps=160,
        strategy_hash="0x" + "ab" * 32,
        parent_id="0x" + "00" * 32,
        nonce=7,
        deadline=1_900_000_000,
        max_notional_quote=200_000_000,
    )

    vault_abi = _art("vault")["abi"]
    c = w3.eth.contract(address=w3.to_checksum_address(vault), abi=vault_abi)
    onchain = c.functions.hashIntent(it.as_tuple()).call()
    offchain = intent_mod.digest(it, vault, 177)
    assert onchain == offchain, "off-chain EIP-712 digest diverged from SentinelVault.hashIntent"

    # and the domain separator matches too (chainId + verifying contract are part of it)
    assert c.functions.domainSeparator().call() == intent_mod.domain_separator(vault, 177)
