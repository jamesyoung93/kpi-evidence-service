from __future__ import annotations

import pytest

from kpi_evidence import platform
from kpi_evidence.settings import default_sql_dir

BRANDS = ("KESTREL", "OSPREY")
SOURCES = ("rx_account_monthly", "rx_panel_monthly", "referrals_monthly")


def test_every_unique_grain_check_includes_the_brand():
    for view, checks in platform.DQ_CHECKS.items():
        grains = [c.columns for c in checks if c.check == "unique_grain"]
        assert grains and all(cols[0] == "brand" for cols in grains), view


@pytest.fixture()
def multibrand(fresh_platform):
    s = fresh_platform.settings
    fresh_platform.execute("CREATE SCHEMA IF NOT EXISTS multi")
    for table in SOURCES:
        union = " UNION ALL ".join(f"SELECT '{b}' AS brand_code, * FROM {s.platform_schema}.{table}" for b in BRANDS)
        fresh_platform.execute(f"CREATE TABLE multi.{table} AS {union}")
    text = (default_sql_dir() / "governed_views.sql").read_text(encoding="utf-8")
    ported = text.replace("'KESTREL' AS brand", "brand_code AS brand").replace("${platform}.", "multi.")
    assert ported.count("brand_code AS brand") == len(SOURCES)
    fresh_platform.execute_script(platform.render_sql(ported, s))
    return fresh_platform


def test_a_multibrand_panel_keeps_the_latest_load_of_every_brand(multibrand):
    s = multibrand.settings
    expected = int(multibrand.query(f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT month, territory, payer_channel "
                                    f"FROM {s.platform_schema}.rx_panel_monthly) d").iloc[0, 0])
    counts = multibrand.query(f"SELECT brand, COUNT(*) AS n FROM {s.gov_schema}.gv_rx_panel_monthly "
                              f"GROUP BY brand ORDER BY brand")
    assert dict(zip(counts["brand"], counts["n"])) == {b: expected for b in BRANDS}


def test_multibrand_views_pass_the_hard_checks(multibrand):
    results = platform.evaluate_dq_checks(multibrand, multibrand.settings, "multibrand")
    hard = [r for r in results if r.object_name in platform.DQ_CHECKS and r.severity == "hard"]
    assert hard and all(r.passed for r in hard), [(r.object_name, r.check_name, r.detail) for r in hard if not r.passed]
