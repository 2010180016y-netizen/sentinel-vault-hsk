"""Deployment preparation for SentinelVault — estimates cost first, then deploys only when told.

Default behaviour is a DRY RUN: it compiles nothing on chain, spends nothing, and prints an estimate.
`--confirm` plus DEPLOYER_KEY in the environment is required to broadcast. Even then the key is read
from the environment and never printed.

    python scripts/deploy.py                       # estimate only (safe)
    DEPLOYER_KEY=0x... python scripts/deploy.py --confirm
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.rpc import Rpc  # noqa: E402

GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"  # OP-stack predeploy (L1 data fee)
ABI_ORACLE = [
    {"name": "getL1Fee", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "data", "type": "bytes"}], "outputs": [{"name": "", "type": "uint256"}]},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="broadcast the deployment")
    ap.add_argument("--artifact", default="out/SentinelVault.sol/SentinelVault.json")
    a = ap.parse_args()

    s = Settings.from_env()
    r = Rpc(s.chain.rpc_url, s.chain.chain_id)
    w3 = r.w3

    art = json.load(open(a.artifact))
    bytecode = art["bytecode"]["object"]
    abi = art["abi"]

    owner = os.environ.get("SENTINEL_OWNER", "")
    strategy = os.environ.get("SENTINEL_STRATEGY_ADDR", "")
    risk = os.environ.get("SENTINEL_RISK_ADDR", "")
    if not (owner and strategy and risk):
        print("set SENTINEL_OWNER, SENTINEL_STRATEGY_ADDR, SENTINEL_RISK_ADDR (no funds needed for an estimate)")
        return 2

    p = s.policy
    policy = (p.max_slippage_bps, p.min_edge_bps, p.max_spot_twap_deviation_bps, p.max_drawdown_bps,
              p.twap_window, p.cooldown_s, p.max_slices, p.max_trade_notional, p.max_daily_notional)
    ctor = w3.codec.encode(
        ["address", "address", "address", "address", "address", "address",
         "(uint16,uint16,uint16,uint16,uint32,uint32,uint8,uint64,uint64)"],
        [owner, s.addresses.router, s.addresses.factory, s.addresses.quote, strategy, risk, policy],
    )
    data = bytes.fromhex(bytecode.removeprefix("0x")) + ctor

    gas_price = w3.eth.gas_price
    try:
        gas_estimate = w3.eth.estimate_gas({"from": owner, "data": data})
    except Exception:
        # An unfunded owner cannot be simulated. The measured deployment estimate after the review pass is ~3.8M gas,
        # so the fallback is that measurement with headroom — the previous 2.5M fallback would have run out of
        # gas mid-deploy (and `--confirm` sizes the real transaction from this number).
        gas_estimate = 4_500_000

    l2_fee = gas_estimate * gas_price
    try:
        oracle = r.contract(GAS_PRICE_ORACLE, ABI_ORACLE)
        l1_fee = oracle.functions.getL1Fee(data).call()
    except Exception:
        l1_fee = 0
    total_wei = l2_fee + l1_fee
    bal = w3.eth.get_balance(owner)

    print(json.dumps({
        "chain_id": s.chain.chain_id,
        "contract_bytes": len(data),
        "eip170_limit": 24_576,
        "fits_eip170": len(data) <= 24_576,
        "gas_estimate": gas_estimate,
        "gas_price_gwei": round(gas_price / 1e9, 3),
        "l2_fee_hsk": round(l2_fee / 1e18, 6),
        "l1_data_fee_hsk": round(l1_fee / 1e18, 6),
        "total_hsk": round(total_wei / 1e18, 6),
        "owner_balance_hsk": round(bal / 1e18, 6),
        "sufficient": bal >= total_wei,
    }, indent=1))

    if not a.confirm:
        print("dry run — nothing sent. Re-run with --confirm and DEPLOYER_KEY to deploy.")
        return 0

    key = os.environ.get("DEPLOYER_KEY", "")
    if not key:
        print("DEPLOYER_KEY missing; refusing to deploy")
        return 2
    acct = w3.eth.account.from_key(key)
    tx = {
        "from": acct.address,
        "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": max(int(gas_estimate * 1.25), 4_000_000),
        "gasPrice": gas_price,
        "chainId": s.chain.chain_id,
    }
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    address = rcpt.get("contractAddress")
    print(json.dumps({"tx": h.hex(), "status": rcpt["status"], "address": address,
                      "explorer": f"{s.chain.explorer}/tx/{h.hex()}"}, indent=1))
    if rcpt["status"] != 1 or not address:
        print("deployment FAILED — nothing to arm")
        return 1
    print(json.dumps({
        "next": [
            f"vault.setMarket({s.addresses.whsk}, 3000, true)          # owner key",
            "vault.registerStrategy(<strategyHash from agent.strategy_hash>)  # owner key",
            "vault.unpause()                                          # owner key: the vault starts paused",
            "vault.deposit(<USDT>, <amount>)                          # owner key",
            "SENTINEL_VAULT=<address> SENTINEL_LIVE=1 python scripts/execute_intent.py --confirm",
        ],
        "reminder": "the agent's strategyHash must be registered by the owner before any live trade",
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
