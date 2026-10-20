from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from kpi_evidence.eval import synth_reports as sr
from kpi_evidence.eval import truth
from kpi_evidence.ids import scope_key

EXPECTED_STATUS_COUNTS = {
    "VERIFIED": 44, "REPORT_ONLY": 8, "UNMAPPED_CAPTURED": 6, "VARIANCE_REVIEW": 2, "INCOMPATIBLE_DEFINITION": 7,
    "UNCERTAIN_EXTRACTION": 3, "REPORT_CONFLICT": 3, "MISSING_ACCESS": 2,
}
EXPECTED_PER_FILE = {sr.DECK1: 34, sr.DECK1_V2: 34, sr.EXEC_PDF: 4, sr.UPDATE_DECK: 2, sr.SCAN_PDF: 1}


def _labels(synth_set, file=None):
    return [lab for lab in synth_set["labels"]["labels"] if file is None or lab["file"] == file]


def test_generates_five_reports_with_labels_and_readme(synth_set):
    out: Path = synth_set["dir"]
    assert [p.name for p in synth_set["paths"]] == list(sr.REPORT_FILES)
    for p in synth_set["paths"]:
        assert p.exists() and 0 < p.stat().st_size < 2 * 1024 * 1024
    assert (out / sr.LABELS_FILE).exists() and (out / sr.README_FILE).exists()
    readme = (out / sr.README_FILE).read_text(encoding="utf-8")
    for name in sr.REPORT_FILES:
        assert name in readme
    for status in EXPECTED_STATUS_COUNTS:
        assert status in readme


def test_label_counts_per_status_and_file(synth_set):
    doc = synth_set["labels"]
    labels = _labels(synth_set)
    assert Counter(lab["expected_status"] for lab in labels) == EXPECTED_STATUS_COUNTS
    assert doc["status_counts"] == dict(sorted(EXPECTED_STATUS_COUNTS.items()))
    assert Counter(lab["file"] for lab in labels) == EXPECTED_PER_FILE
    assert len({lab["label_id"] for lab in labels}) == len(labels)
    for lab in labels:
        assert set(lab) >= {"file", "page", "locator_hint", "kpi_id", "period_end", "period_grain", "scope",
                            "report_value", "display_half_unit", "expected_status", "expected_reason", "note"}
        assert lab["scope_key"] == scope_key(lab["scope"])
    v2_only = Counter((lab["expected_status"]) for lab in _labels(synth_set, sr.DECK1_V2))
    assert v2_only["VARIANCE_REVIEW"] == 0 and v2_only["VERIFIED"] == 21


def test_document_metadata_section(synth_set, synth_set_hashes):
    docs = {d["file"]: d for d in synth_set["labels"]["documents"]}
    assert set(docs) == set(sr.REPORT_FILES)
    assert docs[sr.UPDATE_DECK]["needs_confirmation"] is True
    assert docs[sr.UPDATE_DECK]["initial_status"] == ["AWAITING_CONFIRMATION"]
    assert docs[sr.DECK1_V2]["supersedes"] == sr.DECK1 and docs[sr.DECK1]["final_status"] == "SUPERSEDED"
    assert docs[sr.DECK1]["period_end"] == "2026-05-31" and docs[sr.EXEC_PDF]["period_grain"] == "quarter"
    for name, d in docs.items():
        assert d["sha256"] == synth_set_hashes[name]
    cov = synth_set["labels"]["coverage"]
    assert cov["as_of"] == "2026-06-30" and cov["expected_period_end"] == {"monthly": "2026-05-31", "quarterly": "2026-03-31"}
    assert cov["expected"]["active_referrers"] == "NOT_FOUND" and cov["expected"]["exfactory_units"] == "NO_ACCESS"
    assert sum(1 for s in cov["expected"].values() if s == "FOUND") == 13


@pytest.fixture(scope="module")
def synth_set_hashes(synth_set):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in synth_set["paths"]}


def test_byte_stable_and_idempotent(synth_set, synth_set_hashes, tmp_path):
    again = sr.generate_all(tmp_path)
    for p in again:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == synth_set_hashes[p.name], p.name
    assert (tmp_path / sr.LABELS_FILE).read_bytes() == (synth_set["dir"] / sr.LABELS_FILE).read_bytes()
    assert (tmp_path / sr.README_FILE).read_bytes() == (synth_set["dir"] / sr.README_FILE).read_bytes()
    before = {p.name: p.stat().st_mtime_ns for p in again}
    sr.generate_all(tmp_path)
    assert {p.name: p.stat().st_mtime_ns for p in again} == before


def test_committed_sample_set_is_in_sync(synth_set, synth_set_hashes):
    from kpi_evidence.settings import demo_dir

    committed = demo_dir("reports")
    if not (committed / sr.LABELS_FILE).exists():
        pytest.skip("samples/reports not generated yet")
    assert (committed / sr.LABELS_FILE).read_bytes() == (synth_set["dir"] / sr.LABELS_FILE).read_bytes()
    for name, digest in synth_set_hashes.items():
        assert hashlib.sha256((committed / name).read_bytes()).hexdigest() == digest, name


def test_fixed_core_properties(synth_set):
    from pptx import Presentation

    for name in (sr.DECK1, sr.DECK1_V2, sr.UPDATE_DECK):
        cp = Presentation(str(synth_set["dir"] / name)).core_properties
        assert cp.author == "Synthetic" and cp.last_modified_by == "Synthetic"
        assert cp.created.year == 2026 and cp.created == cp.modified
    blob = (synth_set["dir"] / sr.DECK1).read_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert {i.date_time for i in z.infolist()} == {sr._ZIP_TIME}
        for name in z.namelist():
            if name.endswith(".xlsx"):
                with zipfile.ZipFile(io.BytesIO(z.read(name))) as inner:
                    assert b"2026-06-10T09:00:00Z" in inner.read("docProps/core.xml")
    assert "Kestrel" not in Presentation(str(synth_set["dir"] / sr.UPDATE_DECK)).core_properties.title


def test_values_come_from_truth(synth_set):
    labels = {(lab["file"], lab["page"], json.dumps(lab["locator_hint"], sort_keys=True)): lab for lab in _labels(synth_set)}

    def get(file, page, **hint):
        return labels[(file, page, json.dumps(hint, sort_keys=True))]

    may = "2026-05-31"
    chart = [lab for lab in _labels(synth_set, sr.DECK1) if lab["locator_hint"].get("object") == "TRx by month"]
    assert [lab["locator_hint"]["category"] for lab in chart] == ["Dec-25", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26"]
    for lab in chart:
        assert lab["report_value"] == pytest.approx(truth.trx(lab["period_end"]), rel=1e-11)
        assert lab["display_half_unit"] == 0
    trx = get(sr.DECK1, 3, object="Key performance indicators", row="TRx", col="May 2026")
    assert trx["display_text"] == "5.7K" and trx["report_value"] == 5700 and trx["display_half_unit"] == 50
    assert abs(trx["report_value"] - truth.trx(may)) <= 50
    samples = get(sr.DECK1, 3, object="Key performance indicators", row="Samples", col="May 2026")
    assert samples["report_value"] == round(truth.samples(may) * 1.06)
    assert get(sr.DECK1_V2, 3, object="Key performance indicators", row="Samples", col="May 2026")["report_value"] == truth.samples(may)
    writers = get(sr.DECK1, 3, object="Key performance indicators", row="Call attainment (writers only)", col="May 2026")
    assert writers["report_value"] == pytest.approx(truth.call_attainment(may, eligibility="writers_only"), abs=5e-4)
    il17 = get(sr.DECK1, 3, object="Key performance indicators", row="IL-17 class share", col="May 2026")
    assert il17["report_value"] == pytest.approx(truth.trx_share(may, basket="il17_class"), abs=5e-4)
    hcps = get(sr.DECK1, 5, object="Field activity", row="HCPs attended speaker programs", col="May 2026")
    assert hcps["report_value"] == truth.speaker_attendance(may, measure="distinct_hcps")
    proj = get(sr.DECK1, 5, object="Field activity", row="Referrals (projected)", col="May 2026")
    assert proj["report_value"] == truth.referrals_in(may, projection="projected")
    q2 = get(sr.EXEC_PDF, 1, object="Q2 2026 headline KPIs", row="TRx (retail panel)", col="Q2 2026")
    assert q2["report_value"] == round(truth.trx("2026-06-30", "quarter", source="panel"))
    nrx_pdf = get(sr.EXEC_PDF, 1, object="May 2026 snapshot", row="NRx", col="May 2026")
    nrx_deck = get(sr.DECK1, 3, object="Key performance indicators", row="NRx", col="May 2026")
    assert abs(nrx_pdf["report_value"] - nrx_deck["report_value"]) > nrx_pdf["display_half_unit"] + nrx_deck["display_half_unit"]
    tracker = get(sr.DECK1, 6, object="Brand tracker", row="Brand awareness (aided)", col="Q1 2026")
    assert (tracker["period_end"], tracker["period_grain"], tracker["report_value"]) == ("2026-03-31", "quarter", 0.62)
    image = get(sr.DECK1, 8, object="Picture", kind="image")
    assert image["kpi_id"] is None and image["report_value"] is None
    share = get(sr.UPDATE_DECK, 2, object="Headline numbers", row="Share", col="Latest month")
    assert share["report_value"] == 0.262 and share["period_end"] == "2026-06-30"
    for lab in _labels(synth_set, sr.DECK1) + _labels(synth_set, sr.DECK1_V2):
        if lab["locator_hint"].get("object") == "Call attainment by region":
            assert lab["report_value"] == pytest.approx(truth.call_attainment(may, scope=lab["scope"]), rel=1e-11)


def test_read_display():
    assert sr.read_display("5.7K") == (5700.0, 50.0)
    assert sr.read_display("9.1%") == (0.091, 0.0005)
    assert sr.read_display("-2.1%") == (-0.021, 0.0005)
    assert sr.read_display("+8%") == (0.08, 0.005)
    assert sr.read_display("1,113") == (1113.0, 0.5)


def _extract(path: Path) -> dict:
    from kpi_evidence.extract._vendor.pptx_extract import extract_pptx

    return extract_pptx(path.read_bytes(), source_name=path.name)


def _records(report, kind, slide=None):
    return [r for r in report["records"] if r["kind"] == kind and (slide is None or r["slide_number"] == slide)]


@pytest.mark.parametrize("name", [sr.DECK1, sr.DECK1_V2])
def test_monthly_review_is_machine_readable(synth_set, name):
    report = _extract(synth_set["dir"] / name)
    assert report["slide_count"] == 9
    charts = {r["data"]["chart_title"]: r for r in _records(report, "chart")}
    assert set(charts) == {"TRx by month", "NRx by region", "Call attainment by region"}
    trend = charts["TRx by month"]
    assert trend["slide_number"] == 2 and trend["status"] == "saved_values" and not trend["uncertainty_reasons"]
    assert [row["category"] for row in trend["data"]["rows"]] == ["Dec-25", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26"]
    assert trend["data"]["series"][0]["sources"]["val"]["formula"] == "Sheet1!$B$2:$B$7"
    for slide4 in ("NRx by region", "Call attainment by region"):
        rec = charts[slide4]
        assert rec["slide_number"] == 4 and rec["chart_count_on_slide"] == 2
        assert not {u["code"] for u in rec["uncertainty_reasons"]} & {"MULTIPLE_CHARTS_WITHOUT_LOCAL_LABEL", "CHART_POSITION_UNRESOLVED"}
    assert charts["Call attainment by region"]["data"]["series"][0]["sources"]["val"]["number_format"] == "0.0%"

    for lab in _labels(synth_set, name):
        hint = lab["locator_hint"]
        if hint.get("object") in charts:
            rows = charts[hint["object"]]["data"]["rows"]
            assert any(r["category"] == hint["category"] and r["value"] == lab["report_value"] for r in rows), hint
    sheets = _records(report, "embedded_worksheet")
    assert len(sheets) == 3
    linked = {ref["referencing_part"] for s in sheets for ref in s["data"]["source_references"]}
    assert linked == {c["source_part"] for c in charts.values()}
    trend_sheet = next(s for s in sheets if s["data"]["source_references"][0]["referencing_part"] == trend["source_part"])
    assert [r[1] for r in trend_sheet["data"]["rows"][1:]] == [r["value"] for r in trend["data"]["rows"]]

    tables = _records(report, "table")
    assert [t["slide_number"] for t in tables] == [3, 5, 6, 6, 7] and all(t["data"]["first_row_styled_as_header"] for t in tables)
    for lab in _labels(synth_set, name):
        hint = lab["locator_hint"]
        if "row" not in hint:
            continue
        (rows,) = [t["data"]["rows"] for t in tables
                   if t["slide_number"] == lab["page"] and t["data"]["rows"][0][0] in (hint["object"], "KPI", "Field activity")
                   and any(r[0] == hint["row"] for r in t["data"]["rows"][1:])]
        col = rows[0].index(hint["col"])
        row = next(r for r in rows[1:] if r[0] == hint["row"])
        assert row[col] == lab["display_text"], hint

    texts = {r["slide_number"]: r["data"]["text"] for r in _records(report, "slide_text")}
    assert "Data through May 2026" in texts[2]
    assert sr.SPEAKER_LIFT_TEXT in texts[9]
    images = _records(report, "image_reference")
    assert [r["slide_number"] for r in images] == [8]
    assert not [r for r in report["records"] if r["slide_number"] == 8 and r["kind"] in ("chart", "table")]
    w, h = report["slide_size_emu"]["width"], report["slide_size_emu"]["height"]
    box = images[0]["bounding_box"]
    assert box["width"] * box["height"] / (w * h) >= 0.15


def test_update_deck_has_no_brand_title_or_period(synth_set):
    report = _extract(synth_set["dir"] / sr.UPDATE_DECK)
    text = " ".join(r["data"]["text"] for r in _records(report, "slide_text"))
    assert "Kestrel" not in text and "2026" not in text
    table = _records(report, "table")[0]["data"]["rows"]
    assert table == [["Measure", "Latest month"], ["Share", "26.2%"], ["Calls", "1,163"]]


def test_pdfs_are_readable(synth_set):
    import pdfplumber

    with pdfplumber.open(synth_set["dir"] / sr.EXEC_PDF) as pdf:
        assert len(pdf.pages) == 1
        tables = pdf.pages[0].extract_tables()
        text = pdf.pages[0].extract_text()
    assert "Q2 2026" in text
    cells = {(row[0], row[1]) for t in tables for row in t}
    for lab in _labels(synth_set, sr.EXEC_PDF):
        assert (lab["locator_hint"]["row"], lab["display_text"]) in cells
    with pdfplumber.open(synth_set["dir"] / sr.SCAN_PDF) as pdf:
        page = pdf.pages[0]
        assert len(page.chars) == 0 and len(page.images) == 1


def test_package_exports_are_lazy():
    import kpi_evidence.eval as ev_pkg

    assert ev_pkg.generate_all is sr.generate_all
    assert ev_pkg.compute_metrics.__module__ == "kpi_evidence.eval.metrics"
    assert ev_pkg.run_evaluation.__module__ == "kpi_evidence.eval.evaluate"
    with pytest.raises(AttributeError):
        ev_pkg.not_a_name
