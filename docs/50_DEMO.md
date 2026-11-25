# 3분 데모 — 심사위원이 직접 만지는 순서

준비(한 번):

```bash
export PATH="$HOME/.foundry/bin:$PATH"
python3 -m pip install -r requirements.txt
forge build
python3 -m agent.server --port 8765      # → http://127.0.0.1:8765
```

> 라이브 RPC(HSK 메인넷)를 읽습니다. 지갑·가스·로그인은 필요 없습니다.
> 체인 쓰기(포크 실거래)는 별도 명령 `python3 scripts/fork_demo.py`에서만 일어납니다.

---

## 0:00 — “이 봇은 오늘 거래하지 않습니다”

첫 화면의 **Markets / Edge discipline / Risk policy** 카드.

- WHSK/USDT 가격과 **TWAP 대비 이탈 0bp**, 풀 깊이 ≈ $419–425k.
- `round-trip cost` **66bp** — 다리당 33bp(0.3% 수수료 포함, 실측).
- `edge hurdle` **146bp** — 정책의 온체인 하한. 비용 66bp + 80bp 마진.
- 실제 기록 바 백테스트(터미널): `python3 scripts/backtest.py` → **거래 0 / 거부 20**.

말할 것: “수수료가 이탈보다 큰 시장에서 테이커의 정답은 대부분 ‘거래하지 않는다’입니다.
이 프로젝트는 그 사실을 숫자로 보여주고, **거부도 결정으로 기록**합니다.”

## 0:30 — 게이트 추적: “왜 안 샀는지”가 표에 있다

**1회 판단** 버튼 → **Decision trace** 카드.

- `ORACLE 통과 → EDGE_BELOW_HURDLE 거부` (한도 `≥146bp`, 실제 `0bp`).
- 각 행은 `gate · rule · limit · actual · 결과`. **첫 실패에서 멈춥니다**(EVM이 멈추는 곳과 같습니다).
- 청산이면 `EDGE_BELOW_HURDLE / DAILY_LIMIT / COOLDOWN`이 **n/a**로 표시됩니다 — 예외도 결정이라는 뜻입니다.

## 1:00 — 시나리오 랩: 심사위원이 숫자를 바꾼다

**Scenario lab**에서 프리셋을 차례로:

| 프리셋 | 보이는 것 |
|---|---|
| 조용한 시장 | 전략 HOLD, 요청조차 `EDGE_BELOW_HURDLE` |
| −400bp 덤프 · 추세 필터 | 전략 HOLD(역추세 차단) — **그런데 체인은 그 주문을 허용**한다(전략이 더 엄격) |
| −400bp 덤프 · 이탈 매수 전략 | **BUY 61.2 USDT**, 엣지 180bp > 허들 146bp, 정책·가드 통과 |
| 낙폭 20% · 매수 시도 | 전략은 사려 하지만 **체인 거부: DRAWDOWN** |
| 1회 400 USDT 요청 | 전략은 200으로 줄여 서명, **요청 400은 TRADE_TOO_LARGE** |
| 킬 스위치 ON | 정책 통과, **가드 KILL_SWITCH** |

탭 두 개: **전략이 낼 주문** / **요청한 규모라면**. “대화로 바뀌는 것”과 “체인이 허용하는 것”이
다른 질문임을 한 화면에서 보여줍니다.

## 1:40 — LLM은 숫자를 만들지 않는다

**Edge discipline → LLM 설명** (설정 시) 또는 `/api/narrate`.

- 모델이 사실에 없는 숫자를 쓰면 응답이 **폐기**되고 결정론적 문장이 나옵니다(`rejected: true`).
- LLM에게 매매를 제안시키면 `/api/narrate`가 아니라 `agent.narrator.evaluate_proposal`이
  **정책·가드로 판정**합니다: 500 USDT → `TRADE_TOO_LARGE`, 신호 없는 매수 → `EDGE_BELOW_HURDLE`.
- 테스트: `tests/test_narrator.py` 15개(999bp 폐기, 500 USDT 거부, flat SELL 거부, 결정 불변).

## 2:00 — x402: 402 → 서명 → 정산

**x402 payment** 카드의 **0.001 USDT로 프리미엄 데이터 구매**.

- 기본은 `paper` 정산: EIP-3009 서명을 실제로 검증하고 `paper:` 영수증.
- `SENTINEL_X402_SETTLEMENT=chain`(로컬 포크)에서는 **실제 `transferWithAuthorization` 트랜잭션**과
  해시가 표시됩니다. 포크 실행 기록: tx `0x23d8…`, gas 65,840, nonce 온체인 소진 확인.

## 2:20 — ERC-8004: 평판은 에이전트의 말이 아니라 볼트의 기록

**ERC-8004 identity** 카드(포크 실행 후) — agent #1, `edgeCredibility` 36,144, `executionCost` 297.
어댑터는 `vault.stats()`만 읽습니다. 검증 응답 100은 “거래 2건 + 오라클 정상 + 신뢰도 ≥ 임계”일 때만.

## 2:40 — 온체인 대조 + 포크 실거래

**On-chain reconciliation** 카드: 거래 수·선언/실현 엣지·평균 실행을 **장부 vs 볼트**로 나란히.
4/4 일치.

터미널:

```bash
python3 scripts/fork_demo.py
```

HSK 메인넷 **포크**에서 49개 트랜잭션:

1. WHSK 6,000 × 17 매도 → **381bp 이탈**(실제 라우터·실제 오라클)
2. 기본 전략 **거부 + recordNoTrade tx**
3. 이탈 매수 전략 **50.91 USDT 진입**, 주장 166bp
4. 고래 매수로 **+275bp 반등** → **청산, 실현 601.0bp**
5. ERC-8004 검증 100·평판 36,144
6. x402 0.001 USDT 실정산
7. 장부↔볼트 4/4 일치

정직하게: **+601bp는 알파가 아니라 우리가 만든 반등**입니다. 이 실행이 증명하는 것은 수익성이 아니라
“결정→집행→정산→평판→결제”가 같은 코드로 끊김 없이 돈다는 것입니다.

---

## 발표에서 쓰는 한 문장

> “다른 팀은 에이전트에게 상자를 줍니다. 우리는 상자에 더해 **에이전트의 주장을 온체인에서 정산**합니다 —
> 주장은 시장이 보여준 이탈을 넘을 수 없고, 실현과 같은 단위로 대조되며, 거부도 같은 로그에 남습니다.
> 그리고 그 구조가 실제 HSKSwap에서 49개 트랜잭션으로 돈다는 것을 포크로 보여드렸습니다.”
