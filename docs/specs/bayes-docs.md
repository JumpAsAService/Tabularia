# Spec: the documentation of the Bayesian models (`docs`)

Status: **Approved by the owner on 2026-11-24** (drafted the same day). It specifies the module `docs` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 7 of its order of construction, beside `bayes-mlflow` and
`assistants`, and it depends on **every module**: what it writes is what the other specs built. Nothing of it is written
except what is said in «What exists». Decisions marked **(owner)** are the owner's (the map's «Interview answers», **the
answers**, and the owner's rules); the rest are this spec's, to confirm; those marked «spec decision, 2026-11-24, owner may
revisit» are small choices the owner may change without reopening the module. The five questions the draft left open were
answered by the owner on 2026-11-24 and are in «Decisions».

Words: **verified** = read in this repository on 2026-11-24; **assumed** = not run here, closed by the lot named beside it.

## Objective

Everything a person needs to use, administer and trust the Bayesian models, in the documentation the site publishes, and
the checks that keep it true:

1. **The capability page** `docs/capabilities/bayesian-models.md`: what the space is, how to draw a model (data, parameters,
   priors, the likelihood, the groups of a hierarchical model, the free model's expressions), how to read a prior, the ten
   templates, saving and versions, the node in a flow (binding, sampler, output, the prior predictive check), how to read the
   report (the summary, the diagnostics as measures and facts, the predictive checks as charts), MLflow, the assistant, the
   limits, what is not in this release.
2. **The administrator's guide**: enabling the group, what its build needs, the resources of a run and of a compile, the
   compile cache and how to empty it, the limits and their defaults, the audit, an air-gapped cluster.
3. **The licences**: the notices of CmdStan, Stan, Stan Math, stanc3 and what they bundle, the Python packages of the group's
   image and the frontend's stdlib packages, in the file the site already links and in the documentation, with the full texts
   as shipped and nothing paraphrased.
4. **The site's navigation and the pages that count models or list groups**, so no page says something the release made
   false.
5. **The five-language texts checked**: every text of the editor, the node, the report and the assistant in en, it, de, es, fr
   is scanned by one test for the words the project forbids, and the English pages by the site's checks.

The documentation holds **no name of a competitor** and **no path of a person's computer**, and follows the rule of words of
the product: measures and facts, no verdict (answers 11, 12; the explanations spec).

## Decisions

| Question | Decision | By |
|---|---|---|
| The page's name | **`docs/capabilities/bayesian-models.md`**: the distribution catalogue's script already looks for it (`PAGE` in `.github/scripts/bayes_catalog.py`, verified) and, once it exists, fails when it does not name every distribution `` `id` `` | The existing script; the map |
| One page or several | **One capability page** for the person (draw, run, read) with a section «For administrators» at its end; the licences in a page of their own, `docs/licences.md` (below); `models.md`'s `### Bayesian model` shrinks to the model's entry (variants, outputs, a link) | The map («One capability page»); spec |
| The page's voice | As the other capability pages (`docs/capabilities/*.md`, verified): what it is, where it is in the interface, how it works, who may use it, what it does not do, an example; English; the product's words as the interface says them in English | The existing pages |
| Measures, no verdict | The page's own sentences say what a diagnostic measures and what a run shows; **the literature's reference values appear only as the report quotes them** (in English, as the source writes them, with author, year and page), **never beside a number of a run**; no «converged», «good», «bad», «reliable», «problem», «passes», «fails» | Owner (answer 11); the report spec, «The words» |
| Predictive checks | The page says the checks are charts and that **no p-value, no probability and no share is given**, and why (answer 12) | Owner (answer 12) |
| No competitor, no local path | No other product is named as a comparison; the products the work needs (Stan, CmdStan, MLflow, Kubernetes) are named as what they are; no path under a person's home, no machine name; screenshots hold no secret | The owner's rules (nothing of competitors in texts; no local reference in committed specs and docs; no keys in pictures) |
| GitHub Pages | The page is read by Liquid: any double opening brace or brace-percent pair in it (an expression such as a double brace, a template-looking example) is inside the raw guard, the HTML comment that opens with the word raw in Liquid's tag and closes with endraw, which is the first and the last line of the page as in `flows.md` (verified); the push guard (`.github/scripts/push_guard.py`, check «GitHub Pages») fails a push otherwise | The existing rule |
| Where an administrator reads | **In the capability page's «For administrators»**, linked from `docs/capabilities/models.md`'s «Turning a group on» and from `docs/deploy/kubernetes.md`'s paragraph on the Bayesian group | Owner, 2026-11-24 |
| Counts and lists | Every page that counts the groups or the models or lists them says what the release holds (the groups: eight with `bayes`; models: the catalogue's) from **one place**, checked by a test («Testing strategy») | Spec |
| The release notes | The CHANGELOG entry and the README's line belong to the release, not to this module; this module prepares their text in «As built» | The release process (`docs/release-process.md`) |
| Screenshots | Four, in English, taken by hand on kind in Chrome, with no real name or key: the editor, the node's panel, the report's first page, the diagnostics page, in `docs/media/guide/bayesian-models/` | Owner, 2026-11-24 |
| The licences | CmdStan and its family are added to `third-party-notices.txt` as an `--extra` entry (as Timestral is), plus a short `docs/licences.md` listing the components, versions and licence identifiers with links; the texts are **quoted, never interpreted**. Extending `image_licence_check.py` to the group's image is a **later item**, not in this module | Owner, 2026-11-24 |
| The page's links | `bayesian-models.md` is linked from `docs/capabilities/models.md` and from `docs/deploy/kubernetes.md` | Owner, 2026-11-24 |
| «No competitor» check | A **review by an opus model** of every page, locale and spec of the release, plus a **scan with a list kept outside the repository**; the list never enters the repository | Owner, 2026-11-24 |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The models' page | `docs/capabilities/models.md` (2,121 lines): «At a glance», «Turning a group on (administrators)» (packages pinned by hash, limits and access, the audit actions), «Using a model in a flow», one section a model, «The output», «The Report tab», «How a check speaks: verdicts, facts and measures», «Trying a model: Run, and «not up to date»», «MLflow», «Predict», «Limits, and what it refuses», «Related». **`### Bayesian model` already exists** (lines 1716–1729: the entry, the variants, the report's first pages; it ends «The charts … come with the next lots of the module») | The entry is brought up to the release and links to the new page; «MLflow» gets the paragraph `bayes-mlflow`'s lot M3 writes; «Predict» says a Bayesian version is not applied |
| Its stale counts | The page's opening says «Ten models are in the first release, in five groups» and «Administration → Statistical models lists the seven groups Tabularia ships»; `docs/capabilities/README.md` says «Sixteen models … run as Jobs on Kubernetes (R, and Python for boosting and pre-trained forecasting)»; `statistics-overview.md` lists the families; `README.md` line 218 lists the models | One pass over them in lot D2 |
| The administrator's side | `docs/capabilities/models.md` «Turning a group on»; `docs/deploy/kubernetes.md` (line 209: **«The Bayesian group»**: CmdStan from its release tarball pinned by URL and sha256 for x86-64 and arm64, `github.com` with the storage host its downloads redirect to, PyPI or a mirror for `numpy` and `pyarrow`, Debian's mirror for the toolchain, an air-gapped cluster's `MODELS__SOURCES_BASE_URL`, the build's memory and time); `docs/configuration.md` (`MODELS__SOURCES_BASE_URL`); the group's card in Administration (`api/models_admin_bayes.go`: the compile limits, the cache of binaries, the sampling `/tmp`, the last compiles, «Empty the cache»: `GET`/`PUT /admin/models/bayes/compile`, `POST /admin/models/bayes/cache/empty`); the audit's `bayes_model.*` actions and `models.group_*` (`docs/governance/audit-log.md`) | The guide gathers what the card shows and the limits' defaults (`bayes-contract.md`, «Limits») in one place and links the deployment page |
| The navigation | `website/app/utils/docs-nav.ts`: **Statistics** lists `statistics-overview`, `statistics`, `models` and `/docs/connections/mlflow`; «A route listed here that does not exist at build time is not shown»; a doc not listed lands in «Reference» | One line for the new page (and one for the licences), in Statistics |
| The site's checks | `website/scripts/check-links.mjs` (every link, every anchor, every link to a repository file, run by `.github/workflows/website.yml`); `website/tests/*.test.ts` (placeholders, prose blocks, code copy, tabs) | The new pages pass them |
| The distribution catalogue's check | `.github/scripts/bayes_catalog.py --check`: the copies equal the snapshot, and **when the page exists, it names every distribution `id` between backticks** (16 today: `normal`, `student_t`, `cauchy`, `exponential`, `gamma`, `inv_gamma`, `lognormal`, `beta`, `uniform`, `lkj_corr_cholesky`, `multi_normal_cholesky`, `std_normal`, `bernoulli_logit`, `binomial_logit`, `poisson_log`, `neg_binomial_2_log`) | The page's table of distributions is written from the snapshot's fields (parameters, support, use) and the check passes |
| The templates | `gateway-go/internal/bayestemplates/templates.json` (ten: `glm_linear`, `glm_logistic`, `glm_poisson`, `glm_negbin`, `hier_intercepts`, `hier_slopes`, `hier_nested`, `ts_ar`, `ts_random_walk`, `ts_local_level`) and `.github/scripts/bayes_templates.py --check` | One line per template, from the file's titles; no id invented |
| The limits | `gateway-go/internal/bayescatalog/catalog.json` `limits` (name 40 bytes, label 200, note 2,000, expression 1,000 bytes, nesting 32, nodes 256, lag 60, graph nodes 200, plates 8, plate depth 4, effect components 10, graph 262,144 bytes) and the contract's (`bayes_max_summary_quantities` 2,000, charts 12, `bayes_unit_points` 1,000, `max_quantities` and `max_units` 20,000, the draws' ranges) | A table in «Limits, and what it refuses» read from them, with a test that compares the page's numbers with the catalogues' |
| The licences | `gateway-go/internal/modelgroups/groups/bayes/licence/` (22 files, 204 KB: CmdStan, Stan, Stan Math, stanc3, Boost, Eigen, SUNDIALS, TBB…, each with its sha256 in `bayes-image.md`, «Licences»); the site's footer links **`third-party-notices.txt`**, written by `frontend/scripts/third-party-notices.mjs` from the lockfile (every non-development npm package, **the `@stdlib/*` ones included**, 45 declared) plus `--extra "NAME\|LICENCE\|FILE"` entries (IBM Plex Sans, and the Timestral inference code from `frontend/assets/third-party/timestral.txt`) in `frontend/package.json` (`predev`, `prebuild`) and `website/package.json` (`pregenerate`); `frontend/tests/third-party-notices.test.mjs`; the images' check `.github/scripts/image_licence_check.py` (Python, npm, Go; **not** the group's image or the compiler's) | The CmdStan family becomes one more `--extra` entry; the group's Python packages and the Debian toolchain are named in `docs/licences.md` |
| The words' checks | `frontend/tests/i18n.test.mjs` (every language has English's keys, the placeholders match, every message compiles), `tests/bayes-editor-words.test.mjs` (the editor's keys, the templates' sentences, no judging word), `backend/tests/test_report_explanations.py` (the report's five texts of every id, no verdict word); **no check of competitor names or of local paths exists** (searched) | Lot D3 extends the scan to every Bayesian key and to the pages |
| The AI's page | `docs/integrations/ai.md`: a table of the assistants (data, flow, script, titles) with what each sends | A row for the editor's assistant (`assistants`) |
| Pictures | `docs/media/guide/<area>/…` (the existing guides' screenshots) | A folder `bayesian-models/` if the owner wants them |

## The capability page

The sections, in order (each a heading of the page; the order a person needs them):

1. **What it is** (two paragraphs) and **at a glance** (a table: where, who may use it, what it needs, what it makes, what
   it never does). Says it runs on Kubernetes only, as a Job in a group an administrator enabled, with **no internet access**
   at run time.
2. **Your first model**: from a template, in the browser, to a saved model, to a flow, to a report — one worked example on a
   template with the Compose stack's sample data (the numbers read from a real run on kind, **with the numbers it gave**, and
   no sentence that says they are good).
3. **Drawing a model**: the palette (data, parameter, transformed, linear predictor, likelihood, state, plate), the inspector,
   the names' rule (`[a-z][a-z0-9_]*`, not a Stan reserved word, not ending `__`; otherwise a generated identifier shown in
   the Stan code's mapping table), labels, notes; the keyboard (Mac and a laptop without F/Home/End keys); undo.
4. **Priors**: the preview chart beside the numbers; what a default prior says and where it comes from (the templates'
   sentences); **standardized predictors** (answer 6: a coefficient is per standard deviation, both scales in the report); the
   ten templates, one line each; the sixteen distributions in a table (id, parameters, support, where it can be used) that
   **names every id in backticks**.
5. **Hierarchical models**: plates and nested plates, group indices, the non-centred form by default and its switch (what each
   form is, and that the report's facts tell which one a run may need, never which is right).
6. **The free model's expressions**: the language (names, numbers, `+ - * / ^`, the catalogue's functions, `x[i]`, `lag(x, k)`),
   what it refuses and why (ASCII only, no strings, no statements, the limits), how an error is shown and what a finding says;
   examples are inside the Liquid guard.
7. **Saving, versions and sharing**: the saved model in a folder, its versions, restore, duplicate, «used by N flows», what
   deleting is refused for, the compile in the background (about 10–30 s) and its states.
8. **In a flow**: the node, choosing a model and **pinning a version** (the notice «a newer version exists»), **binding each
   variable to a column** (the types, a group from a text column, a category with its first level as reference, the series'
   time), the sampler (chains, warm-up, draws, seed; Advanced: `adapt_delta`, `max_treedepth`, initial values), the output
   (summary, draws as long rows, replicated data, the ten columns), Run on the development sample, «not up to date», the
   **prior predictive check**.
9. **Reading the report**: the pages in order, as `bayes-report.md` lists them; each diagnostic as **what it measures** and
   where to find it, **never a verdict**; «The literature's reference values» says where the report quotes them (with a link to
   the report's «How this report was computed») and states them **nowhere else**; the predictive checks as charts and why there is
   no p-value; the original scale of a coefficient; the precision of the draws (8 significant digits).
10. **MLflow**: what is recorded (parameters, the summary as metrics, the report, the card, the program; **never the draws**),
    the options, that a registered version is not applied by Predict.
11. **The assistant**: what it reads and sends (the graph and the measures; no row of data), what it proposes and that the
    person applies it, what it never says.
12. **Limits, and what it refuses**: the table read from the catalogues; the refusals' sentences and their way out.
13. **What is not in this release**: predictions on new rows, LOO and model comparison, hand-edited Stan code, user-defined
    functions, variational methods, ODEs, Gaussian processes, mixtures (the map's «Out of scope»).
14. **For administrators**: «The guide».
15. **Related**.

## The administrator's guide («For administrators»)

- **Enabling**: Administration → Statistical models → **Bayesian models**: the build (CmdStan from the release tarball, pinned
  by URL and sha256; `numpy` and `pyarrow` pinned by hash; Debian's toolchain), what it needs at build time only (the hosts
  of `docs/deploy/kubernetes.md`), the build's time and memory, the self-test, the access (who may use the group).
- **Resources**: a **compile** Job (its limit, 300 s by default, the memory and CPU) and a **sampling** Job (memory 2 GiB, the
  `/tmp` of 2 GiB, the longest run 3,600 s, the CPU), a run's refusals when its projection passes a limit (the sentence, never
  a partial result).
- **The compile cache**: a binary per saved version per image, an LRU with a limit, what the card shows (the cache's size, the
  last compiles), **Empty the cache** and what it costs (the next run compiles first), what a rebuild of the group does to it.
- **The limits and their defaults**: the contract's table, each with where it is set.
- **The audit**: the actions the group writes.
- **An air-gapped cluster**, **a proxy**, **arm64**: as `docs/deploy/kubernetes.md` says, linked, never repeated differently.
- **Licences**: a link to the page.

## The licences

- **The file the site links**: `third-party-notices.txt` gains an entry per component of the group (CmdStan, Stan, Stan Math,
  stanc3, Boost, Eigen, SUNDIALS, TBB and the rest the notices of `bayes-image.md` list as «not quoted here»), written by
  `third-party-notices.mjs` from the shipped `licence/` files **verbatim** (an `--extra` entry whose file is assembled from
  them by a small script of the repository, hashes checked against `bayes-image.md`); the `@stdlib/*` packages come with the
  lockfile (Apache-2.0 notices are kept by the script as it keeps every package's).
- **The page** lists what is shipped: component, version, licence identifier, where its full text is (the file, the shipped
  directory), and says the texts are those of the sources, **quoted and never explained** (the rule of the Timestral section
  of `models.md`).
- **The images' check**: the group's image is outside `image_licence_check.py`; the page says which packages it holds (the
  Python ones from the recipe's lock, the toolchain from Debian) and where their notices are in the image
  (`/usr/share/doc/<package>/copyright`); extending the check to the group's image is a later item, outside this module (owner, 2026-11-24).

## Tech stack

- **Markdown** pages under `docs/`, read by GitHub Pages (Liquid) and by the site (Nuxt content): nothing new.
- **Node** for the notices' script and the tests (`frontend/scripts`, `frontend/tests`); **Python** (standard library) for a
  small script of the repository that assembles the CmdStan family's notices and for the pages' checks; no new package.

## Commands

```bash
python3 .github/scripts/bayes_catalog.py --check && python3 .github/scripts/bayes_templates.py --check
python3 .github/scripts/test_push_guard.py            # the Liquid guard's own tests
cd website && npm test && npm run generate && npm run check-links
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/third-party-notices.test.mjs tests/i18n.test.mjs tests/bayes-editor-words.test.mjs tests/bayes-words.test.mjs
cd backend && pytest tests/test_report_explanations.py -k bayes -q          # no -n on this machine
```

## Project structure

```text
docs/capabilities/bayesian-models.md                 the page (with «For administrators»)
docs/licences.md                                      the licences page (list and links; texts quoted, never interpreted)
docs/capabilities/models.md                           (changed) the entry, MLflow, Predict, the stale counts
docs/capabilities/README.md, statistics-overview.md   (changed) the counts and the list
docs/deploy/kubernetes.md, docs/integrations/ai.md    (changed) links; a row for the editor's assistant
docs/connections/mlflow.md                            (changed) «What a model records here»: a Bayesian run
README.md                                             (changed) the models' line (prepared; merged at the release)
website/app/utils/docs-nav.ts                         (changed) two lines in Statistics
frontend/assets/third-party/cmdstan.txt               the CmdStan family's notices, assembled from the shipped files
frontend/package.json, website/package.json           (changed) one more --extra in predev, prebuild, pregenerate
.github/scripts/third_party_bayes.py                  assembles that file; checks it against the shipped hashes
frontend/tests/bayes-words.test.mjs                   the scan of every Bayesian key in five languages
docs/media/guide/bayesian-models/                     the screenshots, if wanted
```

## Code style

A page's table, as the catalogue's check expects it (the raw guard is the page's first and last line, as in `flows.md`; it is
not written out here because this spec is itself read by Liquid):

```markdown
(first line: the raw guard, as in flows.md; last line: its closing)
# Bayesian models

| Distribution | Parameters | Support | Where it goes |
|---|---|---|---|
| `normal` | mean, sd | the real line | a prior or a likelihood |
| `student_t` | degrees of freedom, location, scale | the real line | a prior or a likelihood |
...
```

## Testing strategy

- **The catalogue's check** passes with the page (every distribution id in backticks; the table is generated or checked from the
  snapshot, so a seventeenth distribution fails the build until the page names it).
- **The numbers of the page** (`test_bayes_docs.py`, standard library): every limit of the page's table equals the catalogues'
  value; the templates' list equals `templates.json`'s; the counts of groups and models in the pages equal the catalogue's
  (`models.md`, `capabilities/README.md`, `statistics-overview.md`).
- **The words of the pages**: the new and changed pages are scanned for the verdict words of the report's list (English), for a
  reference value of the literature **outside** the paragraph that quotes it, for a path of a person's machine (a path under a home folder of
  any system, a drive letter), for an address of a private network; a page with a double opening brace or a brace-percent pair outside the Liquid guard fails
  (`push_guard`'s own rule, run on the pages).
- **No competitor**: no list of names can be kept in the repository without naming them, so this is a **review** (lot D4, opus
  reads the diff of every page, locale and spec of the release for another product named as a comparison) and a scan the
  coordinator runs with the owner's own list kept outside the repository (owner, 2026-11-24); the list never enters the repository.
- **The five languages** (`tests/bayes-words.test.mjs`): every key under `bayes`, `bayesModels` and the models' keys the
  Bayesian node adds (`models.names.bayes_model`, `models.mlflow` sentence) exist in en, it, de, es, fr with the same
  placeholders; no vue-i18n syntax; no verdict word in any of the five (the lists of `bayes-editor-words.test.mjs`, extended
  with «converged», «good», «bad», «reliable», «problem», «passes», «fails» and their translations); no path, no address of a
  private network; the report's texts (`report_texts/bayes_model.json`, five languages) by the existing explanations test.
- **The site**: `npm run generate && npm run check-links` (anchors included) with the new pages and the navigation.
- **The notices**: `third-party-notices.test.mjs` with the new `--extra`; the assembled file's hashes equal the shipped files'
  and `bayes-image.md`'s; the generated notices hold CmdStan's, Stan's, Stan Math's and stanc3's texts and the stdlib packages'
  notices.
- **By hand (lot D5)**: the page read once against the finished product on kind, every step of «Your first model» done as
  written, Chrome and Firefox; the screenshots (if wanted).

## Boundaries

- **Always**: what the page says is what the product does, from one place for every number; measures and facts, no verdict; a
  licence quoted, never explained; the guard around Liquid; five languages with the same keys.
- **Ask first**: a new page outside the two named; a claim about a result («the model fits») anywhere; the literature quoted
  outside the report's own paragraphs; a screenshot with a real name; a change to the notices' script that changes the other
  groups' entries.
- **Never**: another product named as a comparison; a path of a person's computer; a key, token or address of a real
  installation in a picture or an example; a licence paraphrased as if it were the licence; a reference value beside a number
  of a run; a double opening brace or a brace-percent pair outside the guard.

## Success criteria

1. `bayesian-models.md` exists, passes the catalogue's check, the site's link check and the Liquid guard, and every step of
   «Your first model» works as written on kind.
2. An administrator can enable the group, size a run, read the compile cache and empty it from the page alone.
3. The licences of everything the group ships and the frontend's stdlib packages are in the notices file the site links, with
   the sources' texts verbatim.
4. No page, key or text of the release holds a verdict word, a number of a run beside a reference value, a path of a person's
   machine or the name of another product as a comparison (scans and review).
5. No page counts the groups or the models wrongly (the test).

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **D0** | The checks first: the numbers' test, the pages' words test, the five-language scan, the catalogue's check with a stub page | `test_bayes_docs.py`, `bayes-words.test.mjs` | they run (red on the stale counts) | — | sonnet |
| **D1** | The licences: the notices' assembly, the `--extra` entries, the page | `third_party_bayes.py`, `cmdstan.txt`, the two `package.json`, `licences.md`, tests | criterion 3 | — | sonnet |
| **D2** | The stale counts and links of the existing pages; the navigation; `models.md`'s entry; `ai.md`'s row | the pages of «Project structure» | criterion 5; the site's checks | D0 | sonnet |
| **D3** | The capability page (sections 1–13, «For administrators»), after the modules it describes are built | `bayesian-models.md` | criteria 1, 2, 4 for the page | every other module; `assistants` and `bayes-mlflow` for sections 10, 11 | sonnet, then **opus** reads for verdicts, competitors and paths |
| **D4** | The review of the release's texts (pages, five languages, specs) for competitors and paths | «As built» | criterion 4 | D3, D5 | **opus** |
| **D5** | On kind: «Your first model» done as written; the screenshots; the release notes' text prepared | «As built» | criterion 1 by hand | D3; the node spec's F5; `bayes-report`'s Q5 | sonnet (by hand, alone) |

D0, D1 and D2 can start now; D3 waits for the others, so that the page describes a finished product.

## As built — lot D5 (2026-11-24, kind trial)

«Your first model» of `docs/capabilities/bayesian-models.md` done as written, in Chrome (every step) and Firefox (steps 1 to 4 and the report, the prior predictive check), on the cluster of the node spec's «As built — lot F5»; the source `order_rows` (104,819 rows) of the sample data.

- **Steps 1 and 2**: **Scripting → Bayesian models → New model** asks for a folder (a list with a search field), then the gallery; «Linear regression» opens the dialog «Typical value of the outcome» and «Typical spread» (left empty: 0 and 1, as the page says) with the three priors that follow; **Open the model**.
- **Step 3**: the node «Effect of the predictor» shows `normal(0, 2.5)` and its chart; typing 1 in the spread changed the chart's quantiles from ±4.11 to ±1.64 while typing.
- **Step 4**: Ctrl+S saved version 1 (the toast «saved as version 1»), compiled in **17 s** (the page says 10 to 30).
- **Step 5**: the palette's section **Models → Bayesian model** (searchable); the node dropped after **Change type**, the saved model chosen from a list (name, folder, family), **Variables** listed **Predictor** (`x`, «standardized in the model») and **Outcome** (`y`), bound to `prezzo_unitario` and `quantita`. **A difference from the page**: on this installation both columns are already *decimal* in the source (the sample's contract types them), so the **Change type** changes nothing; the page's «stored as text» is the Compose stack's. The step stands (it is needed when the columns are text).
- **Step 6**: **Run** in the panel read **every row** of the table (the source has no development sample set): **330 s** (278 s of warm-up, 76 s of sampling), 4 × 1,000 draws, 0 divergences, largest R-hat 1.002, smallest effective sample sizes 2,690 (bulk) and 2,390 (tail); the Report's first page gives them in four cards and the facts line. Posterior of the example: intercept mean 67.61 (sd 0.22), effect of the predictor 0.35 per standard deviation of the price (0.15 per unit, `original` scale), spread 72.25. A sentence was added to the page's step 6 saying that a run on a large table with no sample reads every row and how long this one took.
- **Step 7**: **Run the prior predictive check** ended in 10 to 14 s on the 104,819 rows; the report's switch shows «Prior predictive check» (summary of the prior draws: intercept sd 2.53, effect sd 1.01, spread mean 1.02).
- **The four screenshots**, English interface, dark theme, 1600 × 900, Chrome, no real name or key (the author shown is the installation's example account): `editor.png` (the template open, the inspector of «Effect of the predictor»), `node-panel.png` (the flow, the node's panel and the summary table), `report-first-page.png`, `report-diagnostics.png` (the charts of the draws); in `docs/media/guide/bayesian-models/`, the four lines placed in the page (steps 4, 5 and 6 and «Reading the report»).
- **The release notes' text** (for the CHANGELOG's «Unreleased» and the README's line, to be placed by the release process): «**Bayesian models.** A space of its own, **Scripting → Bayesian models**, to draw a model as a graph (data, parameters with their priors and a chart of each, a likelihood, boxes for the groups of a hierarchical model, series and latent states) from ten templates or from an empty canvas, with the Stan program written from the drawing and shown read only, a check of every graph, versions, and a glossary. A **Bayesian model** node in the **Models** section of the flow editor's palette binds the model's variables to the columns of its input, samples it with Stan (CmdStan, in a Job of an administrator-enabled group with no internet access, the model compiled in its own Job and kept in a cache an administrator can read and empty) and returns the summary, the draws or replicated data, with the posterior predictive checks and a prior predictive check as a trial. The **Report** tab, in PDF and Excel, gives the sampler's measures and facts (R-hat, effective sample sizes, divergences, tree depth, E-BFMI), charts of the draws and the predictive checks as charts, each with a sentence that says what it measures and no verdict; a coefficient of a linear predictor is also given on the column's own scale. A run can be recorded in MLflow (parameters, summary metrics, the report and the model's card and program, never a draw) and registered, and an assistant helps draw the model. [Bayesian models](docs/capabilities/bayesian-models.md).» 
- **Not changed**: the page's other sentences read true against the product in the steps above; the page was not read against the PDF and Excel formats, the assistant (lot A5, the owner's), the keyboard table or the administrator's section beyond the card and the cache of lot C5.

## Open questions

None. The five questions of the draft were answered by the owner on 2026-11-24 (see «Decisions»).
