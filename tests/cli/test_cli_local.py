from __future__ import annotations

import pytest

from cli_support import REPORTS, install_standins
from kpi_evidence import cli

PDF = REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf"


def test_help_and_version(capsys):
    assert cli.main([]) == 2
    assert "check" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_check_needs_paths_or_demo(capsys):
    assert cli.main(["check"]) == 2
    assert "--demo" in capsys.readouterr().out


def test_lint_bundled_registry(capsys):
    assert cli.main(["lint"]) == 0
    out = capsys.readouterr().out
    assert "lint: OK" in out and "KPI(s)" in out


def test_lint_kpi_table_with_an_invalid_row(monkeypatch, tmp_path, capsys):
    install_standins(monkeypatch)
    table = tmp_path / "kpis.csv"
    table.write_text("kpi,table,value_sql,date_column,date_format\n"
                     "TRx,platform_demo.rx_account_monthly,SUM(trx_units),month,yyyy-MM\n"
                     "Bad,platform_demo.rx_account_monthly,SUM(x); DROP TABLE y,month,yyyy-MM\n", encoding="utf-8")
    assert cli.main(["lint", "--kpis", str(table)]) == 1
    out = capsys.readouterr().out
    assert "KPI table messages" in out and "may not contain" in out


def test_lint_example_kpi_table_with_the_real_hints(capsys):
    pytest.importorskip("kpi_evidence.hints")
    from cli_support import ROOT

    assert cli.main(["lint", "--kpis", str(ROOT / "samples" / "kpi_table_example.csv")]) == 0
    assert "lint: OK" in capsys.readouterr().out


def test_check_files_with_the_demo_platform_writes_outputs(monkeypatch, tmp_path, capsys):
    install_standins(monkeypatch)
    html, xlsx = tmp_path / "out" / "check.html", tmp_path / "out" / "check.xlsx"
    code = cli.main(["check", str(PDF), "--demo", "--html", str(html), "--excel", str(xlsx), "--no-materialize"])
    assert code == 0
    out = capsys.readouterr().out
    assert "KPI check: 1 report(s)" in out and "good" in out
    assert html.exists() and xlsx.exists() and f"wrote {html}" in out


def test_check_without_platform_is_an_inventory(monkeypatch, tmp_path, capsys):
    install_standins(monkeypatch)
    assert cli.main(["check", str(PDF), "--brand", "Kestrel"]) == 0
    out = capsys.readouterr().out
    assert "KPI check: 1 report(s)" in out


def test_demo_command_writes_html_and_excel(monkeypatch, tmp_path, capsys):
    install_standins(monkeypatch)
    assert cli.main(["demo", "--out", str(tmp_path / "demo"), "--no-kpi-table"]) == 0
    assert (tmp_path / "demo" / "kpi_check_demo.html").exists()
    assert (tmp_path / "demo" / "kpi_check_demo.xlsx").exists()
    assert "needs review" in capsys.readouterr().out


def test_demo_command_with_the_real_report(tmp_path):
    pytest.importorskip("kpi_evidence.hints")
    pytest.importorskip("kpi_evidence.report")
    pytest.importorskip("kpi_evidence.report_excel")
    assert cli.main(["demo", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "kpi_check_demo.html").stat().st_size > 1000
    assert (tmp_path / "kpi_check_demo.xlsx").stat().st_size > 1000


def test_summary_text_tolerates_partial_results():
    class Partial:
        comparisons = None
        documents = None

    assert cli.summary_text(Partial()) == "KPI check: 0 report(s), 0 comparison(s)"
