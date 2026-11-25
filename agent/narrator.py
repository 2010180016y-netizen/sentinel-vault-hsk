"""An LLM that may talk, but may not move a number.

Track 1 is an *AI agent* track, so this module adds the language model — under the same discipline the
project applies to the agent's own edge claim. Three rules, each enforced in code and tested:

  1. **The LLM never decides.** `signals.decide` chooses BUY/SELL/HOLD and produces every number. The
     narrator receives those numbers as facts and writes prose. There is no code path from narration to
     an intent.
  2. **A number it invents is not shown.** `GuardedNarrator` scans the reply for numeric tokens and
     accepts it only if every one of them appears in the facts it was given. A reply that invents
     "+999bp" is discarded and the deterministic sentence is used instead, with `rejected=True` so the
     discard is visible rather than silent.
  3. **A trade it proposes is judged like any other claim.** `parse_proposal` turns an LLM's suggestion
     into a `Proposal`, and `evaluate_proposal` runs that order through the same policy mirror and swap
     guards a signed intent faces. The classic failure mode — a model that "decides" to buy 500 USDT or
     to trade on no signal — is refused with the on-chain limit quoted back.

The transport is OpenAI-compatible (`POST {base}/chat/completions`), which covers OpenAI, vLLM, Ollama,
LM Studio and the local mock server the tests use. With no endpoint configured the narrator is `Null`
and the deterministic sentence is the answer: the product never depends on an API key being present.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import BPS, RiskPolicy, Strategy
from .lab import Scenario, judge_order, plan_order  # one gate implementation, reused

SYSTEM_PROMPT = (
    "You are a narration layer for a trading agent. You do not decide anything and you may not invent "
    "numbers: use only the numbers in FACTS. Write at most three sentences, in Korean, plainly. Explain "
    "what the agent did or refused and which rule decided it. Never recommend a trade, never claim profit "
    "that is not in FACTS, and never mention a number that is not in FACTS."
)

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
# Protocol names and identifiers contain digits but are not quantities; strip them before the scan.
_STRIP = (
    re.compile(r"0x[0-9a-fA-F]{6,}"),
    re.compile(r"\bERC[- ]?8004\b", re.IGNORECASE),
    re.compile(r"\bx402\b", re.IGNORECASE),
    re.compile(r"\bEIP[- ]?712\b", re.IGNORECASE),
    re.compile(r"\beip155:\d+\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2,5}/[A-Z]{2,5}\b"),  # WHSK/USDT
)


class NameGuardError(ValueError):
    pass


# ---------------------------------------------------------------------------- narrators


class Narrator(Protocol):  # pragma: no cover - typing only
    name: str

    def complete(self, system: str, user: str) -> str: ...


@dataclass
class NullNarrator:
    """No endpoint configured. The deterministic sentence is the answer."""

    name: str = "null"

    def complete(self, system: str, user: str) -> str:
        return ""


@dataclass
class ScriptedNarrator:
    """A fixed reply, for tests and for the offline demo (`SENTINEL_LLM_SCRIPT`)."""

    reply: str
    name: str = "scripted"

    def complete(self, system: str, user: str) -> str:
        return self.reply


@dataclass
class HttpNarrator:
    """OpenAI-compatible chat completion. No retries: a slow model must not stall the dashboard."""

    base_url: str
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout_s: float = 10.0
    name: str = "http"

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def complete(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0.2,
                "max_tokens": 220,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        req = urllib.request.Request(self._endpoint(), data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                blob = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise RuntimeError(f"narrator unavailable: {exc}") from exc
        try:
            return str(blob["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("narrator reply has no choices[0].message.content") from exc


def build_narrator(env: dict | None = None) -> Narrator:
    env = env or os.environ
    script = env.get("SENTINEL_LLM_SCRIPT", "").strip()
    if script:
        return ScriptedNarrator(script)
    base = env.get("SENTINEL_LLM_URL", "").strip()
    if not base:
        return NullNarrator()
    return HttpNarrator(
        base_url=base,
        model=env.get("SENTINEL_LLM_MODEL", "gpt-4o-mini"),
        api_key=env.get("SENTINEL_LLM_API_KEY", ""),
        timeout_s=float(env.get("SENTINEL_LLM_TIMEOUT", "10")),
    )


# ---------------------------------------------------------------------------- number discipline


def _numbers_in(text: str) -> list[str]:
    cleaned = text
    for pattern in _STRIP:
        cleaned = pattern.sub(" ", cleaned)
    out = []
    for match in _NUMBER.findall(cleaned):
        token = match.replace(",", "").lstrip("+")
        if token in {"", "-", "."}:
            continue
        out.append(token)
    return out


def _flatten_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        return
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_numbers(v, out)


def _allowed_tokens(facts: Any) -> set[str]:
    values: set[float] = set()
    _flatten_numbers(facts, values)
    allowed: set[str] = set()
    for v in values:
        for d in (0, 1, 2, 3, 4, 6):
            allowed.add(f"{round(v, d):.{d}f}".rstrip("0").rstrip("."))
            allowed.add(f"{v:g}")
        allowed.add(str(int(v)) if float(v).is_integer() else str(v))
        allowed.add(f"{abs(v):.2f}")
    allowed.discard("")
    allowed.discard("-")
    return allowed


def numbers_are_grounded(text: str, facts: Any) -> tuple[bool, list[str]]:
    allowed = _allowed_tokens(facts)
    stray = [t for t in _numbers_in(text) if t not in allowed]
    return (not stray), stray


@dataclass
class GuardedNarrator:
    """Wrap any narrator: reject invented numbers and fall back to the deterministic sentence."""

    inner: Narrator
    name: str = "guarded"

    @property
    def configured(self) -> bool:
        return not isinstance(self.inner, NullNarrator)

    def narrate(self, facts: dict, fallback: str) -> dict:
        if isinstance(self.inner, NullNarrator):
            return {"text": fallback, "source": "deterministic", "rejected": False, "reason": "no narrator configured"}
        try:
            text = self.inner.complete(SYSTEM_PROMPT, json.dumps(facts, ensure_ascii=False, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - a broken model is reported, not raised at the user
            return {
                "text": fallback,
                "source": "deterministic",
                "rejected": True,
                "reason": f"narrator failed: {type(exc).__name__}: {exc}",
            }
        if not text:
            return {"text": fallback, "source": "deterministic", "rejected": False, "reason": "empty reply"}
        ok, stray = numbers_are_grounded(text, facts)
        if not ok:
            return {
                "text": fallback,
                "source": "deterministic",
                "rejected": True,
                "reason": "the reply invented numbers not present in the facts: " + ", ".join(stray[:5]),
            }
        return {"text": text, "source": "llm", "rejected": False, "reason": ""}


# ---------------------------------------------------------------------------- facts


def facts_from_record(record: dict) -> dict:
    """Only the numbers the agent already decided. The narrator gets no new authority."""
    keep = {
        "action": record.get("action"),
        "executed": record.get("executed"),
        "edge_bps": record.get("edge_bps"),
        "hurdle_bps": record.get("hurdle_bps"),
        "dislocation_bps": record.get("dislocation_bps"),
        "conviction": record.get("conviction"),
        "size_quote": record.get("size_quote"),
        "leg_edge_bps": record.get("leg_edge_bps"),
        "realised_edge_bps": record.get("realised_edge_bps"),
        "tx": record.get("tx", ""),
    }
    for key in ("features", "observation", "policy", "guards"):
        if isinstance(record.get(key), dict):
            keep[key] = record[key]
    return {k: v for k, v in keep.items() if v not in (None, {}, "")}


# ---------------------------------------------------------------------------- proposals


@dataclass(frozen=True)
class Proposal:
    action: str
    size_quote: float
    confidence: float = 0.0
    rationale: str = ""
    source: str = "llm"

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "size_quote": self.size_quote,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "source": self.source,
        }


_ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD"}


def parse_proposal(text: str) -> tuple[Proposal | None, str]:
    """Extract a strict JSON proposal. A model that rambles is not a proposal."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "no JSON object in the reply"
    try:
        blob = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(blob, dict):
        return None, "proposal must be an object"
    action = str(blob.get("action", "")).upper()
    if action not in _ALLOWED_ACTIONS:
        return None, f"action must be one of {sorted(_ALLOWED_ACTIONS)}"
    try:
        size = float(blob.get("size_quote", 0))
        confidence = float(blob.get("confidence", 0))
    except (TypeError, ValueError):
        return None, "size_quote/confidence must be numbers"
    if size < 0 or size > 1e9:
        return None, "size_quote out of range"
    confidence = max(0.0, min(1.0, confidence))
    return Proposal(action, size, confidence, str(blob.get("rationale", ""))[:400]), ""


def evaluate_proposal(
    proposal: Proposal,
    *,
    dev_bps: float,
    has_position: bool,
    position_quote: float,
    price: float,
    policy: RiskPolicy | None = None,
    strategy: Strategy | None = None,
    depth_quote: float | None = None,
) -> dict:
    """Judge an LLM's (or a human's) suggested order with the same gates a signed intent faces."""
    policy = policy or RiskPolicy()
    strategy = strategy or Strategy()
    scenario = Scenario(
        dev_bps=dev_bps,
        size_quote=proposal.size_quote,
        has_position=has_position,
        position_quote=position_quote,
        price=price,
        strategy_mode=strategy.mode,
        depth_quote=depth_quote if depth_quote is not None else 419_423.0,
    ).resolved()

    if proposal.action == "HOLD":
        return {
            "proposal": proposal.as_dict(),
            "allowed": True,
            "blocked_by": "",
            "evaluation": None,
            "note": "HOLD asks for nothing, so nothing is refused",
        }

    is_buy = proposal.action == "BUY"
    notional = proposal.size_quote if is_buy else max(position_quote, 0.0)
    claim = min(abs(dev_bps) * 0.45, abs(dev_bps)) if is_buy else 0.0
    plan = plan_order(scenario, is_buy=is_buy, notional_quote=notional, declared_edge_bps=claim)
    judged = judge_order(scenario, plan, policy, is_buy=is_buy)
    # A sell with no position is refused by the vault (`NothingToSell`), and the mirror says so too.
    note = ""
    if not is_buy and not has_position:
        judged = {
            **judged,
            "allowed": False,
            "blocked_by": "policy:NOTHING_TO_SELL",
        }
        note = "the book is flat: there is nothing to sell"
    return {
        "proposal": proposal.as_dict(),
        "allowed": judged["allowed"],
        "blocked_by": judged["blocked_by"],
        "evaluation": judged,
        "note": note,
    }
