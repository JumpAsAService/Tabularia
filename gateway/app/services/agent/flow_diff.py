"""What a draft changes in a saved flow, node by node: what the plan shows before a save.

Positions, sizes and selection are the canvas' layout, not the flow: they are left out.
"""
from __future__ import annotations

import json
from typing import Any

from app.services import ai_flow_agent

# what a node's place on the canvas is made of: never a change of the flow
_LAYOUT = {"position", "positionAbsolute", "selected", "dragging", "width", "height", "style", "zIndex", "measured"}
MAX_WORDS = 240


def _parse(definition: Any) -> ai_flow_agent.FlowDraft:
    if isinstance(definition, str):
        definition = json.loads(definition or "{}")
    return ai_flow_agent.FlowDraft(definition or {})


def _essence(node: dict) -> str:
    return json.dumps({k: v for k, v in node.items() if k not in _LAYOUT}, sort_keys=True, default=str)


def _edges(draft: ai_flow_agent.FlowDraft) -> set[tuple[str, str, str]]:
    return {(str(e.get("source")), str(e.get("target")), str(e.get("targetHandle") or "left")) for e in draft.edges}


# what an Output writes, where and to whom, by kind: the settings a line shows (and a change of
# which shows as a change of the line)
_OUTPUT_FIELDS = {
    "database": ("connectionId", "table", "mode", "postSql"),
    "s3": ("connectionId", "s3Bucket", "s3Key", "s3Format", "partitionBy"),
    "sftp": ("connectionId", "sftpFolder", "sftpFileName", "sftpFormat"),
    "email": ("connectionId", "emailTo", "emailCc", "emailSubject", "attachmentName", "attachmentFormat"),
}


def node_line(draft: ai_flow_agent.FlowDraft, node: dict) -> str:
    """A node of `draft` in a line; an Output with where it writes and to whom."""
    summary = ai_flow_agent._summary(draft, node)
    if summary.get("kind") == "output":
        d = node.get("data") or {}
        fields = _OUTPUT_FIELDS.get(str(d.get("destType") or "datasource"), ())
        summary["settings"] = {k: d[k] for k in fields if d.get(k) not in (None, "", [])}
    return node_words(summary)


def node_words(summary: dict) -> str:
    """A node in a line: «op-3: filter {"column": …}»."""
    kind = summary.get("kind")
    if kind == "source":
        text = f"source «{summary.get('datasource') or '(empty)'}»"
    elif kind == "step":
        text = f"{summary.get('operation')} {json.dumps(summary.get('params') or {}, ensure_ascii=False, default=str)}"
    elif kind == "output":
        writes = summary.get("writes") or {}
        text = ("output to datasource «" + str(writes.get("datasource")) + "»") if "datasource" in writes \
            else f"output to {writes.get('type')}"
        if summary.get("settings"):
            text += " " + json.dumps(summary["settings"], ensure_ascii=False, default=str)
    elif kind == "note":
        text = f"note «{summary.get('text', '')[:60]}»"
    else:
        text = str(kind)
    text = f"{summary.get('id')}: {text}"
    return text if len(text) <= MAX_WORDS else text[:MAX_WORDS].rstrip() + "…"


def diff(old_definition: Any, new_definition: Any) -> dict:
    """{added, removed, changed, connected, disconnected}: nodes in words (`changed` with
    before and after), connections as «from → to (input)»."""
    old, new = _parse(old_definition), _parse(new_definition)
    old_nodes = {n["id"]: n for n in old.nodes}
    new_nodes = {n["id"]: n for n in new.nodes}
    added = [node_line(new, n) for nid, n in new_nodes.items() if nid not in old_nodes]
    removed = [node_line(old, n) for nid, n in old_nodes.items() if nid not in new_nodes]
    changed = [
        {"node": nid, "before": node_line(old, old_nodes[nid]), "after": node_line(new, n)}
        for nid, n in new_nodes.items() if nid in old_nodes and _essence(n) != _essence(old_nodes[nid])
    ]
    old_edges, new_edges = _edges(old), _edges(new)
    return {
        "added": added, "removed": removed, "changed": changed,
        "connected": [f"{s} → {t} ({h})" for s, t, h in sorted(new_edges - old_edges)],
        "disconnected": [f"{s} → {t} ({h})" for s, t, h in sorted(old_edges - new_edges)],
    }


def changed_node_ids(old_definition: Any, new_definition: Any) -> set[str]:
    """The nodes a draft adds or changes (their settings, not their place)."""
    old, new = _parse(old_definition), _parse(new_definition)
    before = {n["id"]: _essence(n) for n in old.nodes}
    return {n["id"] for n in new.nodes if before.get(n["id"]) != _essence(n)}


def is_empty(d: dict) -> bool:
    return not any(d.get(k) for k in ("added", "removed", "changed", "connected", "disconnected"))


def diff_lines(d: dict) -> list[str]:
    """The difference in words, one line per change."""
    lines = [f"+ {w}" for w in d.get("added") or []]
    lines += [f"- {w}" for w in d.get("removed") or []]
    lines += [f"~ {c['before']}  →  {c['after']}" for c in d.get("changed") or []]
    lines += [f"+ link {w}" for w in d.get("connected") or []]
    lines += [f"- link {w}" for w in d.get("disconnected") or []]
    return lines
