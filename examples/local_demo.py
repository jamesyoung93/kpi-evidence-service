from __future__ import annotations

import sys
from pathlib import Path

import kpi_evidence as ke
from kpi_evidence.cli import summary_text


def main(out_dir: str = "kpi_evidence_demo") -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    check = ke.ReportCheck.demo()
    print(check.documents[["file_name", "format", "status"]].to_string(index=False))

    result = check.run()
    print(summary_text(result))

    waiting = check.documents[check.documents["status"] == "AWAITING_CONFIRMATION"]
    for name in waiting["file_name"]:
        print(f"needs confirmation: {name}")
    if "kestrel-update.pptx" in set(waiting["file_name"]):
        check.confirm("kestrel-update.pptx", period="June 2026")
        result = check.run()
        print(summary_text(result))

    review = result.filter(flag="REVIEW") if hasattr(result, "filter") else result.comparisons
    columns = [c for c in ("report_file", "label_as_printed", "kpi_id", "period_label", "report_value",
                           "platform_value", "status_label", "reason") if c in review.columns]
    print(review[columns].head(15).to_string(index=False))
    print("HTML report:   ", result.save_html(str(out / "kpi_check.html")))
    print("Excel workbook:", result.save_excel(str(out / "kpi_check.xlsx")))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
