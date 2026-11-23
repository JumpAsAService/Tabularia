"""The draws of the diagnostics' witness cases (docs/specs/bayes-diagnostics.md, «Witnesses», lot X0), written once into
cases.json with a fixed seed and STORED AS VALUES, so that no random generator has to agree across languages: make_witness.R
(the R package posterior) and make_witness.py (ArviZ) read the same numbers the runner's bayes_diag.py reads in test_diag.py.

Every value is rounded to 8 significant digits, as CmdStan 2.40.0 writes its CSV (the contract's «Decisions of 2026-11-24
(K0)»), so the cases carry the ties the real draws carry. A case's `draws` are a list of chains, each a list of draws.

Run it by hand from the repository root, with the recipe's numpy (its output is committed; running it again writes the same
file byte for byte):

    uv run --no-project --with numpy==2.5.5 \\
      python -I gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics/make_cases.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SEED = 20261124
DIGITS = 8


def rounded(x: np.ndarray) -> list[list[float]]:
    """The draws as CmdStan writes them: %.8g, read back as doubles."""
    return [[float(f"{v:.{DIGITS}g}") for v in chain] for chain in np.atleast_2d(x)]


def ar1(rng: np.random.Generator, chains: int, draws: int, rho: float) -> np.ndarray:
    """Stationary AR(1) chains of unit marginal variance."""
    x = np.empty((chains, draws))
    x[:, 0] = rng.normal(size=chains)
    noise = rng.normal(size=(chains, draws)) * np.sqrt(1.0 - rho * rho)
    for t in range(1, draws):
        x[:, t] = rho * x[:, t - 1] + noise[:, t]
    return x


def make() -> dict:
    rng = np.random.default_rng(SEED)
    cases = []

    def case(name: str, pins: str, x: np.ndarray) -> None:
        cases.append({"name": name, "pins": pins, "draws": rounded(x)})

    case("iid", "i.i.d. normal draws, 4 x 1,000: R-hat near 1, ESS near S", rng.normal(size=(4, 1000)))
    case("ar1_09", "AR(1) with rho = 0.9, 4 x 1,000: the autocorrelation sum and its truncation", 2.0 + 3.0 * ar1(rng, 4, 1000, 0.9))
    shifted = rng.normal(size=(4, 1000))
    shifted[3] += 1.0
    case("shifted", "one chain of four shifted by one sd: R-hat of the location (bulk)", shifted)
    scaled = rng.normal(size=(4, 1000))
    scaled[3] *= 4.0
    case("scaled", "one chain of four with four times the scale: the folded R-hat (tail) that the bulk misses", scaled)
    case("cauchy", "standard Cauchy draws, 4 x 1,000: the rank normalization where the moments do not exist",
         rng.standard_cauchy(size=(4, 1000)))
    case("ties_two_values", "1,000.12345 + 0.00001 z at 8 significant digits: two values almost only (average ranks)",
         1000.12345 + 1e-5 * rng.normal(size=(4, 1000)))
    case("ties_coarse", "1,000 + 0.001 z, AR(1) rho = 0.3, at 8 significant digits: 288 distinct values, a step of 1e-5 below 1,000 "
         "and 1e-4 above (average ranks)",
         1000.0 + 1e-3 * ar1(rng, 4, 1000, 0.3))
    case("constant", "every draw 3.25, 4 x 100: the NULLs", np.full((4, 100), 3.25))
    case("odd", "an odd number of draws, 4 x 999, AR(1) rho = 0.3: the split's middle draw", ar1(rng, 4, 999, 0.3))
    case("one_chain", "one chain of 1,000, AR(1) rho = 0.5: the split of a single chain", ar1(rng, 1, 1000, 0.5))
    case("antithetic", "AR(1) with rho = -0.6, 4 x 1,000: the improved tau estimate of an antithetic chain", ar1(rng, 4, 1000, -0.6))
    skew = rng.gamma(0.5, size=(4, 1000))
    skew[:, ::50] = 0.0
    case("skewed_atom", "gamma(0.5) draws with 2 % of them exactly 0: a skewed quantity with an atom at its minimum", skew)
    top = rng.beta(5.0, 1.0, size=(4, 1000))
    top[rng.random((4, 1000)) < 0.08] = 1.0
    case("atom_top", "beta(5, 1) draws with 8 % of them exactly 1: an atom above the 95 % quantile (the upper tail's indicator)", top)
    case("short", "2 x 7 draws, split chains of 3: the shortest autocorrelation sum (no pair after the first)",
         rng.normal(size=(2, 7)))
    case("too_short", "4 x 5 draws, split chains of 2: fewer than 3 draws a split chain (the contract's NULLs)",
         rng.normal(size=(4, 5)))

    # the sampler's fields of a fake run: divergences, the tree depth (max_treedepth 10) and an energy whose successive
    # differences are smaller than its spread on one chain (an E-BFMI well below 1) and i.i.d. on the others
    chains, draws = 4, 1000
    energy = 50.0 + 5.0 * rng.normal(size=(chains, draws))
    energy[2] = 50.0 + 5.0 * ar1(rng, 1, draws, 0.95)[0]
    divergent = (rng.random((chains, draws)) < np.array([[0.0], [0.002], [0.03], [0.0]])).astype(float)
    treedepth = np.minimum(rng.poisson(3.0, size=(chains, draws)) + 1, 10).astype(float)
    treedepth[1, ::97] = 10.0
    facts = {"name": "sampler", "pins": "divergent__, treedepth__ and energy__ of a fake run of 4 x 1,000: the facts and E-BFMI",
             "max_treedepth": 10, "divergent__": rounded(divergent), "treedepth__": rounded(treedepth),
             "energy__": rounded(energy)}
    return {"about": "The draws of the diagnostics' witness cases (docs/specs/bayes-diagnostics.md, «Witnesses»), written by "
                     "make_cases.py; every value at 8 significant digits, a case's draws a list of chains.",
            "seed": SEED, "digits": DIGITS, "cases": cases, "facts": facts}


def dump(doc: dict) -> str:
    """JSON with one chain a line (a diff names the chain that changed)."""
    def chains(xs: list[list[float]]) -> str:
        return "[\n" + ",\n".join("      " + json.dumps(c, separators=(",", ":")) for c in xs) + "\n    ]"

    out = ["{", f'  "about": {json.dumps(doc["about"], ensure_ascii=False)},', f'  "seed": {doc["seed"]},',
           f'  "digits": {doc["digits"]},', '  "cases": [']
    for i, c in enumerate(doc["cases"]):
        out.append(f'    {{"name": {json.dumps(c["name"])}, "pins": {json.dumps(c["pins"], ensure_ascii=False)}, "draws": '
                   + chains(c["draws"]) + ("}," if i + 1 < len(doc["cases"]) else "}"))
    out.append("  ],")
    f = doc["facts"]
    out.append(f'  "facts": {{"name": {json.dumps(f["name"])}, "pins": {json.dumps(f["pins"], ensure_ascii=False)}, '
               f'"max_treedepth": {f["max_treedepth"]},')
    fields = ["divergent__", "treedepth__", "energy__"]
    for i, k in enumerate(fields):
        out.append(f'    "{k}": ' + chains(f[k]) + ("," if i + 1 < len(fields) else "}"))
    out.append("}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    text = dump(make())
    json.loads(text)  # well formed
    (HERE / "cases.json").write_text(text, encoding="utf-8")
    print(f"cases.json: {len(text.encode('utf-8')):,} bytes")
