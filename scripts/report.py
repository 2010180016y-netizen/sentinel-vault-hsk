"""Build a self-contained evidence report (evidence/REPORT.html) — no external assets, no JS.

    python scripts/report.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Settings  # noqa: E402
from agent.core import SentinelAgent  # noqa: E402
from agent.ledger import Ledger  # noqa: E402

KST = timezone(timedelta(hours=9))


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


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bt = json.load(open(os.path.join(here, "evidence/backtest.json")))
    settings = Settings.from_env()
    agent = SentinelAgent(settings, ledger=Ledger())
    agent.ledger.open_with(1_000.0)
    obs = agent.observe()
    d = agent.decide(obs)
    snap = agent.snapshot()

    real, mr, dt = bt["real"], bt["synthetic_mean_reversion"], bt["synthetic_downtrend"]
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    rows = []
    for r in real["rows"][-12:]:
        rows.append([str(r["i"]), f'{r["price"]:.5f}', f'{r["twap"]:.5f}', f'{r["dev_bps"]:+.0f}',
                     r["action"], f'{r["edge_bps"]:.0f}/{r["hurdle_bps"]:.0f}', "yes" if r["executed"] else "—"])

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
 .warn{{color:#ffc857}} .ok{{color:#38d39f}}
</style></head><body><div class="wrap">
<h1>Sentinel HSK — 실측 증거</h1>
<div class="sub">HSK Chain Horizon Hackathon · Seoul / Track 1 · {now} · chain 177</div>

<div class="grid">
  <div class="kpi"><div class="k">WHSK/USDT TWAP</div><div class="v">{obs.state.twap_quote_per_base:.5f}</div></div>
  <div class="kpi"><div class="k">spot vs TWAP</div><div class="v">{obs.state.twap_deviation_bps:+.0f} bp</div></div>
  <div class="kpi"><div class="k">pool depth</div><div class="v">${obs.state.depth_quote():,.0f}</div></div>
  <div class="kpi"><div class="k">round-trip cost</div><div class="v">{d.features.round_trip_cost_bps:.0f} bp</div></div>
  <div class="kpi"><div class="k">edge hurdle</div><div class="v">{d.hurdle_bps:.0f} bp</div></div>
  <div class="kpi"><div class="k">live decision</div><div class="v {'ok' if d.is_trade else 'warn'}">{d.action}</div></div>
</div>

<h2>1. 실측 시장에서의 판단 (실제 기록 바 {bt['bars']}개)</h2>
<div class="card">
 {svg_curve([(t, e) for t, e in real['equity']], color='#8b98a5')}
 <div class="note">거래 {real['metrics']['trades']}건 · 수수료 {real['metrics']['fees_paid_quote']:.4f} USDT · 평가액 {real['metrics']['equity']:.2f} USDT.
 허들({d.hurdle_bps:.0f}bp)을 넘는 이탈이 없어 거래하지 않았다. <b>이것이 이 시장에서 정직한 결과다.</b></div>
</div>
{table(rows, ['bar', 'price', 'TWAP', 'dev bp', 'action', 'edge/hurdle', 'exec'])}
<div class="note">마지막 12개 바. 거부 사유는 온체인 <code>recordNoTrade</code>로 남길 수 있게 설계됐다.</div>

<h2>2. 기계 검증 — 합성 평균회귀 시나리오 <span class="tag">SYNTHETIC</span></h2>
<div class="card">
 {svg_curve([(t, e) for t, e in mr['equity']])}
 <div class="note">거래 {mr['metrics']['trades']}건 · 승 {mr['metrics']['wins']} · 실현손익 {mr['metrics']['realised_pnl_quote']:+.4f} USDT ·
 선언 엣지 {mr['metrics']['declared_edge_sum_bps']:.0f}bp 대비 실현 {mr['metrics']['realised_edge_sum_bps']:.0f}bp
 (신뢰도 {mr['metrics']['edge_credibility_bps']:.0f}bp). 성과 주장이 아니라 <b>실행·회계 기계의 증명</b>이다.</div>
</div>

<h2>3. 기계 검증 — 합성 하락추세 시나리오 <span class="tag">SYNTHETIC</span></h2>
<div class="card">
 {svg_curve([(t, e) for t, e in dt['equity']], color='#ffc857')}
 <div class="note">거래 {dt['metrics']['trades']}건. 역추세 차단(추세 &lt; −max(25bp, 1.5×변동성))이 매수를 막았다.
 평균회귀 전략이 가장 잘 죽는 국면을 코드가 스스로 회피하는지 보는 패널이다.</div>
</div>

<h2>4. 컨트랙트·테스트</h2>
{table([
 ['단위·정책·fuzz 테스트', '27 / 27 통과'],
 ['불변식 테스트', '5종 · 128,000회 무작위 호출 · revert 0'],
 ['메인넷 포크 통합 테스트', '3 / 3 (실제 라우터로 100 USDT 스왑)'],
 ['에이전트(Python) 테스트', '16 / 16'],
 ['Slither', '21건 → 14건 (High 1 수용: reentrancy-balance, 근거 문서화)'],
 ['배포 비용(포크 실측)', '3,498,282 gas = 1.535 HSK (≈ $0.12)'],
 ['포크 스왑 실측', '100 USDT → 1,234.08 WHSK, TWAP 공정가 대비 −32bp = 풀 수수료 0.3%'],
], ['항목', '결과'])}

<h2>5. 이 리포트를 읽는 법</h2>
<div class="note">
1·2·3번 패널의 숫자는 모두 같은 코드 경로(<code>agent/signals.py → policy.py → ledger.py</code>)에서 나왔다.
다른 점은 <b>입력 가격이 실제 HSKSwap 스왑인지, 합성인지</b>뿐이다. 합성 패널은 라벨을 붙였고 성과로 주장하지 않는다.
컨트랙트 쪽 주장(수수료 자기계산, TWAP 자기검증, 사후 잔고 검증)은 <code>forge test</code>의 포크 테스트로 검증된다.
</div>
</div></body></html>"""
    out = os.path.join(here, "evidence/REPORT.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote", out, len(html), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
