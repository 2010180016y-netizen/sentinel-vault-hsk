"""Executors: paper (default) and on-chain.

Paper mode is the default on purpose. The vault is real and the router is real, but the money in
this demo is not: a wrong signal must cost a number on a chart, not the user's balance.
Going live requires three independent things: SENTINEL_LIVE=1, a vault address, and signing keys
in the environment -- and even then every intent needs a fresh dual signature.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .config import Addresses, Market, Settings
from .intent import Intent, dual_sign, encode_execute_calldata
from .ledger import Ledger
from .market import DexClient, PoolState
from .rpc import Rpc


@dataclass
class Fill:
    side: str
    amount_in: int
    amount_out: int
    price: float
    fee_quote: float
    notional_quote: float
    tx: str = ""


class PaperExecutor:
    """Fills at the live quoter price. No network write, no signature, no funds."""

    def __init__(self, dex: DexClient, ledger: Ledger, addresses: Addresses) -> None:
        self.dex = dex
        self.ledger = ledger
        self.addresses = addresses

    def buy(self, market: Market, notional_quote: float, state: PoolState) -> Fill:
        amount_in = int(round(notional_quote * 1e6))
        q = self.dex.quote_buy(market, amount_in)
        price = (amount_in / 1e6) / (q.amount_out / 10**market.decimals)
        fee_quote = amount_in / 1e6 * market.fee / 1_000_000
        return Fill("BUY", amount_in, q.amount_out, price, fee_quote, notional_quote)

    def sell(self, market: Market, qty_raw: int, state: PoolState) -> Fill:
        q = self.dex.quote_sell(market, qty_raw)
        price = (q.amount_out / 1e6) / (qty_raw / 10**market.decimals)
        fee_quote = q.amount_out / 1e6 * market.fee / 1_000_000
        return Fill("SELL", qty_raw, q.amount_out, price, fee_quote, q.amount_out / 1e6)


class ChainExecutor:
    """Sends `executeIntent` to the deployed vault with a fresh dual signature."""

    ABI_EXECUTE = [
        {
            "name": "executeIntent",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [
                {
                    "name": "it",
                    "type": "tuple",
                    "components": [
                        {"name": "tokenIn", "type": "address"},
                        {"name": "tokenOut", "type": "address"},
                        {"name": "fee", "type": "uint24"},
                        {"name": "amountIn", "type": "uint128"},
                        {"name": "minAmountOut", "type": "uint128"},
                        {"name": "declaredEdgeBps", "type": "uint16"},
                        {"name": "strategyHash", "type": "bytes32"},
                        {"name": "parentId", "type": "bytes32"},
                        {"name": "nonce", "type": "uint64"},
                        {"name": "deadline", "type": "uint64"},
                        {"name": "maxNotionalQuote", "type": "uint64"},
                    ],
                },
                {"name": "strategySignature", "type": "bytes"},
                {"name": "riskSignature", "type": "bytes"},
            ],
            "outputs": [{"name": "amountOut", "type": "uint256"}],
        },
        {"name": "openParent", "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "parentId", "type": "bytes32"}, {"name": "budgetQuote", "type": "uint64"}], "outputs": []},
        {"name": "recordNoTrade", "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "strategyHash", "type": "bytes32"}, {"name": "edgeBps", "type": "uint16"},
                    {"name": "reasonHash", "type": "bytes32"}, {"name": "nonce", "type": "uint64"}], "outputs": []},
        {"name": "pause", "type": "function", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
    ]

    def __init__(self, settings: Settings, rpc: Rpc, ledger: Ledger) -> None:
        if not settings.live:
            raise RuntimeError("ChainExecutor requires SENTINEL_LIVE=1")
        if not settings.vault:
            raise RuntimeError("SENTINEL_VAULT is not set")
        self.settings = settings
        self.rpc = rpc
        self.ledger = ledger
        self.vault = settings.vault
        self._strategy_key = os.environ.get("SENTINEL_STRATEGY_KEY", "")
        self._risk_key = os.environ.get("SENTINEL_RISK_KEY", "")
        if not self._strategy_key or not self._risk_key:
            raise RuntimeError("both SENTINEL_STRATEGY_KEY and SENTINEL_RISK_KEY must be set for live mode")

    def execute(self, it: Intent) -> dict:
        it.nonce = self.ledger.nonce + 1
        it.deadline = int(time.time()) + 300
        s_sig, r_sig = dual_sign(it, self._strategy_key, self._risk_key, self.vault, self.settings.chain.chain_id)

        w3 = self.rpc.w3
        account = w3.eth.account.from_key(self._strategy_key)
        contract = self.rpc.contract(self.vault, self.ABI_EXECUTE)
        fn = contract.functions.executeIntent(it.as_tuple(), bytes.fromhex(s_sig[2:]), bytes.fromhex(r_sig[2:]))

        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 900_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": self.settings.chain.chain_id,
            }
        )
        signed = account.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        self.ledger.nonce = it.nonce
        return {
            "tx": h.hex(),
            "status": receipt["status"],
            "gasUsed": receipt["gasUsed"],
            "calldata_bytes": len(encode_execute_calldata(it, s_sig, r_sig)),
        }
