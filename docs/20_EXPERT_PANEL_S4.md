# 전문가 회의체 4차 — 전면 재점검(line-by-line) 및 고도화

> **후속 정정(2026-11-25)**: 이 문서의 “왕복 126bp = 2×30 + 33 + 33”은 **수수료 이중계상**이었다.
> 견적에는 이미 수수료가 포함되며, 임팩트도 TWAP이 아니라 spot 기준이어야 한다. 정정 후 실측은
> **65.7bp**(33.0 + 32.7)이고 정책 하한 146bp는 비용 + 약 80bp 마진이다. 경위: `docs/12_deep_audit_and_upgrade.md` F3.
> 이 문서는 당시의 판단 기록이므로 본문 수치는 그대로 두고 이 배너만 추가한다.

대상: `src/SentinelVault.sol`(606→759줄), `src/libraries/TwapLib.sol`, Foundry 테스트 3종, Python 에이전트 11모듈,
`scripts/` 5종, `web/`, Slither 출력, `README.md`·`docs/*`·`evidence/*`.
방식: 1~3차와 동일하다. 5개 역할(P1 퀀트·MEV, P2 감사관, P3 A2A 설계자, P4 심사위원, P5 실자금 운영)의 판단 기준을
명시한 적대적 검증 라운드를 돌리고, 충돌은 증거(테스트 실패·체인 실측·감사 출력)로 판정했다.
**외부 실명 전문가를 소집한 것이 아니다** — 1차 문서에 밝힌 그대로다.

이번 라운드의 목적은 하나였다: **"심사위원이 3분 안에 반박할 수 있는 곳"을 전부 찾아 없애는 것.**
그래서 "동작하는가"가 아니라 "이 숫자가, 이 주장이, 이 규칙이 정말 그렇게 동작하는가"를 코드 한 줄까지 따라갔다.

---

## 요약 — 21건 발견, 21건 수정(수용 결정 0건, 문서·수치 정정 포함)

| # | 발견 | 분류 | 처리 |
|---|---|---|---|
| 1 | `Policy.minEdgeBps` 미검증 — 소유자가 0으로 두면 엣지 허들이 사라짐 | 정책(H) | 수정 |
| 2 | 매도에도 엣지 허들 적용 → 낙폭 구간에서 **청산이 거부** | 정책(H) | 수정 |
| 3 | 쿨다운이 매도에도 적용 → 리스크 축소가 지연 | 정책(M) | 수정 |
| 4 | `recordNoTrade`가 nonce를 소비하지 않음 → 거부 로그 재전송·중복 | 정책(M) | 수정 |
| 5 | `recordNoTrade`에 "어느 허들로 판정했는가"가 없음 | 증거(M) | 수정 |
| 6 | `withdraw`가 포지션 원가 장부를 남김 → 이후 매도 basis 오염 | 회계(M) | 수정 |
| 7 | `stats()`가 오라클 장애 시 revert → 스트레스 상황에서 감사 불가 | 가용성(M) | 수정 |
| 8 | 주장(claim)에 상한 없음 — 시장에 없는 이탈을 주장 가능 | 차별화(H) | 수정 |
| 9 | 엣지 회계 단위 불일치 — 다리별(음수) vs 라운드트립(양수)을 한 합에 | 회계(H) | 수정 |
| 10 | 오프체인 `twap_deviation_bps`가 100배 부풀려짐 | 에이전트(H) | 수정 |
| 11 | 라운드트립 비용 모델이 임팩트를 절반만 계상(거래를 부추김) | 에이전트(H) | 수정 |
| 12 | 라이브 체결량을 `amount_out = 0`으로 기록 → 성공한 거래가 100% 손실로 | 에이전트(H) | 수정 |
| 13 | 거부 결정이 온체인에 **한 번도** 기록되지 않음(문서·데모는 기록한다고 말함) | 완결성(H) | 수정 |
| 14 | 대화형 인터페이스가 읽기 전용 — 트랙 요구사항(규모·빈도·성향·자산 조정) 미충족 | 차별화(H) | 수정 |
| 15 | `strategyHash` 변경이 대화형 조정과 연결되지 않음 | 차별화(M) | 수정 |
| 16 | `avg_price`가 raw 수량으로 나눠 `10**decimals`배 틀림 | 표시(M) | 수정 |
| 17 | 백테스트가 `policy.check()`를 쓰지 않음(리포트는 "같은 코드 경로"라고 주장) | 정직성(M) | 수정 |
| 18 | 미러 테스트 파일(`test_policy_mirror.py`, `test_intent_parity.py`)이 **존재하지 않음** | 완결성(M) | 수정(신규) |
| 19 | `LIQUID_MARKETS` 주석·정의 불일치, 자산 선택 불가 | 완결성(M) | 수정 |
| 20 | 문서 수치 오류: 왕복 96bp(실제 126bp), `slither-report.txt`(21건) vs json(14건), 심각도 분포, 배포 가스 | 정직성(H) | 수정 |
| 21 | `scripts/run_paper.py`가 없는 파일(`execute_intent.py`)을 안내 | 완결성(L) | 수정(신규) |

---

## P2(감사관) — 정책 커널

### 발견 1. 엣지 허들은 이 프로젝트의 심장인데, 소유자가 한 트랜잭션으로 끌 수 있었다 (High)

```solidity
// 이전: minEdgeBps 검증 없음
if (p.maxSlippageBps == 0 || ... || p.maxSpotTwapDeviationBps == 0) revert InvalidConfig();
```
소유자는 `setPolicy`로 `minEdgeBps = 0`을 넣을 수 있었고, 그러면 `declareEdgeBps >= minEdgeBps`가 항상 참이 된다.
차별화의 전부인 "수수료보다 작은 엣지는 거부한다"가 조용히 꺼지는 것이다. 이벤트만 남고 읽는 사람은 알아채기 어렵다.

**수정**: 검증을 `_validatePolicy()` 한 곳으로 모으고(생성자·`setPolicy` 공통, 중복 제거), 허들은 `1..5,000bp`,
쿨다운은 `≤ 7일`로 상한을 뒀다.
**전/후**: `test_Revert_SetPolicyTurnsOffEdgeHurdle`(0 거부, 5,001 거부, 5,000 허용), `test_Revert_ConstructorRejectsZeroEdgeHurdle`,
`test_Revert_ConstructorRejectsAbsurdCooldown`.

### 발견 2. 손실 제한이 또 하나 있었다 — 이번엔 엣지 게이트가 청산을 막았다 (High)

3차 회의에서 "1회 한도가 청산을 막는 역설"을 고쳤는데, 같은 함정이 **엣지 게이트**에 그대로 남아 있었다.
매도 인텐트도 `declaredEdgeBps >= minEdgeBps`를 통과해야 했고, 평균회귀 청산의 기대 엣지는 구조적으로 작다
(주장 = 0.45×이탈). 따라서 낙폭 구간에서 "매도만 허용"이라 선언해 놓고, 정작 매도가 `EdgeBelowHurdle`로 막힐 수 있었다.
Python 미러도 같은 순서로 같은 거부를 냈기 때문에 **양쪽 모두에서** 청산이 막혔다. 테스트가 없어 드러나지 않았다.

**수정**: 게이트를 매수 전용으로. 청산은 `declaredEdgeBps = 0`으로 서명되고, 슬리피지 밴드·일일 한도·낙폭만 지킨다.
**전/후**: `test_ExitIsNotGatedByEdgeHurdle` 신규, `test_DrawdownBlocksBuysButAllowsExits`가 실제로 청산까지 수행.
미러 `policy.check()`도 `is_buy` 조건을 동일하게 적용. 공유 테이블(`test/fixtures/policy_scenarios.json`)의 8개 시나리오가 양쪽에서 같은 판정.

### 발견 3. 쿨다운이 리스크 축소를 지연시켰다 (Medium)

독립 주문(`parentId == 0`)이면 매도도 `cooldown`에 걸렸다. 손절이 60초 늦어지는 것은 작은 차이가 아니다.

**수정**: `lastTradeAt` → `lastBuyAt`, 쿨다운은 진입에만. `test_ExitIsNotBlockedByCooldown` 신규.

### 발견 4·5. 거부 로그가 재전송 가능했고, 무엇으로 판정했는지가 없었다 (Medium)

`recordNoTrade`는 `nonceUsed`를 보지도 쓰지도 않았다. 같은 거부를 여러 번 기록할 수 있고, 거부 로그와 거래 로그를
nonce로 연결할 방법이 없었다. 이벤트에는 `edgeBps`만 있어 "허들 146bp에 30bp였으니 거부"라는 사실을 검증할 수 없었다.

**수정**: 거부도 nonce를 소비한다(한 nonce = 한 결정). 이벤트에 `hurdleBps`를 추가하고, `hurdleBps >= policy.minEdgeBps`를
강제한다 — 볼트가 강제하는 것보다 느슨한 허들로 판정했다고 주장하는 거부는 기록되지 않는다.
**전/후**: `test_NoTradeConsumesNonce`(재기록 거부 + 같은 nonce로 거래도 불가), `test_NoTradeRejectsHurdleBelowFloor`.

### 발견 6. 인출이 장부를 남겼다 (Medium)

`withdraw`는 토큰만 보냈다. `positions[asset].qty/costQuote`는 그대로 남아, 이후 매도에서 `costPortion`을 부풀려
PnL을 왜곡하거나 `pos.qty`가 영원히 0이 되지 않았다.

**수정**: 인출분만큼 원가를 비례 차감. `test_WithdrawAssetKeepsPositionBookConsistent`.

### 발견 7. 심사위원이 읽는 단일 뷰가 스트레스에서 죽었다 (Medium)

`stats()`는 `equity()`를 거치고, `equity()`는 오라클이 창을 서빙하지 못하면 revert했다. 즉 **오라클 장애 = 감사 불가**.
거래 경로는 fail-closed가 맞지만, 조회 경로까지 죽을 이유는 없다.

**수정**: `tryEquity()`(비-revert, 평가 불가 자산은 원가 기준) + `Stats.oracleHealthy` 플래그. `equity()`는 그대로
fail-closed(`OracleUnavailable`). 루프는 자산당 외부 호출 1회(`holdingValue`)로 축약.
**전/후**: `test_StatsSurvivesOracleOutage` — 오라클을 죽인 뒤 `stats()`는 살아 있고 `oracleHealthy == false`, `equity()`는 revert.

## P4(심사위원) — "이 숫자, 지금 반박당하지 않나?"

### 발견 8. 주장에 상한이 없었다 (High)

지금까지 볼트는 "주장 엣지 ≥ 하한"만 봤다. 시장이 5bp밖에 안 움직였는데 500bp를 주장해도 통과했다. 그러면
`edgeCredibilityBps()`는 "허황된 주장을 얼마나 잘 지켰는가"를 재는 셈이 된다.

**수정**: `EdgeClaimAboveDislocation` — 주장은 **그 시점 관측된 spot–TWAP 이탈**을 넘을 수 없다(1 tick ≈ 1bp).
전략의 주장은 `min(0.45×이탈 + 0.35×추세, 이탈)`이라 구조적으로 통과하고, 시장에 없던 움직임을 주장하면 거부된다.
**전/후**: `test_Revert_WhenClaimExceedsObservedDislocation`(모의), `test_Fork_RevertsWhenClaimExceedsDislocation`(실제 풀).

### 발견 9. 신뢰도 지표가 사과와 오렌지를 나누고 있었다 (High)

`realizedEdgeBps`는 **다리별** 값으로, 테이커는 구조적으로 음수다(수수료를 낸다). 그런데 이를 **라운드트립 주장**(양수)과
합산해 `edgeCredibilityBps()`를 만들었다. 3차 문서의 "신뢰도 118%"는 그래서 나온 숫자였다 — 전략이 좋아서가 아니라
분모와 분자가 다른 질문이었기 때문이다. 심사위원이 이걸 캐면 방어선 전체가 흔들린다.

**수정**: 질문을 셋으로 분리하고 각각 다른 누적기를 쓴다.
- `declaredEdgeSumBps`: **진입**에서 주장한 엣지의 합
- `realizedEdgeSumBps`: **청산**에서 원가 대비 실제로 난 값(수수료 두 번 포함)의 합
- `executionEdgeSumBps` / `avgExecutionEdgeBps()`: **다리별** TWAP 대비 실행값(테이커는 음수가 정상)

이제 `edgeCredibilityBps()`는 "내가 주장한 만큼 실제로 벌었나"를 같은 단위로 답한다. 오프체인 `ledger.metrics()`도
같은 셋을 같은 정의로 낸다.
**전/후**: 합성 평균회귀 패널 105%(이전 118%는 정의 오류), 실제 기록 바 0건(주장 자체가 없음 → 0).
`test_ledger_pnl_matches_vault_semantics`가 세 값을 모두 검증.

## P1(퀀트) — 에이전트가 스스로를 속이던 세 곳

### 발견 10. 이탈 측정이 100배 부풀려져 있었다 (High)

```python
# 이전
return (self.tick - self.twap_tick) * 100.0  # "1 tick ≈ 1 bp"
```
1 tick은 1.0001 = **1bp**다. 100을 곱하면 1 tick이 100bp가 된다. 결과: 5 tick(5bp)만 움직여도 온체인 스팟 괴리
가드(500bp)에 걸려 거래가 거부되고, 신호 엔진은 존재하지 않는 이탈을 봤다. 컨트랙트는 처음부터 tick 수를 그대로
bp로 썼기 때문에 **온체인과 오프체인이 서로 다른 시장을 보고 있었다.** 실제 기록 바 백테스트는 `(price/twap-1)*10000`을
써서 정상이었기 때문에 더 오래 숨었다(백테스트는 맞고 라이브만 틀림).

**수정**: `(tick_to_price(Δtick) - 1) * 10_000` (정확식). `test_twap_deviation_is_in_basis_points`가 1 tick < 1.5bp를 고정.

### 발견 11. 왕복 비용을 절반만 세고 있었다 (High)

`2×fee + (impact_in + impact_out)/2` — 테이커는 임팩트를 **두 번** 낸다. 이 모델은 허들을 약 33bp 낮춰 잡아
"비용을 못 넘는 이탈"에도 진입하게 만든다. 정직성 원칙에 정면으로 어긋나는 방향의 버그다.

**수정**: `2×fee + impact_in + impact_out` (실측 126bp) + 20bp = 허들 146bp. 그리고 정책의 **온체인 하한**
`minEdgeBps`도 146으로 올려, "수수료보다 작은 엣지 주장은 컨트랙트가 거부한다"가 말이 아니라 코드가 되게 했다.
**전/후**: 실제 기록 바 0건(동일), 합성 평균회귀는 이탈을 4%대로 키워 기계를 검증(작은 이탈로는 아무것도 증명되지 않음 —
`docs/40_SUBMISSION.md` §3에 이유 명시).

## P3(A2A 설계자) — "주장은 체인에서 정산된다"는 말이 사실인가

### 발견 12. 라이브 체결량이 0으로 기록됐다 (High)

```python
amount_out = 0  # filled in from the receipt by the caller / next observation
```
아무도 채우지 않았다. 라이브 모드에서 성공한 매수가 **0 WHSK, 100% 손실**로 장부에 남고, 그 위의 PnL·낙폭·신뢰도가
전부 오염된다. 페이퍼 모드만 돌려 봤기 때문에 보이지 않았다.

**수정**: `ChainExecutor.execute()`가 영수증의 `IntentExecuted` 로그에서 `amountOut`을 디코드해 돌려주고, 0이면
`RuntimeError`로 **장부에 쓰지 않는다**(눈 먼 기록 금지). `decode_amount_out` 단위 테스트 + FakeChain으로 코어 배선 테스트.

### 발견 13. 거부는 온체인에 한 번도 기록되지 않았다 (High)

README·데모 스크립트·리포트가 모두 "거부도 결정으로 온체인에 남긴다"고 말했지만, `ChainExecutor`에 `recordNoTrade`
ABI만 있고 **호출하는 코드가 없었다.** 차별화 3번이 문서에만 존재했다.

**수정**: `ChainExecutor.record_no_trade()` + `core._record_refusal()` (HOLD 판정 시 자동, `SENTINEL_RECORD_NO_TRADE=0`으로 해제).
비활성/실패는 판단 기록에 남기고 루프를 죽이지 않는다. `test_live_refusal_is_written_on_chain`.

### 발견 14·15. 대화형 인터페이스가 읽기 전용이었다 (High)

트랙 요구사항은 "포지션 크기·거래 빈도·리스크 성향·대상 자산 조정"을 대화형으로 요구한다. 구현은 조회만 했다.
그리고 조정이 `strategyHash`와 연결되지 않아, "전략을 바꿨다"는 사실이 온체인 사전등록과 무관하게 흘러갈 수 있었다.

**수정**: `agent/control.py` — "1회 한도 100으로", "거래 빈도 줄여", "리스크 낮춰", "WETH로 바꿔"를 파싱해
**온체인 한도 안에서만** 적용한다. 한도를 넘는 요청은 조용히 깎지 않고 **거부하며 실제 온체인 숫자를 알려준다**
(예: "온체인 1회 한도 200 USDT — 소유자 키로만 변경 가능"). 전략 파라미터가 바뀌면 `strategyHash`를 재계산하고
`registerStrategy` 필요성을 응답에 명시한다. 조정은 `action=ADJUST`로 원장에 남는다.
**전/후**: `tests/test_controls.py` 9건 — 허용/거부/하한/자산 전환/재해시/원장 기록.

### 신규 — 오프체인 조정과 온체인 뷰의 연결 (`agent/onchain.py`)

`SENTINEL_VAULT`가 설정되면 `stats()`·`edgeCredibilityBps()`·`avgExecutionEdgeBps()`를 직접 읽어 대화 응답에 넣는다.
"온체인"이라고 물으면 볼트의 숫자를, 아니면 원장의 숫자를 답한다 — 두 숫자가 어긋나면 그 자체가 신호다.

## P5(운영) — 제출 완결성

### 발견 16. `avg_price`가 `10**decimals`배 틀렸다 (Medium)

`cost_quote / qty`(raw). 1,234 WHSK = 1.234e21 raw → 평균단가가 8.1e-20으로 표시됐다. `Position.avg_price`가
whole token 기준으로 나누도록 수정. `test_ledger_avg_price_is_per_whole_token`.

### 발견 17. 백테스트가 정책 엔진을 통과하지 않았다 (Medium)

리포트는 "패널의 숫자는 `signals → policy → ledger` 같은 경로"라고 썼지만, `scripts/backtest.py`는 `policy.check()`를
import조차 하지 않았다. 즉 백테스트에는 거부 사유가 없고 `no_trade_decisions`도 항상 0이었다.

**수정**: 실전 루프와 동일하게 `policy.check()`를 통과한 판단만 집행하고, 거부도 `HOLD` 결정으로 원장에 기록한다.
이제 실제 패널은 "거래 0건 / 거부 15건"으로, 거부가 **세어진 결정**이 된다.

### 발견 18. 문서가 가리키는 테스트 파일이 없었다 (Medium)

`agent/policy.py` docstring은 `tests/test_policy_mirror.py`를, `agent/intent.py`는 `tests/test_intent_parity.py`를
가리켰다. 둘 다 없었다. 즉 "미러가 계약과 어긋나지 않음을 테스트로 고정했다"는 주장이 비어 있었다.

**수정**:
- `test/fixtures/policy_scenarios.json` — **하나의 표**를 Solidity(`test_SharedPolicyTable`)와 Python이 함께 읽는다.
  8개 시나리오가 양쪽에서 같은 판정을 내야 통과. 한쪽에 술어를 추가하면 다른 쪽이 즉시 실패한다.
- `tests/test_intent_parity.py` — anvil 로컬 체인에 컨트랙트를 배포해 `hashIntent()`와 Python digest를 **바이트 비교**,
  `domainSeparator()`까지 대조. (없으면 skip — 조용히 통과하지 않는다.)

### 발견 19. 자산 목록이 사실과 달랐다 (Medium)

`LIQUID_MARKETS = ("WHSK",)`의 주석은 "USDC 풀이 비어 있어 제외"라고만 적혀 있었지만, 실제로는 WETH·WBTC 풀도 살아
있었다(재측정: quote-side $41.8k / $45.3k, 120 USDT 임팩트 58.7bp / 56.3bp). 정의와 주석이 어긋나고, 트랙이 요구하는
"대상 자산 조정"도 불가능했다.

**수정**: 3개 시장을 `MARKET_STATS`(측정값)와 함께 노출하고 대화로 전환 가능. 전환 시 **더 비싼 시장임을 숫자로 알린다.**

### 발견 20. 문서 수치가 코드와 어긋났다 (High)

| 항목 | 문서 | 실측 |
|---|---|---|
| 왕복 비용 | 96bp ("수수료 60 + 임팩트 33") | **126bp** (60+33+33) — 96은 산술 오류였고, 33을 절반만 계상한 모델에서 나온 값 |
| 유동성 | README $420k / 00_FACTS §8 $840k | **둘 다 맞지만 정의가 다름** — quote-side $419,423 / 양쪽 합 $838,845 |
| Slither | txt 21건, json 14건, S3 문서 "High1/Med4/Low6/Info3" | **13건, High1/Med1/Low9/Info2** (분포 자체가 틀렸음) |
| 컨트랙트 크기·가스 | 17,308B / 3,498,282 gas / 1.535 HSK | **18,675B / 3,802,309 gas / 1.90 HSK**(500 gwei) |
| 테스트 | 32 Foundry / 16 Python | **47 Foundry / 48 Python** |
| 3개 팀 근거 | `docs/00_FACTS.md §8` | §8은 Track 2 장벽 — 실제 근거는 `docs/20_EXPERT_PANEL.md` 1차 |

**수정**: 전 문서 재작성, `evidence/slither-report.txt`를 재실행 출력으로 교체, `docs/15_SLITHER_TRIAGE.md` 신규,
리포트는 `scripts/report.py`가 파일에서 숫자를 읽어 만들도록(손으로 적은 숫자를 없앰).

### 발견 21. 안내한 스크립트가 없었다 (Low)

`scripts/run_paper.py`가 "라이브는 `scripts/execute_intent.py`"라고 안내했지만 그 파일이 없었다.
**수정**: 신규 작성 — 기본 드라이런, `--confirm` 필수, 4개 환경변수 확인, 정책 미러 사전검증, 거부 시 `recordNoTrade`.

---

## 4차 회의 결론

1. **문서가 주장한 것 중 실제로 동작하지 않던 것 3건**(거부의 온체인 기록, 대화형 조정, 미러/패리티 테스트)을 구현으로 바꿨다.
   이 셋은 모두 "심사위원이 3분 만에 확인 가능한" 종류의 주장이었다 — 그게 가장 위험한 종류다.
2. **회계 2건**(엣지 회계 단위, 라이브 체결량 0)과 **정책 2건**(청산 차단, 허들 미검증)은 자금이 걸린 문제였다.
3. **수치 1건**(이탈 100배)은 라이브 경로만 틀려 있어 백테스트로는 절대 잡히지 않았다. 실제 체인 실측과
   컨트랙트 계산을 나란히 놓고 봐야 잡혔다.
4. 정직성 원칙은 유지·강화됐다: 실측 시장에서 **거래 0건**이라는 결론은 그대로이고, 그 근거가 더 비싸졌다
   (왕복 126bp, 허들 146bp, 그리고 이제 거부 15건이 세어진다).
5. 남은 최대 리스크는 여전히 성능이 아니라 **메인넷 배포·등록 주체**다(`docs/40_SUBMISSION.md` §1).
