"""Writes output_columns.json: the columns, with their types, that the model runners really write
for the next step (the rows, the survival table, the forecast, the results), one case per model and
variant. The engine's test (backend/tests/test_models_output_columns.py) checks the catalogue's
`OUTPUT_TYPES` and `output_columns` against them; the editor's and the flow assistant's tests check
their own reading of the catalogue against the same cases.

Each case is one fit of a group's reference table (gateway-go/internal/modelgroups/groups/<group>/
reference), run by the group's runner as a model Job runs it: the runner's files, the common library
and the catalogue in /tabularia/code, the input as Parquet, `write_output` on. It needs Docker and the
groups' images built on this machine (tabularia-models-<group>:dev, see gateway-go/cmd/modelgroup);
it builds none. From the repository's root, with the engine's Python (Polars):

    backend/.venv/bin/python backend/tests/fixtures/models/make_output_columns.py [group ...]

Run it again when a runner changes what it writes, then the tests. The types are the engine's
names of what it reads from the runner's file (Polars'), as a preview of the next step shows them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.engine.models import catalog  # noqa: E402

GROUPS_DIR = ROOT / "gateway-go" / "internal" / "modelgroups"
OUT = Path(__file__).with_name("output_columns.json")
IMAGE = "tabularia-models-{group}:dev"

# (what, group, the reference case's id, the parameters changed or added)
CASES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("linear, the results table", "regression", "linear_ols", {}),
    ("linear, the rows", "regression", "linear_ols", {"output": "rows"}),
    ("linear with a lasso penalty, the rows", "regression", "penalized_linear_lasso", {"output": "rows"}),
    ("logistic, the rows", "regression", "logistic_ml", {"output": "rows"}),
    ("logistic with a lasso penalty, the rows", "regression", "penalized_logistic_lasso", {"output": "rows"}),
    ("Kaplan–Meier with strata, the survival table", "survival", "kaplan_meier_strata", {"output": "table"}),
    ("Kaplan–Meier without strata, the survival table", "survival", "kaplan_meier_single", {"output": "table"}),
    ("Cox, the rows", "survival", "cox_categorical", {"output": "rows"}),
    ("parametric survival, Weibull, quantiles and times", "survival", "ps_weibull", {"output": "rows"}),
    ("parametric survival, log-normal", "survival", "ps_lognormal", {"output": "rows"}),
    ("parametric survival, Gompertz", "survival", "ps_gompertz", {"output": "rows"}),
    ("parametric survival, the quantile 0.5 is the median", "survival", "ps_exponential_exact",
     {"output": "rows", "quantiles": [0.25, 0.5]}),
    ("parametric survival, the distribution searched for", "survival", "ps_auto_parsimony", {"output": "rows"}),
    ("ARIMA on a whole-number time, the forecast", "timeseries", "arima_ar_index", {"output": "forecast"}),
    ("SARIMA on dates, the forecast", "timeseries", "arima_sarima_log_holdout", {"output": "forecast"}),
    ("ARIMA with automatic orders, the forecast", "timeseries", "arima_auto_nonseasonal", {"output": "forecast"}),
    ("XGBoost regression, the rows", "boosting", "xgboost_regression", {"output": "rows"}),
    ("XGBoost binary, the rows", "boosting", "xgboost_binary", {"output": "rows"}),
    ("XGBoost multiclass, the rows: a probability per class", "boosting", "xgboost_multiclass", {"output": "rows"}),
    ("CatBoost regression, the rows", "boosting", "catboost_regression", {"output": "rows"}),
    ("CatBoost binary, the rows", "boosting", "catboost_binary", {"output": "rows"}),
    ("CatBoost multiclass, the rows: a probability per class", "boosting", "catboost_multiclass", {"output": "rows"}),
    ("customer lifetime value with a spend and a holdout, the rows", "customer_lifetime",
     "clv BG/NBD customers, auto with a spend and a holdout", {}),
    ("customer lifetime value, the purchases only", "customer_lifetime", "clv Pareto/NBD customers, the purchases variant", {}),
    ("BG/BB with a spend and a holdout, the rows", "customer_lifetime", "bg_bb donors like the donations, value with a holdout", {}),
    ("BG/BB, the purchases only", "customer_lifetime", "bg_bb cohorts of different lengths, the purchases variant", {}),
    ("A/B test, binary: the variants", "experiments", "binary_two", {"output": "variants"}),
    ("A/B test, continuous with a cap: the variants", "experiments", "continuous_three_cap_bh", {"output": "variants"}),
    ("A/B test, ratio with CUPED: the variants", "experiments", "cuped_ratio", {"output": "variants"}),
    ("A/B test, a whole-number variant keeps its own type: the variants", "experiments", "types_int", {"output": "variants"}),
    ("A/B planning, binary: the plan", "experiments", "plan_binary_size", {"output": "plan"}),
    ("A/B planning, continuous with CUPED: the plan", "experiments", "plan_cuped_days", {"output": "plan"}),
    ("A/B planning, ratio: the plan", "experiments", "plan_ratio_arms4", {"output": "plan"}),
]

POLARS = {"integer": pl.Int64, "number": pl.Float64, "text": pl.String, "boolean": pl.Boolean, "date": pl.Date,
          "datetime": pl.Datetime("us")}


def reference(group: str, case_id: str) -> dict[str, Any]:
    cases = json.loads((GROUPS_DIR / "groups" / group / "reference" / "expected.json").read_text(encoding="utf-8"))["cases"]
    return next(c for c in cases if c["id"] == case_id)


def input_table(group: str, case: dict[str, Any]) -> pl.DataFrame:
    """The reference table typed as the case says (a column it does not name is a text), as the engine
    hands a model its input."""
    raw = pl.read_csv(GROUPS_DIR / "groups" / group / "reference" / case["data"], infer_schema=False,
                      null_values=["", "NA"])
    out = []
    for name in raw.columns:
        kind = (case.get("types") or {}).get(name, "text")
        col = pl.col(name) if kind == "text" else pl.col(name).str.strip_chars()  # R pads its numbers
        if kind == "boolean":
            out.append(pl.when(col.is_null()).then(None).otherwise(col.str.to_lowercase().is_in(["true", "1"])).alias(name))
        elif kind == "date":
            out.append(col.str.to_date().alias(name))
        elif kind == "datetime":
            out.append(col.str.to_datetime(time_unit="us").alias(name))
        elif kind == "integer":
            out.append(col.cast(pl.Float64).cast(pl.Int64).alias(name))
        else:
            out.append(col.cast(POLARS[kind]).alias(name))
    return raw.select(out)


def code_dir(group: str, language: str, task: dict[str, Any], into: Path) -> None:
    """/tabularia/code as a model Job finds it (gateway-go/internal/modelgroups CodeFiles)."""
    shutil.copytree(GROUPS_DIR / "groups" / group / "runner", into / "runner", ignore=shutil.ignore_patterns("__pycache__"))
    lib = "R" if language == "r" else "py"
    shutil.copytree(GROUPS_DIR / "common" / lib, into / "common" / lib, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(GROUPS_DIR / "common" / "selftest", into / "common" / "selftest")
    shutil.copy(GROUPS_DIR / "card.schema.json", into / "card.schema.json")
    shutil.copy(ROOT / "gateway-go" / "internal" / "modelcatalog" / "catalog.json", into / "catalog.json")
    (into / "task.json").write_text(json.dumps(task), encoding="utf-8")


def run_case(what: str, group: str, case_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    case = reference(group, case_id)
    model = case["model"]
    params = {**case["params"], **changes}
    checked = catalog.check_params(model, params)
    language = catalog.GROUPS[group]["language"]
    task = {"protocol": 1, "mode": "fit", "group": group, "group_version": "witness", "model": model,
            "variant": catalog.variant(model, checked), "params": checked, "output": checked.get("output"),
            "write_output": checked.get("output") != "results", "rows_from": "trial", "mlflow": False,
            "limits": catalog.LIMITS, "ids": {}, "recipe_hash": "", "runner_hash": ""}
    with tempfile.TemporaryDirectory(prefix="tab-output-") as tmp:
        work, code = Path(tmp) / "work", Path(tmp) / "code"
        work.mkdir()
        os.chmod(work, 0o777)
        code_dir(group, language, task, code)
        table = input_table(group, case)
        table.write_parquet(work / "input.parquet")
        run = (["Rscript", "/tabularia/code/runner/run.R"] if language == "r"
               else ["python", "-I", "-B", "/tabularia/code/runner/run.py"])
        cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--memory", "2g",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "OMP_NUM_THREADS=2",
               "-v", f"{code}:/tabularia/code:ro", "-v", f"{work}:/work",
               "-e", "TABULARIA_INPUT=/work/input.parquet", "-e", "TABULARIA_OUTPUT=/work/output.parquet",
               "-e", "TABULARIA_RESULTS=/work/results.parquet", "-e", "TABULARIA_ARTIFACTS=/work/artifacts/",
               IMAGE.format(group=group), *run]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            raise RuntimeError(f"{what}: the runner exited with {done.returncode}\n{done.stderr[-3000:]}")
        written = work / ("results.parquet" if checked.get("output") == "results" else "output.parquet")
        schema = pl.read_parquet_schema(written)
    return {"what": what, "model": model, "params": params,
            "input": [[n, str(t)] for n, t in table.schema.items()],
            "columns": [[n, str(t)] for n, t in schema.items()]}


def main(argv: list[str]) -> int:
    only = set(argv)
    old = {c["what"]: c for c in json.loads(OUT.read_text(encoding="utf-8"))["cases"]} if OUT.exists() else {}
    cases = []
    for what, group, case_id, changes in CASES:
        if only and group not in only:  # another group's: kept as it was written
            if what not in old:
                raise SystemExit(f"{what}: never written yet, run its group too")
            cases.append(old[what])
            continue
        print(f"{group}: {what}", flush=True)
        cases.append(run_case(what, group, case_id, changes))
    doc = {"about": "What the model runners write for the next step, one fit of a reference table each: the input's "
                    "columns and the output's, with the types the engine reads. Written by make_output_columns.py "
                    "(it runs the groups' images); read by the engine's, the editor's and the flow assistant's tests.",
           "cases": cases}
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT.name}: {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
