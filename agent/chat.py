"""Conversational CLI: ask the agent about status, PnL, risk, or why it did (not) trade.

    python -m agent.chat --cycles 1        # one cycle then a prompt
    python -m agent.chat                   # prompt only, reading the saved ledger
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings
from .core import SentinelAgent
from .ledger import Ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sentinel HSK — talk to the trading agent")
    ap.add_argument("--cycles", type=int, default=0, help="run N observe/decide cycles before the prompt")
    ap.add_argument("--state", default="state/ledger.json")
    ap.add_argument("--fresh", action="store_true", help="start from a clean 1,000 USDT ledger")
    ap.add_argument("--ask", default="", help="ask one question and exit")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    try:
        ledger = Ledger.load(args.state) if not args.fresh else Ledger()
    except FileNotFoundError:
        ledger = Ledger()
    if ledger.high_water == 0:
        ledger.open_with(1_000.0)

    agent = SentinelAgent(settings, ledger=ledger)
    if args.cycles:
        agent.run(cycles=args.cycles, on_step=lambda r: print("· " + r.get("explain", "")))

    if args.ask:
        print(agent.answer(args.ask))
        return 0

    print("Sentinel HSK · 페이퍼 모드. 질문 예: 상태 / 손익 / 리스크 / 왜 / 거래내역 / 엔터(1회 판단)")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q in {"exit", "quit", "q"}:
            return 0
        print(agent.answer(q))


if __name__ == "__main__":
    sys.exit(main())
