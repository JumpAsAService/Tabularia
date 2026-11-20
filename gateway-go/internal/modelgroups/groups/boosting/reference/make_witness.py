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
3. CatBoost (lot X3): each CatBoost case of numbers only (catboost_leakage) fitted by the runner,
   its catboost.cbm read back and exported to CatBoost's JSON (the witness's only use of it: that
   JSON is complete only without categorical features): the raw scores by an oblivious-tree walker
   in numpy against CatBoost's and against the runner's pred_value (relative 1e-9);
   PredictionValuesChange recomputed from the leaves' values and weights by its documented
   formula, normalized to 100, against the runner's gain (1e-6); CatBoost's ShapValues against
   shap's own TreeSHAP on the oblivious trees rewritten as binary trees (1e-5; shap.TreeExplainer
   on a CatBoost model calls CatBoost's ShapValues, so it is printed beside, not compared).

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


def catboost_walk(j: dict[str, Any], V: np.ndarray) -> np.ndarray:
    """The raw scores of CatBoost's JSON (numbers only): in each oblivious tree, split i sets bit i
    of the leaf's index when the value (in float32, as CatBoost compares) is above its border, a
    missing value as its feature's nan_value_treatment says; the leaves summed, then scale and bias."""
    treat = {f["feature_index"]: f.get("nan_value_treatment", "AsIs") for f in j["features_info"]["float_features"]}
    scale, bias = j["scale_and_bias"]
    bias = np.atleast_1d(np.asarray(bias, dtype=float))
    k = len(bias)
    V32 = V.astype(np.float32)
    out = np.zeros((len(V), k))
    for t in j["oblivious_trees"]:
        idx = np.zeros(len(V), dtype=np.int64)
        for i, s in enumerate(t["splits"]):
            x = V32[:, s["float_feature_index"]]
            bit = x > np.float32(s["border"])
            bit[np.isnan(x)] = treat[s["float_feature_index"]] == "AsTrue"
            idx |= bit.astype(np.int64) << i
        out += np.asarray(t["leaf_values"], dtype=float).reshape(-1, k)[idx]
    out = scale * out + bias
    return out[:, 0] if k == 1 else out


def catboost_pvc(j: dict[str, Any], p: int) -> np.ndarray:
    """PredictionValuesChange by its documented formula: for every split, every pair of leaves that
    differ only by it, Σ (v1 − avr)² c1 + (v2 − avr)² c2 with avr = (v1 c1 + v2 c2) / (c1 + c2),
    c the leaves' weights; per feature, normalized to 100."""
    F = np.zeros(p)
    for t in j["oblivious_trees"]:
        c = np.asarray(t["leaf_weights"], dtype=float)
        v = np.asarray(t["leaf_values"], dtype=float).reshape(len(c), -1)
        for i, s in enumerate(t["splits"]):
            for lo in range(len(c)):
                hi = lo | 1 << i
                if lo >> i & 1 or c[lo] + c[hi] == 0:
                    continue
                avr = (v[lo] * c[lo] + v[hi] * c[hi]) / (c[lo] + c[hi])
                F[s["float_feature_index"]] += float(np.sum((v[lo] - avr) ** 2 * c[lo] + (v[hi] - avr) ** 2 * c[hi]))
    return 100 * F / F.sum()


def catboost_as_binary_trees(j: dict[str, Any]) -> dict[str, Any]:
    """The oblivious trees as binary trees in shap's own format: the LAST split of a tree at its root
    (the leaf index's highest bit), down to the first; x <= border goes left, as shap's TreeSHAP
    reads; a node's cover is its leaves' weights (a leaf no training row reached gets 1e-12, which
    shap needs to walk it and which changes nothing at 1e-5)."""
    scale, bias = j["scale_and_bias"]
    trees = []
    for t in j["oblivious_trees"]:
        sp = t["splits"]
        vals = np.asarray(t["leaf_values"], dtype=float) * scale
        w = np.maximum(np.asarray(t["leaf_weights"], dtype=float), 1e-12)
        left: list[int] = []
        right: list[int] = []
        feat: list[int] = []
        thr: list[float] = []
        value: list[float] = []
        cover: list[float] = []

        def node(level: int, leaf: int) -> int:
            me = len(left)
            left.append(-1), right.append(-1), feat.append(-1), thr.append(0.0), value.append(0.0), cover.append(0.0)
            if level < 0:
                value[me], cover[me] = vals[leaf], w[leaf]
                return me
            lo, hi = node(level - 1, leaf), node(level - 1, leaf | 1 << level)
            left[me], right[me] = lo, hi
            feat[me], thr[me] = sp[level]["float_feature_index"], float(np.float32(sp[level]["border"]))
            cover[me] = cover[lo] + cover[hi]
            return me
        node(len(sp) - 1, 0)
        trees.append({"children_left": np.array(left), "children_right": np.array(right),
                      "children_default": np.array(left), "features": np.array(feat), "thresholds": np.array(thr),
                      "values": np.array(value)[:, None], "node_sample_weight": np.array(cover)})
    return {"trees": trees, "base_offset": float(np.atleast_1d(bias)[0])}


def catboost_trees() -> None:
    """Lot X3's part: the CatBoost cases of numbers only, fitted by the runner."""
    import os
    import tempfile

    import shap

    import boosting_catboost as C
    from tab_columns import read_csv
    from tab_protocol import catalog, variant_of

    cases = [c for c in json.loads((HERE / "expected.json").read_text(encoding="utf-8")).get("cases", [])
             if c["model"] == "catboost" and not c.get("refuses")
             and all((c.get("types") or {}).get(x) in ("number", "integer") for x in c["params"]["predictors"])]
    if not cases:
        print("skipped: no CatBoost case of numbers only in expected.json")
        return
    for case in cases:
        df = read_csv(str(HERE / case["data"]), case.get("types") or {})
        params = case["params"]
        task = {"protocol": 1, "mode": "fit", "group": "boosting", "group_version": "selftest", "model": "catboost",
                "variant": variant_of("catboost", params), "params": params, "output": "rows",
                "limits": dict(catalog()["limits"]), "_frame": df}
        out = C.fit_catboost(task)
        model = C.load_model_file(out["card"], out["model_file"]["data"])
        frame, _ = B.card_frame(out["card"], df)
        with tempfile.TemporaryDirectory() as tmp:
            model.save_model(os.path.join(tmp, "model.json"), format="json")
            j = json.loads(Path(tmp, "model.json").read_text(encoding="utf-8"))
        V = frame.to_numpy(dtype=float)
        walked = catboost_walk(j, V)
        raw = C.raw_scores(model, frame, 2)
        same(f"{case['id']}: the walker against CatBoost's raw scores, largest relative gap over {walked.size}",
             float((np.abs(walked - raw) / np.maximum(1, np.abs(raw))).max()), 0.0, 1e-9)
        if params["task"] == "regression":
            mine = out["output"]["pred_value"].to_numpy(dtype=float)
            same(f"{case['id']}: the runner's pred_value against the walker, largest gap",
                 float((np.abs(mine - walked) / np.maximum(1, np.abs(walked))).max()), 0.0, 1e-9)
        rows = out["rows"]
        at = {k: i for i, k in enumerate(rows.key)}
        pvc = catboost_pvc(j, len(params["predictors"]))
        for f, name in enumerate(params["predictors"]):
            same(f"{case['id']}: PredictionValuesChange of {name}", rows.value[at[f"importance|{name}||gain"]], pvc[f], 1e-6)
        p = len(params["predictors"])
        sv = np.asarray(model.get_feature_importance(data=C.pool(frame), type="ShapValues"), dtype=float)
        if sv.ndim == 2:
            ex = shap.TreeExplainer(catboost_as_binary_trees(j), feature_perturbation="tree_path_dependent")
            theirs = np.asarray(ex.shap_values(V.astype(np.float32).astype(np.float64)), dtype=float)
            same(f"{case['id']}: SHAP values against shap's TreeSHAP, largest gap over {theirs.size}",
                 float(np.abs(sv[:, :p] - theirs).max()), 0.0, 1e-5)
            same(f"{case['id']}: the base value", float(np.atleast_1d(ex.expected_value)[0]), float(sv[0, p]), 1e-5)
        beside = np.asarray(shap.TreeExplainer(model).shap_values(C.cat_frame(frame)), dtype=float)
        print(f"   beside: shap.TreeExplainer on the CatBoost model, largest gap {float(np.abs(sv[..., :p] - beside).max()):.3g} "
              "(it calls CatBoost's own ShapValues)")


if __name__ == "__main__":
    print("== the metrics against scikit-learn")
    metrics()
    print("== XGBoost's contributions against the exact Shapley values")
    xgboost_shap()
    print("== CatBoost: an oblivious-tree walker, PredictionValuesChange by hand, shap's TreeSHAP")
    catboost_trees()
    print(f"{len(failed)} differ" if failed else "every value agrees")
    sys.exit(1 if failed else 0)
