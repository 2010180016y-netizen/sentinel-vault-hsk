"""Local dashboard + chat API. `python -m agent.server --port 8765` then open http://127.0.0.1:8765.

Deliberately dependency-free (stdlib http.server): the demo must run on a fresh laptop with
`pip install -r requirements.txt` and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .core import SentinelAgent
from .ledger import Ledger

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

_agent: SentinelAgent | None = None


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
        parsed = urlparse(self.path)
        agent = get_agent(self.state_path)
        if parsed.path == "/api/state":
            if agent.last_observation is None:
                agent.observe()
            return self._json(agent.snapshot())
        if parsed.path == "/api/step":
            rec = agent.step()
            agent.ledger.save(self.state_path)
            return self._json({"record": rec, "state": agent.snapshot()})
        if parsed.path == "/api/ask":
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            return self._json({"answer": agent.answer(q)})
        if parsed.path == "/health":
            return self._json({"ok": True})
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
