"""Local dashboard + chat API. `python -m agent.server --port 8765` then open http://127.0.0.1:8765.

Deliberately dependency-free (stdlib http.server): the demo must run on a fresh laptop with
`pip install -r requirements.txt` and nothing else.

The server is threaded, so every mutation of the shared agent is serialised behind one lock: two clicks on
"1회 판단" in the same second must not produce two trades against the same nonce.

Endpoints (all read-only unless noted):

    GET /api/state          dashboard snapshot (market, book, policy, guards, runtime, on-chain view)
    GET /api/step           one observe -> decide -> guard -> execute/refuse cycle (writes the ledger)
    GET /api/ask?q=...      the conversational surface (may adjust agent-side controls)
    GET /api/trace          the last decision with every policy gate it was judged by
    GET /api/simulate       scenario lab: ?dev=-400&size=120&dd=20&kill=0&pos=0&mode=dislocation
    GET /api/presets        the named scenarios the dashboard shows as buttons
    GET /api/catalog        the runtime's actions/providers (what the agent can be asked to do)
    GET /api/x402/info      payee, price, settlement mode (never the facilitator key)
    GET /api/x402/demo      pays this server's own 402 endpoint and returns the receipt (needs a demo key)
    GET /api/erc8004        the ERC-8004 registration file and, if configured, the live reputation
    GET /api/reconcile      off-chain ledger vs on-chain vault.stats() — the difference, line by line
    GET /api/narrate        the LLM narration of the last decision (guarded; falls back to the sentence)
    GET /api/premium/edge   x402-protected: 402 until paid
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .config import Addresses, Settings
from .core import SentinelAgent
from .lab import Scenario, run as run_scenario
from .ledger import Ledger
from .premium import PremiumConfig, PremiumService
from .x402 import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER, Budget, X402Client, encode_header
from .x402.service import PAYMENT_REQUIRED_HEADER

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

_agent: SentinelAgent | None = None
_lock = threading.Lock()
_init_lock = threading.Lock()
_last_observe = 0.0
OBSERVE_TTL_S = 5.0
_premium: PremiumService | None = None
_premium_error = ""
_last_demo_payment = 0.0
DEMO_COOLDOWN_S = 10.0


def get_agent(state_path: str) -> SentinelAgent:
    global _agent
    if _agent is not None:
        return _agent
    with _init_lock:
        if _agent is not None:
            return _agent
        try:
            ledger = Ledger.load(state_path)
        except FileNotFoundError:
            ledger = Ledger()
            ledger.open_with(1_000.0)
        _agent = SentinelAgent(Settings.from_env(), ledger=ledger)
    return _agent


def get_premium(state_path: str) -> PremiumService | None:
    """The paid endpoint is only mounted when an operator configured a payee address."""
    global _premium, _premium_error
    if _premium is None:
        cfg = PremiumConfig.from_env()
        if cfg.requirements is None:
            _premium_error = cfg.reason
            return None
        try:
            _premium = PremiumService(
                get_agent(state_path),
                cfg,
                state_path=os.path.join(os.path.dirname(state_path) or ".", "x402_settlements.json"),
            )
        except ValueError as exc:  # e.g. chain settlement without a facilitator key
            _premium_error = str(exc)
            return None
    return _premium


def _query(parsed) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}


def _f(query: dict, key: str, default: float) -> float:
    try:
        return float(query.get(key, default))
    except (TypeError, ValueError):
        return default


class Handler(SimpleHTTPRequestHandler):
    state_path = "state/ledger.json"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _x402(self, payload: dict, code: int, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        global _last_observe
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._json({"ok": True})
        agent = get_agent(self.state_path)
        q = _query(parsed)

        if parsed.path == "/api/state":
            with _lock:
                if agent.last_observation is None or time.time() - _last_observe > OBSERVE_TTL_S:
                    agent.observe()
                    _last_observe = time.time()
                return self._json(agent.snapshot())
        if parsed.path == "/api/step":
            with _lock:
                rec = agent.step()
                _last_observe = time.time()
                agent.ledger.save(self.state_path)
                return self._json({"record": rec, "state": agent.snapshot()})
        if parsed.path == "/api/ask":
            text = unquote((parse_qs(parsed.query).get("q") or [""])[0])
            with _lock:
                return self._json({"answer": agent.chat(text)})
        if parsed.path == "/api/trace":
            with _lock:
                return self._json(self._trace(agent))
        if parsed.path == "/api/simulate":
            with _lock:
                return self._json(self._simulate(agent, q))
        if parsed.path == "/api/presets":
            with _lock:
                return self._json({"presets": self._presets()})
        if parsed.path == "/api/catalog":
            with _lock:
                return self._json(
                    {
                        "plugin": agent.runtime.plugins,
                        "actions": agent.runtime.catalog(),
                        "providers": agent.runtime.provider_names(),
                        "messages": [m.meta for m in agent.runtime.memory[-10:]],
                    }
                )
        if parsed.path == "/api/x402/info":
            with _lock:
                return self._json(self._x402_info())
        if parsed.path == "/api/x402/demo":
            # Deliberately *not* under the agent lock: the demo pays this same server, whose
            # /api/premium/edge handler takes the lock. Holding it here would deadlock the process.
            return self._json(self._x402_demo(agent))
        if parsed.path == "/api/erc8004":
            with _lock:
                return self._json(self._erc8004(agent))
        if parsed.path == "/api/reconcile":
            with _lock:
                return self._json(self._reconcile(agent))
        if parsed.path == "/api/narrate":
            with _lock:
                return self._json(agent.narrate_last())
        if parsed.path == "/api/premium/edge":
            return self._paid(agent, parsed)
        return super().do_GET()

    # ------------------------------------------------------------------ dashboard helpers

    def _trace(self, agent: SentinelAgent) -> dict:
        record = agent.ledger.decisions[-1] if agent.ledger.decisions else {}
        policy = record.get("policy") or {}
        guards = record.get("guards") or {}
        counterfactual = False
        if not policy.get("gates") and agent.last_observation is not None:
            # The strategy held, so no order was judged. Show what the chain *would* say about an order
            # of the requested size at this moment — the trace is never an empty table.
            from .lab import Scenario, run as run_lab

            obs = agent.last_observation
            m = agent.market
            pos = agent.ledger.positions.get(m.asset)
            metrics = agent.ledger.metrics()
            scenario = Scenario(
                dev_bps=obs.state.twap_deviation_bps,
                size_quote=agent.controls.size_quote,
                price=obs.state.twap_quote_per_base,
                has_position=bool(pos and pos.qty > 0),
                position_quote=(pos.qty / 10**m.decimals * obs.state.twap_quote_per_base) if pos else 0.0,
                equity=metrics["equity"] or 1_000.0,
                high_water=agent.ledger.high_water or 1_000.0,
                depth_quote=obs.state.depth_quote(),
                strategy_mode=agent.strategy.mode,
            )
            res = run_lab(scenario, policy=agent.policy, strategy=agent.strategy)
            chosen = res["order"] or res["requested"]
            policy = chosen["policy"]
            counterfactual = True
        return {
            "record": record,
            "counterfactual": counterfactual,
            "gates": policy.get("gates") or [],
            "policy": {k: policy.get(k) for k in ("ok", "reason", "detail", "notes")},
            "guards": guards,
            "decision": {
                "action": record.get("action"),
                "edge_bps": record.get("edge_bps"),
                "hurdle_bps": record.get("hurdle_bps"),
                "conviction": record.get("conviction"),
                "size_quote": record.get("size_quote"),
                "dislocation_bps": record.get("dislocation_bps"),
                "reasons": record.get("reasons", []),
            },
            "observation": record.get("observation") or (agent.last_observation.as_dict() if agent.last_observation else {}),
        }

    def _simulate(self, agent: SentinelAgent, q: dict) -> dict:
        price = _f(q, "price", 0.0) or (
            agent.last_observation.state.twap_quote_per_base if agent.last_observation else 0.0808
        )
        scenario = Scenario(
            dev_bps=_f(q, "dev", 0.0),
            size_quote=_f(q, "size", agent.controls.size_quote),
            price=price or 0.0808,
            has_position=_f(q, "pos", 0.0) > 0,
            position_quote=_f(q, "pos", 0.0),
            equity=_f(q, "equity", 1_000.0),
            high_water=_f(q, "high", 1_000.0),
            daily_spent=_f(q, "spent", 0.0),
            last_entry_age_s=None if "age" not in q else _f(q, "age", 0.0),
            kill_switch=_f(q, "kill", 0.0) > 0,
            mode=q.get("mode", "confirm") if q.get("mode") in ("confirm", "autonomous") else "confirm",
            strategy_mode=q.get("strategy", "trend_filtered")
            if q.get("strategy") in ("trend_filtered", "dislocation")
            else "trend_filtered",
        )
        return run_scenario(scenario, policy=agent.policy, strategy=agent.strategy)

    def _presets(self) -> dict:
        from .lab import presets

        return presets()

    def _x402_info(self) -> dict:
        service = get_premium(self.state_path)
        if service is None:
            return {"configured": False, "reason": _premium_error or "no payee configured"}
        return {"configured": True, **service.config.public(), "settlement_active": service.settlement_mode}

    def _x402_demo(self, agent: SentinelAgent) -> dict:
        """Pay this server's own endpoint with the agent's wallet — the 402 dance, end to end."""
        global _last_demo_payment
        service = get_premium(self.state_path)
        if service is None:
            return {"ok": False, "error": _premium_error or "x402 endpoint not configured"}
        if time.time() - _last_demo_payment < DEMO_COOLDOWN_S:
            return {"ok": False, "error": f"demo payment cooling down ({DEMO_COOLDOWN_S:.0f}s)"}
        key = os.environ.get("SENTINEL_X402_DEMO_KEY", "").strip() or os.environ.get(
            "SENTINEL_STRATEGY_KEY", ""
        ).strip()
        if not key:
            return {"ok": False, "error": "set SENTINEL_X402_DEMO_KEY (any funded key) to run the demo payment"}
        base = f"http://{self.headers.get('Host', '127.0.0.1')}"
        cap = int(os.environ.get("SENTINEL_X402_BUDGET", "10000"))
        client = X402Client(key, chain_id=agent.settings.chain.chain_id, budget=Budget(cap=cap))
        try:
            res = client.pay_and_fetch(base + "/api/premium/edge", max_amount=int(service.requirements.amount))
        except Exception as exc:  # noqa: BLE001 - reported to the dashboard
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _last_demo_payment = time.time()
        return {
            "ok": res.ok,
            "status": res.status,
            "paid_atomic": res.paid,
            "settlement": res.settlement.as_dict() if res.settlement else None,
            "settlement_mode": service.settlement_mode,
            "premium": res.data,
            "error": res.error,
            "payer": client.payer,
        }

    def _erc8004(self, agent: SentinelAgent) -> dict:
        from . import registry8004 as r8

        s = Settings.from_env()
        agent_id = int(os.environ.get("SENTINEL_8004_AGENT_ID", "0") or 0)
        identity = os.environ.get("SENTINEL_8004_IDENTITY", "")
        reputation = os.environ.get("SENTINEL_8004_REPUTATION", "")
        validation = os.environ.get("SENTINEL_8004_VALIDATION", "")
        adapter = os.environ.get("SENTINEL_8004_ADAPTER", "")
        out: dict = {
            "configured": bool(identity and agent_id),
            "agent_id": agent_id,
            "registries": {"identity": identity, "reputation": reputation, "validation": validation},
            "adapter": adapter,
            "vault": s.vault,
        }
        if identity and agent_id:
            out["registration"] = r8.registration_file(
                agent_id=agent_id,
                identity_registry=identity,
                chain_id=s.chain.chain_id,
                name="Sentinel HSK",
                description="Policy-bound HSKSwap trading agent: strategy pre-registration, claimed-vs-realised edge, recorded refusals",
                services=[
                    {"name": "web", "endpoint": "http://127.0.0.1:8765/"},
                    {"name": "x402", "endpoint": "http://127.0.0.1:8765/api/premium/edge", "version": "2"},
                ],
            )
        # The adapter's own address is the ERC-8004 "client" that posts feedback about the vault's record.
        if identity and reputation and adapter and agent_id:
            try:
                reader = r8.RegistryReader(agent.rpc, identity, reputation, validation)
                out["reviews"] = {
                    "edge_credibility": reader.summary(agent_id, [adapter], "edgeCredibility", "").as_dict(),
                    "execution_cost": reader.summary(agent_id, [adapter], "executionCost", "").as_dict(),
                }
                out["owner"] = reader.owner_of(agent_id)
            except Exception as exc:  # noqa: BLE001 - an RPC hiccup must not break the dashboard
                out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def _reconcile(self, agent: SentinelAgent) -> dict:
        """The agent's own book against the vault's. A difference is a finding, not a rounding error."""
        if not agent.vault_view:
            return {"ok": False, "error": "SENTINEL_VAULT not set"}
        view = agent.vault_view.summary()
        if not view.get("ok"):
            return view
        m = agent.ledger.metrics()
        pairs = {
            "trades": (m["trades"], view["trade_count"]),
            "declared_edge_sum_bps": (m["declared_edge_sum_bps"], view["declared_edge_sum_bps"]),
            "realised_edge_sum_bps": (m["realised_edge_sum_bps"], view["realised_edge_sum_bps"]),
            "realised_pnl_quote": (m["realised_pnl_quote"], view["realised_pnl_quote"] / 1e6),
            "avg_execution_edge_bps": (m["avg_execution_edge_bps"], view["avg_execution_edge_bps"]),
        }
        rows = [
            {"field": k, "ledger": a, "vault": b, "delta": round(a - b, 6), "match": abs(a - b) <= max(1e-6, abs(b) * 0.001)}
            for k, (a, b) in pairs.items()
        ]
        return {
            "ok": True,
            "vault": view,
            "rows": rows,
            "all_match": all(r["match"] for r in rows),
            "note": "a mismatch means one of the two books is lying; the vault's record is the one that cannot be edited",
        }

    # ------------------------------------------------------------------ x402 merchant

    def _paid(self, agent: SentinelAgent, parsed) -> None:
        """x402 v2: 402 + PAYMENT-REQUIRED, then verify -> settle -> 200 + PAYMENT-RESPONSE."""
        with _lock:
            service = get_premium(self.state_path)
            if service is None:
                cfg = PremiumConfig.from_env()
                return self._json(
                    {"error": "x402 endpoint not configured", "hint": cfg.reason or _premium_error, "spec": "coinbase/x402 v2"},
                    503,
                )
            base = f"http://{self.headers.get('Host', '127.0.0.1')}"
            header = self.headers.get(PAYMENT_SIGNATURE_HEADER, "")
            if not header:
                body = service.challenge(base, "PAYMENT-SIGNATURE header is required")
                return self._x402(body, 402, {PAYMENT_REQUIRED_HEADER: encode_header(body)})
            settlement, data = service.settle(header, base)
            response_header = {PAYMENT_RESPONSE_HEADER: encode_header(settlement.as_dict())}
            if not settlement.success:
                body = service.challenge(base, settlement.error_reason)
                response_header[PAYMENT_REQUIRED_HEADER] = encode_header(body)
                return self._x402(body, 402, response_header)
            return self._x402(data or {}, 200, response_header)

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal quiet
        pass


def serve(port: int = 8765, state_path: str = "state/ledger.json", host: str = "127.0.0.1") -> None:
    Handler.state_path = state_path
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Sentinel HSK dashboard on http://{host}:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--state", default="state/ledger.json")
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    serve(a.port, a.state, a.host)
