"""The witnesses of the group «boosting» (docs/specs/statistical-models.md, «Reference values»):
packages that compute the same numbers OUTSIDE Tabularia's image, pinned in witness.json, run by
hand in a scratch environment:

    uv venv --python 3.14 /tmp/witness
    uv pip install --python /tmp/witness/bin/python $(python3 -c "import json; print(' '.join(json.load(open('gateway-go/internal/modelgroups/groups/boosting/reference/witness.json'))['python']))")
    /tmp/witness/bin/python -I gateway-go/internal/modelgroups/groups/boosting/reference/make_witness.py

1. The metrics (lot X1): the runner's hand-written metrics (runner/py/boosting_shared.py) against
   scikit-learn's, all with sample_weight, on synthetic arrays drawn here (ties, weights of 0,
   probabilities at 0 and 1), relative 1e-10: mean_squared_error (its root), mean_absolute_error,
   r2_score, roc_auc_score, roc_curve (every threshold), log_loss (binary and multiclass),
   brier_score_loss, confusion_matrix, f1_score (binary, and macro over the classes present),
   balanced_accuracy_score, accuracy_score.
2. XGBoost (lot X2): for each reference case of expected.json the runner's fit, then its model
   file read back by a fresh xgboost.Booster: the contributions XGBoost computes (pred_contribs)
   against shap.TreeExplainer on the same booster and rows, absolute 1e-5.

Exit 1 when a value differs.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
GROUP = HERE.parent
sys.path[:0] = [str(GROUP.parents[1] / "common" / "py"), str(GROUP / "runner" / "py")]

import boosting_shared as B  # noqa: E402

TOL = 1e-10
failed: list[str] = []


def same(name: str, ours: float, theirs: float, tol: float = TOL) -> None:
    ours, theirs = float(ours), float(theirs)
    gap = abs(ours - theirs)
    ok = gap <= tol * max(1.0, abs(theirs))
    print(f"{'ok    ' if ok else 'DIFFERS'} {name}: ours {ours!r}, witness {theirs!r}, gap {gap:.3g}")
    if not ok:
        failed.append(name)


def metrics() -> None:
    from sklearn import metrics as sk

    gen = np.random.Generator(np.random.PCG64(20261120))
    for case in range(4):
        n = 500
        w = gen.integers(0, 5, size=n).astype(float) if case % 2 else np.ones(n)
        w[:3] = [0.0, 2.5, 0.5] if case % 2 else w[:3]
        # regression
        y = gen.normal(size=n) * 3 + 1
        pred = y + gen.normal(size=n)
        same(f"[{case}] RMSE", B.rmse(y, pred, w), math.sqrt(sk.mean_squared_error(y, pred, sample_weight=w)))
        same(f"[{case}] MAE", B.mae(y, pred, w), sk.mean_absolute_error(y, pred, sample_weight=w))
        same(f"[{case}] R²", B.r_squared(y, pred, w), sk.r2_score(y, pred, sample_weight=w))
        # binary: probabilities rounded (ties), some exactly 0 and 1
        p = np.round(gen.uniform(size=n), 2 if case < 2 else 1)
        p[5:8] = [0.0, 1.0, 1.0]
        yb = (gen.uniform(size=n) < p).astype(np.int64)
        yb[5:8] = [1, 0, 1]
        same(f"[{case}] AUC", B.auc(p, yb, w), sk.roc_auc_score(yb, p, sample_weight=w))
        fpr, tpr = B.roc(p, yb, w)
        sf, st, _ = sk.roc_curve(yb, p, sample_weight=w, drop_intermediate=False)
        if len(sf) == len(fpr) and np.allclose(sf, fpr, rtol=0, atol=1e-12) and np.allclose(st, tpr, rtol=0, atol=1e-12):
            print(f"ok     [{case}] ROC: {len(fpr)} points, every threshold")
        else:
            print(f"DIFFERS [{case}] ROC: {len(fpr)} points, witness {len(sf)}")
            failed.append(f"[{case}] ROC")
        same(f"[{case}] log-loss", B.logloss(yb.astype(float), p, w), sk.log_loss(yb, p, sample_weight=w, labels=[0, 1]))
        same(f"[{case}] Brier", B.brier(yb.astype(float), p, w), sk.brier_score_loss(yb, p, sample_weight=w))
        thr = 0.5
        hat = (p >= thr).astype(np.int64)
        C = B.confusion(yb, hat, w, 2)
        Csk = sk.confusion_matrix(yb, hat, sample_weight=w, labels=[0, 1])
        same(f"[{case}] confusion matrix (largest gap)", float(np.abs(C - Csk).max()), 0.0)
        at = B.binary_at(C)
        same(f"[{case}] accuracy", at["accuracy"], sk.accuracy_score(yb, hat, sample_weight=w))
        same(f"[{case}] sensitivity", at["sensitivity"], sk.recall_score(yb, hat, sample_weight=w))
        same(f"[{case}] specificity", at["specificity"], sk.recall_score(yb, hat, sample_weight=w, pos_label=0))
        same(f"[{case}] precision", at["precision"], sk.precision_score(yb, hat, sample_weight=w))
        same(f"[{case}] F1", at["f1"], sk.f1_score(yb, hat, sample_weight=w))
        # multiclass: four classes, one never observed in this part (the classes present count)
        k = 4
        logits = gen.normal(size=(n, k))
        logits[:, 3] -= 30
        Pr = B.softmax(logits)
        yk = np.array([gen.choice(k, p=row) for row in B.softmax(logits * 1.5)])
        yk[yk == 3] = 0
        present = sorted(set(yk.tolist()))
        same(f"[{case}] multiclass log-loss", B.mlogloss(yk, Pr, w), sk.log_loss(yk, Pr, sample_weight=w, labels=list(range(k))))
        hat = np.argmax(Pr, axis=1)
        C = B.confusion(yk, hat, w, k)
        pc = B.per_class(C)
        on = pc["support"] > 0
        same(f"[{case}] multiclass accuracy", float(np.trace(C) / C.sum()), sk.accuracy_score(yk, hat, sample_weight=w))
        same(f"[{case}] balanced accuracy", float(np.mean(pc["recall"][on])), sk.balanced_accuracy_score(yk, hat, sample_weight=w))
        same(f"[{case}] macro F1 (classes present)", float(np.mean(pc["f1"][on])),
             sk.f1_score(yk, hat, sample_weight=w, average="macro", labels=present))
        for c in present:
            same(f"[{case}] AUC one against the rest, class {c}", B.auc(Pr[:, c], (yk == c).astype(np.int64), w),
                 sk.roc_auc_score((yk == c).astype(np.int64), Pr[:, c], sample_weight=w))


def xgboost_shap() -> None:
    """Lot X2's part, written with the XGBoost node."""
    print("skipped: the XGBoost node is not built yet")


if __name__ == "__main__":
    print("== the metrics against scikit-learn")
    metrics()
    print("== XGBoost's contributions against shap.TreeExplainer")
    xgboost_shap()
    print(f"{len(failed)} differ" if failed else "every value agrees")
    sys.exit(1 if failed else 0)
