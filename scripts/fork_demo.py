#!/usr/bin/env python3
"""End-to-end demonstration on a local fork of HSK Chain mainnet.

What this script proves, in one run, with real transactions:

  1. **Deployment** — `SentinelVault` plus the three ERC-8004 registries and the adapter are deployed
     to a fork of HSK mainnet (chain 177) with three independent keys (owner / strategy / risk).
  2. **A real dislocation** — WHSK is sold through the real HSKSwap router, pushing the real pool's
     spot off its own TWAP. Nothing is rewritten; the oracle is the venue's.
  3. **The agent's own decision, on-chain** — the deterministic strategy observes the live pool. The
     default, trend-filtered strategy refuses the dump (adverse trend) and *writes the refusal*
     (`recordNoTrade`), consuming a nonce. The separately pre-registered `dislocation` strategy takes
     the same dump: the agent dual-signs an EIP-712 intent and `executeIntent` fills it through the
     real router.
  4. **The exit and the claim** — the pool is pushed back above TWAP, the agent exits, and the vault
     settles the entry claim (`realizedEdgeSumBps`, `edgeCredibilityBps`).
  5. **ERC-8004** — an agent identity is registered, a validation request is answered by the adapter
     from the vault's numbers, and reputation feedback is posted and read back.
  6. **x402 settlement, not paper** — a mock EIP-3009 token is deployed, the premium endpoint is
     started with `ChainFacilitator`, and the agent pays 0.001 USDT with a real
     `transferWithAuthorization` transaction. The authorization nonce is verified on-chain.
  7. **Reconciliation** — the agent's off-chain ledger and the vault's `stats()` are compared line by
     line; the run fails if they disagree beyond rounding.

No mainnet transaction is sent: every write goes to anvil on 127.0.0.1 (asserted in `_assert_local`).
The evidence is written to `evidence/e2e_fork.json` and `evidence/e2e_fork.md`.

    python scripts/fork_demo.py                 # starts its own anvil fork
    python scripts/fork_demo.py --rpc http://127.0.0.1:8546   # attach to an existing fork
    python scripts/fork_demo.py --keep          # leave anvil running for the dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from agent.config import Addresses, Chain, RiskPolicy, Settings, Strategy  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.intent import strategy_id  # noqa: E402
from agent.ledger import Ledger  # noqa: E402
from agent.registry8004 import registration_file, registration_uri  # noqa: E402

Account.enable_unaudited_hdwallet_features()

MAINNET_RPC = os.environ.get("HSK_FORK_RPC", "https://mainnet.hsk.xyz")
# anvil's public development mnemonic (foundry default). It has no mainnet funds and is not a secret;
# it exists so the demo can be reproduced without anyone handing over a key.
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"
USDT = Addresses.quote
WHSK = Addresses.whsk
ROUTER = Addresses.router
FACTORY = Addresses.factory
FEE = 3000

ROUTER_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    }
]

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable", "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view", "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "observe", "type": "function", "stateMutability": "view", "inputs": [{"name": "secondsAgos", "type": "uint32[]"}], "outputs": [
        {"name": "tickCumulatives", "type": "int56[]"}, {"name": "secondsPerLiquidityCumulativeX128s", "type": "uint160[]"}]},
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def artifact(name: str, contract: str) -> dict:
    path = ROOT / "out" / name / f"{contract}.json"
    if not path.exists():
        raise SystemExit(f"missing {path}; run `forge build` first")
    return json.loads(path.read_text())


@dataclass
class Receipt:
    name: str
    tx: str
    gas: int
    status: int
    data: dict

    def as_dict(self) -> dict:
        return {"step": self.name, "tx": self.tx, "gas_used": self.gas, "status": self.status, **self.data}


class ForkDemo:
    def __init__(self, rpc: str, evidence_dir: Path) -> None:
        self.rpc = rpc
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
        if not self.w3.is_connected():
            raise SystemExit(f"cannot reach {rpc}")
        self.chain_id = int(self.w3.eth.chain_id)
        self.evidence_dir = evidence_dir
        self.steps: list[Receipt] = []
        self.checks: list[dict] = []
        self.owner = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/0")
        self.strategy = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/1")
        self.risk = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/2")
        self.whale = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/3")
        self.facilitator = Account.from_mnemonic(ANVIL_MNEMONIC, account_path="m/44'/60'/0'/0/4")
        self.usdt = self.w3.eth.contract(address=Web3.to_checksum_address(USDT), abi=ERC20_ABI)
        self.whsk = self.w3.eth.contract(address=Web3.to_checksum_address(WHSK), abi=ERC20_ABI)
        self.router = self.w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
        self.pool_addr = ""
        self.pool = None
        self.twap_window = 600
        self.vault = None
        self.vault_addr = ""
        self.identity = None
        self.reputation = None
        self.validation = None
        self.adapter = None
        self.agent_id = 0
        self.strategies: dict[str, str] = {}

    # ------------------------------------------------------------------ plumbing

    def _assert_local(self) -> None:
        host = (self.w3.provider.endpoint_uri or "").split("//")[-1].split(":")[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit(f"refusing to run against non-loopback RPC {self.w3.provider.endpoint_uri}")

    def rpc_call(self, method: str, params: list):
        return self.w3.provider.make_request(method, params)

    def send(self, fn, name: str, *, account=None, gas: int | None = None, value: int = 0) -> Receipt:
        account = account or self.owner
        opts = {"from": account.address, "value": value}
        if gas:
            opts["gas"] = gas
        tx = fn.transact(opts)
        rc = self.w3.eth.wait_for_transaction_receipt(tx, timeout=180)
        rec = Receipt(name, tx.hex(), int(rc["gasUsed"]), int(rc["status"]), {})
        self.steps.append(rec)
        if rec.status != 1:
            raise SystemExit(f"{name} reverted: {rec.tx}")
        return rec

    def deploy(self, art_name: str, contract: str, args: list, name: str) -> str:
        art = artifact(art_name, contract)
        factory = self.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
        ctor = factory.constructor(*args) if args else factory.constructor()
        # `transact` needs the data; build + estimate once so a revert surfaces with a reason.
        tx = ctor.transact({"from": self.owner.address, "gas": 12_000_000})
        rc = self.w3.eth.wait_for_transaction_receipt(tx, timeout=180)
        addr = rc.get("contractAddress") or rc.get("contractAddress")
        self.steps.append(Receipt(name, tx.hex(), int(rc["gasUsed"]), int(rc["status"]), {"address": addr}))
        if int(rc["status"]) != 1 or not addr:
            raise SystemExit(f"deployment of {contract} failed")
        log(f"deployed {contract} at {addr} ({rc['gasUsed']} gas)")
        return addr

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        log(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            raise SystemExit(f"check failed: {name} {detail}")

    # ------------------------------------------------------------------ pool

    def find_pool(self) -> None:
        factory_abi = [{"name": "getPool", "type": "function", "stateMutability": "view", "inputs": [
            {"name": "a", "type": "address"}, {"name": "b", "type": "address"}, {"name": "f", "type": "uint24"}],
            "outputs": [{"name": "", "type": "address"}]}]
        f = self.w3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=factory_abi)
        self.pool_addr = f.functions.getPool(WHSK, USDT, FEE).call()
        self.pool = self.w3.eth.contract(address=Web3.to_checksum_address(self.pool_addr), abi=POOL_ABI)
        self.check("pool resolved by the factory", self.pool_addr != "0x" + "0" * 40, self.pool_addr)
        for w in (600, 420, 300, 240, 180):
            try:
                self.pool.functions.observe([w, 0]).call()
                self.twap_window = w
                break
            except Exception:  # noqa: BLE001
                continue
        log(f"TWAP window the oracle can serve: {self.twap_window}s")

    def ticks(self) -> tuple[int, int]:
        twap = self.pool.functions.observe([self.twap_window, 0]).call()
        t = int((twap[0][1] - twap[0][0]) / self.twap_window)
        spot = int(self.pool.functions.slot0().call()[1])
        return t, spot

    def dislocation(self) -> int:
        t, spot = self.ticks()
        return abs(spot - t)

    def depth_quote(self) -> float:
        liq = int(self.pool.functions.liquidity().call())
        price = 1.0001 ** int(self.pool.functions.slot0().call()[1])
        # WHSK (18dp) is token0 or token1; read it rather than assume.
        token0 = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.pool_addr),
            abi=[{"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]}],
        ).functions.token0().call()
        if token0.lower() == WHSK.lower():
            # base is token0, quote (USDT) is token1: amount1 = L*sqrtP in raw quote units
            return liq * (price**0.5) / 1e6
        return liq / (price**0.5) / 1e6

    # ------------------------------------------------------------------ setup

    def deploy_stack(self) -> None:
        policy = RiskPolicy(twap_window=self.twap_window)
        self.vault_addr = self.deploy(
            "SentinelVault.sol", "SentinelVault",
            [self.owner.address, ROUTER, FACTORY, USDT, self.strategy.address, self.risk.address,
             (policy.max_slippage_bps, policy.min_edge_bps, policy.max_spot_twap_deviation_bps,
              policy.max_drawdown_bps, policy.twap_window, policy.cooldown_s, policy.max_slices,
              policy.max_trade_notional, policy.max_daily_notional)],
            "deploy SentinelVault",
        )
        art = artifact("SentinelVault.sol", "SentinelVault")
        self.vault = self.w3.eth.contract(address=Web3.to_checksum_address(self.vault_addr), abi=art["abi"])

        ident = self.deploy("IdentityRegistry.sol", "IdentityRegistry", [], "deploy IdentityRegistry")
        rep = self.deploy("ReputationRegistry.sol", "ReputationRegistry", [], "deploy ReputationRegistry")
        val = self.deploy("ValidationRegistry.sol", "ValidationRegistry", [], "deploy ValidationRegistry")
        adapter = self.deploy("Sentinel8004Adapter.sol", "Sentinel8004Adapter",
                              [rep, val, self.owner.address], "deploy Sentinel8004Adapter")
        self.identity = self.w3.eth.contract(address=Web3.to_checksum_address(ident), abi=artifact("IdentityRegistry.sol", "IdentityRegistry")["abi"])
        self.reputation = self.w3.eth.contract(address=Web3.to_checksum_address(rep), abi=artifact("ReputationRegistry.sol", "ReputationRegistry")["abi"])
        self.validation = self.w3.eth.contract(address=Web3.to_checksum_address(val), abi=artifact("ValidationRegistry.sol", "ValidationRegistry")["abi"])
        self.adapter = self.w3.eth.contract(address=Web3.to_checksum_address(adapter), abi=artifact("Sentinel8004Adapter.sol", "Sentinel8004Adapter")["abi"])
        for name, contract in (("ReputationRegistry", self.reputation), ("ValidationRegistry", self.validation)):
            self.send(contract.functions.initialize(ident), f"{name}.initialize")

        # Two pre-registered strategies. The owner (a human) registers both; the agent cannot.
        for mode in ("trend_filtered", "dislocation"):
            self.strategies[mode] = strategy_id(Strategy(mode=mode).spec())
        for mode, h in self.strategies.items():
            self.send(self.vault.functions.registerStrategy(bytes.fromhex(h[2:])), f"registerStrategy({mode})")
        self.send(self.vault.functions.setMarket(WHSK, FEE, True), "setMarket(WHSK/USDT 0.3%)")
        self.send(self.vault.functions.unpause(), "unpause (owner arms the vault)")

    def fund(self) -> None:
        # The pool is a real holder of both tokens; on a fork we can borrow from it to set up the venue.
        self.rpc_call("anvil_impersonateAccount", [self.pool_addr])
        self.rpc_call("anvil_setBalance", [self.pool_addr, hex(100 * 10**18)])
        self.send(self.usdt.functions.transfer(self.owner.address, 5_000 * 10**6), "USDT: pool → owner (fork borrow)",
                  account=_impersonated(self.pool_addr, self.w3))
        self.send(self.whsk.functions.transfer(self.whale.address, 4_000_000 * 10**18), "WHSK: pool → whale (fork borrow)",
                  account=_impersonated(self.pool_addr, self.w3))
        self.send(self.usdt.functions.approve(self.vault_addr, 2**256 - 1), "owner approves vault for USDT")
        self.send(self.vault.functions.deposit(USDT, 1_000 * 10**6), "owner deposits 1,000 USDT")
        self.check("vault holds the deposit", self.usdt.functions.balanceOf(self.vault_addr).call() >= 1_000 * 10**6,
                   f"{self.usdt.functions.balanceOf(self.vault_addr).call() / 1e6:.2f} USDT")

    # ------------------------------------------------------------------ market moves

    def swap(self, account, token_in, token_out, amount_in: int, name: str) -> int:
        approve = self.usdt if token_in == USDT else self.whsk
        if approve.functions.allowance(account.address, ROUTER).call() < amount_in:
            self.send(approve.functions.approve(ROUTER, 2**256 - 1), f"{name}: approve router", account=account)
        params = (token_in, token_out, FEE, account.address, amount_in, 0, 0)
        tx = self.router.functions.exactInputSingle(params).transact({"from": account.address, "gas": 600_000})
        rc = self.w3.eth.wait_for_transaction_receipt(tx, timeout=180)
        self.steps.append(Receipt(name, tx.hex(), int(rc["gasUsed"]), int(rc["status"]), {}))
        if int(rc["status"]) != 1:
            raise SystemExit(f"{name} reverted")
        return tx.hex()

    def push_dislocation(self, target_bps: int) -> int:
        log(f"creating a real dislocation (target {target_bps}bp) with whale sells through HSKSwap…")
        # Geometric steps: the impact grows with each slice, so a coarse ladder overshoots the 500bp
        # manipulation breaker and the vault would (correctly) refuse the trade for the wrong reason.
        # Fixed small slices: concentrated liquidity makes the impact accelerate, and the vault's
        # manipulation breaker (500bp) is only 120bp above the entry hurdle's claim cap. A coarse ladder
        # overshoots into "refused for the wrong reason".
        size = 6_000
        while size > 0:
            have = self.whsk.functions.balanceOf(self.whale.address).call()
            amount = min(size * 10**18, have - 10**18)
            if amount <= 0:
                break
            self.swap(self.whale, WHSK, USDT, amount, f"whale sells {amount / 1e18:,.0f} WHSK")
            d = self.dislocation()
            log(f"  after {amount / 1e18:,.0f} WHSK: dislocation {d}bp")
            if d >= target_bps:
                return d
            if have < (size + 1) * 10**18:
                break
        return self.dislocation()

    def push_reversion(self, target_bps: int) -> int:
        log(f"pushing the pool back above TWAP (target +{target_bps}bp) with whale buys…")
        self.rpc_call("anvil_setBalance", [self.pool_addr, hex(100 * 10**18)])
        self.send(self.usdt.functions.transfer(self.whale.address, 300_000 * 10**6), "USDT: pool → whale (fork borrow)",
                  account=_impersonated(self.pool_addr, self.w3))
        # 1,500 USDT per slice: at this depth ~0.05bp per USDT, so a slice moves the spot ~75bp and the
        # ladder lands inside [entry hurdle, 500bp manipulation cap] instead of jumping over it.
        size = 1_500
        while size < 4_000_000:
            have = self.usdt.functions.balanceOf(self.whale.address).call()
            amount = min(size * 10**6, have - 10**6)
            if amount <= 0:
                break
            self.swap(self.whale, USDT, WHSK, amount, f"whale buys with {amount / 1e6:,.0f} USDT")
            twap, spot = self.ticks()
            dev = spot - twap
            log(f"  after {amount / 1e6:,.0f} USDT: dev {dev:+d}bp")
            if dev >= target_bps:
                return dev
        twap, spot = self.ticks()
        return spot - twap

    # ------------------------------------------------------------------ the agent

    def agent(self, mode: str, ledger: Ledger) -> SentinelAgent:
        settings = Settings.from_env()
        settings.chain = Chain(rpc_url=self.rpc, chain_id=self.chain_id)
        settings.live = True
        settings.vault = self.vault_addr
        settings.policy = RiskPolicy(twap_window=self.twap_window)
        settings.strategy = Strategy(mode=mode)
        settings.record_no_trade = True
        host = (settings.chain.rpc_url.split("//")[-1].split(":")[0] or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit(f"agent would talk to {settings.chain.rpc_url}, not the local fork")
        agent = SentinelAgent(settings, ledger=ledger)
        agent.strategy_hash = self.strategies[mode]
        os.environ["SENTINEL_RECORD_NO_TRADE"] = "1"
        return agent

    # ------------------------------------------------------------------ ERC-8004

    def register_agent(self) -> None:
        uri = registration_uri(
            registration_file(
                agent_id=1,
                identity_registry=self.identity.address,
                chain_id=self.chain_id,
                name="Sentinel HSK",
                description="Policy-bound HSKSwap trading agent: pre-registered strategies, claimed-vs-realised edge, recorded refusals",
                services=[
                    {"name": "web", "endpoint": "http://127.0.0.1:8765/"},
                    {"name": "x402", "endpoint": "http://127.0.0.1:8765/api/premium/edge", "version": "2"},
                ],
            )
        )
        self.send(self.identity.functions.register(uri), "IdentityRegistry.register (agent #1)")
        self.agent_id = int(self.identity.functions.balanceOf(self.owner.address).call())
        self.send(self.adapter.functions.setAgent(self.agent_id), "adapter.setAgent")
        request_hash = bytes.fromhex("11" * 32)
        self.send(self.validation.functions.validationRequest(self.adapter.address, self.agent_id, "ipfs://sentinel/v1", request_hash),
                  "validationRequest (evidence commitment)")
        self.send(self.adapter.functions.validate(request_hash, self.vault_addr, 0, 1), "adapter.validate (from vault.stats)")
        status = self.validation.functions.getValidationStatus(request_hash).call()
        self.check("validation answered 100 from the vault's record", int(status[2]) == 100, f"response={status[2]} tag={status[4]}")

    def attest(self) -> dict:
        self.send(self.adapter.functions.attest(self.vault_addr), "adapter.attest (reputation from vault.stats)")
        summary = self.reputation.functions.getSummary(self.agent_id, [self.adapter.address], "edgeCredibility", "").call()
        execution = self.reputation.functions.getSummary(self.agent_id, [self.adapter.address], "executionCost", "").call()
        self.check("reputation = the vault's own credibility number", int(summary[1]) == int(self.vault.functions.edgeCredibilityBps().call()),
                   f"summary={summary[1]} credibility={self.vault.functions.edgeCredibilityBps().call()}")
        return {
            "agent_id": self.agent_id,
            "credibility_feedback": {"count": int(summary[0]), "value": int(summary[1]), "decimals": int(summary[2])},
            "execution_feedback": {"count": int(execution[0]), "value": int(execution[1]), "decimals": int(execution[2])},
        }

    # ------------------------------------------------------------------ main flow

    def run(self) -> dict:
        self._assert_local()
        # Every component that reads `Settings.from_env()` (the agent, the server, the x402 client) must
        # talk to this fork, never to the public RPC. Pinning it here is the difference between a demo
        # and a mainnet transaction.
        os.environ["HSK_RPC"] = self.rpc
        self.check("chain is HSK mainnet (forked)", self.chain_id == 177, f"chain_id={self.chain_id}")
        self.find_pool()
        log(f"depth (quote side): {self.depth_quote():,.0f} USDT")
        self.deploy_stack()
        self.fund()

        start_tick = self.ticks()
        d0 = self.dislocation()
        self.check("the pool starts near its own TWAP", d0 <= 200, f"dislocation={d0}bp spot={start_tick[1]} twap={start_tick[0]}")

        dislocation = self.push_dislocation(380)
        self.check("a real dislocation was created", dislocation >= 340, f"{dislocation}bp")
        twap, spot = self.ticks()

        # --- the default strategy refuses, and the refusal is on-chain -----------------------------
        ledger = Ledger()
        ledger.open_with(1_000.0)
        agent = self.agent("trend_filtered", ledger)
        rec_default = agent.step()
        self.check("default strategy refuses the dump (adverse trend)", rec_default["action"] == "HOLD",
                   "; ".join(rec_default.get("reasons", []))[:160])
        self.check("the refusal was written on-chain", bool(rec_default.get("onchain_refusal")),
                   str(rec_default.get("onchain_refusal"))[:80])

        # --- the pre-registered dislocation strategy takes it ------------------------------------
        agent2 = self.agent("dislocation", ledger)
        rec_enter = agent2.step()
        self.check("dislocation strategy enters", rec_enter["action"] == "BUY" and rec_enter.get("executed"),
                   f"edge={rec_enter.get('edge_bps')}bp size={rec_enter.get('size_quote')} USDT "
                   f"reasons={'; '.join(rec_enter.get('reasons', []))[:300]}")
        buy_tx = rec_enter.get("tx", "")
        stats_after_buy = self.vault.functions.stats().call()
        self.check("vault tradeCount = 1", int(stats_after_buy[0]) == 1, f"tradeCount={stats_after_buy[0]}")
        self.check("declared edge registered on entry", int(stats_after_buy[4]) == int(rec_enter["edge_bps"]),
                   f"declared={stats_after_buy[4]} agent={rec_enter['edge_bps']}")
        self.check("allowance reset after the trade",
                   self.usdt.functions.allowance(self.vault_addr, ROUTER).call() == 0)
        qty = self.vault.functions.positions(WHSK).call()[0]
        self.check("position booked equals the fill", qty == int(rec_enter["amount_out"]), f"qty={qty}")
        held = ledger.positions.get(WHSK)
        self.check("the agent's own ledger holds the position", bool(held and held.qty > 0),
                   f"ledger_qty={held.qty if held else 0} keys={list(ledger.positions)}")

        # --- reversion, then the exit -------------------------------------------------------------
        self.push_reversion(260)
        rec_exit = agent2.step()
        self.check("agent exits after the reversion", rec_exit["action"] == "SELL" and rec_exit.get("executed"),
                   f"action={rec_exit.get('action')} realised={rec_exit.get('realised_edge_bps')}bp "
                   f"edge={rec_exit.get('edge_bps')}/{rec_exit.get('hurdle_bps')} "
                   f"reasons={'; '.join(rec_exit.get('reasons', []))[:300]} "
                   f"policy={rec_exit.get('policy', {}).get('reason')} "
                   f"guards={(rec_exit.get('guards') or {}).get('code')}")
        stats = self.vault.functions.stats().call()
        self.check("vault tradeCount = 2", int(stats[0]) == 2, f"tradeCount={stats[0]}")
        credibility = int(self.vault.functions.edgeCredibilityBps().call())
        realized = int(stats[5])
        self.check("the entry claim was settled with a realised number", realized != 0, f"realisedEdgeSum={realized}bp credibility={credibility}bp")

        # ERC-8004 after the record exists: the adapter validates from `vault.stats()` and refuses to
        # attest an agent with no declared claim, so the identity is registered once there is evidence.
        self.register_agent()
        erc = self.attest()

        reconcile = self.reconcile(agent2)
        out = {
            "generated_at": int(time.time()),
            "environment": {
                "chain_id": self.chain_id,
                "rpc": self.rpc,
                "fork_of": MAINNET_RPC,
                "twap_window_s": self.twap_window,
                "pool": self.pool_addr,
                "depth_quote_usdt": round(self.depth_quote(), 2),
            },
            "addresses": {
                "vault": self.vault_addr,
                "identity": self.identity.address,
                "reputation": self.reputation.address,
                "validation": self.validation.address,
                "adapter": self.adapter.address,
                "owner": self.owner.address,
                "strategy_signer": self.strategy.address,
                "risk_signer": self.risk.address,
            },
            "strategies": self.strategies,
            "market": {
                "dislocation_bps_created": dislocation,
                "twap_tick": twap,
                "spot_tick": spot,
                "entries": {
                    "decision": rec_enter["action"],
                    "edge_bps": rec_enter.get("edge_bps"),
                    "hurdle_bps": rec_enter.get("hurdle_bps"),
                    "size_quote": rec_enter.get("size_quote"),
                    "amount_out": rec_enter.get("amount_out"),
                    "leg_edge_bps": rec_enter.get("leg_edge_bps"),
                    "tx": buy_tx,
                },
                "exit": {
                    "decision": rec_exit["action"],
                    "realised_edge_bps": rec_exit.get("realised_edge_bps"),
                    "leg_edge_bps": rec_exit.get("leg_edge_bps"),
                    "tx": rec_exit.get("tx"),
                },
                "default_strategy_refusal": {
                    "action": rec_default["action"],
                    "reasons": rec_default.get("reasons"),
                    "onchain_tx": rec_default.get("onchain_refusal"),
                },
            },
            "vault_stats": {
                "tradeCount": int(stats[0]),
                "winCount": int(stats[1]),
                "realizedPnlQuote": int(stats[2]),
                "feesPaidQuote": int(stats[3]),
                "declaredEdgeSumBps": int(stats[4]),
                "realizedEdgeSumBps": int(stats[5]),
                "equity": int(stats[6]),
                "highWater": int(stats[7]),
                "oracleHealthy": bool(stats[9]),
                "executionEdgeSumBps": int(stats[10]),
                "edgeCredibilityBps": credibility,
                "avgExecutionEdgeBps": int(self.vault.functions.avgExecutionEdgeBps().call()),
            },
            "erc8004": erc,
            "reconcile": reconcile,
            "steps": [s.as_dict() for s in self.steps],
            "checks": self.checks,
        }
        return out

    def reconcile(self, agent: SentinelAgent) -> dict:
        view = agent.vault_view.summary() if agent.vault_view else {}
        m = agent.ledger.metrics()
        pairs = {
            "trades": (m["trades"], view.get("trade_count")),
            "declared_edge_sum_bps": (m["declared_edge_sum_bps"], view.get("declared_edge_sum_bps")),
            "realised_edge_sum_bps": (m["realised_edge_sum_bps"], view.get("realised_edge_sum_bps")),
            "avg_execution_edge_bps": (m["avg_execution_edge_bps"], view.get("avg_execution_edge_bps")),
        }
        rows = []
        for k, (a, b) in pairs.items():
            b = float(b or 0)
            rows.append({"field": k, "ledger": a, "vault": b, "delta": round(a - b, 4),
                         "match": abs(a - b) <= max(0.5, abs(b) * 0.01)})
        self.check("off-chain ledger reconciles with the vault", all(r["match"] for r in rows),
                   "; ".join(f"{r['field']}={r['ledger']}vs{r['vault']}" for r in rows if not r["match"]) or "all rows match")
        return {"rows": rows, "all_match": all(r["match"] for r in rows), "vault": view}

    # ------------------------------------------------------------------ x402


def _impersonated(address: str, w3: Web3):
    """A pseudo-robot: anvil signs for an impersonated account, web3 only needs the `from`."""

    class _Acct:
        def __init__(self, addr: str) -> None:
            self.address = addr
            self.key = b""

    return _Acct(address)


def run_x402(demo: ForkDemo, port: int, evidence: dict) -> dict:
    """Deploy a mock EIP-3009 token and settle a real 0.001 USDT payment through the premium endpoint."""
    log("deploying the EIP-3009 token and settling a real x402 payment…")
    token_addr = demo.deploy("MockEIP3009.sol", "MockEIP3009",
                             ["Mock USD", "1", demo.chain_id], "deploy MockEIP3009")
    token = demo.w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=artifact("MockEIP3009.sol", "MockEIP3009")["abi"])
    demo.send(token.functions.mint(demo.strategy.address, 10 * 10**6), "mint 10 mUSDT to the agent wallet")
    demo.send(token.functions.mint(demo.facilitator.address, 10 * 10**6), "mint 10 mUSDT to the payee")

    env = dict(os.environ)
    env.update(
        {
            "HSK_RPC": demo.rpc,
            "SENTINEL_VAULT": demo.vault_addr,
            "SENTINEL_X402_PAY_TO": demo.facilitator.address,
            "SENTINEL_X402_PRICE": "1000",
            "SENTINEL_X402_ASSET": token_addr,
            "SENTINEL_X402_TOKEN_NAME": "Mock USD",
            "SENTINEL_X402_TOKEN_VERSION": "1",
            "SENTINEL_X402_SETTLEMENT": "chain",
            "SENTINEL_X402_RPC": demo.rpc,
            "SENTINEL_X402_FACILITATOR_KEY": demo.facilitator.key.hex(),
            "SENTINEL_X402_DEMO_KEY": demo.strategy.key.hex(),
            "SENTINEL_X402_BUDGET": "10000",
            "SENTINEL_STATE_DIR": str(ROOT / "state" / "fork_demo"),
        }
    )
    os.environ.update({k: env[k] for k in env if k.startswith("SENTINEL_") or k == "HSK_RPC"})

    # A fresh state file so a previous run's nonces cannot mask a replay bug.
    state_path = ROOT / "state" / "fork_demo" / "ledger.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        state_path.unlink()

    import agent.server as server

    server._agent = None
    server._premium = None
    server.Handler.state_path = str(state_path)
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    import urllib.request

    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)

    with urllib.request.urlopen(base + "/api/x402/demo", timeout=120) as r:
        result = json.loads(r.read())
    srv.shutdown()
    demo.check("x402: the agent's payment settled on-chain", bool(result.get("ok")),
               f"status={result.get('status')} settlement={result.get('settlement')} error={result.get('error')}")
    settlement = result.get("settlement") or {}
    tx_hash = settlement.get("transaction", "")
    demo.check("x402: settlement returned a real transaction hash", tx_hash.startswith("0x") and len(tx_hash) == 66, tx_hash)
    rc = demo.w3.eth.get_transaction_receipt(tx_hash)
    demo.check("x402: the settlement transaction succeeded", int(rc["status"]) == 1, f"gas={rc['gasUsed']}")

    used = token.events.AuthorizationUsed().process_receipt(rc)
    demo.check("x402: the EIP-3009 nonce is consumed on-chain", bool(used), f"events={len(used)}")
    nonce = used[0]["args"]["nonce"] if used else b"\x00" * 32
    state = token.functions.authorizationState(demo.strategy.address, nonce).call()
    demo.check("x402: authorizationState[nonce] is true", state is True, f"nonce=0x{nonce.hex()[:16]}…")
    demo.check("x402: the payee received exactly 0.001 USDT",
               token.functions.balanceOf(demo.facilitator.address).call() >= 10 * 10**6 + 1000,
               f"payee={token.functions.balanceOf(demo.facilitator.address).call()}")
    demo.check("x402: the premium payload is the agent's book",
               isinstance(result.get("premium"), dict) and "edge" in (result.get("premium") or {}),
               str((result.get("premium") or {}).get("resource")))

    return {
        "token": token_addr,
        "price_atomic": 1000,
        "payer": demo.strategy.address,
        "payee": demo.facilitator.address,
        "settlement_mode": result.get("settlement_mode"),
        "tx": tx_hash,
        "gas_used": int(rc["gasUsed"]),
        "nonce": "0x" + nonce.hex(),
        "authorization_state": state,
        "payee_balance": token.functions.balanceOf(demo.facilitator.address).call(),
        "premium_resource": (result.get("premium") or {}).get("resource"),
    }


def write_evidence(evidence: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e2e_fork.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    s = evidence["vault_stats"]
    m = evidence["market"]
    lines = [
        "# End-to-end run on an HSK mainnet fork",
        "",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(evidence['generated_at']))}",
        f"- chain: {evidence['environment']['chain_id']} · fork of {evidence['environment']['fork_of']}",
        f"- pool: `{evidence['environment']['pool']}` · depth ≈ ${evidence['environment']['depth_quote_usdt']:,.0f}",
        f"- vault: `{evidence['addresses']['vault']}`",
        f"- identity/reputation/validation: `{evidence['addresses']['identity']}` / `{evidence['addresses']['reputation']}` / `{evidence['addresses']['validation']}`",
        f"- adapter: `{evidence['addresses']['adapter']}`",
        "",
        "## What happened",
        "",
        f"1. A whale sold WHSK through the real router until the pool's spot sat **{m['dislocation_bps_created']}bp** off its own TWAP.",
        f"2. The default (trend-filtered) strategy refused — adverse trend — and the refusal is a transaction: `{m['default_strategy_refusal']['onchain_tx']}`.",
        f"3. The pre-registered dislocation strategy bought **{m['entries']['size_quote']} USDT** claiming **{m['entries']['edge_bps']}bp** (hurdle {m['entries']['hurdle_bps']}bp): `{m['entries']['tx']}`.",
        f"4. The pool was pushed back above TWAP; the agent exited at **{m['exit']['realised_edge_bps']}bp** round trip: `{m['exit']['tx']}`.",
        f"5. ERC-8004: agent #{evidence['erc8004']['agent_id']} validated and rated from `vault.stats()` — credibility feedback **{evidence['erc8004']['credibility_feedback']['value']}**, execution feedback **{evidence['erc8004']['execution_feedback']['value']}**.",
        f"6. x402: **0.001 USDT** settled with a real `transferWithAuthorization` — tx `{evidence['x402']['tx']}` (gas {evidence['x402']['gas_used']}), nonce consumed on-chain."
        if "x402" in evidence
        else "6. x402: skipped (--skip-x402).",
        "",
        "## Vault's own numbers (not the agent's)",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in ("tradeCount", "winCount", "realizedPnlQuote", "feesPaidQuote", "declaredEdgeSumBps",
                "realizedEdgeSumBps", "equity", "highWater", "oracleHealthy", "executionEdgeSumBps",
                "edgeCredibilityBps", "avgExecutionEdgeBps"):
        lines.append(f"| {key} | {s[key]} |")
    lines += [
        "",
        "## Reconciliation (agent ledger vs vault)",
        "",
        "| field | ledger | vault | delta | match |",
        "|---|---|---|---|---|",
    ]
    for row in evidence["reconcile"]["rows"]:
        lines.append(f"| {row['field']} | {row['ledger']} | {row['vault']} | {row['delta']} | {'✅' if row['match'] else '❌'} |")
    lines += [
        "",
        "## Checks",
        "",
    ]
    for c in evidence["checks"]:
        lines.append(f"- {'✅' if c['ok'] else '❌'} {c['check']}" + (f" — {c['detail']}" if c["detail"] else ""))
    lines += ["", f"All {len(evidence['steps'])} transactions: see `e2e_fork.json` → `steps`.", ""]
    (out_dir / "e2e_fork.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="", help="attach to an existing fork instead of starting anvil")
    ap.add_argument("--keep", action="store_true", help="leave anvil running (for the dashboard)")
    ap.add_argument("--skip-x402", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evidence"))
    a = ap.parse_args()

    proc = None
    rpc = a.rpc
    if not rpc:
        port = free_port()
        anvil = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
        log(f"starting anvil fork of {MAINNET_RPC} on 127.0.0.1:{port}")
        proc = subprocess.Popen(
            [anvil, "--fork-url", MAINNET_RPC, "--chain-id", "177", "--port", str(port),
             "--gas-limit", "60000000", "--code-size-limit", "50000", "--silent"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        rpc = f"http://127.0.0.1:{port}"
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        for _ in range(80):
            try:
                if w3.is_connected():
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        else:
            raise SystemExit("anvil did not start")

    # anvil account #1 and #2 (public test keys, never funded on mainnet). Only used if the operator
    # has not set real ones; the agents below sign with these and only against the local fork.
    os.environ.setdefault("SENTINEL_STRATEGY_KEY", "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
    os.environ.setdefault("SENTINEL_RISK_KEY", "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a")

    try:
        demo = ForkDemo(rpc, Path(a.out))
        evidence = demo.run()
        if not a.skip_x402:
            evidence["x402"] = run_x402(demo, free_port(), evidence)
        evidence["checks"] = demo.checks
        write_evidence(evidence, Path(a.out))
        log("evidence written: evidence/e2e_fork.json, evidence/e2e_fork.md")
        log(f"vault stats: {json.dumps(evidence['vault_stats'], ensure_ascii=False)}")
        return 0
    finally:
        if proc and not a.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
