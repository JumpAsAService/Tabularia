#!/usr/bin/env python3
"""The Stan half of the golden test of the priors' marginals (docs/specs/bayes-prior-preview.md, lot P3).

Runs INSIDE the Bayesian group's image (CmdStan 2.40.0, read-only root, no network, uid 10001), one case per call,
by hand and one heavy job at a time (see gateway-go/scripts/bayes-prior-fixtures.sh, which starts the container):

    python3 bayes_prior_fixtures.py --case glm_linear --golden /programs --out /out --seed 4101

For the case the program Tabularia generates with the checks extension (<golden>/<case>.stan and its
manifest, written by `go run ./cmd/bayesfixtures prepare <dir>` from the graphs as they are now) is compiled with the Job's flags and run with
`fixed_param`, `Mode = 1`, 4 chains x 1,000 draws, a fixed seed, on a minimal data file (every group plate 2 units,
a category 3 levels, the rows 6, no rows named for a check). Only the `Prior_m_<k>` columns of the manifest's
`prior_marginal` quantities are kept, read from CmdStan's CSVs line by line (never the whole file), as CmdStan wrote
them (8 significant digits), and written to <out>/<case>.json:

    {"format": 1, "case": ..., "cmdstan": "2.40.0", "seed": ..., "chains": 4, "draws": 1000,
     "program_sha256": ..., "quantities": {"<node id>": ["-0.123", ...], ...}}

A value CmdStan wrote as nan, inf or -inf is kept as a string so the test counts it as a rejected draw.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

CHAINS, DRAWS = 4, 1000


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def data_block(src):
    m = re.search(r"(?:^|\n)data \{(.*?)\n\}", src, re.S)
    return m.group(1) if m else ""


def minimal_data(src):
    """A data file for the program: sizes by name, arrays cycling through their bounds, reals varying."""
    ints, out = {}, {}
    decl = re.compile(r"^\s*(?:(array\[(\w+)\])\s+)?(int|real|vector)(?:\[(\w+)\])?(?:<([^>]*)>)?\s+(\w+);")
    for line in data_block(src).strip().split("\n"):
        m = decl.match(line)
        if not m:
            raise SystemExit("data line not understood: " + line)
        arr, asize, typ, vsize, bounds, name = m.groups()
        bounds = bounds or ""
        upper = re.search(r"upper=(\w+)", bounds)
        lower = re.search(r"lower=(\w+)", bounds)
        size = asize or vsize
        n = None if size is None else (int(size) if size.isdigit() else ints[size])
        if typ == "int" and size is None:
            if name == "Mode":
                v = 1
            elif name == "N":
                v = 6
            elif name in ("N_first", "Prior_first_n"):
                v = 1  # a state: its first row...
            elif name in ("N_next", "Prior_next_n"):
                v = 5  # ...and the five that follow it (N - 1)
            elif name.startswith("J_"):
                v = 2
            elif name.startswith("K_"):
                v = 3
            elif lower and lower.group(1) == "1":
                v = 2
            else:
                v = 0  # Ppc_*, Prior_*, N_lag...: no row named, no lag, the checks off
            ints[name] = v
            out[name] = v
        elif typ == "int":
            top = 1
            if name in ("Rows_first", "Prior_first"):
                out[name] = [1]
                continue
            if name in ("Rows_next", "Prior_next"):
                out[name] = [i + 2 for i in range(n)]
                continue
            if name in ("Prev", "Prior_prev"):
                out[name] = [i + 1 for i in range(n)]
                continue
            if upper:
                u = upper.group(1)
                top = int(u) if u.isdigit() else ints.get(u, 1)
            out[name] = [(i % max(top, 1)) + 1 if lower and lower.group(1) == "1" else (i % 2 if upper else 0) for i in range(n)]
        elif typ == "real" and size is None:
            out[name] = 0.5
        else:
            out[name] = [round(0.3 * (i + 1) - 0.1 * ((i * 7) % 5), 3) for i in range(n)]
    return out


def run(cmd, **kw):
    r = subprocess.run(cmd, **kw)
    if r.returncode:
        sys.stderr.write((r.stderr or b"").decode(errors="replace")[-2000:])
        raise SystemExit("failed: " + cmd[0] + " " + " ".join(cmd[1:3]))


def read_columns(files, names):
    """The named columns of the CSVs, line by line: {name: [text, ...]} in chain order."""
    out = {n: [] for n in names}
    for fn in files:
        idx = None
        with open(fn) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cells = line.rstrip("\n").split(",")
                if idx is None:
                    idx = {n: cells.index(n) for n in names}
                    continue
                for n, i in idx.items():
                    out[n].append(cells[i])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--work", default="/tmp/c")
    a = ap.parse_args()
    stan_src = os.path.join(a.golden, "%s.stan" % a.case)
    manifest = json.load(open(os.path.join(a.golden, "%s.manifest.json" % a.case)))
    os.makedirs(a.work, exist_ok=True)
    stan = os.path.join(a.work, "model.stan")
    with open(stan_src) as f, open(stan, "w") as g:
        src = f.read()
        g.write(src)
    if sha(stan) != manifest["program_sha256"]:
        raise SystemExit("the program does not match its manifest")
    data = os.path.join(a.work, "data.json")
    with open(data, "w") as f:
        json.dump(minimal_data(src), f)
    run(["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "make", "-C", "/opt/cmdstan", a.work + "/model",
         "PRECOMPILED_HEADERS=false"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    run([a.work + "/model", "sample", "algorithm=fixed_param", "num_samples=%d" % DRAWS, "num_chains=%d" % CHAINS,
         "init=0", "data", "file=" + data, "output", "file=" + a.work + "/out.csv", "refresh=0", "random", "seed=%d" % a.seed],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    files = [a.work + "/out_%d.csv" % (c + 1) for c in range(CHAINS)]
    version = ""
    with open(files[0]) as f:
        for line in f:
            m = re.match(r"# stan_version_(major|minor|patch) = (\d+)", line)
            if m:
                version += ("." if version else "") + m.group(2)
            if not line.startswith("#"):
                break
    marg = [(q["stan"], q["of"]) for q in manifest["quantities"] if q.get("role") == "prior_marginal"]
    cols = read_columns(files, [s for s, _ in marg])
    rec = {"format": 1, "case": a.case, "cmdstan": "2.40.0", "stan": version, "seed": a.seed, "chains": CHAINS,
           "draws": DRAWS, "program_sha256": manifest["program_sha256"],
           "quantities": {node: cols[s] for s, node in marg}}
    with open(os.path.join(a.out, a.case + ".json"), "w") as f:
        json.dump(rec, f, separators=(",", ":"))
    print(a.case, "ok", {k: len(v) for k, v in rec["quantities"].items()})


if __name__ == "__main__":
    sys.exit(main())
