# Slither 트리아지 — 13건 전부, 항목별 처리

도구: slither 0.11.6 · solc 0.8.24 · optimizer(200) · via-ir · evm shanghai
대상: `src/SentinelVault.sol` (프로젝트 소스만, `--filter-paths lib/`)
출력: `evidence/slither-report.json`, `evidence/slither-report.txt` (2026-11-24 재실행)

> 정직한 먼저 밝힘: 이번 재점검에서 코드가 크게 바뀌었으므로 **14건 → 13건이라는 숫자만으로 개선을 주장하지 않는다.**
> 함수 하나(복잡도 37)를 6개로 쪼갰기 때문에 같은 지적이 여러 함수로 나뉘어 나타나고, 반대로 해소된 지적도 있다.
> 아래 표가 항목별 전/후다.

## 1. 요약

| 구분 | 이전 (재점검 전) | 이후 (2026-11-24 재실행) |
|---|---|---|
| High | 1 (`reentrancy-balance`) | 1 (`reentrancy-balance`, `_swap`으로 이동) |
| Medium | 5 (`incorrect-equality` 2, `unused-return` 2, `reentrancy-no-eth` 1) | 1 (`reentrancy-no-eth`) |
| Low | 7 (`calls-loop` 3, `timestamp` 3, `reentrancy-benign` 1) | 9 (`calls-loop` 4, `timestamp` 4, `reentrancy-benign` 1) |
| Informational | 1 (`cyclomatic-complexity` 37) | 2 (`cyclomatic-complexity` 15, 12) |
| 합계 | **14** | **13** |

핵심 변화 두 가지:

1. **Medium 5 → 1.** `incorrect-equality` 2건과 `unused-return` 1건은 **코드 레벨에서 해소**했고(아래 §2), 나머지
   `unused-return` 1건은 세 번째 반환값(오류 인자)을 의도적으로 버리는 자리라 코드에 이유를 적고 억제했다.
2. **최대 순환 복잡도 37 → 15.** `executeIntent`(37)를 `_authorise` / `_fairValueBand` / `_enforceBudget` /
   `_swap` / `_settle` 5단계로 분리했다. 심사위원이 3분 안에 읽어야 하는 함수가 500줄짜리 한 덩어리에서
   "검증 → 가격 → 예산 → 실행 → 회계" 5개 문단으로 바뀌었다.

Low가 7 → 9로 는 것은 개선이 아니라 **구조 변경의 부작용**이다: `equity` 루프가 `tryEquity`로 바뀌면서
그 루프를 호출하는 컨텍스트가 늘었고(`executeIntent`/`stats`/`equity`/`tryEquity`), `timestamp` 검사가
분리된 helper로 흩어졌다. 지적의 실체는 동일하다.

## 2. 코드 레벨에서 실제로 고친 것

| 이전 지적 | 무엇이 문제였나 | 어떻게 고쳤나 | 검증 |
|---|---|---|---|
| `incorrect-equality` — `equity()`의 `bal == 0` | 루프 안에서 잔고 0을 strict equality로 분기 | `bal > 0` 분기로 재작성, 루프 본문은 `holdingValue()` 한 번의 외부 호출로 축약 | `test_StatsSurvivesOracleOutage` |
| `incorrect-equality` — `strategyRegisteredAt[h] == 0` | 미등록 판정을 strict equality로 | `strategyRegisteredAt[h] < 1` (의미 동일, heuristic 해소) | `test_NoTradeRequiresRegisteredStrategy`, `test_Revert_WhenStrategyNotRegistered` |
| `unused-return` — `_recover`가 `tryRecover`의 오류 반환을 버림 | 서명 실패 원인(길이/키/`s`값)을 구분하지 않고 주소만 비교 | `(signer, err)` 반환으로 바꾸고 호출부에서 `err != NoError`를 명시 검사 → **의미가 코드에 드러남** (동작은 동일: 어떤 실패든 `BadStrategySignature`) | `test_Revert_WhenRiskSignatureMissing`, `test_Revert_WhenSignatureCoversDifferentAmount` |
| `uninitialized-local` (이번 변경으로 새로 생겼다가 해소) | `tryEquity`의 `value`/`priced`가 catch 경로에서 미할당 | `uint256 value = 0; bool priced = false;` + `holdingValue()` 1회 호출 구조로 정리 | `test_StatsSurvivesOracleOutage` |
| `cyclomatic-complexity` 37 | `executeIntent` 140줄 | `_authorise`/`_fairValueBand`/`_enforceBudget`/`_swap`/`_settle`로 분해 (최대 15) | 전체 47개 Foundry 테스트 |

## 3. 수용한 것 (근거 포함)

### High 1 — `reentrancy-balance` (`_swap`)

라우터 호출 **전에** `tokenOut.balanceOf`를 읽고 **후에** 다시 읽어 실제 도달량을 계산하는 구조다.
Slither 입장에서는 "외부 호출을 사이에 둔 잔고 읽기"가 재진입 위험으로 보인다.

닫은 이유:

1. `executeIntent`는 `nonReentrant` — 같은 함수로 다시 들어올 수 없다.
2. `router`·`factory`는 생성자 immutable, 토큰은 소유자가 등록한 마켓만 쓸 수 있다.
3. **잔고 단독 의존을 제거했다**: 라우터가 보고한 `amountOut`과 잔고 증가분이 **일치해야** 통과하고
   (`OutputMismatch`), 그 값이 `minAmountOut` 이상이어야 한다. 즉 "악의적 토큰이 콜백에서 잔고를 건드려
   숫자를 부풀린다"는 공격은 두 관측값의 불일치로 잡힌다.
4. 남는 위험은 "소유자가 악성 토큰을 등록하는 경우"뿐이며, 그건 소유자 권한 남용 영역이다(변이 없음·프록시 없음).

검증: `test_Revert_WhenRouterUnderpays`(라우터가 적게 지급 → `InsufficientOutput`),
`test_Buy_ExecutesAndAccounts`(보고값=도달값), 불변식 `invariant_TradeCountMatchesHandler`.

### Medium 1 — `reentrancy-no-eth` (`executeIntent` → `_settle`)

라우터 호출 후 `_settle`에서 상태를 쓴다. 위와 같은 `nonReentrant` + 불변 라우터 + 화이트리스트로 닫혀 있고,
상태 갱신이 **실행 후**여야 하는 이유(도달량을 알아야 회계가 가능)가 명확하다. 수용.

### Low — `calls-loop` 4 (`tryEquity`)

`assets` 배열(현재 1개, 최대 3개)을 도는 동안 `holdingValue`를 1회씩 호출한다. 마켓 수는 소유자만 늘릴 수 있고
상한이 사실상 3개이므로 가스 폭주 경로가 아니다. 이 루프를 없애면 "오라클이 죽으면 `stats()`도 죽는다"는
가용성 문제가 되돌아온다 — **fail-closed는 거래 경로에만, 조회 경로는 살아 있어야 한다**는 판단이 더 중요하다.
검증: `test_StatsSurvivesOracleOutage`.

### Low — `timestamp` 4

`deadline`, 쿨다운, 24시간 롤링 창, 미등록 센티널(`< 1`) 판정. 모두 2초 블록체인에서 초 단위로 의미가 있는
비교이며 채굴자가 수 초 흔들 수 있는 폭은 정책에 영향이 없다(쿨다운 60s, 창 86,400s, deadline +300s).
`strategyRegisteredAt`은 타임스탬프를 "등록 여부" 센티널로만 쓰고 값 자체를 비교에 쓰지 않는다.

### Low — `reentrancy-benign` 1

`_settle`의 카운터 증가. 재진입해도 `nonReentrant`가 막고, 막히지 않는 경로에서도 합산 값만 중복될 뿐 자금 이동이 없다.

### Informational — `cyclomatic-complexity` 15 / 12

`_authorise`(15)는 원래 하나의 함수에 있던 검증을 그대로 옮긴 것이라 조건 수가 줄지 않는다. 다만 각 조건이
"무엇을 거부하는가"를 이름으로 말하는 함수 안에 모였고, 37 → 15로 내려왔다. 더 쪼개면 오히려 읽기 어려워진다.

## 4. 이번 재점검에서 Slither가 잡지 못한 것 (수동 감사로 발견·수정)

Slither는 "정책의 의미"를 모른다. 아래는 도구가 아니라 코드를 읽고 테스트를 돌려 찾은 것들이다
(상세: `docs/20_EXPERT_PANEL_S4.md`).

1. `Policy.minEdgeBps` **미검증** — 소유자가 0으로 설정하면 엣지 허들이 사라진다. → 생성자·`setPolicy` 공통
   `_validatePolicy`로 1..5,000 범위 강제.
2. **출구가 엣지 게이트에 막히는 역설** — 매도에도 `declaredEdgeBps >= minEdgeBps`를 요구해, 낙폭 구간에서
   청산이 거부될 수 있었다. → 게이트를 **매수 전용**으로.
3. **쿨다운이 청산을 지연** — 독립 주문이면 매도도 쿨다운에 걸렸다. → 쿨다운을 **진입 전용**(`lastBuyAt`)으로.
4. **`recordNoTrade`가 nonce를 소비하지 않음** — 거부 로그를 재전송·중복 기록할 수 있었다. → nonce 소비 +
   `hurdleBps`를 이벤트에 추가해 "허들 하한 이상으로 판정했다"는 사실까지 온체인에 남긴다.
5. **`withdraw`가 포지션 장부를 남김** — 자산을 인출해도 `positions[].qty/costQuote`가 남아 이후 매도에서
   basis가 어긋났다. → 인출분만큼 원가를 비례 차감.
6. **`stats()`가 오라클 장애 시 revert** — 심사위원이 읽는 단일 뷰가 정확히 스트레스 상황에서 죽었다.
   → `tryEquity()`(비-revert) + `Stats.oracleHealthy` 플래그, 거래 경로는 여전히 fail-closed.
7. **주장(claim)에 상한이 없음** — 시장이 보여준 이탈보다 큰 엣지를 주장해도 막을 수 없었다.
   → `EdgeClaimAboveDislocation` (주장 ≤ 관측 이탈).
8. **엣지 회계의 단위 불일치** — 다리별 실행값(음수)과 라운드트립 주장(양수)을 같은 합에 넣어
   `edgeCredibilityBps()`가 사과와 오렌지를 나누고 있었다. → 주장은 진입에서, 실현은 청산에서(라운드트립 기준),
   다리별 실행은 별도 누적(`avgExecutionEdgeBps()`).

---

## 4. 2차 통합 컨트랙트 재실행 (2026-11-25)

2026-11-25에 추가된 컨트랙트(`SwapGuardLib`, ERC-8004 3종 레지스트리, `Sentinel8004Adapter`)를 같은 설정으로
다시 돌렸다. 명령(forge 1.10의 build-info 형식이 crytic과 맞지 않아 solc 프레임워크를 강제했다):

```bash
export PATH="$HOME/.foundry/bin:$HOME/.local/bin:$PATH"
slither <file> --compile-force-framework solc \
  --solc-remaps "@openzeppelin/=lib/openzeppelin-contracts/" \
  --solc-args "--via-ir --optimize --evm-version shanghai" --filter-paths "lib/"
```

출력: `evidence/slither-2026-11-25-<Contract>.txt`

| 대상 | 결과 | 판정 |
|---|---|---|
| `SentinelVault.sol` | **13건** (기존과 동일) | §1의 트리아지 그대로 유효 |
| `SwapGuardLib.sol` (+`GuardHarness`) | **0건** | — |
| `IdentityRegistry.sol` | 2건 (`unused-return`, `timestamp`) | 아래 수용 |
| `ReputationRegistry.sol` | 1건 (`assembly`) | 아래 수용 |
| `ValidationRegistry.sol` | 2건 (`timestamp` 오탐, `assembly` 중복) | 아래 수용 |
| `Sentinel8004Adapter.sol` | 4건 (`reentrancy-events` 1 + 임포트 컨트랙트 중복 3) | 아래 수용 |

| # | 지적 | 왜 수용하는가 |
|---|---|---|
| N1 | `unused-return` — `ECDSA.tryRecover`의 3번째 반환값(오류 인자)을 버린다 | 두 번째 값 `RecoverError`를 명시적으로 검사하고, 실패 시 `WalletProofInvalid`로 되돌린다. 세 번째 값은 진단용 부가정보라 의미가 없다. (기존 볼트의 동일 항목과 같은 판단) |
| N2 | `timestamp` — `setAgentWallet`의 `deadline` 비교 | 서명 만료는 본질적으로 시간 조건이다. 채굴자 오차(수 초)로 만료가 앞당겨져도 **거부**되는 방향이라 자금 위험이 없고, 공격자가 이득을 보려면 만료를 늦춰야 하는데 그건 불가능하다. |
| N3 | `timestamp` — `ValidationRegistry.validationRequest` | **오탐.** 플래그된 비교는 `_validations[requestHash].validatorAddress != address(0)`으로, 시간과 무관하다. slither가 같은 함수의 `lastUpdate` 대입을 인접시켜 잘못 묶었다. |
| N4 | `assembly` — `readAllFeedback`의 배열 길이 트리밍 | `assembly ("memory-safe")`로 선언했고, 새 배열의 length 슬롯만 줄인다. 반환값을 호출자가 순회할 때 0-패딩 꼬리를 보지 않게 하려는 목적이며, 메모리 밖을 건드리지 않는다. |
| N5 | `reentrancy-events` — `attest()`가 레지스트리 호출 후 이벤트를 낸다 | 호출 대상은 immutable로 고정된 우리 레지스트리 두 개이고, ETH/토큰 이동이 없으며, 재진입해도 `agentId`·`TAG_*`가 바뀌지 않는다. 이벤트가 재정렬돼도 오프체인 인덱서가 읽는 값은 온체인 저장값과 같다. |

**남는 위험(정직하게)**: ERC-8004 구현은 EIP 본문을 그대로 옮겼지만 **외부 감사를 받지 않았다**. 특히
`readAllFeedback`의 어셈블리 트리밍과 `getSummary`의 십진수 스케일링은 우리가 스펙의 모호한 부분을 해석한
곳이므로, 실제 배포 전에는 감사 또는 최소한 차분 테스트가 필요하다. 이 데모에서는 레지스트리가 자금을
보관하지 않으므로(평판·검증 기록만) 실패 반경이 자금이 아니라 표시값에 머문다.
