"""The runner of the group «bayes» (docs/specs/bayes-image.md, docs/specs/bayes-contract.md): `python -I -B /tabularia/code/runner/run.py`
in /tabularia/code, with `--selftest` for the image's self-test. It puts exactly the common library's folder and the runner's own on
sys.path (-I left out PYTHONPATH, the user's site and this file's folder), and hands over to bayes_main, the group's own dispatcher
(tab_protocol.MODES stays the other groups': the four modes of this group are compile, prior_predictive, sample and selftest).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "common" / "py"), str(HERE / "py")]

import bayes_main  # noqa: E402

sys.exit(bayes_main.main(HERE.parent))
