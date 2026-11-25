# 제출 준비 · 회기님 조치 목록

## 0. 현재 상태 (2026-11-25, 7단계 감사·업그레이드 후)

| 항목 | 상태 |
|---|---|
| 트랙 | **Track 1 — AI Agent Trading** 확정 |
| 컨트랙트 | `SentinelVault` + `SwapGuardLib` + ERC-8004 레지스트리·어댑터 + EIP-3009 목 토큰, **74/74** 테스트(포크 실거래 5건 포함) |
| 에이전트 | Python 코어 + elizaOS형 런타임 + x402(paper/chain) + 가드 + 시나리오 랩 + LLM 계층, **173/173** |
| 포크 end-to-end | **49 tx · 26 checks** (`evidence/e2e_fork.md`) — 배포→거래→청산→평판→x402 실정산→장부 대조 |
| 오픈소스 통합 | elizaOS·uniswap-ai·x402·ERC-8004 코드 반영 (`docs/12_OSS_INTEGRATION.md`) |
| 감사 | Slither **13건**(High 1 / Medium 1 / Low 9 / Info 2) + 목 토큰 3(테스트 전용), `docs/15_SLITHER_TRIAGE.md` |
| CI | `ci/ci.yml` (contracts / agent / fork) |
| 메인넷 배포 | **미완** — 가스 1.8885 HSK 필요(지출 금지 지시) |
| DoraHacks 등록 | **미완** — 회기님 계정 필요 |
| GitHub 공개 저장소 | `2010180016y-netizen/sentinel-vault-hsk` (2026-11-25 master push) |
| 데모 영상 | 미제작 — 흐름·숫자 확정(`docs/50_DEMO.md`) |

---

## 1. 회기님이 결정해야 하는 것

### (1) 지갑 — 필수
- 필요: EVM 지갑 3개(소유자/배포자, 전략 서명, 리스크 서명). 세 역할을 한 키로 쓰면 컨트랙트가
  `InvalidConfig`로 배포를 거부한다(이중 통제가 형식이 되지 않도록 한 장치).
- 현재: 신규 EVM 지갑 3개가 로컬 `.env.wallets.local`(600, git 제외)에 있다.
  Owner/Deployer `0x7B3f005fF9AAF4eAae2CF4c7A3343B2384383a83`,
  Strategy `0x7319aF430CED91CEB56279af86b973B0C67c1B02`, Risk `0xCB18eeaF90f75472da6637d490Bb668feDC29544`.
- 선택지: ①이 지갑 사용 ②회기님이 쓰시는 지갑으로 교체(`gen_keys.py` 재실행 또는 `setSigners`).

### (2) HSK 가스 — 소액
- 실측: 3,777,090 gas × 500.001 gwei = **1.8885 HSK** (L1 데이터 수수료 ~0.0003 포함), HSK $0.0808 → **≈ $0.15**.
- Owner 지갑에 **2.0 HSK**면 배포 + `setMarket/registerStrategy/unpause/deposit`까지 충분하다.
- 대안: 대회 측 가스 지원 문의(Q&A에 질문만 있고 답변 0건).

### (3) DoraHacks 등록
- 등록과 제출이 **2026-12-03 13:00 KST 동시 마감**. 로그인 세션이 없어 대신 누를 수 없다.
- 필요한 것: 팀/개인 이름, 연락 이메일, 트랙(AI Agent Trading), (데모 참석 시) 참석 여부.

### (4) 서울 현장 발표 (12/5)
- 필수는 아니고 “우선 고려”. 참석 가능하면 데모팀 선정 확률이 올라간다.
- 온라인 전용 참가 시 사전 녹화 허용 여부는 주최 측 답변 없음.

### (5) 데모 영상
- 90초. `docs/50_DEMO.md`의 3분 흐름을 압축하고, 숫자는 `evidence/e2e_fork.md` 기준.
- 라이브 자금 시연 금지(포크 + 페이퍼로 충분). 합성 패널은 `SYNTHETIC` 라벨이 보이게.

---

## 2. 제출물 체크리스트 (DoraHacks “Submit Build”)

| 항목 | 준비물 | 상태 |
|---|---|---|
| 프로젝트명 | Sentinel HSK — accountable agent on HSKSwap | ✅ |
| 한 줄 소개 | “The agent decides. The chain enforces. And the agent's own claims are settled on-chain.” | ✅ |
| 트랙 | AI Agent Trading | ✅ |
| 상세 설명 | `README.md` + `docs/30_ARCHITECTURE.md` + `docs/12_deep_audit_and_upgrade.md` | ✅ |
| GitHub 저장소 | 공개 repo | ✅ push 완료 |
| 데모 영상 | 90초 | ⏳ 촬영 |
| 메인넷 컨트랙트 주소 | 배포 후 Blockscout 링크 | ⏳ 지갑·가스 |
| 데모 접속 주소(선택) | `python -m agent.server` 로컬 대시보드 | ✅ |
| 포크 실행 증거 | `evidence/e2e_fork.md` / `.json` | ✅ |

---

## 3. 90초 데모 스크립트 (숫자 갱신)

| 초 | 화면 | 말 |
|---|---|---|
| 0-12 | `python3 scripts/backtest.py` 상단 | “HSKSwap 0.3% 풀, 왕복 실측 65.7bp입니다. 오늘 관측된 최대 이탈은 130bp — 허들 146bp를 못 넘습니다. 기록 바 40개에서 거래 0건, 거부 20건. 이 시장에서 테이커의 정답은 대부분 ‘거래하지 않는다’입니다.” |
| 12-30 | 대시보드 Scenario lab: 덤프 프리셋 → 이탈 매수 전략 | “같은 게이트를 손으로 돌려봅니다. -400bp 덤프에서 기본 전략은 역추세로 거부하지만, 체인은 그 주문을 허용합니다. 별도로 사전등록한 이탈 매수 전략은 61 USDT를 주장 166bp로 진입합니다. 1회 400을 요청하면 체인이 `TRADE_TOO_LARGE`로 거부합니다.” |
| 30-55 | `python3 scripts/fork_demo.py` 실행 중 | “메인넷 포크입니다. 실제 라우터로 풀을 381bp 밀었고, 기본 전략의 거부가 온체인 트랜잭션으로 남았습니다. 이탈 매수 전략이 50.91 USDT를 샀고, 풀을 되돌린 뒤 청산에서 600bp가 정산됐습니다. ERC-8004 어댑터가 볼트의 신뢰도 36,144를 그대로 평판으로 올렸습니다.” |
| 55-72 | x402 카드 / `evidence/e2e_fork.md` | “프리미엄 리스크 피드는 x402로 0.001 USDT를 받습니다. 포크에서는 실제 `transferWithAuthorization` tx로 정산되고 nonce가 온체인에서 소진됩니다.” |
| 72-90 | `forge test` 요약 + 불변식 표 | “에이전트 키가 뚫려도 자금은 못 나갑니다. 인출 불가, 리스크 서명 없이는 실행 불가. 128,000회 무작위 호출로 5개 불변식을 검증했고, 장부와 볼트는 4/4로 일치합니다.” |

---

## 4. 심사 방어선 매핑

| 심사 관점 | 우리 대응 | 증거 위치 |
|---|---|---|
| 완성도 | 컨트랙트·에이전트·대시보드·포크 데모·CI·문서 일체 | 저장소 전체, `ci.yml` |
| 기술 성숙도 | EIP-712 이중서명, TWAP 자기검증, 사후 잔고 검증, fuzz+불변식+포크 테스트, Slither, 4개 표준 통합, **49 tx end-to-end** | `test/`, `evidence/`, `docs/12_*` |
| 혁신성 | 전략 사전등록 + 선언/실현 엣지 대조 + 거부 기록 + **LLM 숫자 검증** | `SentinelVault.sol`, `agent/narrator.py` |
| HSK 생태계 | HSKSwap 라우터·쿼터·팩토리·풀 오라클, 체인 177 | `docs/10_OSS_RESEARCH.md` §5 |
| 현장 발표 | 12/5 서울 참석 시 우선 고려 | 회기님 결정 |

---

## 5. 제출 전 마지막 점검 명령

```bash
cd competitions/hsk-chain-seoul-2026
export PATH="$HOME/.foundry/bin:$PATH"
forge test --no-match-path 'test/SentinelVault.fork.t.sol'   # 69
forge test                                                   # 74 (포크 포함, 네트워크)
python3 -m pytest tests/ -q                                  # 173
python3 scripts/backtest.py                                  # 거래 0 / 거부 20 / 합성 패널
python3 scripts/fork_demo.py                                 # 49 tx end-to-end (~2.5분)
python3 -m agent.server --port 8765                          # 대시보드
python3 scripts/deploy.py                                    # 배포 비용 추정(dry-run)
```
