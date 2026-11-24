"""Run the live paper loop against HSK mainnet and persist the ledger.

    python scripts/run_paper.py --cycles 6 --interval 30

Paper only: nothing is signed, nothing is sent. `--live` is intentionally absent; live execution
lives in scripts/execute_intent.py and requires SENTINEL_LIVE=1 plus both keys in the environment.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.ledger import Ledger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--state", default="state/ledger.json")
    ap.add_argument("--capital", type=float, default=1_000.0)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    settings = Settings.from_env()
    if settings.live:
        print("SENTINEL_LIVE=1 is set, but this script is paper-only by design. Use scripts/execute_intent.py.")
        return 2

    ledger = Ledger() if a.reset or not os.path.exists(a.state) else Ledger.load(a.state)
    if ledger.high_water == 0:
        ledger.open_with(a.capital)

    agent = SentinelAgent(settings, ledger=ledger)
    print(f"strategy={agent.strategy.name} hash={agent.strategy_hash[:14]}… mode=paper")
    for i, rec in enumerate(agent.run(cycles=a.cycles, sleep_s=a.interval), start=1):
        m = rec["features"]
        print(f"[{i}/{a.cycles}] {rec['action']:<4} edge {rec['edge_bps']:>6.0f}bp / hurdle {rec['hurdle_bps']:>5.0f}bp "
              f"| dev {m.get('twap_dev_bps', 0):>+6.0f}bp rsi {m.get('rsi', 0):>4.0f} "
              f"| {'executed' if rec['executed'] else 'refused'}")
        ledger.save(a.state)

    s = agent.snapshot()
    mt = s["metrics"]
    print("\n--- summary ---")
    print(f"equity {mt['equity']:.4f} USDT · trades {mt['trades']} · fees {mt['fees_paid_quote']:.4f} · "
          f"no-trade {mt['no_trade_decisions']} · maxDD {mt['max_drawdown_bps']:.0f}bp")
    ledger.save(a.state)
    print(f"ledger -> {a.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
