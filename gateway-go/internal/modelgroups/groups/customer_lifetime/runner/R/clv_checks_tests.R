# The self-test of the checks (clv_checks.R, lot B2): added to TAB_COMMON_TESTS. Each verdict at both
# sides of what decides it, on fits made by hand (no optimizer runs here): a test's p-value against the
# node's alpha, a fact's two states; the measures with a value and no verdict.

.clv_check_fit <- function(model = "bg_nbd", par = c(r = 0.25, alpha = 4, a = 0.8, b = 2.5), spend = c(p = 6, q = 4, gamma = 15),
                           alpha = 0.05, p_value = 0.03, df = 3, holdout = -0.12, vcov = TRUE, converged = TRUE) {
  set.seed(4)
  n <- 200
  x <- stats::rpois(n, 1.5)
  m <- ifelse(x > 0, stats::rgamma(n, 20, rate = 1), NA_real_)
  k <- length(par)
  list(model = model, alpha = alpha, plan = list(horizon = 52),
       s = list(x = x, m = m, T = stats::runif(n, 20, 40), n = n, has_holdout = !is.null(holdout)),
       purchases = list(par = par, converged = converged, vcov_log = if (vcov) diag(0.01, k) else NULL, gradient = rep(1e-4, k)),
       spend = if (is.null(spend)) NULL else list(par = spend, converged = TRUE, vcov_log = diag(0.01, 3)),
       freq = list(statistic = 9.1, df = df, p_value = p_value, max_share_gap = 0.021, n_classes = df + 1 + k),
       holdout = if (is.null(holdout)) NULL else list(rel_error = holdout))
}

.clv_check_frame <- function(fit, variant = if (is.null(fit$spend)) "purchases" else "value") {
  rows <- tab_rows_start(list(model = "clv", variant = variant, rows_from = "run", group_version = "selftest"))
  tab_number(rows, "model", "n", fit$s$n)
  clv_checks(rows, fit)
  problems <- tab_check_declared(rows, "clv", variant)
  .tab_expect(!length(problems), paste(head(problems, 3L), collapse = "; "))
  df <- tab_results_frame(rows)
  df[df$section == "assumption", ]
}

.clv_check_get <- function(a, id, stat, level_2 = "") {
  at <- a$key == paste0("assumption|", id, "|", level_2, "|", stat)
  if (!any(at)) return(NULL)
  if (stat %in% c("verdict", "advice", "advice_id", "h0", "significance")) a$text[at] else a$value[at]
}

TAB_COMMON_TESTS[["customer_lifetime: a verdict is a p-value against the node's alpha or a certain fact, both sides; a measure has none"]] <- function(code) {
  g <- .clv_check_get
  # the χ² of the frequency classes: p 0.03 fails at alpha 0.05, passes at 0.01; no degree of freedom left: not computed
  a <- .clv_check_frame(.clv_check_fit(alpha = 0.05))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "failed") && g(a, "frequency_fit", "threshold") == 0.05 &&
              g(a, "frequency_fit", "value") == 0.021 && g(a, "frequency_fit", "p_value") == 0.03, "frequency_fit at alpha 0.05")
  a <- .clv_check_frame(.clv_check_fit(alpha = 0.01))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "ok") && g(a, "frequency_fit", "threshold") == 0.01, "frequency_fit at alpha 0.01")
  a <- .clv_check_frame(.clv_check_fit(df = 0, p_value = NA_real_))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "not_computed") && is.null(g(a, "frequency_fit", "p_value")) &&
              grepl("no degree of freedom", g(a, "frequency_fit", "advice"), fixed = TRUE), "frequency_fit without a degree of freedom")
  # the spend's independence: its own HC3 p-value, read against alpha on both sides of it
  fit <- .clv_check_fit()
  si <- clv_spend_independence(fit$s$x, fit$s$m)
  .tab_expect(is.finite(si$p_value) && si$p_value > 1e-6 && si$p_value < 1 - 1e-6, "an HC3 p-value inside (0, 1)")
  a <- .clv_check_frame(.clv_check_fit(alpha = min(si$p_value * 1.01, 0.999)))
  .tab_expect(identical(g(a, "spend_independence", "verdict"), "failed") && g(a, "spend_independence", "value") == si$r, "spend_independence below p")
  a <- .clv_check_frame(.clv_check_fit(alpha = si$p_value * 0.99))
  .tab_expect(identical(g(a, "spend_independence", "verdict"), "ok") && g(a, "spend_independence", "count") == sum(fit$s$x > 0), "spend_independence above p")
  # the spend's tail: q above 2 a finite variance, at most 2 none (a fact, its threshold 2)
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2.5, gamma = 15))), "spend_tail", "verdict"), "ok") &&
              identical(g(.clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2, gamma = 15))), "spend_tail", "verdict"), "doubtful"), "spend_tail")
  # the measures: a value, no verdict, no advice, no H0
  a <- .clv_check_frame(.clv_check_fit(holdout = -0.12))
  for (id in c("sample_size", "holdout", "horizon_length")) {
    at <- a$level == id
    .tab_expect(any(at & a$stat == "value") && !any(at & a$stat %in% c("verdict", "advice", "advice_id", "h0")), id, " is not a measure")
  }
  .tab_expect(g(a, "sample_size", "value") == 200 && g(a, "sample_size", "count") == sum(.clv_check_fit()$s$x > 0) &&
              g(a, "holdout", "value") == -0.12 && abs(g(a, "horizon_length", "value") - 52 / max(.clv_check_fit()$s$T)) < 1e-15, "the measures' numbers")
  # nothing bought in the holdout: no denominator, not computed; no holdout column: no row
  a <- .clv_check_frame(.clv_check_fit(holdout = NA_real_))
  .tab_expect(identical(g(a, "holdout", "verdict"), "not_computed"), "holdout without purchases")
  a <- .clv_check_frame(.clv_check_fit(holdout = NULL, spend = NULL))
  .tab_expect(!any(a$level %in% c("holdout", "spend_independence", "spend_tail")), "no holdout, no spend: their rows are not written")
  # convergence: a fact in three states
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit()), "convergence", "verdict"), "ok"), "convergence ok")
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(converged = FALSE)), "convergence", "verdict"), "failed"), "not converged")
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(vcov = FALSE)), "convergence", "verdict"), "failed"), "no Hessian inside the range")
  a <- .clv_check_frame(.clv_check_fit(vcov = FALSE, par = c(r = 0.25, alpha = 4, a = 3e-5, b = 2.5)))
  .tab_expect(identical(g(a, "convergence", "verdict"), "doubtful") && identical(g(a, "convergence", "advice_id"), "convergence_edge"), "no Hessian at an edge")
}

TAB_COMMON_TESTS[["customer_lifetime: the edges of the parameters and what each means"]] <- function(code) {
  g <- .clv_check_get
  a <- .clv_check_frame(.clv_check_fit())
  .tab_expect(identical(g(a, "boundary", "verdict"), "ok") && g(a, "boundary", "count") == 0, "no edge")
  # BG/NBD: a → 0 or b → ∞ is no dropout; r and α both large, a rate with no spread
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4, a = 5e-5, b = 2.5)))
  .tab_expect(identical(g(a, "boundary", "verdict", "a"), "doubtful") && identical(g(a, "boundary", "advice_id", "a"), "boundary_no_dropout") &&
              g(a, "boundary", "value", "a") == 5e-5 && is.null(g(a, "boundary", "verdict")), "BG/NBD a at 0")
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4, a = 0.8, b = 2e4)))
  .tab_expect(identical(g(a, "boundary", "advice_id", "b"), "boundary_no_dropout"), "BG/NBD b at infinity")
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 3e4, alpha = 5e5, a = 0.8, b = 2.5)))
  .tab_expect(identical(g(a, "boundary", "advice_id", "r"), "boundary_homogeneous") && identical(g(a, "boundary", "advice_id", "alpha"), "boundary_homogeneous") &&
              g(a, "boundary", "count", "r") == 2, "a purchase rate with no spread")
  # Pareto/NBD: s and β both large, the same dropout rate for everyone; β alone large, no dropout
  a <- .clv_check_frame(.clv_check_fit(model = "pareto_nbd", par = c(r = 0.5, alpha = 10, s = 2e4, beta = 3e5)))
  .tab_expect(identical(g(a, "boundary", "advice_id", "s"), "boundary_homogeneous") && identical(g(a, "boundary", "advice_id", "beta"), "boundary_homogeneous"),
              "Pareto/NBD s and beta")
  a <- .clv_check_frame(.clv_check_fit(model = "pareto_nbd", par = c(r = 0.5, alpha = 10, s = 0.6, beta = 3e5)))
  .tab_expect(identical(g(a, "boundary", "advice_id", "beta"), "boundary_no_dropout"), "Pareto/NBD beta alone")
  # the spend's parameters are named as the model section names them, with no reading of their own
  a <- .clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2e4, gamma = 15)))
  .tab_expect(identical(g(a, "boundary", "verdict", "spend_q"), "doubtful") && identical(g(a, "boundary", "advice_id", "spend_q"), "boundary"), "the spend's q")
}
