"""Statistics: the hypothesis tests of the `stat_*` operations.

What passes over the rows is computed by the engine the flow uses, in that engine's own
expressions (`aggregates.py` for the three SQL dialects, `polars_source.py` for Polars), and
comes back as a handful of aggregates: at most a row per group. From those to the statistic,
the p-value and the intervals there is one scalar module for every engine (`compute.py`,
`dist.py`: Python's standard library only), so that the same flow gives the same numbers
everywhere. `catalog.py` is the table of the tests, `output.py` the table a node returns.

Nothing is imported here: the engines' registries import the modules they need, and
`catalog.py` must stay importable from `operations.py` without a cycle.
"""
