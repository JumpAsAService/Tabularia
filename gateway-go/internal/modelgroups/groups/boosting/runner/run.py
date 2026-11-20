"""The runner of the group «boosting» (docs/specs/statistical-models.md, «Group `boosting` —
Python», «The runner in Python»): `python -I -B /tabularia/code/runner/run.py` in
/tabularia/code, or with `--selftest`. It puts exactly the common library's folder and the
runner's own on sys.path (-I left out PYTHONPATH, the user's site and this file's folder), imports
one module per model (runner/py/boosting_<model>.py: never a library's own name, so that
`import xgboost` reaches the library) and hands the routes to tab_main.main: which function fits
and which predicts each model of the group. Nothing here reads a column's name as code.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "common" / "py"), str(HERE / "py")]

import boosting_catboost  # noqa: E402
import boosting_shared  # noqa: E402,F401 — the shared module's own tests register themselves
import boosting_xgboost  # noqa: E402
import tab_main  # noqa: E402

tab_main.main("boosting", {
    "xgboost": {"fit": boosting_xgboost.fit_xgboost, "predict": boosting_xgboost.predict_xgboost},
    "catboost": {"fit": boosting_catboost.fit_catboost, "predict": boosting_catboost.predict_catboost},
})
