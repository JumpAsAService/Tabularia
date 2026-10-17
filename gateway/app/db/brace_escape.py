"""The braces of the definitions stored before the placeholder language (used by
revision 0013): written `{{` `}}` so that they stay the literal text they were.

Before the language, braces were plain text in the email subject and attachment
name, in the S3 Output's key and its copy's key, in the SFTP Output's folder, and
in the folder, pattern and path of the SFTP and SharePoint datasources; the SFTP
Output's file name knew only {date}, {datetime}, {run_id} and {flow}. The API
call's templates and every other field are left alone.

Every brace is doubled, `{{` included (it was two literal braces): the
migration runs once, so this is not idempotent and need not be.
"""
from __future__ import annotations

import re

# node data keys of an Output that were literal text
_LITERAL_NODE_FIELDS = ("emailSubject", "attachmentName", "s3Key", "mirrorKey", "sftpFolder")
# datasource source_ref fields that were literal text, by source type
_LITERAL_SOURCE_FIELDS = {"sftp": ("folder", "pattern"), "sharepoint": ("path",)}


def escape_literal(text: str) -> str:
    """Every brace doubled: before the language each one was literal text, `{{`
    included (two braces, not an escape). Not idempotent, and need not be: the
    migration that uses it runs once."""
    return text.replace("{", "{{").replace("}", "}}")


# exactly how the SFTP Output found its placeholders before the language
_OLD_SFTP_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_OLD_SFTP_NAMES = ("date", "datetime", "run_id", "flow")


def escape_sftp_file_name(text: str) -> str:
    """Every brace doubled except the placeholders the old SFTP Output resolved:
    `{date}` `{datetime}` `{run_id}` `{flow}` as its regular expression found them
    (`{{date}}` was a brace, the date, a brace)."""
    parts: list[str] = []
    last = 0
    for m in _OLD_SFTP_PLACEHOLDER.finditer(text):
        if m.group(1) not in _OLD_SFTP_NAMES:
            continue  # the old code refused it: kept as literal text
        parts.append(escape_literal(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(escape_literal(text[last:]))
    return "".join(parts)


def escape_flow_definition(definition: dict) -> dict:
    """The flow's canvas with the braces of its Outputs' literal fields escaped."""
    for node in definition.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "output":
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        for key in _LITERAL_NODE_FIELDS:
            if isinstance(data.get(key), str):
                data[key] = escape_literal(data[key])
        if isinstance(data.get("sftpFileName"), str):
            data["sftpFileName"] = escape_sftp_file_name(data["sftpFileName"])
    return definition


def escape_source_ref(source_type: str, ref: dict) -> dict:
    for key in _LITERAL_SOURCE_FIELDS.get(source_type, ()):
        if isinstance(ref.get(key), str):
            ref[key] = escape_literal(ref[key])
    return ref
