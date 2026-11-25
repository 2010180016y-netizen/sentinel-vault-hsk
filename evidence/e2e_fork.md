# End-to-end run on an HSK mainnet fork

- generated: 2026-11-25 15:13:43
- chain: 177 · fork of https://mainnet.hsk.xyz
- pool: `0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC` · depth ≈ $424,873
- vault: `0x38a024C0b412B9d1db8BC398140D00F5Af3093D4`
- identity/reputation/validation: `0x5fc748f1FEb28d7b76fa1c6B07D8ba2d5535177c` / `0xB82008565FdC7e44609fA118A4a681E92581e680` / `0x2a810409872AfC346F9B5b26571Fd6eC42EA4849`
- adapter: `0xb9bEECD1A582768711dE1EE7B0A1d582D9d72a6C`

## What happened

1. A whale sold WHSK through the real router until the pool's spot sat **381bp** off its own TWAP.
2. The default (trend-filtered) strategy refused — adverse trend — and the refusal is a transaction: `0xd8a21e1d592ea0161c92bc3dec33020850b1845321bff25ca585774d3a800942`.
3. The pre-registered dislocation strategy bought **50.907559 USDT** claiming **166.48bp** (hurdle 146.0bp): `0x652aad507f99e5e3f924c449bedf875e40357b0728c39d9537bef2cee7c39ee6`.
4. The pool was pushed back above TWAP; the agent exited at **601.0bp** round trip: `0x85744e532416981bdddcca471398d6373f18de2750f4abc7a1ba1c24a720cc53`.
5. ERC-8004: agent #1 validated and rated from `vault.stats()` — credibility feedback **36144**, execution feedback **297**.
6. x402: **0.001 USDT** settled with a real `transferWithAuthorization` — tx `0x23d80589d5096ed0832057682a96ef3d3c44dd46688eae93ccd3cd28420a1934` (gas 65840), nonce consumed on-chain.

## Vault's own numbers (not the agent's)

| field | value |
|---|---|
| tradeCount | 2 |
| winCount | 1 |
| realizedPnlQuote | 3059353 |
| feesPaidQuote | 310718 |
| declaredEdgeSumBps | 166 |
| realizedEdgeSumBps | 600 |
| equity | 1003059353 |
| highWater | 1003059353 |
| oracleHealthy | True |
| executionEdgeSumBps | 595 |
| edgeCredibilityBps | 36144 |
| avgExecutionEdgeBps | 297 |

## Reconciliation (agent ledger vs vault)

| field | ledger | vault | delta | match |
|---|---|---|---|---|
| trades | 2 | 2.0 | 0.0 | ✅ |
| declared_edge_sum_bps | 166 | 166.0 | 0.0 | ✅ |
| realised_edge_sum_bps | 601.0 | 600.0 | 1.0 | ✅ |
| avg_execution_edge_bps | 299.3 | 297.0 | 2.3 | ✅ |

## Checks

- ✅ chain is HSK mainnet (forked) — chain_id=177
- ✅ pool resolved by the factory — 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC
- ✅ vault holds the deposit — 1000.00 USDT
- ✅ the pool starts near its own TWAP — dislocation=0bp spot=-301484 twap=-301484
- ✅ a real dislocation was created — 381bp
- ✅ default strategy refuses the dump (adverse trend) — adverse trend -188bp vs bar vol 30bp: mean reversion disarmed
- ✅ the refusal was written on-chain — 0xd8a21e1d592ea0161c92bc3dec33020850b1845321bff25ca585774d3a800942
- ✅ dislocation strategy enters — edge=166.48bp size=50.907559 USDT reasons=spot 370bp below TWAP, reversion edge 166bp > hurdle 146bp
- ✅ vault tradeCount = 1 — tradeCount=1
- ✅ declared edge registered on entry — declared=166 agent=166.48
- ✅ allowance reset after the trade
- ✅ position booked equals the fill — qty=652972164575326718160
- ✅ the agent's own ledger holds the position — ledger_qty=652972164575326718160 keys=['0xB210D2120d57b758EE163cFfb43e73728c471Cf1']
- ✅ agent exits after the reversion — action=SELL realised=601.0bp edge=191.18/146.0 reasons=spot above TWAP (+279bp, RSI 0) -> take profit policy=OK guards=OK
- ✅ vault tradeCount = 2 — tradeCount=2
- ✅ the entry claim was settled with a realised number — realisedEdgeSum=600bp credibility=36144bp
- ✅ validation answered 100 from the vault's record — response=100 tag=hard
- ✅ reputation = the vault's own credibility number — summary=36144 credibility=36144
- ✅ off-chain ledger reconciles with the vault — all rows match
- ✅ x402: the agent's payment settled on-chain — status=200 settlement={'success': True, 'transaction': '0x23d80589d5096ed0832057682a96ef3d3c44dd46688eae93ccd3cd28420a1934', 'network': 'eip155:177', 'payer': '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'} error=
- ✅ x402: settlement returned a real transaction hash — 0x23d80589d5096ed0832057682a96ef3d3c44dd46688eae93ccd3cd28420a1934
- ✅ x402: the settlement transaction succeeded — gas=65840
- ✅ x402: the EIP-3009 nonce is consumed on-chain — events=1
- ✅ x402: authorizationState[nonce] is true — nonce=0x00dd42d94cbb84d6…
- ✅ x402: the payee received exactly 0.001 USDT — payee=10001000
- ✅ x402: the premium payload is the agent's book — /api/premium/edge

All 49 transactions: see `e2e_fork.json` → `steps`.
