"""The optional kwargs are NOT sent when not set.

Celery serializes by name: if the upgraded API sends a parameter the still-old worker
lacks in its signature, the task dies of TypeError BEFORE starting — every run failed for
the whole window of a rolling upgrade, and of a rollback. It happened three times in this
repo (`mirror`, then `sort_keys` on preview_task, then `mirror`+`email`+`engine` together
here), each time because the fix had touched one call and not its sisters.

The test does not look at today's names: it takes the signature the worker had BEFORE
these features and checks that what the route sends fits it. A new kwarg sent
unconditionally fails it — exactly the error one would see in production during the
upgrade.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.api.models import TransformDataRequest
from app.api.routes import tasks as route


class _Spy:
    """Stands in for the task: records the kwargs instead of really queueing."""

    def __init__(self):
        self.kwargs: dict[str, Any] | None = None

    def delay(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"id": "task-finto"})()


def _yesterdays_worker(bucket, input_key, operations, output_key, destination=None):
    """The signature BEFORE mirror/email/engine: the worker that lives a few minutes next to
    the new API during a rolling update."""


def _send(monkeypatch, **fields) -> dict:
    spy = _Spy()
    monkeypatch.setattr(route, "transform_data_task", spy)
    req = TransformDataRequest(
        bucket="b", input_key="in.parquet", output_key="out.parquet", operations=[], **fields
    )
    route.submit_transform_data_task(req)
    assert spy.kwargs is not None
    return spy.kwargs


def test_a_plain_request_still_binds_to_the_previous_worker_signature(monkeypatch):
    """The rolling update's case: no new feature in use."""
    sent = _send(monkeypatch)
    inspect.signature(_yesterdays_worker).bind(**sent)  # raises when there is a kwarg too many


def test_optional_kwargs_are_omitted_when_empty(monkeypatch):
    sent = _send(monkeypatch)
    assert set(sent) == {"bucket", "input_key", "operations", "output_key"}, sent


@pytest.mark.parametrize(
    "field, value",
    [
        ("email", {"connection": {}, "target": {}}),
        ("mirror", {"connection": {}, "target": {}}),
        ("engine", "polars"),
        ("destination", {"type": "s3"}),
    ],
)
def test_a_valued_option_is_sent(monkeypatch, field, value):
    """Leaving out the empty ones must not mean losing those needed."""
    sent = _send(monkeypatch, **{field: value})
    assert sent[field] == value
