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
