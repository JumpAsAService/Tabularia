#!/usr/bin/env python3
"""Reference data of the ten Bayesian templates (lot T3 of docs/specs/bayes-templates.md).

One CSV per template, drawn from KNOWN parameters written below, with NumPy's PCG64 and the fixed seed
20261123 + the template's position (1 to 10, the order of POSITION). Predictors are drawn on their own scale
(a mean of 50, an sd of 10) so that the standardization of the run is exercised. At most 2,000 rows.

Run it inside the group's image (numpy==2.5.5):
    python3 make_reference.py --template glm_linear      # or --all
"""
import argparse
import csv
import os

import numpy as np

POSITION = ["glm_linear", "glm_logistic", "glm_poisson", "glm_negbin", "hier_intercepts",
            "hier_slopes", "hier_nested", "ts_ar", "ts_random_walk", "ts_local_level"]
BASE_SEED = 20261123
HERE = os.path.dirname(os.path.abspath(__file__))


def rng_for(tid):
    return np.random.Generator(np.random.PCG64(BASE_SEED + POSITION.index(tid) + 1))


def zx(x):
    return (x - 50.0) / 10.0  # the standardized scale on which the real slopes are written


def glm_linear(r):
    # standardized-scale truth: alpha 1.5, slope 0.8 per sd of x, sigma 1.2
    n = 300
    x = np.round(r.normal(50, 10, n), 3)
    y = np.round(1.5 + 0.8 * zx(x) + r.normal(0, 1.2, n), 4)
    return ["x", "y"], [x, y]


def glm_logistic(r):
    n = 400  # alpha -0.5, slope 1.0
    x = np.round(r.normal(50, 10, n), 3)
    p = 1 / (1 + np.exp(-(-0.5 + 1.0 * zx(x))))
    return ["x", "y"], [x, (r.random(n) < p).astype(int)]


def glm_poisson(r):
    n = 300  # alpha 1.0, slope 0.4
    x = np.round(r.normal(50, 10, n), 3)
    return ["x", "y"], [x, r.poisson(np.exp(1.0 + 0.4 * zx(x)))]


def glm_negbin(r):
    n = 500  # alpha 1.5, slope 0.5, phi 4
    x = np.round(r.normal(50, 10, n), 3)
    mu = np.exp(1.5 + 0.5 * zx(x))
    phi = 4.0
    y = r.poisson(r.gamma(phi, mu / phi))
    return ["x", "y"], [x, y]


def hier_intercepts(r):
    j, per = 12, 25  # mu_a 2, tau_a 1, slope 0.8, sigma 1.5
    a = 2.0 + 1.0 * r.normal(size=j)
    g = np.repeat(np.arange(j), per)
    x = np.round(r.normal(50, 10, j * per), 3)
    y = np.round(a[g] + 0.8 * zx(x) + r.normal(0, 1.5, j * per), 4)
    return ["g", "x", "y"], [np.array(["g%02d" % (k + 1) for k in g]), x, y]


def hier_slopes(r):
    j, per = 15, 20  # mu_a 1, mu_b 0.5, tau_a 1, tau_b 0.4, rho 0.5, sigma 1
    tau = np.array([1.0, 0.4])
    cov = np.array([[1.0, 0.5], [0.5, 1.0]]) * np.outer(tau, tau)
    eff = r.multivariate_normal([1.0, 0.5], cov, j)
    g = np.repeat(np.arange(j), per)
    x = np.round(r.normal(50, 10, j * per), 3)
    y = np.round(eff[g, 0] + eff[g, 1] * zx(x) + r.normal(0, 1.0, j * per), 4)
    return ["g", "x", "y"], [np.array(["g%02d" % (k + 1) for k in g]), x, y]


def hier_nested(r):
    nd, ns_per, per = 6, 3, 15  # mu 3, tau_d 1, tau_s 0.7, slope 0.6, sigma 1
    ud = 3.0 + 1.0 * r.normal(size=nd)
    ns = nd * ns_per
    dist_of_school = np.repeat(np.arange(nd), ns_per)
    us = ud[dist_of_school] + 0.7 * r.normal(size=ns)
    s = np.repeat(np.arange(ns), per)
    x = np.round(r.normal(50, 10, ns * per), 3)
    y = np.round(us[s] + 0.6 * zx(x) + r.normal(0, 1.0, ns * per), 4)
    d = dist_of_school[s]
    return ["d", "s", "x", "y"], [np.array(["d%d" % (k + 1) for k in d]),
                                  np.array(["s%02d" % (k + 1) for k in s]), x, y]


def ts_ar(r):
    n = 400  # AR(2): mu 2, phi1 0.5, phi2 0.2, sigma 1
    mu, p1, p2, sg = 2.0, 0.5, 0.2, 1.0
    y = np.zeros(n)
    y[0], y[1] = mu + r.normal(0, 1.5, 2)
    for t in range(2, n):
        y[t] = mu + p1 * (y[t - 1] - mu) + p2 * (y[t - 2] - mu) + r.normal(0, sg)
    return ["t", "y"], [np.arange(1, n + 1), np.round(y, 4)]


def ts_random_walk(r):
    n = 300  # drift 0.15, sigma 0.8
    y = np.zeros(n)
    for t in range(1, n):
        y[t] = y[t - 1] + 0.15 + r.normal(0, 0.8)
    return ["t", "y"], [np.arange(1, n + 1), np.round(y, 4)]


def ts_local_level(r):
    n = 200  # initial level 1, sigma_level 0.3, sigma_obs 0.8
    lv = np.zeros(n)
    lv[0] = 1.0
    for t in range(1, n):
        lv[t] = lv[t - 1] + r.normal(0, 0.3)
    return ["t", "y"], [np.arange(1, n + 1), np.round(lv + r.normal(0, 0.8, n), 4)]


MAKERS = {f.__name__: f for f in [glm_linear, glm_logistic, glm_poisson, glm_negbin, hier_intercepts, hier_slopes,
                                  hier_nested, ts_ar, ts_random_walk, ts_local_level]}


def write(tid, out_dir):
    names, cols = MAKERS[tid](rng_for(tid))
    n = len(cols[0])
    assert n <= 2000
    with open(os.path.join(out_dir, tid + ".csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(names)
        for i in range(n):
            w.writerow([c[i].item() if hasattr(c[i], "item") else c[i] for c in cols])
    print(tid, n, "rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", choices=POSITION)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args()
    for t in (POSITION if a.all else [a.template]):
        write(t, a.out)
