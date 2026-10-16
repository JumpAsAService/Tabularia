"""Autoscaling bounds set from Admin → Workers, on Kubernetes only.

One row per kind of worker the administrator has bounded (`run`, `preview`): with
no row the cluster keeps what the Helm chart set. The gateway keeps the rows
applied to the autoscalers (services/kube_scaling.py), so a `helm upgrade` that
puts the chart's values back is undone within a minute; removing the row hands the
bounds back to the chart.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class WorkerScaling(SQLModel, table=True):
    __tablename__ = "worker_scaling"

    kind: str = Field(primary_key=True, max_length=20)  # run | preview
    min_replicas: int
    max_replicas: int
    updated_at: datetime
    # the email of who changed it (the audit has the rest)
    updated_by: Optional[str] = Field(default=None, max_length=320)
