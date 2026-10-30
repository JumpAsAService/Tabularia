"""Metadata-only mode: no value of a row of data goes to a model (docs/integrations/ai.md).

Two switches, each off by default and changed live from Administration → Settings:
`AI__METADATA_ONLY` for what goes through the model configured in Tabularia (the chat, the
flow and script assistants, the dbt export), `MCP__METADATA_ONLY` for the MCP server, whose
model is the client's. Both are read at every use (`on`): `get_settings()` holds what the
panel saved, read again by every gateway within `overrides.REREAD_EVERY`.

What goes: names, types, hand-written descriptions, row counts, the structure of flows and
reports, saved SQL. What does not: sample rows, query results, previews, column values, an
aggregate computed on the data (a total is a value), and the text of an error, which may
quote a value (`could not convert 'abc'`). The barrier is in the tools, not in a prompt:

- every tool declares `reads_data` (services/agent/registry.py, `flow_tool` in
  services/ai_flow_agent.py), with no default, so a new one cannot be forgotten. Those that
  read data are not offered and are refused when called; those that read metadata and data
  leave the data out themselves;
- the two functions every engine preview for a model goes through refuse too
  (`ai_agent._engine_preview`, `ai_flow_agent.preview_node`), the second with an exception
  for the plan's card, which the person reads and the model does not;
- a conversation's earlier turns go back to the model only when they were made in this
  mode (services/ai_chats.py): an older one may hold rows, in a tool's result or in what
  the model wrote about them;
- a turn begun outside the mode stops before its next request when the mode comes on
  (`StopWhenWithheld`): its own results would go back with that request.

A turn begun in the mode stays in it to its end, whatever happens to the switch, so that
what it saved is what its mark says.

What stays visible, because it is the structure and not the data: the constants someone
wrote in a filter, in SQL, in a description, in a report's parameters or in an email, and
the column names a pivot made out of values. The documentation says so.
"""
from __future__ import annotations

from typing import Literal

from app.core.config import get_settings

Origin = Literal["chat", "mcp"]

ERROR_WITHHELD = ("The text of this error is withheld: this installation is in metadata-only mode and an error may quote a "
                  "value of the data. The user reads it in Tabularia.")
PREVIEW_WITHHELD = ("Not previewed: this installation is in metadata-only mode, so no row or computed result reaches you. "
                    "The step is not checked: the user checks it in Tabularia.")
STEP_NOT_CHECKED = ("Done without a preview (metadata-only mode): neither its rows nor its columns are known here. "
                    "Reason from the operation's settings, and ask the user to check the result in Tabularia.")
SAMPLE_WITHHELD = "No sample rows: this installation is in metadata-only mode."
SEARCH_WITHHELD = ("Runs cannot be searched by text in metadata-only mode, because the search reads their errors: filter them "
                   "by flow_id or status.")


def on(origin: Origin = "chat") -> bool:
    """The switch in force for a model of this origin, as of now."""
    s = get_settings()
    return bool(s.mcp.metadata_only if origin == "mcp" else s.ai.metadata_only)


def refusal(tool_name: str) -> str:
    """What a call to a tool that reads data gets back."""
    return (f"{tool_name} returns values of the data, and this installation is in metadata-only mode: no value of a row "
            "reaches an AI model, and no tool can read one. Tell the user so, instead of guessing the values: names, "
            "types, descriptions, row counts and the structure of what is saved stay available.")


class TurnOutdated(Exception):
    """The mode came on during a turn begun outside it: what that turn read must not go
    back to the model with its next request. The turn stops here."""


TURN_OUTDATED = ("An administrator turned on metadata-only mode while this answer was being written, so it stopped here. "
                 "Ask again: from now on the model works without the values of the data.")
