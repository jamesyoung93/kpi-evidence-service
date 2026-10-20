from concurrent.futures import ThreadPoolExecutor
from itertools import product
from math import prod

from .compare import evaluate


def worker_count(value):
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError("max_workers must be an integer between 1 and 32")
    return value


def ordered_map(function, items, max_workers):
    with ThreadPoolExecutor(max_workers=worker_count(max_workers)) as pool:
        return list(pool.map(function, items))


def precompute_fit(spec, targets, runner, *, max_candidates=128, max_queries=1000):
    """Warm declared combinations on fit periods only; never select or accept edits."""
    if any(type(v) is not int or v < 1 for v in (max_candidates, max_queries)):
        raise ValueError("precompute budgets must be positive integers")
    fit = [t for t in targets if t.split == "fit"]
    names = list(spec.params)
    options = [list(spec.params[p].options) for p in names]
    size = prod(len(values) for values in options)
    queries = size * len(fit) * (len(spec.checks) + (2 if spec.ratio else 1))
    if size > max_candidates or queries > max_queries:
        raise ValueError(f"precompute budget exceeded: {size} combinations, at most {queries} queries")
    failures = []
    for values in product(*options):
        choices = dict(zip(names, values))
        try:
            evaluate(runner, spec, choices, fit)
        except Exception as exc:
            failures.append(dict(params=choices, error=str(exc)))
    return dict(candidates=size, query_upper_bound=queries, failures=failures, split="fit")
