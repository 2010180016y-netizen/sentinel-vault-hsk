"""elizaOS-shaped component types, in Python.

`elizaOS/eliza` (`packages/core/src/types/components.vibe`, `plugin.vibe`) defines an agent as a
runtime plus plugins made of **actions** (what it can do), **providers** (what it knows at decision
time) and **evaluators** (what it learns afterwards), with a memory store behind them. We only need
the shape, not the framework: this project's agent has one job and a judge must be able to read its
whole behaviour in one sitting.

What we kept, and why:

  * `Action.validate` — a handler that runs on the wrong message is worse than no handler, so an
    action must say whether it applies *before* it is chosen.
  * `Action.parameters` with a schema — the same discipline as `ActionProvider.get_actions()` in
    `coinbase/agentkit`: arguments are declared, typed and validated, not unpacked from prose.
  * `Provider.get(runtime, message, state)` — the state an action sees is composed from providers, so
    "what the agent knew when it decided" can be printed and audited.
  * `Evaluator` after the action — outcomes are written back to memory/registries after acting, not
    mixed into the action itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Memory:
    role: str  # "user" | "agent" | "system"
    text: str
    ts: int
    meta: dict = field(default_factory=dict)


@dataclass
class State:
    """Composed provider output for one message: text for a human, data for the code."""

    text: str = ""
    data: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"text": self.text, "data": self.data, "values": self.values}


@dataclass
class ActionResult:
    success: bool
    text: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    action: str = ""

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "success": self.success,
            "text": self.text,
            "data": self.data,
            "error": self.error,
        }


@dataclass(frozen=True)
class Parameter:
    name: str
    description: str = ""
    kind: str = "string"  # string | number | boolean
    required: bool = False
    pattern: str = ""
    enum: tuple[str, ...] = ()

    def validate(self, value: Any) -> bool:
        if self.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
        elif self.kind == "boolean":
            if not isinstance(value, bool):
                return False
        else:
            if not isinstance(value, str):
                return False
            if self.pattern and not re.fullmatch(self.pattern, value):
                return False
            if self.enum and value not in self.enum:
                return False
        return True


Validator = Callable[["AgentRuntime", Memory, State], bool]
Handler = Callable[["AgentRuntime", Memory, State, dict], ActionResult]


@dataclass
class Action:
    name: str
    description: str
    handler: Handler
    validate: Validator
    similes: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    priority: int = 0
    tags: tuple[str, ...] = ()

    def params_ok(self, params: dict) -> tuple[bool, str]:
        for p in self.parameters:
            if p.name not in params:
                if p.required:
                    return False, f"missing parameter {p.name}"
                continue
            if not p.validate(params[p.name]):
                return False, f"parameter {p.name} failed its {p.kind} schema"
        return True, ""


class Provider(Protocol):
    name: str
    description: str

    def get(self, runtime: "AgentRuntime", message: Memory, state: State) -> dict:
        """Return {"text": ..., "data": ...}; raising is not fatal (see AgentRuntime.compose)."""
        ...


@dataclass
class ProviderSpec:
    name: str
    description: str
    get: Callable[["AgentRuntime", Memory, State], dict]
    position: int = 0


EvaluatorHandler = Callable[["AgentRuntime", Memory, State, ActionResult], dict | None]


@dataclass
class Evaluator:
    name: str
    description: str
    handler: EvaluatorHandler


@dataclass
class Plugin:
    name: str
    description: str = ""
    actions: tuple[Action, ...] = ()
    providers: tuple[ProviderSpec, ...] = ()
    evaluators: tuple[Evaluator, ...] = ()
