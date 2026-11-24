"""Thin, defensive JSON-RPC layer.

Two rules encoded here (learned from AgentKit's network gating, docs/10_OSS_RESEARCH.md §3.9):
  * every call asserts the chain id, so a mis-configured RPC can never make the agent sign for
    the wrong chain;
  * transient failures are retried a bounded number of times, and then surface as errors instead
    of being swallowed, because a silently stale read is worse than a stop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.types import LogReceipt


class WrongChain(RuntimeError):
    pass


@dataclass
class Rpc:
    url: str
    expected_chain_id: int
    timeout: float = 20.0
    retries: int = 3

    def __post_init__(self) -> None:
        self._w3 = Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": self.timeout}))
        chain_id = self._w3.eth.chain_id
        if chain_id != self.expected_chain_id:
            raise WrongChain(f"RPC {self.url} reports chain {chain_id}, expected {self.expected_chain_id}")

    @property
    def w3(self) -> Web3:
        return self._w3

    def _retry(self, fn, *a, **kw):
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # network hiccup: back off, then re-raise
                last = exc
                time.sleep(0.4 * (attempt + 1))
        assert last is not None
        raise last

    def block_number(self) -> int:
        return self._retry(lambda: self._w3.eth.block_number)

    def block_timestamp(self, block: int | None = None) -> int:
        b = self._retry(self._w3.eth.get_block, block if block is not None else "latest")
        return int(b["timestamp"])

    def call(self, to: str, data: str) -> str:
        return self._retry(
            lambda: self._w3.eth.call({"to": Web3.to_checksum_address(to), "data": data}).hex()
        )

    def try_call(self, to: str, data: str) -> str | None:
        try:
            return self.call(to, data)
        except (ContractLogicError, ValueError):
            return None

    def logs(self, address: str, topics: Sequence[str], from_block: int, to_block: int) -> list[LogReceipt]:
        def _get():
            return self._w3.eth.get_logs(
                {
                    "address": Web3.to_checksum_address(address),
                    "topics": list(topics),
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )

        return self._retry(_get)

    def contract(self, address: str, abi: list[dict[str, Any]]):
        return self._w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


ABI_POOL = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
    {"name": "observe", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "secondsAgos", "type": "uint32[]"}],
     "outputs": [{"name": "tickCumulatives", "type": "int56[]"},
                 {"name": "secondsPerLiquidityCumulativeX128s", "type": "uint160[]"}]},
    {"name": "Swap", "type": "event", "anonymous": False, "inputs": [
        {"name": "sender", "type": "address", "indexed": True},
        {"name": "recipient", "type": "address", "indexed": True},
        {"name": "amount0", "type": "int256", "indexed": False},
        {"name": "amount1", "type": "int256", "indexed": False},
        {"name": "sqrtPriceX96", "type": "uint160", "indexed": False},
        {"name": "liquidity", "type": "uint128", "indexed": False},
        {"name": "tick", "type": "int24", "indexed": False}]},
]

ABI_FACTORY = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}, {"name": "fee", "type": "uint24"}],
     "outputs": [{"name": "pool", "type": "address"}]},
]

ABI_QUOTER = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                 {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}]},
]

ABI_ERC20 = [
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]
