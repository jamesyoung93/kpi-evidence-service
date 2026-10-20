from __future__ import annotations

from ..compare import evaluate, score
from ..schema import Change, CompoundChange, change_key
from .base import JudgeState


class SearchJudge:
    """Search single edits and explicitly declared compound moves on fit data only."""

    name = "search"

    def __init__(self, min_gain: float = 1e-4):
        self.min_gain = min_gain

    def propose(self, state: JudgeState, runner) -> Change | CompoundChange | None:
        candidates = []
        for pname, p in state.spec.params.items():
            for opt in p.options:
                if opt == state.params[pname] or (pname, opt) in state.tried | state.blocked_options:
                    continue
                candidates.append(Change(pname, state.params[pname], opt, p.hypothesis,
                                         "Declared alternative evaluated on fit targets.", self.name))
        for move in state.spec.moves.values():
            edits = [Change(k, state.params[k], v, state.spec.params[k].hypothesis,
                            move.rationale, self.name) for k, v in move.options.items() if state.params[k] != v]
            if len(edits) < 2 or len(edits) > state.max_move_size:
                continue
            candidate = CompoundChange(edits, move.rationale, self.name)
            if any((c.param, c.new) in state.blocked_options for c in edits):
                continue
            if change_key(candidate) not in state.tried_moves:
                candidates.append(candidate)
        best, best_gain = None, self.min_gain
        for candidate in candidates:
            trial = state.params | dict(change_key(candidate))
            try:
                variance = score(evaluate(runner, state.spec, trial, state.targets))
            except Exception:
                continue
            gain = state.last.fit_variance - variance
            if gain > best_gain:
                best, best_gain = candidate, gain
        return best
