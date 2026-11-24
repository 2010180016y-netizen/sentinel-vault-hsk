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

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

_agent: SentinelAgent | None = None
_lock = threading.Lock()
_last_observe = 0.0
OBSERVE_TTL_S = 5.0


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
                return self._json({"answer": agent.answer(unquote(q))})
        return super().do_GET()

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
