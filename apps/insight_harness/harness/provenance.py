"""Provenance: every answer is a reproducible artifact, not a string.

An AnswerArtifact carries the parsed intent, the resolved metric/variant/source,
the exact code executed, the data version, a stable hash of the result, the
confidence tier, and structured caveats. Two runs of the same question must
produce the same result hash — the Reliability page enforces this.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, is_dataclass, asdict
from datetime import datetime, timezone

import pandas as pd

TIER_VERIFIED = "Verified"        # deterministic function of source data
TIER_DIRECTIONAL = "Directional"  # correlational / model-based, labeled
TIER_HYPOTHESIS = "Hypothesis"    # requires analyst validation
TIER_ABSTAINED = "Abstained"      # scoped refusal


def _stable_hash(obj) -> str:
    """Return a content hash for a JSON-safe, deterministic representation."""

    payload = json.dumps(
        _jsonable(obj), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass
class AnswerArtifact:
    question: str
    question_class: str
    tier: str
    engine: str
    headline: str = ""
    value: float | None = None
    table: pd.DataFrame | None = None
    chart_df: pd.DataFrame | None = None
    code: str = ""
    resolution: object = None            # semantic_layer.Resolution
    caveats: list = field(default_factory=list)
    divergence: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)
    data_version: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def result_hash(self) -> str:
        """Hash the complete deterministic result, not presentation/runtime state.

        ``created_at`` and model-translation metadata are deliberately excluded:
        rerunning the same governed computation must reproduce.  Conversely,
        changing a chart series, definition/source resolution, caveat, causal
        check, or data version must change the hash even when the headline's
        rounded number happens to stay the same.
        """

        stable_extras = {
            key: value for key, value in self.extras.items()
            if key not in {"translation", "intent", "analyst_reviewed"}
        }
        return _stable_hash({
            "question_class": self.question_class,
            "tier": self.tier,
            "engine": self.engine,
            "headline": self.headline,
            "value": self.value,
            "table": self.table,
            "chart": self.chart_df,
            "resolution": self.resolution,
            "caveats": self.caveats,
            "divergence": self.divergence,
            "extras": stable_extras,
            "data_version": self.data_version,
        })

    def stamp(self) -> dict:
        """The provenance stamp rendered under every answer."""
        return {
            "tier": self.tier,
            "engine": self.engine,
            "result_hash": self.result_hash,
            "data_version": self.data_version,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        """The full artifact as portable JSON: intent, resolution, code, hashes,
        caveats, divergence — everything needed to audit or reproduce the answer."""
        payload = {
            "question": self.question,
            "question_class": self.question_class,
            "tier": self.tier,
            "engine": self.engine,
            "headline": self.headline,
            "value": self.value,
            "table": _jsonable(self.table),
            "chart": _jsonable(self.chart_df),
            "code": self.code,
            "resolution": _jsonable(self.resolution),
            "caveats": list(self.caveats),
            "divergence": _jsonable(self.divergence),
            "extras": _jsonable(self.extras),
            "data_version": self.data_version,
            "created_at": self.created_at,
            "result_hash": self.result_hash,
        }
        return json.dumps(
            _jsonable(payload), indent=2, sort_keys=True,
            ensure_ascii=False, allow_nan=False,
        )


def _jsonable(obj):
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, pd.DataFrame):
        return _jsonable(obj.to_dict(orient="records"))
    if isinstance(obj, pd.Series):
        return _jsonable(obj.to_dict())
    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, set):
        return [_jsonable(v) for v in sorted(obj, key=str)]
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalars
        return _jsonable(obj.item())
    return str(obj)
