from datetime import date
import re


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}", value):
        raise ValueError(f"expected a simple SQL identifier: {value!r}")
    return value


def _columns(values):
    if not isinstance(values, list) or not values or len(set(values)) != len(values):
        raise ValueError("columns must be a nonempty list of distinct identifiers")
    return [identifier(v) for v in values]


def _date(value, as_of):
    value = as_of if value == "{as_of}" else value
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("cutoff must be an ISO date or {as_of}")
    return f"DATE '{value}'"


def fragment(option, as_of):
    """Render a string option or a structured latest_partition/deduplicate/alignment option."""
    if isinstance(option, str):
        return option.format(as_of=as_of)
    if not isinstance(option, dict):
        raise ValueError("SQL option must be a string or a structured table")
    options = dict(option)
    kind = options.pop("type", None)
    if kind == "latest_partition":
        return latest_partition(as_of=as_of, **options)
    if kind == "deduplicate":
        return deduplicate(**options)
    if kind == "alignment":
        return alignment(**options)
    raise ValueError(f"unknown SQL option type: {kind!r}")


def latest_partition(source, columns, order_by, *, as_of, cutoff_column=None,
                     cutoff="{as_of}", partition_by=None):
    """Select one lexicographically latest snapshot."""
    source, columns, order_by = identifier(source), _columns(columns), _columns(order_by)
    groups = _columns(partition_by) if partition_by else []
    window = ("PARTITION BY " + ", ".join(groups) + " ") if groups else ""
    window += "ORDER BY " + ", ".join(c + " DESC NULLS LAST" for c in order_by)
    where = " AND ".join(c + " IS NOT NULL" for c in order_by)
    if cutoff_column:
        where += f" AND {identifier(cutoff_column)} <= {_date(cutoff, as_of)}"
    return (f"SELECT {', '.join(columns)} FROM (SELECT *, DENSE_RANK() OVER ({window}) "
            f"AS _recon_partition FROM {source} WHERE {where}) _recon_snap WHERE _recon_partition = 1")


def deduplicate(source, columns, keys, order_by):
    """Keep the last row per declared business grain; include a unique tie-breaker in order_by."""
    source, columns = identifier(source), _columns(columns)
    keys, order_by = _columns(keys), _columns(order_by)
    window = "PARTITION BY " + ", ".join(keys) + " ORDER BY " + ", ".join(c + " DESC NULLS LAST" for c in order_by)
    return (f"SELECT {', '.join(columns)} FROM (SELECT *, ROW_NUMBER() OVER ({window}) "
            f"AS _recon_row FROM {source}) _recon_dedup WHERE _recon_row = 1")


def alignment(source, mapping, columns, entity_key, event_date, *, mapping_key=None,
              territory="territory", valid_from="valid_from", valid_to="valid_to"):
    """Join facts to a preselected alignment vintage using [valid_from, valid_to)."""
    source, mapping = identifier(source), identifier(mapping)
    columns = _columns(columns)
    key, mkey, event = map(identifier, (entity_key, mapping_key or entity_key, event_date))
    territory, start, end = map(identifier, (territory, valid_from, valid_to))
    selected = ", ".join("f." + c for c in columns)
    return (f"SELECT {selected}, m.{territory} AS territory FROM {source} f LEFT JOIN {mapping} m "
            f"ON f.{key} = m.{mkey} AND f.{event} >= m.{start} "
            f"AND (f.{event} < m.{end} OR m.{end} IS NULL)")
