"""Importa tutti i modelli così `SQLModel.metadata` li conosce a create_all()."""
from app.models.user import User, Group, UserGroupLink
from app.models.project import Project
from app.models.permission import Permission, Capability
from app.models.flow import Flow

__all__ = ["User", "Group", "UserGroupLink", "Project", "Permission", "Capability", "Flow"]
