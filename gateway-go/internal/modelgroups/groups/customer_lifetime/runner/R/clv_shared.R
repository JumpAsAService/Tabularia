# What the two nodes of the group share (docs/specs/customer-lifetime-models.md, «The group
# customer_lifetime — R»): the summary's checks, the optimizer, the standard errors, Gamma–Gamma,
# the discounting and the CLV, the holdout, the chi-square, the P(alive) grid, the charts' points,
# the card and the rows. Lot B1 writes them here; lot B0 writes only the names of the CLVTools
# functions the runner binds.
#
# CLVTools' fitting functions take a transaction log, and the nodes take a summary, one row per
# customer. The summary is all the data the likelihoods ever see, so the runner calls CLVTools' own
# compiled likelihoods on it, with CLVTools' own optimizer settings. Those functions are documented in
# CLVTools' manual and are not exported: the runner reaches them by name in the version the recipe
# pins (0.12.1), and the self-test compares the estimates they give with the public pnbd(), bgnbd() and
# gg() on a transaction log rebuilt from a whole-day reference case: a later version that renames
# or changes one of them fails the build's check instead of the numbers.

CLV_CLVTOOLS_FUNCTIONS <- c(
  # Pareto/NBD
  "pnbd_nocov_LL_ind", "pnbd_nocov_LL_sum", "pnbd_nocov_PAlive", "pnbd_nocov_CET", "pnbd_nocov_DERT",
  "pnbd_nocov_PMF", "pnbd_nocov_expectation",
  # BG/NBD (no DERT: CLVTools does not derive that expression)
  "bgnbd_nocov_LL_ind", "bgnbd_nocov_LL_sum", "bgnbd_nocov_PAlive", "bgnbd_nocov_CET", "bgnbd_nocov_PMF",
  "bgnbd_nocov_expectation",
  # Gamma–Gamma, and GSL's 2F1 with its status
  "gg_LL", "vec_gsl_hyp2f1_e"
)

# One of CLVTools' functions by name; a fault of the image, said as one, when it is not there.
clv_clvtools <- function(name) {
  found <- tryCatch(utils::getFromNamespace(name, "CLVTools"), error = function(e) NULL)
  if (!is.function(found)) {
    tab_refuse("clvtools_missing", paste0("the image's CLVTools lacks ", name, ": rebuild the group"), list(function_name = name))
  }
  found
}

# The names among CLV_CLVTOOLS_FUNCTIONS that this image's CLVTools does not have.
clv_missing_clvtools <- function() {
  CLV_CLVTOOLS_FUNCTIONS[!vapply(CLV_CLVTOOLS_FUNCTIONS, function(name) {
    is.function(tryCatch(utils::getFromNamespace(name, "CLVTools"), error = function(e) NULL))
  }, logical(1))]
}
