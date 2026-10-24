"""The sheets of a workbook, in their order, and which ones are hidden: the editor shows
them after an upload, to choose the one to read (by default the first that is visible).

The names come from the workbook itself (fastexcel, for every format it reads); for an
.xlsx/.xlsm whether a sheet is hidden comes from `xl/workbook.xml`, read from the zip
without opening any sheet, so a big workbook costs nothing more. For the other formats
(.xls, .ods) every sheet counts as visible."""
from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class Sheet:
    name: str
    hidden: bool = False


def _hidden_in_xlsx(path: str) -> set[str]:
    try:
        with zipfile.ZipFile(path) as z, z.open("xl/workbook.xml") as f:
            root = ElementTree.parse(f).getroot()
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
        return set()
    # «hidden» and «veryHidden» (only a macro shows it again): both hidden
    return {s.get("name", "") for s in root.iter(f"{_MAIN}sheet") if re.match(r"(?i)(very)?hidden$", s.get("state") or "")}


def workbook_sheets(path: str) -> list[Sheet]:
    """The sheets, in the workbook's order; [] when the file cannot be opened (the
    conversion then says why, in its own words)."""
    try:
        import fastexcel

        names = list(fastexcel.read_excel(path).sheet_names)
    except Exception as e:  # not a workbook, or damaged
        logger.info("sheets of %s not listed: %s", path, e)
        return []
    hidden = _hidden_in_xlsx(path)
    return [Sheet(n, n in hidden) for n in names]


def default_sheet(sheets: list[Sheet]) -> str | None:
    """The sheet read when none is chosen: the first visible one (a hidden first sheet,
    of lookups or settings, is not what a person means by «the file»)."""
    visible = [s.name for s in sheets if not s.hidden]
    return (visible or [s.name for s in sheets] or [None])[0]


# «Check the files» downloads one workbook to list its sheets: one this size at most
PEEK_BYTES = 50 * 1024 * 1024
WORKBOOK_EXTS = (".xlsx", ".xlsm", ".xlsb", ".xls", ".ods")


def sheets_of_first(files: Iterable[Any], path_of: Callable[[Any], str], size_of: Callable[[Any], int],
                    fetch: Callable[[Any, str], None], shown_as: Callable[[Any], str]) -> dict | None:
    """The sheets of the first workbook among the files a datasource takes, for «Check the
    files»: the sheet is then chosen from a list, not typed. None when there is no
    workbook within PEEK_BYTES, or it cannot be downloaded or read: the check itself
    does not fail for it."""
    book = next((f for f in files if path_of(f).lower().endswith(WORKBOOK_EXTS) and size_of(f) <= PEEK_BYTES), None)
    if book is None:
        return None
    with tempfile.TemporaryDirectory(prefix="sheets_") as tmp:
        local = os.path.join(tmp, "book" + os.path.splitext(path_of(book))[1].lower())
        try:
            fetch(book, local)
        except Exception as e:  # noqa: BLE001 — the list is a help, the check goes on
            logger.info("sheets of %s not read: %s", shown_as(book), e)
            return None
        sheets = workbook_sheets(local)
    if not sheets:
        return None
    return {"file": shown_as(book), "sheets": [s.name for s in sheets], "hidden": [s.name for s in sheets if s.hidden]}
