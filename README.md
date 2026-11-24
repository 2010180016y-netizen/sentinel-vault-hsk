# Sentinel HSK

**The agent decides. The chain enforces. The agent's own claims are settled on-chain.**

HSK Chain Horizon Hackathon · Seoul 2026 — Track 1 (AI Agent Trading).
An autonomous HSKSwap trading agent whose risk limits, and whose *claims*, live in an immutable
mainnet contract the agent cannot talk its way out of.

---

## What is different about it

Most "guarded agent" projects give the agent a box. Three teams in this hackathon already did that
(measured — see `docs/20_EXPERT_PANEL.md` §1차). Sentinel adds what a box cannot do:

1. **Strategy pre-registration.** A trade can only reference a `strategyHash` a *human* registered
   beforehand. A strategy cannot be silently swapped after a bad run to make the numbers look better —
   including when the change came from a chat message (`strategyHash` is re-derived and the reply says
   the new hash must be registered).
2. **A claim the chain can check.** Every entry signs the edge (bps) it claims. The vault refuses a
   claim below its policy floor, refuses a claim **larger than the dislocation the pool is actually
   showing** (`EdgeClaimAboveDislocation`), recomputes realised edge from the pool's own TWAP, and
   publishes two differently-named numbers: `edgeCredibilityBps()` (claims vs round-trip outcomes) and
   `avgExecutionEdgeBps()` (per-leg execution, negative for any taker).
3. **Refusal is evidence.** `recordNoTrade()` writes a deliberate refusal to the same immutable log a
   trade lands in — with the hurdle it was judged against — and consumes the nonce, so one nonce is
   exactly one decision. The live agent calls it on every refusal.
4. **A conversation that can steer, not just report.** "1회 한도 100", "거래 빈도 줄여", "리스크 낮춰",
   "WETH로 바꿔" move the agent's behaviour *inside* the on-chain limits; anything beyond them is
   refused with the real limit quoted back, because the chat can never move what only the owner key can.

Plus the engineering that makes it safe: EIP-712 dual signatures (strategy + risk keys, neither can
move funds alone), fair value derived from the pool oracle instead of trusting an off-chain quote,
`factory.getPool` pool verification, a post-trade balance re-check that must agree with the router's
report, allowance hygiene, an exit that is never blocked by an entry guard, and a kill switch.

## Measured, not asserted

| Fact | Value | How |
|---|---|---|
| HSKSwap WHSK/USDT pool depth | quote side ≈ **$419k** (both sides ≈ $839k) | pool `liquidity()` + tick, 2026-11-24 |
| Full round trip at 120 USDT | **126 bp** (2 × 30bp fee + 33bp in + 33bp out) | QuoterV2 at both legs |
| Edge hurdle (policy floor) | **146 bp** = 126 measured + 20 margin | `RiskPolicy.min_edge_bps`, `docs/30_ARCHITECTURE.md` §3 |
| Largest observed dislocation | ≈ **130 bp** — below the hurdle | 35 recorded trade bars, 2026-11-24 |
| Agent trades on the real venue | **0** (correctly refused, 15 refusals recorded) | `scripts/backtest.py` |
| One real swap, on a mainnet fork (2026-11-24 run) | a real whale swap pushes the pool 108bp off TWAP, the vault buys the dip: 100 USDT → 1,247.39 WHSK vs TWAP fair 1,238.11 = **+74bp after paying the 30bp fee** | `forge test --match-contract SentinelVaultForkTest` |
| Deployment cost | 3,802,309 gas → **1.90 HSK** (500 gwei, ≈ $0.15), bytecode 18,675B | `scripts/deploy.py` (estimate) |

The honest headline: **on this venue today, a taker strategy does not clear its own costs.** The
repository proves that with the pool's numbers, and proves the machinery that would trade the moment
a dislocation pays. Synthetic scenarios are labelled `SYNTHETIC` and are never presented as returns.

## Layout

```
src/SentinelVault.sol              policy kernel — immutable, no proxy, no owner override of a signed intent
src/libraries/TwapLib.sol          TWAP consult + tick→price (fails closed)
src/interfaces/IHskSwap.sol        SwapRouter02 / Factory / Pool (from verified Blockscout ABIs)
test/                              40 unit/policy/fuzz + 5 invariants (128k calls) + 5 mainnet-fork tests
test/fixtures/policy_scenarios.json  the shared policy table — read by Solidity AND Python
agent/                             the AI agent: rpc, market, signals, policy mirror, control, EIP-712 intent,
                                   ledger, executor, onchain view, core, server
agent/control.py                   conversational adjustments, bounded by the on-chain policy
agent/onchain.py                   reads the vault's own stats/credibility when SENTINEL_VAULT is set
agent/server.py + web/index.html   conversational dashboard (stdlib HTTP, no build step)
scripts/                           deploy (cost estimate first), backtest, paper runner, live runner, report,
                                   key generation (gen_keys.py — local only, prints addresses never keys)
tests/                             48 agent tests, incl. mirror parity, intent parity (anvil), live wiring
docs/                              facts, OSS research, expert panels (4 rounds), architecture, slither triage, submission
oss/                               cloned upstream repositories used for research (<gitignore>d</gitignore>)
```

## Quick start

```bash
python3 -m pip install -r requirements.txt
export PATH="$HOME/.foundry/bin:$PATH"

forge test                          # 47 tests: contracts incl. mainnet-fork integration (needs network)
python3 -m pytest tests/ -q         # 48 agent tests (the anvil parity test needs forge/anvil)
python3 scripts/backtest.py         # real bars + labelled synthetic panels
python3 scripts/run_paper.py --cycles 3
python3 -m agent.server --port 8765 # then open http://127.0.0.1:8765
```

Deployment is dry by default:

```bash
SENTINEL_OWNER=0x.. SENTINEL_STRATEGY_ADDR=0x.. SENTINEL_RISK_ADDR=0x.. python scripts/deploy.py
```

### The three keys

The vault has three roles and refuses to deploy when the two signers are one key
(`strategySigner_ == riskSigner_ → InvalidConfig`; keeper of the funds and keeper of the risk check
must not be the same key). Generate them locally, then feed the file to the deploy script:

```bash
python scripts/gen_keys.py                 # writes .env.wallets.local (mode 600, git-ignored); prints addresses only
set -a; . ./.env.wallets.local; set +a
python scripts/deploy.py                   # estimate; add --confirm with DEPLOYER_KEY to broadcast
```

The file matches `.gitignore` (`.env*`, `*.local`, `*wallet*`, `*.key`), no key is printed to a log,
and nothing in the pipeline reads a key from a tracked file. The owner key is also the deployer and the
only key that can arm, re-policy or unpause the vault; strategy and risk each sign one half of every
EIP-712 intent, so neither can move funds alone. Losing the owner key freezes the funds — there is no
upgrade path, on purpose.

## Live mode is off by default, on purpose

`SENTINEL_LIVE=1` plus `SENTINEL_VAULT`, `SENTINEL_STRATEGY_KEY`, `SENTINEL_RISK_KEY` are all
required before a single transaction can be signed, `scripts/execute_intent.py` additionally requires
`--confirm`, and the paper runner refuses to run with live mode set. The user's money is not the demo
budget.

## Static analysis

Slither (solc 0.8.24, optimizer, via-ir): **13 findings — High 1, Medium 1, Low 9, Informational 2**,
each triaged in `docs/15_SLITHER_TRIAGE.md`. The High (`reentrancy-balance`) is the inherent shape of
measuring a delivered amount across an external call; it is closed by `nonReentrant`, an immutable
router, an owner-only market whitelist, and a router-report/balance-delta cross-check that must agree.

## Honest limitations

- No profitability is claimed. The measured result is zero trades on a venue whose costs exceed its
  dislocations; that is the finding, not a failure to implement.
- Single quote asset (USDT), single-hop pools only. WETH/WBTC pools exist but are thinner and pricier
  (measured: 58.7 / 56.3 bp impact at 120 USDT vs 33 bp for WHSK) — the agent says so when you switch.
- `owner` key loss means frozen funds (no upgrade path — a deliberate choice).
- If the pool oracle cannot serve the TWAP window, trading stops (fail closed) and `stats().oracleHealthy`
  turns false while the read-only views keep working.
- The conversational controls move *agent* behaviour only; the on-chain policy is owner-key territory.

## License

MIT for this repository's code (see `LICENSE`). `lib/v3-core-08/TickMath.sol` is GPL-2.0-or-later (Uniswap); if you
combine it into a single deployed work, that work inherits GPL-2.0-or-later.
