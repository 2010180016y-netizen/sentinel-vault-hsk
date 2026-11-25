# 4개 오픈소스/표준 통합 기록 — 설치·코드·테스트까지

- 작성: 2026-11-25 (KST)
- 대상: HSK Chain Horizon Hackathon · Seoul / Track 1 (AI Agent Trading)
- 원칙: 이 문서에 적힌 것은 **저장소에 실제로 존재하는 코드와, 실제로 돌린 테스트**뿐이다. "참고했다"는 기록하지 않는다.
- 선행 문서: `docs/10_OSS_RESEARCH.md` (1차 리서치), `docs/30_ARCHITECTURE.md` (설계)

---

## 0. 한눈에

| # | 오픈소스/표준 | 커밋 | 우리 코드에 들어온 형태 | 검증 |
|---|---|---|---|---|
| 1 | `elizaOS/eliza` | `4ad4af8` (2026-11-24) | `agent/runtime/` (Action/Provider/Evaluator/Plugin/AgentRuntime) + `agent/plugins/sentinel.py` (액션 9개·프로바이더 6개·평가자 1개) | `tests/test_runtime.py` 16개 |
| 2 | `Uniswap/uniswap-ai` | `989dbaf` (2026-11-04) | `agent/guards.py` + `src/libraries/SwapGuardLib.sol` — 같은 술어 13개를 오프체인·EVM 양쪽에 | `tests/test_guards.py` 27개, `test/SwapGuard.t.sol` 4개 (공유 표 19 시나리오) |
| 3 | `coinbase/x402` | `0e6e8c5` (2026-10-15) | `agent/x402/` (타입·EIP-3009 서명/검증·facilitator·클라이언트·HTTP 헤더) + `agent/premium.py` 유료 엔드포인트 | `tests/test_x402.py` 21개, `tests/test_premium.py` 5개 (실소켓 왕복 포함) |
| 4 | `ERC-8004` (EIP) | EIP 본문 (2026-11-25 조회) | `src/registries/{Identity,Reputation,Validation}Registry.sol` + `src/Sentinel8004Adapter.sol` + `agent/registry8004.py` | `test/ERC8004.t.sol` 17개, `tests/test_erc8004.py` 7개, 실볼트 연동 1개 |

합계: Foundry **69** (기존 47 + 신규 22), Python **125** (기존 48 + 신규 76). 2026-11-25 실행 결과 전부 통과.

```
forge test   → 69 passed, 0 failed  (5 suites)
pytest tests → 125 passed
```

---

## 1. elizaOS/eliza — 에이전트 런타임·플러그인 패턴

### 무엇을 읽었나
`packages/core/src/types/{components.vibe,plugin.vibe,evaluator.vibe,runtime.vibe}`,
`packages/core/src/actions/*.vibe` (액션 파라미터 검증·툴 스키마 변환).
저장소는 `--filter=blob:none --sparse`로 `packages/core`만 받아 실제 소스를 열었다.

### 무엇을 만들었나 — `agent/runtime/`
eliza의 구성요소를 그대로 옮겼다. 이름과 계약을 맞춰 둔 이유는, 심사자가 eliza 문서를 옆에 두고
우리 코드를 읽어도 대응이 되게 하기 위해서다.

| eliza | 우리 구현 | 파일 |
|---|---|---|
| `Action{name, description, handler, validate, similes, parameters}` | `Action` — `validate`가 먼저, `params_ok`가 스키마 검사 후 핸들러 | `agent/runtime/types.py` |
| `Provider.get(runtime, message, state)` | `ProviderSpec` 6개가 `State`를 조립 | `agent/runtime/runtime.py::compose` |
| `Evaluator` (실행 후) | `Evaluator` — 실행 결과를 메모리/온체인 평판 근거로 기록 | `agent/plugins/sentinel.py::execution_quality` |
| `Plugin{actions, providers, evaluators}` | `Plugin` — 이름 중복 등록 거부 | `agent/runtime/runtime.py::register_plugin` |
| 메모리 스토어 | `AgentRuntime.memory` (상한 200, `recent()`) | 동일 |

### 에이전트에 실제로 연결된 것
`agent/core.py`의 `SentinelAgent.runtime` 프로퍼티와 `chat()`이 런타임을 쓴다. 즉 채팅 문장은
이제 **프로바이더가 만든 상태 → 액션 선택 → 파라미터 검증 → 핸들러 → 평가자** 순으로 흐른다.
`agent/server.py`의 `/api/ask`도 `agent.chat()`을 호출한다.

액션 9개: `READ_MARKET`, `REPORT_PERFORMANCE`, `CHECK_RISK`, `EXPLAIN_DECISION`, `TRADE_ONCE`,
`ADJUST_AGENT`, `SET_KILL_SWITCH`, `BUY_PREMIUM_DATA`, `REPORT_REPUTATION`.

> **의도적으로 바꾼 점**: eliza는 LLM이 액션을 고른다. 우리는 **결정적 플래너**를 쓴다(시밀이 일치
> 개수 → priority → 등록순). 매매 여부가 재현되지 않으면 "주장은 검증 가능해야 한다"는 이 프로젝트의
> 전제가 무너지기 때문이다. LLM은 서술만 한다. 이 경계는 `tests/test_runtime.py`가 지킨다.

---

## 2. Uniswap/uniswap-ai — 스왑 안전 실행·슬리피지/유동성 가드

### 무엇을 읽었나
`packages/plugins/uniswap-driver/skills/swap-planner/SKILL.md` (풀 TVL < $100k 경고, 임팩트 표시),
`packages/plugins/uniswap-trading-tools/skills/{dca-bot,copy-trade}/SKILL.md` (per-run/per-period
상한, 토큰 화이트리스트, 드라이런, 킬스위치, autonomous 모드 조건), `docs/skills/swap-integration.md`
(기본 슬리피지 0.5%, 상한 5%), `references/execution-model.md` (confirm/autonomous 구분).

### 무엇을 만들었나 — 술어 13개, 두 언어로
`agent/guards.py`(오프체인)와 `src/libraries/SwapGuardLib.sol`(EVM)이 **같은 순서로 같은 술어**를
평가한다. 두 구현이 갈라지면 `test/fixtures/guard_scenarios.json`(19 시나리오)을 읽는 두 테스트가
동시에 깨진다 — 기존 `policy_scenarios.json`과 같은 방식이다.

| 순서 | 코드 | 근거 스킬 |
|---|---|---|
| 1 | `ADDRESS_INVALID` / `AMOUNT_INVALID` | 모든 스킬의 Input validation 절 (주소 정규식·금액 정규식·메타문자 거부) |
| 2 | `KILL_SWITCH` | dca-bot·copy-trade의 kill switch |
| 3 | `TOKEN_NOT_ALLOWED` | dca-bot token allowlist |
| 4 | `SELL_SIDE_MISSING` | swap-planner "no sell liquidity → honeypot" |
| 5 | `DEPTH_FLOOR` | swap-planner "Pool TVL < $100k = high slippage" |
| 6 | `DEPTH_SHARE` | swap-planner "large trades: split / warn" |
| 7 | `PRICE_IMPACT` | swap-planner impact 표시 |
| 8 | `SLIPPAGE_TOLERANCE` | swap-integration 0.5% 기본 / 5% 상한 |
| 9-10 | `PER_RUN_CAP` / `PER_PERIOD_CAP` | dca-bot·copy-trade spend caps |
| 11 | `QUOTE_STALE` | 실행 모델의 "quote는 실행 직전 것" |
| 12 | `AUTONOMY_UNARMED` | autonomous 모드는 상한 2종 + 화이트리스트 + 드라이런이 모두 있어야 |

### 어디에 걸려 있나
`SentinelAgent.step()`에서 **정책 미러 통과 직후, 인텐트 생성 전**에 `preflight()`가 돈다.
거부되면 `record["guards"]["code"]`와 함께 결정이 원장에 남고, 라이브 모드에서는
`recordNoTrade(..., reason=GUARD_XXX)`로 온체인에도 남는다. 즉 "가드가 막았다"도 증거다.

> **의도적으로 바꾼 점**: Uniswap 스킬은 사람이 읽는 경고문이지만, 우리는 **거부 코드**로 만든다.
> 경고는 무시되지만 revert는 무시되지 않는다. EVM 구현은 볼트를 건드리지 않고 라이브러리로 두었다 —
> 볼트는 감사된 불변 커널이고, 가드는 그보다 **엄격한** 앞단이기 때문이다.

### 실측으로 확인한 것
WHSK/USDT 풀(깊이 419,423 USDT)에서 120 USDT 주문은 깊이의 **2bp**(=0.029%)다. 가드 상한은 2,000bp이므로
정상 크기에서는 6번이 걸리지 않고, 12만 USDT 같은 주문에서만 걸린다. `test_SharedGuardTable`의
`order_is_30pct_of_depth` 시나리오가 그 경계를 고정한다.

---

## 3. coinbase/x402 — HTTP 402 결제 게이트웨이·검증 계층

### 무엇을 읽었나
`specs/x402-specification-v2.md`, `specs/schemes/exact/scheme_exact_evm.md` (EIP-3009
`TransferWithAuthorization`, verify→settle, nonce/deadline), `specs/transports-v2/http.md`
(`PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` 헤더).

### 무엇을 만들었나 — `agent/x402/`
* `types.py` — v2 와이어 타입(`PaymentRequired`, `PaymentRequirements`, `PaymentPayload`, `Settlement`).
* `eip3009.py` — 토큰 도메인(EIP-712)으로 `transferWithAuthorization` **다이제스트를 직접 계산**하고
  서명·복구한다. 파리티 테스트가 `eth_account.messages.encode_typed_data`(독립 구현)와 바이트 단위로 비교한다.
* `verify.py` — 스펙의 검증 순서: 버전 → 스킴 → 전송수단 → 네트워크 → 자산 → payTo → 금액(exact) →
  서명 복구 → 유효창 → **nonce 1회용** → (선택) 잔고. 실패 사유를 이름으로 돌려준다.
* `facilitator.py` — `PaperFacilitator`(검증은 실제, 정산은 영수증)와 `ChainFacilitator`(실브로드캐스트 어댑터).
* `client.py` — 402를 받으면 챌린지에서 지불 수단을 고르고, **예산 안에서만** 서명해 재요청한다.
* `service.py` — 헤더 인코딩/디코딩.
* `agent/premium.py` + `agent/server.py` — `/api/premium/edge` 유료 엔드포인트(0.001 USDT).
  지불자에게는 hurdle·실현/선언 엣지·가드 상태·낙폭이 돌아간다.

### 안전 경계 (코드로 강제)
* **메인넷 정산 금지**: `ChainFacilitator.settle()`은 chain 177에서 `allow_mainnet=True` **그리고**
  `SENTINEL_ALLOW_MAINNET_SPEND=1`이 없으면 `MainnetSpendForbidden`을 던진다. 브리프의 "메인넷 가스 지출 금지"를
  주석이 아니라 예외로 만든 것이다. `tests/test_x402.py::test_chain_settlement_refuses_hsk_mainnet`이 고정한다.
* **영수증 위조 금지**: paper 정산의 `transaction`은 `paper:` 접두사를 붙여 온체인 해시로 오인되지 않게 했다.
* **검증 실패는 nonce를 소모하지 않는다**: verify와 settle을 분리했고, 실패한 결제 뒤 같은 nonce의 올바른 결제가
  통과하는 것을 테스트로 고정했다.
* **지불처를 지어내지 않는다**: `SENTINEL_X402_PAY_TO`(또는 OWNER/VAULT)가 없으면 엔드포인트는 503과 안내를 낸다.

### 정직한 한계
HSK의 USDT는 EIP-3009를 구현하지 않는다. 그래서 이 프로젝트에서 x402는 **검증 계층까지 실제**이고
정산은 paper다. 실배포에서는 Permit2 전송수단이나 EIP-3009 지원 토큰이 필요하며, 그 선택은
`allowed_transfer_methods`로 열어 둘 수 있게 했다(현재 허용: `eip3009`만).

---

## 4. ERC-8004 — Trustless Agents (신원·평판·검증)

### 무엇을 읽었나
`EIPS/eip-8004.md` 전문(조회 2026-11-25): Identity(ERC-721 + URIStorage + 온체인 메타데이터,
예약 키 `agentWallet`, 전송 시 지갑 초기화), Reputation(`int128`+`valueDecimals`, 소유자 자기평가 금지,
`getSummary`는 클라이언트 목록 필수), Validation(`requestHash` 약속, 0-100 응답, `tag`로 단계적 확정).

### 무엇을 만들었나
* `src/registries/IdentityRegistry.sol` — `register` 3종, `setAgentURI`, `get/setMetadata`,
  `agentWallet` 예약키 차단, `setAgentWallet`(EIP-712 증명), 전송 시 지갑 자동 초기화, `agentRegistryId()`.
* `src/registries/ReputationRegistry.sol` — `giveFeedback`, `revokeFeedback`, `appendResponse`,
  `readFeedback`, `readAllFeedback`, `getSummary`, `getClients`, `getLastIndex`, `getResponseCount`.
* `src/registries/ValidationRegistry.sol` — `validationRequest`, `validationResponse`,
  `getValidationStatus`, `getAgentValidation(s)`, `getValidatorRequests`, `getSummary`.
* `src/Sentinel8004Adapter.sol` — **연결부**. `SentinelVault.stats()`·`edgeCredibilityBps()`를 읽어
  평판(`edgeCredibility`, `executionCost`)을 올리고, 검증 요청에는 "거래 수 ≥ N, 오라클 정상, 신뢰도 ≥ 임계"를
  만족할 때만 100을 준다. 검증자가 에이전트의 말을 믿지 않고 **온체인 기록**을 읽는다는 점이 핵심이다.
* `agent/registry8004.py` — 등록 파일(스펙 형태 그대로) + `data:` URI + 읽기 전용 레지스트리 클라이언트
  (`getSummary` ABI 인코딩/디코딩 포함).

### 실볼트 연동 증거
`test/SentinelVault.t.sol::test_ERC8004AttestationUsesTheRealVaultRecord`:
실제 볼트에 매수→매도(선언 100bp, 실현 > 0)를 실행한 뒤
`adapter.attest(vault)`가 돌려준 신뢰도가 `vault.edgeCredibilityBps()`와 **정확히 같고** 0보다 큼을 단언한다.
이어서 `validationRequest` → `adapter.validate(...)`가 100을 주는 것까지 한 테스트에서 확인한다.

### 의도적으로 바꾼 점
* 평판 요약에서 **십진수를 필터에 맞춰** 계산한다. 스펙 문구가 모호한 부분이라, "starred 87"이
  무관한 "uptime 99.77" 때문에 8700으로 재스케일되는 것을 막았다(테스트로 고정).
* `getResponseCount(responders[])`는 응답자별 카운트를 저장하지 않으므로 전체 카운트를 돌려준다.
  스펙은 이벤트만 요구한다. 저장하지 않은 값을 지어내지 않는다.

---

## 5. 테스트·검증 명령 (그대로 재현)

```bash
cd competitions/hsk-chain-seoul-2026
export PATH="$HOME/.foundry/bin:$PATH"

# Solidity: 69 (정책 40 + 불변식 2 + 포크 5 + 가드 4 + ERC-8004 17 + 실볼트 연동 1)
forge test

# Python: 125
python3 -m pytest tests -q

# 가드 파리티만
forge test --match-contract SwapGuardTest
python3 -m pytest tests/test_guards.py -q

# ERC-8004만
forge test --path test/ERC8004.t.sol
python3 -m pytest tests/test_erc8004.py -q
```

## 6. 설치 재현 (심사자용)

```bash
cd competitions/hsk-chain-seoul-2026/oss
git clone --depth 1 https://github.com/Uniswap/uniswap-ai uniswap_ai           # 989dbaf
git clone --depth 1 https://github.com/coinbase/x402 coinbase_x402              # 0e6e8c5
git clone --depth 1 --filter=blob:none --sparse https://github.com/elizaOS/eliza eliza
cd eliza && git sparse-checkout set packages/core && cd ..                      # 4ad4af8
```

`oss/`는 `.gitignore`에 포함돼 저장소에 올라가지 않는다(약 300MB). 위 명령으로 언제든 같은 소스를 얻는다.
