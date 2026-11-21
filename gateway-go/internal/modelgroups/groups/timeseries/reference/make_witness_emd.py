"""The witnesses of the decomposition's EMD (docs/specs/ts-decomposition.md, «Self-test, reference values, witnesses»;
lot D3), computed outside R and outside Tabularia from the tables of make_reference_emd.R, into data/emd_witness.json;
the group's self-test (decomposition_emd_tests.R) compares the runner with it inside the image. Run by hand with the
versions of witness.json:

    uv venv /tmp/emdw && uv pip install --python /tmp/emdw/bin/python numpy==2.5.5 scipy==1.18.1 pandas==3.1.1 \\
        EMD-signal==1.10.0
    /tmp/emdw/bin/python -I gateway-go/internal/modelgroups/groups/timeseries/reference/make_witness_emd.py [--expected]

1. A transcription of libeemd 1.4.4's EMD (Luukko, Helske & Räsänen 2016; the C of Rlibeemd 1.4.4, read for lot D3:
   extrema.c, spline.c, emd.c), written again here in Python, with SciPy's not-a-knot CubicSpline in place of
   libeemd's own tridiagonal solver (the same spline, another code):

     extrema    the points where the slope changes sign, a flat run's centre for a flat extremum; both ends are a
                maximum and a minimum, and with two inner extrema of a kind the end takes the line through the two
                nearest ones when that is more extreme (Wu & Huang 2009); «good» when every inner maximum is > 0 and
                every inner minimum < 0
     envelopes  the cubic spline with not-a-knot ends through the maxima and through the minima, evaluated at every
                instant (2 points: a line; 3: a parabola)
     sifting    h ← h − (upper + lower) / 2 until, for S_number siftings in a row, the numbers of maxima and minima
                changed by at most 1 in total and the extrema are good — or num_siftings siftings
     EMD        M − 1 IMFs sifted one after the other from the residual, the residual last; libeemd never stops early

   then the runner's cut (decomposition_emd.R, written again here): the first k ≥ 0 whose residual x − c₁ − … − c_k
   has at most one extremum (Huang et al. 1998; Huang & Wu 2008), the later columns added to the residue.
   The kept IMFs and the residue are written at 33 instants and as sums; the self-test compares them at 1e-10 of the
   series' largest value. Measured on these tables (every instant, every column): the kept IMFs differ from
   Rlibeemd's by at most 1.3e-14, the residue by 4.5e-15; the columns past the cut, which libeemd sifts out of a
   residual with at most one extremum and the runner adds back into the residue, differ by up to 4.5e-5 (emd_short):
   sifting a curve without oscillation amplifies rounding, which is why they are not IMFs and are not compared.

2. The measures of the IMFs (mean period n / maxima, variance and energy shares, extrema, zero crossings) and the
   orthogonality index IO = Σ_t Σ_{j≠k} c_j c_k / Σ_t x² by numpy, on the transcription's modes.

3. PyEMD 1.10.0 (EMD-signal; natural cubic splines, mirrored ends, its own stopping rules: another algorithm) on
   emd_tones: the mean periods of its IMFs, which the self-test compares with the runner's for the modes near 8 and 64
   within 5 %.

4. With --expected, the reference cases of the EMD (ids «emd_…») in expected.json, from these numbers: each case's
   previous version is replaced, every other case is left as it is, byte for byte (the file is the JSON Python writes
   with an indent of 2, which is jsonlite's pretty form).

Exit 1 when a table is missing; nothing is compared here (the self-test compares).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SAMPLES = 33

# the cases: table, value column, max_imf (the runner's option), S-number, siftings
CASES = {
    "tones": ("emd_tones.csv", "signal", 10, 4, 50),
    "short": ("emd_short.csv", "visits", 10, 4, 50),
    "cut": ("emd_cut.csv", "signal", 3, 4, 50),
    "tones_fixed": ("emd_tones.csv", "signal", 10, 0, 10),
}


# ── libeemd, transcribed ──────────────────────────────────────────────────────
def emd_num_imfs(n: int) -> int:
    return 0 if n == 0 else 1 if n <= 3 else int(math.log2(n))


def find_extrema(x: np.ndarray) -> tuple[list[float], list[float], list[float], list[float], bool]:
    n = len(x)
    maxx, maxy, minx, miny = [0.0], [float(x[0])], [0.0], [float(x[0])]
    good = True
    previous = None
    flat = 0
    for i in range(n - 1):
        if x[i + 1] > x[i]:
            if previous == "down":
                minx.append(i - flat / 2)
                miny.append(float(x[i]))
                if x[i] >= 0:
                    good = False
            previous, flat = "up", 0
        elif x[i + 1] < x[i]:
            if previous == "up":
                maxx.append(i - flat / 2)
                maxy.append(float(x[i]))
                if x[i] <= 0:
                    good = False
            previous, flat = "down", 0
        else:
            flat += 1
    maxx.append(float(n - 1))
    maxy.append(float(x[n - 1]))
    minx.append(float(n - 1))
    miny.append(float(x[n - 1]))

    def line(x0: float, y0: float, x1: float, y1: float, at: float) -> float:
        return y0 + (y1 - y0) * (at - x0) / (x1 - x0)

    if len(maxx) >= 4:
        maxy[0] = max(maxy[0], line(maxx[1], maxy[1], maxx[2], maxy[2], 0.0))
        maxy[-1] = max(maxy[-1], line(maxx[-3], maxy[-3], maxx[-2], maxy[-2], float(n - 1)))
    if len(minx) >= 4:
        miny[0] = min(miny[0], line(minx[1], miny[1], minx[2], miny[2], 0.0))
        miny[-1] = min(miny[-1], line(minx[-3], miny[-3], minx[-2], miny[-2], float(n - 1)))
    return maxx, maxy, minx, miny, good


def envelope(x: list[float], y: list[float], n: int) -> np.ndarray:
    return CubicSpline(np.asarray(x), np.asarray(y), bc_type="not-a-knot")(np.arange(n, dtype=float))


def sift(h: np.ndarray, s_number: int, num_siftings: int) -> np.ndarray:
    h = h.copy()
    n = len(h)
    counter = s_counter = 0
    num_max = num_min = -1
    while num_siftings == 0 or counter < num_siftings:
        counter += 1
        prev_max, prev_min = num_max, num_min
        maxx, maxy, minx, miny, good = find_extrema(h)
        num_max, num_min = len(maxx), len(minx)
        if s_number != 0:
            if abs(num_min - prev_min) + abs(num_max - prev_max) <= 1:
                s_counter += 1
                if s_counter >= s_number and good:
                    break
            else:
                s_counter = 0
        h = h - 0.5 * (envelope(maxx, maxy, n) + envelope(minx, miny, n))
    return h


def libeemd_emd(x: np.ndarray, m: int, s_number: int, num_siftings: int) -> np.ndarray:
    """The n × m matrix of libeemd's emd(x, num_imfs = m): m − 1 IMFs and the residual."""
    res = x.copy()
    out = np.zeros((len(x), m))
    for i in range(m - 1):
        imf = sift(res, s_number, num_siftings)
        res = res - imf
        out[:, i] = imf
    out[:, m - 1] = res
    return out


# ── the runner's cut and measures, written again ─────────────────────────────
def extrema(x: np.ndarray) -> int:
    d = np.sign(np.diff(x))
    d = d[d != 0]
    return int(np.sum(d[1:] != d[:-1]))


def maxima(x: np.ndarray) -> int:
    d = np.sign(np.diff(x))
    d = d[d != 0]
    return int(np.sum((d[:-1] > 0) & (d[1:] < 0)))


def zero_crossings(x: np.ndarray) -> int:
    s = np.sign(x)
    s = s[s != 0]
    return int(np.sum(s[1:] != s[:-1]))


def cut(m: np.ndarray, x: np.ndarray) -> tuple[int, np.ndarray]:
    res = x.copy()
    for k in range(m.shape[1]):
        if extrema(res) <= 1 or k == m.shape[1] - 1:
            return k, (res if k < m.shape[1] - 1 else m[:, -1])
        res = res - m[:, k]
    raise AssertionError


def measures(c: np.ndarray, x: np.ndarray, residue: bool) -> dict:
    mx = maxima(c)
    return {"mean_period": None if residue or mx == 0 else len(c) / mx,
            "variance_share": float(np.var(c, ddof=1) / np.var(x, ddof=1)),
            "energy_share": float(np.sum(c * c) / np.sum(x * x)),
            "extrema": extrema(c), "zero_crossings": zero_crossings(c)}


def samples(n: int) -> list[int]:
    return sorted(set(int(round(v)) for v in np.linspace(0, n - 1, SAMPLES)))


def case(name: str) -> dict:
    table, col, max_imf, s_number, num_siftings = CASES[name]
    x = pd.read_csv(DATA / table)[col].to_numpy(float)
    n = len(x)
    m_cols = min(max_imf, emd_num_imfs(n) - 1) + 1
    m = libeemd_emd(x, m_cols, s_number, num_siftings)
    k, residue = cut(m, x)
    modes = np.column_stack([m[:, :k], residue]) if k else residue[:, None]
    gram = modes.T @ modes
    at = samples(n)
    out = {"table": table, "column": col, "max_imf": max_imf, "s_number": s_number, "max_siftings": num_siftings,
           "columns": m_cols, "at": [i + 1 for i in at],
           "modes": [{"values": [float(v) for v in modes[at, j]], "sum": float(np.sum(modes[:, j])),
                      "sum_squares": float(np.sum(modes[:, j] ** 2))} for j in range(k + 1)],
           "n_imfs": k, "residue_extrema": extrema(residue),
           "imfs": [measures(m[:, j], x, False) for j in range(k)], "residue": measures(residue, x, True),
           "orthogonality": float((gram.sum() - np.trace(gram)) / np.sum(x * x)),
           "identity": float(np.max(np.abs(modes.sum(axis=1) - x)))}
    if k >= 2:  # noise_imfs = 1: R = c₁, T = r, S = c₂ + … + c_k (Wang, Smith & Hyndman 2006)
        r, s = m[:, 0], m[:, 1:k].sum(axis=1)
        out["strengths_noise_1"] = {"trend": max(0.0, 1 - np.var(r, ddof=1) / np.var(residue + r, ddof=1)),
                                    "seasonal": max(0.0, 1 - np.var(r, ddof=1) / np.var(s + r, ddof=1))}
    return out


def pyemd_periods(table: str, col: str) -> list[float]:
    from PyEMD import EMD

    x = pd.read_csv(DATA / table)[col].to_numpy(float)
    emd = EMD()
    emd.emd(x)
    imfs, _ = emd.get_imfs_and_residue()
    return [len(x) / maxima(c) for c in imfs if maxima(c) > 0]


# ── the reference cases ───────────────────────────────────────────────────────
def said(v: float | int) -> str:
    """A number of a reading's line as the runner writes it: the shortest text that reads back as the same double."""
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


def expected_case(case_id: str, w: dict, table: str, types: dict, params: dict, noise: int = 0) -> dict:
    exp: list[dict] = [{"key": "model|||variant", "text": "emd"}, {"key": "model|||n_imfs", "value": w["n_imfs"]}]

    def num(key: str, v) -> None:  # the self-test's tolerance: 1e-9 relative, 1e-12 absolute
        exp.append({"key": key, "value": v})

    # every measure is compared with numpy's by the self-test (decomposition_emd_tests.R); here what a run writes
    for j, m in enumerate(w["imfs"], start=1):
        for stat in ("mean_period", "extrema", "zero_crossings") + (("variance_share", "energy_share") if j == 1 else ()):
            num(f"imf|imf_{j}||{stat}", m[stat])
        exp.append({"key": f"assumption|imf_condition|imf_{j}|verdict",
                    "text": "ok" if abs(m["extrema"] - m["zero_crossings"]) <= 1 else "failed"})
    for stat in ("mean_period", "variance_share", "extrema"):
        num(f"imf|residue||{stat}", w["residue"][stat])
    num("assumption|orthogonality||value", w["orthogonality"])
    cut = w["residue_extrema"] > 1
    exp.append({"key": "assumption|residue_monotonic||verdict", "text": "failed" if cut else "ok"})
    num("assumption|residue_monotonic||count", w["residue_extrema"])
    if noise == 0:
        exp.append({"key": "assumption|ljung_box||verdict", "text": "not_computed"})
        exp.append({"key": "assumption|ljung_box||advice_id", "text": "emd_no_noise"})
    else:
        num("assumption|trend_strength||value", w["strengths_noise_1"]["trend"])
        num(f"assumption|seasonal_strength|{params['period']}|value", w["strengths_noise_1"]["seasonal"])
    max_imf = params.get("max_imf", 10)
    lines = [f"@imfs_found n_imfs={w['n_imfs']} max_imf={max_imf}"]
    if cut:
        lines.append(f"@imfs_cut max_imf={max_imf} extrema={w['residue_extrema']}")
    elif w["n_imfs"] < max_imf:
        lines.append(f"@imfs_empty first={w['n_imfs'] + 1} last={max_imf}")
    lines += [f"@imf_mode imf={j} mean_period={said(m['mean_period'])}" for j, m in enumerate(w["imfs"], start=1)
              if m["mean_period"] is not None]
    for i, line in enumerate(lines, start=1):
        exp.append({"key": f"reading|imfs||line_{i}", "text": line})
    exp.append({"key": "reading|summary||line_1", "text": f"@summary_emd value={params['value']} n={_n(table)}"})
    return {"id": case_id, "model": "decomposition", "data": f"data/{table}", "types": types,
            "params": {"method": "emd", **params}, "expect": exp, "refuses": None}


def _n(table: str) -> int:
    return len(pd.read_csv(DATA / table))


def write_expected(w: dict) -> None:
    index = {"t": "integer", "signal": "number"}
    tones = {"time": "t", "value": "signal", "frequency": "index", "period": 8}
    short = {"time": "day", "value": "visits", "frequency": "day"}
    cases = [
        expected_case("emd_tones", w["tones"], "emd_tones.csv", index, tones),
        expected_case("emd_tones_noise_imfs", w["tones"], "emd_tones.csv", index, {**tones, "noise_imfs": 1}, noise=1),
        expected_case("emd_tones_fixed_siftings", w["tones_fixed"], "emd_tones.csv", index,
                      {**tones, "s_number": 0, "max_siftings": 10}),
        expected_case("emd_short", w["short"], "emd_short.csv", {"day": "date", "visits": "number"}, short),
        expected_case("emd_cut", w["cut"], "emd_cut.csv", index,
                      {"time": "t", "value": "signal", "frequency": "index", "max_imf": 3}),
        {"id": "emd_noise_imfs_above", "model": "decomposition", "data": "data/emd_short.csv",
         "types": {"day": "date", "visits": "number"}, "params": {"method": "emd", **short, "noise_imfs": 5}, "expect": [],
         "refuses": {"code": "noise_imfs_above", "message": f"EMD found {w['short']['n_imfs']} IMFs; 5 cannot be noise: "
                                                             f"choose at most {w['short']['n_imfs']}"}},
    ]
    path = HERE / "expected.json"
    ref = json.loads(path.read_text(encoding="utf-8"))
    ref["cases"] = [c for c in ref["cases"] if not str(c.get("id", "")).startswith("emd_")] + cases
    path.write_text(json.dumps(ref, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(cases)} EMD cases written to expected.json")


def main() -> int:
    for table, *_ in CASES.values():
        if not (DATA / table).exists():
            print("run make_reference_emd.R first", file=sys.stderr)
            return 1
    import importlib.metadata as md
    import scipy

    out = {"generated": {"by": "make_witness_emd.py", "numpy": np.__version__, "scipy": scipy.__version__,
                         "pandas": pd.__version__, "EMD-signal": md.version("EMD-signal")},
           "cases": {name: case(name) for name in CASES},
           "pyemd": {"tones": pyemd_periods("emd_tones.csv", "signal")}}
    (DATA / "emd_witness.json").write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
    print("written data/emd_witness.json")
    if "--expected" in sys.argv[1:]:
        write_expected(out["cases"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
