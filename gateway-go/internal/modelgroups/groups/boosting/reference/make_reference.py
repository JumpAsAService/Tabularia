"""The reference values of the group «boosting», computed OUTSIDE Tabularia
(docs/specs/statistical-models.md, «Reference values»): inside the group's image built from
recipe.json (the same packages as the runner), never by the runner's code. Nothing here imports
runner/ or common/: the tables are read and typed here, the validation part is drawn again from
its definition in the spec, the libraries are called directly with the parameters the spec
names, and every metric is computed again by its formula (the AUC by pairs, the calibration
slope by scipy's optimizer rather than Newton's method).

    docker run --rm --memory=2g -v "$PWD/gateway-go/internal/modelgroups/groups/boosting/reference:/ref" \\
      -w /ref tabularia-models-boosting:dev python -I make_reference.py

It writes the small tables of data/ when they are missing (synthetic, fixed seeds; a table once
written is the data, the seeds only made it), then expected.json in the format
common/py/tab_selftest.py reads. The cases of a model this file does not compute (another lot's)
are kept as they are. Threads: 2, the self-test's (tab_protocol.SELFTEST_THREADS), so that
XGBoost's sums are the runner's.

Lot X2 wrote the XGBoost cases (xgboost_cases), lot X3 CatBoost's (catboost_cases): the same tables,
the same preparation (Case) and the same values (case_values), each library called directly
(xgboost_fit, catboost_fit).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
THREADS = 2
EPS = float(np.finfo(np.float64).eps)

# the defaults of the two nodes, from the spec's table «The two nodes»
DEFAULTS = {"class_weight": "none", "threshold": 0.5, "validation": "random", "validation_share": 0.2, "stratify": True,
            "early_stopping": True, "early_stopping_rounds": 50, "stop_metric": "loss", "max_trees": 1000,
            "learning_rate": 0.05, "max_depth": 6, "subsample": 0.8, "seed": 1, "min_child_weight": 1.0,
            "colsample_bytree": 0.8, "reg_lambda": 1.0, "reg_alpha": 0.0, "gamma": 0.0,
            "l2_leaf_reg": 3.0, "rsm": 1.0, "random_strength": 1.0}


# ── the tables ──────────────────────────────────────────────────────────────────
def _write(name: str, df: pd.DataFrame) -> None:
    path = DATA / name
    if path.exists():
        return
    DATA.mkdir(exist_ok=True)
    df.to_csv(path, index=False, na_rep="", float_format="%.6g", lineterminator="\n")
    print(f"wrote {path.name}: {len(df)} rows")


def write_tables() -> None:
    # regression: boxes ordered, five numbers, a region of four levels, 5 % missing in two predictors,
    # weights (three of 0), two rows without the outcome
    g = np.random.Generator(np.random.PCG64(101))
    n = 300
    price = g.uniform(2, 9, n)
    discount = g.choice([0, 0, 0.1, 0.2], n)
    visits = g.poisson(4, n).astype(float)
    tenure = g.uniform(0, 60, n)
    noise_col = g.normal(size=n)
    region = g.choice(["north", "south", "east", "west"], n)
    effect = {"north": 4.0, "south": -2.0, "east": 1.0, "west": 0.0}
    boxes = (30 - 2.5 * price + 25 * discount + 1.2 * visits + 0.1 * tenure + np.array([effect[r] for r in region])
             + g.normal(scale=2.5, size=n))
    df = pd.DataFrame({"boxes": boxes, "price": price, "discount": discount, "visits": visits, "tenure": tenure,
                       "noise": noise_col, "region": region, "w": g.choice([1.0, 1.0, 2.0, 0.5], n)})
    df.loc[g.uniform(size=n) < 0.05, "price"] = np.nan
    df.loc[g.uniform(size=n) < 0.05, "region"] = None
    df.loc[[10, 50, 90], "w"] = 0.0
    df.loc[[7, 77], "boxes"] = np.nan
    _write("regression.csv", df)

    # binary: will a customer reorder? about one in five, a channel, a first-order date (a time in the data)
    g = np.random.Generator(np.random.PCG64(202))
    n = 400
    price = g.uniform(2, 9, n)
    visits = g.poisson(3, n).astype(float)
    days = g.integers(0, 365, n)
    channel = g.choice(["web", "shop", "phone"], n, p=[0.5, 0.35, 0.15])
    eff = {"web": 0.6, "shop": 0.0, "phone": -0.8}
    logit = -2.0 - 0.7 * (price - 5) + 0.6 * (visits - 3) + 1.5 * np.array([eff[c] for c in channel])
    reordered = np.where(logit + g.logistic(size=n) > 0, "yes", "no")
    first = [(dt.date(2025, 1, 1) + dt.timedelta(days=int(d))).isoformat() for d in days]
    df = pd.DataFrame({"reordered": reordered, "price": price, "visits": visits, "channel": channel, "first_order": first})
    df.loc[g.uniform(size=n) < 0.05, "visits"] = np.nan
    _write("binary.csv", df)

    # multiclass: three segments, a store (the groups of a grouped split), a boolean
    g = np.random.Generator(np.random.PCG64(303))
    n = 450
    spend = g.gamma(2.0, 50.0, n)
    orders = g.poisson(5, n).astype(float)
    store = g.choice([f"s{i:02d}" for i in range(15)], n)
    member = g.uniform(size=n) < 0.35
    score = 0.012 * spend + 0.25 * orders + 0.9 * member + g.normal(scale=0.8, size=n)
    segment = np.where(score > 3.4, "gold", np.where(score > 2.2, "silver", "bronze"))
    df = pd.DataFrame({"segment": segment, "spend": spend, "orders": orders, "store": store,
                       "member": np.where(member, "true", "false")})
    _write("multiclass.csv", df)

    # leakage: «echo» is the outcome plus a little noise
    g = np.random.Generator(np.random.PCG64(404))
    n = 200
    x1, x2 = g.normal(size=n), g.normal(size=n)
    y = 0.5 * x1 - 0.3 * x2 + g.normal(scale=2.0, size=n)
    df = pd.DataFrame({"y": y, "x1": x1, "x2": x2, "echo": y + g.normal(scale=0.05, size=n)})
    _write("leakage.csv", df)

    # in time: daily sales over 300 days with a trend, and an event that stops after the first 70 % of the days
    g = np.random.Generator(np.random.PCG64(505))
    n = 300
    day = np.sort(g.integers(0, 240, n))
    temp = 15 + 10 * np.sin(day / 240 * 2 * np.pi) + g.normal(size=n)
    promo = g.choice(["none", "flyer", "radio"], n)
    sales = 50 + 0.08 * day + 1.5 * temp + np.where(promo == "radio", 8, np.where(promo == "flyer", 4, 0)) + g.normal(scale=4, size=n)
    early = np.where((day < 140) & (g.uniform(size=n) < 0.3), "yes", "no")
    df = pd.DataFrame({"sales": sales, "day": [(dt.date(2026, 1, 1) + dt.timedelta(days=int(d))).isoformat() for d in day],
                       "temp": temp, "promo": promo, "early_event": early})
    _write("time.csv", df)

    # too few rows
    g = np.random.Generator(np.random.PCG64(606))
    df = pd.DataFrame({"y": g.normal(size=15), "x": g.normal(size=15)})
    _write("tiny.csv", df)


# ── reading a table, typed by the case ──────────────────────────────────────────
def read(case: dict[str, Any]) -> pd.DataFrame:
    raw = pd.read_csv(HERE / case["data"], dtype=str, keep_default_na=False, na_values=[""])
    df = pd.DataFrame(index=raw.index)
    for c in raw.columns:
        t = case["types"].get(c, "text")
        s = raw[c]
        if t in ("number", "integer"):
            df[c] = pd.to_numeric(s).astype(float)
        elif t == "date":
            df[c] = [None if pd.isna(v) else dt.date.fromisoformat(v) for v in s]
        else:
            df[c] = [None if pd.isna(v) else str(v) for v in s]
    return df


def missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ── the validation part, again from its definition ──────────────────────────────
def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def draw_validation(n: int, p: dict[str, Any], y: np.ndarray | None, k: int, groups: list[str] | None,
                    t: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """(validation mask, training rows in the order the library sees them)."""
    share = p["validation_share"]
    gen = np.random.Generator(np.random.PCG64(int(p["seed"])))
    val = np.zeros(n, dtype=bool)
    if t is not None:
        srt = sorted(range(n), key=lambda i: (t[i], i))
        m = max(1, round_half_up(share * n))
        cut = t[srt[n - m]]
        val = t >= cut
        return val, np.array([i for i in srt if not val[i]], dtype=np.int64)
    if groups is not None:
        names = sorted(set(groups))
        total = 0
        for j in gen.permutation(len(names)):
            if total >= share * n:
                break
            members = [i for i in range(n) if groups[i] == names[j]]
            val[members] = True
            total += len(members)
        return val, np.flatnonzero(~val)
    if y is not None:
        for c in range(k):
            idx = np.flatnonzero(y == c)
            perm = gen.permutation(idx)
            m = round_half_up(share * len(idx))
            m = min(max(m, 1), len(idx) - 1) if len(idx) >= 2 else 0
            val[perm[:m]] = True
        return val, np.flatnonzero(~val)
    perm = gen.permutation(n)
    val[perm[:round_half_up(share * n)]] = True
    return val, np.flatnonzero(~val)


# ── the metrics, by their formulas ──────────────────────────────────────────────
def wavg(x, w):
    return float(np.dot(w, x) / np.sum(w))


def auc_pairs(s, y, w) -> float:
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    num = 0.0
    for i in pos:
        num += w[i] * float(np.sum(w[neg] * ((s[i] > s[neg]) + 0.5 * (s[i] == s[neg]))))
    return num / (float(w[pos].sum()) * float(w[neg].sum()))


def ll(y, p, w) -> float:
    q = np.clip(p, EPS, 1 - EPS)
    return wavg(-(y * np.log(q) + (1 - y) * np.log(1 - q)), w)


def slope_by_optimizer(p, y, w) -> tuple[float, float]:
    """The slope by scipy's optimizer, and its standard error from the information matrix at the
    optimum written out (the weights scaled to sum to the rows)."""
    from scipy.optimize import minimize

    q = np.clip(p, EPS, 1 - EPS)
    x = np.log(q / (1 - q))
    w = w / w.mean()

    def nll(b):
        z = b[0] + b[1] * x
        return float(np.sum(w * (np.logaddexp(0, z) - y * z)))

    def grad(b):
        mu = 1 / (1 + np.exp(-(b[0] + b[1] * x)))
        r = w * (mu - y)
        return np.array([r.sum(), (r * x).sum()])
    res = minimize(nll, np.array([0.0, 1.0]), jac=grad, method="BFGS", options={"gtol": 1e-12, "maxiter": 10000})
    mu = 1 / (1 + np.exp(-(res.x[0] + res.x[1] * x)))
    v = w * mu * (1 - mu)
    i00, i01, i11 = v.sum(), (v * x).sum(), (v * x * x).sum()
    return float(res.x[1]), math.sqrt(i00 / (i00 * i11 - i01 * i01))


def ece_noise_by_halfnormals(p, w) -> float:
    """The calibration error well calibrated probabilities reach 95 % of the time: per bin of the
    ECE (the same bins) a half-normal of scale σ_b = √(Σ w² p (1 − p)) / W_b, by scipy's
    distributions; their weighted sum taken as normal, its 95 % quantile."""
    from scipy.stats import halfnorm, norm

    order = sorted(range(len(p)), key=lambda i: (p[i], i))
    W = float(w.sum())
    acc = 0.0
    bins: dict[int, list[int]] = {}
    for i in order:
        bins.setdefault(min(9, int(math.floor(10 * acc / W))), []).append(i)
        acc += w[i]
    mean = var = 0.0
    for idx in bins.values():
        wb = w[idx]
        h = halfnorm(scale=math.sqrt(float(np.sum(wb ** 2 * p[idx] * (1 - p[idx])))) / wb.sum())
        mean += wb.sum() / W * h.mean()
        var += (wb.sum() / W) ** 2 * h.var()
    return float(norm(mean, math.sqrt(var)).ppf(0.95))


def ece_by_bins(p, y, w) -> tuple[float, list[float]]:
    order = sorted(range(len(p)), key=lambda i: (p[i], i))
    W = float(w.sum())
    acc = 0.0
    bins: dict[int, list[int]] = {}
    for i in order:
        b = min(9, int(math.floor(10 * acc / W)))
        bins.setdefault(b, []).append(i)
        acc += w[i]
    e = 0.0
    for idx in bins.values():
        wb = w[idx]
        e += wb.sum() / W * abs(np.dot(wb, y[idx]) / wb.sum() - np.dot(wb, p[idx]) / wb.sum())
    return e, [len(v) for v in bins.values()]


def verdict(x: float, ok: Any, doubtful: Any) -> str:
    return "ok" if ok(x) else "doubtful" if doubtful(x) else "failed"


# ── a case: its rows, its kinds and its validation part, from the spec ─────────────
class Case:
    """What both libraries are handed, built again from the spec (never by the runner's code)."""

    def __init__(self, case: dict[str, Any]):
        p = self.p = {**DEFAULTS, **case["params"]}
        self.what = what = p["task"]
        df = read(case)
        self.outcome, self.predictors = outcome, predictors = p["outcome"], list(p["predictors"])
        self.weights = weights = p.get("weights")
        self.order_by = order_by = p.get("order_by") if p["validation"] == "time" else None
        self.split_by = split_by = p.get("split_by") if p["validation"] == "random" else None
        keep = np.array([not missing(df[outcome][i]) and (not weights or not missing(df[weights][i]))
                         and (not order_by or not missing(df[order_by][i])) and (not split_by or not missing(df[split_by][i]))
                         for i in range(len(df))])
        self.n_missing = int((~keep).sum())
        if weights:
            keep &= ~(df[weights].to_numpy(dtype=float) == 0)
        self.d = d = df[keep].reset_index(drop=True)
        self.n = n = len(d)
        self.w = w = d[weights].to_numpy(dtype=float) if weights else np.ones(n)
        self.numbers = [c for c in predictors if case["types"].get(c) in ("number", "integer")]
        self.levels = {c: sorted({v for v in d[c] if not missing(v)}) for c in predictors if c not in self.numbers}
        self.classes = classes = []
        if what == "regression":
            self.y = y = d[outcome].to_numpy(dtype=float)
        else:
            found = sorted(set(d[outcome]))
            self.classes = classes = [v for v in found if v != p["event_value"]] + [p["event_value"]] if what == "binary" else found
            self.y = y = np.array([classes.index(v) for v in d[outcome]], dtype=np.int64)
        self.k = len(classes) if what == "multiclass" else 1
        t = None
        if order_by:
            t = np.array([(v - dt.date(1970, 1, 1)).days if isinstance(v, dt.date) else float(v) for v in d[order_by]], dtype=float)
        strat = y if what != "regression" and p["stratify"] else None
        self.val, self.train_order = draw_validation(n, p, strat, len(classes), list(d[split_by]) if split_by else None, t)
        self.tr, self.va = np.flatnonzero(~self.val), np.flatnonzero(self.val)
        self.fw = w.copy()
        if what != "regression" and p["class_weight"] == "balanced":
            W = w[self.tr].sum()
            self.fw = w * np.array([W / (len(classes) * w[self.tr][y[self.tr] == c].sum()) for c in range(len(classes))])[y]
        self.weighted = bool(weights) or p["class_weight"] == "balanced"


class Fit:
    """What a library called directly gave: the raw scores and the SHAP values of every row, the
    native importance, the rounds run, the round kept, the validation loss per round, the names."""

    def __init__(self, **kw: Any):
        self.__dict__.update(kw)


# ── XGBoost, called directly ────────────────────────────────────────────────────
def xgboost_fit(c: Case) -> Fit:
    import xgboost as xgb

    p, what = c.p, c.what
    cols = {}
    for j, name in enumerate(c.predictors):
        if name in c.numbers:
            cols[f"v{j + 1}"] = c.d[name].to_numpy(dtype=float)
        else:
            cols[f"v{j + 1}"] = pd.Categorical([None if missing(v) else v for v in c.d[name]], categories=c.levels[name])
    X = pd.DataFrame(cols)
    loss = {"regression": "rmse", "binary": "logloss", "multiclass": "mlogloss"}[what]
    stop = {"loss": loss, "mae": "mae", "auc": "auc", "error": "merror" if what == "multiclass" else "error"}[p["stop_metric"]]
    params = {"objective": {"regression": "reg:squarederror", "binary": "binary:logistic", "multiclass": "multi:softprob"}[what],
              "eval_metric": [loss] + ([stop] if stop != loss else []), "tree_method": "hist", "max_bin": 256,
              "eta": p["learning_rate"], "max_depth": p["max_depth"], "subsample": p["subsample"],
              "colsample_bytree": p["colsample_bytree"], "min_child_weight": p["min_child_weight"], "lambda": p["reg_lambda"],
              "alpha": p["reg_alpha"], "gamma": p["gamma"], "max_cat_to_onehot": 4, "max_cat_threshold": 64, "seed": p["seed"],
              "nthread": THREADS, "validate_parameters": True}
    if what == "multiclass":
        params["num_class"] = c.k
    to, va = c.train_order, c.va
    dtrain = xgb.QuantileDMatrix(X.iloc[to], label=c.y[to], weight=c.fw[to] if c.weighted else None,
                                 enable_categorical=True, max_bin=256, nthread=THREADS)
    # the validation part weighted as the fit (balanced class weights too): early stopping reads the loss the fit weighs
    dvalid = xgb.QuantileDMatrix(X.iloc[va], label=c.y[va], weight=c.fw[va] if c.weighted else None, ref=dtrain,
                                 enable_categorical=True, max_bin=256, nthread=THREADS)
    log: dict = {}
    callbacks = []
    if p["early_stopping"]:
        callbacks.append(xgb.callback.EarlyStopping(rounds=p["early_stopping_rounds"], metric_name=stop, data_name="validation",
                                                    maximize=stop == "auc"))
    bst = xgb.train(params, dtrain, num_boost_round=p["max_trees"], evals=[(dtrain, "train"), (dvalid, "validation")],
                    evals_result=log, verbose_eval=False, callbacks=callbacks)
    rounds = bst.num_boosted_rounds()
    if p["early_stopping"]:
        best = bst.best_iteration
        bst = bst[: best + 1]
    else:
        best = rounds - 1
    raw = np.asarray(bst.predict(xgb.DMatrix(X, enable_categorical=True, nthread=THREADS), output_margin=True), dtype=float)
    contribs = np.asarray(bst.predict(xgb.DMatrix(X, enable_categorical=True, nthread=THREADS), pred_contribs=True), dtype=float)
    score = bst.get_score(importance_type="total_gain")
    return Fit(library="xgboost", objective=params["objective"], stop=stop, importance_type="total_gain", raw=raw,
               phi=contribs[..., : len(c.predictors)], gain=np.array([score.get(f"v{j + 1}", 0.0) for j in range(len(c.predictors))]),
               rounds=rounds, best=best, vloss=log["validation"][loss])


# ── CatBoost, called directly ───────────────────────────────────────────────────
CATBOOST_MISSING = "\u241fmissing"


def catboost_fit(c: Case) -> Fit:
    """CatBoost with the parameters of the spec's «The fit»: native categories (their labels, the
    sentinel for a missing value), Bernoulli rows, nan_mode Min, the training rows in time order with
    has_time for a split in time, use_best_model with early stopping."""
    from catboost import CatBoostClassifier, CatBoostRegressor, Pool

    p, what = c.p, c.what
    cols = {}
    for j, name in enumerate(c.predictors):
        if name in c.numbers:
            cols[f"v{j + 1}"] = c.d[name].to_numpy(dtype=float)
        else:
            cols[f"v{j + 1}"] = np.array([CATBOOST_MISSING if missing(v) else str(v) for v in c.d[name]], dtype=object)
    X = pd.DataFrame(cols)
    cats = [j for j, name in enumerate(c.predictors) if name not in c.numbers]
    loss = {"regression": "RMSE", "binary": "Logloss", "multiclass": "MultiClass"}[what]
    stop, key = {"loss": (loss, loss), "mae": ("MAE", "MAE"), "auc": ("AUC:hints=skip_train~false", "AUC"),
                 "error": ("ZeroOneLoss", "ZeroOneLoss")}[p["stop_metric"]]
    kw = {"iterations": p["max_trees"], "learning_rate": p["learning_rate"], "depth": p["max_depth"], "l2_leaf_reg": p["l2_leaf_reg"],
          "rsm": p["rsm"], "random_strength": p["random_strength"], "bootstrap_type": "Bernoulli", "subsample": p["subsample"],
          "loss_function": loss, "eval_metric": stop, "nan_mode": "Min", "random_seed": p["seed"], "thread_count": THREADS,
          "use_best_model": p["early_stopping"], "has_time": c.order_by is not None, "allow_writing_files": False,
          "train_dir": "/tmp/catboost", "verbose": False}
    if key != loss:
        kw["custom_metric"] = [loss]
    if p["early_stopping"]:
        kw["early_stopping_rounds"] = p["early_stopping_rounds"]
    to, va = c.train_order, c.va
    # the start: the best constant; CatBoost's own for RMSE and Logloss, for MultiClass the log of the
    # class shares (the fit's weights, the training rows) as the pools' baseline, then the model's bias
    start = None
    if what == "multiclass":
        start = np.log(np.array([c.fw[c.tr][c.y[c.tr] == j].sum() for j in range(c.k)]) / c.fw[c.tr].sum())
    else:
        kw["boost_from_average"] = True
    train = Pool(X.iloc[to], label=c.y[to], weight=c.fw[to] if c.weighted else None, cat_features=cats,
                 baseline=None if start is None else np.tile(start, (len(to), 1)))
    valid = Pool(X.iloc[va], label=c.y[va], weight=c.fw[va] if c.weighted else None, cat_features=cats,
                 baseline=None if start is None else np.tile(start, (len(va), 1)))
    model = (CatBoostRegressor if what == "regression" else CatBoostClassifier)(**kw)
    model.fit(train, eval_set=valid)
    if start is not None:
        model.set_scale_and_bias(1.0, start.tolist())
    log = model.get_evals_result()
    rounds = len(log["validation"][key])
    best = model.get_best_iteration() if p["early_stopping"] else model.tree_count_ - 1
    whole = Pool(X, cat_features=cats)
    raw = np.asarray(model.predict(whole, prediction_type="RawFormulaVal", thread_count=THREADS), dtype=float)
    sv = np.asarray(model.get_feature_importance(data=whole, type="ShapValues", thread_count=THREADS), dtype=float)
    gain = np.asarray(model.get_feature_importance(type="PredictionValuesChange"), dtype=float)
    return Fit(library="catboost", objective=loss, stop=key, importance_type="PredictionValuesChange", raw=raw,
               phi=sv[..., : len(c.predictors)], gain=gain, rounds=rounds, best=best, vloss=log["validation"][loss])


# ── the values a case expects, from a library's numbers ─────────────────────────
def case_values(case: dict[str, Any], fit_with: Any) -> list[dict[str, Any]]:
    from scipy.stats import spearmanr

    c = Case(case)
    f = fit_with(c)
    p, what, d, n, w, y, k = c.p, c.what, c.d, c.n, c.w, c.y, c.k
    predictors, levels, classes = c.predictors, c.levels, c.classes
    tr, va = c.tr, c.va
    order_by, split_by = c.order_by, c.split_by
    raw, phi, gain, rounds, best = f.raw, f.phi, f.gain, f.rounds, f.best
    fw = c.fw  # the fit's weights (balanced class weights times w)

    E: list[dict[str, Any]] = []

    def num(key: str, value: float, **tol: float) -> None:
        E.append({"key": key, "value": None if value is None or not math.isfinite(value) else float(value), **tol})

    def txt(key: str, value: str) -> None:
        E.append({"key": key, "text": value})

    txt("model|||task", what)
    txt("model|||library", f.library)
    txt("model|||objective", f.objective)
    txt("model|||stop_metric", f.stop)
    txt("model|||validation", "time" if order_by else "grouped" if split_by else "random")
    txt("model|||importance_type", f.importance_type)
    for key, v in (("n", n), ("n_missing", c.n_missing), ("n_train", len(tr)), ("n_validation", len(va)),
                   ("n_predictors", len(predictors)), ("n_categorical", len(levels)), ("trees", best + 1), ("best_iteration", best),
                   ("max_trees", p["max_trees"]), ("stopped_early", int(p["early_stopping"] and rounds < p["max_trees"])),
                   ("stopped_by_time", 0), ("threads", THREADS), ("shap_rows", n), ("seed", p["seed"])):
        num(f"model|||{key}", v)

    if what == "regression":
        base = wavg(y[tr], w[tr])
        r = {"train": y[tr] - raw[tr], "validation": y[va] - raw[va]}
        for part, idx in (("train", tr), ("validation", va)):
            e = r[part]
            num(f"model|||{part}_rmse", math.sqrt(wavg(e ** 2, w[idx])))
            num(f"model|||{part}_mae", wavg(np.abs(e), w[idx]))
            num(f"model|||{part}_r_squared", 1 - np.sum(w[idx] * e ** 2) / np.sum(w[idx] * (y[idx] - wavg(y[idx], w[idx])) ** 2))
        b_va = math.sqrt(wavg((y[va] - base) ** 2, w[va]))
        num("model|||baseline_rmse", b_va)
        num("model|||validation_mean", wavg(y[va], w[va]))
        L_va, B_va = math.sqrt(wavg(r["validation"] ** 2, w[va])), b_va
        metric = 1 - np.sum(w[va] * r["validation"] ** 2) / np.sum(w[va] * (y[va] - wavg(y[va], w[va])) ** 2)
        size, size_ok = len(va), (100, 30)
    elif what == "binary":
        pr = 1 / (1 + np.exp(-raw))
        prev = wavg(y[tr].astype(float), w[tr])
        yf = y.astype(float)
        txt("model|||event_value", p["event_value"])
        num("model|||events", float(w[y == 1].sum()))
        num("model|||validation_events", float(w[va][y[va] == 1].sum()))
        for part, idx in (("train", tr), ("validation", va)):
            num(f"model|||{part}_auc", auc_pairs(pr[idx], y[idx], w[idx]))
            num(f"model|||{part}_logloss", ll(yf[idx], pr[idx], w[idx]))
            num(f"model|||{part}_brier", wavg((pr[idx] - yf[idx]) ** 2, w[idx]))
        num("model|||baseline_logloss", ll(yf[va], np.full(len(va), prev), w[va]))
        thr = p["threshold"]
        hat = pr[va] >= thr
        obs = y[va] == 1
        tp, fp = w[va][hat & obs].sum(), w[va][hat & ~obs].sum()
        fn, tn = w[va][~hat & obs].sum(), w[va][~hat & ~obs].sum()
        num("model|||threshold", thr)
        num("model|||validation_accuracy", (tp + tn) / w[va].sum())
        num("model|||validation_sensitivity", tp / (tp + fn))
        num("model|||validation_specificity", tn / (tn + fp))
        num("model|||validation_precision", tp / (tp + fp) if tp + fp > 0 else None)
        num("model|||validation_f1", 2 * tp / (2 * tp + fp + fn))
        for a, la in enumerate(classes):
            for b, lb in enumerate(classes):
                num(f"cell|{la}|{lb}|observed", float(w[va][(y[va] == a) & ((pr[va] >= thr) == bool(b))].sum()))
        L_va, B_va = ll(yf[va], pr[va], w[va]), ll(yf[va], np.full(len(va), prev), w[va])
        if p["class_weight"] == "balanced":  # the losses the fit weighs; the baseline their best constant
            L_va, B_va = ll(yf[va], pr[va], fw[va]), ll(yf[va], np.full(len(va), wavg(yf[tr], fw[tr])), fw[va])
        metric = auc_pairs(pr[va], y[va], w[va])
        size, size_ok = int(min(np.sum(y[va] == 0), np.sum(y[va] == 1))), (30, 10)
        smallest = int(min(np.sum(y[va] == 0), np.sum(y[va] == 1)))
        if smallest >= 10:
            e, _ = ece_by_bins(pr[va], yf[va], w[va])
            noise = ece_noise_by_halfnormals(pr[va], w[va])
            num("assumption|calibration||value", e)
            num("assumption|calibration||threshold", max(0.05, noise), rel=1e-9)
            txt("assumption|calibration||verdict", verdict(e, lambda x: x <= max(0.05, noise), lambda x: x <= max(0.10, noise + 0.05)))
            s, se = slope_by_optimizer(pr[va], yf[va], w[va])
            lo, hi = s - 1.959963984540054 * se, s + 1.959963984540054 * se
            num("assumption|calibration_slope||value", s, rel=1e-6)
            txt("assumption|calibration_slope||verdict", verdict(0, lambda _: hi >= 0.8 and lo <= 1.25, lambda _: hi >= 0.5 and lo <= 2))
        else:
            txt("assumption|calibration||verdict", "not_computed")
            txt("assumption|calibration_slope||verdict", "not_computed")
    else:
        z = raw - raw.max(axis=1, keepdims=True)
        P = np.exp(z) / np.exp(z).sum(axis=1, keepdims=True)
        sh_tr = np.array([w[tr][y[tr] == c].sum() for c in range(k)]) / w[tr].sum()
        sh_va = np.array([w[va][y[va] == c].sum() for c in range(k)]) / w[va].sum()

        def mll(idx, probs, weights=w):
            return wavg(-np.log(np.clip(probs[np.arange(len(idx)), y[idx]], EPS, 1 - EPS)), weights[idx])
        num("model|||n_classes", k)
        num("model|||train_mlogloss", mll(tr, P[tr]))
        num("model|||validation_mlogloss", mll(va, P[va]))
        num("model|||baseline_mlogloss", mll(va, np.tile(sh_tr, (len(va), 1))))
        hat = np.argmax(P, axis=1)
        num("model|||train_accuracy", wavg((hat[tr] == y[tr]).astype(float), w[tr]))
        num("model|||validation_accuracy", wavg((hat[va] == y[va]).astype(float), w[va]))
        recalls, f1s, aucs = [], [], []
        for c, lv in enumerate(classes):
            obs, pre = y[va] == c, hat[va] == c
            tp = w[va][obs & pre].sum()
            prec = tp / w[va][pre].sum() if pre.any() else None
            rec = tp / w[va][obs].sum()
            f1 = 2 * tp / (w[va][obs].sum() + w[va][pre].sum())
            a = auc_pairs(P[va, c], obs.astype(np.int64), w[va])
            recalls.append(rec)
            f1s.append(f1)
            aucs.append(a)
            for stat, v in (("support", w[va][obs].sum()), ("share_train", sh_tr[c]), ("share_validation", sh_va[c]),
                            ("precision", prec), ("recall", rec), ("f1", f1), ("auc_ovr", a)):
                num(f"class|{lv}||{stat}", v)
            for b, lb in enumerate(classes):
                num(f"cell|{lv}|{lb}|observed", float(w[va][obs & (hat[va] == b)].sum()))
            num(f"importance|{predictors[0]}|{lv}|mean_abs_shap", float(np.abs(phi[:, c, 0]).mean()))
        num("model|||validation_balanced_accuracy", float(np.mean(recalls)))
        num("model|||validation_macro_auc", float(np.mean(aucs)))
        num("model|||validation_macro_f1", float(np.mean(f1s)))
        L_va, B_va = mll(va, P[va]), mll(va, np.tile(sh_tr, (len(va), 1)))
        if p["class_weight"] == "balanced":  # the losses the fit weighs; the baseline their best constant
            even = np.array([fw[tr][y[tr] == j].sum() for j in range(k)]) / fw[tr].sum()
            L_va, B_va = mll(va, P[va], fw), mll(va, np.tile(even, (len(va), 1)), fw)
        metric = float(np.mean(aucs))
        size, size_ok = int(np.bincount(y[va], minlength=k).min()), (30, 10)

    # importance: gain, mean |SHAP| (every row is in the sample: n is below 200)
    a = np.abs(phi)
    if what == "multiclass":
        mas, mas_t, mas_v = a.mean(axis=0).sum(axis=0), a[tr].mean(axis=0).sum(axis=0), a[va].mean(axis=0).sum(axis=0)
    else:
        mas, mas_t, mas_v = a.mean(axis=0), a[tr].mean(axis=0), a[va].mean(axis=0)
    ranked = sorted(range(len(predictors)), key=lambda j: (-mas[j], j))
    for j, c in enumerate(predictors):
        miss = float(np.mean([missing(v) for v in d[c]])) if c in levels else float(np.isnan(d[c].to_numpy(dtype=float)).mean())
        for stat, v in (("gain", gain[j]), ("gain_share", gain[j] / gain.sum()), ("mean_abs_shap", mas[j]),
                        ("mean_abs_shap_share", mas[j] / mas.sum()), ("rank", ranked.index(j) + 1),
                        ("mean_abs_shap_train", mas_t[j]), ("mean_abs_shap_validation", mas_v[j]), ("missing_share", miss)):
            num(f"importance|{c}||{stat}", v)

    # the diagnostics: the numbers and the verdicts, by the spec's rules
    num("assumption|validation_size||value", size)
    txt("assumption|validation_size||verdict", verdict(size, lambda x: x >= size_ok[0], lambda x: x >= size_ok[1]))
    u = 1 - L_va / B_va
    num("assumption|beats_baseline||value", u)
    txt("assumption|beats_baseline||verdict", verdict(u, lambda x: x >= 0.05, lambda x: x > 0))
    vloss = f.vloss
    lowest = min(vloss[: best + 1])
    if B_va - lowest > 0:  # the share of the validation gain given back between the best tree and the tree kept
        r = max(0.0, (vloss[best] - lowest) / (B_va - lowest))
        num("assumption|overfitting||value", r, rel=1e-6)
        txt("assumption|overfitting||verdict", verdict(r, lambda x: x <= 0.05, lambda x: x <= 0.25))
    else:  # the validation loss never beats the baseline's: no gain to give back
        txt("assumption|overfitting||verdict", "not_computed")
    if p["early_stopping"]:
        stopped = rounds < p["max_trees"]
        num("assumption|trees_limit||value", best + 1)
        # few trees are read above the default learning rate only: below it they say a weak pattern
        txt("assumption|trees_limit||verdict", "ok" if stopped and (best + 1 >= 20 or p["learning_rate"] <= 0.05) else "doubtful")
    else:
        excess = (vloss[-1] - min(vloss)) / abs(min(vloss))
        num("assumption|trees_limit||value", excess)
        txt("assumption|trees_limit||verdict", "doubtful" if excess > 0.01 else "ok")
    if what != "regression":
        shares = np.array([w[tr][y[tr] == c].sum() for c in range(len(classes))]) / w[tr].sum()
        r_ = shares.max() / shares.min()
        num("assumption|class_imbalance||value", r_)
        txt("assumption|class_imbalance||verdict", verdict(r_, lambda x: x < 4, lambda x: x < 20))
    if len(predictors) >= 3 and len(va) >= 50:
        top_t = sorted(range(len(predictors)), key=lambda j: (-mas_t[j], j))[:10]
        top_v = sorted(range(len(predictors)), key=lambda j: (-mas_v[j], j))[:10]
        union = sorted(set(top_t) | set(top_v))
        rho = float(spearmanr(mas_t[union], mas_v[union]).statistic)
        num("assumption|importance_stability||value", rho)
        txt("assumption|importance_stability||verdict", verdict(rho, lambda x: x >= 0.8, lambda x: x >= 0.5))
    else:
        txt("assumption|importance_stability||verdict", "not_computed")
    zero = [c for j, c in enumerate(predictors) if gain[j] == 0]
    for c in zero:
        txt(f"assumption|zero_importance|{c}|verdict", "doubtful")
    if not zero:
        txt("assumption|zero_importance||verdict", "ok")
    first = predictors[ranked[0]]
    share = mas[ranked[0]] / mas.sum()
    perfect = metric >= 0.98
    if len(predictors) == 1:  # a lone predictor's share is 1: the fit alone is read
        num(f"assumption|leakage|{first}|value", metric)
        txt(f"assumption|leakage|{first}|verdict", "doubtful" if perfect else "ok")
    else:
        num(f"assumption|leakage|{first}|value", share)
        if share >= 0.8 and perfect:
            said = "failed"
        elif perfect or (share >= 0.6 and metric >= 0.95):
            said = "doubtful"
        else:
            said = "ok"
        txt(f"assumption|leakage|{first}|verdict", said)
    return E


def xgboost_cases() -> list[dict[str, Any]]:
    reg = {"boxes": "number", "price": "number", "discount": "number", "visits": "number", "tenure": "number",
           "noise": "number", "w": "number"}
    cases = [
        {"id": "xgboost_regression", "model": "xgboost", "data": "data/regression.csv", "types": reg,
         "params": {"task": "regression", "outcome": "boxes", "weights": "w",
                    "predictors": ["price", "discount", "visits", "tenure", "noise", "region"]}},
        {"id": "xgboost_binary", "model": "xgboost", "data": "data/binary.csv",
         "types": {"price": "number", "visits": "number", "first_order": "date"},
         "params": {"task": "binary", "outcome": "reordered", "event_value": "yes", "predictors": ["price", "visits", "channel"],
                    "stop_metric": "auc", "threshold": 0.3}},
        {"id": "xgboost_multiclass", "model": "xgboost", "data": "data/multiclass.csv",
         "types": {"spend": "number", "orders": "number", "member": "boolean"},
         "params": {"task": "multiclass", "outcome": "segment", "predictors": ["spend", "orders", "member"],
                    "split_by": "store", "stop_metric": "error", "max_depth": 4}},
        {"id": "xgboost_leakage", "model": "xgboost", "data": "data/leakage.csv",
         "types": {"y": "number", "x1": "number", "x2": "number", "echo": "number"},
         "params": {"task": "regression", "outcome": "y", "predictors": ["x1", "x2", "echo"], "early_stopping": False,
                    "max_trees": 150, "learning_rate": 0.1, "max_depth": 3}},
        {"id": "xgboost_time", "model": "xgboost", "data": "data/time.csv",
         "types": {"sales": "number", "temp": "number", "day": "date"},
         "params": {"task": "regression", "outcome": "sales", "predictors": ["temp", "promo"], "validation": "time",
                    "order_by": "day", "validation_share": 0.25, "stop_metric": "mae"}},
    ]
    out = []
    for c in cases:
        c = {**c, "expect": case_values(c, xgboost_fit), "refuses": None}
        out.append(c)
    out.append({"id": "xgboost_refuses_few_rows", "model": "xgboost", "data": "data/tiny.csv", "types": {"y": "number", "x": "number"},
                "params": {"task": "regression", "outcome": "y", "predictors": ["x"]}, "expect": [],
                "refuses": {"code": "too_few_rows", "message": None}})
    out.append({"id": "xgboost_refuses_events_all_in_training", "model": "xgboost", "data": "data/time.csv",
                "types": {"sales": "number", "temp": "number", "day": "date"},
                "params": {"task": "binary", "outcome": "early_event", "event_value": "yes", "predictors": ["temp", "promo"],
                           "validation": "time", "order_by": "day"}, "expect": [],
                "refuses": {"code": "class_absent", "message": None}})
    return out


def catboost_cases() -> list[dict[str, Any]]:
    """CatBoost on the same tables: native categories with missing values (regression, the sentinel),
    the AUC as stop metric with the training AUC (binary), balanced class weights by groups
    (multiclass), a table of numbers only (leakage: the witness walks its trees), has_time (in time)."""
    reg = {"boxes": "number", "price": "number", "discount": "number", "visits": "number", "tenure": "number",
           "noise": "number", "w": "number"}
    cases = [
        {"id": "catboost_regression", "model": "catboost", "data": "data/regression.csv", "types": reg,
         "params": {"task": "regression", "outcome": "boxes", "weights": "w",
                    "predictors": ["price", "discount", "visits", "tenure", "noise", "region"]}},
        {"id": "catboost_binary", "model": "catboost", "data": "data/binary.csv",
         "types": {"price": "number", "visits": "number", "first_order": "date"},
         "params": {"task": "binary", "outcome": "reordered", "event_value": "yes", "predictors": ["price", "visits", "channel"],
                    "stop_metric": "auc", "threshold": 0.3, "max_depth": 4}},
        {"id": "catboost_multiclass", "model": "catboost", "data": "data/multiclass.csv",
         "types": {"spend": "number", "orders": "number", "member": "boolean"},
         "params": {"task": "multiclass", "outcome": "segment", "predictors": ["spend", "orders", "member", "store"],
                    "class_weight": "balanced", "stop_metric": "error", "max_depth": 4, "l2_leaf_reg": 5.0}},
        {"id": "catboost_leakage", "model": "catboost", "data": "data/leakage.csv",
         "types": {"y": "number", "x1": "number", "x2": "number", "echo": "number"},
         "params": {"task": "regression", "outcome": "y", "predictors": ["x1", "x2", "echo"], "early_stopping": False,
                    "max_trees": 150, "learning_rate": 0.1, "max_depth": 3}},
        {"id": "catboost_time", "model": "catboost", "data": "data/time.csv",
         "types": {"sales": "number", "temp": "number", "day": "date"},
         "params": {"task": "regression", "outcome": "sales", "predictors": ["temp", "promo"], "validation": "time",
                    "order_by": "day", "validation_share": 0.25, "stop_metric": "mae", "rsm": 0.5, "random_strength": 2.0}},
    ]
    out = [{**c, "expect": case_values(c, catboost_fit), "refuses": None} for c in cases]
    out.append({"id": "catboost_refuses_few_rows", "model": "catboost", "data": "data/tiny.csv",
                "types": {"y": "number", "x": "number"}, "params": {"task": "regression", "outcome": "y", "predictors": ["x"]},
                "expect": [], "refuses": {"code": "too_few_rows", "message": None}})
    out.append({"id": "catboost_refuses_events_all_in_training", "model": "catboost", "data": "data/time.csv",
                "types": {"sales": "number", "temp": "number", "day": "date"},
                "params": {"task": "binary", "outcome": "early_event", "event_value": "yes", "predictors": ["temp", "promo"],
                           "validation": "time", "order_by": "day"}, "expect": [],
                "refuses": {"code": "class_absent", "message": None}})
    return out


COMPUTED = {"xgboost": xgboost_cases, "catboost": catboost_cases}


def main() -> None:
    write_tables()
    path = HERE / "expected.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"cases": []}
    cases = [c for c in old.get("cases", []) if c["model"] not in COMPUTED]
    for model, make in COMPUTED.items():
        cases += make()
    names = ("numpy", "pandas", "scipy", "xgboost-cpu", "catboost")
    packages = {}
    for p in names:
        try:
            packages[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            pass
    body = {"format_version": 1,
            "generated": {"by": "make_reference.py", "python": platform.python_version(), "packages": packages, "threads": THREADS},
            "cases": cases}
    path.write_text(json.dumps(body, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(f"wrote expected.json: {len(cases)} cases, {sum(len(c['expect']) for c in cases)} values")


if __name__ == "__main__":
    sys.exit(main())
