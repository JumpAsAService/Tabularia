#!/usr/bin/env python3
"""The comparison of lot T3 (docs/specs/bayes-templates.md, «Reference values»), pure standard library.

    python3 compare.py --stats DIR --image-id sha256:... --out .

For every quantity present in both runs and every one of mean and sd (a quantity whose sd is 0 in both is a constant
and is skipped): z = (tab - ref) / sqrt(mcse_tab^2 + mcse_ref^2), the two-sided normal p-value, Holm-adjusted over
EVERY comparison of the ten templates, each compared with alpha 0.001. Writes <id>.expected.json (the hand-written
program's posterior with its header) and compare.json (the records; the last record of a template names its version).
"""
import argparse
import hashlib
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
POSITION = ["glm_linear", "glm_logistic", "glm_poisson", "glm_negbin", "hier_intercepts", "hier_slopes",
            "hier_nested", "ts_ar", "ts_random_walk", "ts_local_level"]
ALPHA = 0.001
CMDSTAN = "2.40.0"


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def template_versions():
    gw = os.path.normpath(os.path.join(HERE, ".."))
    with open(os.path.join(gw, "..", "templates.json")) as f:
        return {t["id"]: t["version"] for t in json.load(f)["templates"]}


def r6(v):
    return float("%.6g" % v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", required=True)
    ap.add_argument("--image-id", required=True)
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args()
    versions = template_versions()
    scripts = {n: sha(os.path.join(HERE, n)) for n in ("make_reference.py", "run_reference.py", "compare.py")}
    runs, rows = {}, []
    for tid in POSITION:
        ref = json.load(open(os.path.join(a.stats, tid + ".ref.stats.json")))
        tab = json.load(open(os.path.join(a.stats, tid + ".tab.stats.json")))
        runs[tid] = (ref, tab)
        for q in sorted(set(ref["quantities"]) & set(tab["quantities"])):
            r, t = ref["quantities"][q], tab["quantities"][q]
            if r["sd"] == 0 and t["sd"] == 0:
                continue
            for stat in ("mean", "sd"):
                se = math.sqrt(t["mcse_" + stat] ** 2 + r["mcse_" + stat] ** 2)
                z = (t[stat] - r[stat]) / se if se > 0 else 0.0
                rows.append({"template": tid, "quantity": q, "stat": stat, "ref": r6(r[stat]), "tab": r6(t[stat]),
                             "se": r6(se), "z": r6(z), "p": math.erfc(abs(z) / math.sqrt(2))})
    m = len(rows)
    order = sorted(range(m), key=lambda i: rows[i]["p"])
    run = 0.0
    for k, i in enumerate(order):
        run = max(run, min(1.0, (m - k) * rows[i]["p"]))
        rows[i]["p_holm"] = run
    records = []
    for tid in POSITION:
        ref, tab = runs[tid]
        mine = [r for r in rows if r["template"] == tid]
        worst = min(mine, key=lambda r: r["p"])
        header = {"template": tid, "template_version": versions[tid], "cmdstan": CMDSTAN, "image_id": a.image_id,
                  "chains": ref["chains"], "draws": ref["draws"], "warmup": ref["warmup"],
                  "seed_reference": ref["seed"], "seed_tabularia": tab["seed"],
                  "script_sha256": {"make_reference.py": scripts["make_reference.py"],
                                    "run_reference.py": scripts["run_reference.py"]},
                  "program_sha256": ref["program_sha256"], "data_sha256": ref["data_sha256"],
                  "mcse": "batch means of %d draws" % ref["batch"]}
        with open(os.path.join(a.out, tid + ".expected.json"), "w") as f:
            json.dump({"header": header, "sampler": {k: ref[k] for k in ("divergences", "treedepth_at_10")},
                       "quantities": {q: {k: r6(v) for k, v in d.items() if v == v} for q, d in sorted(ref["quantities"].items())}},
                      f, indent=1)
            f.write("\n")
        records.append({"template": tid, "template_version": versions[tid], "compared": len(mine),
                        "quantities": len({r["quantity"] for r in mine}),
                        "min_p": worst["p"], "min_p_quantity": worst["quantity"] + " " + worst["stat"],
                        "min_p_holm": min(r["p_holm"] for r in mine),
                        "tabularia_program_sha256": tab["program_sha256"], "tabularia_data_sha256": tab["data_sha256"],
                        "divergences": {"reference": ref["divergences"], "tabularia": tab["divergences"]},
                        "max_rhat": {"reference": r6(max(v["rhat"] for v in ref["quantities"].values() if v["rhat"] == v["rhat"])),
                                     "tabularia": r6(max(v["rhat"] for v in tab["quantities"].values() if v["rhat"] == v["rhat"]))},
                        "seconds": {"reference": {"compile": ref["compile_seconds"], "sample": ref["sample_seconds"]},
                                    "tabularia": {"compile": tab["compile_seconds"], "sample": tab["sample_seconds"]}},
                        "passed": all(r["p_holm"] >= ALPHA for r in mine),
                        "rows": [{k: v for k, v in r.items() if k != "template"} for r in mine]})
    out = {"header": {"alpha": ALPHA, "correction": "holm", "comparisons": m, "cmdstan": CMDSTAN,
                      "image_id": a.image_id, "script_sha256": scripts},
           "records": records}
    with open(os.path.join(a.out, "compare.json"), "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print("comparisons", m)
    for r in records:
        print("%-16s v%d n=%-4d min_p=%.3g holm=%.3g %s  ref %s tab %s  %s" % (
            r["template"], r["template_version"], r["compared"], r["min_p"], r["min_p_holm"], r["min_p_quantity"],
            r["seconds"]["reference"], r["seconds"]["tabularia"], "PASS" if r["passed"] else "FAIL"))


if __name__ == "__main__":
    main()
