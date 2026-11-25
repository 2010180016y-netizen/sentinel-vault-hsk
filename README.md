# Sentinel HSK

**The agent decides. The chain enforces. The agent's own claims are settled on-chain.**

HSK Chain Horizon Hackathon · Seoul 2026 — Track 1 (AI Agent Trading).
An autonomous HSKSwap trading agent whose risk limits, and whose *claims*, live in an immutable
mainnet contract the agent cannot talk its way out of.

---

## See it run (3 minutes, no wallet, no gas)

```bash
python3 -m pip install -r requirements.txt
export PATH="$HOME/.foundry/bin:$PATH"
forge build
python3 -m agent.server --port 8765      # → http://127.0.0.1:8765
```

The dashboard is not a mock-up: every card reads the live agent. **Scenario lab** lets you move the
dislocation, the order size, the book's drawdown and the kill switch, and shows *which gate* decides
— with the threshold and the actual number side by side — using the same `decide / policy / guards`
the live loop runs. **Decision trace** shows the last order's gates in the contract's own order.
**x402** pays the premium endpoint from the dashboard. Walkthrough: [`docs/50_DEMO.md`](docs/50_DEMO.md).

The chain-side proof is one command and needs no keys or funds:

```bash
python3 scripts/fork_demo.py
```

It starts an anvil fork of HSK mainnet and runs **49 real transactions**: deploy the vault and the
three ERC-8004 registries, create a real 381bp dislocation with a whale selling through the real
router, watch the default strategy *refuse* and write its refusal on-chain, let a separately
pre-registered dislocation strategy enter (50.91 USDT, 166bp claim), push the pool back and exit
(601.0bp round trip), attest the vault's record through ERC-8004, settle a 0.001 USDT x402 payment
with a real `transferWithAuthorization`, and reconcile the agent's ledger against `vault.stats()`
(4/4 rows). Evidence: [`evidence/e2e_fork.md`](evidence/e2e_fork.md).

> The +601bp in that run is **not alpha** — the script creates the reversion it trades. What it proves
> is that decision → execution → settlement → reputation → payment run end to end, on the same code
> the dashboard uses, against the real HSKSwap contracts.

---

## What is different about it

Most "guarded agent" projects give the agent a box. Three teams in this hackathon already did that
(measured — see `docs/20_EXPERT_PANEL.md` §1차). Sentinel adds what a box cannot do:

1. **Strategy pre-registration.** A trade can only reference a `strategyHash` a *human* registered
   beforehand. Two strategies are registered in the demo (trend-filtered and dislocation); switching
   between them changes the hash, so the switch is visible on-chain, not silent.
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
5. **A language model on a leash.** The LLM narrates decisions it did not make and may propose a trade;
   a narration that invents a number is discarded, and a proposal is judged by the same policy and
   guards a signed intent faces (`agent/narrator.py`, 15 tests).

Plus the engineering that makes it safe: EIP-712 dual signatures (strategy + risk keys, neither can
move funds alone), fair value derived from the pool oracle instead of trusting an off-chain quote,
`factory.getPool` pool verification, a post-trade balance re-check that must agree with the router's
report, allowance hygiene, an exit that is never blocked by an entry guard, a sell that cannot exceed
the book, and a kill switch.

## Four standards, integrated (not name-dropped)

Each of these is real code with tests that fail if it regresses; the full record is
[`docs/12_OSS_INTEGRATION.md`](docs/12_OSS_INTEGRATION.md).

| Standard / OSS | What actually runs | Where |
|---|---|---|
| **elizaOS/eliza** plugin model | Agent = runtime + plugin: 6 providers compose the state, 9 actions do the work (each with a validate gate and a parameter schema), 1 evaluator records the outcome. `chat()` is routed through it. | `agent/runtime/`, `agent/plugins/sentinel.py` |
| **Uniswap/uniswap-ai** swap skills | 13 pre-trade guards (depth floor $100k, order ≤20% of depth, price impact, 5% slippage ceiling, per-run/per-period caps, honeypot exit, quote freshness, kill switch, "autonomous requires every guardrail") implemented **twice** — Python and `SwapGuardLib.sol` — over one shared 19-scenario table. The agent refuses *before* building an intent, and the refusal is recorded. | `agent/guards.py`, `src/libraries/SwapGuardLib.sol` |
| **coinbase/x402** v2 | HTTP 402 paywall with the real header transport, EIP-3009 authorization verified by signature recovery, single-use nonces, verify-then-settle. Paper settlement is the default; `chain` settlement broadcasts for real on a local fork (`MockEIP3009`) and refuses HSK mainnet unless explicitly armed. | `agent/x402/`, `agent/premium.py`, `/api/premium/edge` |
| **ERC-8004** Trustless Agents | Identity (ERC-721 + metadata + wallet proof), Reputation, Validation registries, and an adapter that attests the vault's own `edgeCredibilityBps()` and validates only when trade count, oracle health and credibility clear a threshold. | `src/registries/`, `src/Sentinel8004Adapter.sol` |

Why a guard layer at all, when the vault already has limits: the vault answers "may this signed intent
execute?". The guards answer "should this agent be in this pool at this size at all?" — and their
no-trade is written down like any other decision.

## Measured, not asserted

| Fact | Value | How |
|---|---|---|
| HSKSwap WHSK/USDT pool depth | quote side ≈ **$419–425k** (measured 2026-11-24, re-read 2026-11-25) | pool `liquidity()` + tick |
| Full round trip at 120 USDT | **65.7 bp** (33.0 in + 32.7 out; the 30bp fee is *inside* each quote) | QuoterV2 at both legs, 2026-11-25 |
| Edge hurdle (policy floor) | **146 bp** = 65.7 measured + ~80bp deliberate margin | `RiskPolicy.min_edge_bps`, `docs/30_ARCHITECTURE.md` §3 |
| Largest observed dislocation | ≈ **130 bp** — below the hurdle | recorded trade bars |
| Agent trades on the real venue | **0** (correctly refused, 20 refusals recorded) | `scripts/backtest.py` (40 bars, 80k-block window) |
| Fork run: dislocation created / entry / claim / exit | **381bp / 50.91 USDT / 166bp / 601.0bp** | `scripts/fork_demo.py` |
| Vault record after that run | 2 trades, `declaredEdgeSumBps` 166, `realizedEdgeSumBps` 600, `edgeCredibilityBps` 36,144, `avgExecutionEdgeBps` +297 | `vault.stats()` |
| x402 settlement on the fork | real `transferWithAuthorization`, gas **65,840**, nonce consumed | `evidence/e2e_fork.json` |
| Deployment cost | 3,777,090 gas measured on the fork → **1.8885 HSK** (500 gwei, ≈ $0.15); `deploy.py` estimate 3,827,004 → 1.9138. Deployed bytecode 16,666B (init 18,785B with args) | `forge` receipt / `scripts/deploy.py` |

The honest headline: **on this venue today, a taker strategy does not clear its own costs.** The
repository proves that with the pool's numbers, and proves the machinery that would trade the moment
a dislocation pays. Synthetic scenarios are labelled `SYNTHETIC` and are never presented as returns.

> The earlier version of this table said "126bp = 2×30bp fee + 33 + 33". That was wrong twice: the
> quote already contains the fee (so the fee was counted twice), and measuring impact against TWAP
> counted a dislocation as a cost — the very thing the strategy exists to capture. Corrected
> 2026-11-25; the audit trail is `docs/12_deep_audit_and_upgrade.md` F3.

## Layout

```
src/SentinelVault.sol              policy kernel — immutable, no proxy, no owner override of a signed intent
src/libraries/TwapLib.sol          TWAP consult + tick→price (fails closed)
src/libraries/SwapGuardLib.sol     Uniswap-skill swap guards as EVM predicates (parity: agent/guards.py)
src/interfaces/IHskSwap.sol        SwapRouter02 / Factory / Pool (from verified Blockscout ABIs)
src/registries/                    ERC-8004 Identity / Reputation / Validation registries
src/Sentinel8004Adapter.sol        attests the vault's record; validates against a threshold, not a claim
src/mocks/MockEIP3009.sol          test-only EIP-3009 token so x402 chain settlement can be exercised
test/                              46 unit/policy/fuzz (incl. 1 ERC-8004 live-vault test) + 5 invariants (128k calls)
                                   + 5 fork + 4 guard + 17 ERC-8004  → 74 with the fork suite
test/fixtures/policy_scenarios.json  the shared policy table — read by Solidity AND Python
test/fixtures/guard_scenarios.json   the shared guard table — read by SwapGuardLib AND agent/guards.py
agent/                             the AI agent: rpc, market, signals, policy mirror, guards, control, EIP-712
                                   intent, ledger, executor, onchain view, core, server, x402, premium, registry8004
agent/lab.py                       scenario lab: one hypothetical moment through the real gates
agent/narrator.py                  the LLM's leash: grounded narration + proposals judged by the chain rules
agent/runtime/ + agent/plugins/     elizaOS-shaped runtime + the sentinel-hsk plugin
agent/control.py                   conversational adjustments, bounded by the on-chain policy
agent/onchain.py                   reads the vault's own stats/credibility when SENTINEL_VAULT is set
agent/guards.py                    swap guards adopted from Uniswap's agent skills (refuses before signing)
agent/x402/ + agent/premium.py     x402 v2 paywall: EIP-3009 verification, single-use nonces, budget
agent/server.py + web/index.html   conversational dashboard (stdlib HTTP, no build step)
scripts/                           deploy (cost estimate first), backtest, paper runner, live runner, report,
                                   key generation, and fork_demo.py (the end-to-end run)
tests/                             173 agent tests, incl. mirror/guard parity, x402 round trip, runtime,
                                   live wiring, scenario lab, narrator, HTTP API
docs/                              facts, OSS research, expert panels, architecture, slither triage,
                                   the 7-step audit, the demo script, submission
oss/                               cloned upstream repositories used for research (git-ignored)
```

## Quick start

```bash
python3 -m pip install -r requirements.txt
export PATH="$HOME/.foundry/bin:$PATH"

forge test --no-match-path 'test/SentinelVault.fork.t.sol'   # 69 tests (no network)
forge test                                                   # 74 incl. mainnet-fork integration
python3 -m pytest tests/ -q                                  # 173 agent tests
python3 scripts/backtest.py                                  # real bars + labelled synthetic panels
python3 scripts/fork_demo.py                                 # 49-tx end-to-end on an HSK fork
python3 scripts/run_paper.py --cycles 3
python3 -m agent.server --port 8765                          # then open http://127.0.0.1:8765
# the paid endpoint appears when a payee address is configured:
SENTINEL_X402_PAY_TO=0x.. python3 -m agent.server --port 8765   # /api/premium/edge answers 402 first
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

## Live mode: off by default, and one more interlock

`SENTINEL_LIVE=1` plus `SENTINEL_VAULT`, `SENTINEL_STRATEGY_KEY`, `SENTINEL_RISK_KEY` are all required
before a single transaction can be signed, and `SENTINEL_LIVE=1` now actually selects the chain
executor (it used to silently keep the paper one). `scripts/execute_intent.py` additionally requires
`--confirm`, and the paper runner refuses to run with live mode set.

A transaction to a **non-loopback** RPC also needs `SENTINEL_CONFIRM_MAINNET_TX=1`. That interlock was
added after the fork demo was caught reading `HSK_RPC` from the environment and pointing at mainnet; the
node rejected it for lack of funds, but a funded key would have spent gas. A local anvil fork needs no
flag.

## Static analysis

Slither (solc 0.8.24, optimizer, via-ir), 2026-11-25 re-run: **13 findings — High 1, Medium 1, Low 9,
Informational 2**, each triaged in `docs/15_SLITHER_TRIAGE.md`. The High (`reentrancy-balance`) is the
inherent shape of measuring a delivered amount across an external call; it is closed by `nonReentrant`,
an immutable router, an owner-only market whitelist, and a router-report/balance-delta cross-check that
must agree. The test-only `MockEIP3009` adds 3 informational findings; it never holds funds.

CI is prepared in `ci/ci.yml` (contracts / agent / fork jobs). It lives outside `.github/workflows/`
because the connected GitHub App has no `workflows` permission; copy it there once Actions is enabled
for the repository.

## Honest limitations

- No profitability is claimed. The measured result is zero trades on a venue whose costs are close to
  its dislocations; that is the finding, not a failure to implement. The +601bp fork run is a scripted
  reversion, not alpha.
- Single quote asset (USDT), single-hop pools only. WETH/WBTC pools exist but are thinner and pricier
  (measured: 58.7 / 56.3 bp impact at 120 USDT vs 33 bp for WHSK) — the agent says so when you switch.
- `owner` key loss means frozen funds (no upgrade path — a deliberate choice).
- If the pool oracle cannot serve the TWAP window, trading stops (fail closed) and `stats().oracleHealthy`
  turns false while the read-only views keep working.
- The conversational controls move *agent* behaviour only; the on-chain policy is owner-key territory.
- Mainnet deployment and the DoraHacks submission are prepared but not executed (no gas spend, no
  account submission without explicit approval).

## License

MIT for this repository's code (see `LICENSE`). `lib/v3-core-08/TickMath.sol` is GPL-2.0-or-later (Uniswap); if you
combine it into a single deployed work, that work inherits GPL-2.0-or-later.
