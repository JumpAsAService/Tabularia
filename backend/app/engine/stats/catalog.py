"""The catalogue of the hypothesis tests: the one table every engine registers its
statistics operations from, what each test asks of its node's parameters, and the limits.

Every test is an operation of its own, named `stat_` + the test's id (`stat_t_two_sample`):
the operation says which test it is, and its parameters are that test's alone. A new test
is an entry of `TESTS` here and its function in `compute.py`; the four registries pick it
up by themselves (`stat_operations`).

`TESTS` is kept as plain data (names and texts only) because the editor's form mirrors it:
`snapshot()` is the catalogue as the other languages read it (`catalog.json`, beside this
file, and its copies in the interface, in the Go gateway and in the AI service). After a
change here, write it again:

    python -m app.engine.stats.catalog            # the snapshot
    python3 .github/scripts/stat_catalog.py       # its copies (from the repository's root)

(Python warns that the module is found twice when it runs: the engines' registries import
it too. Nothing is shared between the two, and the one that runs only writes the file.)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

from app.engine.exceptions import EngineError

OP_PREFIX = "stat_"

ALTERNATIVES = ("two_sided", "less", "greater")
DETAILS = ("results", "report")

# The families the tests are shown by, in the palette's order.
FAMILIES = ("means", "variances", "proportions", "correlation", "ranks", "distributions")

# Per test: its `family`, the keys it `needs` and the ones it `takes` besides, each with the
# kind of value it holds (a reader of `_READERS`, a control of `KINDS`), and the `defaults` of
# the keys it takes. Nothing is shared between tests: a test that has a parameter of its own
# lists it here, with its kind. The keys are shown in this order by the node's form.
#
# A test whose options exclude each other says so in `forbids`, a list of
#     {"key": "posthoc", "value": "tukey", "when": {"var_equal": False},
#      "reason": "tukey_equal_variances", "message": "Tukey's HSD assumes equal variances: …"}
# — `key` may not be `value` while every key of `when` has that value (the defaults count).
# The node refuses it with `message`; the form disables the choice and says why in the
# person's language (`stat.reasons.<reason>` in the interface's catalogues).
TESTS: dict[str, dict[str, Any]] = {
    "t_two_sample": {
        "family": "means",
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

# The controls a form has to draw: a column of the step's input, some of the values a column
# holds, a switch, a number, one of a few choices.
CONTROLS = ("column", "levels", "boolean", "number", "choice")

# How a form asks for each kind of value: its control, and what the control needs to know.
#   column   `numeric`: only the columns that are numbers
#   levels   `count` values of the column the key `of` names (its groups)
#   number   `above`, `below`: the bounds, both excluded
#   choice   `choices`, in the order shown
# `hidden`: a key the node sets by itself, never asked in the form.
KINDS: dict[str, dict[str, Any]] = {
    "column": {"control": "column"},
    "number_column": {"control": "column", "numeric": True},
    "two_groups": {"control": "levels", "of": "group", "count": 2},
    "boolean": {"control": "boolean"},
    "number": {"control": "number"},
    "alpha": {"control": "number", "above": 0, "below": 1},
    "alternative": {"control": "choice", "choices": list(ALTERNATIVES)},
    "detail": {"control": "choice", "choices": list(DETAILS), "hidden": True},
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
    checked = {
        key: _READERS[kind](test, key, given[key], columns) if key in given else spec["defaults"][key]
        for key, kind in kinds.items()
    }
    for rule in spec.get("forbids", ()):
        if checked[rule["key"]] == rule["value"] and all(checked[k] == v for k, v in rule["when"].items()):
            raise refusal(test, rule["message"])
    return checked


# ── the snapshot: the catalogue as the other languages read it ──────────────
SNAPSHOT = Path(__file__).with_name("catalog.json")


def snapshot() -> dict[str, Any]:
    """The catalogue as plain data: the families, the controls and the kinds, and every test
    in its order with its operation, its keys, their defaults and what it forbids."""
    return {
        "prefix": OP_PREFIX,
        "families": list(FAMILIES),
        "controls": list(CONTROLS),
        "kinds": KINDS,
        "tests": [
            {
                "id": test,
                "operation": operation_name(test),
                "family": spec["family"],
                "needs": spec["needs"],
                "takes": spec["takes"],
                "defaults": spec["defaults"],
                "forbids": list(spec.get("forbids", ())),
            }
            for test, spec in TESTS.items()
        ],
    }


def snapshot_text() -> str:
    """The snapshot as it is written: `catalog.json` and its copies hold exactly this."""
    return json.dumps(snapshot(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    SNAPSHOT.write_text(snapshot_text(), encoding="utf-8")
    print(f"{SNAPSHOT.name}: {len(TESTS)} test{'' if len(TESTS) == 1 else 's'} written")
