"""The runner of the group «foundation_forecast» (docs/specs/foundation-forecast.md, «The runner»):
`python -I -B /tabularia/code/runner/run.py` in /tabularia/code, or with `--selftest`. It puts exactly the common
library's folder and the runner's own on sys.path (-I left out PYTHONPATH, the user's site and this file's folder),
imports the modules of the node (runner/py/ff_<part>.py: never a package's own name, so that the vendored package
`timestral`, runner/py/timestral/, is the one `import timestral` reaches) and hands the routes to tab_main.main: which
function forecasts each model of the group. The node makes no model: no `predict` route. Nothing is random, and
nothing here reads a column's name as code.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "common" / "py"), str(HERE / "py")]

import ff_timestral  # noqa: E402
import tab_main  # noqa: E402

tab_main.main("foundation_forecast", {
    "timestral": {"fit": ff_timestral.fit_timestral},
})
