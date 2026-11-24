# Licences of the software Tabularia includes

Tabularia is licensed under the GNU AGPL-3.0. It includes other software, each piece under the licence its authors give it.
This page lists what the Bayesian models group ships and what the editor's statistics code uses, with where each full text
is. **The texts are those of the sources, quoted and never explained**: the file of each source is authoritative for its
terms.

The notices of every package of the app, of the fonts, of the pre-trained forecasting code and of the components below are in
one file, `third-party-notices.txt`, which the footer of the application and of this site links ("Open source licences"). It
is written at each build from the lockfile and from the files below, so it matches the version you run.

## What the Bayesian models group ships

The group's image holds CmdStan 2.40.0 and the stanc3 2.40.0 compiler. Their licence files, and those of the libraries bundled
in them, ship with the group in
[`gateway-go/internal/modelgroups/groups/bayes/licence/`](https://github.com/JumpAsAService/Tabularia/tree/main/gateway-go/internal/modelgroups/groups/bayes/licence)
(22 files). Each text is in the notices file once, under the path it ships at and its sha256.

| Component | Version | Licence identifier | Its text, in the group's `licence/` folder |
|---|---|---|---|
| CmdStan | 2.40.0 | BSD-3-Clause | `cmdstan-2.40.0/LICENSE` |
| Stan | bundled in CmdStan 2.40.0 | BSD-3-Clause | `cmdstan-2.40.0/stan/LICENSE.md` |
| Stan Math | bundled in CmdStan 2.40.0 | BSD-3-Clause | `cmdstan-2.40.0/stan/lib/stan_math/LICENSE.md` |
| stanc3 | 2.40.0 | BSD-3-Clause | `stanc3-v2.40.0/LICENSE.txt` |
| Boost | 1.87.0 | BSL-1.0 | `.../lib/boost_1.87.0/LICENSE_1_0.txt` |
| Eigen | 5.0.1 | MPL-2.0, with the files `COPYING.*` of the other licences it names | `.../lib/eigen_5.0.1/COPYING.MPL2` and its siblings |
| SUNDIALS | 6.1.1 | BSD-3-Clause | `.../lib/sundials_6.1.1/LICENSE` |
| oneTBB | 2020.3 | Apache-2.0 | `.../lib/tbb_2020.3/LICENSE` |
| cpplint, Google Test, Google Benchmark, the OpenCL headers | as in Stan Math | as in the files | `.../stan_math/licenses/` |

(`...` stands for `cmdstan-2.40.0/stan/lib/stan_math` in the paths above.) Stan Math lists what it bundles in
`licenses/dependencies.txt`, which is in the folder too.

The group's image holds, besides CmdStan, the Python packages `numpy` and `pyarrow` and the toolchain (`g++` and `make`) from
Debian. Their versions are those of the group's recipe, `recipe.json` beside the `licence/` folder. The notices of the
Debian packages are in the image, at `/usr/share/doc/<package>/copyright`.

## What the editor uses

The Bayesian editor draws the shapes of the distributions in your browser with the `@stdlib` packages (the `random-base-*` and
`stats-base-dists-*` ones). They and the packages they depend on are in the lockfile of the frontend, so the notices file
carries their licences and notices as it carries those of every other package.

## Other licences

- The weights of the pre-trained forecasting models come under a licence of their own, accepted by an administrator when the
  group is enabled: see [Statistical models](capabilities/models.md#pre-trained-forecasting-timestral).
- The notices of the fonts and of the Timestral inference code are in the same notices file.
