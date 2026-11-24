"""Live runner: one observe/decide/execute cycle against a deployed SentinelVault.

    python scripts/execute_intent.py                 # dry run: prints the intent it *would* sign
    python scripts/execute_intent.py --confirm       # signs and sends (needs SENTINEL_LIVE=1 + keys)

Everything is explicit on purpose:

  * `SENTINEL_LIVE=1`, `SENTINEL_VAULT`, `SENTINEL_STRATEGY_KEY`, `SENTINEL_RISK_KEY` are all required
    before a single byte is signed, and `--confirm` is required as well. The paper runner refuses to run
    with live mode set; this runner refuses to run without a confirmation.
  * A refusal is sent as `recordNoTrade`, so the decision lands in the same log a trade would.
  * The amount actually received is read from the `IntentExecuted` log of the receipt — never assumed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.executor import ChainExecutor  # noqa: E402
from agent.ledger import Ledger  # noqa: E402
from agent.policy import build_min_out, check  # noqa: E402
from agent.rpc import Rpc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually sign and send the intent")
    ap.add_argument("--state", default="state/ledger.json")
    a = ap.parse_args()

    settings = Settings.from_env()
    if not settings.live:
        print("SENTINEL_LIVE is not 1: this script will not sign. Run scripts/run_paper.py for the paper loop.")
        return 2
    if not settings.vault:
        print("SENTINEL_VAULT is not set")
        return 2
    if not os.environ.get("SENTINEL_STRATEGY_KEY") or not os.environ.get("SENTINEL_RISK_KEY"):
        print("SENTINEL_STRATEGY_KEY and SENTINEL_RISK_KEY are both required (dual control)")
        return 2

    ledger = Ledger.load(a.state) if os.path.exists(a.state) else Ledger()
    if ledger.high_water == 0:
        ledger.open_with(0.0)

    rpc = Rpc(settings.chain.rpc_url, settings.chain.chain_id)
    agent = SentinelAgent(settings, rpc=rpc, ledger=ledger, executor=ChainExecutor(settings, rpc, ledger))

    obs = agent.observe()
    d = agent.decide(obs)
    print(f"market {agent.market.symbol}/USDT · spot {obs.state.twap_deviation_bps:+.1f}bp vs "
          f"{obs.state.twap_window}s TWAP · round-trip cost "
          f"{2 * agent.market.fee / 100 + obs.impact_bps_buy + obs.impact_bps_sell:.1f}bp")
    print(f"decision {d.action} edge {d.edge_bps:.0f}bp / hurdle {d.hurdle_bps:.0f}bp · {d.reasons[0]}")
    print(f"strategyHash {agent.strategy_hash}")

    if not d.is_trade:
        print("refusal — nothing to sign." + (" --confirm would send recordNoTrade." if a.confirm else ""))
        if a.confirm:
            print(json.dumps(agent._record_refusal(d)))  # noqa: SLF001 - the runner is the wiring
        return 0

    is_buy = d.action == "BUY"
    pos = ledger.positions.get(agent.market.asset)
    amount_in = int(round(d.size_quote * 1e6)) if is_buy else (pos.qty if pos else 0)
    fair = obs.fair_buy if is_buy else obs.fair_sell
    quoted = obs.quote_buy if is_buy else obs.quote_sell
    min_out = build_min_out(quoted, fair, agent.policy.max_slippage_bps)

    print(json.dumps({
        "token_in": settings.addresses.quote if is_buy else agent.market.asset,
        "token_out": agent.market.asset if is_buy else settings.addresses.quote,
        "amount_in": amount_in,
        "fair_out": fair,
        "quoted_out": quoted,
        "min_amount_out": min_out,
        "declared_edge_bps": int(round(d.edge_bps)) if is_buy else 0,
        "max_notional_quote": int(agent.policy.max_trade_notional if is_buy else 2**63 - 1),
        "nonce": ledger.nonce + 1,
    }, indent=1))

    verdict = check(
        is_buy=is_buy,
        notional_quote=(d.size_quote if is_buy else fair / 1e6),
        declared_edge_bps=int(round(d.edge_bps)) if is_buy else 0,
        min_out=min_out,
        fair_out=fair,
        max_notional_quote=(agent.policy.max_trade_notional / 1e6) if is_buy else 1e18,
        policy=agent.policy,
        account=agent._account_state(obs),  # noqa: SLF001
        market=agent._market_state(obs, fair, quoted),  # noqa: SLF001
        now_ts=__import__("time").time().__int__(),
    )
    if not verdict.ok:
        print(f"policy mirror would refuse: {verdict.reason} — {verdict.detail}. Not signing.")
        return 1

    if not a.confirm:
        print("dry run — nothing signed. Re-run with --confirm to send.")
        return 0

    rec = agent.step()
    ledger.save(a.state)
    print(json.dumps({k: rec.get(k) for k in ("executed", "tx", "amount_out", "leg_edge_bps", "equity", "explain")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
