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
from typing import Any, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app.services import ai_agent, ai_prompts
from app.services.script_nodes import MAX_FILE_BYTES, MAX_FILES, _PATH

MAX_ERROR_CHARS = 4000
MAX_PACKAGES = 300
MAX_COLUMNS = 400


# ── what of a log may go to the model ────────────────────────────────────────
_PY_TRACEBACK = "Traceback (most recent call last):"
# an exception's line: `KeyError: 'x'`, `pkg.mod.SomeError: …`, `pyarrow.lib.ArrowInvalid: …` — a
# dotted name, or a name ending as exceptions do, so that a line the script printed («Error
# rate: 5%», «Totale: 5») is not taken for one
_PY_ERROR_LINE = re.compile(r"^((\w+\.)+\w+|\w+(Error|Exception|Invalid|Interrupt|Exit))(: .*)?$")
# without a traceback, Python writes only its syntax errors
_PY_SYNTAX_LINE = re.compile(r"^(SyntaxError|IndentationError|TabError)(: .*)?$", re.M)
# the marks under a frame's code line (Python 3.11+): «    ~~~^^^^^»
_PY_MARKS = re.compile(r"^\s+[~^]+\s*$")
_R_ERROR = re.compile(r"^(Error|Errore|Erreur|Fehler)\b")
_R_HALTED = "Execution halted"
_R_RLANG = ("! ", "✖ ", "ℹ ", "Caused by error")


def error_excerpt(log: str, language: str) -> str:
    """The error of a failed run, without what the script printed. A Job's log mixes its
    stdout into its stderr — the printed rows may land in the middle of a traceback — and
    a script may print lines shaped like errors (`message("Errore nel cliente …")`): only
    what the interpreter itself writes for an error goes.
    Python: from the last traceback, its frames (`  File …`, the code line under each and
    its `^^^` marks) and the first exception line after the last frame. Without a
    traceback, only a syntax error's line. R: the last «Error…» line before «Execution
    halted» (what Rscript writes after an error at top level), the line it continues on
    when it ends with «:», rlang's «! …» lines, «Calls: …» and «Execution halted»; without «Execution halted»
    nothing. Empty when there is no such error: the model is then told the exit code. The
    message itself may quote a value, as errors do («could not convert 'abc'»)."""
    lines = (log or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    if language == "python":
        starts = [i for i, line in enumerate(lines) if line.startswith(_PY_TRACEBACK)]
        if starts:
            kept, expect, last_frame = [_PY_TRACEBACK], "", None
            for i in range(starts[-1] + 1, len(lines)):
                line = lines[i]
                if line.startswith("  File "):
                    kept.append(line)
                    expect, last_frame = "code", i
                elif expect == "code" and line.startswith("    ") and line.strip():
                    kept.append(line)
                    expect = "marks"
                elif expect == "marks" and _PY_MARKS.match(line):
                    kept.append(line)
                    expect = ""
                else:
                    expect = ""
            after = lines[last_frame + 1:] if last_frame is not None else lines[starts[-1] + 1:]
            kept += [line for line in after if _PY_ERROR_LINE.match(line)][:1]
        else:
            kept = [m.group(0) for m in _PY_SYNTAX_LINE.finditer("\n".join(lines))][-1:]
    else:
        halted = [i for i, line in enumerate(lines) if line.strip() == _R_HALTED]
        errors = [i for i, line in enumerate(lines) if _R_ERROR.match(line) and halted and i < halted[-1]]
        if errors:
            at = errors[-1]
            kept = [lines[at]]
            rest = lines[at + 1:halted[-1]]
            if lines[at].rstrip().endswith(":") and rest and rest[0].startswith("  "):
                kept.append(rest.pop(0))  # «Error in f(x) :» wraps its message on the next line
            # rlang's errors (the tidyverse) put the message on «! …», «✖ …», «ℹ …» lines
            for line in rest:
                if line.startswith("Backtrace:"):
                    break
                if line.startswith(_R_RLANG):
                    kept.append(line)
            kept += [line for line in rest if line.startswith("Calls: ")][:1]
            kept.append(_R_HALTED)
    excerpt = "\n".join(kept).strip()
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


def context(d: ScriptDeps) -> str:
    """What Tabularia adds to the text at every request (services/ai_prompts.py): the
    script, its environment, its inputs and output, its files and the last error."""
    lang = "Python" if d.language == "python" else "R"
    where = "a saved script" if d.saved else "a Script node of a flow"
    files = "\n\n".join(f"--- {f['path']}{' (runs)' if f['path'] == d.entrypoint else ''} ---\n{f['content']}" for f in d.files) or "(no files yet)"
    packages = ", ".join(d.packages[:MAX_PACKAGES]) or "none declared beyond the language's own"
    io = (
        f"Read inputs with {'polars or pyarrow (pl.scan_parquet / pl.read_parquet)' if d.language == 'python' else 'arrow::read_parquet'} "
        "on the address in the variable: those libraries read s3:// by themselves with the AWS_* variables set."
    )
    error = f"\nThe last run FAILED with this error (only the error is shown, not the script's output):\n{d.error}\n" if d.error else ""
    return f"""{ai_prompts.CONTEXT_HEADER}
- {ai_prompts.today()}
- The user writes {where}, in {lang} {d.language_version}, in the environment «{d.environment}».
- The environment's packages (install nothing else; if the task needs a package that is missing, say so and that an administrator adds it to the environment): {packages}.
- Variables the script finds (names): {", ".join(d.variables) or "none"}.
- Inputs:
{_inputs_text(d)}
- Output: {_output_text(d)}
- {io}
{error}
The files now ({d.entrypoint or 'no entrypoint'} is the one that runs):
{files}

{ai_prompts.language_line(d.locale)}"""


def _instructions(ctx: RunContext[ScriptDeps]) -> str:
    return ai_prompts.compose(ctx.deps.prompt or ai_prompts.SCRIPT, context(ctx.deps))


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
