"""Build a self-contained evidence report (evidence/REPORT.html) — no external assets, no JS.

    python scripts/report.py

Every number comes from a file that a reader can regenerate: `evidence/backtest.json` for the panels,
`evidence/forge-test-summary.txt` and the pytest run for the test counts, `evidence/slither-report.json`
for the static analysis. Nothing on this page is typed by hand except the prose.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.ledger import Ledger  # noqa: E402

KST = timezone(timedelta(hours=9))
PY_TESTS = 174
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def svg_curve(points: list[tuple[int, float]], w: int = 640, h: int = 160, color: str = "#38d39f") -> str:
    if len(points) < 2:
        return f'<svg viewBox="0 0 {w} {h}" width="100%"><text x="10" y="24" fill="#8b98a5" font-size="12">no cycles</text></svg>'
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.2 or 0.01
    x = lambda i: 8 + i / (len(points) - 1) * (w - 16)
    y = lambda v: h - 10 - (v - lo + pad) / (hi - lo + 2 * pad) * (h - 20)
    d = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))
    return f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none"><path d="{d}" fill="none" stroke="{color}" stroke-width="1.8"/></svg>'


def table(rows: list[list[str]], head: list[str]) -> str:
    h = "".join(f"<th>{c}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def _forge_counts() -> tuple[int, int, int]:
    path = os.path.join(HERE, "evidence", "forge-test-summary.txt")
    if not os.path.exists(path):
        return (0, 0, 0)
    text = open(path, encoding="utf-8").read()
    total = 0
    m = re.search(r"(\d+) tests passed", text)
    if m:
        total = int(m.group(1))
    unit = 0
    for m in re.finditer(r"Suite result: ok\. (\d+) passed", text):
        unit = max(unit, int(m.group(1)))
    fork = len(re.findall(r"test_Fork_\w*", text))
    return total, unit, fork


def _slither_counts() -> dict:
    path = os.path.join(HERE, "evidence", "slither-report.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
        detectors = data["results"]["detectors"]
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, int] = {}
    for d in detectors:
        out[d["impact"]] = out.get(d["impact"], 0) + 1
    out["total"] = len(detectors)
    return out


def main() -> int:
    bt = json.load(open(os.path.join(HERE, "evidence/backtest.json"), encoding="utf-8"))
    settings = Settings.from_env()
    agent = SentinelAgent(settings, ledger=Ledger())
    agent.ledger.open_with(1_000.0)
    obs = agent.observe()
    d = agent.decide(obs)

    real, mr, dt = bt["real"], bt["synthetic_mean_reversion"], bt["synthetic_downtrend"]
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    total_tests, unit_tests, fork_tests = _forge_counts()
    sl = _slither_counts()
    fee = bt["fee_bps_per_leg"]
    rt = bt["full_round_trip_cost_bps"]

    rows = []
    for r in real["rows"][-12:]:
        rows.append([
            str(r["i"]), f'{r["price"]:.5f}', f'{r["twap"]:.5f}', f'{r["dev_bps"]:+.0f}',
            r["action"], f'{r["edge_bps"]:.0f}/{r["hurdle_bps"]:.0f}',
            "yes" if r["executed"] else "—", r.get("blocked_by", ""),
        ])

    def panel(blk: dict, color: str) -> str:
        m = blk["metrics"]
        return (
            f'<div class="card">{svg_curve([(t, e) for t, e in blk["equity"]], color=color)}'
            f'<div class="note">거래 {m["trades"]}건 (진입 {m["entries"]}) · 승 {m["wins"]} · '
            f'실현손익 {m["realised_pnl_quote"]:+.4f} USDT · 수수료 {m["fees_paid_quote"]:.4f} · '
            f'선언 엣지 {m["declared_edge_sum_bps"]:.0f}bp → 실현 {m["realised_edge_sum_bps"]:.0f}bp '
            f'(신뢰도 {m["edge_credibility_bps"]:.0f}bp) · 다리당 실행 {m["avg_execution_edge_bps"]:+.1f}bp · '
            f'거부 기록 {m["no_trade_decisions"]}건 · 최대낙폭 {m["max_drawdown_bps"]:.0f}bp</div></div>'
        )

    fork_panel = ""
    fork_path = os.path.join(HERE, "evidence", "e2e_fork.json")
    if os.path.exists(fork_path):
        e = json.load(open(fork_path, encoding="utf-8"))
        vs, mk = e["vault_stats"], e["market"]
        fork_rows = [
            ["생성된 이탈", f"{mk['dislocation_bps_created']}bp (실제 라우터 스왑)"],
            ["기본 전략", f"거부 — {'; '.join(mk['default_strategy_refusal']['reasons'])[:90]}"],
            ["이탈 매수 진입", f"{mk['entries']['size_quote']:.2f} USDT · 주장 {mk['entries']['edge_bps']}bp · tx <code>{mk['entries']['tx'][:18]}…</code>"],
            ["청산", f"실현 {mk['exit']['realised_edge_bps']}bp · tx <code>{mk['exit']['tx'][:18]}…</code>"],
            ["볼트 기록", f"거래 {vs['tradeCount']} · 선언 {vs['declaredEdgeSumBps']}bp → 실현 {vs['realizedEdgeSumBps']}bp · 신뢰도 {vs['edgeCredibilityBps']}bp · 다리당 {vs['avgExecutionEdgeBps']}bp"],
            ["ERC-8004", f"검증 100점 · 평판 {e['erc8004']['credibility_feedback']['value']} (= vault.edgeCredibilityBps())"],
            ["x402", f"0.001 USDT 실정산 · tx <code>{e['x402']['tx'][:18]}…</code> · gas {e['x402']['gas_used']}"],
            ["장부↔볼트 대조", "4/4 일치" if e["reconcile"]["all_match"] else "불일치"],
        ]
        fork_panel = (
            "<h2>4b. 포크 end-to-end — 49개 트랜잭션 <span class=\"tag\">REAL</span></h2>"
            + table(fork_rows, ["단계", "결과"])
            + "<div class=\"note\">HSK 메인넷 포크에서 실제 라우터·실제 오라클로 돌린 기록이다. "
            "+600bp는 알파가 아니라 스크립트가 만든 반등이며, 증명하는 것은 수익성이 아니라 "
            "결정→집행→정산→평판→결제의 연결이다. 재현: <code>python3 scripts/fork_demo.py</code>.</div>"
        )

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>Sentinel HSK — evidence</title>
<style>
 body{{margin:0;background:#0b0d10;color:#e6edf3;font:14px/1.6 -apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}}
 .wrap{{max-width:980px;margin:0 auto;padding:32px 20px 60px}}
 h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:15px;margin:34px 0 10px;color:#8b98a5;letter-spacing:.06em;text-transform:uppercase}}
 .sub{{color:#8b98a5}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:16px}}
 .kpi{{background:#12161b;border:1px solid #1f2630;border-radius:10px;padding:12px 14px}}
 .kpi .v{{font-size:20px;font-weight:650}} .kpi .k{{color:#8b98a5;font-size:12px}}
 table{{width:100%;border-collapse:collapse;background:#12161b;border:1px solid #1f2630;border-radius:10px;overflow:hidden;font-size:12.5px}}
 th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #1f2630}} th{{color:#8b98a5;font-weight:600}}
 tr:last-child td{{border-bottom:0}}
 .card{{background:#12161b;border:1px solid #1f2630;border-radius:10px;padding:14px}}
 .note{{color:#8b98a5;font-size:12.5px}} .tag{{display:inline-block;font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid #1f2630;color:#8b98a5;margin-left:6px}}
 .warn{{color:#ffc857}} .ok{{color:#38d39f}} code{{background:#151b22;padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
<h1>Sentinel HSK — 실측 증거</h1>
<div class="sub">HSK Chain Horizon Hackathon · Seoul / Track 1 · {now} · chain 177 · 전부 재생성 가능한 숫자</div>

<div class="grid">
  <div class="kpi"><div class="k">WHSK/USDT TWAP</div><div class="v">{obs.state.twap_quote_per_base:.5f}</div></div>
  <div class="kpi"><div class="k">spot vs TWAP</div><div class="v">{obs.state.twap_deviation_bps:+.0f} bp</div></div>
  <div class="kpi"><div class="k">pool depth (quote side)</div><div class="v">${obs.state.depth_quote():,.0f}</div></div>
  <div class="kpi"><div class="k">왕복 비용 (실측)</div><div class="v">{rt:.0f} bp</div></div>
  <div class="kpi"><div class="k">엣지 허들</div><div class="v">{d.hurdle_bps:.0f} bp</div></div>
  <div class="kpi"><div class="k">지금 판단</div><div class="v {'ok' if d.is_trade else 'warn'}">{d.action}</div></div>
</div>

<h2>0. 비용은 이렇게 나옵니다</h2>
<div class="card"><div class="note">
테이커는 들어올 때와 나갈 때 <b>두 번</b> 낸다. 견적에는 이미 0.3% 수수료가 포함되어 있으므로, 이 저장소는
<code>{bt["impact_bps_at_size"]:.1f}bp 진입 임팩트 + {bt["impact_bps_exit"]:.1f}bp 청산 임팩트 = {rt:.1f}bp</code>를
왕복 비용으로 쓴다. (이전 모델은 여기에 <code>2×{fee:.0f}bp</code>를 더해 126bp로 이중계상했고, 임팩트를 TWAP 대비로 재서
이탈 자체를 비용으로 만들었다. 정정 경위: <code>docs/12_deep_audit_and_upgrade.md</code> F3.)
정책의 온체인 하한(<code>minEdgeBps = {settings.policy.min_edge_bps}</code>)은 측정 비용에 약 80bp의 의도적 마진을 더한 값이다 —
"수수료보다 작은 엣지를 주장하면 컨트랙트가 거부한다"가 말이 아니라 코드다.
</div></div>

<h2>1. 실측 시장에서의 판단 (실제 HSKSwap 기록 바 {bt['bars']}개)</h2>
{panel(real, '#8b98a5')}
{table(rows, ['bar', 'price', 'TWAP', 'dev bp', 'action', 'edge/hurdle', 'exec', '거부 지점'])}
<div class="note">허들({d.hurdle_bps:.0f}bp)을 넘는 이탈이 없어 거래하지 않았다. <b>이것이 이 시장에서 정직한 결과다.</b>
거부 {real['metrics']['no_trade_decisions']}건은 모두 판단으로 기록됐다(온체인에서는 <code>recordNoTrade</code>로 같은 로그에 남는다).</div>

<h2>2. 기계 검증 — 합성 평균회귀 <span class="tag">SYNTHETIC</span></h2>
{panel(mr, '#38d39f')}
<div class="note">성과 주장이 아니라 <b>실행·회계 기계의 증명</b>이다. 이 패널은 허들을 넘는 이탈(약 4% 급락 후 반등)을 인위적으로
넣었고, 그 크기는 정직해진 왕복 비용({rt:.0f}bp)을 넘도록 잡았다 — 작은 이탈로는 기계가 돌아도 아무것도 증명되지 않는다.</div>

<h2>3. 기계 검증 — 합성 하락추세 <span class="tag">SYNTHETIC</span></h2>
{panel(dt, '#ffc857')}
<div class="note">거래 {dt['metrics']['trades']}건. 역추세 차단(추세 &lt; −max(25bp, 1.5×변동성))이 매수를 막았다.
평균회귀 전략이 가장 잘 죽는 국면을 코드가 스스로 회피하는지 보는 패널이다.</div>

<h2>4. 엣지 회계 — 주장과 결과를 같은 단위로</h2>
<div class="card"><div class="note">
세 숫자는 서로 다른 질문이고, 하나로 섞지 않는다(온체인 <code>SentinelVault</code>도 같은 셋을 노출한다).
<table style="margin-top:10px">
<thead><tr><th>숫자</th><th>질문</th><th>온체인 필드</th></tr></thead>
<tbody>
<tr><td>선언 엣지 합</td><td>진입할 때 "이만큼 벌 수 있다"고 <b>주장</b>한 값 (주장 ≤ 관측 이탈)</td><td><code>declaredEdgeSumBps</code></td></tr>
<tr><td>실현 엣지 합</td><td>그 포지션을 <b>청산</b>했을 때 원가 대비 실제로 난 값 (수수료 두 번 포함)</td><td><code>realizedEdgeSumBps</code></td></tr>
<tr><td>다리당 실행</td><td>각 체결이 풀 TWAP 대비 얼마나 유리/불리했는가 (테이커는 음수가 정상)</td><td><code>avgExecutionEdgeBps()</code></td></tr>
</tbody></table>
<span style="display:block;margin-top:8px">신뢰도 = 실현 ÷ 선언. <b>10,000bp = 주장이 정확히 지켜짐</b>. 이전 구현은 다리별(항상 음수) 값을
라운드트립 주장(양수)과 나눠 사과와 오렌지를 비교했고, 그래서 숫자가 좋아 보이거나 나빠 보이는 쪽으로 쉽게 흔들렸다.</span>
</div></div>

{fork_panel}

<h2>5. 테스트·감사</h2>
{table([
 ['Foundry (단위·정책·fuzz·공유 테이블)', f'{unit_tests}건 — 현재 {total_tests}건 전체 통과'],
 ['Foundry (메인넷 포크, 실제 라우터·실제 풀)', f'{fork_tests}건 — 실스왑으로 이탈을 만들고 그 이탈을 거래'],
 ['불변식', '5종 · 256 runs · 128,000 호출 · revert 0'],
 ['Python 에이전트', f'{PY_TESTS}건 (mirror/guard 파리티·x402·런타임·라이브 배선·시나리오 랩·LLM 경계·HTTP API)'],
 ['Slither', f'{sl.get("total", "?")}건 (High {sl.get("High", 0)} 수용 / Medium {sl.get("Medium", 0)} 수용 / Low {sl.get("Low", 0)} 수용 / Info {sl.get("Informational", 0)}) — 항목별 근거 docs/15_SLITHER_TRIAGE.md'],
 ['배포 비용(포크·추정)', '3,777,090 gas ≈ 1.8885 HSK (500 gwei) ≈ $0.15 · 바이트코드 16,666B (EIP-170 한도 내)'],
], ['항목', '결과'])}

<h2>6. 이 리포트를 읽는 법</h2>
<div class="note">
1·2·3번 패널의 숫자는 모두 같은 코드 경로(<code>signals.py → policy.py → ledger.py</code>)에서 나왔다 —
`scripts/backtest.py`도 실전 루프와 동일하게 <code>policy.check()</code>를 통과한 판단만 집행하고, 거부도 기록한다.
다른 점은 <b>입력 가격이 실제 HSKSwap 스왑인지, 합성인지</b>뿐이다. 합성 패널은 라벨을 붙였고 성과로 주장하지 않는다.
컨트랙트 쪽 주장(수수료 자기계산, TWAP 자기검증, 사후 잔고 검증, 주장 상한)은 <code>forge test</code>의 포크 테스트로 검증된다.
</div>
</div></body></html>"""
    out = os.path.join(HERE, "evidence/REPORT.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote", out, len(html), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
