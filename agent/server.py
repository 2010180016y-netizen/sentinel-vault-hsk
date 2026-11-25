"""Local dashboard + chat API. `python -m agent.server --port 8765` then open http://127.0.0.1:8765.

Deliberately dependency-free (stdlib http.server): the demo must run on a fresh laptop with
`pip install -r requirements.txt` and nothing else.

The server is threaded, so every mutation of the shared agent is serialised behind one lock: two clicks on
"1회 판단" in the same second must not produce two trades against the same nonce.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import os

from .config import Settings
from .core import SentinelAgent
from .ledger import Ledger
from .premium import PremiumConfig, PremiumService
from .x402 import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER, encode_header
from .x402.service import PAYMENT_REQUIRED_HEADER

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

_agent: SentinelAgent | None = None
_lock = threading.Lock()
_last_observe = 0.0
OBSERVE_TTL_S = 5.0
_premium: PremiumService | None = None


def get_agent(state_path: str) -> SentinelAgent:
    global _agent
    if _agent is None:
        try:
            ledger = Ledger.load(state_path)
        except FileNotFoundError:
            ledger = Ledger()
            ledger.open_with(1_000.0)
        _agent = SentinelAgent(Settings.from_env(), ledger=ledger)
    return _agent


def get_premium(state_path: str) -> PremiumService | None:
    """The paid endpoint is only mounted when an operator configured a payee address."""
    global _premium
    if _premium is None:
        cfg = PremiumConfig.from_env()
        if cfg.requirements is None:
            return None
        _premium = PremiumService(get_agent(state_path), cfg, state_path=os.path.join(os.path.dirname(state_path) or ".", "x402_settlements.json"))
    return _premium


class Handler(SimpleHTTPRequestHandler):
    state_path = "state/ledger.json"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            with _lock:
                return self._json({"answer": agent.chat(unquote(q))})
        if parsed.path == "/api/premium/edge":
            return self._paid(agent, parsed)
        return super().do_GET()

    def _paid(self, agent: SentinelAgent, parsed) -> None:
        """x402 v2: 402 + PAYMENT-REQUIRED, then verify -> settle -> 200 + PAYMENT-RESPONSE."""
        with _lock:
            service = get_premium(self.state_path)
            if service is None:
                cfg = PremiumConfig.from_env()
                return self._json(
                    {"error": "x402 endpoint not configured", "hint": cfg.reason, "spec": "coinbase/x402 v2"},
                    503,
                )
            base = f"http://{self.headers.get('Host', '127.0.0.1')}"
            header = self.headers.get(PAYMENT_SIGNATURE_HEADER, "")
            if not header:
                body = service.challenge(base, "PAYMENT-SIGNATURE header is required")
                return self._x402(
                    body, 402, {PAYMENT_REQUIRED_HEADER: encode_header(body)}
                )
            settlement, data = service.settle(header, base)
            response_header = {PAYMENT_RESPONSE_HEADER: encode_header(settlement.as_dict())}
            if not settlement.success:
                body = service.challenge(base, settlement.error_reason)
                response_header[PAYMENT_REQUIRED_HEADER] = encode_header(body)
                return self._x402(body, 402, response_header)
            return self._x402(data or {}, 200, response_header)

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal quiet
        pass


def serve(port: int = 8765, state_path: str = "state/ledger.json") -> None:
    Handler.state_path = state_path
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Sentinel HSK dashboard on http://127.0.0.1:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--state", default="state/ledger.json")
    a = ap.parse_args()
    serve(a.port, a.state)
