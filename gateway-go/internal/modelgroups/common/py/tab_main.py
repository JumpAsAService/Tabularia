"""The dispatcher every Python group's run.py calls: main(group, routes) (the twin of
common/R/main.R).

`routes` maps a model's id to its functions:
    {"xgboost": {"fit": fit_xgboost, "predict": predict_xgboost}, …}
A fit returns {"rows": Rows, "output": a pandas frame or pyarrow table or None, "card": a dict or
None, "model_file": {"name": "xgboost.json", "data": bytes} or None}; a predict returns
{"output": …}. Before anything is written the rows are checked against the catalogue's
declaration for the variant: a runner that writes another shape is a bug, and the Job fails
(exit 1).

What main writes, in fit mode: TABULARIA_RESULTS (always), TABULARIA_OUTPUT (when the task says
write_output and the fit gave rows), artifacts/model.json (the card, unless it or the model file
is too large), artifacts/MLmodel and the model file (when the task says mlflow),
artifacts/run.json (software, timings, warnings) — in predict mode TABULARIA_OUTPUT and run.json.
The warnings of the libraries are caught and written to run.json, as R's are.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import warnings
from typing import Any, Callable

from tab_card import card_fits, mlmodel, model_file_fits, versions
from tab_protocol import (ENV_MODEL_IN, ENV_OUTPUT, EXIT_OK, EXIT_REFUSED, PROTOCOL, Refusal, artifact, code_dir,
                          read_task, read_text, to_json, variant_of, write_bytes, write_error, write_text)
from tab_results import check_declared, write as write_results
from tab_selftest import selftest

Routes = dict[str, dict[str, Callable[..., dict[str, Any]]]]


def main(group: str, routes: Routes) -> None:
    """Runs the task (or the self-test with --selftest) and exits with its code."""
    code = code_dir()
    started = time.monotonic()
    phases: dict[str, float] = {}
    task: dict[str, Any] | None = None
    status = 1
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        def phase(name: str, f: Callable[[], Any]) -> Any:
            t0 = time.monotonic()
            value = f()
            phases[name] = round(time.monotonic() - t0, 3)
            return value

        try:
            if "--selftest" in sys.argv[1:]:
                task = {"protocol": PROTOCOL, "mode": "selftest", "group": group}
                status = selftest(group, routes, code)
            else:
                task = read_task(code / "task.json")
                if task["mode"] == "selftest":
                    status = selftest(group, routes, code)
                else:
                    task["group"] = task.get("group") or group
                    task["variant"] = task.get("variant") or variant_of(task.get("model") or "", task["params"])
                    route = routes.get(task.get("model") or "")
                    if route is None:
                        raise Refusal("unknown_model", f"the group {group} has no model «{task.get('model') or ''}»")
                    if task["mode"] == "fit":
                        result = phase("fit", lambda: route["fit"](task))
                        phase("write", lambda: finish_fit(task, result))
                    else:
                        card = phase("read_card", lambda: json.loads(read_text(os.environ.get(ENV_MODEL_IN, ""))))
                        result = phase("predict", lambda: route["predict"](task, card))
                        phase("write", lambda: write_output(result.get("output")))
                    status = EXIT_OK
        except Refusal as e:
            write_error(e)
            status = EXIT_REFUSED
        except Exception as e:  # noqa: BLE001 — a crash: the log's tail says what
            print(f"the runner failed: {e}", file=sys.stderr)
            for line in traceback.format_exception(e)[-12:]:
                print("  " + line.rstrip().replace("\n", "\n  "), file=sys.stderr)
            status = 1
        said = list(dict.fromkeys(f"{w.category.__name__}: {w.message}" for w in caught))[:50]
    if task is not None and task.get("mode") != "selftest":
        run = {"protocol": PROTOCOL, "group": group, "model": task.get("model"), "mode": task.get("mode"), "exit": status,
               "software": versions(), "seconds": round(time.monotonic() - started, 3), "phases": phases, "warnings": said}
        try:
            write_text(artifact("run.json"), to_json(run))
        except Exception as e:  # noqa: BLE001
            print(f"run.json could not be written: {e}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(status)


def finish_fit(task: dict[str, Any], result: dict[str, Any]) -> None:
    """Writes what a fit gave, after checking its rows. A card larger than max_card_bytes, or a
    model file larger than max_model_file_bytes, does not stop the step: its results, rows and
    report are written, with a note; model.json, MLmodel and the model file are not."""
    rows = result["rows"]
    model_file = result.get("model_file")
    card_text = None
    if result.get("card") is not None:
        if model_file is None or model_file_fits(task, model_file["data"], rows):
            card_text = card_fits(task, result["card"], rows)
    problems = check_declared(rows, task["model"], task["variant"])
    if problems:
        raise RuntimeError("the results do not match the catalogue:\n  " + "\n  ".join(problems[:20]))
    write_results(rows)
    if task.get("write_output") and result.get("output") is not None:
        write_output(result["output"])
    if card_text is not None:
        write_text(artifact("model.json"), card_text)
        if task.get("mlflow"):
            write_text(artifact("MLmodel"), mlmodel(result["card"]))
            if model_file is not None:
                write_bytes(artifact(model_file["name"]), model_file["data"])


def write_output(output: Any, path: str | None = None) -> bool:
    """The rows (a pandas frame or a pyarrow table) as TABULARIA_OUTPUT's Parquet file."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if output is None:
        return False
    path = path if path is not None else os.environ.get(ENV_OUTPUT, "")
    if not path:
        raise RuntimeError("TABULARIA_OUTPUT is empty")
    table = output if isinstance(output, pa.Table) else pa.Table.from_pandas(output, preserve_index=False)
    pq.write_table(table, path)
    return True


def model_input(task: dict[str, Any], columns: list[str] | None = None):
    """The step's input: the frame a self-test hands over, or the Parquet file of
    TABULARIA_INPUT. A runner reads its input through this, never TABULARIA_INPUT itself."""
    from tab_columns import read_input

    if task.get("_frame") is not None:
        frame = task["_frame"]
        return frame if columns is None else frame[columns]
    return read_input(columns=columns)
