"""The check of the table a model's runner writes (`results.parquet`): the statistics' ten
columns with their exact types, and only the rows the catalogue declares for the model's
variant. The engine runs it before accepting a step's results (docs/specs/
statistical-models.md, «A run», 4): a runner that writes another shape is a bug, and the
step fails with the sentence — the report and MLflow never read a table this refuses.

What a row must be:

- the ten columns of `stats.output.OUTPUT_COLUMNS`, in their order, of their types
  (a text may be Arrow's `string` or `large_string`); `section`, `stat`, `ord`, `key`
  never null; a number is finite or null;
- `ord` is 0 … N−1, each once; `key` is unique and is the row's section, level, level_2
  and stat joined by «|» (null parts empty), then «|» and the point's index for the rows
  of a chart (`SECTION_SHAPES[section]["indexed"]`);
- its section is one the variant declares, its level and level_2 null or not as the
  section's shape says, its level one the declaration lists (when it lists them), its stat
  a declared number (text null, but a chart's point may carry a label) or a declared text
  (value null); a declared stat ending in «*» stands for every stat that starts with it;
- an assumption's `verdict` is one of `VERDICTS`;
- the rows every model writes are there: `model|||model` (the model's id),
  `model|||variant` (the variant's id), `model|||n`.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from app.engine.exceptions import EngineError
from app.engine.models import catalog
from app.engine.stats.output import OUTPUT_COLUMNS, OUTPUT_NAMES

REQUIRED_KEYS = ("model|||model", "model|||variant", "model|||n")

_INDEX = re.compile(r"^(0|[1-9][0-9]*)$")
_ARROW_TYPES = {"String": ("string", "large_string"), "Float64": ("double",), "Int64": ("int64",)}


class ResultsError(EngineError):
    """A runner's table that is not what the catalogue says: a bug of the runner."""


def _fail(message: str) -> ResultsError:
    return ResultsError(f"results.parquet: {message}")


def _declared(stat: str, names: list[str]) -> bool:
    return any(stat == n or (n.endswith("*") and stat.startswith(n[:-1])) for n in names)


def check_schema(schema: Any) -> None:
    """The ten columns, in their order, of their types (a pyarrow schema)."""
    names = list(schema.names)
    if names != list(OUTPUT_NAMES):
        missing = [n for n in OUTPUT_NAMES if n not in names]
        extra = [n for n in names if n not in OUTPUT_NAMES]
        detail = (f"missing {', '.join(missing)}" if missing else "") + ("; " if missing and extra else "") + \
                 (f"unexpected {', '.join(extra)}" if extra else "")
        raise _fail(f"the columns must be {', '.join(OUTPUT_NAMES)}, in this order" + (f" ({detail})" if detail else ""))
    for name, kind, _ in OUTPUT_COLUMNS:
        got = str(schema.field(name).type)
        if got not in _ARROW_TYPES[kind]:
            raise _fail(f"the column {name} must be {kind}, not {got}")


def check_table(model: str, params: Mapping[str, Any], table: Any) -> str:
    """Refuse (ResultsError) a pyarrow table that is not what the model's variant writes;
    the variant's id when it is. `params` are the node's, checked (`catalog.check_params`)."""
    check_schema(table.schema)
    limit = catalog.LIMITS["max_result_rows"]
    if table.num_rows > limit:
        raise _fail(f"{table.num_rows} rows, more than the {limit} a model's results may hold")
    spec = catalog.variant_spec(model, params)
    sections = spec["sections"]
    cols = {name: table.column(name).to_pylist() for name in OUTPUT_NAMES}
    for name in ("section", "stat", "ord", "key"):
        if any(v is None for v in cols[name]):
            raise _fail(f"the column {name} holds a null")
    if sorted(cols["ord"]) != list(range(table.num_rows)):
        raise _fail("ord must number the rows 0 to N−1, each once")
    seen: set[str] = set()
    texts: dict[str, str] = {}
    for i in range(table.num_rows):
        section, level, level_2, stat = cols["section"][i], cols["level"][i], cols["level_2"][i], cols["stat"][i]
        value, text, key = cols["value"][i], cols["text"][i], cols["key"][i]
        where = f"the row {key!r}"
        for v in (value, cols["x0"][i], cols["x1"][i]):
            if v is not None and not math.isfinite(v):
                raise _fail(f"{where} holds a number that is not finite: write null")
        if key in seen:
            raise _fail(f"the key {key!r} is there twice")
        seen.add(key)
        if section not in catalog.SECTION_SHAPES:
            raise _fail(f"{where}: there is no section {section!r}")
        if section not in sections:
            raise _fail(f"{where}: the variant {spec['id']} of {model} has no section {section!r}")
        shape, decl = catalog.SECTION_SHAPES[section], sections[section]
        for part, v in (("level", level), ("level_2", level_2)):
            if shape[part] == "null" and v is not None:
                raise _fail(f"{where}: {part} must be null in the section {section}")
            if shape[part] == "required" and (v is None or v == ""):
                raise _fail(f"{where}: {part} is required in the section {section}")
        if decl["levels"] is not None and level not in decl["levels"]:
            raise _fail(f"{where}: {level!r} is not one of the {section} rows of {model} ({spec['id']})")
        base = "|".join((section, level or "", level_2 or "", stat))
        indexed = shape["indexed"] and stat not in shape.get("unindexed", ())
        if indexed:
            if not key.startswith(base + "|") or not _INDEX.match(key[len(base) + 1:]):
                raise _fail(f"{where}: the key of a chart's point is {base}|<index>")
        elif key != base:
            raise _fail(f"{where}: the key must be {base!r}")
        if _declared(stat, decl["numbers"]):
            if text is not None and section not in ("point", "curve"):
                raise _fail(f"{where}: {stat} is a number, its text must be null")
        elif _declared(stat, decl["texts"]):
            if value is not None:
                raise _fail(f"{where}: {stat} is a text, its value must be null")
            texts[key] = text
            if section == "assumption" and stat == "verdict" and text not in catalog.VERDICTS:
                raise _fail(f"{where}: a verdict is one of {', '.join(catalog.VERDICTS)}")
        else:
            raise _fail(f"{where}: {model} ({spec['id']}) writes no {stat!r} in the section {section}")
    for key in REQUIRED_KEYS:
        if key not in seen:
            raise _fail(f"the row {key!r} is missing")
    if texts.get("model|||model") != model:
        raise _fail(f"the row 'model|||model' must say {model!r}")
    if texts.get("model|||variant") != spec["id"]:
        raise _fail(f"the row 'model|||variant' must say {spec['id']!r}, the variant the parameters make")
    return spec["id"]


def check_file(model: str, params: Mapping[str, Any], source: Any) -> str:
    """`check_table` on a Parquet file (a path or a file-like object)."""
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(source)
    except Exception as e:  # noqa: BLE001 — what pyarrow raises on a file that is not Parquet varies
        raise _fail(f"not a Parquet file ({type(e).__name__})") from e
    return check_table(model, params, table)


# ── fixtures: a table per model and variant, before the runners exist ───────
def table_of_rows(rows: list[list[Any]]) -> Any:
    """A list of rows of the ten columns as a pyarrow table of their types."""
    from app.engine.stats.output import to_arrow

    return to_arrow([tuple(r) for r in rows])


def read_fixture(path: str | Path) -> tuple[str, dict[str, Any], Any]:
    """A fixture of backend/tests/fixtures/models/: (model, its checked parameters, the
    table). The report lane draws them; the runners' lanes replace them with real output."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("columns") != list(OUTPUT_NAMES):
        raise ValueError(f"{path}: the columns must be {', '.join(OUTPUT_NAMES)}")
    params = catalog.check_params(data["model"], data["params"])
    return data["model"], params, table_of_rows(data["rows"])
