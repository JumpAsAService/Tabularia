"""Importa tutti i modelli così `SQLModel.metadata` li conosce a create_all()."""
from app.models.user import User, Group, UserGroupLink
from app.models.project import Project
from app.models.permission import Permission, Capability
from app.models.flow import Flow
from app.models.flow_version import FlowVersion
from app.models.connection import Connection
from app.models.datasource import Datasource
from app.models.run import Run
from app.models.upload import Upload
from app.models.blob_deletion import PendingBlobDeletion
from app.models.audit import AuditLog
from app.models.banner import Banner
from app.models.engine_policy import DisabledEngine
from app.models.saved_view import SavedView
from app.models.ai_model import AiModel
from app.models.ai_chat import AiChat, AiChatTurn, AiSpend
from app.models.privacy import PrivacyNotice
from app.models.shared_state import FlowPresence, LoginAttempt
from app.models.contract import DataContract, DataContractResult, DataContractVersion
from app.models.dbt_ai_text import DbtAiText

__all__ = [
    "User", "Group", "UserGroupLink", "Project", "Permission", "Capability",
    "Flow", "FlowVersion", "Connection", "Datasource", "Run", "Upload", "PendingBlobDeletion",
    "AuditLog", "Banner", "DisabledEngine", "SavedView", "AiModel", "AiChat", "AiChatTurn", "AiSpend", "PrivacyNotice",
    "FlowPresence", "LoginAttempt",
    "DataContract", "DataContractResult", "DataContractVersion", "DbtAiText",
]
