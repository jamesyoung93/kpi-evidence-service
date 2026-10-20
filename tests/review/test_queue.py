from __future__ import annotations

from kpi_evidence import review
from kpi_evidence.contracts import ValidationStatus as S
from kpi_evidence.review import QUEUE_COLUMNS, review_queue


def _seed_mix(seeder):
    a = seeder.doc("A_deck.pptx")
    b = seeder.doc("B_deck.pptx")
    rows = {
        "var_b3": seeder.seed("samples", S.VARIANCE_REVIEW, doc=b, page=3, report_value=1060, platform_value=1000),
        "var_a5": seeder.seed("samples", S.VARIANCE_REVIEW, doc=a, page=5, report_value=1060, platform_value=1000),
        "var_a2": seeder.seed("nrx", S.VARIANCE_REVIEW, doc=a, page=2),
        "inc": seeder.seed("call_attainment", S.INCOMPATIBLE_DEFINITION, doc=a, page=4, unit="fraction",
                           report_value=0.919, platform_value=0.8442),
        "unc": seeder.seed("trx", S.UNCERTAIN_EXTRACTION, doc=b, page=9, report_value=None, platform_value=None),
        "acc": seeder.seed("exfactory_units", S.MISSING_ACCESS, doc=a, page=6, platform_value=None),
        "ver": seeder.seed("trx", S.VERIFIED, doc=a, page=2),
        "rep": seeder.seed("brand_awareness", S.REPORT_ONLY, doc=a, page=7, unit="fraction", report_value=0.62,
                           platform_value=None),
    }
    return rows


def test_queue_orders_by_status_then_document_then_page(seeder, store, registry):
    rows = _seed_mix(seeder)
    q = review_queue(store, registry=registry)
    assert list(q.columns) == list(QUEUE_COLUMNS)
    expected = [rows[k].result.result_id for k in ("var_a2", "var_a5", "var_b3", "inc", "unc", "acc")]
    assert list(q["result_id"]) == expected
    assert set(q["group"]) == {"REVIEW", "BLOCKED"}
    first = q.iloc[0]
    assert first["status_label"] == "Variance to review"
    assert first["kpi_label"] == registry.kpis["nrx"].label
    assert first["file_name"] == "A_deck.pptx" and int(first["page"]) == 2
    assert not q["decided"].any()


def test_queue_groups_and_brand_filter(seeder, store, registry):
    rows = _seed_mix(seeder)
    other = seeder.doc("Other_brand.pptx", brand="OTHER")
    seeder.seed("trx", S.VARIANCE_REVIEW, doc=other, brand="OTHER")
    good = review_queue(store, groups=("GOOD",), registry=registry)
    assert set(good["result_id"]) == {rows["ver"].result.result_id, rows["rep"].result.result_id}
    blocked = review_queue(store, groups=("BLOCKED",))
    assert list(blocked["status"]) == ["MISSING_ACCESS"]
    assert len(review_queue(store, brand="OTHER")) == 1
    assert len(review_queue(store, brand="KESTREL")) == 6
    assert review_queue(store, groups=()).empty


def test_queue_kpi_label_from_registry_table_when_no_registry(seeder, store, registry):
    _seed_mix(seeder)
    store.upsert(registry.registry_rows())
    q = review_queue(store)
    assert q.loc[q["kpi_id"] == "nrx", "kpi_label"].iloc[0] == registry.kpis["nrx"].label


def test_decided_results_leave_the_queue_until_the_version_changes(seeder, store, registry, settings):
    rows = _seed_mix(seeder)
    target = rows["var_a2"].result
    review.decide(store, registry, settings, target.result_id, "REJECT", "wrong slide", "r@example.com",
                  decision_id="d-1")
    q = review_queue(store)
    assert target.result_id not in set(q["result_id"])
    q_all = review_queue(store, include_decided=True)
    assert bool(q_all.loc[q_all["result_id"] == target.result_id, "decided"].iloc[0]) is True
    target.result_version = 2
    store.upsert([target])
    assert target.result_id in set(review_queue(store)["result_id"])


def test_not_current_results_are_not_queued(seeder, store):
    s = seeder.seed("nrx", S.VARIANCE_REVIEW, is_current=False)
    assert s.result.result_id not in set(review_queue(store)["result_id"])


def test_queue_works_without_the_review_open_view(seeder, store, registry):
    _seed_mix(seeder)
    with_view = review_queue(store, registry=registry)
    store.execute(f"DROP VIEW IF EXISTS {store.fq('review_open')}")
    without = review_queue(store, registry=registry)
    assert list(with_view["result_id"]) == list(without["result_id"])


def test_record_opened_writes_one_event_per_request(seeder, store):
    s = seeder.seed("nrx", S.VARIANCE_REVIEW)
    review.record_opened(store, s.result.result_id, "r@example.com", request_id="req-1")
    review.record_opened(store, s.result.result_id, "r@example.com", request_id="req-1")
    events = store.read_df("review_events", "event = 'opened'")
    assert len(events) == 1 and events.iloc[0]["request_id"] == "req-1"
