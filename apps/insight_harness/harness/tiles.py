"""Registry-driven tiles, saved questions, scopes, and cache identities.

A tile is an immutable saved-question specification.  This module owns the
translation from display controls to a registry-valid spec; execution remains
in :mod:`harness.tile_runtime` and always enters through ``answer_intent``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import NamedTuple

from . import semantic_layer as sl
from . import services, triage


ALL_REGIONS = "All regions"  # backward-compatible public alias
ALL_SCOPES = "all"
SCOPE_DIMENSIONS = ("region", "district", "territory", "specialty", "payer_channel")

WINDOW_CONTROLS = MappingProxyType({
    "Latest": None,
    "R3M": 3,
    "R6M": 6,
    "R12M": 12,
})
BASIS_CONTROLS = MappingProxyType({
    "MoM": "prior_month",
    "QoQ": "prior_quarter",
    "YoY": "yoy",
})
VIZ_KINDS = ("sparkline", "metric", "line", "table", "count")
QUESTION_CLASSES = (
    triage.DESCRIPTIVE, triage.DIAGNOSTIC, triage.RETRIEVAL, triage.COHORT,
)
RETRIEVAL_TEMPLATES = ("whitespace", "top_accounts", "top_writers")
PERSONA_IDS = (
    "sales_rep", "district_manager", "brand_marketing", "market_access", "executive",
)

FilterValue = str | tuple[str, ...]
FilterItems = tuple[tuple[str, FilterValue], ...]
GovernanceKey = tuple[float, tuple[tuple[str, str], ...], str]
_INHERIT = object()


def freeze_filters(filters: Mapping | FilterItems | None) -> FilterItems:
    """Canonicalize filters, including behavior-equivalent duplicate values."""

    if not filters:
        return ()
    items = filters.items() if isinstance(filters, Mapping) else filters
    frozen: list[tuple[str, FilterValue]] = []
    for dimension, value in items:
        if isinstance(value, (list, tuple, set, frozenset)):
            normalized_values = tuple(sorted({str(item) for item in value}))
            normalized: FilterValue = normalized_values
        else:
            normalized = str(value)
        frozen.append((str(dimension), normalized))
    order = {dimension: index for index, dimension in enumerate(sl.DIMENSIONS)}
    return tuple(sorted(frozen, key=lambda item: (order.get(item[0], len(order)), item[0])))


def dimension_values(dimension: str) -> tuple[str, ...]:
    if dimension not in sl.DIMENSIONS:
        raise ValueError(f"unregistered dimension: {dimension!r}")
    fact = sl.load_fact("source_a")
    if dimension not in fact.columns:
        return ()
    return tuple(sorted(str(value) for value in fact[dimension].dropna().unique()))


@dataclass(frozen=True)
class ScopeOption:
    key: str
    label: str
    filters: FilterItems = ()


def scope_options() -> tuple[ScopeOption, ...]:
    options = [ScopeOption(ALL_SCOPES, "All scopes")]
    for dimension in SCOPE_DIMENSIONS:
        label = dimension.replace("_", " ").title()
        options.extend(ScopeOption(f"{dimension}::{value}", f"{label} · {value}",
                                   ((dimension, value),))
                       for value in dimension_values(dimension))
    return tuple(options)


def region_options() -> tuple[str, ...]:
    """Compatibility API retained for older callers and persisted snapshots."""

    return (ALL_REGIONS, *dimension_values("region"))


def normalize_scope(scope: Mapping | FilterItems | str | None = None) -> FilterItems:
    if scope is None or scope in (ALL_REGIONS, ALL_SCOPES, "All scopes"):
        return ()
    if isinstance(scope, str):
        if "::" in scope:
            dimension, value = scope.split("::", 1)
            return freeze_filters({dimension: value})
        if scope in dimension_values("region"):
            return (("region", scope),)
        raise ValueError(f"unknown scope: {scope!r}")
    return freeze_filters(scope)


def scope_errors(scope: Mapping | FilterItems | str | None) -> tuple[str, ...]:
    try:
        frozen = normalize_scope(scope)
    except ValueError as exc:
        return (str(exc),)
    errors: list[str] = []
    dimensions = [dimension for dimension, _ in frozen]
    duplicates = sorted({d for d in dimensions if dimensions.count(d) > 1})
    if duplicates:
        errors.append(f"duplicate scope dimension(s): {duplicates!r}")
    for dimension, value in frozen:
        if dimension not in SCOPE_DIMENSIONS:
            errors.append(f"unsupported scope dimension: {dimension!r}")
            continue
        values = value if isinstance(value, tuple) else (value,)
        unknown = [item for item in values if item not in set(dimension_values(dimension))]
        if unknown:
            errors.append(f"unregistered {dimension} value(s): {unknown!r}")
    return tuple(errors)


def require_valid_scope(scope: Mapping | FilterItems | str | None) -> FilterItems:
    errors = scope_errors(scope)
    if errors:
        raise ValueError("; ".join(errors))
    return normalize_scope(scope)


def scope_key(scope: Mapping | FilterItems | str | None) -> str:
    frozen = require_valid_scope(scope)
    if not frozen:
        return ALL_SCOPES
    if len(frozen) == 1 and not isinstance(frozen[0][1], tuple):
        return f"{frozen[0][0]}::{frozen[0][1]}"
    return "|".join(f"{dimension}::{','.join(value) if isinstance(value, tuple) else value}"
                    for dimension, value in frozen)


def scope_label(scope: Mapping | FilterItems | str | None) -> str:
    frozen = require_valid_scope(scope)
    if not frozen:
        return "All scopes"
    return " · ".join(
        f"{dimension.replace('_', ' ').title()}: "
        f"{', '.join(value) if isinstance(value, tuple) else value}"
        for dimension, value in frozen
    )


def scope_from_key(key: str) -> FilterItems:
    if key == ALL_SCOPES:
        return ()
    match = next((option for option in scope_options() if option.key == key), None)
    if match is None:
        raise ValueError(f"unknown scope key: {key!r}")
    return match.filters


@dataclass(frozen=True)
class SavedQuestionSpec:
    metric: str
    filters: FilterItems = ()
    source: str | None = None
    variant: str | None = None
    window: str = "Latest"
    basis: str = "MoM"
    viz_kind: str = "sparkline"
    default_personas: tuple[str, ...] = ()
    question_class: str = triage.DESCRIPTIVE
    breakdown_dimension: str | None = None
    retrieval_template: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filters", freeze_filters(self.filters))
        object.__setattr__(self, "default_personas", tuple(dict.fromkeys(self.default_personas)))


def spec_errors(spec: SavedQuestionSpec) -> tuple[str, ...]:
    errors: list[str] = []
    metric = sl.METRICS.get(spec.metric)
    if metric is None:
        errors.append(f"unregistered metric: {spec.metric!r}")
    else:
        if spec.source is not None and spec.source not in metric["sources"]:
            errors.append(f"source {spec.source!r} is not registered for {spec.metric!r}")
        if spec.variant is not None and spec.variant not in metric["variants"]:
            errors.append(f"variant {spec.variant!r} is not registered for {spec.metric!r}")

    dimensions = [dimension for dimension, _ in spec.filters]
    duplicates = sorted({d for d in dimensions if dimensions.count(d) > 1})
    if duplicates:
        errors.append(f"duplicate filter dimension(s): {duplicates!r}")
    for dimension, value in spec.filters:
        if dimension not in sl.DIMENSIONS:
            errors.append(f"unregistered dimension: {dimension!r}")
            continue
        values = value if isinstance(value, tuple) else (value,)
        if not values:
            errors.append(f"empty filter values for {dimension!r}")
            continue
        unknown = [item for item in values if item not in set(dimension_values(dimension))]
        if unknown:
            errors.append(f"unregistered {dimension} value(s): {unknown!r}")

    if spec.window not in WINDOW_CONTROLS:
        errors.append(f"unknown window control: {spec.window!r}")
    if spec.basis not in BASIS_CONTROLS:
        errors.append(f"unknown comparison control: {spec.basis!r}")
    if spec.viz_kind not in VIZ_KINDS:
        errors.append(f"unknown visualization kind: {spec.viz_kind!r}")
    unknown_personas = [p for p in spec.default_personas if p not in PERSONA_IDS]
    if unknown_personas:
        errors.append(f"unknown default persona(s): {unknown_personas!r}")
    if spec.question_class not in QUESTION_CLASSES:
        errors.append(f"unsupported tile question class: {spec.question_class!r}")
    if spec.question_class == triage.DIAGNOSTIC:
        if spec.breakdown_dimension not in sl.DIMENSIONS:
            errors.append(f"invalid diagnostic breakdown dimension: {spec.breakdown_dimension!r}")
        if spec.retrieval_template is not None:
            errors.append("diagnostic tiles cannot declare a retrieval template")
    elif spec.breakdown_dimension is not None:
        errors.append(f"{spec.question_class} tiles cannot declare a breakdown dimension")
    if spec.question_class == triage.RETRIEVAL:
        if spec.retrieval_template not in RETRIEVAL_TEMPLATES:
            errors.append(f"invalid retrieval template: {spec.retrieval_template!r}")
    elif spec.retrieval_template is not None:
        errors.append(f"{spec.question_class} tiles cannot declare a retrieval template")
    if spec.question_class == triage.COHORT and spec.metric != "nrx":
        errors.append("cohort tiles must use the governed NRx-share selection recipe")
    return tuple(errors)


def require_valid_spec(spec: SavedQuestionSpec) -> SavedQuestionSpec:
    errors = spec_errors(spec)
    if errors:
        raise ValueError("; ".join(errors))
    return spec


@dataclass(frozen=True)
class TileDefinition:
    id: str
    label: str
    metric: str
    filters: FilterItems = ()
    source: str | None = None
    variant: str | None = None
    window: str = "Latest"
    basis: str = "MoM"
    viz_kind: str = "sparkline"
    default_personas: tuple[str, ...] = ()
    question_class: str = triage.DESCRIPTIVE
    breakdown_dimension: str | None = None
    retrieval_template: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filters", freeze_filters(self.filters))
        object.__setattr__(self, "default_personas", tuple(dict.fromkeys(self.default_personas)))
        require_valid_spec(question_spec_from_definition(self))


def question_spec_from_definition(definition: TileDefinition) -> SavedQuestionSpec:
    return SavedQuestionSpec(
        metric=definition.metric, filters=definition.filters, source=definition.source,
        variant=definition.variant, window=definition.window, basis=definition.basis,
        viz_kind=definition.viz_kind, default_personas=definition.default_personas,
        question_class=definition.question_class,
        breakdown_dimension=definition.breakdown_dimension,
        retrieval_template=definition.retrieval_template,
    )


_CALL_ATTAINMENT_METRIC = "call_attainment" if "call_attainment" in sl.METRICS else "calls"
TILE_DEFINITIONS = (
    TileDefinition("trx", "TRx", "trx", default_personas=PERSONA_IDS),
    TileDefinition("nrx", "NRx", "nrx",
                   default_personas=("district_manager", "brand_marketing", "executive")),
    TileDefinition("nbrx", "NBRx", "nbrx",
                   default_personas=("district_manager", "brand_marketing", "executive")),
    TileDefinition("trx_share", "TRx market share", "trx_share",
                   default_personas=("brand_marketing", "market_access", "executive")),
    TileDefinition("calls", "Details", "calls",
                   default_personas=("sales_rep", "district_manager")),
    TileDefinition("call_attainment", "Call attainment", _CALL_ATTAINMENT_METRIC,
                   viz_kind="metric", default_personas=("sales_rep", "district_manager")),
    TileDefinition("new_writers", "New writers", "new_writers",
                   default_personas=("sales_rep", "district_manager", "brand_marketing")),
    TileDefinition("samples", "Samples dropped", "samples",
                   default_personas=("sales_rep", "district_manager")),
    TileDefinition("commercial_trx", "Commercial TRx", "trx",
                   (("payer_channel", "Commercial"),),
                   default_personas=("market_access", "executive")),
    TileDefinition("payer_mix", "Payer channel mix", "trx", viz_kind="table",
                   default_personas=("district_manager", "brand_marketing", "market_access"),
                   question_class=triage.DIAGNOSTIC, breakdown_dimension="payer_channel"),
    TileDefinition("whitespace_hcps", "Whitespace HCPs", "trx", viz_kind="count",
                   default_personas=("sales_rep", "district_manager"),
                   question_class=triage.RETRIEVAL, retrieval_template="whitespace"),
    TileDefinition("top_writers", "Top writers", "nrx", viz_kind="table",
                   default_personas=("district_manager", "brand_marketing"),
                   question_class=triage.RETRIEVAL, retrieval_template="top_writers"),
    TileDefinition("incoming_referrals", "Incoming referrals", "referrals_in",
                   default_personas=("district_manager", "market_access")),
    TileDefinition("active_referrers", "Active referrers", "active_referrers",
                   default_personas=("district_manager", "market_access")),
    TileDefinition("hcp_cohort", "Top HCP activity gaps", "nrx", viz_kind="table",
                   default_personas=("brand_marketing", "executive"),
                   question_class=triage.COHORT),
)
TILES_BY_ID = MappingProxyType({tile.id: tile for tile in TILE_DEFINITIONS})
if len(TILES_BY_ID) != len(TILE_DEFINITIONS):
    raise RuntimeError("tile registry ids must be unique")


def tile_definition(tile: TileDefinition | str) -> TileDefinition:
    if isinstance(tile, TileDefinition):
        return tile
    try:
        return TILES_BY_ID[tile]
    except KeyError as exc:
        raise ValueError(f"unknown tile id: {tile!r}") from exc


def question_spec(tile: TileDefinition | str, *, window: str | None = None,
                  basis: str | None = None, source=_INHERIT, variant=_INHERIT,
                  viz_kind: str | None = None) -> SavedQuestionSpec:
    definition = tile_definition(tile)
    base = question_spec_from_definition(definition)
    return require_valid_spec(replace(
        base,
        source=definition.source if source is _INHERIT else source,
        variant=definition.variant if variant is _INHERIT else variant,
        window=window if window is not None else definition.window,
        basis=basis if basis is not None else definition.basis,
        viz_kind=viz_kind if viz_kind is not None else definition.viz_kind,
    ))


@dataclass(frozen=True)
class MaterializedSpec:
    spec: SavedQuestionSpec
    disclosures: tuple[str, ...] = ()


def materialize_spec(tile: TileDefinition | str, *, window: str | None = None,
                     basis: str | None = None, source: str | None = None,
                     variant: str | None = None, viz_kind: str | None = None) -> MaterializedSpec:
    """Apply only compatible, behavior-affecting global controls."""

    definition = tile_definition(tile)
    base = question_spec(definition, viz_kind=viz_kind)
    disclosures: list[str] = []
    updates: dict = {}
    if base.question_class in (triage.DESCRIPTIVE, triage.DIAGNOSTIC):
        if window is not None:
            updates["window"] = window
        if basis is not None:
            updates["basis"] = basis
    elif window is not None or basis is not None:
        disclosures.append("Window and comparison controls do not apply to this retrieval tile.")

    metric = sl.METRICS[base.metric]
    if base.question_class in (triage.RETRIEVAL, triage.COHORT):
        if source is not None or variant is not None:
            disclosures.append(
                "Source and sales-type controls do not apply to this governed recipe.")
    else:
        if source is not None:
            if source in metric["sources"]:
                updates["source"] = source
            else:
                source_label = sl.SOURCES.get(source, {}).get("name", source)
                disclosures.append(
                    f"{source_label} is not registered for {metric['label']}; "
                    "this tile retained its governed source.")
        if variant is not None:
            if variant in metric["variants"]:
                updates["variant"] = variant
            else:
                disclosures.append(
                    f"Sales type {variant!r} is not registered for {metric['label']}; "
                    "this tile retained its governed variant.")
    return MaterializedSpec(require_valid_spec(replace(base, **updates)), tuple(disclosures))


def effective_spec_filters(spec: SavedQuestionSpec,
                           scope: Mapping | FilterItems | str | None = None) -> dict[str, FilterValue]:
    require_valid_spec(spec)
    scoped = dict(require_valid_scope(scope))
    # Preserve hierarchy context explicitly.  Human-readable district values
    # contain their region name, so the rule parser correctly discovers both;
    # adding unique ancestors here keeps the saved spec and parsed Intent exact
    # while remaining behaviorally equivalent at query time.
    if scoped and any(dimension in scoped for dimension in ("territory", "district")):
        fact = sl.apply_filters(sl.load_fact("source_a"), scoped)
        for ancestor in ("district", "region"):
            if ancestor in scoped or ancestor not in fact.columns:
                continue
            values = tuple(sorted(str(value) for value in fact[ancestor].dropna().unique()))
            if len(values) == 1:
                scoped[ancestor] = values[0]
    filters = dict(spec.filters)
    for dimension, value in scoped.items():
        filters.setdefault(dimension, value)
    return filters


def fixed_scope_disclosures(spec: SavedQuestionSpec,
                            scope: Mapping | FilterItems | str | None = None) -> tuple[str, ...]:
    """Disclose global-scope choices that a fixed-scope tile cannot honor.

    A catalog tile such as Commercial TRx intentionally pins one dimension.
    Silently presenting it as Medicaid after the global selector changes would
    be false; silently ignoring Medicaid would also violate the control-band
    promise. The fixed definition wins and the incompatibility is explicit.
    """

    selected = dict(require_valid_scope(scope))
    fixed = dict(spec.filters)
    notes: list[str] = []
    for dimension in sorted(set(selected).intersection(fixed)):
        requested = selected[dimension]
        registered = fixed[dimension]
        requested_values = set(requested if isinstance(requested, tuple) else (requested,))
        registered_values = set(registered if isinstance(registered, tuple) else (registered,))
        if requested_values.isdisjoint(registered_values):
            label = dimension.replace("_", " ").title()
            notes.append(
                f"Selected {label} ({', '.join(sorted(requested_values))}) conflicts with "
                f"this tile's fixed {label} ({', '.join(sorted(registered_values))}); "
                "the governed fixed scope was retained."
            )
    return tuple(notes)


def effective_filters(tile: TileDefinition | str,
                      region: Mapping | FilterItems | str | None = ALL_REGIONS, *,
                      scope: Mapping | FilterItems | str | None = None) -> dict[str, FilterValue]:
    selected = region if scope is None else scope
    return effective_spec_filters(question_spec(tile), selected)


def _scope_values(filters: Mapping[str, FilterValue]) -> list[str]:
    values: list[str] = []
    for dimension in sl.DIMENSIONS:
        if dimension in filters:
            value = filters[dimension]
            values.extend(value if isinstance(value, tuple) else (value,))
    return values


_BREAKDOWN_LABELS = {
    "specialty": "specialties", "payer_channel": "payer channels",
    "territory": "territories", "district": "districts", "region": "regions",
}


def canonical_question_for_spec(spec: SavedQuestionSpec,
                                scope: Mapping | FilterItems | str | None = None) -> str:
    require_valid_spec(spec)
    filters = effective_spec_filters(spec, scope)
    keyword = services.PRIMARY_KEYWORD.get(spec.metric, spec.metric.replace("_", " "))
    scope_values = _scope_values(filters)
    where = f" in {' and '.join(scope_values)}" if scope_values else ""

    if spec.question_class == triage.RETRIEVAL:
        if spec.retrieval_template == "whitespace":
            return f"List whitespace HCPs by {keyword} with no activity{where}"
        if spec.retrieval_template == "top_writers":
            return ("Top 15 HCP writers by trailing-12-month NRx share"
                    f"{where}")
        return f"Top 15 accounts by {keyword}{where}"
    if spec.question_class == triage.COHORT:
        return ("Compare the activity mix of top 20 HCPs by NRx share with "
                f"matched peers{where}")

    months = WINDOW_CONTROLS[spec.window]
    window = f" last {months} months" if months is not None else ""
    basis = triage.BASIS_LABELS[BASIS_CONTROLS[spec.basis]]
    if spec.question_class == triage.DIAGNOSTIC:
        target = _BREAKDOWN_LABELS[spec.breakdown_dimension]
        return f"Which {target} account for the {keyword} change{where}{window} {basis}?"
    return f"Trend {keyword} by month{window}{where} {basis}"


def canonical_question(tile: TileDefinition | str, *, window: str | None = None,
                       basis: str | None = None,
                       region: Mapping | FilterItems | str | None = ALL_REGIONS,
                       scope: Mapping | FilterItems | str | None = None) -> str:
    selected = region if scope is None else scope
    return canonical_question_for_spec(question_spec(tile, window=window, basis=basis), selected)


def intent_for_spec(spec: SavedQuestionSpec,
                    scope: Mapping | FilterItems | str | None = None) -> triage.Intent:
    question = canonical_question_for_spec(spec, scope)
    intent = triage.parse(question)
    expected_filters = effective_spec_filters(spec, scope)
    expected_intent_filters = {
        dimension: (list(value) if len(value) > 1 else value[0])
        if isinstance(value, tuple) else value
        for dimension, value in expected_filters.items()
    }
    valid = (intent.question_class == spec.question_class and intent.metric == spec.metric
             and intent.filters == expected_intent_filters)
    if spec.question_class == triage.DESCRIPTIVE:
        valid = valid and intent.trend
    elif spec.question_class == triage.DIAGNOSTIC:
        valid = valid and intent.dim_breakdown == spec.breakdown_dimension
    elif spec.question_class == triage.RETRIEVAL:
        valid = valid and intent.template == spec.retrieval_template
    elif spec.question_class == triage.COHORT:
        valid = valid and intent.metric == "nrx"
    if not valid:
        raise RuntimeError(f"saved question no longer round-trips: {question!r}")
    return intent


def intent_for(tile: TileDefinition | str, *, window: str | None = None,
               basis: str | None = None,
               region: Mapping | FilterItems | str | None = ALL_REGIONS,
               scope: Mapping | FilterItems | str | None = None) -> triage.Intent:
    selected = region if scope is None else scope
    return intent_for_spec(question_spec(tile, window=window, basis=basis), selected)


def governance_cache_key() -> GovernanceKey:
    # Import lazily: tiles are imported from several view modules during a
    # cold Streamlit start, while the semantic registry may still be entering
    # sys.modules.  Basket governance belongs in the cache key, but importing
    # the validating basket registry at module load would create a partial-
    # initialization cycle (tiles -> baskets -> semantic_layer).
    from . import baskets

    variants = tuple((metric, sl.default_variant(metric)) for metric in sorted(sl.METRICS))
    return sl.materiality(), variants, baskets.registry_fingerprint()


class TileCacheKey(NamedTuple):
    spec: SavedQuestionSpec
    effective_scope: FilterItems
    governance: GovernanceKey
    data_version: str

    @property
    def effective_region(self):  # compatibility for older callers
        return dict(self.effective_scope).get("region")


def cache_key_for_spec(spec: SavedQuestionSpec, *,
                       scope: Mapping | FilterItems | str | None = None,
                       governance: GovernanceKey | None = None,
                       data_version: str | None = None) -> TileCacheKey:
    require_valid_spec(spec)
    return TileCacheKey(
        spec=spec,
        effective_scope=require_valid_scope(scope),
        governance=governance if governance is not None else governance_cache_key(),
        data_version=data_version if data_version is not None else sl.data_version(),
    )


def cache_key(tile: TileDefinition | str, *, window: str | None = None,
              basis: str | None = None,
              region: Mapping | FilterItems | str | None = ALL_REGIONS,
              scope: Mapping | FilterItems | str | None = None,
              source: str | None = None, variant: str | None = None,
              governance: GovernanceKey | None = None,
              data_version: str | None = None) -> TileCacheKey:
    selected = region if scope is None else scope
    materialized = materialize_spec(tile, window=window, basis=basis,
                                    source=source, variant=variant)
    return cache_key_for_spec(materialized.spec, scope=selected,
                              governance=governance, data_version=data_version)
