"""The catalogue of the hypothesis tests: the one table every engine registers its
statistics operations from, what each test asks of its node's parameters, and the limits.

Every test is an operation of its own, named `stat_` + the test's id (`stat_t_two_sample`):
the operation says which test it is, and its parameters are that test's alone. A new test
is an entry of `TESTS` here and its function in `compute.py`; the four registries pick it
up by themselves (`stat_operations`).

`TESTS` is kept as plain data (names and texts only) because the editor's form mirrors it.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping, NamedTuple

from app.engine.exceptions import EngineError

OP_PREFIX = "stat_"

ALTERNATIVES = ("two_sided", "less", "greater")
DETAILS = ("results", "report")

# Per test: the keys it `needs` and the ones it `takes` besides, each with the kind of value
# it holds (a reader of `_READERS`), and the `defaults` of the keys it takes. Nothing is
# shared between tests: a test that has a parameter of its own lists it here, with its kind.
TESTS: dict[str, dict[str, dict[str, Any]]] = {
    "t_two_sample": {
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"groups": "two_groups", "var_equal": "boolean", "mu": "number",
                  "alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"groups": None, "var_equal": False, "mu": 0.0,
                     "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
}

# Beyond this many groups the column is almost surely the wrong one. It bounds what a test
# reads back from the engine too: a row per group, never more.
MAX_STAT_GROUPS = 200


def operation_name(test: str) -> str:
    """The operation that runs the test `test`."""
    return OP_PREFIX + test


def stat_operations() -> dict[str, str]:
    """Operation → test, for every test there is: what the engines' registries go through."""
    return {operation_name(test): test for test in TESTS}


def is_stat_operation(name: Any) -> bool:
    """Whether `name` is a statistics operation (the lists that treat them alike ask here)."""
    return isinstance(name, str) and name in stat_operations()


class Column(NamedTuple):
    """A column of the step's input as a test sees it: its type as the engine names it, and
    whether it is a number."""

    dtype: str
    numeric: bool


def label_of(v: Any) -> str:
    """A group's label: its value as text, the same on every engine (`pivot_label`). A
    datetime that comes back with a zone is the same instant, naive in UTC, as every engine's
    preview shows it; a zero is «0.0» whatever its sign (the engines that hold −0.0 and 0.0
    one group answer with either)."""
    import datetime as _dt

    from app.engine.operations import pivot_label

    if isinstance(v, _dt.datetime) and v.tzinfo is not None:
        v = v.astimezone(_dt.UTC).replace(tzinfo=None)
    elif isinstance(v, float) and v == 0.0:
        v = 0.0
    return pivot_label(v)


def refusal(test: str, message: str) -> EngineError:
    """A test's refusal, led by its operation's name as every operation's message is."""
    return EngineError(f"{operation_name(test)}: {message}")


# ── one reader per kind of value: the value as the tests use it, or a sentence ──
def _column(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> str:
    if not isinstance(v, str) or not v:
        raise refusal(test, f"'{key}' must be the name of a column")
    if v not in columns:
        raise refusal(test, f"the column «{v}» ('{key}') is not in the step's input")
    return v


def _number_column(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> str:
    name = _column(test, key, v, columns)
    if not columns[name].numeric:
        raise refusal(
            test,
            f"the column «{name}» ('{key}') is not a number ({columns[name].dtype}): "
            "convert it with a cast step, or choose another column",
        )
    return name


def _two_groups(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> list[str]:
    if not isinstance(v, (list, tuple)) or len(v) != 2 or any(g is None or isinstance(g, (list, dict)) for g in v):
        raise refusal(test, f"'{key}' must be the two groups to compare, the first minus the second")
    labels = [g if isinstance(g, str) else label_of(g) for g in v]
    if labels[0] == labels[1]:
        raise refusal(test, f"'{key}' names the group «{labels[0]}» twice: choose two different groups")
    return labels


def _boolean(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> bool:
    if not isinstance(v, bool):
        raise refusal(test, f"'{key}' must be true or false")
    return v


def _number(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise refusal(test, f"'{key}' must be a number")
    return float(v)


def _alpha(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 < v < 1.0:
        raise refusal(test, f"'{key}' must be a number between 0 and 1, both excluded (0.05 is the usual choice)")
    return float(v)


def _one_of(choices: tuple[str, ...]) -> Callable[..., str]:
    def read(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> str:
        if not isinstance(v, str) or v not in choices:
            raise refusal(test, f"'{key}' must be one of {', '.join(choices)}")
        return v

    return read


_READERS: dict[str, Callable[..., Any]] = {
    "column": _column,
    "number_column": _number_column,
    "two_groups": _two_groups,
    "boolean": _boolean,
    "number": _number,
    "alpha": _alpha,
    "alternative": _one_of(ALTERNATIVES),
    "detail": _one_of(DETAILS),
}


def check_params(test: str, params: dict[str, Any] | None, columns: Mapping[str, Column]) -> dict[str, Any]:
    """The parameters of a `test` node, checked against the test's keys and the input's
    columns, with the defaults filled in: every key the test knows is in the answer. A key
    the test does not take is refused, as a key it needs and lacks; a key set to null is a
    key not given."""
    spec = TESTS[test]
    kinds = {**spec["needs"], **spec["takes"]}
    given = {k: v for k, v in (params or {}).items() if v is not None}
    for key in given:
        if key not in kinds:
            raise refusal(test, f"there is no parameter '{key}' (it takes: {', '.join(kinds)})")
    for key in spec["needs"]:
        if key not in given:
            raise refusal(test, f"'{key}' is required")
    return {
        key: _READERS[kind](test, key, given[key], columns) if key in given else spec["defaults"][key]
        for key, kind in kinds.items()
    }
