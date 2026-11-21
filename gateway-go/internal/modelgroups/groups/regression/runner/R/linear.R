# Linear regression: ordinary and weighted least squares, with classical, heteroscedasticity-
# consistent (HC0–HC3), HAC (Newey–West) or clustered standard errors, and the checks of its
# assumptions (docs/specs/statistical-models.md, `linear`, lane M1). With a penalty the route
# goes to penalized.R instead.
#
# The fit is stats::lm on the design of tab_design, whose columns have positional names (d1,
# d2…): no column's name ever reaches a formula. The covariance comes from sandwich, the tests
# of the coefficients from lmtest::coeftest and coefci with that covariance; the checks call
# lmtest, nortest and stats as the specification's table says. A check that cannot be computed
# on these rows is a row «not_computed» with the reason, never a crash of the node.
#
# Weighted least squares: the packages that ignore weights (lmtest's RESET, Durbin–Watson and
# Breusch–Godfrey refit unweighted) are not called on the weighted fit: the RESET is the F test
# of the weighted fit with the powers of the fitted values added, and the tests of independence
# run on the transformed regression (√w·y on √w·X), whose residuals are the weighted residuals.
# The normality checks and the charts read the weighted residuals (√w·e, as plot.lm does).

LINEAR_PACKAGES <- c("sandwich", "lmtest", "nortest")
LINEAR_LABELLED <- 3L        # points named on a chart: the largest, as plot.lm names them
LINEAR_VIF_DOUBTFUL <- 5
LINEAR_VIF_FAILED <- 10
LINEAR_KAPPA_DOUBTFUL <- 30  # Belsley's condition number of the scaled design
LINEAR_KAPPA_FAILED <- 100
LINEAR_MAX_SHAPIRO <- 5000

# The node's parameters with the catalogue's defaults under them (a task written by hand, or a
# reference case, may give only some).
linear_params <- function(params) {
  p <- tab_model_spec("linear")$defaults
  for (k in names(params)) p[k] <- list(params[[k]])
  p
}

fit_linear <- function(task) {
  p <- linear_params(task$params)
  alpha <- as.numeric(p$alpha %||% 0.05)
  outcome <- p$outcome
  predictors <- unlist(p$predictors)
  wls <- identical(p$estimation, "wls")
  weights_col <- if (wls) p$weights else NULL
  cluster_col <- if (identical(p$se, "cluster")) p$cluster else NULL
  order_col <- p$order_by
  has_int <- !isFALSE(p$intercept)
  rows <- tab_rows_start(task)

  df <- tab_input(task)
  for (column in c(outcome, predictors, weights_col, cluster_col, order_col)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  if (!tab_kind(df[[outcome]]) %in% c("number", "integer")) {
    tab_refuse("outcome_not_number", paste0("the outcome «", outcome, "» is not a number: a linear regression explains a number ",
                                            "(for two values use the logistic regression)"), list(column = outcome))
  }
  if (!is.null(weights_col) && !tab_kind(df[[weights_col]]) %in% c("number", "integer")) {
    tab_refuse("weights_not_number", paste0("the weights «", weights_col, "» are not numbers"), list(column = weights_col))
  }
  design <- tab_design(df, predictors, outcome = outcome, reference = p$reference %||% list(),
                       interactions = p$interactions %||% list(), intercept = has_int,
                       also = c(weights_col, cluster_col, order_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  w <- NULL
  zero_weights <- 0L
  if (wls) {
    w <- as.numeric(design$data[[weights_col]])
    if (any(w < 0)) {
      tab_refuse("weights_negative", paste0("the weights «", weights_col, "» hold a negative value: a weight is 0 or more"),
                 list(column = weights_col))
    }
    zero_weights <- sum(w == 0)
    if (zero_weights > 0L) {
      design <- linear_subset(design, w > 0)
      w <- w[w > 0]
    }
  }
  k <- ncol(design$X)
  if (design$n < k + 2L) {
    tab_refuse("too_few_rows", paste0("the model has ", k, " terms and ", design$n, " rows it can use: it needs at least ", k + 2L,
                                      " rows (more rows than terms + 1)"), list(rows = design$n, terms = k))
  }
  tab_refuse_aliased(design)

  # ── the fit, on positional names ──────────────────────────────────────────
  y <- as.numeric(design$y)
  X <- design$X
  Xn <- if (has_int) X[, -1L, drop = FALSE] else X
  dat <- data.frame(y = y, Xn, check.names = FALSE)
  rownames(dat) <- NULL
  form <- stats::reformulate(colnames(Xn), response = "y", intercept = has_int)
  m <- if (is.null(w)) stats::lm(form, data = dat) else {
    dat$.w <- w
    stats::lm(form, data = dat, weights = .w)
  }
  s <- summary(m)
  b <- stats::coef(m)
  n <- design$n
  df_res <- m$df.residual
  order_by <- if (is.null(order_col)) NULL else design$data[[order_col]]

  V0 <- stats::vcov(m)
  V <- switch(p$se %||% "classical",
    classical = V0,
    hc0 = sandwich::vcovHC(m, type = "HC0"),
    hc1 = sandwich::vcovHC(m, type = "HC1"),
    hc2 = sandwich::vcovHC(m, type = "HC2"),
    hc3 = sandwich::vcovHC(m, type = "HC3"),
    hac = if (is.null(order_by)) sandwich::NeweyWest(m) else sandwich::NeweyWest(m, order.by = order_by),
    cluster = sandwich::vcovCL(m, cluster = design$data[[cluster_col]]),
    tab_refuse("se", paste0("unknown standard errors «", p$se, "»")))
  ct <- lmtest::coeftest(m, vcov. = V)
  ci <- lmtest::coefci(m, vcov. = V, level = 1 - alpha)

  # ── the model section ─────────────────────────────────────────────────────
  tab_text(rows, "model", "formula", design$formula)
  tab_text(rows, "model", "family", "gaussian")
  tab_text(rows, "model", "link", "identity")
  tab_text(rows, "model", "se_type", p$se %||% "classical")
  tab_text(rows, "model", "software", tab_software(LINEAR_PACKAGES))
  tested <- if (has_int) seq_along(b)[-1L] else seq_along(b)
  q <- length(tested)
  if (identical(p$se %||% "classical", "classical")) {
    f_stat <- unname(s$fstatistic[["value"]])
  } else {  # a Wald test of every coefficient but the intercept, with the chosen covariance
    bt <- b[tested]
    f_stat <- as.numeric(crossprod(bt, solve(V[tested, tested, drop = FALSE], bt))) / q
  }
  f_p <- stats::pf(f_stat, q, df_res, lower.tail = FALSE)
  e <- y - stats::fitted(m)
  kappa_ <- linear_condition_number(X, w)
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "n_missing", design$n_missing)
  tab_number(rows, "model", "df_model", q)
  tab_number(rows, "model", "df_residual", df_res)
  tab_number(rows, "model", "r_squared", s$r.squared)
  tab_number(rows, "model", "adj_r_squared", s$adj.r.squared)
  tab_number(rows, "model", "sigma", s$sigma)
  tab_number(rows, "model", "f_statistic", f_stat)
  tab_number(rows, "model", "f_df_1", q)
  tab_number(rows, "model", "f_df_2", df_res)
  tab_p(rows, "model", f_p, stat = "f_p_value")
  tab_number(rows, "model", "log_lik", as.numeric(stats::logLik(m)))
  tab_number(rows, "model", "aic", stats::AIC(m))
  tab_number(rows, "model", "bic", stats::BIC(m))
  tab_number(rows, "model", "rmse", sqrt(mean(e^2)))
  tab_number(rows, "model", "mae", mean(abs(e)))
  tab_number(rows, "model", "condition_number", kappa_)

  # ── coefficients ──────────────────────────────────────────────────────────
  vif <- linear_vif(V0, design$terms, has_int)
  terms <- design$terms
  for (j in seq_along(b)) {
    level <- terms$term[[j]]
    level_2 <- if (is.na(terms$level[[j]])) NULL else terms$level[[j]]
    tab_number(rows, "coef", "estimate", ct[j, 1L], level, level_2)
    tab_number(rows, "coef", "std_error", ct[j, 2L], level, level_2)
    tab_number(rows, "coef", "statistic", ct[j, 3L], level, level_2)
    tab_number(rows, "coef", "df", df_res, level, level_2)
    tab_p(rows, "coef", ct[j, 4L], level = level, level_2 = level_2)
    tab_number(rows, "coef", "ci_low", ci[j, 1L], level, level_2)
    tab_number(rows, "coef", "ci_high", ci[j, 2L], level, level_2)
    at <- match(terms$term_index[[j]], vif$term_index)
    if (!is.na(at)) tab_number(rows, "coef", "vif", vif$value[[at]], level, level_2)
  }

  # ── the assumptions ───────────────────────────────────────────────────────
  infl <- list(h = stats::hatvalues(m), cooks = stats::cooks.distance(m), std = stats::rstandard(m),
               student = stats::rstudent(m))
  wr <- stats::weighted.residuals(m)
  linear_assumptions(rows, m = m, p = p, alpha = alpha, w = w, X = X, y = y, order_by = order_by, vif = vif,
                     kappa_ = kappa_, infl = infl, wr = wr, row_numbers = design$rows)

  # ── the charts' points ────────────────────────────────────────────────────
  linear_charts(rows, task, m, y, wr, infl, design$rows)

  # ── notes ─────────────────────────────────────────────────────────────────
  # each note an id and its values, which the report words in the page's language (linear_note); the English here
  if (length(p$interactions %||% list())) {
    # «The terms of an interaction are correlated with their parts by construction: their VIFs are large for that
    # reason; centre the predictors to read them.»
    tab_note(rows, linear_note("interaction_vif"))
  }
  if (!wls && !is.null(p$weights)) {
    # «The weights «{weights}» are not used with ordinary least squares: choose estimation = wls to use them.»
    tab_note(rows, linear_note("weights_unused", weights = p$weights))
  }
  if (wls) {
    # «Weighted least squares: the normality checks, the tests of independence and the charts read the weighted
    # residuals (√w × residual); the RESET is the F test of the weighted fit.»
    tab_note(rows, linear_note("wls"))
  }
  if (zero_weights > 0L) {
    # «{n} rows have weight 0 and were left out of the fit.» (one row: zero_weight)
    tab_note(rows, linear_note(if (zero_weights == 1L) "zero_weight" else "zero_weights", n = zero_weights))
  }
  if (identical(p$se, "hac") && is.null(order_col)) {
    # «The HAC standard errors read the rows in the order they reached the node: choose order_by to make that order
    # explicit.»
    tab_note(rows, linear_note("hac_order"))
  }
  if (is.finite(s$sigma) && s$sigma <= sqrt(.Machine$double.eps) * max(1, stats::sd(y))) {
    # «The model fits the rows exactly (σ is 0): a predictor repeats the outcome, or there are as many terms as rows;
    # the tests mean nothing here.»
    tab_note(rows, linear_note("exact_fit"))
  }

  # ── the card, and the rows output ─────────────────────────────────────────
  card <- tab_card(task, design, LINEAR_PACKAGES)
  card$outcome <- list(name = outcome)
  card$coefficients <- as.list(unname(b))
  card$vcov <- tab_card_vcov(task, unname(V))
  card$link <- "identity"
  card$alpha <- alpha
  card$linear <- list(sigma = s$sigma, df_residual = df_res)
  linear_check_card(task, card, design, stats::fitted(m))

  output <- NULL
  if (identical(task$output %||% p$output, "rows")) {
    output <- linear_rows_output(df, card, design, m, w, infl, V)
  }
  list(rows = rows, output = output, card = card, native = m)
}

# The design without the rows `keep` drops (rows of weight 0).
linear_subset <- function(design, keep) {
  design$X <- design$X[keep, , drop = FALSE]
  design$y <- design$y[keep]
  design$data <- design$data[keep, , drop = FALSE]
  design$rows <- design$rows[keep]
  design$n <- sum(keep)
  design
}

# Belsley's condition number: the ratio of the largest to the smallest singular value of the
# design (weighted: √w·X) with each column scaled to unit length, the intercept included.
linear_condition_number <- function(X, w = NULL) {
  Xw <- if (is.null(w)) X else X * sqrt(w)
  norms <- sqrt(colSums(Xw^2))
  if (any(norms == 0)) return(NA_real_)
  d <- svd(sweep(Xw, 2L, norms, "/"), nu = 0L, nv = 0L)$d
  max(d) / min(d)
}

# The variance inflation of each term, as car::vif computes it from the coefficients'
# correlations (Fox and Monette's generalized VIF for a term of several columns): GVIF, its
# degrees of freedom, and the value read against 5 and 10, GVIF^(1/df) (the VIF itself for a
# term of one column). The intercept has none.
linear_vif <- function(V0, terms, has_int) {
  cols <- if (has_int) seq_len(nrow(V0))[-1L] else seq_len(nrow(V0))
  out <- data.frame(term_index = integer(), term = character(), gvif = numeric(), df = integer(), value = numeric())
  if (!length(cols)) return(out)
  R <- stats::cov2cor(V0[cols, cols, drop = FALSE])
  det_r <- det(R)
  index <- terms$term_index[cols]
  for (t in unique(index)) {
    sub <- which(index == t)
    gvif <- det(R[sub, sub, drop = FALSE]) * det(R[-sub, -sub, drop = FALSE]) / det_r
    out[nrow(out) + 1L, ] <- list(t, terms$term[cols][sub[[1L]]], gvif, length(sub), gvif^(1 / length(sub)))
  }
  out
}

linear_verdict_p <- function(pv, alpha) if (is.na(pv)) "not_computed" else if (pv < alpha) "failed" else "ok"

# One check's rows, in the order the report reads them.
linear_check <- function(rows, id, h0, verdict, advice, level_2 = NULL, statistic = NULL, df = NULL, df_2 = NULL, p = NULL,
                         threshold = NULL, value = NULL, count = NULL, at_rows = NULL) {
  section <- "assumption"
  tab_text(rows, section, "h0", h0, id, level_2)
  if (!is.null(statistic)) tab_number(rows, section, "statistic", statistic, id, level_2)
  if (!is.null(df)) tab_number(rows, section, "df", df, id, level_2)
  if (!is.null(df_2)) tab_number(rows, section, "df_2", df_2, id, level_2)
  if (!is.null(p)) tab_p(rows, section, p, level = id, level_2 = level_2)
  if (!is.null(threshold)) tab_number(rows, section, "threshold", threshold, id, level_2)
  if (!is.null(value)) tab_number(rows, section, "value", value, id, level_2)
  if (!is.null(count)) tab_number(rows, section, "count", count, id, level_2)
  tab_text(rows, section, "verdict", verdict, id, level_2)
  tab_text(rows, section, "advice", advice, id, level_2)
  if (length(at_rows)) tab_text(rows, section, "rows", paste(at_rows, collapse = ", "), id, level_2)
  invisible(rows)
}

# A test that ran, or that could not run on these rows (its reason in the advice).
linear_test <- function(rows, id, h0, advice, alpha, compute) {
  r <- tryCatch(compute(), error = function(e) e)
  if (inherits(r, "error")) {
    return(linear_check(rows, id, h0, "not_computed", paste0("It could not be computed on these rows: ", conditionMessage(r))))
  }
  linear_check(rows, id, h0, linear_verdict_p(r$p, alpha), advice, statistic = r$statistic, df = r$df, df_2 = r$df_2, p = r$p)
}

htest_of <- function(t) list(statistic = unname(t$statistic), df = if (length(t$parameter)) unname(t$parameter[[1L]]) else NULL,
                             df_2 = if (length(t$parameter) > 1L) unname(t$parameter[[2L]]) else NULL, p = unname(t$p.value))

linear_assumptions <- function(rows, m, p, alpha, w, X, y, order_by, vif, kappa_, infl, wr, row_numbers) {
  se <- p$se %||% "classical"
  robust <- !identical(se, "classical")
  n <- length(y)
  k <- ncol(X)

  linear_test(rows, "reset", "No curvature is missing from the model (powers 2 and 3 of the fitted values add nothing).",
              "Add the missing curvature: a square, a log, an interaction.", alpha, function() {
    if (is.null(w)) return(htest_of(lmtest::resettest(m, power = 2:3, type = "fitted")))
    fv <- stats::fitted(m)
    big <- stats::lm.wfit(cbind(X, fv^2, fv^3), y, w)
    q <- big$rank - k
    rss1 <- sum(w * stats::residuals(m)^2)
    rss2 <- sum(w * big$residuals^2)
    stat <- ((rss1 - rss2) / q) / (rss2 / big$df.residual)
    list(statistic = stat, df = q, df_2 = big$df.residual, p = stats::pf(stat, q, big$df.residual, lower.tail = FALSE))
  })
  hetero <- if (robust) {
    paste0("The standard errors are already robust (se = ", se, "): the inference does not rely on this assumption.")
  } else "Use robust standard errors (se = hc3)."
  linear_test(rows, "breusch_pagan", "The residuals' variance does not change with the predictors.", hetero, alpha,
              function() htest_of(lmtest::bptest(m)))
  linear_test(rows, "white", "The residuals' variance does not change with the fitted values or their squares.", hetero, alpha,
              function() htest_of(lmtest::bptest(m, ~ fitted + I(fitted^2), data = data.frame(fitted = stats::fitted(m)))))

  normal <- "The intervals rely on n being large; with few rows, transform the outcome."
  normal_h0 <- "The residuals come from a normal distribution."
  if (n <= LINEAR_MAX_SHAPIRO) {
    linear_test(rows, "shapiro_wilk", normal_h0, normal, alpha, function() htest_of(stats::shapiro.test(wr)))
  } else {
    linear_test(rows, "anderson_darling", normal_h0, normal, alpha, function() htest_of(nortest::ad.test(wr)))
  }
  linear_test(rows, "jarque_bera", "The residuals' skewness and kurtosis are a normal distribution's.",
              "The intervals rely on normal errors: with few rows read them with care, or transform the outcome.", alpha,
              function() linear_jarque_bera(wr))

  independence <- if (identical(se, "hac")) {
    "The standard errors are already HAC (se = hac): the inference allows for this correlation."
  } else "Use se = hac, or model the time."
  checks <- list(durbin_watson = "Residuals next to each other in the order of the rows are not correlated.",
                 breusch_godfrey_1 = "No autocorrelation of order 1 in the residuals.",
                 breusch_godfrey_4 = "No autocorrelation up to order 4 in the residuals.")
  if (is.null(order_by)) {
    for (id in names(checks)) {
      linear_check(rows, id, checks[[id]], "not_computed",
                   "It depends on the order of the rows: choose the column that orders them (order_by).")
    }
  } else {
    mt <- if (is.null(w)) m else {  # the transformed regression: its residuals are the weighted ones
      td <- data.frame(y = sqrt(w) * y, X * sqrt(w))
      rownames(td) <- NULL
      stats::lm(stats::reformulate(colnames(X), response = "y", intercept = FALSE), data = td)
    }
    linear_test(rows, "durbin_watson", checks$durbin_watson, independence, alpha,
                function() htest_of(lmtest::dwtest(mt, order.by = order_by))[c("statistic", "p")])
    linear_test(rows, "breusch_godfrey_1", checks$breusch_godfrey_1, independence, alpha,
                function() htest_of(lmtest::bgtest(mt, order = 1, order.by = order_by)))
    linear_test(rows, "breusch_godfrey_4", checks$breusch_godfrey_4, independence, alpha,
                function() htest_of(lmtest::bgtest(mt, order = 4, order.by = order_by)))
  }

  collinear <- "Drop or combine the correlated predictors, or turn on a penalty."
  for (i in seq_len(nrow(vif))) {
    value <- vif$value[[i]]
    verdict <- if (!is.finite(value)) "not_computed" else if (value > LINEAR_VIF_FAILED) "failed" else
      if (value > LINEAR_VIF_DOUBTFUL) "doubtful" else "ok"
    linear_check(rows, "vif", "Each term is not explained by the other terms (VIF 1).", verdict, collinear,
                 level_2 = vif$term[[i]], statistic = if (vif$df[[i]] > 1L) vif$gvif[[i]] else NULL,
                 df = if (vif$df[[i]] > 1L) vif$df[[i]] else NULL, threshold = LINEAR_VIF_DOUBTFUL, value = value)
  }
  verdict <- if (!is.finite(kappa_)) "not_computed" else if (kappa_ > LINEAR_KAPPA_FAILED) "failed" else
    if (kappa_ > LINEAR_KAPPA_DOUBTFUL) "doubtful" else "ok"
  linear_check(rows, "condition_number", "The design is well conditioned (condition number under 30).", verdict,
               "Drop or combine the correlated predictors, centre them, or turn on a penalty.",
               threshold = LINEAR_KAPPA_DOUBTFUL, value = kappa_)

  linear_influence_checks(rows, infl$cooks, infl$h, k, row_numbers)
  t <- infl$student
  if (any(is.finite(t))) {  # car::outlierTest: the largest |studentized residual|, Bonferroni-adjusted
    i <- which.max(abs(t))
    df_t <- m$df.residual - 1
    p_bonf <- min(1, n * 2 * stats::pt(abs(t[[i]]), df_t, lower.tail = FALSE))
    linear_check(rows, "outlier_test", "No studentized residual is larger than chance allows.", linear_verdict_p(p_bonf, alpha),
                 "Look at the rows named: an outlier pulls the fit towards itself.", statistic = unname(t[[i]]), df = df_t,
                 p = p_bonf, at_rows = row_numbers[[i]])
  } else {
    linear_check(rows, "outlier_test", "No studentized residual is larger than chance allows.", "not_computed",
                 "It could not be computed on these rows: the studentized residuals are not finite.")
  }
  invisible(rows)
}

# Influence, Cook's distance and leverage (linear.R and logistic.R): the verdict by the classical
# thresholds, which a clean fit seldom passes — Cook's distance above the median of F(p, n − p)
# doubtful (Cook and Weisberg), above 1 failed; leverage above 3p/n and above 0.5 doubtful
# (3p/n alone is passed by the largest of a few hundred rows of a normal design nearly always:
# 99% of clean fits at n = 200; Huber's 0.5 is a row that weighs as much as all the others on
# its own fitted value). The wide thresholds 4/n and 2p/n, which a fit of a few hundred rows
# passes nearly always, only name the rows to look at (at most 5, the largest first): they no
# longer make the verdict. Each check writes the largest value, the threshold of its verdict
# and how many rows pass it.
LINEAR_COOKS_FAILED <- 1
LINEAR_LEVERAGE_TIMES <- 3
LINEAR_LEVERAGE_HIGH <- 0.5

linear_influence_checks <- function(rows, cooks, h, k, row_numbers) {
  n <- length(cooks)
  named <- function(score, above) {
    top <- head(order(-score), 5L)
    row_numbers[top[!is.na(score[top]) & score[top] > above]]
  }
  limit <- if (n > k) min(LINEAR_COOKS_FAILED, stats::qf(0.5, k, n - k)) else LINEAR_COOKS_FAILED
  biggest <- if (any(is.finite(cooks))) max(cooks, na.rm = TRUE) else NULL
  linear_check(rows, "cooks_distance", "No row moves the fit much (Cook's distance under the median of F(p, n − p)).",
               if (any(cooks > LINEAR_COOKS_FAILED, na.rm = TRUE)) "failed" else if (any(cooks > limit, na.rm = TRUE)) "doubtful" else "ok",
               "Look at the rows named: a mistake in the data, or a case the model does not describe.",
               threshold = limit, value = biggest, count = sum(cooks > limit, na.rm = TRUE), at_rows = named(cooks, 4 / n))
  limit <- max(LINEAR_LEVERAGE_TIMES * k / n, LINEAR_LEVERAGE_HIGH)
  linear_check(rows, "leverage", "No row has an unusual combination of predictors (leverage under 3p/n and under 0.5).",
               if (any(h > limit, na.rm = TRUE)) "doubtful" else "ok",
               "Look at the rows named: their predictors are far from the others'.",
               threshold = limit, value = if (any(is.finite(h))) max(h, na.rm = TRUE) else NULL, count = sum(h > limit, na.rm = TRUE),
               at_rows = named(h, 2 * k / n))
}

# Jarque–Bera on the moments (as tseries::jarque.bera.test): n/6·(S² + (K − 3)²/4), χ² with 2 df.
linear_jarque_bera <- function(x) {
  x <- x[is.finite(x)]
  n <- length(x)
  d <- x - mean(x)
  m2 <- mean(d^2)
  stat <- n / 6 * ((mean(d^3) / m2^1.5)^2 + (mean(d^4) / m2^2 - 3)^2 / 4)
  list(statistic = stat, df = 2, p = stats::pchisq(stat, 2, lower.tail = FALSE))
}

# The points and lines of the diagnostics, each chart at most max_chart_points points (a sample
# with the node's seed above it, the most extreme kept), the largest named by their row number.
linear_charts <- function(rows, task, m, y, wr, infl, row_numbers) {
  fv <- stats::fitted(m)
  std <- infl$std
  most <- tab_limit(task, "max_chart_points")
  keep <- tab_limit(task, "extreme_points")
  points <- function(id, x, yy, score, labelled = TRUE) {
    i <- tab_cap(length(yy), max = most, score = score, keep = keep)
    labels <- if (labelled) tab_extreme_labels(score, row_numbers, keep = LINEAR_LABELLED)[i] else NULL
    tab_points(rows, "point", id, "y", x = x[i], y = yy[i], labels = labels)
  }
  line <- function(id, series, xy) if (nrow(xy)) tab_points(rows, "curve", id, "y", x = xy$x, y = xy$y, level_2 = series)

  points("resid_fitted", fv, wr, std)
  line("resid_fitted", "lowess", tab_lowess(fv, wr))
  qq <- tab_qq(std, tab_limit(task, "qq_points"))
  if (nrow(qq)) {
    tab_points(rows, "point", "qq", "y", x = qq$x, y = qq$y)
    ab <- tab_qq_line(std)
    ends <- range(qq$x)
    line("qq", "line", data.frame(x = ends, y = ab[["intercept"]] + ab[["slope"]] * ends))
  }
  root <- sqrt(abs(std))
  points("scale_location", fv, root, std)
  line("scale_location", "lowess", tab_lowess(fv, root))
  points("resid_leverage", infl$h, std, infl$cooks)
  h <- tab_hist(wr)
  if (nrow(h)) tab_points(rows, "hist", "residual", "count", x = h$x0, y = h$count, x1 = h$x1)
  points("observed_fitted", fv, y, std, labelled = FALSE)
  invisible(rows)
}

# Predicting the rows the model was fitted on from its card gives its fitted values: checked in
# the self-test (a reference case), where a difference is a failure.
linear_check_card <- function(task, card, design, fitted) {
  if (!identical(task$group_version, "selftest")) return(invisible(TRUE))
  d <- tab_design_rows(card, design$data)
  again <- as.vector(d$X %*% unlist(card$coefficients))
  gap <- max(abs(again - fitted) / pmax(1, abs(fitted)))
  if (!all(d$ok) || !is.finite(gap) || gap > 1e-9) stop("the card does not give back the fitted values (", gap, ")")
  invisible(TRUE)
}

# The prediction of a card on rows: the fitted value, its prediction interval at 1 − alpha
# (the card's covariance and σ, a new row of weight 1). NA where a value is empty or a level
# was not seen when fitting.
linear_predict_rows <- function(card, X, ok, weights = 1) {
  b <- vapply(card$coefficients, function(x) if (is.null(x)) NA_real_ else as.numeric(x), numeric(1))
  fit <- as.vector(X %*% b)
  alpha <- as.numeric(card$alpha %||% card$params$alpha %||% 0.05)
  low <- high <- rep(NA_real_, length(fit))
  if (!is.null(card$vcov) && !is.null(card$linear)) {
    V <- do.call(rbind, lapply(card$vcov, function(r) vapply(r, function(x) if (is.null(x)) NA_real_ else as.numeric(x), 0)))
    half <- stats::qt(1 - alpha / 2, as.numeric(card$linear$df_residual)) *
      sqrt(rowSums((X %*% V) * X) + as.numeric(card$linear$sigma)^2 / weights)
    low <- fit - half
    high <- fit + half
  }
  fit[!ok] <- NA_real_
  low[!ok] <- NA_real_
  high[!ok] <- NA_real_
  list(fit = fit, low = low, high = high)
}

# The rows output: the input's columns, then the model's (docs: «Rows output»). Every row whose
# predictors are present gets its fitted value and prediction interval; the rows the fit used
# also their residual, standardized residual, leverage and Cook's distance.
linear_rows_output <- function(df, card, design, m, w, infl, V) {
  made <- c("pred_fitted", "pred_residual", "pred_std_residual", "pred_leverage", "pred_cooks_distance", "pred_low", "pred_high")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- tab_design_rows(card, df)
  weights <- rep(1, nrow(df))
  if (!is.null(w)) weights[design$rows] <- w
  pr <- linear_predict_rows(card, d$X, d$ok, weights)
  out <- df
  used <- design$rows
  blank <- rep(NA_real_, nrow(df))
  out$pred_fitted <- pr$fit
  out$pred_fitted[used] <- unname(stats::fitted(m))
  out$pred_residual <- blank
  out$pred_residual[used] <- as.numeric(design$y) - unname(stats::fitted(m))
  out$pred_std_residual <- blank
  out$pred_std_residual[used] <- unname(infl$std)
  out$pred_leverage <- blank
  out$pred_leverage[used] <- unname(infl$h)
  out$pred_cooks_distance <- blank
  out$pred_cooks_distance[used] <- unname(infl$cooks)
  out$pred_low <- pr$low
  out$pred_high <- pr$high
  rownames(out) <- NULL
  out
}

# Predict: the card applied to new rows (mode predict), `prediction` and, with `interval`, its
# prediction interval. A level the fit did not see gives an empty prediction, counted.
predict_linear <- function(task, card) {
  df <- tab_input(task)
  made <- c("prediction", if (isTRUE(task$params$interval)) c("prediction_low", "prediction_high"))
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- tab_design_rows(card, df)
  if (d$unseen > 0L) warning(d$unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  pr <- linear_predict_rows(card, d$X, d$ok)
  out <- df
  out$prediction <- pr$fit
  if (isTRUE(task$params$interval)) {
    out$prediction_low <- pr$low
    out$prediction_high <- pr$high
  }
  rownames(out) <- NULL
  list(output = out)
}

# A note the report says in the page's language (layouts/common.py, structured_note): «@id name=value …». A value is
# a column's name or a number: «%», « » and «,» in it are written %25, %20 and %2C, which the report decodes.
linear_note <- function(id, ...) {
  values <- list(...)
  encode <- function(x) gsub(",", "%2C", gsub(" ", "%20", gsub("%", "%25", enc2utf8(as.character(x)), fixed = TRUE), fixed = TRUE), fixed = TRUE)
  parts <- vapply(names(values), function(k) paste0(k, "=", encode(values[[k]])), "")
  paste(c(paste0("@", id), parts), collapse = " ")
}

TAB_COMMON_TESTS[["linear: the notes are ids with their values, which the report words in the page's language"]] <- function(code) {
  .tab_expect(identical(linear_note("weights_unused", weights = "peso 50%,a"), "@weights_unused weights=peso%2050%25%2Ca"), "the encoding")
  .tab_expect(identical(linear_note("interaction_vif"), "@interaction_vif"), "an id alone")
}
