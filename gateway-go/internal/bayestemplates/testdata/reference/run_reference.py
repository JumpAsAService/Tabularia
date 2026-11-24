#!/usr/bin/env python3
"""One reference run of lot T3, inside the group's image (CmdStan 2.40.0), by hand, one at a time.

    python3 run_reference.py --template glm_linear --which ref|tab --golden DIR --out DIR

`ref` runs the hand-written <id>.stan; `tab` runs the program Tabularia generates for the template (the golden
file of `bayescodegen`, byte-equal to its output, checked by the Go suite). The script standardizes the
predictors itself and writes the data file (`ref`: the hand program's names; `tab`: the generated program's
names). 4 chains x 2,000 draws after 1,000 of warm-up, a fixed seed. The draws are read from CmdStan's CSV
line by line (Welford for the mean and the sd, split chains for R-hat, batch means for the MCSEs): never
loaded whole. Writes <out>/<id>.<which>.stats.json.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POSITION = ["glm_linear", "glm_logistic", "glm_poisson", "glm_negbin", "hier_intercepts", "hier_slopes",
            "hier_nested", "ts_ar", "ts_random_walk", "ts_local_level"]
GOLDEN_NAME = {"ts_ar": "ts_ar2"}
WARMUP, DRAWS, CHAINS, BATCH = 1000, 2000, 4, 100
SEEDS = {"ref": 11000, "tab": 22000}


def read_csv(tid):
    with open(os.path.join(HERE, tid + ".csv")) as f:
        rd = csv.DictReader(f)
        rows = list(rd)  # at most 2,000 rows by construction
    return rows


def standardize(v):
    n = len(v)
    m = sum(v) / n
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / (n - 1))
    return [(a - m) / sd for a in v]


def codes(labels):
    """1-based codes in the order of first appearance."""
    seen = {}
    for a in labels:
        seen.setdefault(a, len(seen) + 1)
    return [seen[a] for a in labels], len(seen)


def build(tid, which):
    rows = read_csv(tid)
    n = len(rows)
    col = lambda k, f=float: [f(r[k]) for r in rows]
    fam, _, kind = tid.partition("_")
    if fam == "glm":
        x = standardize(col("x"))
        y = col("y", int if tid != "glm_linear" else float)
        if which == "ref":
            return {"N": n, "X": [[a] for a in x], "y": y}
        return {"N": n, "x": x, "y": y, "Mode": 0}
    if tid in ("hier_intercepts", "hier_slopes"):
        g, j = codes([r["g"] for r in rows])
        x, y = standardize(col("x")), col("y")
        if which == "ref":
            return {"N": n, "J": j, "g": g, "x": x, "y": y}
        return {"N": n, "J_grp": j, "g": g, "x": x, "y": y, "Mode": 0}
    if tid == "hier_nested":
        d, jd = codes([r["d"] for r in rows])
        s, js = codes([r["s"] for r in rows])
        up = [0] * js
        for di, si in zip(d, s):
            up[si - 1] = di
        x, y = standardize(col("x")), col("y")
        if which == "ref":
            return {"N": n, "J_d": jd, "J_s": js, "s": s, "district_of": up, "x": x, "y": y}
        return {"N": n, "J_dst": jd, "J_sch": js, "d": d, "s": s, "x": x, "y": y, "Up_d_sch": up, "Mode": 0}
    y = col("y")  # the series are in time order in the file
    if which == "ref":
        return {"N": n, "y": y}
    idx = list(range(1, n + 1))
    if tid == "ts_ar":
        return {"N": n, "y": y, "N_lag": n - 2, "Rows_lag": idx[2:], "Lag1": idx[1:-1], "Lag2": idx[:-2], "Mode": 0}
    if tid == "ts_random_walk":
        return {"N": n, "y": y, "N_lag": n - 1, "Rows_lag": idx[1:], "Lag1": idx[:-1], "Mode": 0}
    return {"N": n, "y": y, "N_first": 1, "Rows_first": [1], "N_next": n - 1, "Rows_next": idx[1:],
            "Prev": idx[:-1], "Mode": 0}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def draws(path):
    """Yield (header, rows) lazily: header list then one list of floats per draw (comments skipped)."""
    with open(path) as f:
        header = None
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            if header is None:
                header = line.strip().split(",")
                yield header
                continue
            yield [float(v) for v in line.split(",")]


def stats(files):
    first = next(draws(files[0]))
    keep = [i for i, h in enumerate(first) if not h.endswith("__")]
    names = [first[i] for i in keep]
    idiv, itd = first.index("divergent__"), first.index("treedepth__")
    q, nc = len(keep), len(files)
    # pass 1: mean/sd per chain and per half chain (Welford), sampler facts
    def acc():
        return [[0, 0.0, 0.0] for _ in range(q)]
    chain, half = [acc() for _ in range(nc)], [acc() for _ in range(2 * nc)]
    div = td = 0
    ndr = 0
    for c, fn in enumerate(files):
        it = draws(fn)
        next(it)
        k = 0
        for row in it:
            div += row[idiv] > 0
            td += row[itd] >= 10
            hh = half[2 * c + (1 if k >= DRAWS // 2 else 0)]
            for a, i in enumerate(keep):
                v = row[i]
                for s in (chain[c][a], hh[a]):
                    s[0] += 1
                    d = v - s[1]
                    s[1] += d / s[0]
                    s[2] += d * (v - s[1])
            k += 1
        ndr += k
    def merge(parts):
        n = sum(p[0] for p in parts)
        m = sum(p[0] * p[1] for p in parts) / n
        ss = sum(p[2] + p[0] * (p[1] - m) ** 2 for p in parts)
        return n, m, ss
    mean, sd, rhat = [], [], []
    for a in range(q):
        n, m, ss = merge([chain[c][a] for c in range(nc)])
        mean.append(m)
        sd.append(math.sqrt(ss / (n - 1)))
        hs = [half[h][a] for h in range(2 * nc)]
        nh = hs[0][0]
        w = sum(p[2] / (p[0] - 1) for p in hs) / len(hs)
        bm = sum(p[1] for p in hs) / len(hs)
        b = nh * sum((p[1] - bm) ** 2 for p in hs) / (len(hs) - 1)
        var = (nh - 1) / nh * w + b / nh
        rhat.append(math.sqrt(var / w) if w > 0 else float("nan"))
    # pass 2: batch means of the draw and of its squared deviation, BATCH draws a batch, within a chain
    bx, bs = [[] for _ in range(q)], [[] for _ in range(q)]
    for fn in files:
        it = draws(fn)
        next(it)
        sx, ss, k = [0.0] * q, [0.0] * q, 0
        for row in it:
            for a, i in enumerate(keep):
                v = row[i]
                sx[a] += v
                ss[a] += (v - mean[a]) ** 2
            k += 1
            if k == BATCH:
                for a in range(q):
                    bx[a].append(sx[a] / BATCH)
                    bs[a].append(ss[a] / BATCH)
                sx, ss, k = [0.0] * q, [0.0] * q, 0
    out = {}
    for a, nm in enumerate(names):
        nb = len(bx[a])
        vx = sum((u - sum(bx[a]) / nb) ** 2 for u in bx[a]) / (nb - 1)
        vs = sum((u - sum(bs[a]) / nb) ** 2 for u in bs[a]) / (nb - 1)
        out[nm] = {"mean": mean[a], "sd": sd[a], "mcse_mean": math.sqrt(vx / nb),
                   "mcse_sd": (math.sqrt(vs / nb) / (2 * sd[a])) if sd[a] > 0 else 0.0, "rhat": rhat[a]}
    return out, {"draws_total": ndr, "divergences": int(div), "treedepth_at_10": int(td)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, choices=POSITION)
    ap.add_argument("--which", required=True, choices=["ref", "tab"])
    ap.add_argument("--golden", required=True, help="bayescodegen's testdata/golden (tab only)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tid, w = a.template, a.which
    work = "/tmp/c"
    os.makedirs(work, exist_ok=True)
    src = os.path.join(HERE if w == "ref" else a.golden, (tid if w == "ref" else GOLDEN_NAME.get(tid, tid)) + ".stan")
    stan = os.path.join(work, "model.stan")
    shutil.copy(src, stan)
    data = os.path.join(work, "data.json")
    with open(data, "w") as f:
        json.dump(build(tid, w), f)
    t0 = time.time()
    run(["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "make", "-C", "/opt/cmdstan", work + "/model",
         "PRECOMPILED_HEADERS=false"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t_compile = time.time() - t0
    seed = SEEDS[w] + POSITION.index(tid) + 1
    t0 = time.time()
    run([work + "/model", "sample", "num_samples=%d" % DRAWS, "num_warmup=%d" % WARMUP, "num_chains=%d" % CHAINS,
         "data", "file=" + data, "output", "file=" + work + "/out.csv", "refresh=0", "random", "seed=%d" % seed,
         "num_threads=2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t_sample = time.time() - t0
    files = [work + "/out_%d.csv" % (c + 1) for c in range(CHAINS)]
    q, facts = stats(files)
    rec = {"template": tid, "which": w, "seed": seed, "chains": CHAINS, "draws": DRAWS, "warmup": WARMUP,
           "batch": BATCH, "program_sha256": sha(stan), "data_sha256": sha(data), "binary_sha256": sha(work + "/model"),
           "compile_seconds": round(t_compile, 1), "sample_seconds": round(t_sample, 1),
           "csv_bytes": sum(os.path.getsize(f) for f in files), **facts, "quantities": q}
    with open(os.path.join(a.out, "%s.%s.stats.json" % (tid, w)), "w") as f:
        json.dump(rec, f, indent=1)
    print(tid, w, "compile %.1fs sample %.1fs div %d td10 %d maxrhat %.4f" % (
        t_compile, t_sample, facts["divergences"], facts["treedepth_at_10"],
        max((v["rhat"] for v in q.values() if v["rhat"] == v["rhat"]), default=0)))


if __name__ == "__main__":
    sys.exit(main())
