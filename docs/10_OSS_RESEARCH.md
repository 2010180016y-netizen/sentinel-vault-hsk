# 오픈소스 리서치·설치·내재화 기록

- 작성: 2026-11-24 (KST)
- 대상: HSK Chain Horizon Hackathon · Seoul / Track 1 (AI Agent Trading)
- 원칙: "읽었다"가 아니라 **내려받아 설치하고, 코드를 열어 확인하고, 우리 설계에 반영한 것만** 기록한다.

모든 저장소는 `competitions/hsk-chain-seoul-2026/oss/`에 실제로 클론되어 있고, 아래 커밋 해시는 `git -C <repo> log -1`로 확인한 실측값이다.

---

## 1. 설치·실행 확인 (증거)

| 도구 | 버전/결과 | 확인 명령 |
|---|---|---|
| Foundry (forge/cast/anvil/chisel) | `forge 1.10.0` / commit `083e229941` / build 2026-11-19 | `forge --version` |
| solc | `0.8.24` (py-solc-x로 설치) | `solcx.get_installed_solc_versions()` |
| Python | 3.12.16 | `python3 --version` |
| web3.py | 8.0.0 | `pip list` |
| eth-tester / py-evm | 0.14.0b1 / 0.12.1b1 | `pip list` |
| eth-account | 0.14.0 | `pip list` |

> 주의: 이 실행 환경의 `/tmp`는 128MB tmpfs라 foundryup 압축 해제가 `No space left on device`로 실패했다. `TMPDIR=/workspace/.tmp`로 바꿔 재실행해 성공했다. (재현 시 필요)

---

## 2. 클론·분석한 오픈소스 목록

| # | 저장소 | 커밋 | 라이선스 | 실제로 한 일 | 얻은 것 |
|---|---|---|---|---|---|
| 1 | [foundry-rs/foundry](https://github.com/foundry-rs/foundry) | `5c5ec26` | MIT/Apache | `foundryup`으로 바이너리 설치, `forge`/`cast`/`anvil` 사용 | 메인넷 포크 테스트, fuzz/invariant, revert selector 단위 검증 |
| 2 | [OpenZeppelin/openzeppelin-contracts](https://github.com/OpenZeppelin/openzeppelin-contracts) | `5006930` (v5.7.0) | MIT | `ReentrancyGuard.sol`, `SafeERC20.sol`, `ECDSA.sol`, `MessageHashUtils.sol`, `Ownable2Step`, `Pausable` 소스 확인 후 의존성으로 사용 | EIP-712 서명 검증·재진입 방지·비표준 ERC20 대응 |
| 3 | [Uniswap/v3-core](https://github.com/Uniswap/v3-core) | `5ea68a7` | BUSL→GPL-2.0 | `UniswapV3Pool.sol`의 `observe`/`slot0`/`Swap` 이벤트, `libraries/TickMath.sol` 분석 | TWAP 오라클 사용법, tick↔가격 변환의 정확 구현 |
| 4 | [Uniswap/v3-periphery](https://github.com/Uniswap/v3-periphery) | `24ccf01` | GPL-2.0 | `interfaces/ISwapRouter.sol`, `interfaces/IQuoterV2.sol`, `libraries/OracleLibrary.sol` 분석 | `QuoterV2`는 view가 아니어서 **온체인 호출 불가**, `OracleLibrary.consult` 패턴 |
| 5 | [coinbase/x402](https://github.com/coinbase/x402) | `0e6e8c5` (2026-10-15) | Apache-2.0 | `specs/x402-specification-v2.md`, `specs/schemes/exact/*` 정독 | 에이전트 결제의 verify→settle 순서, EIP-3009 nonce/deadline 재전송 방지 |
| 6 | [google-agentic-commerce/AP2](https://github.com/google-agentic-commerce/AP2) | `e1ea56d` (2026-04-29) | Apache-2.0 | README·docs(개요/용어)·SDK 구조 확인 | Intent/Cart/Payment Mandate 3계층 책임 분리, 검증가능 자격증명 |
| 7 | [coinbase/agentkit](https://github.com/coinbase/agentkit) | `f2f3e8c` (2026-10-15) | Apache-2.0 | `python/coinbase-agentkit/.../action_providers/action_provider.py`, `wallet_providers/*`, `action_providers/{x402,pyth,erc20}` | ActionProvider 스키마 패턴, 네트워크 게이팅, "에이전트가 데이터를 사서 쓰는" x402 소비 패턴 |
| 8 | [safe-global/safe-smart-account](https://github.com/safe-global/safe-smart-account) | `247b8ce` | LGPL-3.0 | `base/GuardManager.sol`의 `checkTransaction`/`checkAfterExecution`, 모듈 실행 흐름 확인 | 실행 전·후 이중 검사(post-condition) 개념, 임계 서명(2-of-N) |
| 9 | [gnosis/zodiac](https://github.com/gnosis/zodiac) | `b7988c0` | LGPL-3.0 | README의 Avatar/Module/Modifier/Guard 4계층, `mastercopies/` 구조 | "에이전트 키에 자금 권한을 주지 않는다"는 아키텍처 원칙 |
| 10 | [gnosisguild/zodiac-core](https://github.com/gnosisguild/zodiac-core) | (HEAD) | LGPL-3.0 | `contracts/core/{Module,Modifier,GuardableModule,Operation}.sol` | 실행 위임과 가드 체인의 분리 |
| 11 | [gnosis/zodiac-modifier-roles](https://github.com/gnosis/zodiac-modifier-roles) | (HEAD, v3) | LGPL-3.0 | `Roles.sol`, `types/{Permission,Condition,Allowance}.sol`, `common/{AllowanceConsumer,PriceLoader}.sol` | **함수 단위 + 파라미터 조건 단위 권한 스코프**, 허용량(allowance) 소비 모델 |
| 12 | [@hskswap/sdk-core (npm)](https://www.npmjs.com/package/@hskswap/sdk-core) | `1.0.3` | GPL | tarball을 내려받아 `dist/sdk-core.cjs.development.js`에서 체인 177/133 컨트랙트 주소 테이블 추출 | HSKSwap 공식 라우터·쿼터·팩토리 주소를 **추측 없이** 확보 |
| 13 | HSK Chain 공식 문서 | - | - | `docs.hskchain.net` network-info / token-contracts / contract-addresses | 체인 177·133, WHSK/USDT/USDC/WETH/WBTC 주소·소수점 |
| 14 | HSK Blockscout | - | - | `hsk.blockscout.com/api?module=contract&action=getabi` | SwapRouter02·QuoterV2·Factory·Pool의 **검증된 ABI** 확보 |

부가로, npm tarball까지 포함해 12개 저장소·패키지를 실제로 내려받아 코드를 열었다. 다만 아래 3개는 "설치·실행"까지는 가지 않고 소스·문서 분석에 머물렀다(정직하게 구분): AP2 샘플 실행(Google API Key 필요), x402 facilitator 실행(별도 서비스 필요), Roles v3 배포(미출시 버전).

### 2.1 2차 설치·통합 (2026-11-25) — 코드로 들어간 4건

| # | 저장소/표준 | 커밋 | 우리 코드에 들어간 형태 |
|---|---|---|---|
| 15 | [elizaOS/eliza](https://github.com/elizaOS/eliza) | `4ad4af8` | `agent/runtime/` (Action/Provider/Evaluator/Plugin) + `agent/plugins/sentinel.py` — 채팅 경로가 런타임을 지난다 |
| 16 | [Uniswap/uniswap-ai](https://github.com/Uniswap/uniswap-ai) | `989dbaf` | `agent/guards.py` + `src/libraries/SwapGuardLib.sol` — 같은 가드 13개, 공유 표 19 시나리오 |
| 17 | [coinbase/x402](https://github.com/coinbase/x402) | `0e6e8c5` | `agent/x402/` + `agent/premium.py` — EIP-3009 검증·1회용 nonce·HTTP 402 게이트 (정산은 paper, 메인넷 금지) |
| 18 | ERC-8004 Trustless Agents | EIP 본문 | `src/registries/*` + `src/Sentinel8004Adapter.sol` + `agent/registry8004.py` |

상세 구현·테스트·의도적 변경점은 **`docs/12_OSS_INTEGRATION.md`** 에 있다. 이 문서(§3~§4)는 1차 리서치 기록으로 남긴다.

---

## 3. 코드에서 직접 확인한 핵심 사실 (설계를 바꾼 것들)

### 3.1 QuoterV2는 온체인에서 호출할 수 없다
`v3-periphery/contracts/interfaces/IQuoterV2.sol` 주석 그대로:
> "These functions are not marked view because they rely on calling non-view functions and reverting to compute the result. They are also not gas efficient and **should not be called on-chain**."

→ **설계 변경**: "컨트랙트가 쿼터를 직접 호출해 최신 견적을 검증한다"는 안은 폐기. 대신 컨트랙트가 **풀 오라클(TWAP)에서 스스로 가격 하한선을 계산**하고, 에이전트가 제시한 견적은 신뢰하지 않는다. 에이전트의 견적 조작·지연 공격이 원천 차단된다.

### 3.2 SwapRouter02에는 deadline 파라미터가 없다
Blockscout에서 받은 검증 ABI:
```
exactInputSingle((address tokenIn,address tokenOut,uint24 fee,address recipient,
                  uint256 amountIn,uint256 amountOutMinimum,uint160 sqrtPriceLimitX96))
multicall(uint256 deadline, bytes[] data)          // ← 데드라인은 여기로
```
→ v1 `SwapRouter`의 `deadline` 필드를 그대로 쓰면 컴파일은 되어도 의미가 없다. **`multicall(deadline, ...)`로 감싸** 라우터 레벨 만료를 실제로 강제한다.

### 3.3 풀 주소는 반드시 팩토리에서 재계산해 검증한다
`v3-periphery/libraries/{PoolAddress,CallbackValidation}.sol`의 존재 이유가 곧 공격 사례다. 임의의 "가짜 풀"을 신뢰하면 TWAP을 통째로 조작할 수 있다.
→ **설계 반영**: `factory.getPool(tokenIn, tokenOut, fee)`와 인텐트에 실린 풀 주소가 다르면 revert.

### 3.4 TWAP은 `observe`의 tick 누적으로만 만든다
`UniswapV3Pool.observe(uint32[] secondsAgos)`는 `tickCumulatives`를 돌려준다. 실측한 HSKSwap WHSK/USDT 풀은 `observationCardinality = 240`으로 오라클이 살아 있었다.
→ **설계 반영**: 리스크 판단의 기준가를 `slot0`(스팟)이 아니라 10분 TWAP으로 삼는다. 스팟은 단일 트랜잭션 안에서 플래시스왑으로 조작 가능하다.

### 3.5 x402의 "verify-then-settle"과 EIP-3009 재전송 방지
`specs/schemes/exact/scheme_exact_evm.md`는 `TransferWithAuthorization(from,to,value,validAfter,validBefore,nonce)`를 쓰고, facilitator가 **실행 전에 검증하고 실행 후에 정산**한다. 금액 상한은 클라이언트(지갑) 쪽 예산 관리로 처리한다.
→ **설계 반영**: 우리 `PolicyIntent`도 `validAfter`/`validBefore`/`nonce` 삼종 세트를 갖는다. 그리고 "검증→실행→사후 재검증" 순서를 컨트랙트 안에 그대로 옮겼다(실행 후 잔고 델타로 `minAmountOut` 재확인).

### 3.6 AP2의 3계층 책임 분리
AP2는 (1) 사용자 의도(Intent Mandate) (2) 판매자 청구(Cart Mandate) (3) 지불자 승인(Payment Mandate)을 분리하고, 각 단계에 서명·검증가능 자격증명을 붙인다.
→ **설계 반영**: 우리는 (1) 전략 에이전트의 `PolicyIntent` (2) 리스크 서명자의 승인 (3) 체인의 `IntentExecuted` 영수증 — 3계층으로 옮겼다. 사후에 "누가 왜 원했고, 누가 승인했고, 무엇이 정산됐는지"가 온체인에 남는다.

### 3.7 Safe Guard의 사전·사후 이중 검사
`GuardManager.sol`의 `checkTransaction`(사전) + `checkAfterExecution(bytes32 hash, bool success)`(사후).
→ **설계 반영**: 실행 전 정책 검사뿐 아니라 **실행 후 잔고 델타 검사**를 필수로 넣었다. 사전 검사만으로는 슬리피지·수수료·비정상 풀 동작을 잡지 못한다.

### 3.8 Zodiac / Roles: 에이전트 키에 자금 권한을 주지 않는다
Zodiac은 Avatar(자금 보관) / Module(의사결정) / Modifier(권한 제한) / Guard(검사)를 분리한다. Roles는 "허용 주소 + 허용 함수 + 파라미터 조건" 단위로 권한을 스코프한다.
→ **설계 반영**: 에이전트 개인키는 **의사표시만** 한다. 자금은 볼트가 보관하고, 볼트는 "이 함수, 이 파라미터 범위"만 실행한다. 에이전트 키가 유출돼도 정책 밖 거래는 불가능하다.

### 3.9 AgentKit의 ActionProvider 패턴
`ActionProvider.get_actions()`는 `(name, description, args_schema, invoke)` 튜플로 행동을 노출하고, `supports_network(network)`로 네트워크별 게이팅을 한다.
→ **설계 반영**: 우리 에이전트의 도구 레지스트리도 스키마 타입이 붙은 액션으로 노출하고, 모든 RPC 호출 앞에 `chain_id == 177` 단정을 둔다(잘못된 체인에서의 서명·전송 사고 방지).

### 3.10 OZ의 실무 디테일
- `ECDSA.recoverCalldata`는 high-s(가변성) 서명을 거부한다(`ECDSAInvalidSignatureS`). → 서명 가변성 악용 차단.
- `SafeERC20.forceApprove`는 USDT류(0이 아닌 값에서 0이 아닌 값으로 approve 거부) 토큰을 처리한다. → **매매 후 allowance를 0으로 되돌리는** 습관과 함께 사용.
- `ReentrancyGuard` nonReentrant → 스왑 콜백 경로에서 재진입 차단.

---

## 4. 오픈소스를 그대로 쓰지 않고 "우리식으로" 바꾼 지점

| 오픈소스 패턴 | 그대로 쓸 때의 문제 | 우리 구현 |
|---|---|---|
| Safe의 2-of-N 임계 서명 (온체인 승인 트랜잭션 필요) | 매매마다 온체인 승인 tx가 하나 더 붙어 가스 2배 | **오프체인 EIP-712 이중 서명 + 온체인 단일 실행**. 두 키의 서명을 한 트랜잭션에서 검증 → 승인 지연 0, 가스 1회 |
| Zodiac Roles의 온체인 역할/권한 저장 구조 | 권한 1건 변경에 다수 트랜잭션, 가스·복잡도 큼 | **정책을 단일 struct로 압축**해 `setPolicy` 1회로 교체, 변경은 이벤트로 감사 |
| Uniswap `OracleLibrary.consult` (periphery 의존) | periphery 전체 의존성 유입 | 필요한 부분(TickMath + consult 계산)만 **자체 라이브러리로 내재화** |
| x402 facilitator (별도 서비스 신뢰) | 외부 서비스 다운 시 결제 불가 | **검증 계층은 직접 구현**(EIP-3009 서명·nonce·유효창), 정산은 paper. 메인넷 브로드캐스트는 코드에서 금지(`MainnetSpendForbidden`) |
| AgentKit (Python/TS 프레임워크 전체) | 프레임워크 종속, 해커톤 심사에서 "우리 코드"가 흐려짐 | **패턴(액션 스키마·네트워크 게이팅)만 차용**하고 코어는 자체 구현 |
| Pyth 등 외부 오라클 가격 | HSK 메인넷에 Pyth 피드가 없음(확인 필요) + 외부 의존 | **HSKSwap 풀 자체 TWAP**을 기준가로 사용. 외부 오라클 없이 자기완결적 |

---

## 5. 참고: HSKSwap 온체인 실측 (오픈소스 주소 테이블 → 체인 검증)

`@hskswap/sdk-core@1.0.3`의 주소 테이블을 그대로 믿지 않고, HSK 메인넷(177)에서 코드 존재와 팩토리 질의로 교차 검증했다.

| 항목 | 주소 | 검증 |
|---|---|---|
| V3 Factory | `0x972cA9D1662F5e029cD18327D29026532E84c742` | 코드 24,535 bytes |
| SwapRouter02 | `0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3` | 코드 24,497 bytes, Blockscout ABI 검증됨 |
| QuoterV2 | `0x603f70466fDdbE3F238220B9a74FFF419a2BbFDD` | 코드 8,273 bytes |
| NonfungiblePositionManager | `0xDF0A9b833Db0E4CeEa103A4408f3B68e7FC0cad5` | 코드 24,384 bytes |
| Multicall | `0x7D0CBa43EbDD69ec2CE3B5AeAD3b0FbC796c565B` | 코드 1,383 bytes |
| WHSK / USDT / USDC.e / WETH / WBTC | `0xB210…1Cf1` / `0xf1b5…9029` / `0x054e…8D0a` / `0xefd4…Cdfd0` / `0x6119…5b14` | symbol·decimals 실측 (18/6/6/18/8) |

실측 풀 (2026-11-24, block 30,575,819):

| 풀 | 주소 | 상태 |
|---|---|---|
| WHSK/USDT 0.3% | `0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC` | 활성, L=1.4758e18, tick −301,487 |
| WHSK/USDC.e 0.3% | `0xB580D654cE892181937Aac80EA165950CB7D0E02` | 극미 유동성(L=2.5e11, 약 $0.08 상당) |
| WHSK/USDC.e 1% | `0x9861246B8BdF619Cc0e8C0422E0a662b51A2c3F4` | 유동성 0 (빈 풀) |
| USDT/WETH 0.3% | `0xb92b79ed6b370D5cFee2E14B1D0983C258304C16` | 활성 |
| USDT/WBTC 0.3% | `0xd9EA2cE468957Ba49cCC38C4758Ed6ecD561449A` | 활성 |

**전략적 함의(중요)**: HSKSwap은 현재 5개 풀의 **성형(star) 토폴로지**로 USDT를 중심으로 연결되어 있다. 3개 풀이 고리를 이루지 않으므로 **삼각 차익거래 경로가 존재하지 않는다**. 따라서 "차익거래 봇"으로 트랙을 잡으면 데모에서 실체가 드러난다. 우리는 대신 **TWAP 기준 상대가치 이탈 + 유동성 대비 주문 크기 규율**로 전략을 재정의했다(`docs/30_ARCHITECTURE.md` 3장).
