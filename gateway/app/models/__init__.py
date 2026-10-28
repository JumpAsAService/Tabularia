"""Imports every model so `SQLModel.metadata` knows them at create_all()."""
from app.models.user import User, Group, UserGroupLink
from app.models.user_session import UserSession
from app.models.api_token import ApiToken
from app.models.webhook import Webhook, WebhookDelivery
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
from app.models.engine_setting import EngineSetting
from app.models.worker_scaling import WorkerScaling
from app.models.siem import SiemDestination
from app.models.db_backup import DatabaseBackup
from app.models.gateway_setting import GatewaySetting, SettingsRollout
from app.models.scripting import (
    ScriptEnvironment, ScriptEnvironmentGroup, ScriptEnvironmentVariable, ScriptEnvironmentVersion, ScriptStorage,
)
from app.models.registry import ContainerRegistry, ContainerRegistryAccess, ContainerRegistryVariable
from app.models.saved_script import SavedScript, SavedScriptVersion
from app.models.saved_view import SavedView
from app.models.sql_script import SqlScript, SqlScriptVersion
from app.models.report import Report, ReportVersion, ReportView
from app.models.flow_group import FlowGroup
from app.models.notebook import Notebook, NotebookSession, NotebookTicket, NotebookVersion, NotebookVisitor
from app.models.notebook_read import NotebookRead
from app.models.ai_model import AiModel, AiPrompt
from app.models.ai_chat import AiChat, AiChatTurn, AiSpend
from app.models.agent import AgentDraft, AgentPlan
from app.models.privacy import PrivacyNotice
from app.models.shared_state import FlowPresence, LoginAttempt
from app.models.contract import DataContract, DataContractResult, DataContractVersion
from app.models.dbt_ai_text import DbtAiText

__all__ = [
    "User", "Group", "UserGroupLink", "UserSession", "ApiToken", "Webhook", "WebhookDelivery", "Project", "Permission", "Capability",
    "Flow", "FlowVersion", "Connection", "Datasource", "Run", "Upload", "PendingBlobDeletion",
    "AuditLog", "Banner", "DisabledEngine", "EngineSetting", "WorkerScaling", "SiemDestination", "DatabaseBackup", "GatewaySetting", "SettingsRollout", "ScriptStorage", "ScriptEnvironment", "ScriptEnvironmentVersion",
    "ScriptEnvironmentVariable", "ScriptEnvironmentGroup", "ContainerRegistry", "ContainerRegistryAccess",
    "ContainerRegistryVariable", "SavedScript", "SavedScriptVersion", "SavedView", "SqlScript", "SqlScriptVersion", "Report", "ReportVersion", "ReportView", "Notebook", "NotebookVersion", "NotebookSession", "NotebookVisitor", "NotebookTicket", "NotebookRead", "FlowGroup", "AiModel", "AiPrompt", "AiChat", "AiChatTurn", "AiSpend", "AgentDraft", "AgentPlan", "PrivacyNotice",
    "FlowPresence", "LoginAttempt",
    "DataContract", "DataContractResult", "DataContractVersion", "DbtAiText",
]
