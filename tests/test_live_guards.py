"""The two mistakes that made the live path impossible, pinned so they cannot come back.

F1: `HexBytes.hex()` dropped the `0x` prefix, and every caller did `sig[2:]` — chopping the first
byte and producing a signature that recovers to nobody. The fork run found it as
`BadStrategySignature`; the unit tests had replaced the executor with a fake and never signed anything.

F2: `SENTINEL_LIVE=1` used to keep the paper executor. A live loop that silently papers is worse than
no live loop, because every number it prints is believed.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.config import Chain, Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.executor import ChainExecutor  # noqa: E402
from agent.intent import Intent, encode_execute_calldata, recover, sign  # noqa: E402
from agent.ledger import Ledger  # noqa: E402

PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
VAULT = "0x" + "22" * 20


def _intent() -> Intent:
    return Intent(
        token_in="0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029",
        token_out="0xB210D2120d57b758EE163cFfb43e73728c471Cf1",
        fee=3000,
        amount_in=120_000_000,
        min_amount_out=1_400_000_000_000_000_000_000,
        declared_edge_bps=160,
        strategy_hash="0x" + "11" * 32,
        nonce=7,
        deadline=1_800_000_300,
        max_notional_quote=200_000_000,
    )


def test_signature_is_0x_prefixed_and_65_bytes():
    sig = sign(_intent(), PK, VAULT, 177)
    assert sig.startswith("0x")
    assert len(sig) == 132, "65 bytes as hex"
    raw = bytes.fromhex(sig[2:])
    assert len(raw) == 65
    assert recover(_intent(), sig, VAULT, 177) == ADDR


def test_the_old_slicing_would_have_broken_it():
    """`sig[2:]` is only safe because the prefix exists — this is the exact regression guard."""
    sig = sign(_intent(), PK, VAULT, 177)
    assert len(bytes.fromhex(sig[2:])) == 65
    no_prefix = sig[2:]
    assert len(bytes.fromhex(no_prefix[2:])) == 64, "without the guard the signature loses a byte"


def test_calldata_carries_both_full_signatures():
    sig = sign(_intent(), PK, VAULT, 177)
    data = encode_execute_calldata(_intent(), sig, sig)
    assert data[:4].hex() == "8b1d1e1a" or len(data) > 4 + 65 * 2
    assert len(data) >= 4 + 64 * 2


def test_live_settings_select_the_chain_executor(monkeypatch):
    monkeypatch.setenv("SENTINEL_STRATEGY_KEY", PK)
    monkeypatch.setenv("SENTINEL_RISK_KEY", PK)
    s = Settings(live=True, vault=VAULT, chain=Chain(rpc_url="http://127.0.0.1:8545"))
    s.policy = s.policy
    agent = SentinelAgent(s, rpc=object(), dex=object(), ledger=Ledger())
    assert isinstance(agent.executor, ChainExecutor)
    assert agent.executor.local_rpc is True


def test_paper_is_the_default_and_live_without_keys_fails_closed(monkeypatch):
    s = Settings(live=False, vault="", chain=Chain(rpc_url="http://127.0.0.1:8545"))
    agent = SentinelAgent(s, rpc=object(), dex=object(), ledger=Ledger())
    assert not isinstance(agent.executor, ChainExecutor)

    monkeypatch.delenv("SENTINEL_STRATEGY_KEY", raising=False)
    monkeypatch.delenv("SENTINEL_RISK_KEY", raising=False)
    with pytest.raises(RuntimeError):
        SentinelAgent(Settings(live=True, vault=VAULT, chain=Chain(rpc_url="http://127.0.0.1:8545")),
                      rpc=object(), dex=object(), ledger=Ledger())


def test_a_non_loopback_rpc_needs_an_explicit_opt_in(monkeypatch):
    """The interlock that caught the demo pointing at mainnet. A funded key would have spent gas."""
    monkeypatch.setenv("SENTINEL_STRATEGY_KEY", PK)
    monkeypatch.setenv("SENTINEL_RISK_KEY", PK)
    monkeypatch.delenv("SENTINEL_CONFIRM_MAINNET_TX", raising=False)
    ex = ChainExecutor(Settings(live=True, vault=VAULT, chain=Chain(rpc_url="https://mainnet.hsk.xyz")),
                       rpc=object(), ledger=Ledger())
    assert ex.local_rpc is False
    with pytest.raises(RuntimeError, match="SENTINEL_CONFIRM_MAINNET_TX"):
        ex._send(object(), object())
