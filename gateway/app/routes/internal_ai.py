"""The AI service's internal API: the AI comments of a document a flow group's Pushmail cell
sends, which the Go gateway's queue asks for. docs/specs/gateway-go.md, «The AI service».

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
