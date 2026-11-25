# End-to-end run on an HSK mainnet fork

- generated: 2026-11-25 15:22:44
- chain: 177 · fork of https://mainnet.hsk.xyz
- pool: `0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC` · depth ≈ $424,873
- vault: `0x38a024C0b412B9d1db8BC398140D00F5Af3093D4`
- identity/reputation/validation: `0x5fc748f1FEb28d7b76fa1c6B07D8ba2d5535177c` / `0xB82008565FdC7e44609fA118A4a681E92581e680` / `0x2a810409872AfC346F9B5b26571Fd6eC42EA4849`
- adapter: `0xb9bEECD1A582768711dE1EE7B0A1d582D9d72a6C`

## What happened

1. A whale sold WHSK through the real router until the pool's spot sat **381bp** off its own TWAP.
2. The default (trend-filtered) strategy refused — adverse trend — and the refusal is a transaction: `0x746f6ef4d52c60369a82aa67ff6355c0998905d24beed7423cbaa4547aac5159`.
3. The pre-registered dislocation strategy bought **51.708177 USDT** claiming **166.48bp** (hurdle 146.0bp): `0x63831b5968ec40a490f4cc2356bc3e70b4931a47db661cdb9c1f771e53b61b7b`.
4. The pool was pushed back above TWAP; the agent exited at **601.0bp** round trip: `0xac4143d647f6a73100e1ba8a96dabc94aec849ed46e595848640fd45984442f8`.
5. ERC-8004: agent #1 validated and rated from `vault.stats()` — credibility feedback **36144**, execution feedback **297**.
6. x402: **0.001 USDT** settled with a real `transferWithAuthorization` — tx `0xd44fce5ccdbc33531aa6899d557899d915b6ddf0336caf7911df770fe2114c5a` (gas 65840), nonce consumed on-chain.

## Vault's own numbers (not the agent's)

| field | value |
|---|---|
| tradeCount | 2 |
| winCount | 1 |
| realizedPnlQuote | 3107457 |
| feesPaidQuote | 315621 |
| declaredEdgeSumBps | 166 |
| realizedEdgeSumBps | 600 |
| equity | 1003107457 |
| highWater | 1003107457 |
| oracleHealthy | True |
| executionEdgeSumBps | 594 |
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
- ✅ default strategy refuses the dump (adverse trend) — adverse trend -188bp vs bar vol 31bp: mean reversion disarmed
- ✅ the refusal was written on-chain — 0x746f6ef4d52c60369a82aa67ff6355c0998905d24beed7423cbaa4547aac5159
- ✅ 대화 → 정책: 온체인 1회 한도를 넘는 요청은 실제 한도와 함께 거부된다 — ⛔ 요청한 500 USDT는 온체인 1회 한도 200 USDT를 넘습니다. 이 한도는 소유자 키의 정책(정책 교체 트랜잭션)으로만 바뀌고, 대화로는 바꿀 수 없습니다.
- ✅ dislocation strategy enters — edge=166.48bp size=51.708177 USDT reasons=spot 370bp below TWAP, reversion edge 166bp > hurdle 146bp
- ✅ vault tradeCount = 1 — tradeCount=1
- ✅ declared edge registered on entry — declared=166 agent=166.48
- ✅ allowance reset after the trade
- ✅ position booked equals the fill — qty=663240102953806772489
- ✅ the agent's own ledger holds the position — ledger_qty=663240102953806772489 keys=['0xB210D2120d57b758EE163cFfb43e73728c471Cf1']
- ✅ agent exits after the reversion — action=SELL realised=601.0bp edge=191.23/146.0 reasons=spot above TWAP (+279bp, RSI 0) -> take profit policy=OK guards=OK
- ✅ vault tradeCount = 2 — tradeCount=2
- ✅ the entry claim was settled with a realised number — realisedEdgeSum=600bp credibility=36144bp
- ✅ validation answered 100 from the vault's record — response=100 tag=hard
- ✅ reputation = the vault's own credibility number — summary=36144 credibility=36144
- ✅ off-chain ledger reconciles with the vault — all rows match
- ✅ x402: the agent's payment settled on-chain — status=200 settlement={'success': True, 'transaction': '0xd44fce5ccdbc33531aa6899d557899d915b6ddf0336caf7911df770fe2114c5a', 'network': 'eip155:177', 'payer': '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'} error=
- ✅ x402: settlement returned a real transaction hash — 0xd44fce5ccdbc33531aa6899d557899d915b6ddf0336caf7911df770fe2114c5a
- ✅ x402: the settlement transaction succeeded — gas=65840
- ✅ x402: the EIP-3009 nonce is consumed on-chain — events=1
- ✅ x402: authorizationState[nonce] is true — nonce=0x12434dd97308228f…
- ✅ x402: the payee received exactly 0.001 USDT — payee=10001000
- ✅ x402: the premium payload is the agent's book — /api/premium/edge

All 49 transactions: see `e2e_fork.json` → `steps`.
