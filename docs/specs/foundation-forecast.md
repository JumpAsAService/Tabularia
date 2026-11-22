# Spec: forecasting with a pre-trained model — the group `foundation_forecast` (Timestral)

Status: **approved by the owner on 2026-11-21. This file is not committed.** Nothing is built. The
same day the owner answered «Open questions for the owner» (each marked «Decided 2026-11-21»): every
recommendation approved, with **one change — no migration**: there is no table
`model_group_licences`; the acceptance is the audit entry `models.licence_accept`, and each version of
the group records, in a field it already has, the acceptance its build relied on («The licence:
enabling the group», «Where the acceptance is kept, without a migration»). The owner's request of
2026-11-21: add forecasts of time series with **Timestral** by
Mistral AI (pre-trained zero-shot models of 5M, 28M, 123M and 209M parameters, a context of 32,768
steps, past-only and known-future covariates, 21 quantiles, inference on CPU), with a note on the
licence of the weights where the administrator enables the group: «il docker di base non è buildato,
quindi niente di male, ma se l'azienda che lo utilizza tramite tabularia è grande, serve avvertire
sulla licenza».

It adds one **group** and one **node** to `docs/specs/statistical-models.md` (**the models spec**),
which it reuses and does not repeat: a group's recipe, its image built in the cluster when an
administrator enables it, the check Job and the self-test (exit 3 a refusal, 4 a failed self-test),
`RunnerHash` and the recheck of a changed runner, the Job with its Parquet input and its ConfigMap
(900 KiB, `groups_test.go`), the ten columns, the layouts and `report_texts`, Run in the editor and
«not up to date», MLflow logged by the Go gateway. It is the second **Python** group after
`boosting` («Group `boosting` — Python» and «As built, the boosting group»: `common/py`, `python -I
-B`, `--no-deps --only-binary=:all:` and `pip check`). It follows the newest specs,
`docs/specs/ab-testing.md` (**the A/B spec**) and `docs/specs/ts-decomposition.md` (**the
decomposition spec**), wherever they set a rule the owner wants everywhere: verdicts only from a
test with a p-value against the node's `alpha`, a light runtime, the input's types kept (`=<key>`),
a card-less model (`card: false`), columns known before the Run, the explanation of the method in
the report («Explaining the method in the report»), and the series rules of `ts_series.R` (lot D1).

Words: **verified** = read in this repository's code on 2026-11-21; **checked online** = read on
2026-11-21 in a primary source named in «The primary sources» (Mistral AI's GitHub repository at
commit `a534bdbd244d`, the Hugging Face repositories of the weights at the revisions named below,
the announcement, the documentation page, the paper, the PyPI and PyTorch package indexes);
**assumed** = not run. **Nothing was measured for this spec** (lots D and E are running on this
machine, one heavy job at a time): every time and size below is an estimate that lot F0 replaces
with a measurement on 1,000, 10,000 and 100,000 points and on many series.

This is not legal advice. The licence's words are quoted exactly; what they mean for a given
company is that company's to decide, and «Open questions for the owner» says where a lawyer's
reading would help.

## Objective

A person who prepares data wants the next weeks of a series — orders per day per channel,
kilowatt-hours per hour per site, boxes per month per product — with a range, not only a number,
and without fitting a model per series. They drop **Forecast (Timestral)** after a step that holds
one row per period (and per series), choose the time, the value, the series column when there are
many, the frequency, the horizon and the model's size, press **Run**, and get:

- the **forecast** as the next step's input: one row per instant and series, the median and the
  quantiles the node asked for, the observed values before it, flags that say which rows are history,
  holdout or forecast — with every column known before the Run;
- a **report** whose first page says how far ahead, with which model, how the forecast did on the
  last weeks kept out (the holdout) against the simplest honest forecast (the seasonal naïve), how
  often the real values fell inside the bands, and — in words for a reader who is not a statistician —
  what a pre-trained model is, what a quantile is, and what such a model cannot know about the
  business;
- the **licence** of the weights said where it matters: when the administrator enables the group
  (an explicit acceptance, recorded), in the group's card, in the documentation, in the report.

No model is fitted: Timestral is used as it is («zero-shot»), so there is nothing to register in
MLflow and no Predict («MLflow, and no Predict»).

## Decisions

| Question | Decision | By |
|---|---|---|
| Where it runs | **Kubernetes only**, a group of its own with an image separate from Tabularia's and from the other groups', built in the cluster when an administrator enables it; on Compose the node is greyed out, «requires Kubernetes» | Owner (the models' rule, 2026-11-19) |
| Language | **Python** (the inference library is PyTorch) | Owner's rule («R for classical models, Python for the more complex ones») |
| The weights | **Never in Tabularia's images nor in the repository.** The group's build, in the customer's cluster, downloads them from Mistral AI's Hugging Face repositories at pinned revisions, checked by sha256, into the group's image | Owner (2026-11-21: «il docker di base non è buildato») |
| The licence note | Where the administrator enables the group: the clause quoted, a link to the official text, an explicit required checkbox with the recommended words (no question on revenue), who accepted and when in the **audit log** (`models.licence_accept`) and, for each version built, in that version's own record; **no migration**; the same note in the documentation, in the group's card and in the report | Owner (2026-11-21, Open questions 1, 2, 16) |
| Sizes | The administrator ticks the sizes to download, all four by default; the node's default is **28M** | Owner (2026-11-21, Open questions 3, 4) |
| A lawyer's reading | Recommended before the first release that ships the group, of the panel's sentences and the documentation's paragraph («Boundaries») | Owner (2026-11-21, Open question 18) |
| Verdicts | Only from a test with a p-value against the node's `alpha`: one test, the comparison with the seasonal naïve **across series** (Wilcoxon's signed-rank). Every error measure on the holdout — MASE, sMAPE, the pinball loss, the coverage of the bands — is a **measure without verdict** | Owner's rule (2026-11-20) |
| Runtime | Light: no simulation, no bootstrap, no sampling of forecasts (Timestral's quantiles are direct outputs). Timings on 1,000, 10,000 and 100,000 points and on many series in every lot; the model's loading time counted | Owner's rule (2026-11-20) |
| Explanation | One to three sentences with the run's numbers under each block, a last page «How this report was computed», `@id name=value` rows said by `structured_note` in five languages, no AI at run time — the A/B's mechanism | Owner (2026-11-21) |
| Columns before the Run | Every output column known from the catalogue and the options: `quantile_<level>` from `quantiles`, `series` only with a series column, `time` of the input's exact type (`=time`), the series column of its exact type (`=series`) | Owner's rule |
| Gaps | The decomposition's rule: **refused** by default, `linear` interpolation as an option, every filled instant flagged; plus `model` (the gap handed to Timestral as a missing value, which it is built to read) | Owner (2026-11-21: «riusa la regola della decomposizione»); `model` is this spec's addition (Open question 6) |
| Model card, Predict | **None**: `card: false`, no registration, no Predict node entry. MLflow logs the run | Spec («MLflow, and no Predict») |

## The primary sources, checked on 2026-11-21

| Source | What it gave |
|---|---|
| Hugging Face, `mistralai/Timestral-5M`, `-28M`, `-123M`, `-209M`: `LICENSE`, `README.md` (the model card), `config.json`, and the Hub's API (`/api/models/…?blobs=true`, `/commits/main`) | The licence's text (identical in the four: sha256 `648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c`, 1,695 bytes), the revisions, the files, their sizes and sha256, the card's facts (horizon, quantiles, inputs) |
| GitHub, `mistralai/timestral`, commit `a534bdbd244d` («Timestral release», 2026-10-15, the only commit; no tag): `README.md`, `LICENSE`, `pyproject.toml`, `uv.lock`, `src/timestral/*.py`, `tests/test_forecaster.py` | The code's licence, the package, its dependencies, the API, the input and output shapes, the covariates, loading, what is random and what is not |
| `https://mistral.ai/news/timestral/` | The four sizes, CPU inference, 21 quantiles, 32,768 steps, «Modified MIT» |
| `https://docs.mistral.ai/models/timestral-1-0-123m.md` | «Timestral 1.0 123M», version 1.0, status GA, licence «Modified MIT», the weights' address |
| arXiv `2610.37585` (v1, 27 October 2026, «Timestral», Petralia et al.), the PDF | The architecture, the sizes, §4.3 «Inference Speed» (Table 2, Figure 9), the training data |
| PyPI JSON API; `https://download.pytorch.org/whl/cpu/torch/` | That `timestral` is **not** on PyPI (404); the versions, licences and dependencies of every package of the recipe; the CPU-only PyTorch wheels and their sha256 |

Secondary sources (news sites) were not used for any fact below. One of them reported the revenue
threshold correctly; the primary text is quoted here instead.

### The licence of the weights, verbatim

The file `LICENSE` of each of the four repositories (identical bytes), in full:

> Modified MIT License
>
> Attribution notice: 2026 - Mistral AI
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of the weights of
> this model and associated documentation files (the “Model”), to deal in the Model without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Model, and to permit persons to whom the Model is
> furnished to do so, subject to the following conditions:
>
> 1. The above attribution notice and this permission notice shall be included in all copies or
>    substantial portions of the Model.
> 2. You are not authorized to exercise any rights under this license if the global consolidated
>    monthly revenue of your company (or that of your employer) exceeds $20 million (or its equivalent
>    in another currency) for the preceding month. This restriction in (b) applies to the Model and
>    any derivatives, modifications, or combined works based on it, whether provided by Mistral AI or
>    by a third party. You may contact Mistral AI (sales@mistral.ai) to request a commercial license,
>    which Mistral AI may grant you at its sole discretion, or choose to use the Model on Mistral AI's
>    hosted services available at https://mistral.ai/.
>
> THE MODEL IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
> LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
> IN NO EVENT SHALL MISTRAL AI BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
> ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE MODEL OR THE
> USE OR OTHER DEALINGS IN THE MODEL.

The model cards add, under «License»: «Models have been released under a Modified MIT license» and
«*You must not use this model in a manner that infringes, misappropriates, or otherwise violates any
third party’s rights, including intellectual property rights*». The cards' metadata say `license:
other`. The GitHub README: «The Timestral model weights are not covered by the Apache License 2.0.
They are distributed separately on the Hugging Face Hub under a Modified MIT license».

**What the text says, and what it does not** (a plain reading, not legal advice):

- **The threshold** is **$20 million of global consolidated *monthly* revenue, for the preceding
  month** — about $240 million a year if the months are even. It is measured on «your company (or
  that of your employer)», so it reaches the company of whoever uses the model, consolidated
  (groups included). It is re-assessed every month: a company can cross it later.
- **Above it, no right at all** under this licence: not to use, not to copy. Running a forecast is a
  use. The way out the text names: a commercial licence from Mistral AI («at its sole discretion»),
  or Mistral AI's hosted services.
- **«Derivatives, modifications, or combined works … whether provided by Mistral AI or by a third
  party»**: the restriction follows the weights into whatever is built on them, including a product
  such as Tabularia that runs them. It does not create an obligation for a third party that does not
  hold the weights; it means that a large company does not escape the clause by getting the model
  through Tabularia.
- **«This restriction in (b)»** refers to a point (b) that the text numbers «2.»: a drafting slip in
  the official text. The spec quotes it as it is and does not interpret it.
- **Attribution** (point 1): every copy of the weights carries the notice. The group's image is such
  a copy: the build puts `LICENSE` beside each `model.safetensors` («The recipe»).
- **Outputs** (the forecasts) are not mentioned by the licence. The spec does not claim anything
  about them.
- **Why Tabularia itself does not hit the clause** (the owner's reading of 2026-11-21): Tabularia
  never ships the weights. Its images and its repository hold only the recipe (addresses, revisions,
  checksums) and the inference code (Apache 2.0, below). The weights are downloaded by the customer's
  own cluster, from Mistral AI's repository, when the customer's administrator enables the group —
  after reading the licence and accepting it in Tabularia's panel. Whether a given customer may use
  them is that customer's question; Tabularia's job is to make sure it is asked, answered by someone
  named, and recorded.

### The inference code: package, version, dependencies, licences

| Fact | Value | Source |
|---|---|---|
| Licence of the code | **Apache License 2.0** («The code in this repository (the `timestral` package and tests) is licensed under the Apache License 2.0»); `pyproject.toml`: `license = "Apache-2.0"`; GitHub's API: `apache-2.0` | GitHub, checked online |
| Package | `timestral`, version **1.0.0** (`pyproject.toml`, `__version__`), Python ≥ 3.10, build backend setuptools ≥ 77 | GitHub |
| On PyPI | **No** (`https://pypi.org/pypi/timestral/json` answers 404). The documented install is `pip install git+https://github.com/mistralai/timestral.git` | PyPI, GitHub |
| Releases | One commit, `a534bdbd244d…` (2026-10-15), no tag. «We do not accept pull requests» | GitHub |
| Dependencies (declared) | `huggingface_hub>=0.23`, `numpy>=1.24`, `safetensors>=0.4`, `torch>=2.4` | `pyproject.toml` |
| Dependencies (the lock) | `uv.lock`: torch **2.14.1**, huggingface-hub 2.2.0, safetensors 0.8.0, numpy 2.5.4 (Python ≥ 3.12) | `uv.lock` |
| Files | `__init__.py`, `config.py`, `covariate_encoding.py`, `forecaster.py`, `layers.py`, `loading.py`, `masks.py`, `model.py`, `scaler.py`: **79 KB** of Python | GitHub tree |
| Where `huggingface_hub` is used | only `loading.py`: `snapshot_download` (Hub repos) and three error classes, **imported at module level**; a local directory never touches the Hub («an existing directory never touches the Hub», the package's own test) | `loading.py`, `tests/test_forecaster.py` |

Licences of what the group's image installs (PyPI's `license_expression` or classifiers, checked
online):

| Package | Version | Licence |
|---|---|---|
| `torch` (the **CPU-only** build, `2.14.1+cpu`, from `download.pytorch.org/whl/cpu`) | 2.14.1 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT |
| `numpy` | 2.5.5 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `safetensors` | 0.8.0 | Apache-2.0 |
| `pyarrow` | 26.0.1 | Apache-2.0 |
| torch's closure: `filelock` 4.2.12, `typing-extensions` 4.16.0, `setuptools` 84.2.1, `sympy` 1.14.0, `mpmath` 1.3.0 (sympy needs < 1.4), `networkx` 3.7, `jinja2` 3.1.6, `markupsafe` 3.0.4, `fsspec` 2026.9.0 | | MIT, PSF-2.0, MIT, BSD, BSD-3-Clause, BSD-3-Clause, BSD, BSD-3-Clause, BSD-3-Clause |
| `timestral` (vendored, «The runner») | 1.0.0 at `a534bdbd` | Apache-2.0 |

Every licence is permissive. Apache 2.0 code can be combined into Tabularia's AGPL-3.0 (one way:
Apache 2.0 is compatible with GPLv3 and AGPLv3); the vendored files keep Mistral AI's `LICENSE` and a
notice of the one change («The runner»), and the third-party notices of the gateway image list them
(`scripts/third-party-notices.mjs` covers the frontend today; lot F7 adds the gateway's embedded
vendor files). **The PyPI `torch` wheel for Linux is a CUDA build** that requires `cuda-toolkit`,
`nvidia-cudnn-cu13`, `nvidia-nccl-cu13`, `triton` and more (its `Requires-Dist`, checked online): the
recipe never takes it. The CPU wheel (`torch-2.14.1+cpu-cp314-cp314-manylinux_2_28_x86_64.whl`,
196,260,555 bytes; `…_aarch64.whl`, 159,263,447 bytes) requires only the nine packages of its closure
above.

### Sizes, and where the weights are

| Size | Hugging Face repository | Revision pinned (the `main` of 2026-11-21) | `model.safetensors`: bytes, sha256 | Layers × width (paper, Table 1) |
|---|---|---|---|---|
| 5M (5.2M) | `mistralai/Timestral-5M` | `bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f` | 20,843,960 · `7ace108c4987e7f2a99a75a445240019d1d31c2117139d1bd05ffdfd25aca738` | 8 × 192 |
| 28M (27.8M) | `mistralai/Timestral-28M` | `49b26409ac87b5935539a5d8ffe7f397b720db47` | 111,296,464 · `dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d` | 12 × 384 |
| 123M (122.6M) | `mistralai/Timestral-123M` | `5a147287addda85f6acd8da3fb8858fe9af7e8f7` | 490,445,904 · `599fafa73cadbbc800b5a346ca900fa741ee3e5fd22e3b4a4430e151e74f4bc2` | 20 × 640 |
| 209M (209.3M) | `mistralai/Timestral-209M` | `9874d252ab0aae16f7f7c999f98f98d6ad7d8365` | 837,271,648 · `1456effb9927e3daa8c7234f10497e3424233869245a9c72b8ecebaf7ad2b7fa` | 24 × 768 |

`config.json` sha256: 5M `b5f27f61…c2f`, 28M `0cdf44b8…8c`, 123M `7bb4c042…bf`, 209M `1764139b…d8` (the
recipe holds them in full). The repositories are public, not gated (`gated: false`). Each repository's
history is a squash and one to three README edits (28M's two of 2026-10-19 are README only). All four
`config.json` have the same quantile knots, `context_length` 32768, `patch_size` 32, `block_size` 32,
`covariate_enabled: true`, and a layer schedule with variate attention every fourth block, so
`supports_known_covariates` holds for every size (`config.py`, read). Weights are **float32**;
«Inference was validated in float32» (`loading.py`).

### The inference API (verbatim from `forecaster.py`)

```python
TimestralForecaster.from_pretrained(path_or_repo_id, device="cpu", max_context=None,
    per_shot_horizon_patches=32, quantile_real_cap_k=1e4, revision=None, cache_dir=None,
    token=None, local_files_only=False) -> TimestralForecaster

TimestralForecaster.forecast(context, prediction_length, observation_mask=None,
    past_covariates=None, known_covariates=None, categorical_past_covariates=None,
    categorical_known_covariates=None, symmetric_averaging=True, clip_nonnegative=True) -> Forecast
```

`from_pretrained` takes «a local directory or a Hugging Face Hub repo id» holding `config.json` and
`model.safetensors`; with a directory nothing is downloaded. `Forecast.quantiles` is a tensor `(B, V,
H, Q)`, «sorted along the quantile axis»; `Forecast.quantile_levels` the 21 levels; `.median` and
`.quantile(level)` (a level not among the 21 raises).

### Inputs and outputs

- **Context**: `(B, V, T)` (series, variates, time), `(B, T)` or `(T,)`, NumPy or torch; NaN marks a
  missing value; `observation_mask` marks more. Every series of a call has the same `T`: shorter
  series are padded **on the left** with NaN by the caller; the library pads to a multiple of 32 by
  itself, also on the left, as unobserved (`pad_to_patch`).
- **Context length**: «capped at the trained length (32768 steps); the most recent steps are kept».
- **Horizon**: «up to 1,024 time steps (32 patches) in a single forward pass. Longer horizons are
  forecast block by block, with each block's median forecast appended to the context» (model card).
- **Output**: 21 quantiles per step: **0.01, 0.05, 0.10, 0.15, …, 0.85, 0.90, 0.95, 0.99**
  (`quantile_knots`); «A quantile head outputs 21 non-crossing quantiles per time step». The
  symmetric central intervals are therefore 98 % (0.01–0.99), 90 % (0.05–0.95), 80 % (0.10–0.90), 70 %,
  … — **there is no 95 % interval** (no 0.025 or 0.975).
- **The defaults of `forecast`** (`symmetric_averaging = True`, `clip_nonnegative = True`, the
  module's docstring): the series and its negation are both forecast and averaged («two forward
  passes»), and a series whose observed context is all ≥ 0 gets forecasts clamped at 0. The
  quantiles are also clamped to the context's range ± 10⁴ times the last patch's scale
  (`quantile_real_cap_k`).
- **Degenerate input**: «forecast produced … NaN values; the input is degenerate» (`ValueError`).
- **The model reads no timestamps and no frequency** (paper, §3: «No timestamps, frequency
  information, or role or position embeddings are added»). The frequency is Tabularia's business: the
  grid, the future times, the baseline's season.

### Covariates

- **Past-only** `(B, C_past, T)`: «known over the context only». **Known-future** `(B, C_known, T +
  H)`: «known over context and horizon. Only these see the forecast horizon». «Covariates are
  conditioning rows: they are not forecast, and covariate rows never read the targets.» NaN marks a
  missing covariate value. «Known-future covariate rows whose values are all 0 or 1 are treated as
  indicators.»
- **Categorical** (`categorical_past_covariates`, `categorical_known_covariates`): numeric codes, NaN
  missing; for one target variate each row is **target-encoded per series** — category c becomes
  «(mean + sum_c) / (1 + count_c) over the forward-filled context», missing values a category of their
  own, a category absent from the context the series' mean.
- **What it cannot say**: no coefficient is estimated; the report cannot say «the holiday flag moved
  the forecast by x» without a second forecast without the covariate (Open question 10).

### Memory, speed, determinism (what the sources say)

- **Weights in memory**: 21, 111, 490, 837 MB (float32). `load_model` builds the model, then
  `load_state_dict(load_file(…))`: the weights exist twice for a moment (**assumed**: peak ≈ 2 × the
  checkpoint; F0 measures the resident peak).
- **Speed (paper, Table 2, verbatim setup)**: «forecasting 96 steps of a univariate series without
  covariates from a context of 2,048 steps, with symmetric averaging. CPU latency is the median of 50
  runs at batch size 1. CPU throughput is per core at batch size 16», measured «with bfloat16 weights
  and torch.compile» on «a 48-core server CPU (AMD EPYC 9454)» with PyTorch 2.14:

  | Size | 8 threads | 1 thread | series/s per core |
  |---|---|---|---|
  | 5M | 17 ms | 16 ms | 106 |
  | 28M | 31 ms | 56 ms | 24 |
  | 123M | 85 ms | 210 ms | 5.9 |
  | 209M | 113 ms | 345 ms | 3.5 |

  «Up to 2,048 steps, latency changes little: it roughly doubles from 128 to 2,048 steps. Beyond, it
  grows nearly in proportion to the context, by about 9 to 12 times from 2,048 to 32,768 steps … a
  forecast takes 119 ms with Timestral-5M and 1.3 s with Timestral-209M. A series with 32 variates
  takes 7 to 15 times as long as a univariate series. Covariates cost more than target variates: one
  target with seven known-future covariates takes 1.7 to 2.0 times as long as eight target variates.»
  **Tabularia will run float32 without `torch.compile`** (compiling needs a C compiler in the image and
  tens of seconds per Job, against the light-runtime rule): slower than the table by a factor F0
  measures (assumed 1.5–3×).
- **No GPU**: «Timestral does not require a GPU at inference.»
- **Determinism**: nothing in the package draws a random number (no sampling, `dropout_p=0.0`; the
  paper: «Neither dropout nor stochastic depth is used»); the only seeds are in the tests. The sources
  say nothing about determinism across thread counts, batch compositions or CPUs («Determinism» below).
- **Training data** (paper §4.1): «pretrained exclusively on synthetic data … then post-trained on a
  mixture of real and synthetic data». The real data are not listed in the paper's text read for this
  spec: the report says only that the model was trained before and elsewhere, never on the user's data.

### What the sources do not say

The memory needed at inference (only the checkpoint sizes), any determinism guarantee, a version
number of the weights beyond «1.0» (docs) and the Hub revisions, a PyPI release, any statement on the
forecasts' ownership. The technical report «coming soon» of the model cards is the arXiv paper above.

## What exists and is reused (verified)

- **A Python group** (`boosting`, «As built, the boosting group»): `common/py/` (`tab_protocol.py`,
  `tab_columns.py`, `tab_results.py`, `tab_card.py`, `tab_charts.py`, `tab_selftest.py`, `tab_main.py`),
  the command `python -I -B /tabularia/code/runner/run.py` (`modelgroups.Commands`), `PYTHONHASHSEED=0`,
  `OMP_NUM_THREADS` = the CPU limit (`threadsOf`, `jobs_models.go:58, 621`), the base `python:3.14-slim`
  (`"base": "3.14"`).
- **The recipe machinery** (`gateway-go/internal/modelgroups/groups.go`, `Recipe`: `language`, `base`,
  `pinned_on`, `packages`, `system_libraries`, `resources`, `max_rows`, `max_seconds`;
  `internal/scriptimage/scriptimage.go`, `Dockerfile`: a managed Python image is `FROM <base>`, `ENV`
  with `PIP_INDEX_URL` from `SCRIPTS__PYPI_INDEX_URL`, the requirements as a heredoc, `pip install
  --no-deps --only-binary=:all: -r … && pip check`, then user 10001). The builder is
  `moby/buildkit:v0.33.1-rootless` (`config_jobs.go:134`, `values.yaml:1002`).
- **The embedding rule**: `//go:embed card.schema.json common groups` leaves out every file whose name
  starts with `_` or `.` (`groups.go:17–19, 40`) — a Python package's `__init__.py` among them.
- **The ConfigMap's ceiling**: `groups_test.go:120` fails a group whose self-test ConfigMap passes
  900 KiB.
- **Enabling** (`internal/api/models_admin.go`): `POST /admin/models/{group_id}/enable` (administrators,
  not with an API token), the table `model_groups` (`enabled_by`, `enabled_at`…), `mgQueueIfNeeded`
  (a version is queued when no ready version has the shipped recipe hash), the audit actions
  `models.group_enable`, `_disable`, `_rebuild`, `_settings`, `_access`. The panel
  `frontend/components/admin/ModelsSection.vue` (Enable is one click today; Disable asks `confirm`).
  Audit entries older than `AUDIT__RETENTION_MINUTES` are deleted (`internal/jobs/retention`), when
  an installation sets one.
- **The schema of a group, as 0044 and 0045 left it** (read in `0044_models.py`, `0045_model_recheck.py`,
  `0015_scripting_environments.py`): `model_groups` holds `id`, `environment_id`, `enabled`, `everyone`,
  `max_rows`, `enabled_by`, `enabled_at`, `updated_by`, `updated_at` — **no JSON of settings, no free
  field**; `enabled_by`/`enabled_at` are overwritten at each enabling. `script_environment_versions`
  holds per version `packages` (text), `created_by`, `created_at`, `recipe_hash`, `runner_hash`,
  `selftest`, `lock`, `build_log`, `error`, and 0045's `recheck_*`. For a managed environment `packages`
  is written by Tabularia alone (`mgNewVersion`: the recipe's packages, one per line), never by a person,
  and the build reads it back through `sbParsePackages`, which **drops everything after a `#` on each
  line** (`jobs_script_builds.go:141–163`, verified). **The owner decided on 2026-11-21: no migration
  for this group** («Where the acceptance is kept, without a migration»).
- **The audit** (`audit_logs`: `ts`, `actor_id`, `actor_label`, `action`, `outcome`, `target_type`,
  `target_label`, `detail` text, `ip`, `user_agent`); `AUDIT__RETENTION_MINUTES` defaults to **0 =
  forever** (`config.go:155`), and an installation may set a retention.
- **The catalogue** (`backend/app/engine/models/catalog.py`): `KINDS` (`time_column` date, datetime,
  integer; `frequency`; `alpha`), `SEASON_OF_FREQUENCY` (hour 24, day 7, week 52, month 12, quarter 4,
  year 1, index 1), `OUTPUT_TYPES` with `=<key>`, `_ROWS_WILDCARDS` (a prefix → a function of the
  parameters; `_labels_of("quantiles")` already names `pred_quantile_<q>` without 0.5), `card: false`,
  `max_result_rows` 100,000, a model's own `max_rows`.
- **The series rules of lot D1** (`groups/timeseries/runner/R/ts_series.R`): the grid of the frequency,
  `duplicate_time`, `irregular_time`, `frequency_hour`, the span from the first to the last observed
  value, `missing` (refuse, linear, seasonal) with `filled`, the time written back in the input field's
  exact Arrow type (`ts_time_array`), the envelope thinning of a long series for its chart
  (`ts_envelope`). They are R; this group is Python: lot F2 writes their **Python twin** and a test
  that feeds both the same cases.
- **The explanation's machinery** (A/B E6, decomposition D4): `structured_note`, `NOTE_NAMES`, the
  number forms `pct`, `spct`, `int`, `p`, `num`, `date`; `reading_of`, `reading_under`, `how_computed`
  (in `layouts/experiments.py` until the owner's piece moves them); `measure_readings` and
  `verdict_words` with the test that no measure's sentence holds a verdict word.
- **Sample data**: the orders (`vendite.ordini`, 2023-07-01 … 2026-07-15), the customers' channel
  (`canale`: five values), the calendar `ref.calendario` (`giorno`, `festivo` boolean, `nome_festa`,
  one year past the data), the decomposition's flow pattern «calendar Left join → Fill nulls 0».

## The group `foundation_forecast` — Python

Id `foundation_forecast`; label «Pre-trained forecasting» (it «Previsioni con modelli
pre-addestrati», de «Prognosen mit vortrainierten Modellen», fr «Prévisions par modèles
pré-entraînés», es «Previsiones con modelos preentrenados»); one model, `timestral`. A group of its
own, not a member of `timeseries`: its image is Python and PyTorch, the R image is not; its licence
differs from every other group's; a customer that must not use the weights enables `timeseries` and
never this one.

### The recipe

`groups/foundation_forecast/recipe.json` (two new fields, `wheels` and `weights`, and one, `licence`,
that the panel reads; «Changes to the machinery»):

```json
{ "language": "python", "base": "3.14", "pinned_on": "2026-11-21",
  "packages": ["filelock==4.2.12", "fsspec==2026.9.0", "jinja2==3.1.6", "markupsafe==3.0.4",
               "mpmath==1.3.0", "networkx==3.7", "numpy==2.5.5", "pyarrow==26.0.1",
               "safetensors==0.8.0", "setuptools==84.2.1", "sympy==1.14.0",
               "typing-extensions==4.16.0"],
  "wheels": [
    { "name": "torch", "version": "2.14.1+cpu", "machine": "x86_64",
      "url": "https://download.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_x86_64.whl",
      "sha256": "35103e180214793207f95d7e12e100ecba3ace6f5fb37e1fcefbb07e096ed180" },
    { "name": "torch", "version": "2.14.1+cpu", "machine": "aarch64",
      "url": "https://download.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_aarch64.whl",
      "sha256": "f56cd23a3bf003bfbed3da0645ba09b3ccc66c0a2f2ac726a9b98f34e0e4926f" } ],
  "weights": {
    "licence": { "name": "Modified MIT License (Mistral AI)", "sha256": "648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c",
                 "text": "licence/LICENSE.txt" },
    "sizes": {
      "5m":   { "repo": "mistralai/Timestral-5M",   "revision": "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f",
                "files": { "model.safetensors": { "bytes": 20843960, "sha256": "7ace108c…a738" },
                           "config.json": { "bytes": 904, "sha256": "b5f27f61…7c2f" },
                           "LICENSE": { "bytes": 1695, "sha256": "64819472…3fc8c" } } },
      "28m":  { "repo": "mistralai/Timestral-28M",  "revision": "49b26409ac87b5935539a5d8ffe7f397b720db47", "files": { … } },
      "123m": { "repo": "mistralai/Timestral-123M", "revision": "5a147287addda85f6acd8da3fb8858fe9af7e8f7", "files": { … } },
      "209m": { "repo": "mistralai/Timestral-209M", "revision": "9874d252ab0aae16f7f7c999f98f98d6ad7d8365", "files": { … } } } },
  "resources": { "cpu": "2", "memory": "4Gi" }, "max_rows": 1000000, "max_seconds": 3600 }
```

(The `…` stand for the full hashes of «Sizes, and where the weights are»; the file holds them whole.)
The closure was read from the packages' `Requires-Dist` on PyPI and the CPU wheel's metadata; **F0
resolves it again** with `uv pip compile --only-binary :all:` for Python 3.14, x86_64 and aarch64, and
`pip check` in the build is the last word. **Not installed**: `huggingface_hub` and its closure
(`click`, `hf-xet`, `httpx2`, `httpcore2`, `anyio`, `idna`, `truststore`, `pyyaml`, `tqdm`,
`packaging`…: about a dozen packages the Job would import and never call; «The runner» says how),
`pandas`, `scipy` (the few statistics are written by hand with numpy and checked against SciPy as a
witness outside the image), any CUDA package, `mlflow`, `statsmodels`.

**Why the CPU wheel by URL and not an extra index**: `--extra-index-url` makes pip choose between two
indexes by version, the classic confusion; a URL with its sha256 names exactly one file. The
machinery picks the wheel of the build's machine (`uname -m` in the build, «Changes to the
machinery»). A mirror for air-gapped clusters: the setting `MODELS__WHEELS_BASE_URL` replaces
`https://download.pytorch.org/whl/cpu/` (the file name and the sha256 do not change).

**The weights in the image**: the build downloads `config.json`, `model.safetensors` and `LICENSE` of
each size the administrator chose («The licence: enabling the group»), from
`https://huggingface.co/<repo>/resolve/<revision>/<file>`, each with BuildKit's `ADD --checksum=sha256:…`
(the build fails on a different byte), into `/opt/tabularia/weights/<size>/`, owned by root, readable by
user 10001. The `LICENSE` of each size is checked against the recipe's `licence.sha256` too — the text
the administrator accepted is byte for byte the file that travels with the weights (point 1 of the
licence). A mirror: `MODELS__WEIGHTS_BASE_URL` replaces `https://huggingface.co/` (same paths, same
checksums), for a cluster that may not reach the Hub; the customer fills the mirror by itself.

**The image's size** (assumed; F0 measures): `python:3.14-slim` about 0.13 GB; torch CPU installed about
0.7 GB (a 196 MB wheel); numpy, pyarrow, sympy and the rest about 0.3 GB; weights 0.02 + 0.11 + 0.49 +
0.84 = **1.46 GB with the four sizes**. About **2.6 GB** with every size, about **1.3 GB** with 5M and
28M only. The build is a download (nothing compiles): about 1.7 GB from two hosts, minutes. The image is
pulled on a node the first time a Job lands there: the guide's advice to pre-pull on large clusters
(the models spec, «Start-up») matters more here.

**Why the weights are in the image, not in a volume** (Open question 12): the image is checked by its
digest, self-tested with the weights it holds, and immutable; the Job keeps internet access off and needs
nothing at run time. A shared volume needs a `ReadOnlyMany` storage class the cluster may not have
(kind's default has none) and a life of its own (filled by whom, checked when); the scripts bucket would
copy up to 837 MB into each Job's `/tmp` (memory, seconds) on every run.

### Changes to the machinery (lot F0, Go)

| Where | Change |
|---|---|
| `internal/modelgroups/groups.go` | `Recipe` gains `Wheels []Wheel` (`name`, `version`, `machine`, `url`, `sha256`) and `Weights *Weights` (`licence` {`name`, `sha256`, `text`}, `sizes` map → {`repo`, `revision`, `files` map → {`bytes`, `sha256`}}); `load` checks them (an https URL, a 64-hex sha256, sizes among `5m`, `28m`, `123m`, `209m`, the licence's text file present and of that sha256). `RecipeHash` stays the hash of `recipe.json` for every group (no hash moves). The **sizes a version is built with** are written in that version's `packages` text as comment lines («Where the acceptance is kept, without a migration»); a change of sizes queues a new version from the card, as **Build again** does |
| `internal/scriptimage/scriptimage.go` | `Settings` gains `Wheels` (the wheels of the build's machine, URL rewritten by the mirror setting) and `Files` ({url, sha256, path}). A managed Python image with wheels adds, before the requirements, `RUN pip install --no-deps --only-binary=:all: "<url>#sha256=<hex>"` per wheel — the architecture chosen in the Dockerfile by `ARG TARGETARCH` (BuildKit's, `amd64` → `x86_64`, `arm64` → `aarch64`), one `RUN` per architecture guarded by a shell test, so one Dockerfile serves both; then `ADD --checksum=sha256:<hex> <url> <path>` per file of the weights. **Assumed**: BuildKit 0.33's default Dockerfile frontend accepts `ADD --checksum` for an HTTP source (stable since Dockerfile 1.6) without a `# syntax=` line, and follows the Hub's redirect to its storage; F0 checks both, and the fallback is a `RUN python -I -c` download with `hashlib` (the image has Python before the weights). The R images and the other Python images are byte for byte as before (the existing test) |
| `internal/api/models_build.go` | `mgCheckScript` for this group: the lock (`pip` list), the sizes present (`ls /opt/tabularia/weights`), each `model.safetensors`' sha256 recomputed in the check Job (a second check, after the build's), then the self-test |
| `internal/api/models_admin.go` | The licence flow («The licence: enabling the group»); `mgNewVersion` appends the `# tabularia:` lines for a group with `weights`; `mgQueueIfNeeded` queues nothing without a valid acceptance in the audit |
| `internal/api/jobs_script_builds.go` | For this group's environment, the Dockerfile's weights are the sizes of the version's `# tabularia:weights` lines (the package parser already ignores them) |
| `internal/api/jobs_models.go` | Env `HF_HUB_OFFLINE=1`, `TORCH_HOME=/tmp/torch`, `HOME=/tmp`, `MKL_NUM_THREADS` and `OMP_NUM_THREADS` = the threads; `task.limits` gains the sizes present; `/tmp`'s `sizeLimit` as the boosting's rule |
| `internal/modelgroups` embed | `//go:embed … all:groups/foundation_forecast/runner/py/timestral`: the vendored package keeps its `__init__.py` (the `all:` prefix embeds names starting with `_` for that folder only). The Go test that lists the source tree and asserts `CodeFiles` holds every file counts it |
| `cmd/modelgroup` | `dockerfile foundation_forecast [--sizes 5m,28m]`, `code foundation_forecast <dir>`, as for boosting |
| Settings | `MODELS__WHEELS_BASE_URL`, `MODELS__WEIGHTS_BASE_URL` (empty: the official hosts), read by the gateway from the environment as `MODELS__ENABLED` is; the chart passes them when set; `docs/configuration.md` and `docs/deploy/kubernetes.md` list them |

The build namespace must reach `download.pytorch.org` and `huggingface.co` (and its storage's redirect
target) besides PyPI: `docs/deploy/kubernetes.md` says so, with the mirrors for air-gapped clusters.

### The runner

```text
gateway-go/internal/modelgroups/
  common/py/tab_series.py            the Python twin of ts_series.R: grid, gaps, span, time arrays (lot F2)
  groups/foundation_forecast/
    recipe.json
    licence/LICENSE.txt               the weights' licence, byte for byte (sha256 648194…3fc8c)
    runner/run.py                     sys.path, then tab_main.main("foundation_forecast", ROUTES)
    runner/py/ff_series.py            the input: series, covariates, batches, holdout cut
    runner/py/ff_timestral.py         loading, the forecasts, threads, the time budget
    runner/py/ff_measures.py          baseline, MASE, sMAPE, pinball, coverage, Wilcoxon
    runner/py/ff_reading.py           the explanation's template ids
    runner/py/timestral/              the vendored package (Apache 2.0): its nine files, LICENSE, NOTICE
    reference/data/*.csv, expected.json, make_reference.py, witness.json
```

- **No module named `timestral.py`** among the runner's: it would shadow the vendored package (the
  boosting's rule, «a module's name never equals an installed package's»).
- **The vendored package**: the nine files of `src/timestral/` at `a534bdbd244d`, byte for byte, with
  **one change**: `loading.py` imports `huggingface_hub` inside `resolve_checkpoint`'s Hub branch instead
  of at the top, so that a local directory — the only way the runner loads — needs no `huggingface_hub`
  in the image. `NOTICE` says it («Modified by Tabularia: the import of huggingface_hub moved into
  resolve_checkpoint; nothing else changed»), as Apache 2.0's §4(b) asks; `vendor.json` holds the commit
  and each file's sha256 at that commit, and a test compares the files with those hashes, `loading.py`
  against its patched hash. Why not install it from GitHub in the build: `timestral` is not on PyPI, so
  the build would compile an sdist from a third host, against `--only-binary=:all:`; why not install
  `huggingface_hub`: a dozen packages imported for nothing. The numbers of the vendored copy equal the
  official package's on the reference cases (the witness, below). Open question 11.
- **Loading**: `TimestralForecaster.from_pretrained("/opt/tabularia/weights/<size>", device="cpu")`,
  never a repo id; `HF_HUB_OFFLINE=1` besides.
- **Threads**: `torch.set_num_threads(threads)` and `torch.set_num_interop_threads(1)` before anything
  else, `threads` = the CPU limit (`model|||threads`).

### Determinism

- **Nothing is random**, so the node has **no `seed`** (the decomposition's rule).
- **The same input, parameters, group version, thread count and CPU family give the same bits**: the
  self-test forecasts the same cases twice in one process and compares bit for bit.
- **Batches never change a series' forecast** by their composition: a batch holds only series with the
  same number of patches (the same `T` after padding to 32) and the same horizon, in the series' text
  order, so no series is padded because of another. **Assumed**: a series forecast alone and inside a
  batch of 16 give the same bits (PyTorch's CPU matrix products usually do not depend on the batch's
  size, but nothing promises it). F0 measures it; if they differ, the batch size becomes a constant of
  the runner (not of the data), and the report's notes say that a series may differ in its last digits
  from the same series forecast alone.
- **Threads and CPUs**: the sums of a matrix product may be split differently with 1, 2 or 4 threads and
  on CPUs with different vector units (AVX2, AVX-512, NEON). F0 measures the gaps (1 against 2 and 4
  threads; the dev machine against the image on kind), and they set the self-test's tolerance against
  values computed outside the image. The report writes the threads and the CPU's vector unit
  (`torch.backends.cpu.get_cpu_capability()`).

## The licence: enabling the group

### In the panel (`ModelsSection.vue`) — decided 2026-11-21

The group's card, before **Enable**, holds a box **«Licence of the model weights»** (its own colour, an
info icon, not an error):

1. **One sentence**: «The Timestral weights are Mistral AI's, under the Modified MIT License. Tabularia
   does not ship them: enabling this group downloads them into your cluster from Mistral AI's
   repository.»
2. **The clause, quoted**: point 2 of the licence, verbatim, in English (the legal text is not
   translated; the sentences around it are, in five languages), with the threshold in bold.
3. **What it means, in one line**: «If your company's (or your employer's) global consolidated revenue
   was above $20 million in the last month, this licence gives you no right to use the weights: you need
   a commercial licence from Mistral AI.» — and «This is not legal advice.»
4. **Links**: the full text at the pinned revision
   (`https://huggingface.co/mistralai/Timestral-28M/blob/49b26409…/LICENSE`), and the full text shipped
   with Tabularia, expandable in place (`licence/LICENSE.txt`, the same bytes).
5. **The sizes to download**, checkboxes with their sizes («5M · 21 MB», «28M · 111 MB», «123M · 490 MB»,
   «209M · 837 MB»), all ticked by default (decided 2026-11-21); at least one.
6. **The checkbox**, unticked, required: «I have read the licence of the Timestral weights, and the
   organisation that uses this installation may use them under it, or holds a separate licence from
   Mistral AI.» (decided 2026-11-21: these words; no question on revenue).
7. **Enable** stays disabled until the box is ticked; its tooltip says why.

After enabling, the card keeps a line: «Weights licence: Modified MIT (Mistral AI) — accepted by
ada@example.com on 21 Nov 2026 for version 3 · read it», always shown while the group exists, with the
sizes built. Who and when are read from **the current version's record** (below), so the line survives
an audit retention. **Disable** does not withdraw the acceptance (it is a fact of the past). The box
comes back — unticked, required — whenever a build is about to be queued and the gateway finds no valid
acceptance («When the acceptance is asked again»).

### Where the acceptance is kept, without a migration (decided 2026-11-21)

The owner decided: **no migration**, the acceptance in the audit. What the schema offers, read on
2026-11-21 («What exists and is reused»): `model_groups` has **no field** for a licence (no JSON of
settings; `enabled_by`/`enabled_at` are overwritten at every enabling and say nothing of a licence). So
the design has two parts, each in something that already exists:

1. **The act — the audit entry `models.licence_accept`** (target type `model_group`, target label
   `foundation_forecast`, actor the administrator, `ts`, `ip`, `user_agent`; `detail` a JSON object:
   `licence_name`, `licence_sha256`, `sizes`, `recipe_hash`). This is **the** record of the acceptance,
   and the only thing the gateway consults to decide whether a build may start.
2. **What each build relied on — two comment lines in the version's `packages` text**, written by
   `mgNewVersion` after the recipe's packages:

   ```text
   # tabularia:weights 5m,28m,123m,209m
   # tabularia:licence 648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c accepted-by ada@example.com accepted-at 2026-11-21T10:14:03Z audit 81234
   ```

   `packages` is Tabularia's own text for a managed environment (never typed by a person), kept with the
   version for as long as the version exists, and the build's parser already drops everything after `#`
   (`sbParsePackages`, verified), so the lines change no build of any other group. The builder of this
   group reads the sizes from the first line; the panel, the report and MLflow read who and when from the
   second. This is **not** the acceptance — it is the version's own note of the acceptance it was built
   under, the way `lock` is its note of what it installed.

**The rules the gateway applies** (`models_admin.go`, the build loop):

- **A valid acceptance** for a build is an audit row `models.licence_accept` for this group whose
  `detail.licence_sha256` equals the shipped `licence.sha256`, still present in `audit_logs`.
- **Enable, Build again, a change of sizes** (an administrator's click): the request must carry the
  acceptance (`{"licence_sha256": "<hex>", "accept": true, "sizes": [...]}`); the gateway writes
  `models.licence_accept` **in the same transaction** as the version it queues, so the version's line
  and the audit row name the same act (`audit` = the audit row's id). A missing or false `accept` → 409
  «enabling this group requires accepting the licence of its weights»; a `licence_sha256` other than the
  shipped one → 409 «the licence shown is not the one this Tabularia ships: reload the page»; `sizes`
  empty or unknown → 422. **Every administrator's click that queues a build asks again**: the panel shows
  the box each time, ticked by nobody (it is one tick, and it keeps the record next to every build).
- **A build queued by Tabularia itself** (a new recipe shipped, `mgQueueIfNeeded` at start-up or in the
  build loop): queued only if a valid acceptance exists in the audit for the shipped licence; its
  version's line copies that acceptance (actor, time, audit id) and the sizes of the version in use.
  Without one, nothing is queued: the group's state is **`licence_pending`** («Licence to accept» in the
  panel), and its steps fail with «the model group «foundation_forecast» waits for an administrator to
  accept the licence of its weights».
- **Runs never consult the audit**: a ready version runs, whatever the audit holds now — it was built
  under an acceptance its own record names. Revoking is **Disable**.
- A group without `weights` ignores all this, as today.
- **Who**: administrators, not with an API token (the section's rule). `models.group_enable`,
  `models.group_rebuild`, `models.group_settings` are written as today, beside `models.licence_accept`.
- `GET /admin/models` and `/models/palette` answer the licence (name, sha256, the link), the current
  version's acceptance line, whether a valid acceptance exists for the shipped licence (`licence_pending`
  otherwise), and, for the palette, the sizes the ready version holds (the form offers only those).

**When the acceptance is asked again, and the limits** (said in the documentation and in the panel's
help):

- **The audit is kept forever by default** (`AUDIT__RETENTION_MINUTES = 0`): then the acceptance is asked
  at each administrator's build, and an automatic rebuild after a Tabularia upgrade proceeds on the
  acceptance in the audit.
- **With a retention set**, once the acceptance's row is deleted: the version in use **keeps running**
  (its own line names who accepted and when); the **next build** — an administrator's click (which asks
  anyway) or an upgrade's automatic rebuild — needs a new acceptance. An upgrade that ships a new recipe
  then leaves the group in `licence_pending`, and its steps stop until an administrator accepts, as
  «Rebuild needed» stops them today. The panel warns ahead when the retention is shorter than the age of
  the acceptance («the acceptance of 21 Nov 2026 will leave the audit on …: the next rebuild will ask
  again»).
- **The history of acceptances** is the audit's while it keeps them, and the versions' lines for as long as
  the versions exist; a version deleted with its environment takes its line with it. There is no
  separate, permanent register: that was the table the owner declined.
- **The version's line is text**, protected like every version field (only administrators reach it, only
  Tabularia writes it); it is not a signature. The audit row's id in it lets a reviewer match the two
  while the audit keeps the row.
- **A licence text changed by a Tabularia release** always needs a new acceptance: the shipped sha256 no
  longer matches any audit row.

### Where else the note appears

| Where | What |
|---|---|
| The group's card | The line above (from the current version's record), with the link, while the group exists |
| The node's form | Under the size: «Weights by Mistral AI under the Modified MIT License, accepted for this installation by your administrator» and the link |
| The report | First page: one line under the cards («Forecast by Timestral-28M, weights by Mistral AI under the Modified MIT License: see the last page»); the last page: the attribution notice and the licence's text verbatim, the repository, the revision and the sha256 of the weights used |
| MLflow | Tags `tabularia.weights.repo`, `.revision`, `.sha256`, `tabularia.weights.licence_sha256` |
| Documentation | `docs/capabilities/models.md` «Pre-trained forecasting (Timestral)» opens with the licence: the clause quoted, the threshold, who decides, «not legal advice», the link; `docs/governance/audit-log.md` gains `models.licence_accept`; `docs/deploy/kubernetes.md` the hosts and mirrors |

### When the licence changes

- **The pinned revision never changes by itself**: a Hub commit is immutable, and the recipe's
  checksums fail the build if a byte differs. A licence change reaches Tabularia only when a Tabularia
  release moves the revision.
- **Moving a revision** is a lot of its own: fetch the new `LICENSE`, compare it with
  `licence/LICENSE.txt`; equal → only the revisions and the weights' hashes change; different → the new
  text replaces the file, `licence.sha256` changes, the spec and the documentation quote the new clause,
  `CHANGELOG.md` says «the Timestral weights' licence changed: administrators must accept it again», and
  a Go test fails if `licence.sha256` changed while the documentation still quotes the old clause (the
  documentation's quote carries the sha256 in a comment).
- **On an installation**, a new Tabularia with a new licence: the group shows **«New licence to
  accept»** (`licence_pending`), no version is built until an administrator accepts; the steps fail with
  «the model group «foundation_forecast» waits for an administrator to accept the new licence of its
  weights», as «Rebuild needed» stops them today (decided 2026-11-21, Open question 5).
- **If Mistral AI withdraws or gates the repositories**: a new build fails («the weights could not be
  downloaded from …: the repository may be gated or gone; a mirror can serve them»); the images already
  built keep working. A gated repository would need a token setting; not in this release.

## The node `timestral` — Forecast (Timestral)

Palette: under *Pre-trained forecasting*, one entry **Forecast (Timestral)** (it «Previsione
(Timestral)»), the size in the form.

### The form

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `time` | `time_column` (date, datetime, integer) | — (needed) | always | One row per period (per series) |
| `value` | `number_column` | — (needed) | always | The series; empty in the future rows that carry known covariates |
| `series` | column (text, integer, boolean) | none | always | One series per value of this column; none: the table is one series |
| `frequency` | `frequency` | — (needed) | always | hour, day, week, month, quarter, year, index: the grid, the future times |
| `season` | integer 1–`max_period` | the frequency's (`SEASON_OF_FREQUENCY`: 24, 7, 52, 12, 4, 1, 1) | always | The baseline's period and MASE's scale; 1: none (the naïve forecast) |
| `size` | choice `5m` \| `28m` \| `123m` \| `209m` | **`28m`** (Open question 4) | always | The model; the form offers the sizes the ready version holds |
| `horizon` | integer 1–`max_horizon` (1,024) | 28 | without `known_covariates` | Steps after each series' last observed value |
| `quantiles` | a subset of the 21 levels without 0.5 | **0.05, 0.1, 0.9, 0.95** | always | The quantile columns of the output (the median is `forecast`); the report always draws the 80 % and 90 % bands |
| `past_covariates` | columns (number, boolean, text) | none | always | Known only up to each series' last observed value |
| `known_covariates` | columns (number, boolean, text) | none | always | Known also in the future: the future rows of the input hold them, and the horizon is those rows |
| `holdout` | integer 0–`max_horizon` | **= `horizon`** (with known covariates: the future rows' count) | always | The last k observed values of each series kept out, forecast from before them, measured; 0: no holdout, no measures |
| `missing` | choice `refuse` \| `linear` \| `model` | `refuse` | always | A period without a value inside a series' span |
| `symmetric_averaging` | boolean | true | always | The library's default: forecast the series and its negation and average (two passes) |
| `clip_nonnegative` | boolean | true | always | The library's default: a series never negative gets no negative forecast |
| `history` | boolean | true | `output = forecast` | Write the observed rows before the forecast |
| `alpha` | `alpha` | 0.05 | always | The comparison's test |
| `output` | choice `forecast` \| `results` | `forecast` | always | What the next step receives |

`disjoint`: `time`, `value`, `series`, `past_covariates`, `known_covariates`. `max_covariates` (16) for
both lists together. No `seed` («Determinism»). Variants: one, `default`; the sections that depend on
`holdout` and on the series' count are declared with `when` rules.

**Why these defaults.** `28m`: 3.6 times faster than 209M in the paper's table, with a small loss on
the authors' benchmarks (Open question 4); a node with many series must stay light. `horizon` 28: four
weeks of days (the sample flow). Quantiles 0.05, 0.1, 0.9, 0.95: the 90 % and 80 % bands, the widest
common ones the model has (no 95 %). `holdout` = horizon: the forecast is measured on the distance it
is used for.

### Combinations

| Rule | Kind | Reason id: sentence |
|---|---|---|
| `horizon` | `applies` without `known_covariates` | — |
| `history` | `applies` with `output = forecast` | — |
| `season` greater than 1 with `frequency = year` | allowed | a yearly series with a known cycle (sunspots) |
| `size` not in the ready version | refused by the gateway | `size_absent`: «Timestral-209M was not downloaded by this installation: choose 5M or 28M, or ask an administrator to add it» |
| a level in `quantiles` not among the 21 | refused | `quantile_level`: «Timestral forecasts the levels 0.01, 0.05, 0.1 … 0.95, 0.99: 0.025 is not one» |

### The series

The rules of `ts_series.R`, in Python (`tab_series.py`, lot F2), with the same codes and sentences,
**per series**:

- one row per period of `frequency` per series (`duplicate_time`, «aggregate first with Group by»; with
  `series`, the sentence names the series); times on the frequency's grid (`irregular_time`); a date
  with `frequency = hour` refused (`frequency_hour`); a text or boolean `value` refused; an infinite
  value stops the node;
- **the span** of each series runs from its first to its last observed value; rows before the first are
  left out and counted (`n_missing`); rows after the last are **future rows**: used only with
  `known_covariates` (their covariates), otherwise left out and counted;
- **a period without a value inside the span**: `refuse` (default) — `gaps`, the decomposition's
  sentence («the series «Online» has 3 days without a value inside its span (the first: 2024-08-15):
  fill them in the flow (a calendar Left join and Fill nulls with 0 when an empty day means none), or
  choose missing = linear or model»); `linear` — straight-line interpolation between the neighbours,
  `filled = true`; `model` — the period stays empty, handed to Timestral as NaN (the model embeds «each
  patch … from its values together with their missing-value indicators»), `filled = false`, counted as
  `n_model_missing`. The decomposition's `seasonal` fill is not offered: it is an MSTL, which this image
  does not have (Open question 6);
- **each series has its own end**: its forecast starts after its own last value; a note lists the series
  that end before the latest end («4 series end before 15 Jul 2026: their forecasts start earlier»);
- **length**: a series needs one observed value to be forecast. Its measures need `holdout` + `season` + 1
  observed values (the holdout and one season of history for MASE's scale); a shorter series is forecast,
  its measures are «not computed» with that reason, and it does not count in the comparison;
- **context**: Timestral reads at most the last 32,768 values of a series; a longer one is read in full
  for the output and the baseline, cut for the model, and noted («the model read the last 32,768 of
  100,000 days of «Online»»);
- **types**: the output's `time` is written in the input field's exact Arrow type (Int64, Int32, date32,
  timestamp with its unit and zone), filled instants and future instants included (`ts_time_array`'s
  twin); the `series` column too (`=series`, the A/B's `ab_variant_array` pattern).

### Covariates

- **Numbers** go as they are; **booleans** as 0/1 (a known 0/1 row is an indicator for the model);
  **texts** as categorical codes — the labels (`catalog.label_of`) in text order, codes 0…k−1 — into
  `categorical_past_covariates` or `categorical_known_covariates`, target-encoded by the library.
- **Past covariates**: their values on the span's rows; their values on future rows are ignored.
- **Known covariates**: the future rows of each series — the rows after its last observed value, with
  `value` empty — must follow it on the grid without a hole («`known_future_gap`: the future rows of
  «Online» skip 2026-07-20: the future must be complete on the grid»); their count is the series'
  horizon (at most `max_horizon`); a series without a future row is refused («`known_future_missing`:
  the series «Online» has no row after its last value: with known covariates the future rows hold
  them»). Different series may have different horizons.
- An empty covariate value is passed as NaN (the library reads it as missing) and counted per column
  (`model|||covariate_missing_<k>` and a note); a covariate column with no value at all is refused
  (`empty_covariate`).
- With a holdout, the covariates of the holdout rows are the real ones (known covariates are known).

### The forecast

1. **Read and cut**: per series the span (filled or not), the future rows, the covariates; with
   `holdout` k, the first forecast's context ends k values before the span's end.
2. **Batches**: series grouped by (patches of their context, horizon), in text order of their labels,
   at most `batch_rows` series per call, `batch_rows` set by memory (the activations of one call kept
   under a quarter of the memory limit: F0 measures the bytes per patch per series and writes the rule).
3. **Two forecasts per series** when `holdout` > 0: from the cut, `holdout` steps (measured), and from the
   end, `horizon` steps (the forecast). Without holdout, one.
4. **The time budget** comes from the step's timeout (the boosting's rule, no number of ours): the runner
   times the first batch of the first forecast; if the projected total passes 85 % of the time left, it
   stops at once with exit 3, `too_slow`: «at the speed of the first 64 series (0.31 s each with
   Timestral-209M on 2 CPUs), 10,000 series need about 52 minutes; the step allows 15: choose a smaller
   size, fewer series, or a longer timeout». Nothing is written but the refusal: a half forecast is
   worse than none.
5. **Outputs** per series and step: the 21 quantiles (all kept for the measures and the charts); the
   output writes the median and the chosen ones.

### The baseline and the measures

All on the holdout (the last k observed values of each series), **measures, without verdict**:

| Measure | Formula (series i, holdout steps t = 1…k, median ŷ, season m) |
|---|---|
| Scale | sᵢ = mean over the history before the holdout of \|y_τ − y_{τ−m}\| (the in-sample seasonal naïve's error; m = 1 for none) |
| MASE | mean_t \|y_t − ŷ_t\| / sᵢ; the baseline's the same with its forecast |
| Relative MASE | MASE / baseline's MASE; across series their **geometric mean** (the usual summary of ratios) |
| sMAPE | mean_t 200 \|y_t − ŷ_t\| / (\|y_t\| + \|ŷ_t\|) in %, steps where both are 0 left out and counted |
| Pinball loss | ρ_q(u) = max(q u, (q − 1) u), u = y − ŷ_q; mean over the 21 levels and the steps; also **scaled** (÷ mean \|y_t\|, the «weighted quantile loss») so that series of different sizes compare |
| Coverage | share of steps inside the 80 % band (0.1–0.9) and the 90 % band (0.05–0.95) |
| Across series | the mean MASE and sMAPE, the geometric mean of the relative MASE, the pooled coverage, the scaled pinball loss weighted by each series' scale |

**The baseline** is the **seasonal naïve** forecast, the last season repeated: ŷ_{T+h} = y_{T+h−m⌈h/m⌉}
(the naïve, the last value, when m = 1). It needs no fit, costs nothing, and is the reference MASE is
built on (a MASE below 1 beats the in-sample seasonal naïve; a relative MASE below 1 beats it on the
holdout). ARIMA as a second baseline would need `statsmodels` and `scipy` in the image and a fit per
series (`auto.arima` takes seconds per seasonal series): against the light-runtime rule for many series;
the flow can run the ARIMA node on the same table (Open question 8).

**Why the coverage has no test**: the k errors of one forecast origin are not independent (a forecast
too low on day 1 is usually too low on day 2), so a binomial test of «80 % of the values inside» would
claim far more certainty than the data hold. The report gives the share and says what to read in it.

### The comparison test

**One test**, when at least two series have their measures: **Wilcoxon's signed-rank test** on the
per-series differences dᵢ = MASEᵢ(Timestral) − MASEᵢ(baseline) (the difference of the scaled absolute
errors), H0: the differences are symmetric around 0 (neither forecast is better on these series),
two-sided, against the node's `alpha`. Exact null distribution without ties and zeros up to 50 series
(the counts of the 2ⁿ sign patterns by dynamic programming, numpy), normal approximation with the tie
correction above or with ties — as `scipy.stats.wilcoxon` with `method = "auto"`, its witness; a zero
difference is dropped (`zero_method = "wilcox"`) and counted. Verdict: `supported` (Timestral better,
p < alpha and the median d < 0), `opposite` (the baseline better, p < alpha and median d > 0),
`not_supported` (p ≥ alpha). The row also writes how many series each side won.

Why this test: the question «is the pre-trained model's advantage over the seasonal naïve more than the
luck of these series?» is a test's question, and the series are the independent units the data hold
(assumed independent: the report says so). It needs no distribution of the errors, and it is a few
lines of numpy. **With one series**, no test: «not computed: one series gives one comparison; a test
needs several series (or several forecast origins)»; the measures remain. Testing one series would need
several forecast origins (Diebold–Mariano on rolling one-step forecasts: k forecasts per series), which
multiplies the cost by the holdout's length (Open question 7).

## The output

### What the next step receives (`output`)

`forecast` (default) — **one row per instant and series**:

| Column | Type | When | What |
|---|---|---|---|
| `series` | `=series` (the input column's exact type) | with `series` | The series |
| `time` | `=time` (the input column's exact type) | always | The instant |
| `observed` | Float64 | always | The value; empty where filled, missing, or in the future |
| `forecast` | Float64 | always | The median (0.5): on holdout rows the holdout forecast, on future rows the forecast, empty on history rows |
| `quantile_<level>` | Float64, one per level of `quantiles`, increasing | always | Where `forecast` is |
| `baseline` | Float64 | always | The seasonal naïve on holdout and future rows |
| `filled` | Boolean | always | Interpolated by `missing = linear` |
| `is_holdout` | Boolean | always | Kept out and forecast from before it |
| `is_forecast` | Boolean | always | After the series' last value |

With `history = false`, only holdout and future rows. Rows in the order: series (text order of the
labels), then time.

### Columns before the Run (`OUTPUT_TYPES`, `output_columns`)

`OUTPUT_TYPES["quantile_forecast"]`: `series` `=series`, `time` `=time`; `observed`, `forecast`,
`quantile_*`, `baseline` Float64; `filled`, `is_holdout`, `is_forecast` Boolean. `series` has a
`rows_when` rule (`series` given). The wildcard `quantile_` is one more rule of `_ROWS_WILDCARDS`, the
same function as `pred_quantile_` (`_labels_of("quantiles")`, 0.5 left out: it is `forecast`), so
`quantiles = [0.05, 0.1, 0.9, 0.95]` gives `quantile_0.05`, `quantile_0.1`, `quantile_0.9`,
`quantile_0.95` before a run. The three copies of the rule (engine, `modelCatalog.ts`,
`ai_flow_agent.py`) and `output_columns.json` (made by running the runner) as the decomposition did. A
clash between an output column and nothing else is impossible: the output holds only these columns.

### The results table (`results`, and always kept with the run)

The ten columns. Sections:

| Section | `level` | `level_2` | Holds |
|---|---|---|---|
| `model` | null | null | texts `model`, `variant`, `formula` («orders ~ Timestral-28M · 5 series by channel · daily · 28 days ahead»), `software` («Python 3.14.x; torch 2.14.1+cpu; timestral 1.0.0 (a534bdbd)»), `weights` («mistralai/Timestral-28M@49b26409»), `weights_sha256`, `licence` («Modified MIT License (Mistral AI)»), `licence_sha256`, `group_version`, `rows_from`, `note_*`, `frequency`, `time_kind`, `missing`, `size`, `cpu_capability`; numbers `n_series`, `n_points`, `n_missing`, `n_filled`, `n_model_missing`, `n_future_rows`, `horizon` (the largest), `holdout`, `season`, `alpha`, `threads`, `batches`, `seconds_load`, `seconds_forecast`, `n_quantiles`, the across-series measures (`mase`, `baseline_mase`, `relative_mase_geomean`, `smape`, `baseline_smape`, `wql`, `coverage_80`, `coverage_90`, `n_measured`) |
| `series` (new: level required, level_2 null) | the series' label | — | numbers `n`, `n_used` (the context the model read), `n_missing`, `n_filled`, `first`, `last` (x0), `horizon`, `scale`, `mase`, `baseline_mase`, `relative_mase`, `smape`, `baseline_smape`, `pinball`, `wql`, `coverage_80`, `coverage_90`, `total` (Σ\|y\|, the order of the charts); text `not_measured` (a reason id) |
| `assumption` | `baseline_comparison` | null | the test: `h0`, `statistic` (W), `p_value`, `significance`, `verdict`, `advice`, `advice_id`, `count` (series compared), `value` (the median of d); and the measures with `value`, no verdict: `coverage_80`, `coverage_90`, `relative_mase` |
| `series_curve` (new: level and level_2 required) | the series' label | the line: `observed`, `median`, `q0.05`, `q0.1`, `q0.9`, `q0.95`, `baseline`, `holdout_median`, `holdout_q0.1`, `holdout_q0.9`, `holdout_q0.05`, `holdout_q0.95` | stat `y`, `x0` = the time's axis (ARIMA's `x0`) |
| `reading` | the block | a series or null | `line_*`, `@id name=value…` |

**Which series are drawn**: the first `max_series_charts` (12) by `total`, then text order; the history
drawn is the last `max(3 × horizon, 4 × season)` instants, thinned by the envelope rule
(`ts_envelope`'s twin, 2,000 points at most per line). Every series is in the `series` section and in
the Excel. Rows: 12 series × (≤ 2,000 history + 11 lines × ≤ 1,024) ≈ 160,000 at the very worst —
above `max_result_rows` (100,000): the drawn history shrinks to fit (an even share per series, said in
the notes), the measures never.

## Explaining the method in the report

The A/B's and the decomposition's mechanism, unchanged: the runner (`ff_reading.py`) chooses template
ids by the verdicts and options and writes `reading|<block>|<level_2>|line_<k>` = `@<id> name=value…`
(raw numbers at 17 digits); the layout says them with `structured_note(…, texts = "reading_texts")` from
`report_texts/timestral.json`, five languages; measures never use a verdict word (the shared test).
New names in `NOTE_NAMES`: `series`, `size`, `level`.

| Block | Reading (under the block), with the run's numbers | Method (last page) |
|---|---|---|
| `summary` | «Timestral-28M forecast 5 series of «orders» by «canale», 28 days ahead from 15 Jul 2026. It is a model trained in advance on many other series; it was not fitted on yours: it reads each series' recent history (up to 1,111 days here) and continues it.» + gaps: «3 days without a value were filled by linear interpolation; they are marked in the output.» + covariates: «It also read «festivo», known in the future.» | **What a pre-trained («zero-shot») model is**: trained once, by its authors, on a large collection of synthetic and real series; here it only reads; nothing about your data is learned or kept; why this needs no fitting and no tuning |
| `forecast` (per series drawn) | «The median says 41.2 orders on 16 Jul; there is an 80 % chance, by the model's own reckoning, that the day falls between 33.0 and 49.8, and 90 % between 30.4 and 52.7.» | **What a quantile is**: the 0.9 quantile is a value the model expects to be exceeded one time in ten; the median one in two; the bands are pairs of quantiles; why the bands widen with the distance |
| `holdout` (measure) | «On the last 28 days, kept out and forecast from before them, the forecast missed by 0.62 times the usual day-to-day-of-the-week change (MASE 0.62); the same-weekday-last-week forecast missed by 1.00 times it (MASE 1.00).» | MASE, sMAPE, the pinball loss, in words and formulas; why a holdout: a forecast must be judged on values it did not see |
| `coverage` (measure) | «The 80 % band held 22 of the 28 days (79 %), the 90 % band 26 (93 %). With one forecast origin these shares move a lot by chance; read them as a hint, not a measurement of the bands' quality.» | How to read a coverage: close to the nominal share is good; far below means the bands are too narrow for this series; why no test (the errors of one origin are not independent) |
| `comparison` (test, ≥ 2 series) | supported: «Timestral beat the seasonal naïve on 4 of 5 series. If neither forecast were better, differences this one-sided would appear by chance with probability 0.06, above α = 0.05: the data do not show an advantage.» — or «… with probability 0.003, below α = 0.05: Timestral's advantage is supported on these series.» — one series: «With one series the comparison is a measure: …» | Wilcoxon's signed-rank test, its hypothesis, why the series are the units, what «not supported» does not mean |
| `limits` | «Timestral does not know your business: a promotion, a price change, a new shop or a closure is invisible to it unless a column says so, known in the future. It reads only the series and the columns given.» + per context: «It read the last 32,768 of 100,000 hours of «site A».» | What a pre-trained model cannot know; why the covariates help only when they are really known ahead; the context cap; the clamping at 0 and the symmetric averaging the library applies |
| `licence` | «The weights are Mistral AI's, under the Modified MIT License, accepted for this installation by ada@example.com on 21 Nov 2026.» | the licence page («The report») |
| `method`, `formulas` | — | «How this report was computed»: one paragraph per part that ran; the formula box (the scale, MASE, sMAPE, ρ_q, WQL, coverage, the seasonal naïve, Wilcoxon's W and its normal approximation) |

Template ids: `summary`, `summary_covariates`, `filled_linear`, `missing_model`, `forecast_bands`,
`holdout_mase`, `holdout_not_measured`, `coverage`, `comparison_supported`, `comparison_opposite`,
`comparison_not_supported`, `comparison_one_series`, `comparison_not_computed`, `limits`,
`limits_context`, `limits_clip`, `licence`, `method_*`, `formula_*`.

## The report (pixel perfect)

`backend/app/engine/models/layouts/timestral.py`; words in `report_texts/timestral.json`; A4 portrait,
`common.page`, the header and footer of every model; each time chart drawn twice (dates, integers), one
visible by `time_kind` (ARIMA's way).

1. **Summary**: four cards — *Series* (5 · 5,555 points · «1 Jul 2023 – 15 Jul 2026»), *Model*
   (Timestral-28M · CPU, 2 threads), *Horizon* (28 days · holdout 28), *Holdout MASE* (0.62 against
   1.00 for the seasonal naïve; no colour); the formula line; **the licence line**; the comparison's row
   «Checks at α = 0.05»; «Measures, not tests» (mean MASE, relative MASE, sMAPE, WQL, coverage 80 % and
   90 %); readings `summary`, `holdout`, `comparison`.
2. **Forecasts**: one chart per series drawn, two per page — the history (thin line), the 90 % band
   (light) and the 80 % band (darker) as `band_low`/`band_high`, the median (accent), the baseline
   (dashed), a vertical line at the forecast's start (`target_x`); the series' reading under it.
3. **Holdout**: the same series around the holdout — observed, the holdout's median and bands, the
   baseline, a vertical line at the cut; the table of every series (MASE, relative MASE, sMAPE, scaled
   pinball, coverage 80/90, not measured and why); a bar chart of the relative MASE of the first 20
   series by `total` with a line at 1; readings `holdout`, `coverage`.
4. **Limits**: the reading `limits` and its siblings, the covariates used, the contexts cut.
5. **How this report was computed**: `how_computed` — a paragraph per part, the formula box.
6. **Notes and licence**: the notes (rows left out, filled, series ending early, contexts cut, batches,
   the time budget used), the software, the group version, the weights (repository, revision, sha256),
   and **the licence**: the attribution notice and the full text verbatim, with who accepted it and when.

**Excel**: sheets *Series* (every series' measures), *Checks* (the test and the measures), *Reading*,
*Licence*. The forecast itself is the node's output (its Table tab exports it), not repeated.

## Performance and limits

**Estimated cost** (assumed, from the paper's Table 2 divided by an assumed factor of 2 for float32
without compilation, on the group's 2 CPUs; symmetric averaging on; one forecast — **double it with a
holdout**; F0 measures every cell, the lots after it repeat the ones they touch):

| Part | 5M | 28M | 123M | 209M |
|---|---|---|---|---|
| Python, `import torch`, numpy, pyarrow (fixed) | 1.5–3 s | 1.5–3 s | 1.5–3 s | 1.5–3 s |
| Loading the weights (fixed; from the image's layer, cold page cache) | < 0.5 s | 0.5–1 s | 1–3 s | 2–5 s |
| 1 series, 1,000 points, 28 steps | 0.03 s | 0.08 s | 0.3 s | 0.5 s |
| 1 series, 10,000 points | 0.1 s | 0.3 s | 1.2 s | 2 s |
| 1 series, 100,000 points (the model reads 32,768) | 0.3 s | 1 s | 5 s | 8 s |
| 100 series × 1,000 points (100,000 points) | 1 s | 4 s | 17 s | 29 s |
| 1,000 series × 100 points (100,000 points) | 5 s | 20 s | 85 s | 145 s |
| 10,000 series × 100 points (1,000,000 points) | 50 s | 3.5 min | 14 min | 24 min |
| Peak memory (weights twice while loading, the runtime, one batch) | ~0.4 GB | ~0.6 GB | ~1.5 GB | ~2.3 GB |
| Reading, grid, measures, test, output (all sizes) | O(points): < 1 s at 100,000 | | | |

So a single series is dominated by the fixed start (3–8 s); many series by the model, linear in their
count; 209M on 10,000 series does not fit a 15-minute step (the time budget says so after the first
batch). The test and the measures cost nothing next to the model. With covariates, the paper's factor
(«1.7 to 2.0 times as long» for seven known covariates) on top.

| Limit | Default | Where | Beyond it |
|---|---|---|---|
| Input rows (`max_rows`) | 1,000,000 | group setting | the common sentence, before the Job |
| Series (`max_series`) | 10,000 | catalogue, checked by the runner on reading | `too_many_series`: «the column «sku» makes 48,212 series; this node forecasts at most 10,000: filter or aggregate first» |
| Context per series | 32,768 (the model's) | model | not a refusal: the most recent kept, noted |
| Horizon (`max_horizon`) | 1,024 (one forward pass, as trained) | catalogue | the form's bound; with known covariates, `known_future_long` (Open question 13) |
| Holdout | 0–1,024; at most the series' length − 1 | catalogue, runner | a series too short is forecast without measures (`not_measured`) |
| Covariates (`max_covariates`) | 16 | catalogue | the form's bound |
| Levels of a text covariate | 1,000 (`max_levels_trees`, reused) | catalogue | «the column … has 4,812 values: is it an identifier?» |
| Memory, CPU | 4 GiB, 2 CPU (request half) | group setting | the cluster's exit 137, the Script node's message |
| Time | the group's 3,600 s; the node's timeout within it; the measured projection after the first batch | group, node, runner | `too_slow` (exit 3) with the measured speed |
| Charts | 12 series, 2,000 points per line | catalogue (`max_series_charts`), runner | the rest in the table and the Excel |

## Refusals

Through `Refusal(code, message, params)`, exit 3. Shared with the series rules (same code, same
sentence as `ts_series.R`): `no_column`, `time_type`, `frequency_hour`, `not_a_number`,
`duplicate_time`, `irregular_time`, `no_values`, `gaps`. New:

| Code | Sentence |
|---|---|
| `too_many_series` | «the column «sku» makes 48,212 series; this node forecasts at most 10,000: filter or aggregate first» |
| `known_future_missing` | «the series «Online» has no row after its last value: with known covariates the future rows hold them» |
| `known_future_gap` | «the future rows of «Online» skip 2026-07-20: the future must be complete on the grid» |
| `known_future_long` | «the series «Online» has 1,200 future rows; Timestral forecasts at most 1,024 steps» |
| `empty_covariate` | «the column «promo» has no value: drop it from the covariates» |
| `size_absent` | «Timestral-209M was not downloaded by this installation: choose 5M or 28M, or ask an administrator to add it» |
| `too_slow` | «at the speed of the first 64 series (0.31 s each with Timestral-209M on 2 CPUs), 10,000 series need about 52 minutes; the step allows 15: choose a smaller size, fewer series, or a longer timeout» |
| `degenerate` | «Timestral could not forecast «Online»: its values are degenerate (the library produced no number)» — the library's `ValueError`, caught and named |

## MLflow, and no Predict

**Logged** on a flow run by the existing code: the parameters (flattened); metrics: every number of
`model` (the across-series measures, the counts, the timings), `assumption.baseline_comparison.statistic`
and `.p_value`, and `series.<label>.<stat>` for the first 100 series by `total` (the 250-character key
rule, collisions suffixed as the boosting's); artifacts `tabularia/results.parquet` and
`tabularia/report.pdf`; tags `tabularia.weights.repo`, `.revision`, `.sha256`, `.licence_sha256`,
`tabularia.size`. The forecast output is the flow's data, not logged.

**No card, no registration, no Predict** (`card: false`; `PREDICT` has no entry). A zero-shot model
estimates nothing from the data: the «model» is Mistral AI's weights, the same for every run, already
pinned by the group's version. Registering it would store a copy of the weights (837 MB, under their
licence) or a pointer to the group, and applying it «to new rows» is exactly running this node again on
the new table. The form hides «Register the model as»; the gateway refuses a `registerAs` with «a
pre-trained forecast registers no model: its run is logged to MLflow». A Predict node that runs
Timestral on rows from elsewhere would be this node under another name.

## Sample data

No new data. The sample flow, added by the seed only when the group is enabled (as the experiments'
and the decomposition's), **«Orders by channel: four weeks ahead»**: `calendar` (`ref.calendario`, to
2026-08-12) Cross join the five channels → Left join (orders joined to customers, Group by `data_ordine`,
`canale`, count of `id`) → Fill nulls 0 on the count for the days up to 2026-07-15 → Forecast (Timestral):
time `giorno`, value the count, series `canale`, frequency day, size 28M, known covariate `festivo`
(the 28 days after 15 July hold it and an empty count), holdout 28. Five series of 1,111 days, a weekly
and a yearly pattern (the decomposition spec, «Sample data»), Ferragosto inside the horizon. The first
page: five forecasts, the holdout's MASE against the same-weekday forecast, the comparison across five
series (a test with five series can reach p = 0.0625 at best two-sided: the reading says why it cannot
be «supported» with five; Open question 15).

## Self-test, reference values, witnesses

**Reference data, synthetic** (`reference/data/*.csv`, written by `make_reference.py` with fixed seeds,
small: the ConfigMap's ceiling):

| File | Rows | What it tests |
|---|---|---|
| `ff_daily_one.csv` | 400 | one daily series, weekly sine + trend + noise: the forecast, the holdout, MASE, coverage, the baseline |
| `ff_daily_three.csv` | 3 × 300 | three series of different lengths and ends: batches by patches, the notes, the comparison «not computed»/computed |
| `ff_covariates.csv` | 2 × 330 | a known 0/1 covariate with 30 future rows, a past number, a text covariate of 3 levels |
| `ff_gaps.csv` | 120 | 3 gaps: `refuse`, `linear`, `model` |
| `ff_types.csv` (+ Parquet twins written by the test) | 60 | a time of Int64 above 2³¹, Int32, date32, timestamp[us, UTC], timestamp[ns]; a series column of Int64 and of text |
| `ff_edge_*.csv` | 1–40 | a constant series (`degenerate` or a flat forecast: F3 records which), one value, a negative series (no clamping), column names that look like code |
| `ff_wilcoxon.csv` | 12 series × 60 | the test: exact and normal paths, a zero difference, ties |

**Reference values**: `make_reference.py` runs **outside Tabularia** with `uv` and the **official,
unmodified** package at `a534bdbd` (`witness.json`: `timestral` from the Git commit, `torch==2.14.1+cpu`
from the CPU index, `numpy`, `safetensors`, `huggingface_hub` at `uv.lock`'s 2.2.0, `scipy` 1.18.1,
`pyarrow` 26.0.1), the weights read from a local directory filled once from the pinned revisions and
checked by sha256 (never committed). It calls `TimestralForecaster.forecast` directly on the same arrays
the runner builds (written by hand there from the CSVs: a second construction, not the runner's code), for
each size, and writes the quantiles of the drawn cases (a few steps and levels each, compact, 13 digits:
the ConfigMap).

| Check | Where | Tolerance |
|---|---|---|
| The runner's quantiles against the official package called outside the image | self-test | relative 1e-4 of the series' scale (**assumed**: CPU kernels differ between the dev machine and a cluster node; F0 measures the gap and sets it) |
| The runner against the vendored package called directly in the image, same arrays | self-test | bit for bit |
| Two runs in one process | self-test | bit for bit |
| A series alone against inside a batch | self-test | bit for bit, or F0's measured gap («Determinism») |
| Quantiles sorted; the 0.5 equals `median`; symmetric averaging odd (`forecast(-x)` = −reflected) | self-test | exact (the package's own test, rewritten without pytest) |
| MASE, sMAPE, pinball, WQL, coverage, the seasonal naïve | witness (numpy by the formulas, written again) | 1e-12 |
| Wilcoxon's statistic and p-value | witness `scipy.stats.wilcoxon` | 1e-12 |
| The grid, gaps, types, refusals: the Python twin against `ts_series.R` on `dec_types.csv`, `dec_gaps.csv` and `ff_types.csv` | a test in the timeseries image (R) and this image (Python), comparing their outputs | exact |
| The sizes present: each `model.safetensors`' sha256 | the check Job | exact |
| Counts, texts, verdicts, reading ids, refusals | self-test | exact |

The self-test runs the cases of **the sizes the image holds** (`expected.json` keyed by size) and loads
one size at a time (memory). Expected time: about 10–40 s with the four sizes (loading dominates).

## Security

- **No user code**, the runner is Tabularia's; columns travel as data, renamed before any array is built.
- **Internet off** for the Jobs (the models' rule): the weights are in the image, `HF_HUB_OFFLINE=1`;
  the vendored loader never reaches the Hub from a local directory.
- **Safe weights format**: `safetensors` holds tensors only and runs no code when read (unlike a pickle);
  read only after the build's and the check Job's sha256 checks.
- **The supply chain**: every downloaded byte is pinned by sha256 (the wheel by URL, the weights, their
  `config.json`, the licence); the vendored code is pinned by commit and per-file hashes.
- **Builds** reach `download.pytorch.org` and `huggingface.co` (or the mirrors); the Jobs reach neither.
- **The licence acceptance** is an administrator's act, not available to API tokens, recorded in the
  audit (`models.licence_accept`) and noted in the version it authorised (its `packages` text); no new
  table.

## Boundaries

As in the models, A/B and decomposition specs, plus:

- **Always**: the weights only from the pinned revisions with their sha256; `LICENSE` beside them in the
  image; the acceptance before any build; verdicts only from the test against `alpha`; the time's and the
  series' types kept; every sentence from a template in five languages; the licence on the report's first
  and last page.
- **Ask first**: another size or revision of the weights; any package beyond the recipe; a second baseline;
  any test besides Wilcoxon's; changing the licence's wording in the panel.
- **Never**: the weights in Tabularia's images, its repository, its CI or its Docker Hub tags; a download at
  run time; a translation of the licence presented as the licence; a claim that a customer may or may not use
  the weights; simulation, bootstrap or sampled forecasts; `torch.compile`; a model card, a registration or a
  Predict entry; another model's or product's name in the documents; a migration for this group (decided
  2026-11-21).
- **Recommended before the first release that ships the group** (decided 2026-11-21, Open question 18): a
  lawyer's short reading of the panel's sentences, the documentation's licence paragraph and the report's
  licence line — in particular of «derivatives, modifications, or combined works … by a third party» and of
  the text's slip «(b)». The release checklist (`CHANGELOG.md`'s entry for that release and the release
  notes) carries it as an item to tick, and the release notes say: «The Timestral weights are under Mistral
  AI's Modified MIT License, which withholds every right from companies above $20 million of monthly
  revenue: read it before enabling the group».

## Success criteria

1. On kind, an administrator sees the licence box, cannot enable without ticking it, enables with two sizes;
   the acceptance is in the audit (`models.licence_accept`, with the licence's sha256 and the sizes) and in
   the version's `# tabularia:` lines, with the same audit id; the image builds with those two sizes only,
   checks their sha256 and self-tests. With a short `AUDIT__RETENTION_MINUTES` and the acceptance's row gone,
   the ready version keeps running and shows its acceptance line, and the next build asks again; a recipe
   change then leaves the group in `licence_pending`. No migration was added.
2. A new Tabularia with a changed `licence/LICENSE.txt` (a test build) shows «New licence to accept» and
   builds nothing until accepted.
3. The sample flow gives five forecasts with bands, the holdout's measures, the comparison's row, the
   readings in five languages and the licence on the first and last page.
4. The forecast reaches the next step with the columns the editor showed before the Run; `time` and
   `canale` keep their types.
5. The quantiles equal the official package's within F0's tolerance; the measures and the test equal their
   witnesses.
6. Timings measured on 1k, 10k and 100k points and on 100, 1,000 and 10,000 series for each size, with the
   loading time apart; a 10,000-series run with 209M on a short timeout refuses after its first batch with the
   measured speed.
7. Every other group's Dockerfile and recipe hash unchanged; `groups_test.go`'s 900 KiB holds.

## The lots F0–F9

Rules of the earlier lots: **F0 first, alone**; then lanes on disjoint files; **one heavy job at a time** on
this machine (16 GB): an image build, the engine suite, the gateway suite, a kind run. The image is built once,
in F0; later lots run the self-test with `cmd/modelgroup code foundation_forecast <dir>` mounted on it. No lot
changes `recipe.json` after F0. Agents: sonnet for the medium work, haiku for the simple, opus for the
difficult and the review.

**Where it fits with the lots now running.** The A/B's E9 (kind) and E10 (final review), and the
decomposition's D2–D9, are in progress. Shared files: the catalogue and its copies (`catalog.py`,
`catalog.json` ×4), `make_output_columns.py` and `output_columns.json`, `layouts/common.py` (`NOTE_NAMES`),
`modelCatalog.ts`, `ModelForm.vue`, `OpSidebar.vue`, `ai_flow_agent.py`, `docs/capabilities/models.md`,
`CHANGELOG.md`, `seed_tabularia.py`, `groups.go` and `groups_test.go`. So:

- **F0 starts after D8 and E10** (no lane edits the catalogue, `groups.go` or the editor after them), and its
  **image build runs in a window with no suite and no kind run** — never beside D9's suites. The build is a
  download of about 1.7 GB (minutes), then the measurements, which load 209M (about 2 GB): alone.
- **F1** (licence, **no migration**: decided 2026-11-21) touches `models_admin.go`, the build loop's
  reading of the sizes, `ModelsSection.vue` and the locales only.
- **F4** (engine suite) after D9's; **F5** after D5; **F7** after D6 and E9 (same docs and prompts); **F8**
  (kind) after D8's and E10's kind work.

```text
F0 contract, recipe machinery (wheels, weights, ADD --checksum), vendored package, image, measurements (first, alone; after D8 and E10)
 ├── F1 licence without a migration: audit models.licence_accept, the version's # tabularia: lines, licence_pending, panel box
 ├── F2 tab_series.py (the Python twin of ts_series.R) + the cross-language test
 │    └── F3 the runner: series, covariates, batches, forecasts, time budget, measures, Wilcoxon, readings, references ──┐
 ├── F4 report and explanation (layouts, five languages) — after D9's suite ──────────────────────────────────────────┤
 └── F5 the node in the editor (form, sizes, quantiles, columns before Run) — after D5 ─────────────────────────────────┤
                                                                                                                       ▼
                                      F6 MLflow tags, the refusal of registerAs, gateway checks ── F7 sample flow, docs, assistants ── F8 kind ── F9 final review
```

| Lot | Objective | Files (owned) | Depends on | Accepted when (small data) | Heavy? | Agent |
|---|---|---|---|---|---|---|
| **F0** Contract, machinery, image | Catalogue: group `foundation_forecast`, model `timestral` (`card: false`, kinds, `applies`, `OUTPUT_TYPES["quantile_forecast"]`, the `quantile_` wildcard, sections `series` and `series_curve`, `LIMITS` `max_series`, `max_horizon`, `max_covariates`, `max_series_charts`), fixtures, `params_cases.json`; `Recipe` with `wheels` and `weights`; the Dockerfile (`TARGETARCH`, `ADD --checksum`, mirrors; the sizes read from the version's `# tabularia:weights` line, the four when it has none); the `all:` embed; the vendored package, `NOTICE`, `vendor.json`; `run.py` and a stub refusing «not built yet»; **the image** with the four sizes: its time, size, lock; `ADD --checksum` and the Hub's redirect checked; **measurements**: import, loading per size, the table of «Performance» at 1k/10k/100k points and 100/1,000/10,000 series, threads 1/2/4, peak memory, batch invariance, the gap against the dev machine; `uv pip compile` of the closure | `catalog.py`, `catalog.json` and copies, the engine's catalogue tests, `fixtures/models/timestral_*.json`; `groups.go`, `groups_test.go`, `scriptimage.go` and its test, `models_build.go` (check script), `cmd/modelgroup`; `groups/foundation_forecast/**` (stub runner) | D8, E10 | `--check` green; `output_columns` names `quantile_0.05`… before a run; the image builds for x86_64; every other Dockerfile byte for byte; the stub's self-test passes; this spec's «As built» holds every measurement | **yes**: the build, the measurements | sonnet (machinery), **opus** (measurements and determinism) |
| **F1** Licence and enabling (no migration) | The enable, rebuild and sizes bodies, the 409s; `models.licence_accept` written in the transaction that queues the version; the `# tabularia:weights` and `# tabularia:licence` lines in `mgNewVersion`; the valid-acceptance lookup in `audit_logs` (action, target, `detail.licence_sha256`); `licence_pending` for automatic builds without one, and its step sentence; the warning when the retention will remove the acceptance; the panel's box (shown at every build an administrator queues), checkbox, sizes, the acceptance line read from the current version, five languages | `models_admin.go` and its test, `jobs_script_builds.go` (the sizes), `ModelsSection.vue`, `frontend/i18n/locales/*.gen.ts` (namespace `modelsAdmin`) | F0 | Chrome and Firefox, mouse and keyboard alone (Mac, a laptop without F/Home/End keys); Enable disabled until ticked; a stale sha256 refused; with the audit row deleted (a test with a short retention) the ready version still runs, its line still shows, an automatic rebuild waits in `licence_pending`; other groups' versions' `packages` unchanged; **no file under `gateway/app/db/migrations`** | no (gateway suite subset) | sonnet |
| **F2** Series in Python | `tab_series.py`: grid, `duplicate_time`, `irregular_time`, span, `missing` (refuse, linear, model), time arrays of the input's type, envelope thinning; the cross-language test against `ts_series.R` | `common/py/tab_series.py`, its tests, `groups/timeseries/reference/…` (read only), the comparison script | F0 | equal to `ts_series.R` on `dec_types`, `dec_gaps`, `ff_types`; the same sentences | no | **opus** |
| **F3** The runner | `ff_series.py`, `ff_timestral.py`, `ff_measures.py`, `ff_reading.py`: covariates and their codes, batches, two forecasts, the time budget, the measures, Wilcoxon, the rows, the reading ids; `make_reference.py`, `witness.json`, `expected.json` | `groups/foundation_forecast/runner/py/*`, `reference/*` | F2 | the table of «Self-test» green; timings at 1k/10k/100k and 100/1,000 series; `too_slow` measured | no (self-test in Docker, light) | **opus** |
| **F4** Report and explanation | `layouts/timestral.py`, `report_texts/timestral.json` (five languages, from one generator), `NOTE_NAMES`, golden PDFs | `layouts/timestral.py`, `layouts/__init__.py`, `layouts/common.py` (`NOTE_NAMES` only), `report_texts/timestral.json`, `test_models_layouts.py` | F0 (fixtures); F3 for real outputs; D9's suite done | every fixture in five languages passes `reports.definition.check`; no measure sentence holds a verdict word; the licence page verbatim; other PDFs unchanged | **yes**: engine suite | sonnet (layout), **opus** (texts) |
| **F5** Editor | palette entry, the form (sizes from the palette, quantiles as a multi-choice of the 21, covariates, the licence line), the wildcard in `modelCatalog.ts`, «Register» hidden | `OpSidebar.vue`, `useOpIcons.ts`, `ModelForm.vue`, `modelCatalog.ts`, locales, `frontend/tests/model-*.test.mjs` | F0, D5 | Chrome and Firefox, keyboard alone; columns after the node shown before Run | no | sonnet |
| **F6** Gateway checks and MLflow | `size_absent` before the Job; `registerAs` refused; the weights' tags; env (`HF_HUB_OFFLINE`, threads) | `jobs_models.go`, `jobs_models_mlflow.go`, their tests | F3 | a run on a size not built refused before the Job; tags present | no | sonnet |
| **F7** Sample flow, docs, assistants | the seed's flow (with the group enabled only); `docs/capabilities/models.md` «Pre-trained forecasting (Timestral)» with the licence first; `audit-log.md`, `kubernetes.md` (hosts, mirrors, image size), `configuration.md`, `CHANGELOG.md`; the gateway's third-party notices for the vendored files; flow assistant (prompt: when to choose Timestral or ARIMA, the licence sentence), `aiprompts`, MCP tools | `seed_tabularia.py`, docs, `ai_flow_agent.py`, `internal/aiprompts/`, `internal/mcp/`, `scripts/third-party-notices.mjs` | F3–F6, D6, E9 | `small` only; the flow runs; the docs quote the licence's clause with its sha256 | light | haiku (docs), sonnet (seed, prompts) |
| **F8** kind | enable on kind with the licence, two sizes, build, self-test, the sample flow, PDF and Excel in two languages, MLflow tags, the timings at 100k points and 1,000 series, the image's pull time on a fresh node | — (this spec's «As built») | F7, D8, E10 | success criteria 1–7 | **yes**: gateway suite, kind | sonnet |
| **F9** Final review | this spec against the code; the licence's quote against the Hub's file; the formulas against the witnesses; the readings against the verdict/measure rule in five languages | — | F8 | corrections committed | the suites | opus |

**In parallel**: after F0, F1 ∥ F2 ∥ F4 (fixtures, when the engine suite is free) ∥ F5; after F2, F3; F6 after
F3. **In sequence**: F0 alone; F7 after F3–F6; F8 after everything; F9 last.

## Open questions for the owner

Every question was answered by the owner on 2026-11-21: every recommendation approved, except question 16
(no migration). The options stay for the record.

1. **The acceptance's form.** **Decided 2026-11-21: a required checkbox and a record** — the box with the clause
   quoted and the link, the checkbox required, the acceptance recorded in the audit (`models.licence_accept`)
   and noted in each version it authorises (question 16). *Options were:* (a) that, with a table; (b) only a
   notice; (c) (a) plus the company's name typed.
2. **The checkbox's words.** **Decided 2026-11-21: the recommended words**, «I have read the licence of the
   Timestral weights, and the organisation that uses this installation may use them under it, or holds a
   separate licence from Mistral AI.» **No question on revenue.** *Options were:* «I have read» only; those
   words plus a revenue band.
3. **Which sizes are downloaded.** **Decided 2026-11-21: the administrator ticks them, all four ticked by
   default.** *Options were:* all four always; 5M and 28M by default.
4. **The node's default size.** **Decided 2026-11-21: 28M.** *Options were:* 5M, 123M, 209M.
5. **A new licence on upgrade.** **Decided 2026-11-21: the group stops (`licence_pending`) until an
   administrator accepts**, consistent with «Rebuild needed». *The other option was* the version in use
   serving on under its old licence.
6. **Gaps.** **Decided 2026-11-21: `refuse` by default, `linear` and `model` as options.** *Options were:*
   `model` by default; also `seasonal` (an MSTL in Python).
7. **The comparison's test.** **Decided 2026-11-21: Wilcoxon's signed-rank across series, none with one
   series.** *Options were:* also Diebold–Mariano on rolling one-step forecasts (the cost times the holdout);
   no test.
8. **The baseline.** **Decided 2026-11-21: the seasonal naïve only.** *The other option was* ARIMA by
   `statsmodels` in the image.
9. **Several value columns forecast jointly.** **Decided 2026-11-21: not in this release.**
10. **How much a covariate moved the forecast.** **Decided 2026-11-21: not now**; later as an option, off by
    default.
11. **The inference code.** **Decided 2026-11-21: vendored at the pinned commit with the one-line lazy import,
    `NOTICE` saying so.** *Options were:* vendored unchanged with `huggingface_hub` installed; built from GitHub.
12. **Where the weights live.** **Decided 2026-11-21: in the group's image.** *Options were:* a shared volume;
    the scripts bucket.
13. **The longest horizon.** **Decided 2026-11-21: 1,024 steps, one forward pass.** *The other option was* up
    to 4,096 by blocks.
14. **The library's defaults.** **Decided 2026-11-21: on, shown as options.**
15. **Sample data.** **Decided 2026-11-21: the existing orders by channel with `festivo` as a known
    covariate**, added when the group is enabled; the reading explains why five series cannot reach α = 0.05.
16. **Where the acceptance is kept.** **Decided 2026-11-21 (owner): no migration, no table
    `model_group_licences`.** The schema has no field for it (`model_groups` holds no settings JSON), so the
    acceptance is the audit entry `models.licence_accept`, which decides whether a build may start; each
    version notes the acceptance it was built under in comment lines of its `packages` text, which the build
    already ignores; with an audit retention set, once the row is gone the version in use runs on and the
    next build or enabling asks again («Where the acceptance is kept, without a migration»). *The option
    declined was* migration 0046 with a permanent table of acceptances.
17. **Hugging Face as the source of the weights.** **Decided 2026-11-21: accepted for the build only**, with
    `MODELS__WEIGHTS_BASE_URL` for a mirror. *The other option was* requiring a mirror.
18. **A lawyer's reading.** **Decided 2026-11-21: recommended before the first release that ships the group**,
    written in «Boundaries» and carried by that release's notes and checklist.

## As built

*(Filled by the lots: deviations, the image's size and build time, the timings on 1,000, 10,000 and 100,000
points and on 100, 1,000 and 10,000 series per size with the loading apart, the peak memory, the determinism
measurements, the self-test's count, the ConfigMap's size.)*

### F0 — contract, machinery, image

**Built (2026-11-22).** The catalogue (the group `foundation_forecast`, the node `timestral`, `card: false`, the kinds, the rules, the
section `series` and `series_curve`, the output `quantile_forecast`, the wildcard `quantile_`, the limits) with its snapshot and three copies,
a results fixture (`timestral-default.json`: three imaginary series, 702 rows, the comparison's p-value computed from the sign patterns) and 25
shared parameter cases the engine and the Go gateway answer word for word; the recipe (`wheels`, `weights`), the Dockerfile machinery
(`scriptimage.Settings.Wheels` and `.Files`, `WeightsSizes`, `modelgroups.Build`/`BuildInputs`/`DockerfileFor`, `cmd/modelgroup dockerfile
foundation_forecast --sizes …`, the gateway's `dockerfile()` reading the version's `# tabularia:weights` line and the two mirror variables), the
check script (`mgWeightsCheck`), the vendored package (`runner/py/timestral/`, `NOTICE`, `LICENSE`, `vendor.json`), `run.py` and a stub that refuses
«… is not built yet», the group's `licence/LICENSE.txt` (sha256 `648194…3fc8c`), the image and the measurements below. The palette leaves the node out
(`MODEL_NOT_IN_THE_PALETTE_YET`, lot F5 takes it away) and the group's name and description are in the five languages.

**Where the spec was wrong, or silent, and what was done** (nothing else of the spec was changed):

1. **pandas is in the image.** «Not installed: … `pandas`» cannot hold: the shared library `common/py` imports pandas at module level
   (`tab_columns.py`, read by `tab_selftest.py`, read by `tab_main.py`), so a runner without it does not start, and the library's own self-test
   (reading a CSV, the column kinds) uses it. The recipe gains `pandas==3.1.1`, `python-dateutil==4.0.0` and `tzdata==2026.6` (the closure
   `uv pip compile` gives; the boosting recipe's pins; `six` is not needed). Cost, measured: `import pandas` 0.5 s and 49 MB of resident memory
   (`import torch` 2.5 s, 244 MB; with pyarrow and pandas 3.0 s, 321 MB), 83 MB on disk. The way back, if the owner prefers the lighter image: make
   the library's pandas import lazy and let the group's self-test skip the tests that need it (a change to the shared library, and `Passed()` forbids a
   skipped check today); F3 decides with the runner.
2. **`LIMITS["max_horizon"]` already exists** (1,000: ARIMA's and the lifetime nodes') and stays. The node's own limit is `max_foundation_horizon` (1,024),
   which the kinds `foundation_horizon` and `foundation_holdout` read. The other three limits are as named (`max_series`, `max_covariates`,
   `max_series_charts`).
3. **The output's value is `quantile_forecast`, not `forecast`.** `OUTPUT_TYPES` is keyed by the value of the node's `output` (`components`, `variants`,
   `plan`), and `forecast` is ARIMA's; so the choice is `quantile_forecast` | `results`, the default `quantile_forecast`, and `history` applies with it.
   The form's label for it is lot F5's.
4. **`model|||n` is required of every model** (`results.py`): the model section declares `n` (the points) besides the spec's `n_points`.
5. **What a list of numbers cannot say in the catalogue** stays with the runner: that a quantile is one of the 21 levels (`quantile_level`), and that the two
   covariate lists hold 16 columns together (each list is bounded at 16 by the kind). The catalogue refuses a number outside 0 and 1, a number given twice and more than 20 of them by its `numbers` control; 0.5 and 0.3 pass it (the
   runner refuses 0.3, and drops 0.5, the median being `forecast`).
6. **Section declarations are a superset**: a variant declares what the runner MAY write (there is no `when` on a section); `series_curve` has no declared
   levels (a level is a series' label) and `holdout`'s default is `None`, read as «the horizon» (with known covariates, the future rows' count) by the runner.
7. **`season` is 1 to `max_period` (10,000)**, a kind of its own (`season_steps`): ARIMA's kind stops at `max_season` (60).
8. **The fixture's name is `timestral-default.json`** (`<model>-<variant>.json`, as the tests glob), not `timestral_*.json`. `output_columns.json` has no case for the
   node until its runner exists (`NOT_RUN_YET`); the columns before a run are tested in `test_models_catalog.py`, in `model-catalog.test.mjs` and in the assistant's test.
9. **`ADD --chmod=0644` also gives the folders it creates the mode 0644** (no `x`: user 10001 could not enter them; found by running the check, not by the build). The
   Dockerfile makes the folders first (`RUN mkdir -p /opt/tabularia/weights/<size> …`), then adds the files. Symbolic modes (`a+rX`) also work with BuildKit here, but the
   builder in the cluster is another version; plain octal needs no frontend feature.
10. **`filelock`**: the resolver now answers 4.3.0; the recipe keeps the 4.2.12 of the spec (its licence was read; `pip check` passes).
11. **A constant, padded batch is what makes a series' forecast independent of the others** (the measurements below): the spec's assumption «a series alone and in a
    batch of 16 give the same bits» is false, and «a batch holds only series with the same number of patches» is not enough.

**The image** (`tabularia-models-foundation_forecast:dev`, `go run ./cmd/modelgroup dockerfile foundation_forecast`, the four sizes, x86_64): built **from scratch
(`--no-cache`, base image pulled) in 69 seconds** (a first build, 72 s), about 1.7 GB downloaded from `download.pytorch.org`, `pypi.org` and `huggingface.co`; **2.79 GB**
(layers: Python 0.12, torch 0.78, the other packages 0.43, weights 0.02 + 0.11 + 0.49 + 0.84 with their `config.json` and `LICENSE`). The lock is 17 lines: filelock 4.2.12,
fsspec 2026.9.0, Jinja2 3.1.6, MarkupSafe 3.0.4, mpmath 1.3.0, networkx 3.7, numpy 2.5.5, pandas 3.1.1, pip 26.2.2, pyarrow 26.0.1, python-dateutil 4.0.0, safetensors 0.8.0, setuptools
84.2.1, sympy 1.14.0, torch 2.14.1+cpu, typing_extensions 4.16.0, tzdata 2026.6; `pip check` passes in the build. **`ADD --checksum` works with the Hub's redirect** to its storage in
BuildKit's default Dockerfile frontend without a `# syntax=` line (checked with the engine's BuildKit and its `docker build`; the in-cluster builder, `moby/buildkit:v0.33.1-rootless`, is
F8's to run), and a wrong checksum fails the build («digest mismatch»); pip's `#sha256=` on the wheel's address fails on a wrong hash too. The check script (the lock, then each file of each
size's sha256 against the recipe's, one `weights-<size>@<sha256>` line a size in the lock, then the self-test) takes **12 s** on 2 CPUs; the stub's self-test passes with **14 checks, none
failed or skipped** (the library's own; the reference cases are lot F3's); the job's ConfigMap is 296 KB (the limit 900 KiB). The other groups' Dockerfiles and recipe hashes are unchanged
(tests: every group but this one has the Dockerfile `scriptimage` makes of its recipe, no `ARG`, no `ADD`; the R image's sha256 is the old one).

**Measurements** (this machine: Intel Core i5-8350U, 4 cores / 8 threads, AVX2 (`torch.backends.cpu.get_cpu_capability()`), 16 GB, kind clusters idle beside; the image on Docker with
`--cpus 2 --memory 4g --memory-swap 4g --read-only`, `torch.set_num_threads(2)`, PyTorch 2.14.1+cpu, **float32, no `torch.compile`, symmetric averaging and clamping on (the library's defaults), the
page cache warm** (a cold first load is slower by the disk's time to read the file; not measured). Context = the series' points, horizon 28, **one forecast** (double it with a holdout), series
generated and forecast in batches of 16 and written by blocks (nothing kept but a checksum), so the resident memory is the model's plus one batch's. Seconds of the forecast alone, the load apart.)

| Part | 5M | 28M | 123M | 209M |
|---|---|---|---|---|
| `import torch` + numpy | 2.2–2.5 s | 2.2–2.5 s | 2.2–2.5 s | 2.2–2.5 s |
| `import pandas` / `pyarrow` / `timestral` | 0.41 / 0.03 / 0.04 s | same | same | same |
| Loading the weights (warm cache) | 0.07 s | 0.31 s | 1.1 s | 1.8 s |
| 1 series, 1,000 points | 0.09 s | 0.08 s | 0.23 s | 0.36 s |
| 1 series, 10,000 points | 0.18 s | 0.39 s | 1.3 s | 2.2 s |
| 1 series, 100,000 points (the model reads 32,768) | 0.47 s | 1.4 s | 4.9 s | 7.7 s |
| 100 series × 1,000 points (100,000 points) | 0.95 s | 3.5 s | 12.6 s | 21.0 s |
| 1,000 series × 100 points (100,000 points) | 2.1 s | 6.5 s | 24.6 s* | 41.2 s* |
| 10,000 series × 100 points (1,000,000 points) | 20.4 s | 68 s* | 250 s* | 415 s* |
| Peak resident memory of the process (`VmHWM`: the weights twice while loading) | 293 MB | 466 MB | 1,190 MB | 1,851 MB |
| Peak charged to the container (`memory.peak`) | 181–217 MB | 267–306 MB | 634–693 MB | 962–1,256 MB |

\* extrapolated, linearly, from the first 320 (123M and 209M, 1,000 series; 28M's 1,000 are measured) or 640 series (28M, 123M and 209M, 10,000 series) of the table, at the speed measured
for them (every batch had the same cost); 5M's 10,000 series and the 1,000 of 28M are measured whole. The table's first row of each size includes the thread pool's warm-up (about 0.05 s: 28M's
single series at 1,000 points is not faster than 5M's). The estimates of «Performance and limits» were pessimistic by a factor of 2 to 3.5 (the paper's compiled bfloat16 numbers, halved):
the unit cost of a series of 100 points is **2 ms (5M), 6.5 ms (28M), 25 ms (123M), 41 ms (209M)**, of 1,000 points 9.5, 35, 126 and 210 ms. **A 10,000-series run with 209M fits a 15-minute step
(about 7 minutes); the time budget's refusal, `too_slow`, has to be measured on a shorter timeout (F3).**

Threads (100 series × 1,000 points, batch 16, the container's `--cpus` = the threads; seconds; the memory is the same):

| Threads | 5M | 28M | 123M | 209M |
|---|---|---|---|---|
| 1 | 1.39 | 5.40 | 22.0 | 35.7 |
| 2 | 1.07 | 3.51 | 12.5 | 20.4 |
| 4 | 0.86 | 2.69 | 9.25 | 15.0 |

Two threads cost 0.57–0.77 of one, four 0.42–0.62: the second CPU is worth taking, the third and fourth much less.

**The memory of a batch** (the container's peak against a batch of one, per series and per patch of 32 steps of context): about **22 KB (5M) to 70 KB (209M)**, i.e. about **100 × `d_model` bytes**
(192, 384, 640, 768): 64 series of 1,000 points add 37 MB with 5M and 145 MB with 209M; three more series of 32,768 points add 67 MB with 5M and 215 MB with 209M. The runner's rule is `batch_rows` ≤ ¼ of the memory limit ÷ (patches of the context × 100 ×
`d_model`), at most 16 (4 GiB: 13 series of the longest context with 209M, 16 of every shorter one).

**Determinism** (every size; 16 series of 400 points, 28 steps; the quantiles compared as float32):

| Comparison | Result |
|---|---|
| The same input twice in one process | bit for bit equal, all sizes |
| The same input, 1, 2 and 4 threads | **not always equal**: the largest gap is 1.5 × 10⁻⁵ (1.2 × 10⁻⁷ of the largest quantile, one float32 unit in the last place); some pairs are equal |
| A series alone against the same series inside a batch of 16 | **not equal**: the same gap, 1.2 × 10⁻⁷ relative; two batches of 8 against one of 16 the same; the first 4 of 16 against a batch of 4 equal for 28M, 123M and 209M, not for 5M |
| The order of the series inside a batch (reversed) | bit for bit equal, all sizes |
| A short batch **padded with copies to the constant size 16**, its rows against the same rows of the full batch (the padding after them, or before them) | **bit for bit equal**, all sizes and threads |

So the library's kernels choose their blocking by the batch's shape, and nothing else of a batch changes a row: **F3 runs every forecast in batches of one constant size (16, or the smaller constant the memory rule gives for a long context) and pads the last one with copies**, and a
series' bits then depend on nothing but the input, the group version, the thread count and the CPU family. The self-test's «a series alone against inside a batch» check compares the padded one, bit for bit, and the
comparison with the values computed outside the image has a **tolerance of 1e-6 of the largest quantile of the series** (the spec's first estimate was 1e-4; the gap between thread counts and batch shapes measured is 1.2e-7, one float32 unit in the last place, so ten times that is enough). The gap between CPU
families (AVX2 against AVX-512 or NEON, the dev machine against the image on kind) could not be measured here: the machine has one family (kind runs on the same CPU), so the report writes the family
(`cpu_capability`) and F3's tolerance holds until another family shows more.

### F1 — licence and enabling (no migration)

**Built (2026-11-22).** `models_licence.go` (new, beside `models_admin.go`), `audit.RecordID`, `Group.LicenceText`, the panel's box and the five languages; no file under
`gateway/app/db/migrations`.

- **The click's body** (Enable and Build again; a change of sizes is a Build again with the new sizes, there is no route of its own): `{licence_sha256, accept, sizes}`.
  Checked in the order of the spec: `accept` missing or false, 409 «enabling this group requires accepting the licence of its weights»; a `licence_sha256` other than the
  shipped one, 409 «the licence shown is not the one this Tabularia ships: reload the page»; sizes empty, unknown or repeated, 422. A refusal rolls back the whole
  transaction (an Enable that was making the environment leaves nothing behind). **Enable asks for the acceptance only when it would queue a version**: a group disabled
  with a ready version on the shipped recipe is enabled again with no body (nothing is built, nothing to accept), and the panel shows no box for it. A group without weights
  ignores the body, as before.
- **`models.licence_accept`** is written by `audit.RecordID` in the transaction that queues the version (the audit's insert now returns its id; `Record` is unchanged for
  everyone else), with `licence_name`, `licence_sha256`, `sizes` (in the recipe's order) and `recipe_hash`; the version's `# tabularia:licence` line names the same row.
  `mgNewVersion` takes the acceptance and refuses a group with weights without one; the other groups' `packages` are the recipe's text, byte for byte (a test).
- **A valid acceptance** is the newest `models.licence_accept` row of the group, success, whose `detail.licence_sha256` is the shipped one (a `LIKE` on the hash narrows the
  rows, the JSON decides). `mgLoad` reads it for a group with weights; `state()` answers **`licence_pending`** where it would answer `rebuild_needed` and there is none.
  `mgQueueStale` (the loop's automatic build) queues nothing without one, logs it, and with one copies it (actor, time, row) with the sizes of the version in use (the current
  version's line, else the newest's, else every size). `ReadyModelGroupImage` answers the reason `licence_pending` and «the model group "foundation_forecast" waits for an
  administrator to accept the licence of its weights» (one sentence; the spec's «new licence» variant is not told apart from the plain one: when the audit holds a row of an
  earlier text, the group is as pending as with none).
- **`GET /admin/models`** answers `licence` for a group with weights (name, sha256, the link at the pinned revision of the smallest size, the shipped text, the sizes with
  their bytes, `pending`, `line` read from the version in use or else the newest that has one, `audit_expires_at` = the valid row's time plus `AUDIT__RETENTION_MINUTES`, null
  when the audit is kept forever); the palette entry has `sizes` (the version in use's line, else all) and `licence_pending`. `jobs_script_builds.go` needed no change: F0's
  `dockerfile()` already reads the sizes.
- **The panel**: the box (introduction, point 2 cut from the shipped text around its threshold, never retyped, the not-legal-advice line, the link, the shipped text in a
  disclosure, the sizes all ticked, the tick) is shown by Enable of a group with nothing ready to serve and by **Build again**, which opens it, moves the focus to the tick,
  and builds from the box's own button (Cancel gives the focus back to Build again). The tick is never kept. The line of the version in use is shown whenever there is one, with
  «read it» and a warning when its licence text is not the shipped one; the retention warning under it when `audit_expires_at` is set; `licence_pending` has a label, an icon and
  a hint. Observers read the line and see no box.

**Tests.** Gateway: 5 new tests (the lines written and read back, the build's parser unmoved, the other groups' text unchanged, the state with and without the row, the 409/422 table,
nothing left by a refusal, the version's line equal to the audit row, a rebuild with other sizes, the row deleted: the version serves, its line shows, the loop queues nothing and the step
says why, then an administrator accepts and the next recipe is queued by itself on that row with the sizes of the version in use, a row of another licence is no acceptance). Frontend: 4 new
tests (the draft, the clause cut from the real text, the card's wiring, the composable). Retention is tested by deleting the row, which is what the retention job does; its own job is the
audit retention's and is tested there. Browsers: Chromium and Firefox headless on the real component with a stand-in for the API, mouse and keyboard alone (Tab, Shift+Tab, Space, Enter): 98
checks, in five languages, at 390 px and in the dark theme.

### F2 — the series in Python

**Built (2026-11-22).** `common/py/tab_series.py` (the twin of `ts_series.R`), its self-test `common/py/tab_series_tests.py` (registered by `tab_selftest`
with the library's own tests, so every Python group's image checks the module it ships), `reference/data/ff_types.csv` and its generator
`reference/make_reference_series.py`, and the comparison with R: `reference/compare_series.py` (the cases, the Python side, the comparison), `compare_series.R`
(the R side) and `compare_series.sh`, which runs them in the two images with the files mounted (`sh gateway-go/internal/modelgroups/groups/foundation_forecast/reference/compare_series.sh`
from the repository's root; no image is rebuilt). The module needs numpy and pyarrow, never pandas.

**What F3 calls** (each a function of the module; the docstrings say the rest):

```python
table = tab_series.read_table(task, [time, value, series, *covariates])   # the schema first: no_column before any data
cols = tab_series.columns(table, time, value, frequency)                  # once per table: time_type, frequency_hour, not_a_number, infinite
labels, rows, no_series = tab_series.split(table.column(series))         # text order, the engine's labels; rows: views of one argsort
s = tab_series.series(cols, rows[k], missing, label=labels[k], max_periods=…)   # no_values, duplicate_time, irregular_time, too_many_periods, gaps
s.z, s.y, s.filled, s.n_missing, s.n_before, s.n_after, s.n_filled, s.n_model_missing
s.after_rows, s.after_pos          # the rows after the last observed value and their places after it (1 = the next): the known covariates' future,
                                   # complete on the grid when after_pos is 1…m (otherwise known_future_gap at the first missing place)
s.span_rows                        # the input row of each place of the span (−1 where none): the covariates' values on the span
s.time_array(); s.time_array(s.after(np.arange(1, h + 1)))   # the span's and the future's times, in the input's own Arrow type
tab_series.envelope(y, max_points)  # the places to draw (0-based)
```

**Where the spec was silent, and what was done:**

1. **The sentences are `ts_series.R`'s, word for word, with two parameters**: the `gaps` sentence's way out names the node's own other choices (`fills`; Timestral's
   «missing = linear or model», the decomposition's «linear or seasonal» in the comparison), and with a series column the series is named — «the series «Online» has 3 periods
   without a value …», and « in the series «Online»» after the time in `duplicate_time`, `irregular_time`, `too_many_periods` and `no_values`, whose params gain `series`. The
   spec's example «3 days» is R's «3 periods».
2. **The grid is decided in R's doubles**: seconds as a double, days = seconds / 86400, an hour's period = days × 24, a week's days / 7, the months of a datetime in UTC, the same
   tolerance of 1e-9 of a period; so a time R calls off the grid is off the grid here. Times themselves are held as integer «ticks» (an integer's value, a date's days, a timestamp's
   units), so the times written back are exact (an Int64 above 2⁵³ keeps every digit; the grid of such a time is R's double, as R's). **The linear fill is `stats::approx`'s formula**,
   `y[i] + (y[j] − y[i]) · ((v − x[i]) / (x[j] − x[i]))`, not `numpy.interp`'s order of operations: bit for bit equal to R's.
3. **`missing = model`**: the places stay NaN and `z` *is* `y` (no copy), `filled` is false everywhere, the count is `n_model_missing`; `n_filled` counts the linear fill only.
4. **`max_periods`** defaults to `ts_series.R`'s 200,000 places a series; F3 passes its own when a series may be longer (the group's `max_rows` is 1,000,000).
5. **Not ported** (Timestral uses none of them): the Box–Cox and log transforms, the seasonal fill (an MSTL), `ts_positions`.
6. **`ff_types.csv`** is written by `make_reference_series.py` (by formula, no random numbers), not by F3's `make_reference.py`: 60 rows, three series (Online
   3000000001, Negozio 3000000002, Telefono 3000000003) in an order that is not the time's, a value empty before the span, two gaps (an empty value and a missing row), three future rows,
   the hours across Rome's change of the clock of 2026-03-29. The comparison writes its time as Int64 (above 2³¹), Int32, date32, timestamp[us, UTC], timestamp[ns] and timestamp[ms, Europe/Rome].
7. **pandas stays in the image.** The library's import of pandas is now lazy (`tab_columns.py`: inside the functions that take or make a frame; the boosting group's
   self-test is green, 56 checks), but **pyarrow imports pandas by itself whenever it is installed** — on `pa.array` of a numpy array, on a Parquet read — so the lazy import alone
   saves nothing: measured in the image, a 1,000-row read and series costs **1.36 s and 136 MB with pandas installed, 1.02 s and 102 MB with pandas hidden** (one CPU; with
   2.5 GB). The library and the series work without it (a self-test check hides pandas and reads, splits, fills and writes a series). Removing it from the recipe is F3's to decide
   with the runner: the library's own self-test (`read_csv` and `read_input`, which make frames, and the checks that use them) and `reference_case` (a CSV read into a frame) need pandas, and
   `Passed()` forbids a skipped check, so they would have to be written with pyarrow first.
8. **R's one difference: an Int64 time's label.** `tab_label` (`common/R/columns.R`) writes bit64's `integer64` — which `ts_read` asks arrow for — as a double: the decomposition
   says «(the first: 5000000029.0)» where the engine, ARIMA (which reads a small Int64 as an R integer) and `tab_series` say «5000000029». The Python twin keeps the engine's label;
   the comparison lists the four cases as *known*, separately, and counts any other difference. The fix belongs to R's library (`inherits(x, "integer64")` →
   `as.character(x)` in `tab_label`), which every R group ships: not done in this lot.

**The comparison** (`compare_series.sh`; R 4.6.0 and arrow 26.0.1 in `tabularia-models-timeseries:dev`, Python 3.14 in `tabularia-models-foundation_forecast:dev`; each step one container, one CPU):
**100 cases** — `dec_types.csv` as eight Arrow types (Int64, Int32, Int16, date32, timestamp[us, UTC], timestamp[ns], timestamp[ms, Europe/Rome], double) with linear
and model, four of them with refuse too; `dec_gaps.csv` with refuse, linear and model; 14 refusal cases (the self-test's of `ts_series.R` and more: a boolean or fractional time, a day of datetimes off the
hour, two instants in one month, an Int64 twice, a column missing, an infinite value, a grid too long); empty times; the calendar (the end of a month, a datetime at a month's end in
Rome, quarters and years from mid-period dates, weeks of seconds, an index of doubles and of dates, hours across the change of the clock); `ff_types.csv` read as one table and split
(Python) against each series alone (R), six types × three series × refuse, linear and model — and **7 envelopes** (the self-test's walk at 100, 401 and 3 points, with empty values,
a spike in 100,000 points, ties at 50 and 51). Compared exactly: the counts, the codes, the sentences and their params, the span's times as Arrow arrays (type and values), `y`, `z`
and the chart's `x` bit for bit, `filled`; a `model` case against R's linear case of the same input. **69 series with 2,842 places (27 of them `model`), 31 refusals, 0
differences, 4 known** (point 8).

**The self-test**: six checks (the types, the gaps, the refusals word for word, many series, the envelope, the library without pandas), every table built in the check with its
answer written by hand; **foundation_forecast 20 checks, boosting 56, none failed or skipped**.

**Memory and time** (the image, `--memory 2500m --cpus 1`, `VmHWM`; one daily series, 1 % of the values empty and 1 % of the rows missing, the rows shuffled; read, columns,
split, series with the linear fill, its time array and its envelope; the peak above the same run on 10 rows, i.e. what the data costs):

| Points | Series | Peak above 10 rows | of which the Parquet read | the series (all of them) | Time (pandas installed / hidden) |
|---|---|---|---|---|---|
| 100,000 | 1 | +41 MB | +24 MB | +9 MB | 0.56 s / 0.22 s |
| 200,000 | 1 | +47 MB | +22 MB | +19 MB | 0.65 s / 0.30 s |
| 200,000 | 100 | +29 MB | +20 MB | +0 MB (one series at a time) | 0.59 s / 0.22 s |

The 10-row run itself peaks at 137 MB (67 MB of Python, numpy and pyarrow, 31 MB of pyarrow's Parquet machinery, 34 MB of pandas, which pyarrow imports). A series costs
about 95 bytes a place at its peak (its own arrays — times, y, z, filled, span_rows: 33 bytes a place — and the argsort and the grid's arrays, freed as it goes); the table is held once,
as Arrow columns, with one numpy array of ticks and one of values beside it.

### F5 — the node in the editor

**Built (2026-11-22).** The form of Forecast (Timestral) is drawn from the catalogue by `ModelForm.vue`, with four additions that name a kind, not the model:

- **Sizes.** `ModelForm` takes `sizes` (the sizes the group's ready version holds) and offers only those; the size the node already has stays, ticked, with the sentence
  «… was not downloaded by this installation». No `sizes` (a route that does not say): the four. `ModelNodePanel` reads them from `/models/groups` (`groups[].sizes`, which F1 answers).
- **Quantiles.** The kind `foundation_quantiles` is drawn as a box for each of the 21 levels, in a labelled group (the levels as the language writes them, comma included); the median
  is shown grey and never ticked («the forecast column itself»). The boxes show what the node asks for: with none given, the default four. Unticking the last one gives the default
  back, and the box is set by hand after the change (Vue sees «ticked» before and after and would leave the unticked box as it is: found in the browser).
- **Covariates.** The two lists are the existing boxes of columns (a column of one is not offered to the other, nor the time, the series' value or the series column); a note says when
  the two together pass `max_covariates` (the catalogue bounds each alone). Known covariates take the horizon away.
- **Licence.** One sentence under the size, with a link that opens apart (`noopener noreferrer`); the node's own words (`models.timestral.licence`). The name and the address are the route's,
  else the group's shipped ones (`MODEL_GROUP_LICENCES`); only an https address becomes a link.

«Register» is not drawn: the node has `card: false`, so the MLflow block says there is no model to register. The icon is a lucide `Telescope`, the summary «28M · orders · ~ shop». The
columns after the node (`quantile_0.05`…) were already known before the Run through the wildcard of F0: now tested down to the steps after it. The words are in the five languages under
`models` (own words for the keys the other models read differently: horizon, quantiles, holdout, missing, output).

**Deviations.** (1) The palette entry stays hidden (`MODEL_NOT_IN_THE_PALETTE_YET`) until the runner (F3): F7 takes it away. It sits under its group's header, «Pre-trained forecasting», as the spec says, not
under «Time series». (2) `/models/groups` answers `sizes` but no licence, so the line's name and link come from the shipped constant until a route says them; the link points at the licence file of the
28M repository on the Hub, whose text is identical in the four. (3) The catalogue still says `numbers` for the quantiles; the 21 levels are the editor's constant, and the runner refuses any other (`quantile_level`).

**Tests.** `frontend/tests/model-foundation.test.mjs` (14): keys, sizes, quantiles, covariates, licence, no registration, keyboard, columns, palette, icon, summary, words in five languages;
the word and palette tests of `model-catalog` and `model-experiments` no longer leave the node out. Frontend suite 589 of 589; typecheck with no error. Chromium and Firefox headless on the real
`ModelForm.vue` with made-up data, keyboard alone (Tab, Shift+Tab, Space, Enter, the arrows; no F, Home, End or Menu): the selects, the size, the licence link, the levels (the median skipped by Tab), the
covariates, MLflow, the sizes offered, five languages.

### F4 — the report and its explanation

**Built (2026-11-22).** `layouts/timestral.py` (registered in `layouts/__init__.py`), its words `report_texts/timestral.json` in the five languages
(written by one generator, not committed: the same keys in each language, every sentence that names a unit of time expanded by `frequency`), `NOTE_NAMES`
gains `series` and `size`. The runner writes the explanation (`ff_reading.py`, lot F3), as the A/B's and the decomposition's do: the model registers
nothing in `READINGS`, the layout places the rows with `common.reading_under` and `common.how_computed`.

The report, in the order of «The report (pixel perfect)»: (1) the summary — four cards (series and points; the model and its CPU; the horizon in the
frequency's words and the holdout; the holdout's MASE beside the seasonal naïve's, no colour), the formula line in the page's words, the licence line, the
summary's reading, the check (Wilcoxon across the series, the only verdict, in its own words: «– not supported» muted, «✓ Timestral ahead» or «✓ seasonal
naïve ahead» by the sign of the median difference), «Measures, not tests» (relative MASE, coverage 80 % and 90 %) and the mean errors over the series
(MASE, the naïve's MASE, sMAPE, the naïve's sMAPE, WQL, the series measured), each with what it is, then the readings of the holdout and of the comparison;
(2) the forecasts — one chart a series drawn, full width, two a page: the 90 % and the 80 % bands, the history, the seasonal naïve, the median, a dashed line
at the first instant forecast, the series' reading under it; (3) the holdout (not drawn with `holdout = 0`) — two charts a row (the observed values, the
holdout's median and bands, a dashed line at the first value kept out), the table of every series (points, points read, MASE, the naïve's, relative MASE,
sMAPE, scaled pinball, coverage 80/90, why not measured), the relative MASE of the series drawn on a log scale with a line at 1, the coverage's reading;
(4) what the model cannot know — its readings and the covariates the node read; (5) how this report was computed, the numbers of the forecast, the notes;
(6) the licence, last — its reading, the weights used (repository@revision, sha256 of the weights and of the licence) and the licence's text **verbatim**:
the vendored `licence/LICENSE.txt`, the same bytes in the five languages (sha256 `648194…3fc8c`), in one cell of a one-row table so that the workbook has it
as its own sheet. Excel's sheets: the check, the measures, the mean errors, the series, «Reading», the numbers, the notes, the weights, the licence.

**The template ids and the names the runner sends** (the contract with F3; `tests/test_models_timestral_report.py`, `READINGS` and `NOTES`, holds them
with example values). Summary: `summary` (size, n_series, value, series, horizon, last, context), `summary_one` (the same without n_series and series),
`summary_steps` and `summary_one_steps` (`last` a position: for a time in steps), `summary_known` and `summary_past` (covariate: a list), `filled_linear`
and `missing_model` (count). Forecast, level_2 = the series: `forecast_bands` (median, low80, high80, low90, high90, at) and `forecast_bands_steps`.
Holdout: `holdout_mase` (holdout, mase, baseline_mase), `holdout_not_measured` (count, n_series), `holdout_none`. Coverage: `coverage` (in80, in90, steps,
share80, share90). Comparison: `comparison_supported`, `comparison_opposite`, `comparison_not_supported` (better, n_series, p_value, alpha),
`comparison_few_series` (n_series, min_p, alpha: the smallest p-value the test can reach, the sample flow's five series), `comparison_one_series` (mase,
baseline_mase), `comparison_not_computed` (n_measured). Limits: `limits`, `limits_context` (used, n, series), `limits_clip`. Licence: `licence` (licence),
`licence_accepted` (by, on). Method: `method_zero_shot`, `method_quantiles`, `method_holdout`, `method_baseline`, `method_coverage`, `method_wilcoxon`,
`method_limits`, `method_missing`; formulas: `formula_naive`, `formula_mase`, `formula_smape`, `formula_pinball`, `formula_coverage`, `formula_wilcoxon`.
Notes (`note_<k>` = `@id …`): `ends_early` (count, last), `context_cut` (used, n, series), `history_shrunk` (points), `future_left_out` (count),
`covariate_missing` (count, covariate), `time_budget` (seconds, limit). The verdict row of the check: `ok` when p ≥ α, `failed` when p < α (the fixture's
reading of «not rejected» and «rejected»), `value` the median of the differences; its `advice_id` may say `one_series`, `no_holdout` (the not-computed
sentences). A series' `not_measured` reason: `too_short`, `no_holdout`, `zero_scale` (any other id is said «not measured»).

**Where the spec was silent, or the machinery decided** (nothing else of the spec was changed):

1. **The charts are slots.** A definition is made from the node's settings, never from the rows, so the layout cannot name the series it draws: the k-th
   of twelve slots (`max_series_charts`) shows the k-th series whose `median` line the runner wrote, found by 24 calculated fields (`drawn_<k>`, an
   aggregate over the whole dataset and a LOOKUP); a slot without a series takes no room (`collapse_hidden`). The runner's order of the `series_curve`
   rows is the order of the charts.
2. **The relative MASE is drawn for the series drawn**, not «the first 20 by total»: the page cannot rank the series by `total` (the same reason), and the
   table beside it holds every series. It is a dot per series with its line at 1 (the engine's interval chart: a bar chart has no line), on a log scale.
3. **The baseline is a line in amber, not dashed**: the engine's `xy` chart draws dashes only for its reference lines.
4. **The holdout's charts have no baseline**: `series_curve` holds one `baseline` line, which the fixture writes on the forecast's instants. A
   `holdout_baseline` line from F3 would need one mark in `_holdout` (a request, not done: a line with no rows would still enter the legend).
5. **The licence on the first page is its line** («Forecast by …, weights by Mistral AI under the Modified MIT License: the licence's full text is on the
   last page»), the text verbatim on the last page, as «Where else the note appears» says; the text is never translated (Boundaries).
6. **Two summary ids and one forecast id more than the spec's list** (`summary_one`, `summary_*_steps`, `forecast_bands_steps`), and `comparison_few_series`:
   a sentence cannot see `time_kind` (a model row, not a setting), and a number of days written as a date would be wrong for a time in steps. Without a
   series column the summary says one series. An hourly series' sentences say «the last hour observed» instead of a date (the `date` form writes days).
7. **`level` is not a note name**: ARIMA's readings send a band's level as a number (`{level:pct}`); the spec's third name would have changed its page.
   `value` neither (the A/B's cap note): a value column called «2024» would take the language's decimal mark in the summary's sentence, an edge case.
8. **«Many rows» is not drawn** under the check: the test compares series, one difference each, whatever the points.

**Criteria.** Every fixture in five languages: `reports.definition.check` clean, the PDF drawn (five pages with the fixture's readings, six with a line of
every template), no warning, no «= <». No sentence of a measure holds a word of `verdict_words` in any language (`measure_readings`, 21 ids, the measures'
and the mean errors' sentences, the captions, the cards, the notes), and each of the three verdicts holds one. The licence: the words' text is the vendored
file byte for byte (by its sha256 in the engine's suite, which holds only the backend; by the file itself when the repository is beside it) and is handed
whole to the last page. **Golden** (a scratch dump on clean copies of `f08266c9` and of `f08266c9` with this lot's files, the fake chart service):
**500 of 500 renderings byte for byte as before** (the PDF, the definition, the labelled rows) — every other model's
fixture (32) and every variant of every statistics test (68), in the five languages; none with a warning. The slots' calculated fields on about
100,000 rows (twelve series of some 8,000 points drawn, the results' ceiling): the report in 13.9 s on one CPU, every field in DuckDB (none by the
interpreter), eleven pages.

**Tests.** `tests/test_models_timestral_report.py` (new, 54) and `test_models_layouts.py` (the node is no longer pending; it is explained, as the
decomposition). Run together with `test_report_explanations`,
`test_explanations_regression_survival`, `test_explanations_ts_trees_clv`, `test_models_ab_review`, `test_models_catalog`,
`test_models_output_columns`, `test_models_results`, `test_reports_model`, `test_stats_explanations` and `test_stats_layouts`, **1,798 passed**
(the CI image, `--memory=2500m --cpus=1`).

### F3 — the runner

**Built (2026-11-22).** `runner/py/ff_series.py` (the options, the covariates, the survey of every series, the plan of blocks and batches, a
batch's arrays), `ff_timestral.py` (`fit_timestral`: loading, threads, the time budget, the forecasts block by block, the output by row groups, the
results), `ff_measures.py` (the seasonal naïve, the scale, MASE, sMAPE, pinball, WQL, coverage, the sums across the series, Wilcoxon), `ff_reading.py`
(the `@id name=value` rows, F4's contract word for word), `ff_tests.py` (the group's self-test checks); `reference/make_reference.py` (the tables of
`reference/data/ff_*.csv` and `expected.json`, computed with the official package outside the image), `witness.json` (the pins and the closure uv
resolved), `reference/measure.py` (one run's time and memory peak in the image); the stub «not built yet» is gone; `make_output_columns.py` has two
Timestral cases and `test_models_output_columns.py` no longer leaves the node out.

**How it runs.** (1) The options with the catalogue's defaults (`quantile_level`, `too_many_covariates`: the two lists together past
`max_covariates`), the table read once as Arrow columns, the covariates as one array each over the table's rows (a number as it is, a boolean 0/1,
a text the code of its label in the text order of the whole column, `empty_covariate`, `too_many_levels`, `covariate_type`); (2) **the survey**:
every series built and checked in text order (the series rules of `tab_series`, `too_many_series`, `known_future_missing`, `known_future_gap`,
`known_future_long`), a few numbers kept of each (`Meta`), its arrays dropped — every refusal of the data comes before anything is loaded or
forecast, and `size_absent` after them; (3) the size's weights from the image's folder, `torch.set_num_threads` = the Job's threads, one interop
thread; (4) **blocks** of consecutive series (at most 400,000 places of span, holdout and horizon; at least one series), each rebuilt, forecast in its
batches, measured, written to the output as **one row group** of a `ParquetWriter` kept open, its series' numbers added to the totals
(`ff_measures.Across`: sums and counts), and dropped; (5) the results: the model's numbers, the series' table, the comparison and the measures, the
curves of the series drawn, the reading.

**Where the spec was silent, or wrong, and what was done** (nothing else of the spec was changed):

1. **The batches.** F0 found that only a batch of one constant size makes a series' bits independent of the others. Here the constant size is a
   function of the batch's shape alone, and the shape of the series alone: the context is padded on the left with missing values to the next rung of
   a ladder of patch counts (1 … 8, then four rungs a doubling: 10, 12, 14, 16, 20 … 1,024; at most a quarter more patches than it needs; leading
   missing patches are never keys of the model's attention), the horizon is the forecast's own, and the size is `min(16, 256 // (rows × (patches +
   horizon patches)))`, at least 1 (rows = the series and its covariates), the last batch of a shape filled with copies of its last series. Measured
   (one batch, 2 threads): with 128 values a batch of 16 costs 1.8 (5M) to 3.1 (209M) times a batch of one; with 1,024 values a batch of 7–8 costs
   within 5–15 % per series of a batch of 16 and half its time when it holds one series alone (209M: 1.78 s against 3.39 s), longer contexts gain
   nothing from batching (F0). So the same series gives the same bits whatever the table, the series beside it, its place and the blocks (a
   self-test check runs every series in a block of its own and compares bit for bit), and `batches` counts the library's calls.
2. **The tolerance against the official package is 5e-5 of the series' largest quantile**, not 1e-6. Measured on the reference cases, the four
   sizes: at most 6.4e-7 of it, except one series of Timestral-5M (level 0.01, first step of its holdout) at 1.15e-5. The batch's shape alone moves
   that series by 7e-6 inside the image (a batch of 2 against 1), and the witness's process (a venv, the same CPU, the same torch wheel) differs from
   the image's already for a batch of one (2e-6 of it): the kernels' blocking, not the code. A measure made of the forecasts is checked within 1e-3 of
   itself (the quantiles' gap divided by the series' scale); every number made of the data alone, and the measures and the test on fixed numbers, within 1e-12.
3. **A constant series is forecast flat**, not refused (`degenerate` stays for the library's own error, named per series): its holdout is not
   measured (`zero_scale`). One value is forecast flat too, its holdout `too_short`. A series never negative gets no negative quantile (the library's
   clamping, said by `limits_clip`); a negative series is not clamped.
4. **The holdout, per series.** `holdout` empty means the horizon (with known covariates: the series' own future rows). A series shorter than
   holdout + season + 1 places is forecast but has no holdout forecast: its last places are history rows (`is_holdout` false), `not_measured =
   too_short`. The measures read the holdout's observed values only (a filled or a missing value is not measured) whose seasonal naïve exists; the
   scale reads the pairs of observed values of the history; the seasonal naïve reads the series as the model reads it (filled by `linear`) and, at a
   value left missing (`model`), the value one season earlier. `model|||holdout` and `horizon` are the largest of the series'.
5. **Wilcoxon is `scipy.stats.wilcoxon(method="auto")` exactly**, including its third path the spec did not name: with ties or zeros and at most 13
   differences, scipy counts the 2ⁿ sign patterns (a permutation test, exact) rather than the normal approximation. The statistic is the smaller of
   W+ and W−; `count` the non-zero differences; `value` the median of every difference; the verdict `ok` (p ≥ α) or `failed` (p < α), `not_computed`
   with `advice_id` `one_series`, `no_holdout` or none; `comparison_few_series` follows the verdict's line when the smallest p-value the test can give
   (2/2ⁿ, or the normal approximation's at W = 0) is not below α.
6. **The time budget** is projected after every batch, not only the first (the first one decides a run too slow at once; the later ones catch a
   table whose first shapes are cheap), at the speed measured so far by the unit «patches × rows the model reads», against 85 % of the time left from
   the runner's start (`task["_started"]`, `limits.time_budget_seconds`). The sentence counts forecasts, not series (a holdout doubles them):
   «at the speed of the first 16 forecasts (0.031 s each with Timestral-209M on 2 CPUs), the 10,000 series need about 12 minutes; the step allows 60
   seconds: …». **The output is removed** when a refusal or a crash comes after a block was written (a self-test check: none is left).
7. **The results stay within `max_result_rows`** (the engine refuses a step above it): the series' table comes first; the series drawn (twelve,
   by Σ|y|) keep their lines and share what is left for their history (`note_history_shrunk`), fewer are drawn when even their lines do not fit, and
   when the series' table alone does not fit (above about 5,000 series of 18 numbers) it keeps the series with the largest Σ|y| and says so in
   `note_series_left_out` — an English sentence: F4's words have no template for it. The series' rows are written statistic by statistic (one call
   each: 0.03 s for 1,000 series instead of 1.4 s).
8. **Notes**: `note_ends_early`, `note_future_left_out`, `note_context_cut_<k>` (the first three series cut to 32,768 values; the reading's
   `limits_context` names the same), `note_covariate_missing_<k>` (the empty values the forecasts from the end read, per column), `note_time_budget`
   (with a budget), `note_history_shrunk`, `note_series_left_out`. A line `holdout_baseline` is written beside the holdout's lines (F4 does not draw it
   yet: its note 4). Without a series column the one series is named after the value column (the curves' and the series' `level`).
9. **The licence's acceptance in the reading** (`licence_accepted`) is said when the task carries `licence: {"by", "at"}` (the audit row's actor and
   time, ISO): lot F6 passes it; without it the line is not written.
10. **pandas stays in the image** (F0 and F2 left it to this lot): the library's self-test and `reference_case` read the CSVs into frames, and pyarrow
    imports it anyway. The runner itself never makes a frame.
11. **`expected.json`** holds `cases` (five refusals, any size, run by the library's self-test), `size_cases` and `sizes` (ten cases, each size's
    expected quantiles at the first, middle and last step of five levels for three series a case, the measures, the test, the counts, the reading's ids),
    `measures` and `wilcoxon` (fixed numbers; the test's three paths). `ff_wilcoxon.csv` is twelve series of 60 days (the exact path end to end); the
    zeros and the ties are in the fixed differences. 149 KB; the Job's code folder is 667 KB (the 900 KiB ceiling's test passes).

**The self-test**: 36 checks, none failed or skipped (the library's 19, `tab_series`' six among them; the five refusals of `expected.json`; the
group's 12: the weights' `config.json` and `LICENSE`, the measures and Wilcoxon against their witnesses, the plan, the reference cases of each size present, the same bits twice,
a series alone in its blocks, the vendored package called directly on hand-made arrays bit for bit, sorted quantiles and odd symmetric averaging, the
types of the time and the series, `size_absent`, `too_many_series`, `too_slow` with nothing left, the results' ceiling); **32 s** with the four sizes
(2 CPUs, 2.5 GB; the edge cases and the determinism checks run with the smallest size present).

**Time and memory** (`reference/measure.py` in the image, 2 CPUs, `--memory 2500m` for 5M and 28M, `4g` for 123M and 209M; daily series, horizon and
holdout 28, so **two forecasts a series**; seconds of the forecasts, the whole fit with the import of torch (about 2 s) and the loading in brackets;
peak: the container's `memory.peak` / the process's `VmHWM`, the input's generation included):

| Run | 5M | 28M | 123M | 209M |
|---|---|---|---|---|
| Loading the weights | 0.06–0.09 s | 0.24–0.28 s | 1.0–1.4 s | 1.7–2.0 s |
| 1 series, 1,000 points | 0.07 (1.9) | 0.26 (2.2) | 1.14 (4.3) | 1.65 (5.4) |
| 1 series, 10,000 points | 0.20 (2.1) | 0.84 (2.9) | 2.62 (5.4) | 4.66 (8.4) |
| 1 series, 100,000 points (reads 32,768) | 0.91 (2.8) | 2.80 (4.8) | 9.29 (12.1) | 15.9 (19.6) |
| 100 series × 1,000 points | 1.74 (3.7) | 7.53 (9.7) | 27.8 (30.7) | 47.7 (51.3) |
| 1,000 series × 100 points | 3.83 (6.5) | 11.9 (14.9) | 42.3 (46.1) | 71.8 (76.4) |
| 10,000 series × 100 points | 37.7 (47.9) | — | — | — |
| Peak, container / process | 235–325 / 384–456 MB (10,000 series: 515 / 580) | 325–409 / 557–594 MB | 692–793 / 1,281–1,315 MB | 1,088–1,290 / 1,942–1,976 MB |

The work around the model (survey, blocks, measures, output, results) is about 0.8 ms a series of 100 points: 8 s of the 10,000 series' 48. **The
memory does not grow with the table**: 1,000 series of 28M peak at **407 MB** in the 2.5 GB container (`peak_mb=1000` passes), 10,000 series of 5M at
515 MB against 324 MB for 1,000; the output of 1.28 million rows is written in four row groups. **`too_slow`, measured**: 10,000 series × 100 points with
209M and a 60 s budget refuse after the first batch, 5.4 s into the fit («… need about 12 minutes; the step allows 60 seconds …»; F0's rate makes it
13.8 minutes), with no output left; 1,000 series with 28M and 10 s refuse at 2.3 s («about 17 seconds»; the run takes 15).

**Tests.** The image's self-test above; `make_output_columns.py foundation_forecast` (two cases: one series, and series of a text column with known
and past covariates); the engine's `test_models_output_columns`, `test_models_layouts`, `test_models_timestral_report`, `test_models_catalog`,
`test_models_results`: **1,184 passed** (the CI image, 2.5 GB, 1 CPU); the editor's `model-catalog.test.mjs` (54); `go test ./internal/modelgroups/
./cmd/modelgroup/`. The runner's real results of three runs (three series, two shops with known and past covariates, gaps filled) pass the engine's
`check_table` and F4's report in English and Italian with no sentence left as an id.

## References

- Mistral AI, *Timestral* model cards and `LICENSE`, `https://huggingface.co/mistralai/Timestral-{5M,28M,123M,209M}`
  at the revisions above (read 2026-11-21).
- Mistral AI, `https://github.com/mistralai/timestral`, commit `a534bdbd244d` (2026-10-15).
- Mistral AI, «Timestral», `https://mistral.ai/news/timestral/`; `https://docs.mistral.ai/models/timestral-1-0-123m.md`.
- A. Petralia et al., «Timestral», arXiv:2610.37585 (2026).
- R. J. Hyndman, A. B. Koehler (2006), «Another look at measures of forecast accuracy», *International Journal of
  Forecasting* 22(4) — MASE.
- R. Koenker, G. Bassett (1978), «Regression quantiles», *Econometrica* 46(1) — the pinball loss.
- F. Wilcoxon (1945), «Individual comparisons by ranking methods», *Biometrics Bulletin* 1(6).
- F. X. Diebold, R. S. Mariano (1995), «Comparing predictive accuracy», *Journal of Business & Economic Statistics*
  13(3); D. Harvey, S. Leybourne, P. Newbold (1997), *International Journal of Forecasting* 13(2) — Open question 7.
