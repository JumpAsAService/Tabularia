"""The Tabularia agent: one set of tools for the chat and for MCP (docs/specs/mcp-agent.md).

- registry.py   the tools: name, description, arguments, level, handler; `call` runs one
- read_tools.py what every token level may use: the catalog, folders, connections, runs
- flow_tools.py the flows' drafts, and the flows' actions of a plan
- plans.py      plans: building them, applying them once a person agrees
- outputs.py    the Outputs' settings, checked as a run checks them

Importing the package registers every tool.
"""
from app.services.agent import read_tools, plans, flow_tools  # noqa: F401,I001 — they register the tools, in this order
from app.services.agent.registry import REGISTRY, Tool, ToolContext, ToolError, UnknownTool, available, call

__all__ = ["REGISTRY", "Tool", "ToolContext", "ToolError", "UnknownTool", "available", "call"]
