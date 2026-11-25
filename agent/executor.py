"""Executors: paper (default) and on-chain.

Paper mode is the default on purpose. The vault is real and the router is real, but the money in
this demo is not: a wrong signal must cost a number on a chart, not the user's balance.
Going live requires three independent things: SENTINEL_LIVE=1, a vault address, and signing keys
in the environment -- and even then every intent needs a fresh dual signature.

Two things in the live path are deliberate and were added after review:
  * the filled amount is read back from the receipt's `IntentExecuted` log, not assumed. Recording a
    trade with amount_out = 0 (the earlier placeholder) books a 100% loss on a trade that succeeded.
  * `recordNoTrade` is a real call, so a refusal is evidence on-chain and not only in this process.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from eth_utils import keccak

from .config import Addresses, Market, Settings
from .intent import Intent, dual_sign, encode_execute_calldata
from .ledger import Ledger
from .market import DexClient, PoolState
from .rpc import Rpc

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def hex_hash(value) -> str:
    """A `0x`-prefixed transaction hash. HexBytes.hex() drops the prefix in this web3/eth-account pair,
    and a hash without `0x` is not a hash anyone can open in an explorer."""
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else "0x" + text

INTENT_EXECUTED_TOPIC = "0x" + keccak(
    text=(
        "IntentExecuted(bytes32,bytes32,bytes32,address,address,uint256,uint256,uint256,uint256,uint16,int256,uint64)"
    )
).hex()


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
    """Sends `executeIntent` (and `recordNoTrade`) to the deployed vault with a fresh dual signature."""

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
                    {"name": "hurdleBps", "type": "uint16"}, {"name": "reasonHash", "type": "bytes32"},
                    {"name": "nonce", "type": "uint64"}], "outputs": []},
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
        # One more interlock before money can move in the user's name: a non-loopback RPC needs an
        # explicit, named opt-in. This is what caught a demo script that forgot to point HSK_RPC at its
        # own anvil fork — the refusal happened at the node, but a funded key would have spent gas.
        host = (urlparse(settings.chain.rpc_url).hostname or "").lower()
        self.local_rpc = host in LOCAL_HOSTS
        self.rpc_host = host

    # ---------------- internals ----------------

    def _send(self, fn, account) -> dict:
        if not self.local_rpc and os.environ.get("SENTINEL_CONFIRM_MAINNET_TX") != "1":
            raise RuntimeError(
                f"refusing to broadcast to {self.rpc_host} without SENTINEL_CONFIRM_MAINNET_TX=1 "
                "(the hackathon brief forbids mainnet gas spend; a local fork needs no flag)"
            )
        w3 = self.rpc.w3
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
        if receipt["status"] != 1:
            # A bare "reverted" hash is useless at 3am. Re-run the exact call one block earlier and
            # surface the contract's own error (Executed before a revert has no reason to give).
            reason = ""
            try:
                w3.eth.call(
                    {"from": tx["from"], "to": tx.get("to"), "data": tx.get("data"), "value": tx.get("value", 0)},
                    block_identifier=max(0, int(receipt["blockNumber"]) - 1),
                )
            except Exception as exc:  # noqa: BLE001 - the message is the point
                reason = f" — {exc}"
            raise RuntimeError(f"transaction reverted: {hex_hash(h)}{reason[:400]}")
        return {"tx": hex_hash(h), "status": receipt["status"], "gasUsed": receipt["gasUsed"], "logs": receipt["logs"]}

    @staticmethod
    def decode_amount_out(logs) -> int:
        """Read `amountOut` from the IntentExecuted log the vault emitted. This is the only honest source
        for what the trade actually delivered — the router's return value is not in the receipt."""
        for log in logs or []:
            topics = log.get("topics") or []
            if not topics:
                continue
            topic0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
            if topic0.lower().removeprefix("0x") != INTENT_EXECUTED_TOPIC.removeprefix("0x"):
                continue
            data = log["data"]
            if hasattr(data, "hex"):
                data = data.hex()
            raw = bytes.fromhex(str(data).removeprefix("0x"))
            # tokenIn, tokenOut, amountIn, amountOut, notionalQuote, fairOut, declaredEdgeBps,
            # realizedEdgeBps, nonce
            words = [raw[i : i + 32] for i in range(0, len(raw), 32)]
            if len(words) < 9:
                continue
            return int.from_bytes(words[3], "big")
        return 0

    # ---------------- actions ----------------

    def execute(self, it: Intent) -> dict:
        it.nonce = self.ledger.nonce + 1
        it.deadline = int(time.time()) + 300
        s_sig, r_sig = dual_sign(it, self._strategy_key, self._risk_key, self.vault, self.settings.chain.chain_id)

        w3 = self.rpc.w3
        account = w3.eth.account.from_key(self._strategy_key)
        contract = self.rpc.contract(self.vault, self.ABI_EXECUTE)
        fn = contract.functions.executeIntent(
            it.as_tuple(), bytes.fromhex(s_sig.removeprefix("0x")), bytes.fromhex(r_sig.removeprefix("0x"))
        )
        sent = self._send(fn, account)
        self.ledger.nonce = it.nonce
        amount_out = self.decode_amount_out(sent["logs"])
        if amount_out <= 0:
            raise RuntimeError(f"IntentExecuted not found in receipt {sent['tx']}; refusing to book a blind fill")
        return {
            "tx": sent["tx"],
            "status": sent["status"],
            "gasUsed": sent["gasUsed"],
            "amount_out": amount_out,
            "calldata_bytes": len(encode_execute_calldata(it, s_sig, r_sig)),
        }

    def record_no_trade(self, *, strategy_hash: str, edge_bps: int, hurdle_bps: int, reason: str) -> dict:
        """Write a deliberate refusal to the same log a trade would land in."""
        nonce = self.ledger.nonce + 1
        w3 = self.rpc.w3
        account = w3.eth.account.from_key(self._strategy_key)
        contract = self.rpc.contract(self.vault, self.ABI_EXECUTE)
        reason_hash = "0x" + keccak(text=reason).hex()
        sh = strategy_hash if isinstance(strategy_hash, bytes) else bytes.fromhex(str(strategy_hash).removeprefix("0x"))
        fn = contract.functions.recordNoTrade(
            sh, int(edge_bps), int(hurdle_bps), bytes.fromhex(reason_hash.removeprefix("0x")), nonce
        )
        sent = self._send(fn, account)
        self.ledger.nonce = nonce
        return {"tx": sent["tx"], "nonce": nonce}
