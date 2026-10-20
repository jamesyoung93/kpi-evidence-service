from __future__ import annotations

import io
import shutil
from datetime import date

import pytest

from check_standins import REPORTS
from kpi_evidence.check import ReportCheck, period_fields
from kpi_evidence.contracts import Document

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06.pptx"
PDF = REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf"
REPORT_FILES = sorted(p.name for p in REPORTS.iterdir() if p.suffix.lower() in (".pptx", ".pdf"))


@pytest.fixture()
def check() -> ReportCheck:
    return ReportCheck()


def test_folder_is_read_recursively_and_skips_non_report_files(check, tmp_path):
    nested = tmp_path / "reports" / "2026" / "06"
    nested.mkdir(parents=True)
    shutil.copy(DECK, nested / DECK.name)
    shutil.copy(PDF, tmp_path / "reports" / PDF.name)
    (tmp_path / "reports" / "notes.txt").write_text("not a report", encoding="utf-8")
    (tmp_path / "reports" / "~$lock.pptx").write_bytes(b"lock")
    out = check.add_reports(tmp_path / "reports")
    assert sorted(out["file_name"]) == sorted([DECK.name, PDF.name])
    assert set(out["note"]) == {"added"}
    assert all(src.endswith(name) for src, name in zip(out["source"], out["file_name"]))


def test_bundled_samples_folder_excludes_readme_and_labels(check):
    out = check.add_reports(REPORTS)
    assert sorted(out["file_name"]) == REPORT_FILES
    assert list(check.documents["file_name"]) == sorted(REPORT_FILES, key=str.lower)


def test_glob_and_list_of_sources(check):
    out = check.add_reports([str(REPORTS / "Kestrel_Monthly_*.pptx"), PDF])
    assert sorted(out["file_name"]) == sorted(["Kestrel_Monthly_Brand_Review_2026-06.pptx",
                                               "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", PDF.name])


def test_bytes_pair_dict_and_file_object(check):
    out = check.add_reports(("deck.pptx", DECK.read_bytes()))
    out2 = check.add_reports({"summary.pdf": PDF.read_bytes()})
    out3 = check.add_reports(("again.pdf", io.BytesIO(PDF.read_bytes())))
    assert list(out["file_name"]) == ["deck.pptx"] and list(out["source"]) == ["<bytes>"]
    assert list(out2["format"]) == ["PDF"]
    assert list(out3["note"]) == ["already added"]
    doc = check.store.get(Document, doc_id=out.iloc[0]["doc_id"])
    assert str(check.settings.local_dir) in doc.file_path


def test_volume_like_absolute_path_is_registered_in_place(check, tmp_path):
    volume = tmp_path / "Volumes" / "main" / "brand" / "reports" / "2026"
    volume.mkdir(parents=True)
    shutil.copy(DECK, volume / DECK.name)
    out = check.add_reports(volume.as_posix() + "/")
    doc = check.store.get(Document, doc_id=out.iloc[0]["doc_id"])
    assert doc.file_path == str((volume / DECK.name).resolve())


def test_dbfs_prefix_is_read_through_the_fuse_path(check):
    from kpi_evidence.check import _local_path

    assert _local_path("dbfs:/Volumes/main/brand/reports/a.pdf").as_posix() == "/Volumes/main/brand/reports/a.pdf"
    assert _local_path("DBFS:/Volumes/x").as_posix() == "/Volumes/x"
    with pytest.raises(FileNotFoundError, match="dbfs:/Volumes/nope/missing.pdf"):
        check.add_reports("dbfs:/Volumes/nope/missing.pdf")


def test_unsupported_file_named_explicitly_is_registered_with_remedy(check, tmp_path):
    legacy = tmp_path / "old.ppt"
    legacy.write_bytes(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1" + b"\x00" * 600)
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    out = check.add_reports([legacy, text])
    assert set(out["status"]) == {"UNSUPPORTED_FORMAT"}
    assert all(out["remedy"].str.len() > 10)


def test_missing_path_and_empty_glob_raise(check, tmp_path):
    with pytest.raises(FileNotFoundError, match="no such report file or folder"):
        check.add_reports(tmp_path / "nope.pptx")
    with pytest.raises(FileNotFoundError, match="no files match"):
        check.add_reports(str(tmp_path / "*.pptx"))
    with pytest.raises(TypeError):
        check.add_reports(12345)


def test_empty_folder_warns(check, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.warns(UserWarning, match="holds no report files"):
        out = check.add_reports(tmp_path / "empty")
    assert out.empty


def test_overrides_are_validated_and_parsed(check):
    out = check.add_reports(DECK, brand="Kestrel", period="May 2026")
    entry = check._entries[out.iloc[0]["doc_id"]]
    assert entry.overrides["brand"] == "Kestrel"
    assert entry.overrides["period_end"] == date(2026, 5, 31) and entry.overrides["period_grain"] == "month"
    with pytest.raises(ValueError, match="unknown metadata fields"):
        check.confirm(DECK.name, shade="red")
    with pytest.raises(ValueError, match="could not read a period"):
        check.confirm(DECK.name, period="sometime soon")
    with pytest.raises(ValueError, match="confirm needs at least one field"):
        check.confirm(DECK.name)


def test_report_type_override_must_be_known_when_the_registry_has_types():
    check = ReportCheck(load_demo_platform=True)
    with pytest.raises(ValueError, match="unknown report type"):
        check.add_reports(DECK, report_type="weekly_flash")


def test_confirm_finds_reports_by_name_or_doc_id(check):
    out = check.add_reports([DECK, PDF])
    doc_id = out.iloc[0]["doc_id"]
    check.confirm(doc_id[:12], period="Q2 2026")
    assert check._entries[doc_id].overrides["period_grain"] == "quarter"
    check.confirm(PDF.name.lower(), brand="KESTREL")
    with pytest.raises(KeyError, match="no report 'other.pptx'"):
        check.confirm("other.pptx", brand="KESTREL")


def test_period_fields_accepts_dates_and_rejects_unknown_grains():
    assert period_fields(date(2026, 6, 30)) == {"period_end": date(2026, 6, 30)}
    q = period_fields("Q2 2026")
    assert (q["period_start"], q["period_end"], q["period_grain"]) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")
    assert period_fields(None) == {}
