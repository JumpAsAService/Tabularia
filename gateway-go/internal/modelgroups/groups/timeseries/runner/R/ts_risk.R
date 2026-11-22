# The backtest of a 1-step value at risk (docs/specs/garch-risk.md, «The holdout and the backtest»; lot G1), shared by the GARCH
# and the risk simulation nodes. A verdict comes only from a p-value against alpha; the counts are measures.
#
#   ts_backtest(r, var, level, alpha)   the violations I_t = 1 when r_t < VaR_t, their counts, Kupiec's and Christoffersen's tests
#   ts_backtest_rows(rows, bt)          the section `backtest`: one level a test, `counts`
#
# x = Σ I_t violations on T days, p = 1 − level. Kupiec (1995): LR_uc = −2 ln[(1 − p)^{T−x} p^x] + 2 ln[(1 − x/T)^{T−x} (x/T)^x],
# χ²₁. Christoffersen (1998), over the T − 1 transitions i → j (n_ij): π₀₁ = n₀₁/(n₀₀ + n₀₁), π₁₁ = n₁₁/(n₁₀ + n₁₁),
# π = (n₀₁ + n₁₁)/(T − 1); LR_ind = −2 ln[(1 − π)^{n₀₀+n₁₀} π^{n₀₁+n₁₁}] + 2 ln[(1 − π₀₁)^{n₀₀} π₀₁^{n₀₁} (1 − π₁₁)^{n₁₀} π₁₁^{n₁₁}],
# χ²₁; LR_cc = −2 ln[(1 − p)^{n₀₀+n₁₀} p^{n₀₁+n₁₁}] + 2 ln[the Markov likelihood], χ²₂. 0·ln 0 = 0. Not computed: all three with
# T < 2, the independence and the conditional coverage without a violation (nothing to cluster). No minimum size: the expected
# count T·p is said beside every verdict.

# n·ln(q), 0 when n is 0 (whatever q)
.ts_nlog <- function(n, q) if (n == 0) 0 else n * log(q)

ts_backtest <- function(r, var, level, alpha) {
  hit <- as.integer(r < var)
  T <- length(hit)
  x <- sum(hit)
  p <- 1 - level
  pct <- format(signif(p, 10), scientific = FALSE, drop0trailing = TRUE, trim = TRUE)
  h0 <- c(kupiec = paste0("the violation rate is ", pct), christoffersen_ind = "a violation today does not change tomorrow's chance",
          christoffersen_cc = "the rate is right and the violations do not cluster")
  df <- c(kupiec = 1, christoffersen_ind = 1, christoffersen_cc = 2)
  prev <- hit[-T]
  nxt <- hit[-1L]
  n <- c(n00 = sum(prev == 0 & nxt == 0), n01 = sum(prev == 0 & nxt == 1), n10 = sum(prev == 1 & nxt == 0), n11 = sum(prev == 1 & nxt == 1))
  runs <- rle(hit)
  longest <- if (x) max(runs$lengths[runs$values == 1L]) else 0
  lr <- c(kupiec = NA_real_, christoffersen_ind = NA_real_, christoffersen_cc = NA_real_)
  why <- c(kupiec = NA_character_, christoffersen_ind = NA_character_, christoffersen_cc = NA_character_)
  if (T < 2L) {
    why[] <- "the holdout has fewer than 2 days: there is nothing to test"
  } else {
    lr[["kupiec"]] <- -2 * (.ts_nlog(T - x, 1 - p) + .ts_nlog(x, p)) + 2 * (.ts_nlog(T - x, 1 - x / T) + .ts_nlog(x, x / T))
    if (x == 0L) {
      why[c("christoffersen_ind", "christoffersen_cc")] <- "no day broke the VaR, so there is nothing to cluster"
    } else {
      p01 <- n[["n01"]] / (n[["n00"]] + n[["n01"]])
      p11 <- n[["n11"]] / (n[["n10"]] + n[["n11"]])
      pi <- (n[["n01"]] + n[["n11"]]) / (T - 1)
      markov <- .ts_nlog(n[["n00"]], 1 - p01) + .ts_nlog(n[["n01"]], p01) + .ts_nlog(n[["n10"]], 1 - p11) + .ts_nlog(n[["n11"]], p11)
      lr[["christoffersen_ind"]] <- -2 * (.ts_nlog(n[["n00"]] + n[["n10"]], 1 - pi) + .ts_nlog(n[["n01"]] + n[["n11"]], pi)) + 2 * markov
      lr[["christoffersen_cc"]] <- -2 * (.ts_nlog(n[["n00"]] + n[["n10"]], 1 - p) + .ts_nlog(n[["n01"]] + n[["n11"]], p)) + 2 * markov
    }
  }
  tests <- lapply(names(lr), function(id) {
    if (is.na(lr[[id]])) return(list(id = id, h0 = h0[[id]], verdict = "not_computed", why = why[[id]]))
    stat <- max(0, lr[[id]])  # a likelihood ratio is ≥ 0; −1e−15 is rounding
    pv <- stats::pchisq(stat, df[[id]], lower.tail = FALSE)
    list(id = id, h0 = h0[[id]], statistic = stat, df = df[[id]], p_value = pv, verdict = if (pv < alpha) "failed" else "ok")
  })
  names(tests) <- names(lr)
  list(hit = hit, tests = tests, counts = c(violations = x, expected_violations = T * p, violation_rate = if (T) x / T else NA_real_,
                                             n, longest_run = longest), days = T)
}

ts_backtest_rows <- function(rows, bt) {
  for (t in bt$tests) {
    tab_text(rows, "backtest", "h0", t$h0, level = t$id)
    if (!is.null(t$statistic)) {
      tab_number(rows, "backtest", "statistic", t$statistic, level = t$id)
      tab_number(rows, "backtest", "df", t$df, level = t$id)
      tab_p(rows, "backtest", t$p_value, level = t$id)
    }
    tab_text(rows, "backtest", "verdict", t$verdict, level = t$id)
  }
  for (k in names(bt$counts)) tab_number(rows, "backtest", k, bt$counts[[k]], level = "counts")
  invisible(rows)
}
