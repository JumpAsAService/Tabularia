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
   against the exact Shapley values by every coalition, computed here from xgboost.json alone,
   relative 1e-5. shap.TreeExplainer is printed beside, not compared: on an XGBoost model shap
   0.54.1 calls XGBoost's own pred_contribs (its «short circuit»), so it witnesses nothing.

Exit 1 when a value differs.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
GROUP = HERE.parent
sys.path[:0] = [str(GROUP.parents[1] / "common" / "py"), str(GROUP / "runner" / "py")]

import boosting_shared as B  # noqa: E402
import tab_protocol  # noqa: E402

# the catalogue the gateway copies beside the runner is, in the source tree, modelcatalog's
tab_protocol.code_dir = lambda: GROUP.parents[2] / "modelcatalog"

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


def brute_shapley(model: dict[str, Any], V: np.ndarray, rows: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Exact Shapley values of the trees' path-dependent expectation (TreeSHAP's definition: a
    predictor outside the coalition follows both children, weighted by their sum_hessian), by
    every coalition of the p predictors, from xgboost.json alone: rows × classes × p, and the
    value of the empty coalition per class (base_score included, as a margin)."""
    learner = model["learner"]
    gb = learner["gradient_booster"]["model"]
    k = max(1, int(learner["learner_model_param"].get("num_class", "0") or 0))
    p = V.shape[1]
    trees = []
    for t, cls in zip(gb["trees"], gb["tree_info"]):
        cats = {node: set(t["categories"][a:a + z]) for node, a, z in zip(t["categories_nodes"], t["categories_segments"],
                                                                          t["categories_sizes"])}
        trees.append((t, cats, cls))

    def left_of(t, cats, j, v):
        if math.isnan(v):
            return bool(t["default_left"][j])
        if t["split_type"][j] == 1:
            return int(v) not in cats.get(j, set())
        return np.float32(v) < np.float32(t["split_conditions"][j])

    def expect(t, cats, x, S, j=0):
        a, b = t["left_children"][j], t["right_children"][j]
        if a == -1:
            return t["split_conditions"][j]
        f = t["split_indices"][j]
        if f in S:
            return expect(t, cats, x, S, a if left_of(t, cats, j, x[f]) else b)
        ca, cb = t["sum_hessian"][a], t["sum_hessian"][b]
        return (ca * expect(t, cats, x, S, a) + cb * expect(t, cats, x, S, b)) / (ca + cb)

    raw = learner["learner_model_param"]["base_score"]
    base = np.asarray(json.loads(raw) if raw.startswith("[") else [float(raw)], dtype=float)
    if learner["objective"]["name"] == "binary:logistic":
        base = np.log(base / (1 - base))  # saved as a probability, applied as a margin
    base = base if len(base) == k else np.full(k, base[0])
    subsets = [frozenset(S) for r in range(p + 1) for S in itertools.combinations(range(p), r)]
    phi = np.zeros((len(rows), k, p))
    empty = np.zeros(k)
    for ri, i in enumerate(rows):
        x = V[i]
        val = {S: base.copy() for S in subsets}
        for t, cats, cls in trees:
            for S in subsets:
                val[S][cls] += expect(t, cats, x, S)
        empty = val[frozenset()]
        for f in range(p):
            others = [g for g in range(p) if g != f]
            for r in range(p):
                for S in itertools.combinations(others, r):
                    wgt = math.factorial(r) * math.factorial(p - r - 1) / math.factorial(p)
                    phi[ri, :, f] += wgt * (val[frozenset(S) | {f}] - val[frozenset(S)])
    return phi, empty


def xgboost_shap() -> None:
    """Each XGBoost case of expected.json fitted by the runner in this process (the self-test's
    task), its model file read back by a fresh Booster; XGBoost's pred_contribs on 8 rows of the
    case (with missing values among them) against the exact Shapley values by every coalition,
    relative 1e-5 (float32 leaves). shap.TreeExplainer (0.54.1) is called too and said: on an
    XGBoost model it hands the rows to XGBoost's own pred_contribs, so it is not an independent
    witness of XGBoost (it is of CatBoost only if it parses the model itself)."""
    import shap
    import xgboost as xgb

    import boosting_xgboost as X
    from tab_columns import read_csv
    from tab_protocol import catalog, variant_of

    cases = [c for c in json.loads((HERE / "expected.json").read_text(encoding="utf-8")).get("cases", [])
             if c["model"] == "xgboost" and not c.get("refuses")]
    if not cases:
        print("skipped: no XGBoost case in expected.json")
        return
    for case in cases:
        df = read_csv(str(HERE / case["data"]), case.get("types") or {})
        task = {"protocol": 1, "mode": "fit", "group": "boosting", "group_version": "selftest", "model": "xgboost",
                "variant": variant_of("xgboost", case["params"]), "params": case["params"], "output": "results",
                "limits": dict(catalog()["limits"]), "_frame": df}
        out = X.fit_xgboost(task)
        booster = X.load_model_file(out["card"], out["model_file"]["data"])
        frame, _ = B.card_frame(out["card"], df)
        p = frame.shape[1]
        ours = np.asarray(booster.predict(xgb.DMatrix(frame, enable_categorical=True), pred_contribs=True), dtype=float)
        ours = ours.reshape(len(frame), -1, p + 1)
        V = np.column_stack([np.where(frame[c].cat.codes.to_numpy() < 0, np.nan, frame[c].cat.codes.to_numpy())
                             if hasattr(frame[c], "cat") else frame[c].to_numpy(dtype=float) for c in frame.columns])
        has_missing = [i for i in range(len(frame)) if np.isnan(V[i]).any()][:3]
        rows = sorted(set(has_missing + list(range(8 - len(has_missing)))))
        model = json.loads(out["model_file"]["data"])
        phi, empty = brute_shapley(model, V, rows)
        gap = float((np.abs(ours[rows, :, :p] - phi) / np.maximum(1, np.abs(phi))).max())
        same(f"{case['id']}: SHAP on {len(rows)} rows by every coalition (largest relative gap)", gap, 0.0, tol=1e-5)
        gap0 = float((np.abs(ours[rows[0], :, p] - empty) / np.maximum(1, np.abs(empty))).max())
        same(f"{case['id']}: the base value, the empty coalition's", gap0, 0.0, tol=1e-5)
        theirs = np.asarray(shap.TreeExplainer(booster).shap_values(frame.iloc[rows]), dtype=float)
        print(f"said   {case['id']}: shap.TreeExplainer's largest gap {np.abs(theirs.reshape(-1) - np.moveaxis(ours[rows, :, :p], 1, -1).reshape(-1) if theirs.ndim == 3 else theirs.reshape(-1) - ours[rows, 0, :p].reshape(-1)).max():.3g} (XGBoost's own numbers)")


if __name__ == "__main__":
    print("== the metrics against scikit-learn")
    metrics()
    print("== XGBoost's contributions against the exact Shapley values")
    xgboost_shap()
    print(f"{len(failed)} differ" if failed else "every value agrees")
    sys.exit(1 if failed else 0)
