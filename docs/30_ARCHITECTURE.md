# 아키텍처 — Sentinel HSK

한 줄 요지: **에이전트가 결정하고, 체인이 강제한다.** 그리고 에이전트의 *주장*까지 온체인에서 대조한다.

---

## 1. 왜 이 구조인가 (설계 근거)

| 근거 | 출처 | 설계에 반영한 방식 |
|---|---|---|
| HSKSwap은 5개 풀 성형 토폴로지 → 삼각 차익거래 경로 없음 | 2026-11-24 체인 실측 (`docs/00_FACTS.md` §7) | "차익거래 봇"으로 포지셔닝하지 않음. TWAP 대비 상대가치로 전략 재정의 |
| 수수료 0.3% (왕복 0.6%) | HSKSwap 풀 `fee()` | **엣지 허들**을 정책 1급 시민으로 승격. 허들 미달이면 거래하지 않고 그 결정을 기록 |
| QuoterV2는 온체인 호출 불가 | `IQuoterV2.sol` 주석 | 컨트랙트가 TWAP에서 공정가를 **스스로** 계산. 에이전트 견적 불신 |
| SwapRouter02엔 deadline 파라미터 없음 | Blockscout 검증 ABI | `multicall(deadline, data[])`로 만료 강제 |
| 슬롯0 스팟은 단일 tx 조작 가능 | Uniswap v3 설계 | 기준가는 10분 TWAP, 스팟은 괴리 측정용으로만 |
| 에이전트 키 유출 = 자금 유출이면 안 됨 | Zodiac/Safe 설계 철학 | 에이전트 키는 의사표시만. 자금은 볼트, 실행은 정책 통과 시에만 |
| 이중 서명을 온체인 임계서명으로 하면 매매마다 tx 2배 | Safe 패턴 분석 | **오프체인 EIP-712 이중 서명 + 온체인 단일 실행** |
| 다른 팀 3곳이 이미 "가드 볼트" | DoraHacks Builds 16건 분석 | 한도에 더해 **전략 사전등록·주장 엣지 대조·노-트레이드 기록**으로 차별화 |

---

## 2. 구성도

```
                    ┌──────────────────────────── off-chain (agent/) ───────────────────────────┐
                    │                                                                           │
  HSK mainnet RPC ──┤ rpc.py ──> market.py ──> signals.py ──> policy.py ──> core.py ──> ledger.py│
  (chain 177)       │            │  TWAP/spot     │ features      │ 검증 미러      │ 결정/회계   │
                    │            │  quotes        │ decision      │               │             │
                    │            └── bars from Swap events                        │             │
                    │                                                              ▼             │
                    │                                       executor.py (paper | chain)          │
                    └───────────────────────────────────────────────┬───────────────────────────┘
                                                                    │ dual-signed EIP-712 intent
                                                                    ▼
                    ┌──────────────────────── on-chain (src/) ───────────────────────┐
                    │ SentinelVault                                                  │
                    │  1 verify: strategy registered, nonce, deadline                │
                    │  2 verify: BOTH signatures (strategy + risk)                   │
                    │  3 verify: pool == factory.getPool(tokens, fee)                │
                    │  4 verify: TWAP fair value → minOut inside slippage band       │
                    │  5 verify: notional / daily / drawdown / cooldown / parent     │
                    │  6 execute: SwapRouter02.multicall(deadline, exactInputSingle) │
                    │  7 re-verify: reported == received, received >= minOut        │
                    │  8 account: PnL, fees, high-water, declared vs realised edge   │
                    └───────────────────────────────┬────────────────────────────────┘
                                                    ▼
                                          HSKSwap WHSK/USDT pool
```

---

## 3. 전략 (정직한 알파 원천)

**시장 구조 실측**: WHSK/USDT 0.3% 풀이 유일한 유동 시장(깊이 약 $420k, 스왑 2.8시간에 10건).
관측된 바 수익률 범위는 −60bp~+61bp, 최대 단일 이동 약 130bp. 왕복 비용(수수료 60bp + 임팩트 33bp)은
**96bp**. 따라서 이 시장에서 테이커 전략은 **이론적으로 손익분기 근처**이며, 대부분의 시점에서
정답은 "거래하지 않는다"이다.

전략은 이 사실을 전제로 설계됐다.

| 요소 | 정의 |
|---|---|
| 기준가 | 풀 자체 TWAP(기본 600초, 오라클이 서빙 가능한 창으로 자동 축소) |
| 신호 1 | **TWAP 이탈(평균회귀)**: `dev_bps = (spot − twap)/twap` |
| 신호 2 | **추세**: EMA(5) vs EMA(20) |
| 신호 3 | **체결 흐름 불균형**: 최근 20바의 매수/매도 건수 |
| 신호 4 | **변동성**: 바 수익률 표준편차 |
| 노이즈 필터 | \|dev\| < 1.5 × 변동성 이면 신호 아님 |
| 역추세 차단 | 추세가 −max(25bp, 1.5×변동성) 보다 강한 하락이면 매수 금지 |
| 기대 엣지 | `0.45×\|dev\| + 0.35×\|trend\|` (추세가 이탈과 같은 방향일 때만) |
| 허들 | `max(min_edge_bps, 왕복비용 + 20bp)` — **비용을 넘지 못하면 거래하지 않는다** |
| 사이즈 | `max_trade_notional × target_frac × conviction` (conviction 0~1) |
| 청산 | 보유 중이고 (dev ≥ 허들 또는 RSI 과매수) 이면 청산. 청산은 conviction 조건을 요구하지 않음(위험 축소는 약한 근거로도 허용) |

에이전트는 **결정적**이다. LLM은 서술만 하고 숫자를 만들지 않는다. 이유: 이 프로젝트의 전제가
"주장은 검증 가능해야 한다"이므로, 재현 불가능한 판단을 수익 주장의 근거로 쓸 수 없다.

---

## 4. 컨트랙트 사양 (`src/SentinelVault.sol`)

### 4.1 불변값
`owner`(사람, 인출·정책·키 교체), `router`, `factory`, `quoteToken`(USDT), `domainSeparator`.
프록시·업그레이드 경로 없음. 소유자도 서명된 인텐트의 내용을 바꿀 수 없다.

### 4.2 역할
| 키 | 할 수 있는 것 | 할 수 없는 것 |
|---|---|---|
| owner (사람) | 입금·인출, 정책 교체, 전략 등록, 마켓 등록, 키 교체, 재가동 | 서명 없이 거래 |
| strategySigner (에이전트 핫키) | 인텐트 서명, 부모주문 개설, 노-트레이드 기록 | 인출, 정책 변경, 단독 실행(리스크 서명 필요) |
| riskSigner (콜드) | 인텐트 공동 서명, 즉시 정지 | 재가동, 인출, 단독 실행 |

### 4.3 인텐트 (EIP-712)
```
Intent(address tokenIn,address tokenOut,uint24 fee,uint128 amountIn,uint128 minAmountOut,
       uint16 declaredEdgeBps,bytes32 strategyHash,bytes32 parentId,uint64 nonce,
       uint64 deadline,uint64 maxNotionalQuote)
```
- `uint128`인 이유: 18자리 자산의 raw 수량은 `uint64`(최대 1.8e19 ≈ 18.4 WHSK)를 넘는다. 구현 중 실제로 이 버그를 잡았다(→ 3차 회의).
- 서명 순서: 전략 → 리스크. 둘 중 하나라도 불일치면 `BadStrategySignature`/`BadRiskSignature`.

### 4.4 실행 순서 (모두 실패 시 전체 revert)
1. `paused` 아님, `deadline` 이내, `amountIn > 0`
2. `strategyRegisteredAt[strategyHash] != 0` (사전등록)
3. `nonceUsed[nonce] == false` → 사용 표시
4. 두 서명 검증(`ECDSA.tryRecover`, high-s 거부)
5. `declaredEdgeBps >= minEdgeBps`
6. 거래 leg 중 하나는 반드시 `quoteToken` (USDT) — 노셔널·평가를 쿼트 자산으로 일관되게 계산하기 위한 제약
7. 마켓 활성 + `fee` 일치 + `factory.getPool(...) == market.pool`
8. 잔고 충분
9. **TWAP 산출**(`observe(window)`) → `fairOut` 계산. `|spot−twap| ≤ maxSpotTwapDeviationBps` (1 tick ≈ 1bp)
10. `minAmountOut ∈ [fairOut×(1−maxSlippage), fairOut]`
11. 노셔널(쿼트 단위) ≤ 1회 한도(매도는 ×`maxSlices`까지 허용) 및 인텐트 자체 상한
12. 롤링 24h 한도 (매수만 계상)
13. 낙폭: `equity < highWater×(1−maxDrawdown)` 이면 **매수 금지**, 매도 허용
14. 쿨다운(신규 부모주문), 부모 예산·조각 수
15. `forceApprove(router, amountIn)` → `multicall(deadline, [exactInputSingle(...)])`
16. 승인 0 복구
17. **사후 검증**: 라우터가 보고한 `amountOut == 실제 잔고 증가분`, 그리고 `≥ minAmountOut`
18. 회계: 수수료, 평균단가, 실현손익, 승수, 고점, 선언/실현 엣지 누적

### 4.5 뷰 (심사용)
- `stats()` — 거래수, 승수, 실현손익, 수수료, 선언 엣지 합, 실현 엣지 합, 평가액, 고점, 일일 사용액
- `edgeCredibilityBps()` — `실현 엣지 합 / 선언 엣지 합` (주장이 시장에서 버틴 비율)
- `equity()` — TWAP 평가 자산 + 쿼트 잔고
- `hashIntent()` — 오프체인 서명과 온체인 digest 대조용

---

## 5. 위협 모델

| 위협 | 방어 | 검증 방법 |
|---|---|---|
| 에이전트 개인키 탈취 | 키는 의사표시만. 정책 밖 실행 불가, 인출 불가, 리스크 서명 없이 실행 불가 | `test_StrategyKeyCannotWithdraw`, `test_Revert_WhenRiskSignatureMissing` |
| 악의적/손상 라우터가 적은 수량 지급 | 라우터 보고값과 실제 잔고 증가분이 다르면 revert | `test_Revert_WhenRouterUnderpays` |
| 오프체인 견적 조작·지연 | 컨트랙트가 TWAP으로 공정가를 재계산, `minOut`을 밴드 안으로 강제 | `test_Revert_WhenSlippageTooWide`, `test_Revert_WhenMinOutAboveFair` |
| 가짜 풀 주입 | `factory.getPool` 재확인 | `test_Revert_*PoolMismatch`(마켓 미등록 경로) |
| 스팟 조작(플래시스왑) 후 실행 | 스팟–TWAP 괴리 게이트 | `test_Revert_WhenSpotDeviatesFromTwap` |
| 오라클 미준비 상태에서 거래 | `observe` 실패 시 `OracleNotReady`로 fail-closed | `test_Revert_WhenOracleNotReady` |
| 서명 재전송 | nonce 1회용 + deadline | `test_Revert_WhenNonceReplayed`, `test_Revert_WhenExpired` |
| 서명 내용 바꿔치기 | digest가 모든 필드를 덮음 | `test_Revert_WhenSignatureCoversDifferentAmount` |
| 승인 잔존으로 자금 인출 | 매매 후 allowance 0 | 불변식 `invariant_NoDanglingAllowance` |
| 전략 몰래 교체 | 사전등록된 `strategyHash`만 실행 | `test_Revert_WhenStrategyNotRegistered` |
| 자금을 퍼나르는 반복 매수 | 1회·일일·쿨다운·조각 수·낙폭 | `test_Revert_WhenDailyLimitExceeded`, `test_Revert_WhenCooldownActive`, 불변식 `invariant_DailyBudgetRespected` |
| 재진입 | `nonReentrant` + 실행 후에만 상태 갱신 | Slither(수용 항목, 아래) |
| 소유자 악용 | 프록시 없음, 정책 변경 이벤트, 정지·인출 외 권한 없음 | 코드 리뷰(불변 컨트랙트) |

---

## 6. 알려진 한계 (정직하게)

1. **수익성 실증 없음.** 2026-11-24 실측 시장에서는 왕복 비용(96bp)이 관측 이탈(≤130bp)을 거의 덮는다.
   실제 기록 바 백테스트에서 매매 0건 — 이것이 **정직한 결과**이며, 리포트에 그대로 적었다.
   합성 시나리오(라벨 명시)에서만 매매가 발생해 회계·실행 기계를 증명한다.
2. **단일 마켓·단일 leg.** USDT 대비 직접 풀이 있는 자산만 거래한다(2-hop 라우팅 없음).
3. **TWAP 창은 풀 오라클 용량에 종속.** 창을 못 채우면 거래 중단(fail-closed).
4. **소유자 키 분실 = 자금 동결.** 업그레이드가 없기 때문. 데모 자금 규모에서 수용한 트레이드오프.
5. **Slither High 1건 수용.** `reentrancy-balance`(라우터 호출 후 잔고 델타 사용) — `nonReentrant`, 불변 라우터,
   소유자 승인 토큰만 허용, 라우터 보고값과 델타 일치 강제로 위험을 닫았다. 상세는 `docs/20_EXPERT_PANEL_S3.md`.
6. **HSP 미사용.** Track 1이므로 요건 아님. Track 2의 설계 원칙(의도/승인/정산 분리)만 차용.

---

## 7. 파일 지도

```
src/SentinelVault.sol          정책 커널 (불변, 프록시 없음)
src/libraries/TwapLib.sol      TWAP consult + tick→가격 (fail-closed)
src/interfaces/IHskSwap.sol    SwapRouter02/Factory/Pool 최소 인터페이스 (검증된 ABI 기준)
src/mocks/                     테스트용 라우터·풀·팩토리·ERC20
test/SentinelVault.t.sol       단위·정책·fuzz (27)
test/SentinelVault.invariant.t.sol  불변식 5종, 128,000 호출
test/SentinelVault.fork.t.sol  HSK 메인넷 포크 실거래 3건
agent/                         AI 에이전트 코어 (Python, 의존성 web3/eth-account만)
agent/server.py + web/         대화형 대시보드
scripts/                       deploy(비용 산정·배포), backtest, run_paper
tests/                         에이전트 단위 테스트 16
docs/                          사실관계·OSS 리서치·전문가 회의·아키텍처·제출
```
