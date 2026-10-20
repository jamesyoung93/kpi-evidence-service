from __future__ import annotations

import io
from datetime import date, datetime

import pytest
from interp_builders import DIMS, codes, make_doc, make_value, table_cell

from kpi_evidence.contracts import Document, DocumentFormat, DocumentStatus, ExtractionMethod
from kpi_evidence.ids import sha256_bytes
from kpi_evidence.standardize import interpret


def run(values, *, registry=None, dims=DIMS):
    return interpret(make_doc(), values, [], registry, None, dims)


def one(value, **kw):
    [obs] = run([value], **kw)
    return obs


def sheet_cell(raw: str, row: int, labels: list[str]):
    return make_value(raw, raw_number=float(raw.replace(",", "")), method=ExtractionMethod.WORKBOOK,
                      number_format="General", locator={"sheet": "Regional KPIs", "cell": f"C{row + 1}", "row": row,
                                                        "col": 2},
                      object_kind="worksheet", number_source="stored", row_label=labels[0],
                      row_labels=labels if len(labels) > 1 else None, col_header="May 2026", object_title="Regional KPIs")


def test_region_in_a_second_label_column_sets_scope(registry):
    obs = one(table_cell("1,265", row_label="TRx", row_labels=["TRx", "West"], col_header="May 2026"),
              registry=registry)
    assert obs.label == "TRx" and obs.scope == {"region": "West"}


def test_region_in_the_first_label_column_does_not_become_the_label(registry):
    obs = one(table_cell("1,265", row_label="West", row_labels=["West", "TRx"], col_header="May 2026",
                         object_title="Regional detail"), registry=registry)
    assert obs.label == "TRx" and obs.scope == {"region": "West"}


def test_unknown_region_in_a_label_column_is_not_the_brand_total(registry):
    obs = one(table_cell("1,265", row_label="TRx", row_labels=["TRx", "West"], col_header="May 2026"),
              registry=registry, dims=None)
    assert obs.label == "TRx" and obs.scope == {} and "SCOPE_UNKNOWN" in codes(obs)


def test_merged_region_cell_carries_down_to_the_rows_below(registry):
    obs = run([sheet_cell("1265", 1, ["West", "TRx"]), sheet_cell("180", 2, ["NBRx"]),
               sheet_cell("1520", 3, ["East", "TRx"]), sheet_cell("200", 4, ["NBRx"])], registry=registry)
    assert [(o.label, o.scope) for o in obs] == [("TRx", {"region": "West"}), ("NBRx", {"region": "West"}),
                                                 ("TRx", {"region": "East"}), ("NBRx", {"region": "East"})]


def test_merged_metric_cell_carries_down_to_the_rows_below(registry):
    obs = run([sheet_cell("1265", 1, ["TRx", "West"]), sheet_cell("1520", 2, ["East"])], registry=registry)
    assert [(o.label, o.scope) for o in obs] == [("TRx", {"region": "West"}), ("TRx", {"region": "East"})]


def test_row_without_its_trailing_label_is_not_filled(registry):
    obs = run([sheet_cell("1265", 1, ["TRx", "West"]), sheet_cell("410", 2, ["NRx"])], registry=registry)
    assert (obs[1].label, obs[1].scope) == ("NRx", {})


def test_rows_that_are_not_adjacent_are_not_filled(registry):
    obs = run([sheet_cell("1265", 1, ["West", "TRx"]), sheet_cell("180", 3, ["NBRx"])], registry=registry)
    assert obs[1].scope == {}


def test_merged_region_rows_in_a_real_workbook(registry):
    openpyxl = pytest.importorskip("openpyxl")
    from kpi_evidence.extract import extract_document

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Regional KPIs"
    for row in (["Region", "Metric", "May 2026"], ["West", "TRx", 1265], [None, "NBRx", 180], ["East", "TRx", 1520],
                [None, "NBRx", 200]):
        ws.append(row)
    ws.merge_cells("A2:A3")
    ws.merge_cells("A4:A5")
    buf = io.BytesIO()
    wb.save(buf)
    blob = buf.getvalue()
    now = datetime(2026, 6, 5, 12)
    doc = Document(doc_id=sha256_bytes(blob), file_name="Kestrel_Regional_2026-05.xlsx", file_path="/tmp/r.xlsx",
                   size_bytes=len(blob), format=DocumentFormat.XLSX, status=DocumentStatus.READY, registered_at=now,
                   updated_at=now, brand="KESTREL", report_type="monthly_brand_review", period_label="May 2026",
                   period_start=date(2026, 5, 1), period_end=date(2026, 5, 31), publication_date=date(2026, 6, 1))
    out = extract_document(None, doc, blob)
    observations = interpret(doc, out.values, out.pages, registry, None, DIMS)
    found = sorted((o.value, o.label, o.scope.get("region")) for o in observations if o.value is not None)
    assert found == [(180.0, "NBRx", "West"), (200.0, "NBRx", "East"), (1265.0, "TRx", "West"), (1520.0, "TRx", "East")]
