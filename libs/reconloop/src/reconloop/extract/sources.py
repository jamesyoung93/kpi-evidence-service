from pathlib import Path

from .csv_targets import load_targets
from .pdf import ExtractionResult, extract_pdf


def load_sources(config: dict, base: Path, *, evidence_dir=None) -> ExtractionResult:
    from ..storage import local_path, load_bundle
    result = ExtractionResult()
    sources = list(config.get("sources", []))
    if config.get("file"):
        sources.insert(0, {"type": "csv", "file": config["file"]})
    if not sources:
        raise ValueError("targets needs file or at least one [[targets.sources]] entry")
    for source in sources:
        path = local_path(source["file"], base)
        kind = source.get("type", "csv")
        if kind == "csv":
            result.targets.extend(load_targets(path))
        elif kind == "bundle":
            result.targets.extend(load_bundle(path))
        elif kind in {"pdf", "bar_chart"}:
            options = {k: v for k, v in source.items() if k not in {"type", "file"}}
            if kind == "bar_chart":
                from .charts import extract_bars
                extracted = extract_bars(path, **options, strict=False, evidence_dir=evidence_dir)
            else:
                extracted = extract_pdf(path, **options, strict=False, evidence_dir=evidence_dir)
            result.targets.extend(extracted.targets)
            result.evidence.extend(extracted.evidence)
            result.issues.extend(extracted.issues)
        else:
            raise ValueError(f"unknown target source type: {kind}")
    keys = [(t.report, t.metric, t.target_id) for t in result.targets]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate target IDs within a report/metric across source files")
    return result
