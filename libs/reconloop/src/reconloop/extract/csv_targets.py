from __future__ import annotations

import csv
from dataclasses import asdict, fields
from pathlib import Path

from ..schema import Target

REQUIRED = {"target_id", "report", "metric", "as_of", "value"}


def load_targets(path: str | Path) -> list[Target]:
    from ..storage import local_path
    path = local_path(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path}: no targets")
    out = []
    for r in rows:
        out.append(Target(target_id=r["target_id"], report=r["report"], metric=r["metric"], as_of=r["as_of"],
                          value=float(str(r["value"]).replace(",", "")), unit=r.get("unit", ""),
                          stated_rule=r.get("stated_rule", ""), source_file=r.get("source_file", ""),
                          page=r.get("page", ""), split=r.get("split", "fit") or "fit",
                          confidence=float(r.get("confidence") or 1.0),
                          extraction_method=r.get("extraction_method", ""), source_region=r.get("source_region", ""),
                          value_lower=float(r["value_lower"]) if r.get("value_lower") else None,
                          value_upper=float(r["value_upper"]) if r.get("value_upper") else None))
    return out


def write_targets(targets: list[Target], path: str | Path):
    from ..storage import local_path
    path = local_path(path)
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[f.name for f in fields(Target)])
        writer.writeheader()
        writer.writerows(asdict(t) for t in targets)
