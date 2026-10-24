"""The script editor's assistant (pydantic-ai): it writes and fixes the code of a Script
node or of a saved script, in Python or R, for the environment it runs in.

What reaches the model, and nothing else (the user's choice of 2026-10-24: no records):
the code, the environment's language, version and declared packages, the NAMES of the
variables the script finds, the names and types of its inputs' columns, and, to fix a
run that failed, only the error part of its log (`error_excerpt`): a traceback or an R
error, never what the script printed. No tool reads data.

Its one tool proposes files; when the turn ends the editor receives the proposal and the
person applies it or not. Nothing is saved or run here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app.services import ai_agent
from app.services.script_nodes import MAX_FILE_BYTES, MAX_FILES, _PATH

MAX_ERROR_CHARS = 4000
MAX_PACKAGES = 300
MAX_COLUMNS = 400


# ── what of a log may go to the model ────────────────────────────────────────
_PY_TRACEBACK = "Traceback (most recent call last):"
# an exception's last line, `KeyError: 'x'`, `pkg.mod.SomeError: …`: a name before «Error», so
# that a line the script printed («Error rate: 5%») is not taken for one
_PY_ERROR_LINE = re.compile(r"^(\w+\.)*\w+(Error|Exception)(: .*)?$", re.M)
_R_ERROR = re.compile(r"^(Error|Errore|Erreur|Fehler)\b.*$", re.M)


def error_excerpt(log: str, language: str) -> str:
    """The error of a failed run, without what the script printed: from the last Python
    traceback to the end, or the last error line; for R, from the first «Error» line to
    «Execution halted». Empty when the log holds no recognisable error."""
    log = (log or "").replace("\r\n", "\n")
    if language == "python":
        at = log.rfind(_PY_TRACEBACK)
        if at != -1:
            excerpt = log[at:]
        else:
            lines = [m.group(0) for m in _PY_ERROR_LINE.finditer(log)]
            excerpt = lines[-1] if lines else ""
    else:
        m = _R_ERROR.search(log)
        excerpt = ""
        if m:
            tail = log[m.start():]
            end = tail.find("Execution halted")
            excerpt = tail[: end + len("Execution halted")] if end != -1 else "\n".join(tail.splitlines()[:20])
    excerpt = excerpt.strip()
    return excerpt[-MAX_ERROR_CHARS:] if len(excerpt) > MAX_ERROR_CHARS else excerpt


# ── dependencies ─────────────────────────────────────────────────────────────
@dataclass
class ScriptDeps(ai_agent.ChatDeps):
    """The script's context, and the proposal the turn builds."""

    language: str = "python"
    language_version: str = ""
    environment: str = ""
    packages: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)  # names only
    files: list[dict[str, str]] = field(default_factory=list)
    entrypoint: str = ""
    inputs: list[dict[str, Any]] = field(default_factory=list)  # [{variable, columns: [{name, dtype}]}]
    output: str = "optional"  # required | optional | none
    saved: bool = False  # a saved script's page, not a Script node
    error: str = ""  # error_excerpt of the last run, when it failed
    proposal: Optional[dict[str, Any]] = None


class FileChange(BaseModel):
    path: str = Field(description="the file's path, relative: main.py, lib/scoring.py, R/helpers.R")
    content: str = Field(description="the WHOLE new content of the file")


# ── instructions ─────────────────────────────────────────────────────────────
def _inputs_text(deps: ScriptDeps) -> str:
    if not deps.inputs:
        return "The script reads no input."
    out = []
    for i in deps.inputs:
        cols = ", ".join(f"{c.get('name')} ({c.get('dtype')})" for c in (i.get("columns") or [])[:MAX_COLUMNS])
        out.append(f"- {i.get('variable')}: a parquet file (s3:// address in that variable) with columns {cols or 'not known yet'}")
    return "\n".join(out)


def _output_text(deps: ScriptDeps) -> str:
    if deps.output == "required":
        return "The script MUST write one parquet file at the address in TABULARIA_OUTPUT: the steps after it read it."
    if deps.output == "none":
        return "The script writes no data for Tabularia (TABULARIA_OUTPUT is empty): it does its work and ends."
    return "The script MAY write its result as one parquet file at the address in TABULARIA_OUTPUT."


def _instructions(ctx: RunContext[ScriptDeps]) -> str:
    d = ctx.deps
    language = ai_agent._LANGUAGES.get(d.locale[:2].lower(), "English")
    lang = "Python" if d.language == "python" else "R"
    where = "a saved script" if d.saved else "a Script node of a flow"
    files = "\n\n".join(f"--- {f['path']}{' (runs)' if f['path'] == d.entrypoint else ''} ---\n{f['content']}" for f in d.files) or "(no files yet)"
    packages = ", ".join(d.packages[:MAX_PACKAGES]) or "none declared beyond the language's own"
    io = (
        f"Read inputs with {'polars or pyarrow (pl.scan_parquet / pl.read_parquet)' if d.language == 'python' else 'arrow::read_parquet'} "
        "on the address in the variable: those libraries read s3:// by themselves with the AWS_* variables set."
    )
    error = f"\nThe last run FAILED with this error (only the error is shown, not the script's output):\n{d.error}\n" if d.error else ""
    return f"""You are the coding assistant inside Tabularia's script editor. The user writes {where}, in {lang} {d.language_version}, \
in the environment «{d.environment}». It runs as a Kubernetes Job; Tabularia gives it its inputs and takes its output as parquet files.

Today is {date.today().isoformat()}. Answer in {language}, concisely, in Markdown.

The environment's packages (install nothing else; if the task needs a package that is missing, say so and that an administrator adds it to the environment): {packages}.
Variables the script finds (names): {", ".join(d.variables) or "none"}.
Inputs:
{_inputs_text(d)}
Output: {_output_text(d)}
{io}
Never write the scripts bucket's name or an s3:// address in the code: always read the variables.
{error}
The files now ({d.entrypoint or 'no entrypoint'} is the one that runs):
{files}

How to work:
1. Answer the request. To change the code, call propose_files ONCE with the WHOLE content of every file you change or add (not a diff, not an excerpt); leave the other files alone. Set entrypoint only to change which file runs.
2. Keep the user's code and style: change only what the request needs.
3. You never see the data, only the columns' names and types: do not invent values, and say when a choice depends on data you cannot see.
4. You cannot run the code. After proposing, say in a few lines what you changed and why; the user applies the proposal and runs it.
5. If the request is not about this script, answer briefly and propose nothing."""


script_agent: Agent[ScriptDeps, str] = Agent(
    deps_type=ScriptDeps,
    instructions=_instructions,
    retries=2,
    name="tabularia-script-assistant",
)


@script_agent.tool
async def propose_files(ctx: RunContext[ScriptDeps], files: list[FileChange], summary: str,
                        entrypoint: Optional[str] = None, remove: Optional[list[str]] = None) -> dict[str, Any]:
    """Propose new contents for some files of the script (whole files), optionally
    removing files or changing the one that runs. The user applies it or not."""
    d = ctx.deps
    current = {f["path"]: f["content"] for f in (d.proposal or {}).get("files", d.files)}
    changed: list[str] = []
    for f in files:
        path = f.path.strip()
        if not _PATH.match(path) or ".." in path.split("/"):
            raise ModelRetry(f"«{path}» is not a valid file path: letters, digits, _ . - and folders separated by /")
        if len(f.content.encode()) > MAX_FILE_BYTES:
            raise ModelRetry(f"«{path}» is over {MAX_FILE_BYTES // 1024} KB: split it into modules")
        if current.get(path) != f.content:
            changed.append(path)
        current[path] = f.content
    removed = [p for p in (remove or []) if p in current]
    for p in removed:
        del current[p]
    if len(current) > MAX_FILES:
        raise ModelRetry(f"a script has at most {MAX_FILES} files")
    entry = (entrypoint or "").strip() or d.entrypoint
    if entry not in current:
        entry = d.entrypoint if d.entrypoint in current else next(iter(current), "")
    d.proposal = {"files": [{"path": p, "content": c} for p, c in current.items()], "entrypoint": entry,
                  "changed": sorted(set((d.proposal or {}).get("changed", [])) | set(changed)),
                  "removed": sorted(set((d.proposal or {}).get("removed", [])) | set(removed)),
                  "summary": summary.strip()[:500]}
    return {"ok": True, "changed": changed, "removed": removed, "entrypoint": entry}
