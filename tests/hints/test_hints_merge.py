from __future__ import annotations

import copy
import dataclasses

import pytest
from hints_testkit import ACCOUNT, EXAMPLE_CSV, row, table

from kpi_evidence.contracts import RegistryEntry, RegistryStatus
from kpi_evidence.hints import (
    CheckRegistry, definition_source, is_hint, lint_for_hints, load_kpi_table, merge_registry, parse_hints,
    scope_values_sql,
)
from kpi_evidence.registry import normalize_label


@pytest.fixture()
def example_hints(registry):
    return parse_hints(load_kpi_table(EXAMPLE_CSV), base_registry=registry)


def test_example_table_merges_lint_clean(registry, example_hints):
    assert example_hints.errors.empty and example_hints.warnings.empty
    merged = merge_registry(registry, example_hints)
    assert isinstance(merged, CheckRegistry)
    assert lint_for_hints(merged) == []
    assert set(merged.kpis) == set(registry.kpis)
    assert {k for k, v in merged.kpis.items() if is_hint(v)} == set(example_hints.kpis)
    assert definition_source(merged.kpis["speaker_attendance"]) == "registry"
    assert definition_source(merged.kpis["trx"]) == "hint" and definition_source(None) is None


def test_hint_wins_on_id_and_keeps_registry_synonyms(registry, example_hints):
    merged = merge_registry(registry, example_hints)
    trx = merged.kpis["trx"]
    assert trx is example_hints.kpis["trx"] and trx.status == RegistryStatus.DRAFT
    assert set(registry.kpis["trx"].synonyms) <= set(trx.synonyms)
    assert merged.kpis["speaker_attendance"] is registry.kpis["speaker_attendance"]
    assert registry.kpis["trx"].status == RegistryStatus.APPROVED


def test_approved_includes_hint_kpis_so_reports_map_to_them(registry, example_hints):
    merged = merge_registry(registry, example_hints)
    approved = {k.kpi_id for k in merged.approved()}
    assert approved == set(merged.kpis)
    assert merged.version != registry.version
    draft_base = dataclasses.replace(registry, kpis={**registry.kpis, "x": dataclasses.replace(
        registry.kpis["calls"], kpi_id="x", status=RegistryStatus.DRAFT, synonyms=())})
    assert "x" not in {k.kpi_id for k in merge_registry(draft_base, example_hints).approved()}


def test_new_hint_kpi_takes_over_a_registry_label(registry):
    hints = parse_hints(table(row(kpi="referral_volume", table="platform_demo.referrals_monthly",
                                  value_sql="SUM(referrals_in)", aliases="Referrals")), base_registry=registry)
    merged = merge_registry(registry, hints)
    assert "referrals" not in {normalize_label(s) for s in merged.kpis["referrals_in"].synonyms}
    assert "referrals" in {normalize_label(s) for s in merged.kpis["referral_volume"].synonyms}
    assert "Referrals" not in merged.kpis["referrals_in"].raw["synonyms"]
    assert "referrals" in {normalize_label(s) for s in registry.kpis["referrals_in"].synonyms}
    assert lint_for_hints(merged) == []


def test_without_a_base_registry():
    hints = parse_hints(load_kpi_table(EXAMPLE_CSV))
    merged = merge_registry(None, hints)
    assert set(merged.kpis) == set(hints.kpis) and merged.goal_list == [] and merged.policy == {}
    assert list(merged.brands) == ["KESTREL"] and merged.brand_aliases()["kestrel"] == "KESTREL"
    assert merged.kpis["trx"].label == "trx" and merged.kpis["trx"].definition.startswith("KPI table hint:")
    assert lint_for_hints(merged) == []


def test_brands_are_added_once(registry):
    hints = parse_hints(table(row(brand="Kestrel"), row(kpi="nrx", value_sql="SUM(nrx)", brand="Falcon")),
                        base_registry=registry)
    merged = merge_registry(registry, hints)
    assert set(merged.brands) == {"KESTREL", "FALCON"}
    assert merged.brands["FALCON"]["aliases"] == ["falcon"] and "FALCON" not in registry.brands


def test_report_type_conventions_for_redefined_kpis_are_dropped(registry, example_hints):
    base = copy.deepcopy(registry)
    base.report_types["executive_summary"]["assumed_definitions"] = {"trx": {"source": "account_feed"},
                                                                     "speaker_attendance": {"measure": "attendances"}}
    merged = merge_registry(base, example_hints)
    assert merged.report_types["executive_summary"]["assumed_definitions"] == {"speaker_attendance": {"measure": "attendances"}}
    assert lint_for_hints(merged) == []


def test_lint_for_hints_reports_real_problems(registry, example_hints):
    merged = merge_registry(registry, example_hints)
    bad = dataclasses.replace(merged.kpis["calls"], dimensions=("brick",))
    merged.kpis["calls"] = bad
    assert any("dimension 'brick'" in p for p in lint_for_hints(merged))


def test_sync_registry_mirrors_hint_kpis_as_drafts(mem_store, registry, example_hints):
    from kpi_evidence.pipeline import sync_registry

    merged = merge_registry(registry, example_hints)
    sync_registry(mem_store, merged)
    rows = {r.kpi_id: r for r in mem_store.read(RegistryEntry)}
    assert rows["trx"].status == RegistryStatus.DRAFT and rows["trx"].approved_by is None
    assert rows["trx"].spec["definition_source"] == "hint" and rows["trx"].spec["hint_row"] == 1
    assert rows["speaker_attendance"].status == RegistryStatus.APPROVED


def test_scope_values_sql(demo_store, example_hints):
    pairs = scope_values_sql(example_hints)
    assert ("region", f"SELECT DISTINCT region AS v FROM {ACCOUNT} LIMIT 1000") in pairs
    assert all(not sql.startswith("SELECT DISTINCT brand") for _, sql in pairs)
    assert pairs == sorted(set(pairs))
    region_sql = dict((d, s) for d, s in pairs if ACCOUNT in s)["region"]
    values = set(demo_store.platform_access.query(region_sql)["v"])
    assert {"East", "North", "South", "West"} <= values
