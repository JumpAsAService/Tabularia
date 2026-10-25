"""Saved views: a NAMED configuration of the Viewer, inside a folder.

The Viewer builds filters, computed fields and pivots on the fly on a datasource and
saves nothing. A saved view is that same configuration put in the catalog next to
flows, datasources and connections — so with a name, a folder and permissions
inherited from the project, like everything else.

Two properties chosen explicitly:

- **the view FOLLOWS the data, it does not freeze it.** A reference to the datasource
  and the configuration are stored, never rows: reopening it tomorrow reads the
  current snapshot again. It is a lens, not a copy — and for that it has no blob, no
  snapshot to replace, nothing for `blobgc` to collect.
- **it is shared, not personal.** It sits in a folder, so anyone with VIEW on that
  folder sees it; changing or deleting it requires EDIT, as for the flows.
  `owner_id` says who created it, not who may see it.

NB on the name: in the code it stays `SavedView`, never `View` — `VIEW` is the name of
a capability already (`Capability.VIEW`) and mixing them up in the permissions would
invite mistakes. In the interface it is simply called «view».
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SavedView(SQLModel, table=True):
    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_saved_view_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    # the datasource the view applies to. No cascading deletion: when the datasource goes
    # the view stays and says so when opened, instead of vanishing silently from someone
    # else's folder.
    datasource_id: int = Field(foreign_key="datasources.id", index=True)

    # The Viewer's configuration in JSON: engine, filters, computed fields, pivot. Opaque
    # text for the gateway — the FRONTEND owns its shape, and the gateway only checks it is
    # valid JSON. So adding a control to the view requires no migration here.
    spec: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
