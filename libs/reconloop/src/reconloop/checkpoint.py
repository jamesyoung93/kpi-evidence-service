from dataclasses import asdict
from hashlib import sha256
import json
from math import isfinite

from .schema import Change, CompoundChange, Iteration, ReconRecord, Residual
from .storage import local_path


def _encode(value):
    if isinstance(value, float) and not isfinite(value):
        return {"__nonfinite__": str(value)}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__nonfinite__"}:
            if value["__nonfinite__"] not in {"inf", "-inf"}:
                raise ValueError("invalid checkpoint number")
            return float(value["__nonfinite__"])
        return {k: _decode(v) for k, v in value.items()}
    return [_decode(v) for v in value] if isinstance(value, list) else value


def _digest(value):
    return sha256(json.dumps(_encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _binding(session):
    return _digest(dict(specs={k: asdict(v) for k, v in session.specs.items()},
                        targets=[asdict(t) for t in session.targets], policy=asdict(session.policy),
                        start=session.start, diagnostics=session.diagnostic_config))


def _pack(state):
    if state is None:
        return None
    return dict(report=state["report"], metric=state["spec"].name, params=state["params"], start=state["start"],
                history=[asdict(i) for i in state["history"]], tried=sorted(state["tried"]),
                tried_moves=sorted(state["tried_moves"]), lifecycle=state.get("lifecycle", []))


def _iteration(value):
    value = dict(value)
    change = value.get("change")
    if change:
        value["change"] = (CompoundChange([Change(**c) for c in change["changes"]], change["rationale"], change["proposed_by"])
                           if "changes" in change else Change(**change))
    return Iteration(**value)


def save_session(session, path, *, source_version):
    if not isinstance(source_version, str) or not source_version.strip():
        raise ValueError("checkpoint needs an immutable source_version")
    payload = dict(schema_version=1, source_version=source_version, binding=_binding(session), active=_pack(session._cur),
                   deferred=[dict(state=_pack(p["state"]), reason=p["reason"], depends_on=p["depends_on"])
                             for p in session._deferred.values()], records=[r.to_dict() for r in session.records],
                   spec_changes=session.spec_changes)
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(dict(payload=_encode(payload), sha256=_digest(payload)), stream, indent=2, allow_nan=False)
    return path


def restore_session(session, path, *, source_version):
    if session._cur or session._deferred or session.records or session.spec_changes:
        raise ValueError("restore requires an empty session")
    document = json.loads(local_path(path).read_text(encoding="utf-8"))
    payload = _decode(document["payload"])
    if _digest(payload) != document["sha256"] or payload.get("schema_version") != 1:
        raise ValueError("checkpoint integrity/schema check failed")
    if payload.get("source_version") != source_version:
        raise ValueError("checkpoint source_version changed")
    if payload["binding"] != _binding(session):
        raise ValueError("checkpoint specs, targets, starts, diagnostics or policy changed")
    seen = set()
    def claim(report, metric):
        pair = (report, metric)
        if pair not in session._pairs or pair in seen:
            raise ValueError("checkpoint contains unknown or duplicate pairs")
        seen.add(pair)
        return pair
    def unpack(state):
        if state is None:
            return None
        claim(state["report"], state["metric"])
        spec = session.specs[state["metric"]]
        ts = [t for t in session.targets if (t.report, t.metric) == (state["report"], state["metric"])]
        history = [_iteration(i) for i in state["history"]]
        if not history or [i.step for i in history] != list(range(len(history))):
            raise ValueError("checkpoint history is incomplete")
        params = spec.initial_choices(state["params"])
        if next(i for i in reversed(history) if i.accepted is not False).params != params:
            raise ValueError("checkpoint choices disagree with its history")
        return dict(report=state["report"], spec=spec, params=params, start=spec.initial_choices(state["start"]),
                    fit=[t for t in ts if t.split == "fit"], hold=[t for t in ts if t.split == "holdout"], history=history,
                    tried={tuple(v) for v in state["tried"]},
                    tried_moves={tuple(tuple(v) for v in move) for move in state["tried_moves"]}, lifecycle=state["lifecycle"])
    active, deferred, records = unpack(payload["active"]), {}, []
    for item in payload["deferred"]:
        state = unpack(item["state"])
        dependencies = [tuple(p) for p in item["depends_on"]]
        if any(p not in session._pairs for p in dependencies):
            raise ValueError("unknown checkpoint dependency")
        deferred[(state["report"], state["spec"].name)] = dict(state=state, reason=item["reason"], depends_on=dependencies)
    for raw in payload["records"]:
        claim(raw["report"], raw["metric"])
        raw = dict(raw, iterations=[_iteration(i) for i in raw["iterations"]], residual=Residual(**raw["residual"]))
        records.append(ReconRecord(**raw))
    session._cur, session._deferred, session.records = active, deferred, records
    session.spec_changes = payload["spec_changes"]
