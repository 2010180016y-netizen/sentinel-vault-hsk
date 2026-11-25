"""A small, deterministic elizaOS-style runtime.

elizaOS normally lets a language model pick the action. Here the decision that matters (buy/sell/
refuse) must be reproducible, so the runtime's planner is deterministic too: an action is eligible
when its `validate` says so, and among eligible actions the one with the most keyword hits from its
own similes wins, breaking ties by `priority` and then registration order. The model, when one is
configured, only narrates the result — it never chooses whether to trade.

The loop is the eliza one:

    compose state (providers) -> select action (validate + similes) -> validate params -> handle
    -> evaluate (write the outcome back) -> remember
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .types import Action, ActionResult, Evaluator, Memory, Plugin, ProviderSpec, State


@dataclass
class RuntimeResult:
    action: str
    result: ActionResult
    state: State
    params: dict = field(default_factory=dict)
    evaluations: list[dict] = field(default_factory=list)
    matched: bool = True

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "matched": self.matched,
            "result": self.result.as_dict(),
            "params": self.params,
            "evaluations": self.evaluations,
        }


class AgentRuntime:
    def __init__(self, *, agent_id: str = "sentinel-hsk", max_memory: int = 200) -> None:
        self.agent_id = agent_id
        self.actions: dict[str, Action] = {}
        self.providers: list[ProviderSpec] = []
        self.evaluators: list[Evaluator] = []
        self.memory: list[Memory] = []
        self.max_memory = max_memory
        self.plugins: list[str] = []

    # ------------------------------------------------------------------ registration

    def register_plugin(self, plugin: Plugin) -> None:
        if plugin.name in self.plugins:
            raise ValueError(f"plugin {plugin.name!r} is already registered")
        for action in plugin.actions:
            if action.name in self.actions:
                raise ValueError(f"action {action.name!r} is already registered")
            self.actions[action.name] = action
        self.providers.extend(plugin.providers)
        self.providers.sort(key=lambda p: p.position)
        self.evaluators.extend(plugin.evaluators)
        self.plugins.append(plugin.name)

    def get_action(self, name: str) -> Action | None:
        return self.actions.get(name)

    # ------------------------------------------------------------------ memory

    def remember(self, role: str, text: str, **meta) -> Memory:
        m = Memory(role=role, text=text, ts=int(time.time()), meta=meta)
        self.memory.append(m)
        if len(self.memory) > self.max_memory:
            self.memory = self.memory[-self.max_memory :]
        return m

    def recent(self, n: int = 5, role: str | None = None) -> list[Memory]:
        items = [m for m in self.memory if role is None or m.role == role]
        return items[-n:]

    # ------------------------------------------------------------------ state

    def compose(self, message: Memory | None = None, base: State | None = None) -> State:
        """One state built from every provider. A provider that throws contributes its error, not silence."""
        state = base or State()
        message = message or Memory(role="system", text="", ts=int(time.time()))
        chunks: list[str] = []
        for provider in self.providers:
            try:
                out = provider.get(self, message, state) or {}
            except Exception as exc:  # noqa: BLE001 - a broken provider must not darken the whole state
                out = {"text": f"[{provider.name} unavailable: {type(exc).__name__}]", "data": {"error": str(exc)}}
            text = str(out.get("text", "")).strip()
            if text:
                chunks.append(text)
            for key, value in (out.get("data") or {}).items():
                state.data[key] = value
            for key, value in (out.get("values") or {}).items():
                state.values[key] = value
        state.text = "\n".join(chunks)
        return state

    # ------------------------------------------------------------------ selection

    def candidates(self, message: Memory, state: State) -> list[Action]:
        return [a for a in self.actions.values() if _safe_validate(a, self, message, state)]

    def select(self, message: Memory, state: State) -> tuple[Action | None, int]:
        best: Action | None = None
        best_key = (0, 0, 0)
        for order, action in enumerate(self.candidates(message, state)):
            score = _keyword_score(action, message.text)
            key = (score, action.priority, -order)
            if score > 0 and key > best_key:
                best, best_key = action, key
        return best, best_key[0]

    # ------------------------------------------------------------------ process

    def process(
        self,
        text: str,
        *,
        forced_action: str | None = None,
        params: dict | None = None,
    ) -> RuntimeResult:
        message = Memory(role="user", text=text, ts=int(time.time()))
        self.memory.append(message)
        state = self.compose(message)

        if forced_action:
            action = self.get_action(forced_action)
            if action is None:
                return RuntimeResult(
                    forced_action,
                    ActionResult(False, error=f"unknown action {forced_action}", action=forced_action),
                    state,
                    matched=False,
                )
        else:
            action, score = self.select(message, state)
            if action is None:
                result = ActionResult(False, text="", error="no action matched", action="")
                self.remember("system", f"unrouted: {text}")
                return RuntimeResult("", result, state, matched=False)

        ok, why = action.params_ok(params or {})
        if not ok:
            result = ActionResult(False, error=why, action=action.name)
            self.remember("system", f"{action.name} rejected: {why}")
            return RuntimeResult(action.name, result, state, params or {})

        result = action.handler(self, message, state, params or {})
        result.action = action.name
        evaluations = []
        for evaluator in self.evaluators:
            try:
                out = evaluator.handler(self, message, state, result)
            except Exception as exc:  # noqa: BLE001 - an evaluator failure is recorded, not raised
                out = {"evaluator": evaluator.name, "error": f"{type(exc).__name__}: {exc}"}
            if out:
                evaluations.append(out)

        self.remember("agent", result.text or result.error, action=action.name)
        return RuntimeResult(action.name, result, state, params or {}, evaluations)

    # ------------------------------------------------------------------ introspection

    def catalog(self) -> list[dict]:
        return [
            {
                "name": a.name,
                "description": a.description,
                "similes": list(a.similes),
                "parameters": [
                    {"name": p.name, "kind": p.kind, "required": p.required} for p in a.parameters
                ],
                "priority": a.priority,
                "tags": list(a.tags),
            }
            for a in self.actions.values()
        ]

    def provider_names(self) -> list[str]:
        return [p.name for p in self.providers]


def _safe_validate(action: Action, runtime: AgentRuntime, message: Memory, state: State) -> bool:
    try:
        return bool(action.validate(runtime, message, state))
    except Exception:  # noqa: BLE001 - a validator that throws does not get the action
        return False


def _keyword_score(action: Action, text: str) -> int:
    lowered = text.lower()
    score = 0
    for word in action.similes:
        if word and word.lower() in lowered:
            score += 1
    return score
