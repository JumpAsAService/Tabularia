"""Importa tutti i modelli così `SQLModel.metadata` li conosce a create_all()."""
from app.models.user import User, Group, UserGroupLink
from app.models.project import Project
from app.models.permission import Permission, Capability
from app.models.flow import Flow
from app.models.connection import Connection
from app.models.datasource import Datasource
from app.models.run import Run
from app.models.upload import Upload
from app.models.blob_deletion import PendingBlobDeletion

__all__ = [
    "User", "Group", "UserGroupLink", "Project", "Permission", "Capability",
    "Flow", "Connection", "Datasource", "Run", "Upload", "PendingBlobDeletion",
]
