from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..metricspec import MetricSpec
from ..schema import Change, CompoundChange, Iteration, Target


@dataclass
class JudgeState:
    spec: MetricSpec
    params: dict[str, str]
    targets: list[Target]
    last: Iteration
    history: list[Iteration]
    tried: set[tuple[str, str]]
    residual_pattern: str
    tried_moves: set[tuple[tuple[str, str], ...]] = field(default_factory=set)
    max_move_size: int = 3
    blocked_options: set[tuple[str, str]] = field(default_factory=set)


class Judge(Protocol):
    name: str

    def propose(self, state: JudgeState, runner) -> Change | CompoundChange | None:
        """Return one change to try, or None if there is nothing defensible left."""
        ...
