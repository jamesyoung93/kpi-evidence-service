from __future__ import annotations

from hints_testkit import EXAMPLE_CSV, EXAMPLE_MD, EXAMPLE_XLSX

from kpi_evidence.hints import COLUMNS, example_kpi_table_path, load_kpi_table, parse_hints


def test_example_path_points_at_the_committed_csv():
    assert example_kpi_table_path().resolve() == EXAMPLE_CSV.resolve()
    assert EXAMPLE_XLSX.exists()


def test_every_row_is_valid_and_every_kind_of_row_is_present():
    hints = parse_hints(load_kpi_table(EXAMPLE_CSV))
    assert hints.messages.empty and all(r.accepted for r in hints.rows)
    rows = {r.kpi_id: r for r in hints.rows}
    assert len(rows["trx"].tables) == 2 and len(rows["calls"].tables) == 2
    assert rows["call_attainment"].alternatives
    assert rows["trx_share"].numerator and rows["trx_share"].denominator
    assert rows["exfactory_units"].tables[0].startswith("platform_restricted.")
    assert {k for k, r in rows.items() if r.report_only} == {"brand_awareness", "share_of_voice", "persistency_12m"}
    assert hints.default_brand == "KESTREL"


def test_kpi_table_md_explains_every_column_and_row():
    text = EXAMPLE_MD.read_text(encoding="utf-8")
    for col in COLUMNS:
        assert f"`{col}`" in text, col
    for kpi_id in load_kpi_table(EXAMPLE_CSV)["kpi"]:
        assert f"| `{kpi_id}` |" in text, kpi_id
