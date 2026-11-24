# Sentinel HSK

**The agent decides. The chain enforces. The agent's own claims are settled on-chain.**

HSK Chain Horizon Hackathon · Seoul 2026 — Track 1 (AI Agent Trading).
An autonomous HSKSwap trading agent whose risk limits, and whose *claims*, live in an immutable
mainnet contract the agent cannot talk its way out of.

---

## What is different about it

Most "guarded agent" projects give the agent a box. Three teams in this hackathon already did that
(measured — see `docs/00_FACTS.md` §8). Sentinel adds three things a box cannot do:

1. **Strategy pre-registration.** A trade can only reference a `strategyHash` a *human* registered
   beforehand. A strategy cannot be silently swapped after a bad run to make the numbers look better.
2. **Declared edge, reconciled on-chain.** Every trade signs the edge (in bps) it claims to have.
   The vault refuses any trade claiming less than the cost hurdle, recomputes the *realised* edge
   against the pool's own TWAP, and publishes `edgeCredibilityBps()` — the share of the agent's
   claims that survived contact with the market.
3. **Refusal is evidence.** `recordNoTrade()` writes deliberate refusals to the same immutable log,
   so "did nothing because the fee hurdle was not met" is a recorded decision, not an absence.

Plus the engineering that makes it safe: EIP-712 dual signatures (strategy + risk keys, neither can
move funds alone), fair value derived from the pool oracle instead of trusting an off-chain quote,
`factory.getPool` pool verification, a post-trade balance re-check, allowance hygiene, kill switch.

## Measured, not asserted

| Fact | Value | How |
|---|---|---|
| HSKSwap WHSK/USDT pool depth | ≈ $420k | pool `liquidity()` + tick |
| Round-trip cost at 120 USDT | **96 bp** | fee 0.3% ×2 + quoted impact 33 bp |
| Largest observed dislocation | ≈ 130 bp | 36 recorded trade bars, 2026-11-24 |
| Agent trades on the real venue | **0** (correctly refused) | `scripts/backtest.py` |
| One real swap, on a mainnet fork | 100 USDT → 1,234.08 WHSK, −32 bp vs TWAP = exactly the 0.3% fee | `forge test --match-contract SentinelVaultForkTest` |
| Deployment cost | 3,498,282 gas → **1.535 HSK** (≈ $0.12) | `scripts/deploy.py` on a mainnet fork |

The honest headline: **on this venue today, a taker strategy does not clear its own costs.** The
repository proves that with the pool's numbers, and proves the machinery that would trade the moment
a dislocation pays. Synthetic scenarios are labelled `SYNTHETIC` and are never presented as returns.

## Layout

```
src/SentinelVault.sol              policy kernel — immutable, no proxy, no owner override of a signed intent
src/libraries/TwapLib.sol          TWAP consult + tick→price (fails closed)
src/interfaces/IHskSwap.sol        SwapRouter02 / Factory / Pool (from verified Blockscout ABIs)
test/                              27 unit/policy/fuzz + 5 invariants (128k calls) + 3 mainnet-fork tests
agent/                             the AI agent: rpc, market, signals, policy mirror, EIP-712 intent, ledger, core
agent/server.py + web/index.html   conversational dashboard (stdlib HTTP, no build step)
scripts/                           deploy (cost estimate first), backtest, paper runner
tests/                             16 agent unit tests
docs/                              facts, OSS research, expert panels, architecture, submission
oss/                               cloned upstream repositories used for research (<gitignore>d</gitignore>)
```

## Quick start

```bash
python3 -m pip install -r requirements.txt
export PATH="$HOME/.foundry/bin:$PATH"

forge test                          # contracts, incl. mainnet-fork integration (needs network)
python3 -m pytest tests/ -q         # agent
python3 scripts/backtest.py         # real bars + labelled synthetic panels
python3 scripts/run_paper.py --cycles 3
python3 -m agent.server --port 8765 # then open http://127.0.0.1:8765
```

Deployment is dry by default:

```bash
SENTINEL_OWNER=0x.. SENTINEL_STRATEGY_ADDR=0x.. SENTINEL_RISK_ADDR=0x.. python scripts/deploy.py
```

## Live mode is off by default, on purpose

`SENTINEL_LIVE=1` plus `SENTINEL_VAULT`, `SENTINEL_STRATEGY_KEY`, `SENTINEL_RISK_KEY` are all
required before a single transaction can be signed, and the paper runner refuses to run with live
mode set. The user's money is not the demo budget.

## Honest limitations

- No profitability is claimed. The measured result is zero trades on a venue whose costs exceed its
  dislocations; that is the finding, not a failure to implement.
- Single quote asset (USDT), single-hop pools only.
- `owner` key loss means frozen funds (no upgrade path — a deliberate choice).
- Slither High 1 (`reentrancy-balance`) accepted with documented mitigations: `docs/20_EXPERT_PANEL_S3.md`.

## License

MIT for this repository's code. `lib/v3-core-08/TickMath.sol` is GPL-2.0-or-later (Uniswap); if you
combine it into a single deployed work, that work inherits GPL-2.0-or-later.
