"""Typst in a child process: the one way a report is laid out (PDF, the pages on screen as
SVG, the charts of the email as PNG).

The child (typst_child.py) limits its own memory to REPORTS__RENDER_MEMORY_MB and dies with
the worker's process; here the wait is limited to REPORTS__RENDER_SECONDS, and anything
that stops the wait — the timeout, the task's soft time limit, a cancellation — kills it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.reports.errors import ReportError

HERE = Path(__file__).resolve().parent
CHILD = HERE / "typst_child.py"
TEMPLATE = HERE / "template.typ"
FONTS = HERE / "fonts"


def prepare(root: Path) -> None:
    """The template next to the files it reads: Typst reads nothing outside its root."""
    shutil.copyfile(TEMPLATE, root / "report.typ")


def run(job: dict, memory_mb: int, seconds: float, max_pages: int = 0, child: Path | None = None) -> dict:
    """Runs a job in the child; ReportError when it fails, is too long or too large."""
    job = {"fonts": str(FONTS), **job}
    # nothing of the worker's environment (its keys) reaches the child: the interpreter is a
    # full path and Typst starts no program
    env = {"MALLOC_ARENA_MAX": "2", "LANG": "C.UTF-8", "HOME": job["root"], "TMPDIR": job["root"]}
    proc = subprocess.Popen([sys.executable, "-I", str(child or CHILD), str(memory_mb), str(os.getpid())],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            cwd=job["root"])
    try:
        out, err = proc.communicate(json.dumps(job).encode(), timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ReportError(f"laying out the report took longer than {seconds:g} s, the limit "
                          "(REPORTS__RENDER_SECONDS): show fewer rows, or fewer pages", "timeout") from None
    except BaseException:  # the task's time limit, a cancellation: the child goes with it
        proc.kill()
        proc.wait()
        raise
    text = err.decode("utf-8", "replace")
    try:
        result = json.loads(out) if out.strip() else None
    except ValueError:
        result = None
    out_of_memory = ReportError(f"laying out the report needs more than {memory_mb} MB, the limit "
                                "(REPORTS__RENDER_MEMORY_MB): show fewer rows, or fewer pages")
    if result is None:
        if proc.returncode in (-6, -9, 134, 137) or "memory allocation" in text or "MemoryError" in text:
            raise out_of_memory
        raise ReportError(f"laying out the report failed: {text.strip()[-500:] or f'exit code {proc.returncode}'}",
                          "bad_request")
    if not result.get("ok"):
        if result.get("error") == "memory":
            raise out_of_memory
        if result.get("error") == "pages":
            raise ReportError(f"the report would have {result.get('pages')} pages, more than the {max_pages} of the "
                              "limit (REPORTS__MAX_PAGES): filter it, or show fewer rows")
        raise ReportError(f"laying out the report failed: {result.get('detail')}", "bad_request")
    return result
