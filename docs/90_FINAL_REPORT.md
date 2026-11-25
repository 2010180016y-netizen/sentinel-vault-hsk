# HSK Chain Horizon Hackathon · Seoul — 제출 준비 보고

**최종 갱신: 2026-11-25 (오픈소스 4건 통합 완료) · Track 1 (AI Agent Trading) 확정**
산출물: `competitions/hsk-chain-seoul-2026/` → GitHub `2010180016y-netizen/sentinel-vault-hsk` (master)
핵심 파일: `src/SentinelVault.sol` · `agent/` · `test/` · `tests/` · `docs/` · `evidence/REPORT.html`

---

## 0. 한 줄 결론

Track 1로 확정하고 **"에이전트가 결정하고, 체인이 강제하며, 에이전트의 주장까지 온체인에서 대조하는"** 프로토타입을
완성한 뒤, 같은 저장소를 처음부터 끝까지 다시 읽으며 **21건의 결함·빈틈을 찾아 전부 수정**했다.
지금 상태: Foundry **69/69**(단위 40 + 불변식 5 + 메인넷 포크 5 + 스왑 가드 4 + ERC-8004 18), Python **125/125**, Slither **13건 전부 트리아지**.
2026-11-25에 elizaOS·Uniswap uniswap-ai·coinbase x402·ERC-8004를 실제 코드로 통합했다(`docs/12_OSS_INTEGRATION.md`).
남은 것은 회기님 결정 3건(지갑·HSK 가스·DoraHacks 등록)과 데모 영상이다. GitHub 공개 저장소는 push 완료.

---

## 1. 대회 실측 사실 (2026-11-24 직접 조회)

| 항목 | 값 |
|---|---|
| 등록·제출 마감 | **2026-12-03 13:00 KST** (등록과 제출 동시 마감, 이후 제출은 심사 제외) |
| 기간 | 2026-11-20 ~ 12-05, 데모 발표팀 발표 12-04, Demo Day 12-05 서울, 수상 발표 12-06 |
| 상금 | 총 12,000 USDT (트랙별 6,000: 3,000 / 2,000 / 1,000) |
| 필수 요건 | ①HSK Chain 기반 ②해커톤 기간 중 신규 개발 ③**스마트 컨트랙트 메인넷 배포**(없으면 데모 선정 자격 상실) ④Track 2는 HSP 필수 ⑤공식 제품 사용 시 가산점 |
| 심사 | 공개 기준은 "완성도·기술 성숙도·혁신성"으로 데모 발표팀을 선정한다는 문장뿐, 가중치 비공개 |
| 문서 모순 | 본문 "메인넷 배포 없으면 자격 상실" vs Testnet 탭 "수상팀은 종료 후 배포 가능". Q&A 답변 0건 → **제출 전 배포**를 기본으로 |

---

## 2. 재점검 결과 (2026-11-24, `docs/20_EXPERT_PANEL_S4.md`)

### 2.1 자금·정책이 걸린 것 (7건)

| # | 결함 | 왜 치명적인가 | 수정 |
|---|---|---|---|
| 1 | 엣지 허들(`minEdgeBps`) 미검증 | 소유자가 0으로 두면 차별화의 심장이 조용히 꺼짐 | `_validatePolicy`로 1..5,000 + 쿨다운 상한 강제 |
| 2 | **매도가 엣지 게이트에 막힘** | 낙폭 구간 "매도만 허용"인데 청산이 `EdgeBelowHurdle`로 거부 | 게이트를 매수 전용으로 |
| 3 | 쿨다운이 매도에도 적용 | 손절이 최대 60초 지연 | `lastBuyAt` — 진입 전용 |
| 4 | `recordNoTrade`가 nonce 미소비 | 거부 로그 재전송·중복 가능 | nonce 소비 + 이벤트에 `hurdleBps` 추가 |
| 5 | `withdraw`가 원가 장부를 남김 | 인출 후 매도에서 PnL 왜곡 | 인출분만큼 원가 비례 차감 |
| 6 | `stats()`가 오라클 장애 시 revert | 스트레스 상황에서 감사 불가 | `tryEquity()` + `oracleHealthy`, 거래 경로는 fail-closed |
| 7 | 라이브 체결량 `amount_out = 0` 기록 | 성공한 거래가 100% 손실로 장부에 남음 | 영수증 `IntentExecuted` 로그 디코드, 0이면 기록 거부 |

### 2.2 차별화·정직성이 걸린 것 (7건)

| # | 결함 | 수정 |
|---|---|---|
| 8 | 주장(claim)에 상한이 없음 | `EdgeClaimAboveDislocation` — 주장 ≤ 관측 이탈 |
| 9 | 엣지 회계가 다리별(음수)과 라운드트립(양수)을 한 합에 넣음 | 질문 3개를 누적기 3개로 분리(선언/실현/다리별 실행) |
| 10 | 오프체인 이탈이 **100배** 부풀려짐 | 정확식으로 교체 (`1.0001^Δtick − 1`) |
| 11 | 왕복 비용이 임팩트를 절반만 계상(93bp) | `2×fee + 두 임팩트` = 126bp, 온체인 하한 146bp |
| 12 | 거부가 온체인에 **한 번도** 기록되지 않음 | `ChainExecutor.record_no_trade` + 코어 자동 호출 |
| 13 | 대화형 인터페이스가 읽기 전용 | `agent/control.py` — 규모·빈도·성향·자산 조정, 온체인 한도 밖은 거부 |
| 14 | `strategyHash`가 조정과 미연결 | 조정 시 재계산 + `registerStrategy` 필요 안내 |

### 2.3 완결성·문서 (7건)

미러/패리티 테스트 파일 부재 → 공유 정책 테이블(8 시나리오, Solidity·Python 동시 판독) + anvil EIP-712 바이트 대조 신규.
백테스트가 `policy.check()` 미사용(리포트는 "같은 코드 경로"라고 주장) → 실제로 통과시키고 거부도 기록.
`avg_price` 10^decimals 오차, `LIQUID_MARKETS` 주석 불일치, 없는 스크립트(`execute_intent.py`) 안내 → 신규 작성.
문서 수치(96bp→126bp, Slither 분포, 배포 가스 1.535→1.90 HSK, 테스트 수) 전면 정정.

---

## 3. 지금의 실측 숫자

| 항목 | 값 | 출처 |
|---|---|---|
| WHSK/USDT 깊이 | quote-side **$419,423** / 양쪽 $838,845 | 풀 `liquidity()`+`slotAt0` 2026-11-24 |
| 120 USDT 임팩트 | 진입 **33.0bp** / 청산 **32.7bp** | QuoterV2 |
| 왕복 비용 | **125.7bp** (2×30 + 33.0 + 32.7) | `scripts/backtest.py` |
| 엣지 허들 | **146bp** (정책 `min_edge_bps`, 온체인 하한) | `agent/config.py` |
| 실제 기록 바(35개) 백테스트 | **거래 0건 / 거부 15건** | `evidence/backtest.json` |
| 합성 평균회귀 | 3건, +3.69 USDT, 선언 359bp → 실현 379bp(신뢰도 105%), 다리당 실행 +269bp | SYNTHETIC 라벨 |
| 합성 하락추세 | **0건**(역추세 차단 작동) | SYNTHETIC 라벨 |
| 포크 실거래 | 고래 매도로 **108bp 이탈 생성** → 볼트가 매수: 100 USDT → **1,247.39 WHSK**, TWAP 공정가 1,238.11 대비 **+74bp**(수수료 30bp 지불 후) | `test_Fork_RealDislocationRealSwap` |
| 포크 청산 | 진입 100 USDT → 청산 99.40 USDT(풀에 되팔며 2차 임팩트) · 라운드트립 −59bp · 신뢰도 음수 | `test_Fork_ExitSettlesTheClaim` — **정직한 손실 기록** |
| 배포 | 18,675 bytes / 3,802,309 gas / **1.90 HSK** ≈ $0.15 (500 gwei) | `scripts/deploy.py` |
| Slither | 13건 (High 1 / Medium 1 / Low 9 / Info 2), 최대 복잡도 37→15 | `evidence/slither-report.*`, `docs/15_SLITHER_TRIAGE.md` |

> 포크 청산이 −59bp인 것은 실패가 아니라 실측이다: 120 USDT 규모를 같은 풀에 곧바로 되팔면 2차 임팩트와
> 수수료를 또 낸다. "들어올 때 이탈을 먹어도, 나올 때 비용을 다시 낸다"는 것이 이 시장의 구조이며, 그래서
> 허들이 146bp다. 데모에서는 이 두 숫자를 함께 보여주는 편이 더 설득력 있다.

---

## 4. 제가 대신 실행하지 않은 것 (의도적)

메인넷 배포, GitHub push, DoraHacks 등록, 데모 영상 공개 — 모두 회기님의 자금·계정·평판이 걸린 행위다.
`docs/40_SUBMISSION.md` §1에 결정 항목별로 비용·절차·화면 준비 상태를 정리했다.

---

## 5. 남은 작업 (마감 2026-12-03 13:00)

1. 회기님 결정 수신 → 지갑 3역할(소유자·전략·리스크) 지정/생성
2. HSK 1.90 확보 → `DEPLOYER_KEY=... python scripts/deploy.py --confirm` → Blockscout 소스 검증
3. 공개 저장소 push (로컬 커밋 완료 상태)
4. 데모 영상 90초 촬영(`docs/40_SUBMISSION.md` §3, 숫자 갱신 완료)
5. DoraHacks 제출 폼 채우기(로그인 후)
6. (선택) 대회 Telegram에 메인넷 배포 시점·가스 지원 문의 — 회기님 명의 발송이라 승인 필요

---

## 6. 정직하게 밝히는 한계

- **수익성은 실증되지 않았다.** 왕복 비용 126bp, 허들 146bp, 관측 최대 이탈 130bp → 실제 기록 바 백테스트 거래 0건.
  이는 구현 실패가 아니라 이 시장이 그만큼 비싸다는 측정 결과이며, README·리포트·데모 모두 첫 줄에 둔다.
- 단일 쿼트 자산(USDT)·단일 hop. WETH/WBTC는 유동성이 더 얇고 비싸다(측정값 공개).
- 오라클이 창을 서빙하지 못하면 거래 중단(fail-closed). 조회 뷰는 `oracleHealthy=false`로 살아 있다.
- 소유자 키 분실 = 자금 동결(업그레이드 없음 — 의도적).
- Slither High 1건은 구조적으로 수용(근거: `docs/15_SLITHER_TRIAGE.md` §3).
