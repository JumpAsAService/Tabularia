"""The catalogue of the hypothesis tests as the one list every other list is made from: the
snapshot the other languages read (`app/engine/stats/catalog.json`) is what `catalog.py`
says, every test has a family and every kind of value a control a form can draw, and what a
test forbids is refused by the node with the catalogue's own sentence.

The copies of the snapshot (the interface's, the Go gateway's, the AI service's) are checked
by `.github/scripts/stat_catalog.py --check`, which needs nothing installed.
"""
from __future__ import annotations

import json

import pytest

from app.engine.exceptions import EngineError
from app.engine.stats import catalog
from app.engine.stats.catalog import Column, check_params

COLUMNS = {"amount": Column("Float64", True), "channel": Column("String", False)}


def test_the_snapshot_is_what_the_catalogue_says():
    written = catalog.SNAPSHOT.read_text(encoding="utf-8")
    assert written == catalog.snapshot_text(), (
        "app/engine/stats/catalog.json is out of date: run `python -m app.engine.stats.catalog`, "
        "then `python3 .github/scripts/stat_catalog.py` for its copies"
    )
    assert json.loads(written) == json.loads(json.dumps(catalog.snapshot()))


def test_the_snapshot_lists_every_test_in_the_catalogue_s_order():
    snap = catalog.snapshot()
    assert snap["prefix"] == catalog.OP_PREFIX
    assert [t["id"] for t in snap["tests"]] == list(catalog.TESTS)
    assert [t["operation"] for t in snap["tests"]] == list(catalog.stat_operations())
    for entry in snap["tests"]:
        assert set(entry) == {"id", "operation", "family", "needs", "takes", "defaults", "forbids"}
        spec = catalog.TESTS[entry["id"]]
        # the keys in the order the form shows them
        assert list(entry["needs"]) == list(spec["needs"]) and list(entry["takes"]) == list(spec["takes"])


def test_every_test_has_one_of_the_six_families():
    assert len(catalog.FAMILIES) == len(set(catalog.FAMILIES)) == 6
    for test, spec in catalog.TESTS.items():
        assert spec["family"] in catalog.FAMILIES, test


def test_every_kind_of_value_has_a_reader_and_a_control_a_form_can_draw():
    assert set(catalog.KINDS) == set(catalog._READERS)
    for kind, how in catalog.KINDS.items():
        assert how["control"] in catalog.CONTROLS, kind
        assert set(how) <= {"control", "numeric", "of", "count", "above", "below", "choices", "hidden"}, kind
        if how["control"] == "choice":
            # the form offers exactly what the node accepts
            assert how["choices"] and all(isinstance(c, str) for c in how["choices"]), kind
            for choice in how["choices"]:
                assert catalog._READERS[kind]("t_two_sample", "k", choice, {}) == choice
            with pytest.raises(EngineError, match="must be one of " + ", ".join(how["choices"])):
                catalog._READERS[kind]("t_two_sample", "k", "nope", {})
        if how["control"] == "levels":
            assert isinstance(how["count"], int) and how["count"] >= 1, kind
    # a control nobody uses would be a control nobody tests
    assert {how["control"] for how in catalog.KINDS.values()} == set(catalog.CONTROLS)


def test_a_form_can_draw_every_key_of_every_test():
    for test, spec in catalog.TESTS.items():
        kinds = {**spec["needs"], **spec["takes"]}
        for key, kind in kinds.items():
            how = catalog.KINDS[kind]
            if how["control"] == "levels":
                # the column whose values are listed is a key of the same test
                assert catalog.KINDS[kinds[how["of"]]]["control"] == "column", (test, key)
            # a key the form never asks must have a default the node can run with
            if how.get("hidden"):
                assert key in spec["defaults"] and spec["defaults"][key] is not None, (test, key)
        # what a new node starts with is what the node accepts
        given = {k: v for k, v in spec["defaults"].items() if v is not None}
        for key, value in given.items():
            assert catalog._READERS[kinds[key]](test, key, value, {}) == value, (test, key)
        for rule in spec.get("forbids", ()):
            assert set(rule) == {"key", "value", "when", "reason", "message"}, test
            assert rule["key"] in kinds and set(rule["when"]) <= set(kinds) and rule["when"], test
            assert rule["key"] not in rule["when"], test
            assert rule["reason"].isidentifier() and rule["message"], test


def test_what_a_test_forbids_is_refused_with_its_sentence(monkeypatch):
    spec = {
        **catalog.TESTS["t_two_sample"],
        "forbids": [{"key": "alternative", "value": "less", "when": {"var_equal": True},
                     "reason": "less_needs_welch", "message": "'less' is only there for unequal variances"}],
    }
    monkeypatch.setitem(catalog.TESTS, "t_two_sample", spec)
    base = {"value": "amount", "group": "channel"}
    assert check_params("t_two_sample", {**base, "alternative": "less"}, COLUMNS)["alternative"] == "less"
    assert check_params("t_two_sample", {**base, "var_equal": True}, COLUMNS)["var_equal"] is True
    with pytest.raises(EngineError) as e:
        check_params("t_two_sample", {**base, "var_equal": True, "alternative": "less"}, COLUMNS)
    assert str(e.value) == "stat_t_two_sample: 'less' is only there for unequal variances"
    assert catalog.snapshot()["tests"][0]["forbids"] == spec["forbids"]


def test_a_default_counts_for_what_a_test_forbids(monkeypatch):
    # `var_equal` is false until given: what is forbidden «while it is false» is forbidden without it
    spec = {
        **catalog.TESTS["t_two_sample"],
        "forbids": [{"key": "alternative", "value": "greater", "when": {"var_equal": False},
                     "reason": "greater_needs_student", "message": "'greater' needs equal variances"}],
    }
    monkeypatch.setitem(catalog.TESTS, "t_two_sample", spec)
    with pytest.raises(EngineError, match="'greater' needs equal variances"):
        check_params("t_two_sample", {"value": "amount", "group": "channel", "alternative": "greater"}, COLUMNS)
