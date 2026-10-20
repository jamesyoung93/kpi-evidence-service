from __future__ import annotations

import json
from pathlib import Path
from .record import record_filename


def score_dir(out_dir: str | Path, answers_path: str | Path) -> dict:
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    per, hits, total, cases_ok = {}, 0, 0, 0
    for key, ans in answers.items():
        report, metric = key.split("/")
        f = Path(out_dir) / record_filename(report, metric)
        total += len(ans["params"])
        if not f.exists():
            per[key] = "missing"
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        got = rec["final_params"]
        right = {k: got.get(k) == v for k, v in ans["params"].items()}
        hits += sum(right.values())
        expected = ans.get("residual", "none")
        residual_ok = (rec["status"] == "reconciled" if expected == "none" else
                       rec["status"] == "explained_residual" and
                       expected in rec["residual"]["likely_causes"])
        ok = all(right.values()) and residual_ok
        cases_ok += ok
        per[key] = {"params_correct": right, "status": rec["status"], "expected_residual": ans.get("residual", "none"), "case_correct": ok}
    return {"param_accuracy": hits / total if total else 0.0, "cases_correct": f"{cases_ok}/{len(answers)}", "detail": per}
