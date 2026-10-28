"""The Tabularia agent: one set of tools for the chat and for MCP (docs/specs/mcp-agent.md).

- registry.py   the tools: name, description, arguments, level, handler; `call` runs one
- read_tools.py what every token level may use: the catalog, folders, connections, runs
- flow_tools.py the flows' drafts, and the flows' actions of a plan
- plans.py      plans: building them, applying them once a person agrees
- outputs.py    the Outputs' settings, checked as a run checks them
- datasource_tools.py, sql_tools.py, group_tools.py, notebook_tools.py
                datasources from existing connections, SQL scripts, flow groups and
                marimo notebooks: their reading and their actions of a plan
- planning.py   what those share: links, schedules, an object a plan creates first
- questions.py  the chat's questions with choices to click (ask_user)
- chat.py       the tools in the chat: the Assistant page's agent (imported by its route)

Importing the package registers every tool.
"""
from app.services.agent import read_tools, plans, flow_tools  # noqa: F401,I001 — they register the tools, in this order
from app.services.agent import questions  # noqa: F401,I001 — the chat's ask_user
# datasources from existing connections, SQL scripts, flow groups, notebooks
from app.services.agent import datasource_tools, sql_tools, group_tools, notebook_tools  # noqa: F401,I001
from app.services.agent.registry import REGISTRY, Tool, ToolContext, ToolError, UnknownTool, available, call

__all__ = ["REGISTRY", "Tool", "ToolContext", "ToolError", "UnknownTool", "available", "call"]
