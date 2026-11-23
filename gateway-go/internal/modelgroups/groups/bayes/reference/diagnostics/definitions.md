# The definitions of the diagnostics: posterior 1.8.0, transcribed

The spec `docs/specs/bayes-diagnostics.md` («Decisions») takes the measures of Vehtari, Gelman, Simpson, Carpenter and
Bürkner (2021) **as the R package `posterior` 1.8.0 implements them**: where the paper leaves a detail open, posterior's
choice is the runner's (`runner/py/bayes_diag.py`). This file transcribes, verbatim, the parts of posterior 1.8.0's source
that define them (CRAN's tarball `posterior_1.8.0.tar.gz`, sha256
`b89ba062c762bdc02ae368bb502f9e389cbd0cc34b73f75064441e45f4572dbf`, published 2026-11-18), each with its file and lines,
and lists the choices they make. `make_witness.R` runs these functions on `cases.json`; `witness.json` holds their numbers;
`runner/tests/test_diag.py` compares `bayes_diag.py` with them.

## The choices, read in the source

For a quantity with M chains of N draws (`x`: N rows, M columns):

| Detail | posterior 1.8.0 | Where |
|---|---|---|
| The split | each chain cut into its first ⌊N/2⌋ draws and its last ⌊N/2⌋: with an odd N the middle draw is left out; one chain of one draw is not split | `.split_chains`, split_chains.R:48–56 |
| The ranks of the rank normalization | the ranks among the **split** draws (2M·⌊N/2⌋ of them: with an odd N, without the middle draws), ties given their average rank | `z_scale`, convergence.R:645–654 (applied after `.split_chains`) |
| The normal scores | Φ⁻¹((r − 3/8) / (S − 2·3/8 + 1)), S the number of ranked draws, R's `qnorm` (AS241) | `backtransform_ranks`, convergence.R:708–712 |
| The folded draws | \|x − median(x)\|, the median of **all** M·N draws (before the split) | `fold_draws`, convergence.R:718–720; `rhat.default`, convergence.R:148–152 |
| R-hat | √((N′·var(chain means) / mean(chain variances) + N′ − 1) / N′) on the split chains (N′ draws each), the larger of the bulk's and the folded draws'; **no minimum of draws** | `.rhat`, convergence.R:726–738 |
| The autocovariance | per split chain, by FFT with zero padding to 2·`nextn`(N′), then **rescaled so that lag 0 is the variance with divisor N′** computed directly (`ac / ac[1] * var(x) * (N - 1) / N`); a chain of zero variance has every autocovariance 0 | `autocovariance`, convergence.R:596–612 |
| ρ̂ₜ | 1 − (W − mean over chains of acovₜ) / var⁺, W the mean chain variance (divisor N′ − 1), var⁺ = W·(N′ − 1)/N′ + var(chain means) (the last term only with more than one split chain) | `.ess`, convergence.R:744–757 |
| Geyer's initial positive sequence | pairs (ρ̂₂ₖ, ρ̂₂ₖ₊₁) from k = 1 while the previous pair's sum is positive and 2k ≤ N′ − 4; a pair of negative sum is not kept; the last pair's even term kept when positive (the «improved estimate») | `.ess`, convergence.R:759–779 |
| Geyer's initial monotone sequence | each pair's sum lowered to the previous pair's (both terms set to half of it) | `.ess`, convergence.R:781–790 |
| τ̂ | −1 + 2·Σ ρ̂ₜ over t = 0 … max_t − 1 + ρ̂ at max_t (the last even term); when no pair after the first was summed (max_t = 0, split chains of at most 5 draws or ρ̂₀ + ρ̂₁ ≤ 0), R's `1:0` reads ρ̂₀ alone, so τ̂ = −1 + 2·1 + 1 = 2 | `.ess`, convergence.R:791–795 |
| The floor of τ̂ | 1 / log₁₀(S′), S′ = 2M·⌊N/2⌋ the split draws (with a warning «The ESS has been capped»): ESS at most S′·log₁₀(S′) | `.ess`, convergence.R:796–805 |
| ESS | S′ / τ̂; NA when a split chain has fewer than 3 draws or the matrix it is given is constant | `.ess`, convergence.R:744–806 |
| Constant | `is_constant`: the range is one value, or its width is at most 2.2·10⁻¹⁶ times the largest absolute value; also NA, NaN or ±Inf anywhere | `should_return_NA`, convergence.R:810–827; `is_constant`, misc.R:198–203 |
| `ess_bulk` | the ESS of the rank-normalized split chains | convergence.R:188–190 |
| `ess_tail` | min(ESS of I(x ≤ q₀.₀₅), ESS of **I(x ≥ q₀.₉₅)**): the quantile of all M·N draws (type 7), the indicator of the upper tail mirrored, then split; NA when x is constant or either indicator is | `ess_tail.default`, convergence.R:226–230; `.ess_quantile`, convergence.R:289–305 |
| `mcse_mean` | sd(x) (all draws, divisor M·N − 1) / √ESS of the split chains of x (not rank-normalized) | convergence.R:469–471, 329–331 |
| `mcse_sd` | s = (x − mean(x))²; √(mean((s − mean(s))²) / ESS(s) / mean(s) / 4), ESS(s) the split chains' (Kenney and Keeping 1951) | convergence.R:504–521 |
| Quantiles | R's `quantile` type 7 on all M·N draws | `quantile2.default`, convergence.R:557–574 |

Where the spec's wording differs, the source above is what the runner does (the spec's decision): the ranks are among the
split draws, not «among all S», with an odd N; `ess_tail`'s upper indicator is I(x ≥ q₀.₉₅), not I(x ≤ q₀.₉₅) (they differ
only by the draws equal to q₀.₉₅: ties and atoms, `witness.json`'s `atom_top`, `ties_*`); the floor is 1/log₁₀ of the split
draws' count. Beyond posterior, the runner gives NULL for every measure when a split chain has fewer than 3 draws (the
contract), for R-hat too, and NULL instead of any infinite or undefined number.

## The source, verbatim

posterior 1.8.0 is licensed under the BSD 3-clause licence; its licence is reproduced at the end of this file, as its
conditions ask of a redistribution of source code.

### `rhat`: the larger of the bulk and the folded R-hat — `R/convergence.R`, lines 148–152

```r
 148  rhat.default <- function(x, ...) {
 149    rhat_bulk <- .rhat(z_scale(.split_chains(x)))
 150    rhat_tail <- .rhat(z_scale(.split_chains(fold_draws(x))))
 151    max(rhat_bulk, rhat_tail)
 152  }
```

### `ess_bulk` — `R/convergence.R`, lines 188–190

```r
 188  ess_bulk.default <- function(x, ...) {
 189    .ess(z_scale(.split_chains(x)))
 190  }
```

### `ess_tail` — `R/convergence.R`, lines 226–230

```r
 226  ess_tail.default <- function(x, ...) {
 227    q05_ess <- ess_quantile(x, 0.05)
 228    q95_ess <- ess_quantile(x, 0.95)
 229    min(q05_ess, q95_ess)
 230  }
```

### `.ess_quantile`: the indicator of one quantile — `R/convergence.R`, lines 289–305

```r
 289  # ESS of a single quantile
 290  .ess_quantile <- function(x, prob) {
 291    if (should_return_NA(x)) {
 292      return(NA_real_)
 293    }
 294    x <- as.matrix(x)
 295    if (prob == 1) {
 296      len <- length(x)
 297      prob <- (len - 0.5) / len
 298    }
 299    q <- quantile(x, prob)
 300    # the upper-tail indicator mirrors the lower-tail indicator, so that for
 301    # discrete draws the result for x at prob equals the result for -x at
 302    # 1 - prob
 303    I <- if (prob > 0.5) x >= q else x <= q
 304    .ess(.split_chains(I))
 305  }
```

### `ess_mean` — `R/convergence.R`, lines 329–331

```r
 329  ess_mean.default <- function(x, ...) {
 330    .ess(.split_chains(x))
 331  }
```

### `ess_sd` — `R/convergence.R`, lines 363–368

```r
 363  ess_sd.default <- function(x, ...) {
 364    # var/sd are not a simple expectation of g(X), e.g. variance
 365    # has (X-E[X])^2. The following ESS is based on a relevant quantity
 366    # in the computation and is empirically a good choice.
 367    .ess(.split_chains((x-mean(x))^2))
 368  }
```

### `mcse_mean` — `R/convergence.R`, lines 469–471

```r
 469  mcse_mean.default <- function(x, ...) {
 470    sd(x) / sqrt(ess_mean(x))
 471  }
```

### `mcse_sd` — `R/convergence.R`, lines 504–521

```r
 504  mcse_sd.default <- function(x, ...) {
 505    # var/sd are not a simple expectation of g(X), e.g. variance
 506    # has (X-E[X])^2. The following ESS is based on a relevant quantity
 507    # in the computation and is empirically a good choice.
 508    sims_c <- x - mean(x)
 509    sims_sq <- sims_c^2
 510    ess <- ess_mean(sims_sq)
 511    # Variance of variance estimate by Kenney and Keeping (1951, p. 141),
 512    # which doesn't assume normality of sims.
 513    Evar <- mean(sims_sq)
 514    varvar <- mean((sims_sq - Evar)^2) / ess
 515    # The first order Taylor series approximation of variance of sd.
 516    # Kenney and Keeping (1951, p. 141) write "...since fluctuations of
 517    # any moment are of order N^{-1/2}, squares and higher powers of
 518    # differentials of the moments can be neglected "
 519    varsd <- varvar / Evar / 4
 520    sqrt(varsd)
 521  }
```

### `quantile2` (R's `quantile`, type 7 by default) — `R/convergence.R`, lines 557–574

```r
 557  quantile2.default <- function(
 558    x, probs = c(0.05, 0.95), na.rm = FALSE, names = TRUE, ...
 559  ) {
 560    names <- as_one_logical(names)
 561    na.rm <- as_one_logical(na.rm)
 562    if (!na.rm && anyNA(x)) {
 563      # quantile itself doesn't handle this case (#110)
 564      out <- rep(NA_real_, length(probs))
 565    } else {
 566      out <- quantile(x, probs = probs, na.rm = na.rm, ...)
 567    }
 568    if (names) {
 569      names(out) <- paste0("q", probs * 100)
 570    } else {
 571      names(out) <- NULL
 572    }
 573    out
 574  }
```

### `autocovariance` — `R/convergence.R`, lines 596–612

```r
 596  autocovariance <- function(x) {
 597    N <- length(x)
 598    varx <- var(x)
 599    if (varx==0) {
 600      # if variance is 0, then all autocovariances are 0
 601      return(rep(0, N))
 602    }
 603    # zero padding makes fft() much faster when N > 1000
 604    M <- nextn(N)
 605    Mt2 <- 2 * M
 606    yc <- x - mean(x)
 607    yc <- c(yc, rep.int(0, Mt2 - N))
 608    # FFT based unnormalized autocovariances
 609    ac <- Re(fft(abs(fft(yc))^2, inverse = TRUE)[1:N])
 610    # use "biased" estimate as recommended by Geyer (1992)
 611    # direct scaling with var(x) avoids need to compute "mask effect"
 612    ac <- ac / ac[1] * varx * (N - 1) / N
```

### `z_scale`: the rank normalization — `R/convergence.R`, lines 645–654

```r
 645  z_scale <- function(x, c = 3/8) {
 646    r <- rank(as.array(x), ties.method = 'average')
 647    z <- qnorm(backtransform_ranks(r, c = c))
 648    z[is.na(x)] <- NA
 649    if (!is.null(dim(x))) {
 650      # output should have the input dimension
 651      z <- array(z, dim = dim(x), dimnames = dimnames(x))
 652    }
 653    z
 654  }
```

### `backtransform_ranks` and `fold_draws` — `R/convergence.R`, lines 708–720

```r
 708  backtransform_ranks <- function(r, c = 3/8) {
 709    c <- as_one_numeric(c)
 710    S <- length(r)
 711    (r - c) / (S - 2 * c + 1)
 712  }
 713
 714  #' Fold draws around their median
 715  #' @template args-conv
 716  #' @return An array or vector of folded draws.
 717  #' @noRd
 718  fold_draws <- function(x) {
 719    abs(x - median(x))
 720  }
```

### `.rhat`, `.ess` and `should_return_NA` — `R/convergence.R`, lines 726–827

```r
 726  .rhat <- function(x) {
 727    x <- as.matrix(x)
 728    if (should_return_NA(x)) {
 729      return(NA_real_)
 730    }
 731    nchains <- NCOL(x)
 732    niterations <- NROW(x)
 733    chain_mean <- matrixStats::colMeans2(x)
 734    chain_var <- matrixStats::colVars(x, center=chain_mean)
 735    var_between <- niterations * var(chain_mean)
 736    var_within <- mean(chain_var)
 737    sqrt((var_between / var_within + niterations - 1) / niterations)
 738  }
 739
 740  #' Compute the effective sample size
 741  #' @template args-conv
 742  #' @template return-conv
 743  #' @noRd
 744  .ess <- function(x) {
 745    x <- as.matrix(x)
 746    nchains <- NCOL(x)
 747    niterations <- NROW(x)
 748    if (niterations < 3L || should_return_NA(x)) {
 749      return(NA_real_)
 750    }
 751    acov <- apply(x, 2, autocovariance)
 752    acov_means <- matrixStats::rowMeans2(acov)
 753    mean_var <- acov_means[1] * niterations / (niterations - 1)
 754    var_plus <- mean_var * (niterations - 1) / niterations
 755    if (nchains > 1) {
 756      var_plus <- var_plus + var(matrixStats::colMeans2(x))
 757    }
 758
 759    # Geyer's initial positive sequence
 760    rho_hat_t <- rep.int(0, niterations)
 761    t <- 0
 762    rho_hat_even <- 1
 763    rho_hat_t[t + 1] <- rho_hat_even
 764    rho_hat_odd <- 1 - (mean_var - acov_means[t + 2]) / var_plus
 765    rho_hat_t[t + 2] <- rho_hat_odd
 766    while (t < NROW(acov) - 5 && !is.nan(rho_hat_even + rho_hat_odd) &&
 767           (rho_hat_even + rho_hat_odd > 0)) {
 768      t <- t + 2
 769      rho_hat_even = 1 - (mean_var - acov_means[t + 1]) / var_plus
 770      rho_hat_odd = 1 - (mean_var - acov_means[t + 2]) / var_plus
 771      if ((rho_hat_even + rho_hat_odd) >= 0) {
 772        rho_hat_t[t + 1] <- rho_hat_even
 773        rho_hat_t[t + 2] <- rho_hat_odd
 774      }
 775    }
 776    max_t <- t
 777    # this is used in the improved estimate
 778    if (rho_hat_even>0)
 779      rho_hat_t[max_t + 1] <- rho_hat_even
 780
 781    # Geyer's initial monotone sequence
 782    t <- 0
 783    while (t <= max_t - 4) {
 784      t <- t + 2
 785      if (rho_hat_t[t + 1] + rho_hat_t[t + 2] >
 786          rho_hat_t[t - 1] + rho_hat_t[t]) {
 787        rho_hat_t[t + 1] = (rho_hat_t[t - 1] + rho_hat_t[t]) / 2;
 788        rho_hat_t[t + 2] = rho_hat_t[t + 1];
 789      }
 790    }
 791    ess <- nchains * niterations
 792    # Geyer's truncated estimate
 793    # tau_hat <- -1 + 2 * sum(rho_hat_t[1:max_t])
 794    # Improved estimate reduces variance in antithetic case
 795    tau_hat <- -1 + 2 * sum(rho_hat_t[1:max_t]) + rho_hat_t[max_t+1]
 796    # Safety check for negative values and with max ess equal to ess*log10(ess)
 797    tau_bound <- 1 / log10(ess)
 798    if (tau_hat < tau_bound) {
 799      warning_no_call(
 800        "The ESS has been capped to avoid unstable estimates.",
 801        class = "posterior_warning_ess_capped"
 802      )
 803      tau_hat <- tau_bound
 804    }
 805    ess <- ess / tau_hat
 806    ess
 807  }
 808
 809  # should NA be returned by a convergence diagnostic?
 810  should_return_NA <- function(x, tol = .Machine$double.eps) {
 811    if (anyNA(x) || checkmate::anyInfinite(x)) {
 812      return(TRUE)
 813    }
 814    # checking for constant input per chain is too conservative for ess_tail
 815    # so we don't check this for the time being until we find a better solution
 816    # if (is.matrix(x)) {
 817    #   # is_constant() vectorized over x columns
 818    #   if (is.logical(x)) {
 819    #     return(any(matrixStats::colAnys(x) == matrixStats::colAlls(x)))
 820    #   } else if (is.integer(x)) {
 821    #     return(any(matrixStats::rowDiffs(matrixStats::colRanges(x)) == 0L))
 822    #   } else {
 823    #     return(any(abs(matrixStats::rowDiffs(matrixStats::colRanges(x))) < tol))
 824    #   }
 825    # }
 826    is_constant(x, tol = tol)
 827  }
```

### `is_constant` — `R/misc.R`, lines 198–203

```r
 198  is_constant <- function(x, tol = .Machine$double.eps) {
 199    r <- range(x)
 200    spread <- r[2] - r[1]
 201    # the equality check covers all-zero and all-infinite x
 202    r[1] == r[2] || (is.finite(spread) && spread <= tol * max(abs(r)))
 203  }
```

### `.split_chains` — `R/split_chains.R`, lines 48–56

```r
  48  .split_chains <- function(x) {
  49    x <- as.matrix(x)
  50    niter <- NROW(x)
  51    if (niter == 1L) {
  52      return(x)
  53    }
  54    half <- niter / 2
  55    cbind(x[1:floor(half), ], x[ceiling(half + 1):niter, ])
  56  }
```

## posterior's licence

The file `LICENSE` of posterior 1.8.0:

```text
YEAR: 2021
COPYRIGHT HOLDER: posterior package authors; Stan Developers and their Assignees; Trustees of Columbia University
ORGANIZATION: copyright holder
```

The licence it names (`License: BSD_3_clause + file LICENSE` in its DESCRIPTION), R's `share/licenses/BSD_3_clause`:

```text
Based on <https://opensource.org/licenses/BSD-3-Clause>

This is a template.  Provide completed entries of the form

YEAR:
COPYRIGHT HOLDER: 
ORGANIZATION:

(separated by empty lines) as file LICENSE, and specify

License: BSD_3_clause + file LICENSE

in file DESCRIPTION.  

(Use

ORGANIZATION: copyright holder

if there is no appropriate organization.)

************************************************************************

Copyright (c) <YEAR>, <COPYRIGHT HOLDER>

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

    Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

    Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.

    Neither the name of the <ORGANIZATION> nor the names of its
    contributors may be used to endorse or promote products derived
    from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

```
