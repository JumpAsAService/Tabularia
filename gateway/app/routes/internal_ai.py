"""The AI service's internal API: the AI comments of a document a flow group's Pushmail cell
sends, which the Go gateway's queue asks for, and the catalog of a provider an administrator
just added, whose chat models the Go gateway then enables. docs/specs/gateway-go.md, «The AI
service».

The comments are the assistant's (services/ai_comment.py, pydantic-ai): they stay in Python.
Mounted only when `APP__ROLE=ai` (main.py), out of the OpenAPI schema, behind the internal
token (routes/internal_sql.py `_internal_only`).

Everything around them (the cell's checks, the documents prepared with the rights of whoever
runs the group, the email's task) is the Go gateway's; this is the part of
`flow_group_runner._cell_comments` that calls `reports.comments_for` for one document.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.core.routing import ReleasingRoute
from app.db.session import engine
from app.models import User
from app.routes.internal_sql import _internal_only

router = APIRouter(route_class=ReleasingRoute, prefix="/internal/ai", include_in_schema=False, dependencies=[Depends(_internal_only)])


class CommentsIn(BaseModel):
    user_id: int  # whoever runs the group: on their bill, with their rights
    definition: dict[str, Any]  # what the engine reads (a dashboard's, as laid out)
    datasets: dict[str, Any]
    params: dict[str, Any]
    given: dict[str, list]  # reports.given_params of the cell's values
    where: Optional[tuple[str, int]] = None  # ("report" | "dashboard", its id)
    title: str = ""
    of_report: bool = True
    words_of: Optional[dict[str, Any]] = None  # a dashboard's content, as the screen has it


class CommentsOut(BaseModel):
    comments: dict[str, Any]
    reading: Optional[str]


@router.post("/comments", response_model=CommentsOut)
async def comments(body: CommentsIn) -> CommentsOut:
    from app.services import reports

    with Session(engine) as s:
        user = s.get(User, body.user_id)
        if user is None:
            raise HTTPException(status_code=422, detail="No such user")
        said, reading = await reports.comments_for(
            s, user, body.definition, body.datasets, body.params, body.given, where=body.where, title=body.title,
            of_report=body.of_report, words_of=body.words_of)
    return CommentsOut(comments=said, reading=reading)


class CatalogIn(BaseModel):
    provider_id: int


class CatalogOut(BaseModel):
    models: list[str] = []  # the names at the provider, as PUT /ai/models checks them
    problem: Optional[str] = None  # the catalog could not be read: why, in words


@router.post("/catalog", response_model=CatalogOut)
async def catalog(body: CatalogIn) -> CatalogOut:
    """A saved provider's catalog, read as the panel and PUT /ai/models read it (its kind's
    reader: Anthropic's SDK, or GET /models), fresh, and kept in the cache they use."""
    from app.services import ai_models

    with Session(engine) as s:
        ep = ai_models.endpoint_of_key(s, f"provider:{body.provider_id}")
    if ep is None:
        raise HTTPException(status_code=422, detail="No such provider")
    try:
        return CatalogOut(models=await ai_models.provider_models(force=True, provider=ep))
    except HTTPException as e:  # 502: the provider's failure; 409: its key can no longer be read
        return CatalogOut(problem=str(e.detail))
