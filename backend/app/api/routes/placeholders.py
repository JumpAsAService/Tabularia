"""The placeholders' checker and live preview (INTERNAL endpoint, reached by the gateway only).

POST /placeholders/preview resolves templates NOW — the folder and pattern of a
file datasource, the file name, folder, key or subject of an Output — and says
for each one its value («export_20261018.csv») or the message of what is wrong,
the same message the datasource's validation or the run would give. The forms
call it while the user types; the gateway calls it before launching a run.
Nothing is read or written anywhere: it is arithmetic on dates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core import placeholders as ph
from app.core.config import get_settings

router = APIRouter(prefix="/placeholders", tags=["placeholders"])

Target = Literal["file", "path", "text"]


class PreviewRequest(BaseModel):
    templates: dict[str, str] = Field(max_length=20)
    kind: Literal["input", "output"] = "input"
    timezone: str = Field(default="UTC", max_length=64)
    # inputs: the datasource's variables and {last_refresh}
    variables: list[ph.Variable] = Field(default_factory=list, max_length=10)
    last_refresh_initial: str = Field(default="", max_length=100)
    last_refresh: str | None = None
    # outputs: what each template is (a file name, a folder or key, a text) and the flow
    targets: dict[str, Target] = Field(default_factory=dict)
    flow: str = Field(default="", max_length=500)
    run_id: int | None = None


class FieldPreview(BaseModel):
    value: str | None = None
    last: str | None = None  # inputs with variables: the last expansion
    error: str | None = None


class PreviewAnswer(BaseModel):
    fields: dict[str, FieldPreview]
    expansions: int = 1
    error: str | None = None  # the zone, the variables: not one template's fault
    now: str


def _input(req: PreviewRequest, now: datetime) -> PreviewAnswer:
    answer = PreviewAnswer(fields={}, now=now.isoformat(timespec="seconds"))
    try:
        ph.zone(req.timezone)
        ph.check_variables(req.variables)
        has_initial = bool(req.last_refresh_initial.strip())
        if has_initial:
            ph.resolve_initial(req.last_refresh_initial, now, ph.zone(req.timezone))
        base = ph.make_context(now, req.timezone, req.last_refresh, req.last_refresh_initial, {})
        combos = ph.combinations(req.variables, base, get_settings().placeholders.max_values)
    except ph.PlaceholderError as e:
        answer.error = str(e)
        return answer
    lists, ranges = ph.variable_names(req.variables)
    answer.expansions = len(combos)
    for name, template in req.templates.items():
        try:
            ph.check(template, lists, has_initial, ranges=ranges)
            first = ph.render(template, ph.with_values(base, combos[0]))
            last = ph.render(template, ph.with_values(base, combos[-1])) if len(combos) > 1 else None
            answer.fields[name] = FieldPreview(value=first, last=last if last != first else None)
        except ph.PlaceholderError as e:
            answer.fields[name] = FieldPreview(error=str(e))
    return answer


def _output(req: PreviewRequest, now: datetime) -> PreviewAnswer:
    answer = PreviewAnswer(fields={}, now=now.isoformat(timespec="seconds"))
    try:
        ctx = ph.output_context(now, req.timezone, req.run_id, req.flow)
    except ph.PlaceholderError as e:
        answer.error = str(e)
        return answer
    if req.run_id is None:
        ctx.variables["run_id"] = "{run_id}"  # known only when the flow runs: shown as it is
    for name, template in req.templates.items():
        try:
            ph.check(template, output=True)
            answer.fields[name] = FieldPreview(
                value=ph.render_output(template, ctx, req.targets.get(name, "text"), name.replace("_", " ")))
        except ph.PlaceholderError as e:
            answer.fields[name] = FieldPreview(error=str(e))
    return answer


@router.post("/preview", response_model=PreviewAnswer)
def preview(req: PreviewRequest) -> PreviewAnswer:
    now = datetime.now(timezone.utc)
    return _input(req, now) if req.kind == "input" else _output(req, now)
