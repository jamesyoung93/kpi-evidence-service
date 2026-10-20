from .compare import split_targets


def select_pairs(specs, targets, *, reports=None, metrics=None):
    available_reports = {t.report for t in targets}
    available_metrics = {t.metric for t in targets}
    for requested, available, name in ((reports, available_reports, "reports"),
                                       (metrics, available_metrics, "metrics")):
        if requested is not None and (not requested or set(requested) - available):
            raise ValueError(f"unknown or empty {name} selection: {requested}")
    selected = [t for t in targets if (reports is None or t.report in reports)
                and (metrics is None or t.metric in metrics)]
    if not selected:
        raise ValueError("no target pairs match the selection")
    missing = {t.metric for t in selected} - specs.keys()
    if missing:
        raise ValueError(f"targets have no metric spec: {sorted(missing)}")
    pairs = sorted({(t.report, t.metric) for t in selected})
    groups = []
    for report, metric in pairs:
        rows = [t for t in selected if (t.report, t.metric) == (report, metric)]
        split_targets(rows, metric)
        unit = specs[metric].unit
        units = {t.unit for t in rows if t.unit}
        if len(units) > 1 or (unit and any(u != unit for u in units)):
            raise ValueError(f"{report}/{metric}: target units do not match metric unit {unit!r}")
        groups.append((report, metric, rows))
    return groups
