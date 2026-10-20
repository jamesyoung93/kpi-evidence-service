CATALOG: dict[str, str] = {
    "time_window": "Different period definition: fiscal vs calendar week, rolling vs fixed, cut-off day.",
    "universe": "Different population: which accounts, stores, customers or entities are counted.",
    "basket": "Different scope of what is counted: products, categories, segments, indications.",
    "measure": "Different measure construction: gross vs net, with or without tax, returns, discounts.",
    "deduplication": "Records counted once vs every time they appear (multi-mapping, repeats).",
    "mapping": "Different hierarchy or assignment: which territory, region or owner a record rolls to.",
    "restatement": "The source was revised after the report was published.",
    "late_data": "Records that arrive after the cut-off and change recent periods.",
    "source": "A different or partial source: a feed the report uses that the platform does not have.",
    "category_filter": "Different category eligibility: an entity can belong to more than one category.",
    "data_vintage": "Different source snapshot or processing cycle available at the reporting cutoff.",
    "alignment_vintage": "Different dated version of the entity-to-territory assignment.",
}

RESIDUAL_HINTS: dict[str, list[str]] = {
    "level_shift": ["restatement", "source", "measure"],
    "drift": ["late_data", "restatement", "mapping"],
    "noisy": ["mapping", "deduplication", "late_data"],
}


def validate(hypothesis: str) -> bool:
    return hypothesis in CATALOG
