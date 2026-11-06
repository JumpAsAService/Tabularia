"""The AI service's internal API: the check of a Script node's Python code that the Go gateway
asks for (POST /scripting/lint). docs/specs/gateway-go.md, «The AI service».

Ruff is a binary the Python package ships, and the imports are read with Python's own `ast`:
neither has a faithful Go equivalent. Mounted only when `APP__ROLE=ai` (main.py), out of the
OpenAPI schema, behind the internal token (routes/internal_sql.py `_internal_only`).

Everything around the check (the environment, who may use it, the size, the rate per person,
the installed modules read from the environment's version) is the Go gateway's; this is the
part of `routes/scripting.py editor_check` from `lint_python` to the sorted findings.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.routing import ReleasingRoute
from app.routes.internal_sql import _internal_only
from app.services import script_lint

router = APIRouter(route_class=ReleasingRoute, prefix="/internal/script", include_in_schema=False, dependencies=[Depends(_internal_only)])


class LintIn(BaseModel):
    path: str
    content: str
    language_version: Optional[str] = None  # the environment's current version's, None without one
    check_imports: bool = False  # the set of installed modules is not empty
    installed: list[str] = []  # its names (the ones that are texts)
    local: list[str] = []  # node_modules of the node's file paths
    environment: str = ""


class LintOut(BaseModel):
    diagnostics: list[dict]


@router.post("/lint", response_model=LintOut)
def lint(body: LintIn) -> LintOut:
    found = script_lint.lint_python(body.path, body.content, body.language_version)
    if body.check_imports:
        found = sorted(found + script_lint.missing_imports(body.content, set(body.installed), set(body.local), body.environment),
                       key=lambda d: (d["line"], d["column"]))
    return LintOut(diagnostics=found)
