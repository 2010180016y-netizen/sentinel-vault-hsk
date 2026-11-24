# 대회 실측 사실관계 (2026-11-24 확인)

출처: DoraHacks 상세 페이지(`dorahacks.io/hackathon/hskchainseoul/detail`), Luma 한국어 페이지(`luma.com/mwn8lt3q`), 대회 Q&A, Testnet Faucet & Tokens 탭, Agentic Payment Guide 탭, Builds 목록. 모두 2026-11-24 직접 조회.

## 1. 일정·규모

| 항목 | 값 |
|---|---|
| 대회명 | HSK Chain Horizon Hackathon · Seoul |
| 주최 | HashKey Chain (진행: Pentabase, HashKey On-Chain, Flora HSK) |
| 기간 | 2026-11-20 ~ 2026-12-05 |
| 사전등록 시작 | 2026-11-17 12:00 |
| 제출 오픈 | 2026-11-20 13:00 |
| **등록·제출 마감** | **2026-12-03 13:00 KST** (등록과 제출이 동시 마감, 이후 제출은 심사 제외) |
| 데모 발표팀·일정 발표 | 2026-12-04 |
| Demo Day | 2026-12-05, 서울 (장소는 승인된 참석자에게만 공개) |
| 수상팀 발표 | 2026-12-06 |
| 총상금 | 12,000 USDT (트랙별 6,000) |
| 트랙별 시상 | 1등 3,000 / 2등 2,000 / 3등 1,000 USDT |
| 현재 참가 규모 | Builds 16, Hackers 91 (2026-11-24 기준), 제출 마감까지 8일 |

## 2. 트랙

- **Track 1 — AI Agent Trading**: HSKSwap 같은 DEX에서 시장·온체인 데이터를 분석해 신호를 잡고 자율 주문 실행. 요구 기능으로 실시간 모니터링, 매매 전략·신호 분석, **차익거래·스프레드 기회 탐색**, 자동 주문 실행·포지션 관리, **손절 등 리스크 관리**, 대화형 인터페이스(포지션·손익 확인, 포지션 크기/거래 빈도/리스크 성향/대상 자산 조정)를 명시.
- **Track 2 — Agent Payments & A2A Settlement**: HSP(HashKey Settlement Platform) **필수**. HSP API로 결제 조건 수신 → Payment Mandate 생성 → 지갑 서명 → HSP Gateway로 온체인 결제 검증·실행.

## 3. 제출 요건 (영문 원문 요지)

1. 국가·지역 무관, 개인/팀 참가 가능.
2. 독창적이어야 하며 **해커톤 기간 중 새로 개발하거나 재구축**한 결과물이어야 함.
3. HSK Chain 기반으로 두 트랙 중 하나 선택.
4. **공식 권장 제품·프레임워크를 쓰면 평가에서 가산점.**
5. Track 2는 반드시 HSP 사용.
6. **"Deploy the project's smart contract on HSK Chain mainnet. Projects without a mainnet deployment automatically lose eligibility for demo selection."** → 메인넷 배포는 데모 발표 후보 자격 요건.
7. 서울 현장 발표가 가능한 팀은 **데모 발표팀 선정에서 우선 고려**(필수는 아님).
8. 수상팀은 상금 수령을 위해 HSK Chain 메인넷 배포 필요. 상금은 발표 후 영업일 14일 이내 지급.

## 4. 심사

- 공개된 심사 기준은 "Judges will review projects for a fair, comprehensive evaluation" 뿐이며 **가중치·세부 항목은 비공개**.
- 데모 발표팀은 **완성도(completeness), 기술적 성숙도(technical maturity), 혁신성(innovation)** 기준으로 선정된다고 명시.

→ 전략적 함의: 계량 기준이 없으므로 ①메인넷에 실제로 배포된 실물 ②완성도(테스트·데모·문서) ③혁신성(다른 팀이 안 한 것) ④공식 스택(HSKSwap/HSP/Chainlink CCIP) 사용을 심사 방어선으로 삼아야 한다.

## 5. 모순·확인 필요 사항

| 쟁점 | 상세 페이지 | Testnet Faucet 탭 | 해석 |
|---|---|---|---|
| 메인넷 배포 시점 | "메인넷 배포 없으면 데모 선정 자격 상실", "수상팀은 메인넷 배포 필요" | "**수상팀은 해커톤 종료 후** 메인넷으로 마이그레이션·배포할 수 있다" | 제출 시점 메인넷 배포가 원칙이나, FAQ성 문서는 사후 배포를 허용하는 듯 읽힌다. **주최 측 확인 없이는 "제출 전 배포"를 기본으로 잡는 것이 안전.** |

- 대회 Q&A에 같은 질문(메인넷 배포 시점·형식, 가스 지원, 온라인 발표 형식)이 이미 올라와 있으나 **2026-11-24 기준 답변 0건**. (Phant Studio가 "500 gwei에서 3~5 HSK 필요, 일본 에디션처럼 가스 지원이 있나"라고 질문)
- 추가 확인 필요: 온라인 전용 참가 시 데모 형식(사전 녹화 영상 허용 여부), 데모 슬롯 미배정 팀의 수상 자격.

## 6. 개발 리소스 (공식 링크)

- HashFans 개발자 리소스·커뮤니티, HSK Chain 토큰 컨트랙트 문서, Chainlink CCIP Router 디렉터리, HSP API 문서, Telegram 지원 그룹, HSP Agentic Payment Setup & Execution Guide.
- Testnet Faucet: HSKChain Testnet(133) HSK 수도꼭지 / USDC는 Ethereum Sepolia의 Circle 공식 테스트 USDC(`0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`).

## 7. 네트워크·컨트랙트 실측 (체인에서 직접 확인)

| 항목 | 값 |
|---|---|
| HSK Chain 메인넷 | chainId **177** (`0xb1`), RPC `https://mainnet.hsk.xyz`, explorer `https://hsk.blockscout.com`, 네이티브 HSK(18) |
| HSK Chain 테스트넷 | chainId **133** (`0x85`), RPC `https://testnet.hsk.xyz` |
| 블록타임 | 2초 (최근 10블록 실측) |
| 가스 가격(2026-11-24) | **500.001 gwei** |
| HSK 가격(온체인 유도) | 약 0.0805~0.0808 USDT (WHSK/USDT 0.3% 풀 실측) |
| WETH 가격(온체인 유도) | 약 1,704 USDT (USDT/WETH 0.3% 풀) |
| WBTC 가격(온체인 유도) | 약 61,830 USDT (USDT/WBTC 0.3% 풀) |

계약 배포 비용 추정: 타 팀 공개 질문 기준 3~5 HSK(≈ $0.24~0.40). 본 작업에서 anvil 포크로 실제 배포해 **실측치로 대체**했다(→ `docs/40_SUBMISSION.md`).

## 8. Track 2의 기술적 장벽 (실측)

- HSP QA MCP 엔드포인트 `https://mcp-qa.hashkeymerchant.com/health` → 본 환경에서 **HTTP 403 (Cloudflare "Sorry, you have been blocked")**. 대회 가이드 1단계가 "프로젝트 팀의 공인 egress IP를 HSP Operations에 전달해 allowlist 등록"이므로 **정상적인 차단**이다.
- 즉 Track 2는 ①주최 측 IP allowlist 등록(외부 대기) ②지갑 수동 승인·테스트 토큰 충전(팀의 수동 작업)이 선행되어야 첫 결제가 가능하다. 남은 8일 중 외부 응답 대기가 리스크.
- Track 1은 HSKSwap 메인넷이 이미 살아 있고(풀 5개, WHSK/USDT 유동성 **quote-side 약 $419k / 양쪽 합 약 $839k**, 2026-11-24 재측정) 읽기·견적·TWAP이 모두 무허가로 가능하다 → **우리 환경에서 완주 가능한 트랙은 Track 1**.

> 유동성 표기 주의: 두 숫자가 문서마다 섞여 있었다(README $420k / 이 문서 $840k). 둘 다 맞지만 정의가 다르다 —
> quote-side(USDT 쪽, `PoolState.depth_quote()`가 내는 값)가 $419,423이고, 자산 쪽까지 더한 양쪽 합이 $838,845다.
> 명령(`liquidity()` × tick)은 같고, 어느 쪽을 세느냐만 다르다. 이후 문서는 **quote-side를 기본**으로 쓰고 필요하면 병기한다.
