"""The runner of the group «bayes» (docs/specs/bayes-image.md, docs/specs/bayes-contract.md): `python -I -B
/tabularia/code/runner/run.py --selftest` in /tabularia/code. It puts exactly the common library's folder and the runner's own on
sys.path (-I left out PYTHONPATH, the user's site and this file's folder).

This version runs the image's self-test only (lot B0 of docs/specs/bayes-image.md): the runs of a model — the task reader, the
compile and the sampling — come with lots K1 to K3 of docs/specs/bayes-contract.md, which give the group its own dispatcher
(tab_protocol.MODES stays the other groups'). A task is refused, with the log's TABULARIA_ERROR line.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "common" / "py"), str(HERE / "py")]

import bayes_selftest  # noqa: E402
from tab_protocol import EXIT_REFUSED, to_json  # noqa: E402

if "--selftest" in sys.argv[1:]:
    sys.exit(bayes_selftest.main(HERE.parent))
print("TABULARIA_ERROR " + to_json({"code": "not_built", "message": "the Bayesian models are not built yet in this version of "
                                    "Tabularia: its image runs its self-test only", "params": {"what": "bayes_model"}}), flush=True)
sys.exit(EXIT_REFUSED)
