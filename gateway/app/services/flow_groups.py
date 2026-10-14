"""Shared helpers of flow groups (models/flow_group.py), used by the routes and by
the runner."""
import json

from app.models import FlowGroup
from app.schemas.models import FlowGroupDefinition


def definition_of(group: FlowGroup) -> FlowGroupDefinition:
    """The group's grid; an unreadable one reads as empty rather than failing."""
    try:
        return FlowGroupDefinition.model_validate(json.loads(group.definition or "{}"))
    except ValueError:
        return FlowGroupDefinition()
