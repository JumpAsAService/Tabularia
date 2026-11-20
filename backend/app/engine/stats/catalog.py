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
#
# A key that matters only with some values of another says so in `applies`, as
#     {"p_adjust": {"posthoc": ["pairwise_t"]}}
# — the form shows the key only then (the defaults count); the node ignores it otherwise.
TESTS: dict[str, dict[str, Any]] = {
    "t_one_sample": {
        "family": "means",
        "needs": {"value": "number_column"},
        "takes": {"mu": "number", "alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"mu": 0.0, "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "t_two_sample": {
        "family": "means",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"groups": "two_groups", "var_equal": "boolean", "mu": "number",
                  "alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"groups": None, "var_equal": False, "mu": 0.0,
                     "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "t_paired": {
        "family": "means",
        "needs": {"x": "number_column", "y": "number_column"},
        "takes": {"mu": "number", "alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"mu": 0.0, "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "anova": {
        "family": "means",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"var_equal": "boolean", "posthoc": "anova_posthoc", "p_adjust": "p_adjust",
                  "alpha": "alpha", "detail": "detail"},
        "defaults": {"var_equal": True, "posthoc": "none", "p_adjust": "holm", "alpha": 0.05, "detail": "results"},
        "forbids": [
            {"key": "posthoc", "value": "tukey", "when": {"var_equal": False}, "reason": "tukey_equal_variances",
             "message": "Tukey's HSD assumes equal variances: with Welch's ANOVA (var_equal = false) use Games–Howell"},
            {"key": "posthoc", "value": "games_howell", "when": {"var_equal": True}, "reason": "games_howell_unequal_variances",
             "message": "Games–Howell is for unequal variances: with the classic ANOVA (var_equal = true) use Tukey's HSD"},
        ],
        "applies": {"p_adjust": {"posthoc": ["pairwise_t"]}},
    },
    "var_f": {
        "family": "variances",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"groups": "two_groups", "ratio": "positive", "alternative": "alternative", "alpha": "alpha",
                  "detail": "detail"},
        "defaults": {"groups": None, "ratio": 1.0, "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "levene": {
        "family": "variances",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"center": "levene_center", "alpha": "alpha", "detail": "detail"},
        "defaults": {"center": "median", "alpha": 0.05, "detail": "results"},
    },
    "prop_one_sample": {
        "family": "proportions",
        "needs": {"value": "column", "success": "success"},
        "takes": {"p0": "proportion", "method": "prop_method", "correction": "boolean", "alternative": "alternative",
                  "alpha": "alpha", "detail": "detail"},
        "defaults": {"p0": 0.5, "method": "score", "correction": True, "alternative": "two_sided", "alpha": 0.05,
                     "detail": "results"},
        "applies": {"correction": {"method": ["score"]}},
    },
    "prop_two_sample": {
        "family": "proportions",
        "needs": {"value": "column", "success": "success", "group": "column"},
        "takes": {"groups": "two_groups", "correction": "boolean", "alternative": "alternative", "alpha": "alpha",
                  "detail": "detail"},
        "defaults": {"groups": None, "correction": True, "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "chi2_independence": {
        "family": "proportions",
        "needs": {"x": "column", "y": "column"},
        "takes": {"correction": "boolean", "posthoc": "chi2_posthoc", "p_adjust": "p_adjust", "alpha": "alpha",
                  "detail": "detail"},
        "defaults": {"correction": True, "posthoc": "none", "p_adjust": "holm", "alpha": 0.05, "detail": "results"},
        "applies": {"p_adjust": {"posthoc": ["cells", "pairs"]}},
    },
    "chi2_gof": {
        "family": "proportions",
        "needs": {"value": "column"},
        "takes": {"expected": "expected_proportions", "alpha": "alpha", "detail": "detail"},
        "defaults": {"expected": None, "alpha": 0.05, "detail": "results"},
    },
    "fisher_exact": {
        "family": "proportions",
        "needs": {"x": "column", "y": "column"},
        "takes": {"alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "mcnemar": {
        "family": "proportions",
        "needs": {"x": "column", "y": "column"},
        "takes": {"method": "mcnemar_method", "correction": "boolean", "alpha": "alpha", "detail": "detail"},
        "defaults": {"method": "auto", "correction": True, "alpha": 0.05, "detail": "results"},
        "applies": {"correction": {"method": ["auto", "asymptotic"]}},
    },
    "cmh": {
        "family": "proportions",
        "needs": {"x": "column", "y": "column", "strata": "column"},
        "takes": {"correction": "boolean", "alpha": "alpha", "detail": "detail"},
        "defaults": {"correction": True, "alpha": 0.05, "detail": "results"},
    },
    "g_test": {
        "family": "proportions",
        "needs": {"x": "column", "y": "column"},
        "takes": {"williams": "boolean", "posthoc": "g_posthoc", "p_adjust": "p_adjust", "alpha": "alpha", "detail": "detail"},
        "defaults": {"williams": False, "posthoc": "none", "p_adjust": "holm", "alpha": 0.05, "detail": "results"},
        "applies": {"p_adjust": {"posthoc": ["cells"]}},
    },
    "pearson": {
        "family": "correlation",
        "needs": {"x": "number_column", "y": "number_column"},
        "takes": {"alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "spearman": {
        "family": "correlation",
        "needs": {"x": "number_column", "y": "number_column"},
        "takes": {"alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "mann_whitney": {
        "family": "ranks",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"groups": "two_groups", "method": "rank_method", "correction": "boolean", "alternative": "alternative",
                  "alpha": "alpha", "detail": "detail"},
        "defaults": {"groups": None, "method": "auto", "correction": True, "alternative": "two_sided", "alpha": 0.05,
                     "detail": "results"},
        "applies": {"correction": {"method": ["auto", "asymptotic"]}},
    },
    "wilcoxon_signed_rank": {
        "family": "ranks",
        "needs": {"x": "number_column"},
        "takes": {"y": "number_column", "mu": "number", "zeros": "wilcoxon_zeros", "method": "rank_method",
                  "correction": "boolean", "alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"y": None, "mu": 0.0, "zeros": "wilcox", "method": "auto", "correction": True,
                     "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
        "applies": {"correction": {"method": ["auto", "asymptotic"]}},
    },
    "kruskal_wallis": {
        "family": "ranks",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"posthoc": "kw_posthoc", "p_adjust": "p_adjust", "alpha": "alpha", "detail": "detail"},
        "defaults": {"posthoc": "none", "p_adjust": "holm", "alpha": 0.05, "detail": "results"},
        "applies": {"p_adjust": {"posthoc": ["dunn"]}},
    },
    "ks_two_sample": {
        "family": "distributions",
        "needs": {"value": "number_column", "group": "column"},
        "takes": {"groups": "two_groups", "method": "ks_method", "alternative": "alternative", "alpha": "alpha",
                  "detail": "detail"},
        "defaults": {"groups": None, "method": "auto", "alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
    "normality": {
        "family": "distributions",
        "needs": {"value": "number_column"},
        "takes": {"method": "normality_method", "alpha": "alpha", "detail": "detail"},
        "defaults": {"method": "dagostino", "alpha": 0.05, "detail": "results"},
    },
}

# The choices of the tests' own options.
ANOVA_POSTHOC = ("none", "tukey", "games_howell", "pairwise_t")
P_ADJUST = ("holm", "bonferroni", "sidak", "none")
LEVENE_CENTER = ("median", "mean")
PROP_METHOD = ("score", "exact")
CHI2_POSTHOC = ("none", "cells", "pairs")
G_POSTHOC = ("none", "cells")
MCNEMAR_METHOD = ("auto", "exact", "asymptotic")
RANK_METHOD = ("auto", "exact", "asymptotic")
WILCOXON_ZEROS = ("wilcox", "pratt")
KW_POSTHOC = ("none", "dunn")
KS_METHOD = ("auto", "exact", "asymptotic")
NORMALITY_METHOD = ("dagostino", "jarque_bera")

# Beyond this many groups the column is almost surely the wrong one. It bounds what a test
# reads back from the engine too: a row per group, never more.
MAX_STAT_GROUPS = 200
# Groups compared two by two: 190 pairs at most.
MAX_POSTHOC_GROUPS = 20
# Levels of each variable of a contingency table: 2,500 cells at most.
MAX_STAT_LEVELS = 50
# The most outcomes an exact binomial or Fisher computation goes through (n + 1; the values
# the first cell of a 2×2 table may take with its margins).
EXACT_SUPPORT = 1_000_000
# The most steps Fisher's exact test of an r × c table takes (the network algorithm,
# `exact.fisher_rxc`: each cell of a way of filling a column, each path extended, each node
# remembered, each line bounded), counted as they are taken; a table whose own path through
# the network already costs more is refused before the count. Measured on the development
# machine in the backend's image (2026-11-20) on tables that reach it — 3 × 3, 3 × 4, 3 × 5,
# 4 × 4, 6 × 6, 7 × 7: refused after 4.2 s at most, and 235 MB above the process's own; a 3 × 3
# table of 242 rows takes 2.6 million steps (2.7 s), an 8 × 8 one of 144 rows is refused at once.
EXACT_NETWORK = 4_000_000
# The largest sample of the exact Mann–Whitney and signed-rank distributions (R's: below 50),
# and the largest n1·n2 of the exact two-sample Kolmogorov–Smirnov one (R's: below 10,000).
# Past them, or with ties, the normal (Kolmogorov's) approximation.
EXACT_MAX_N = 49
EXACT_KS_PRODUCT = 9_999

# The rows a report's charts draw (`detail = report`): the points of the reference
# distribution's curve, and the intervals of a histogram, between the two whatever the rows.
CURVE_POINTS = 201
MIN_BINS = 5
MAX_BINS = 30


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


def _positive(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise refusal(test, f"'{key}' must be a number above 0")
    return float(v)


def _alpha(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 < v < 1.0:
        raise refusal(test, f"'{key}' must be a number between 0 and 1, both excluded (0.05 is the usual choice)")
    return float(v)


def _proportion(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 < v < 1.0:
        raise refusal(test, f"'{key}' must be a proportion between 0 and 1, both excluded")
    return float(v)


def _success(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> Any:
    """The value of the outcome column that counts as a success: a text, a number or true or
    false, as a person writes it or a column holds it. It is typed by the column's kind when
    the engine is asked (`aggregates.typed_value`)."""
    if isinstance(v, str):
        if not v.strip():
            raise refusal(test, f"'{key}' must be the value that counts as a success")
        return v
    if isinstance(v, bool) or (isinstance(v, (int, float)) and math.isfinite(v)):
        return v
    raise refusal(test, f"'{key}' must be the value that counts as a success: a text, a number, true or false")


def _expected_proportions(test: str, key: str, v: Any, columns: Mapping[str, Column]) -> dict[str, float]:
    """The proportion each category is expected to have: {category: proportion}, each above 0,
    summing to 1 (within 1e-9). Whether they are exactly the categories found is the data's
    to say."""
    if not isinstance(v, dict) or not v:
        raise refusal(test, f"'{key}' must give each category its expected proportion, as {{category: proportion}}")
    out: dict[str, float] = {}
    for label, share in v.items():
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not math.isfinite(share) or share <= 0:
            raise refusal(test, f"'{key}': the proportion of «{label}» must be a number above 0")
        out[str(label)] = float(share)
    total = math.fsum(out.values())
    if abs(total - 1.0) > 1e-9:
        raise refusal(test, f"'{key}': the proportions sum to {total:.12g}, and must sum to 1")
    return out


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
    "positive": _positive,
    "alpha": _alpha,
    "alternative": _one_of(ALTERNATIVES),
    "anova_posthoc": _one_of(ANOVA_POSTHOC),
    "p_adjust": _one_of(P_ADJUST),
    "levene_center": _one_of(LEVENE_CENTER),
    "proportion": _proportion,
    "success": _success,
    "prop_method": _one_of(PROP_METHOD),
    "chi2_posthoc": _one_of(CHI2_POSTHOC),
    "g_posthoc": _one_of(G_POSTHOC),
    "mcnemar_method": _one_of(MCNEMAR_METHOD),
    "rank_method": _one_of(RANK_METHOD),
    "wilcoxon_zeros": _one_of(WILCOXON_ZEROS),
    "kw_posthoc": _one_of(KW_POSTHOC),
    "ks_method": _one_of(KS_METHOD),
    "normality_method": _one_of(NORMALITY_METHOD),
    "expected_proportions": _expected_proportions,
    "detail": _one_of(DETAILS),
}

# The controls a form has to draw: a column of the step's input, some of the values a column
# holds, a switch, a number, one of a few choices; one value of a column (chosen among its
# values or written), a proportion for each value of a column.
CONTROLS = ("column", "levels", "boolean", "number", "choice", "level", "proportions")

# How a form asks for each kind of value: its control, and what the control needs to know.
#   column       `numeric`: only the columns that are numbers
#   levels       `count` values of the column the key `of` names (its groups)
#   number       `above`, `below`: the bounds, both excluded
#   choice       `choices`, in the order shown
#   level        one value of the column the key `of` names: one of those it holds, or written
#   proportions  a proportion above 0 for each value of the column `of` names, summing to 1;
#                nothing given: all equal
# `hidden`: a key the node sets by itself, never asked in the form.
KINDS: dict[str, dict[str, Any]] = {
    "column": {"control": "column"},
    "number_column": {"control": "column", "numeric": True},
    "two_groups": {"control": "levels", "of": "group", "count": 2},
    "boolean": {"control": "boolean"},
    "number": {"control": "number"},
    "positive": {"control": "number", "above": 0},
    "alpha": {"control": "number", "above": 0, "below": 1},
    "alternative": {"control": "choice", "choices": list(ALTERNATIVES)},
    "anova_posthoc": {"control": "choice", "choices": list(ANOVA_POSTHOC)},
    "p_adjust": {"control": "choice", "choices": list(P_ADJUST)},
    "levene_center": {"control": "choice", "choices": list(LEVENE_CENTER)},
    "proportion": {"control": "number", "above": 0, "below": 1},
    "success": {"control": "level", "of": "value"},
    "prop_method": {"control": "choice", "choices": list(PROP_METHOD)},
    "chi2_posthoc": {"control": "choice", "choices": list(CHI2_POSTHOC)},
    "g_posthoc": {"control": "choice", "choices": list(G_POSTHOC)},
    "mcnemar_method": {"control": "choice", "choices": list(MCNEMAR_METHOD)},
    "rank_method": {"control": "choice", "choices": list(RANK_METHOD)},
    "wilcoxon_zeros": {"control": "choice", "choices": list(WILCOXON_ZEROS)},
    "kw_posthoc": {"control": "choice", "choices": list(KW_POSTHOC)},
    "ks_method": {"control": "choice", "choices": list(KS_METHOD)},
    "normality_method": {"control": "choice", "choices": list(NORMALITY_METHOD)},
    "expected_proportions": {"control": "proportions", "of": "value"},
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


def applies(test: str, key: str, checked: Mapping[str, Any]) -> bool:
    """Whether the key `key` matters with the other parameters as they are (`applies`)."""
    rule = TESTS[test].get("applies", {}).get(key)
    return rule is None or all(checked.get(k) in values for k, values in rule.items())


# ── the snapshot: the catalogue as the other languages read it ──────────────
SNAPSHOT = Path(__file__).with_name("catalog.json")


def snapshot() -> dict[str, Any]:
    """The catalogue as plain data: the families, the controls and the kinds, and every test
    in its order with its operation, its keys, their defaults, what it forbids and the keys
    that matter only with some values of another."""
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
                "applies": dict(spec.get("applies", {})),
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
