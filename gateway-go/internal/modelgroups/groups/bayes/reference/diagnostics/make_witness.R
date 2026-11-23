# The first witness of the diagnostics (docs/specs/bayes-diagnostics.md, «Witnesses», lot X0): the R package posterior
# 1.8.0 (BSD-3-Clause) on the draws of cases.json, the numbers the runner's bayes_diag.py must give (the spec's decision:
# Vehtari et al. 2021 as posterior 1.8.0 implements them; definitions.md transcribes its source). It writes witness.json
# afresh: the versions and, per case, posterior's numbers; make_witness.py then adds ArviZ's beside them. Never installed in
# the group's image; run by hand, rarely, never in CI. One way, in a throw-away container of any rocker-based R 4.6 image
# (the R groups' images are), with a scratch library holding posterior 1.8.0 from CRAN's tarball and its imports:
#
#   docker run --rm --memory 2g --cpus 2 --user "$(id -u):$(id -g)" -e HOME=/tmp -e R_LIBS_USER=/wl \
#     -v <scratch library>:/wl -v "$PWD/gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics:/ref" \
#     <R image> Rscript /ref/make_witness.R
#
# Every number is written with 17 significant digits (checked by reading the file back). NA becomes null.

suppressMessages({
  library(posterior)
  library(jsonlite)
})
stopifnot(as.character(packageVersion("posterior")) == "1.8.0")

here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
cases <- fromJSON(file.path(here, "cases.json"), simplifyVector = TRUE, simplifyMatrix = TRUE)

# a case's draws as posterior reads a variable: iterations x chains
as_draws <- function(chains) {
  m <- if (is.matrix(chains)) chains else matrix(chains, nrow = 1)
  t(m)
}
num <- function(v) if (is.null(v) || length(v) != 1 || is.na(v)) NA_real_ else as.numeric(v)

measures <- function(x) {
  warnings <- character()
  out <- withCallingHandlers({
    q <- quantile2(x, probs = c(0.05, 0.25, 0.5, 0.75, 0.95), names = FALSE)
    split <- posterior:::.split_chains(x)
    list(
      chains = ncol(x), draws = nrow(x),
      mean = mean(x), sd = sd(x),
      q5 = q[1], q25 = q[2], q50 = q[3], q75 = q[4], q95 = q[5],
      rhat = num(rhat(x)),
      rhat_bulk = num(posterior:::.rhat(z_scale(split))),
      rhat_tail = num(posterior:::.rhat(z_scale(posterior:::.split_chains(posterior:::fold_draws(x))))),
      ess_bulk = num(ess_bulk(x)),
      ess_tail = num(ess_tail(x)),
      ess_q5 = num(ess_quantile(x, 0.05, names = FALSE)),
      ess_q95 = num(ess_quantile(x, 0.95, names = FALSE)),
      ess_mean = num(ess_mean(x)),
      ess_sd = num(ess_sd(x)),
      mcse_mean = num(mcse_mean(x)),
      mcse_sd = num(mcse_sd(x)),
      mcse_q5 = num(mcse_quantile(x, 0.05, names = FALSE)),
      mcse_q95 = num(mcse_quantile(x, 0.95, names = FALSE))
    )
  }, warning = function(w) {
    warnings <<- c(warnings, conditionMessage(w))
    invokeRestart("muffleWarning")
  })
  out$warnings <- I(unique(warnings))
  out
}

results <- list()
for (i in seq_len(nrow(cases$cases))) {
  name <- cases$cases$name[i]
  x <- as_draws(cases$cases$draws[[i]])
  results[[name]] <- measures(x)
}

deps <- c("posterior", "jsonlite", "abind", "checkmate", "rlang", "tibble", "vctrs", "tensorA", "pillar", "distributional",
          "matrixStats", "backports", "cli", "glue", "lifecycle", "magrittr", "pkgconfig", "fansi", "utf8", "generics",
          "numDeriv")
installed <- installed.packages()
versions <- vapply(deps[deps %in% rownames(installed)], function(p) paste0(p, "@", installed[p, "Version"]), "")

doc <- list(
  about = paste(
    "The witnesses of the diagnostics (docs/specs/bayes-diagnostics.md, «Witnesses»): the R package posterior 1.8.0 and",
    "ArviZ on the draws of cases.json, written by make_witness.R (posterior's numbers, the versions) and make_witness.py",
    "(ArviZ's, the differences between the two). Never installed in the group's image; not part of the recipe, so changing",
    "them builds nothing. bayes_diag.py must equal posterior's numbers within the spec's tolerances (test_diag.py)."),
  checked_on = format(Sys.Date()),
  r = c(R.version.string, unname(versions)),
  posterior = list(
    tarball_sha256 = "b89ba062c762bdc02ae368bb502f9e389cbd0cc34b73f75064441e45f4572dbf",
    cases = results
  )
)
text <- toJSON(doc, auto_unbox = TRUE, digits = I(17), na = "null", null = "null", pretty = TRUE)
out <- file.path(here, "witness.json")
writeLines(text, out, useBytes = TRUE)

# every number read back equal to the one computed
back <- fromJSON(out, simplifyVector = TRUE)
for (name in names(results)) for (k in names(results[[name]])) {
  a <- results[[name]][[k]]
  if (is.numeric(a) && !identical(as.numeric(a), as.numeric(if (is.null(back$posterior$cases[[name]][[k]])) NA_real_ else back$posterior$cases[[name]][[k]]))) {
    stop(sprintf("%s %s: %.17g written as %s", name, k, a, format(back$posterior$cases[[name]][[k]], digits = 17)))
  }
}
cat(sprintf("witness.json: posterior %s on %d cases\n", packageVersion("posterior"), length(results)))
