from __future__ import annotations

import pytest

from harness import drill


def test_geo_drill_only_returns_registered_children_and_breadcrumbs():
    assert drill.next_dimension({}) == "region"
    regions = drill.child_options({})
    assert regions
    assert {option.dimension for option in regions} == {"region"}

    region_scope = drill.select_child({}, "region", regions[0].value)
    districts = drill.child_options(region_scope)
    assert districts and {option.dimension for option in districts} == {"district"}
    district_scope = drill.select_child(region_scope, "district", districts[0].value)
    territories = drill.child_options(district_scope)
    assert territories and {option.dimension for option in territories} == {"territory"}
    territory_scope = drill.select_child(
        district_scope, "territory", territories[0].value)

    assert drill.next_dimension(territory_scope) is None
    labels = [label for label, _ in drill.breadcrumbs(territory_scope)]
    assert labels[0] == "National"
    assert labels[-1].startswith("Territory:")


def test_geo_drill_rejects_guessed_or_out_of_order_children():
    with pytest.raises(ValueError, match="next drill dimension"):
        drill.select_child({}, "territory", "made-up")
    with pytest.raises(ValueError, match="not a registered region"):
        drill.select_child({}, "region", "made-up")


def test_hcp_endpoint_requires_territory_and_is_ranked():
    region = drill.child_options({})[0]
    district = drill.child_options(dict(region.filters))[0]
    territory = drill.child_options(dict(district.filters))[0]
    scope = dict(territory.filters)

    with pytest.raises(ValueError, match="selected territory"):
        drill.hcp_rows(dict(region.filters))
    rows = drill.hcp_rows(scope, "nrx", top_n=10, min_volume=1)
    assert 0 < len(rows) <= 10
    assert rows["nrx_ttm"].is_monotonic_decreasing
    assert set(rows["territory"]) == {scope["territory"]}
