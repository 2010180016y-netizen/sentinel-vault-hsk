# 12. 7단계 전면 감사 및 수상급 업그레이드 — 실행 기록

작성 2026-11-25 · 대상 `competitions/hsk-chain-seoul-2026/` · 공개 저장소 `2010180016y-netizen/sentinel-vault-hsk`
기준선(작업 전): Foundry 69/69 · Python 125/125 · Slither 13건 · **실제 체인에서 돈이 움직이는 경로는 한 번도 끝까지 돌지 않음**
결과(작업 후): Foundry **74/74** · Python **173/173** · Slither **13건 유지(신규 0)** · **포크 메인넷에서 49개 트랜잭션으로 전 구간 실측 통과**

> 이 문서는 “무엇을 고쳤다”가 아니라 **무엇이 왜 틀렸고, 어떤 명령으로 확인했는지**를 남긴다.
> 모든 수치는 이 저장소에서 다시 실행해 재현할 수 있다(§7 명령).

---

## 0. 한 줄 결론

기준선은 “읽으면 좋은 코드”였고, 실제로는 **한 번도 체인에서 돌지 않았다**. 7단계 감사에서
라이브 실행 경로를 막는 결함 3건(서명 절단·실행기 선택 누락·비용 모델 이중계상)과, 심사위원이 만질 수
있는 표면(데모·추적·결제·평판)의 공백을 찾아 전부 고쳤다. 지금은 같은 코드가 **HSK 메인넷 포크에서
배포→이탈 생성→거부 기록→진입→청산→ERC-8004 평판→x402 실정산**까지 49개 트랜잭션으로 통과하고,
에이전트 장부와 볼트 `stats()`가 줄 단위로 일치한다.

| 지표 | 작업 전 | 작업 후 | 근거 |
|---|---|---|---|
| Foundry | 69/69 | **74/74** | §7.1 |
| Python | 125/125 | **173/173** | §7.2 |
| 체인 end-to-end | 없음(유닛 목만) | **포크 49 tx, 26개 체크 통과** | `evidence/e2e_fork.json` |
| x402 정산 | paper만 | **실제 `transferWithAuthorization` tx** | `evidence/e2e_fork.json` → `x402` |
| 왕복 비용(120 USDT) | 126bp로 표기(이중계상) | **65.7bp 실측**(수수료는 견적에 포함) | `evidence/backtest.json` |
| 인터랙티브 데모 | 정적 카드 6개 | **게이트 추적·시나리오 랩·x402 결제·온체인 대조** | `web/index.html`, `tests/test_api.py` |
| LLM | 없음 | **서술·제안 계층(숫자 검증, 제안은 정책이 거부)** | `agent/narrator.py` |
| Slither | 13 | **13 (High 1/Medium 1/Low 9/Info 2) + 목 토큰 3(테스트 전용)** | `evidence/slither-2026-11-25-*` |

---

## 1단계 — 현황 파악

### 1.1 실제 가동 구조 (읽고 확인한 것)

```
HSK mainnet RPC(177) ─ rpc.py ─ market.py ─ signals.py ─ policy.py ─ core.py ─ ledger.py
                                                │             │
                                                │             ├─ guards.py (Uniswap 스킬 13종)
                                                │             ├─ runtime/ + plugins/sentinel.py (elizaOS형 6 provider/9 action/1 evaluator)
                                                │             └─ executor.py (paper | ChainExecutor: EIP-712 이중서명)
                                                ▼
                                     SentinelVault.sol (불변 정책 커널)
                                                ▼
                                     HSKSwap Router02 → WHSK/USDT 풀
                                     (+ ERC-8004 Identity/Reputation/Validation + Adapter)
                                     (+ x402 v2: 402 → EIP-3009 검증 → 정산)
```

| 영역 | 파일 | 줄 | 상태(작업 전) |
|---|---|---|---|
| 컨트랙트 | `src/SentinelVault.sol` | 760 | 정책 커널, 41 테스트 + 5 불변식 + 5 포크 |
| 라이브러리 | `TwapLib`, `SwapGuardLib` | 250 | 파리티 표 19 시나리오 공유 |
| ERC-8004 | `src/registries/*` + `Sentinel8004Adapter` | 769 | 스펙 구현, 어댑터는 볼트 기록만 읽음 |
| 에이전트 | `agent/*.py` (18 모듈) | 3,000+ | 결정론적 신호·정책 미러·가드·런타임·x402 |
| 데모 | `agent/server.py` + `web/index.html` | 154 + 212 | 정적 카드 + 채팅 |
| 스크립트 | `deploy/backtest/run_paper/report/gen_keys/execute_intent` | 1,000 | 배포는 dry-run 기본 |

### 1.2 기준선 실측 (작업 전 그대로)

```
$ forge test --no-match-path 'test/SentinelVault.fork.t.sol'   → 64 passed
$ forge test --match-path 'test/SentinelVault.fork.t.sol'      → 5 passed
$ python3 -m pytest tests/ -q                                  → 125 passed
$ python3 scripts/backtest.py                                  → "not enough recorded bars to backtest"
```

마지막 줄이 첫 번째 단서였다. README는 “35개 기록 바 백테스트, 거래 0건”을 주장하는데, **다음 날
실행하면 백테스트 자체가 죽는다**. 원인은 하드코딩된 40,000블록 창과, 그보다 더 조용한 캐시 결함이었다(→ F6).

---

## 2단계 — 적대적 감사 (심사위원·보안 감사자 관점)

발견 26건. **자금·정합성 11건 / 실제 작동성·데모 8건 / 증거·문서 5건 / 잔여 위험 2건**.
“통과하는 테스트”가 아니라 “심사위원이 눌렀을 때 무엇이 일어나는가”를 기준으로 찾았다.

### 2.1 자금·정합성 (F)

| # | 결함 | 근거(어떻게 찾았나) | 왜 치명적인가 | 조치 | 검증 |
|---|---|---|---|---|---|
| **F1** | **라이브 서명이 2글자 잘려 있었다.** `HexBytes.hex()`가 `0x`를 반환하지 않는 버전에서 `sig[2:]`가 서명 첫 바이트를 잘라 64바이트 blob을 만들었다 | 포크 end-to-end에서 `BadStrategySignature(0xb9d0e8c5)`로 첫 실행 실패 → 셀렉터 역산 | 라이브 모드가 **단 한 번도 성공할 수 없었다.** 유닛 테스트는 `ChainExecutor`를 fake로 바꿔 이 경로를 건너뛰었다 | `intent._hex_signature()`로 정규화, 소비 측도 `removeprefix("0x")` | 포크 49 tx 중 첫 intent 성공, `tests/test_agent.py` 유지 |
| **F2** | `SENTINEL_LIVE=1`이 실제 실행기를 바꾸지 않았다 — `SentinelAgent`가 항상 `PaperExecutor`를 만들었다 | 코드 읽기(`core.__init__`) | 운영자가 라이브라고 믿는 동안 페이퍼로 돌아 **모든 실측이 거짓**이 된다 | `settings.live`면 `ChainExecutor` 생성(키·볼트 없으면 fail-closed) | 포크에서 실제 tx 발생 |
| **F3** | **비용 모델이 수수료를 두 번 세고, 이탈(dislocation)을 비용으로 계산했다.** `2×fee + impact_in + impact_out`인데 견적에는 이미 수수료가 포함; impact는 TWAP 대비라 -381bp 이탈이 “465bp 비용”으로 보였다 | 포크에서 전략이 -381bp 이탈에도 HOLD, `reasons`가 “cost 461bp” | 봇이 **자기가 잡으려는 이탈을 비용으로 지불**해 모든 거래를 거부. 문서의 “왕복 126bp”도 이중계상 | impact를 **현재 spot 대비**로 측정(`market.spot_impact_bps`), 왕복 = `impact_in + impact_out` = **65.7bp** | `tests/test_agent.py::test_round_trip_cost_counts_both_legs`, 백테스트 65.7bp |
| **F4** | 가드의 `PRICE_IMPACT`가 TWAP 기준이라, 덤프 중 **청산**이 “10000bp 임팩트”로 차단됐다 | `test_live_path`의 SELL이 `PRICE_IMPACT`로 거부 | 위험을 줄이는 거래를 비용 가드가 막는, 볼트에서 이미 고쳤던 것과 같은 함정 | 가드 요청의 benchmark를 spot으로 | `test_live_path::test_live_exit_settles_the_round_trip`, 포크 청산 |
| **F5** | 가드가 **주문의 견적이 아니라 관측 시점의 cap 견적**을 심사했다 | SELL에서 `quoted_out`(cap) vs `fair_out`(실주문) 불일치 | 실제로 낼 주문과 다른 주문을 검사 → 통과/거부가 둘 다 틀릴 수 있다 | 주문 규모로 재견적한 뒤 그 값으로 가드·정책·minOut 계산 | 포크 SELL 통과, 테스트 173 |
| **F6** | 백테스트 캐시 키에 **lookback이 없었다.** 40k 결과가 320k 요청에 최대 2분간 서빙되어 “11 bars” | 320k 창이 11 bars를 반환하는 것을 실측 | “전수 조사”를 표방한 백테스트가 실제로는 40k만 본다 | 키에 lookback 포함 + 창을 40k→320k로 확장하며 25바 미만이면 실패 대신 확장 | 백테스트 40 bars/80k window |
| **F7** | 승률 = 승 / **전체 거래**(미청산 진입 포함) | 코드 읽기 | 열린 포지션을 손실로 세는 숫자 | 승 / 청산 수, `exits` 추가 | 백테스트 MR `hit_rate 1.0` (3 trades/1 exit) |
| **F8** | 청산 규모를 `fair_out`에서 역산하고, 포지션이 볼트의 청산 상한(800 USDT)을 넘어도 한 번에 팔려 했다. 같은 **단위 오류**가 `observe()`에도 있어 `max_trade_notional`(raw USDT)을 포지션(raw base)과 직접 비교 → 보유 시 청산 견적이 사실상 0으로 잡혀 다리당 비용이 0에 가깝게 보고됐다 | 코드 읽기 + `test_depth` 작성 중 발견 | 견적·정책 판정이 어긋나고, 큰 포지션은 `TradeTooLarge`로 청산 불가. 보유 중 허들이 과소평가 | 포지션 가치(가격×수량)로 상한·notional 계산, `observe`는 cap을 **base 단위로 환산**해 비교 | `test_live_exit...`, `test_depth::test_the_guard_benchmark_is_spot_not_twap`, 포크 |
| **F9** | 볼트에 **직접 보낸 토큰(기부)** 은 장부에 없는데 매도하면 `_settle`에서 산술 panic(스왑 후) | 코드 읽기 | 실패 사유가 익명 panic, 스왑 후 revert라 진단 불가 | `SellExceedsPosition`을 `_authorise`에서 사전 검사 | `test_Revert_WhenSellExceedsTheBook`, `test_SellEqualToTheBookIsAllowed` |
| **F10** | tx 해시가 `0x` 없이 장부·응답에 기록됐다 | 포크 x402 검증 실패 | 익스플로러에서 열 수 없는 “증거” | `hex_hash()`로 정규화(executor·facilitator) | 포크 x402 tx `0x23d8…` |
| **F11** | 데모 결제가 **자기 서버에 요청하면서 락을 쥐고 있었다** | 코드 읽기(`_lock` + self-request) | 대시보드가 결제 버튼에서 영구 정지 | 해당 엔드포인트는 락 밖에서 실행, `get_agent`는 별도 init 락 | `tests/test_api.py` |

### 2.2 실제 작동성·데모 (D)

| # | 결함 | 왜 심사에 치명적인가 | 조치 | 검증 |
|---|---|---|---|---|
| **D1** | 인터랙티브 데모 없음 — 심사위원이 숫자를 **바꿔볼 수단이 없다** | “완성도·기술 성숙도”의 첫인상이 정적 카드 | 시나리오 랩(`/api/simulate`), 게이트 추적(`/api/trace`), 프리셋 7종, 슬라이더 | `tests/test_lab.py`, `tests/test_api.py` |
| **D2** | 게이트가 **왜 거부했는지** 화면에 없다(첫 실패 사유 문자열만) | “정책이 강제한다”는 주장을 눈으로 확인할 수 없다 | `policy.evaluate()`가 계약과 같은 순서로 게이트 목록 반환(첫 실패에서 멈춤) | `test_gate_order_matches_the_contract` |
| **D3** | LLM이 어디에도 없다 — Track 1이 “AI Agent”인데 키워드 라우터뿐 | “AI가 어디 있나” 질문에 답이 없다 | 서술 계층 + **숫자 검증**(사실에 없는 수는 폐기) + **제안 심사**(LLM 제안을 정책·가드로 판정) | `tests/test_narrator.py` 15개 |
| **D4** | x402 정산이 paper 고정, `ChainFacilitator`는 한 번도 실행되지 않음 | “결제 통합”이 형식적 | 로컬 체인에서 실제 `transferWithAuthorization` 정산 + nonce 온체인 확인 | 포크 x402, gas 65,840 |
| **D5** | ERC-8004는 격리 테스트만, **살아있는 볼트·에이전트 조합**에서 검증된 적 없음 | 평판·검증이 “볼트 기록에서 온다”는 주장 미검증 | 포크에서 신원 등록→검증 요청→어댑터 응답 100→평판 36144를 볼트 값과 대조 | `evidence/e2e_fork.md` |
| **D6** | 에이전트가 체인에 대해 끝까지 도는 경로가 없다(모든 라이브 테스트가 fake) | F1·F5를 못 잡은 근본 원인 | `scripts/fork_demo.py` — 실제 에이전트·실제 라우터·실제 오라클 | 49 tx / 26 checks |
| **D7** | 전략이 **cap 크기로만** 견적해, 실제로 낼 크기(conviction 축소)의 비용을 모른다 | 얇은 풀에서 통과/거부가 모두 틀린다 | depth-aware sizing: 실제 크기로 재견적, 비용이 안 맞으면 크기를 줄이고, 그래도 안 되면 HOLD | 포크 50.91 USDT 진입 |
| **D8** | CI 없음 — “테스트 통과”가 커밋마다 증명되지 않는다 | 성숙도 신호 | `ci/ci.yml` (contracts / agent / fork) — 연결된 GitHub App에 `workflows` 권한이 없어 `.github/workflows/`에 올릴 수 없다. 저장소에서 Actions를 켠 뒤 복사하면 된다 | 워크플로 파일 |

### 2.3 증거·문서 (E)

| # | 결함 | 조치 |
|---|---|---|
| **E1** | “왕복 126bp” — 수수료 이중계상(F3). “146bp = 126 + 20”이라는 설명도 성립하지 않는다 | 65.7bp로 정정하고, 정책 하한 146bp는 **비용 + 80bp 마진**으로 정직하게 표기 |
| **E2** | “69/69, 125/125” 등 테스트 수치가 이후 추가분과 불일치 | 74/173으로 전면 갱신 |
| **E3** | 배포 가스 3,802,309 / 1.90 HSK — 코드 변경 후 재측정 필요 | 포크 실측 3,777,090 / **1.8885 HSK**(500.001 gwei), `deploy.py` 추정 3,827,004 → 1.9138 HSK / 배포 16,666B(init 18,785B) |
| **E4** | Slither 13건이 마지막 코드 변경을 반영하지 않음 | 2026-11-25 재실행, 13건 유지(High 1/Medium 1/Low 9/Info 2) + 목 토큰 3건 테스트 전용 명시 |
| **E5** | 제출 문서가 “공개 저장소 push 완료”인데 CI·데모·실측 문서가 없다 | 본 문서 + `docs/50_DEMO.md` + CI + 재현 명령 |

### 2.4 잔여 위험 (수용, 문서화)

| # | 항목 | 판단 |
|---|---|---|
| **R1** | `recordNoTrade`는 임의의 `edgeBps`를 기록할 수 있다(허들 하한만 강제) | 전략 키의 서명이 필요한 신호이며 자금 이동이 없다. “거부의 증거”라는 목적에는 충분. 위조된 거부는 평판에 반영되지 않음(어댑터는 `stats()`만 읽음) |
| **R2** | `tryEquity`가 등록된 모든 자산을 순회 — 소유자가 마켓을 많이 추가하면 거래 가스가 증가 | 소유자 권한·불변 프록시 없음. 마켓 3개 상한은 문서화된 운영 규칙 |

### 2.5 감사 중 “아차” 한 건 (정직하게)

포크 데모를 처음 돌렸을 때 에이전트가 `HSK_RPC`를 읽어 **메인넷**을 보고 있었다. 서명 계정은 anvil 공개
계정 #1로, 메인넷에 **다른 사용자가 남긴 이력(nonce 126)과 먼지 잔고(≈4×10⁻⁷ HSK)** 가 있었다. 900k gas ×
438 gwei(≈3.9×10¹⁴ wei)에 훨씬 못 미쳐 노드가 `insufficient funds`로 거부했고, **nonce는 126 그대로**다
(트랜잭션 미발생). 공개 테스트 키를 쓰는 데모는 이 인터록이 없으면 언제든 실수로 메인넷에 닿을 수 있다. 조치:
①데모가 `HSK_RPC`를 자기 포크로 고정하고 loopback을 assert, ②`ChainExecutor`에 **비-loopback RPC는
`SENTINEL_CONFIRM_MAINNET_TX=1` 없이는 브로드캐스트 거부** 인터록 추가. 이 인터록이 다음 실행에서
조용한 메인넷 전송을 코드 레벨에서 막는다.

---

## 3단계 — 전략 설계 (수상 경쟁력)

심사 공개 문구는 “완성도·기술 성숙도·혁신성”뿐이고 가중치는 비공개다. 그래서 **증명 가능한 것**에 집중했다.

| 기둥 | 무엇을 했나 | 어떤 심사 기준을 겨냥하나 |
|---|---|---|
| **P1. 실제로 도는 증거** | 포크 메인넷 49 tx end-to-end, 에이전트 장부↔볼트 대조, x402 실정산 | 완성도·기술 성숙도(“데모가 아니라 실행”) |
| **P2. 보이지 않는 것을 보이게** | 게이트 추적, 시나리오 랩, 거부 기록, LLM 숫자 검증 | 혁신성(“주장을 검사 가능하게”) |
| **P3. AI를 책임 있게** | LLM은 서술·제안만, 숫자와 주문은 결정론·정책이 결정 | 혁신성 + Track 1 정합성 |
| **P4. 비용을 정직하게** | 비용 모델 정정, depth-aware sizing, 실측 65.7bp | 기술 성숙도(자기 숫자를 의심) |
| **P5. 제출 가능한 형태** | CI, 문서, 재현 명령, 비밀값 스캔 | 완성도 |

**하지 않기로 한 것**(수상 전략의 일부): 메인넷 가스 지출, DoraHacks 최종 제출, 수익성 주장.
포크 시나리오의 +601bp는 **우리가 만든 반등**이지 알파가 아니다 — 문서·화면 모두 그렇게 표기한다.

---

## 4단계 — 프로세스 설계 (구현 순서·게이트)

| 순서 | 변경 범위 | 회귀 위험 | 게이트 |
|---|---|---|---|
| 1 | 정책 게이트 트레이스(`policy.evaluate`) | 미러 파리티 | 기존 32 테스트 → 신규 11 |
| 2 | 시나리오 랩(`agent/lab.py`) | 없음(읽기 전용) | `test_lab.py` 경계값 |
| 3 | 비용 모델·depth sizing(`market/signals/core`) | **거래 판정 전면** | 백테스트 + 유닛 + 포크 |
| 4 | 라이브 경로(F1·F2·F5·F10) | 자금 | `test_live_path` + **포크 end-to-end** |
| 5 | x402 chain settlement + mock EIP-3009 | 결제 | `test_x402` 21 + 포크 |
| 6 | ERC-8004 실볼트 연결 | 평판 | 포크 어댑터 검증 |
| 7 | 서버·UI·LLM | 데모 | `test_api.py` 10 + 실제 브라우저 스모크 |
| 8 | 컨트랙트 하드닝(`SellExceedsPosition`) | 자금 | forge 74 + Slither |
| 9 | 문서·CI·push | — | 비밀값 스캔, `git status`, 원격 확인 |

각 단계 후 **반드시 전체 스위트**를 돌렸다(부분 통과로 다음 단계를 시작하지 않음).

---

## 5단계 — 구현 (파일별 변경)

### 5.1 새 모듈

| 파일 | 역할 | 계약(테스트로 고정한 것) |
|---|---|---|
| `agent/lab.py` | 시나리오 1개를 전략→정책→가드로 판정, 게이트·한도·실제값 반환 | 계약과 같은 게이트 순서, 첫 실패에서 멈춤, 결정론 |
| `agent/narrator.py` | LLM 서술·제안 계층 | 사실에 없는 숫자는 폐기, 제안은 정책·가드가 거부, 결정 불변 |
| `src/mocks/MockEIP3009.sol` | EIP-3009 목 토큰(6dp, `transferWithAuthorization`, `authorizationState`) | Python 서명과 온체인 ECDSA 검증 일치 |
| `scripts/fork_demo.py` | 포크 end-to-end 오케스트레이터(배포·이탈·거부·진입·청산·8004·x402) | 26개 체크, 49 tx, loopback assert |
| `tests/test_lab.py` | 랩 경계 11개 | 오버사이즈·낙폭·킬스위치·청산 예외 |
| `tests/test_narrator.py` | LLM 경계 15개 | 999bp 폐기, 500 USDT 거부, flat SELL 거부 |
| `tests/test_api.py` | 대시보드 API 10개 | 실제 HTTP, 실제 에이전트 |
| `ci/ci.yml` | 계약·에이전트·포크 3잡 | — |

### 5.2 기존 파일의 의미 있는 변경

- `src/SentinelVault.sol` — `SellExceedsPosition` 사전 검사(스왑 전, 기부 토큰 케이스). 그 외 정책 커널 불변.
- `agent/policy.py` — `Verdict.gates` + `evaluate()`: 계약과 같은 순서, 적용 예외(`applies=false`) 표기, `build_min_out`에 5bp 헤드룸(청산이 프리미엄일 때 TWAP 미세 변동으로 `MinOutAboveFair`가 나는 것 방지).
- `agent/market.py` — `spot_impact_bps()`(수수료 포함, spot 기준), `swaps()` 캐시 키에 lookback 포함.
- `agent/signals.py` — 왕복 비용 = 두 impact의 합, `mode=dislocation`에서 역추세 차단 해제(별도 전략 id).
- `agent/core.py` — 라이브 실행기 자동 선택, 주문 크기 재견적, depth-aware sizing, 게이트 기록, LLM 서술 연결.
- `agent/intent.py` — `0x` 정규화(F1).
- `agent/executor.py` — non-loopback 브로드캐스트 인터록, revert 사유 재시뮬레이션, `0x` 해시.
- `agent/x402/facilitator.py` — 실제 브로드캐스트(로컬 서명), 영수증 status 확인, loopback 예외, `0x` 해시.
- `agent/premium.py` — paper/chain 선택, 자산·도메인 설정, chain 모드 verifier의 잔고 확인.
- `agent/server.py` — trace/simulate/presets/catalog/x402 info·demo/erc8004/reconcile/narrate, 락 데드락 수정.
- `web/index.html` — 전면 재작성(추적·랩·결제·평판·대조).
- `agent/config.py` — `Strategy.mode`, `SENTINEL_STRATEGY_MODE`.
- `agent/ledger.py` — `exits`, 승률 = 승/청산.

---

## 6단계 — 전략 점검 (수상 기준 대조)

| 기준 | 주장 | 증거 | 판정 |
|---|---|---|---|
| **독창성** | 전략 사전등록 + 선언/실현 엣지 온체인 대조 + 거부의 증거 | `SentinelVault` 상단 주석, `EdgeClaimAboveDislocation`, `recordNoTrade`, `edgeCredibilityBps` | 충족 |
| **기술 완성도** | 4개 표준이 실제 코드, 173+74 테스트, CI | `docs/12_OSS_INTEGRATION.md`, `ci.yml` | 충족 |
| **실제 작동성** | 배포→거래→청산→평판→결제가 한 번에 | `evidence/e2e_fork.md` 49 tx | 충족(포크) / **메인넷 배포는 미완**(가스 지출 금지 지시) |
| **보안** | 이중서명·TWAP·사후검증·불변식·Slither 트리아지 | `test/`, `evidence/slither-2026-11-25-*` | 충족, High 1건 구조적 수용(근거 문서화) |
| **AI 정합성** | LLM이 숫자를 만들지 않는다 | `tests/test_narrator.py`, `/api/narrate` | 충족 |
| **심사 접근성** | 3분 안에 만질 수 있는 데모 | `docs/50_DEMO.md` | 충족 |

**미흡해서 즉시 보완한 것**: ①처음 구현한 depth sizing이 “HOLD일 때는 작동하지 않아” 포크에서 거래가
안 났다 → HOLD의 비용 거부에도 재측정 적용. ②랩의 카운터팩추얼이 전략 크기와 섞여 오해를 낳았다 →
“전략이 낼 주문”과 “요청한 규모라면”을 분리. ③청산이 depth fitting에 들어가 HOLD로 뒤집혔다 →
SELL 제외 + `has_position` 전달.

**남은 공백(정직)**: 메인넷 배포·DoraHacks 등록은 지시대로 하지 않았다. 데모 영상도 미제작.

---

## 7단계 — 실행·가동 점검 (실측)

### 7.1 Foundry

```
$ forge test --no-match-path 'test/SentinelVault.fork.t.sol'
Ran 4 test suites: 69 tests passed, 0 failed, 0 skipped (69 total)
$ forge test --match-path 'test/SentinelVault.fork.t.sol'
Ran 1 test suite: 5 tests passed, 0 failed, 0 skipped (5 total)
                                                → 합계 74/74
```

신규 5: `test_Revert_WhenSellExceedsTheBook`, `test_SellEqualToTheBookIsAllowed`,
`test_ConfigViewsAndGuards`, `test_Revert_WhenSignerRotationCollapsesDualControl`, `test_Revert_WhenDepositIsZero`.

**기존 테스트 중 3개의 기대값을 고쳤다(동작이 아니라 가짜 거래소의 단위가 틀렸던 것)**:
`test_round_trip_cost_counts_both_legs`(이중계상 126bp를 기대), `test_live_buy_books_the_amount...`(가짜 체결가가
가짜 풀 스케일과 불일치), `test_live_exit_settles_the_round_trip`(포지션 스케일 불일치). 세 테스트가 지키던
동작(체결량은 영수증에서, 청산은 라운드트립을 정산)은 그대로이며 포크 end-to-end가 더 강하게 검증한다.

### 7.2 Python

```
$ python3 -m pytest tests/ -q
173 passed, 1 warning in 11.09s
```

내역: 기존 125 + `test_lab` 11 + `test_narrator` 15 + `test_api` 10 + `test_live_guards` 6 + `test_depth` 6.

### 7.3 포크 end-to-end (핵심 증거)

```
$ python3 scripts/fork_demo.py
chain 177 (fork of https://mainnet.hsk.xyz) · TWAP window 600s · depth ≈ $424,873
deploy SentinelVault            3,777,090 gas
deploy Identity/Reputation/Validation/Adapter
whale sells 6,000 WHSK × 17 → dislocation 381bp (real router, real pool)
default strategy  → HOLD (adverse trend) → recordNoTrade tx
dislocation strat → BUY 50.907559 USDT, claim 166bp (hurdle 146bp) → executeIntent tx
whale buys 1,500 USDT × 9 → +275bp
agent             → SELL, realised 601.0bp → executeIntent tx
ERC-8004          → validation response 100, reputation 36144 (= vault.edgeCredibilityBps())
reconcile         → ledger vs vault: 4/4 rows match
x402              → 0.001 USDT transferWithAuthorization, gas 65,840, nonce consumed on-chain
26 checks passed · 49 transactions · evidence/e2e_fork.json, .md
```

**정직한 해석**: +601bp는 알파가 아니다. 우리가 고래 매수로 풀을 +275bp까지 되돌린 **시나리오**다.
이 실행이 증명하는 것은 수익성이 아니라 **(a) 에이전트의 결정이 온체인에서 그대로 집행되고,
(b) 주장이 청산에서 정산되며, (c) 평판·검증·결제가 그 기록 위에서 실제로 동작한다**는 것이다.

### 7.4 백테스트·페이퍼

```
$ python3 scripts/backtest.py
bars=40 from a 80,000-block window
impact_in=33.0bp impact_out=32.7bp round_trip=65.7bp (fee inside each quote) size=120 USDT
REAL       trades 0 · no_trade 20
MR SYNTH   trades 3 (2 entries, 1 exit) · +4.33 USDT · declared 358 → realised 442 (credibility 12,346bp)
DOWN SYNTH trades 0 (역추세 차단)
$ python3 scripts/run_paper.py --cycles 3   → HOLD ×3, cost 66bp, no-trade 기록
```

### 7.5 정적 분석

```
$ python3 -m slither src/SentinelVault.sol --compile-force-framework solc ...
13 result(s) found — High 1 (reentrancy-balance), Medium 1 (reentrancy-no-eth), Low 9, Info 2
$ python3 -m slither src/mocks/MockEIP3009.sol ...
3 result(s) found — 테스트 전용 토큰(constable-states 등), 자금 보관 없음
```

High/Medium 수용 근거는 `docs/15_SLITHER_TRIAGE.md` §3 그대로이며, 신규 `SellExceedsPosition`은
복잡도만 +1(16)로 늘고 신규 지적은 없다.

### 7.6 대시보드 스모크

```
$ python3 -m agent.server --port 8765
GET /health              200 {"ok": true}
GET /api/state           시장·정책·가드·런타임(9 action)·LLM 상태
GET /api/step            실제 판단 1회
GET /api/trace           게이트 8~12행
GET /api/simulate?dev=-400&strategy=dislocation   → BUY 61.2 USDT
GET /api/presets         7개 시나리오
GET /api/x402/info        payee/가격/정산 모드
```

### 7.7 비밀값·push

```
$ git ls-files | grep -iE 'env|wallet|key|pem'   → .env.example, gen_keys.py, (lib의 벤더 파일)만
$ git grep -nE 'private[_-]?key\s*=\s*0x[0-9a-f]{64}'  → 0건
$ git status --short                              → 커밋 대상만
→ 공개 저장소 2010180016y-netizen/sentinel-vault-hsk master push (9e52b39, 462 files)
```

`.env.wallets.local`(600)은 `.gitignore`(`.env*`, `*.local`, `*wallet*`, `*.key`)로 제외, 스크립트는 키를
출력하지 않는다. 포크 데모가 쓰는 키는 **anvil 기본 니모닉**이며 메인넷 자금이 없다.

---

## 부록 A — 커밋 전 확인한 “주장 vs 실측” 대조

| 문서 주장(작업 전) | 실측(작업 후) | 처리 |
|---|---|---|
| 왕복 126bp = 2×30 + 33 + 33 | **65.7bp** = 33.0 + 32.7 (수수료 포함) | README·아키텍처·리포트 정정 |
| 허들 146bp = 126 + 20 | 146bp = 65.7 + **80bp 마진**(의도적 보수) | 설명 교체 |
| Foundry 69 / Python 125 | 74 / 173 | 전면 갱신 |
| 배포 3,802,309 gas / 1.90 HSK | **3,777,090 / 1.8885 HSK** / 16,666B | 갱신 |
| “35 기록 바, 거래 0건” | **40 바 / 80k 창, 거래 0건 / 거부 20건** | 갱신 |
| x402 “paper 정산” | paper **또는 chain**(포크 실정산) | 문서·코드 갱신 |
| “라이브 모드” | 이제 실제로 라이브(인터록 포함) | F1·F2 |

## 부록 B — 재현 명령

```bash
export PATH="$HOME/.foundry/bin:$PATH"
forge build && forge test --no-match-path 'test/SentinelVault.fork.t.sol'
python3 -m pytest tests/ -q
python3 scripts/backtest.py
python3 scripts/fork_demo.py            # 포크 49 tx (~2.5분, 네트워크 필요)
python3 -m agent.server --port 8765     # 대시보드
```

## 부록 C — 이번 작업에서 바뀐 파일

```
신규  agent/lab.py · agent/narrator.py · src/mocks/MockEIP3009.sol · scripts/fork_demo.py
      tests/test_lab.py · tests/test_narrator.py · tests/test_api.py · tests/test_live_guards.py · tests/test_depth.py · ci/ci.yml
      docs/12_deep_audit_and_upgrade.md · docs/50_DEMO.md · evidence/e2e_fork.{json,md}
수정  src/SentinelVault.sol · agent/{policy,market,signals,core,intent,executor,ledger,config,server,premium}.py
      agent/x402/facilitator.py · agent/plugins/sentinel.py · web/index.html · scripts/{backtest,report}.py
      test/SentinelVault.t.sol · tests/{test_agent,test_live_path,test_x402}.py
      README.md · docs/{00_FACTS,15_SLITHER_TRIAGE,30_ARCHITECTURE,40_SUBMISSION,90_FINAL_REPORT}.md
      .env.example
```
