# Logistic regression: maximum likelihood or Firth's penalized likelihood, logit or probit,
# with classical, heteroscedasticity-consistent (HC0–HC3) or clustered standard errors, and the
# checks of its assumptions (docs/specs/statistical-models.md, `logistic`, lane M2). With a
# penalty the route goes to penalized.R instead.
#
# The fit is stats::glm(family = binomial(link)) on the design of tab_design, whose columns have
# positional names (d1, d2…): no column's name ever reaches a formula; Firth's fit is the same
# call with method = brglm2::brglmFit (type = "AS_mean", Firth's for the logit link). Before it,
# detectseparation::detect_separation looks for complete or quasi-complete separation: with
# maximum likelihood a separated outcome stops the node with its sentence (the estimates would be
# infinite), with Firth's it is a row of the checks. The covariance comes from sandwich, the
# tests of the coefficients from lmtest::coeftest (z) and their intervals from lmtest::coefci
# (Wald) or stats::confint (profile likelihood, maximum likelihood only: brglm2 has none).
#
# The rest is a few lines each, checked in the reference values against a second construction:
# the AUC (Mann–Whitney) with DeLong's interval as pROC computes it, the Hosmer–Lemeshow test as
# ResourceSelection::hoslem.test computes it (10 groups by the quantiles of the predicted risk),
# Box–Tidwell (each positive number predictor's x·log(x) added to the fit, its Wald test), the
# VIFs (linear.R's, from the coefficients' correlations), the events per variable.
#
# Weights are frequencies, whole numbers: a row of weight 3 stands for three cases. The fit, the
# rates, the AUC and its interval, the Hosmer–Lemeshow groups and the confusion matrix all count
# cases, so they equal the same computations on the rows repeated (the reference values check
# it); n and the events are sums of the weights.

LOGISTIC_PACKAGES <- c("sandwich", "lmtest", "brglm2", "detectseparation")
LOGISTIC_HL_GROUPS <- 10L
LOGISTIC_EPV <- 10            # events per variable under which the estimates are biased
LOGISTIC_DEV_RESIDUAL <- 3    # |deviance residual| above which a row is predicted badly
LOGISTIC_PROB_BINS <- 20L     # the histogram of the predicted probabilities, on [0, 1]
LOGISTIC_LABELLED <- 3L       # points named on a chart

# The notes the runner may write: an id each (the row's stat is note_<id>) and one sentence,
# with no number or name in it, so that the report's words can say it in the page's language
# (report_texts/logistic.json, `values`).
LOGISTIC_NOTES <- c(
  firth = paste0("Firth's penalized likelihood (brglm2, AS_mean): the estimates are pulled towards 0 and stay finite under ",
                 "separation; the deviance, the likelihood-ratio test and the AIC are the ordinary likelihood's at these ",
                 "estimates."),
  firth_profile = "brglm2 has no profile likelihood intervals for Firth's fit: the intervals are Wald's.",
  firth_robust = "With Firth's estimation the robust standard errors are sandwich's for a glm, at Firth's estimates.",
  probit = paste0("With the probit link the coefficients are on the scale of a standard normal variable, not of log odds: ",
                  "there are no odds ratios."),
  weights = "The weights are frequencies: n, the events, the rates and the checks count the cases the rows stand for.",
  robust_weights = "With weights, the robust standard errors take each row as one observation, not as many as its weight.",
  zero_weights = "Rows of weight 0 were left out of the fit.",
  interactions = paste0("The terms of an interaction are correlated with their parts by construction: their VIFs are large ",
                        "for that reason; centre the predictors to read them."),
  not_converged = paste0("The fit did not converge: the estimates may be unreliable; look for a predictor that almost ",
                         "separates the outcome.")
)

logistic_note <- function(rows, id) tab_text(rows, "model", paste0("note_", id), LOGISTIC_NOTES[[id]])

logistic_params <- function(params) {
  p <- tab_model_spec("logistic")$defaults
  for (k in names(params)) p[k] <- list(params[[k]])
  p
}

# The outcome as 0 and 1: the event is the value `event_value` names (a number compared as a
# number, a boolean as true or false, a text as it is). Refused: one value, more than two, an
# event that is not one of them.
logistic_outcome <- function(y, event_value, outcome) {
  labels <- tab_label(y)
  values <- tab_sort_labels(labels)
  if (length(values) < 2L) {
    tab_refuse("one_outcome_value", paste0("the outcome «", outcome, "» holds one value only in the rows the model uses: ",
                                           "a logistic regression needs two"), list(column = outcome))
  }
  if (length(values) > 2L) {
    tab_refuse("outcome_not_binary", paste0("the outcome «", outcome, "» has ", length(values), " values: a logistic ",
                                            "regression explains two (an event and its absence)"),
               list(column = outcome, values = length(values)))
  }
  wanted <- if (is.null(event_value)) NA_character_ else if (is.logical(event_value)) tab_label(event_value) else
    as.character(event_value)
  kind <- tab_kind(y)
  event <- if (kind %in% c("number", "integer")) {
    v <- suppressWarnings(as.numeric(wanted))
    values[!is.na(v) & suppressWarnings(as.numeric(values)) == v]
  } else if (kind == "boolean") {
    values[values == tolower(wanted)]
  } else {
    values[values == wanted]
  }
  if (length(event) != 1L) {
    tab_refuse("event_value", paste0("the outcome «", outcome, "» has no value «", wanted, "» to be the event: its values are «",
                                     paste(values, collapse = "», «"), "»"), list(column = outcome, value = wanted))
  }
  list(y = as.numeric(labels == event), event = event, other = setdiff(values, event))
}

fit_logistic <- function(task) {
  p <- logistic_params(task$params)
  alpha <- as.numeric(p$alpha %||% 0.05)
  outcome <- p$outcome
  predictors <- unlist(p$predictors)
  weights_col <- p$weights
  cluster_col <- if (identical(p$se, "cluster")) p$cluster else NULL
  link <- p$link %||% "logit"
  firth <- identical(p$estimation, "firth")
  se <- p$se %||% "classical"
  has_int <- !isFALSE(p$intercept)
  threshold <- as.numeric(p$threshold %||% 0.5)
  rows <- tab_rows_start(task)

  df <- tab_input(task)
  for (column in c(outcome, predictors, weights_col, cluster_col)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  if (!is.null(weights_col) && !tab_kind(df[[weights_col]]) %in% c("number", "integer")) {
    tab_refuse("weights_not_number", paste0("the weights «", weights_col, "» are not numbers"), list(column = weights_col))
  }
  design <- tab_design(df, predictors, outcome = outcome, reference = p$reference %||% list(),
                       interactions = p$interactions %||% list(), intercept = has_int, also = c(weights_col, cluster_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  w <- rep(1, design$n)
  zero_weights <- 0L
  if (!is.null(weights_col)) {
    w <- as.numeric(design$data[[weights_col]])
    if (any(w < 0) || any(w != round(w))) {
      tab_refuse("weights_not_frequencies", paste0("the weights «", weights_col, "» are frequencies: whole numbers, 0 or more"),
                 list(column = weights_col))
    }
    zero_weights <- sum(w == 0)
    if (zero_weights > 0L) {
      design <- linear_subset(design, w > 0)
      w <- w[w > 0]
    }
  }
  ev <- logistic_outcome(design$y, p$event_value, outcome)
  y <- ev$y
  k <- ncol(design$X)
  if (design$n < k + 2L) {
    tab_refuse("too_few_rows", paste0("the model has ", k, " terms and ", design$n, " rows it can use: it needs at least ", k + 2L,
                                      " rows (more rows than terms + 1)"), list(rows = design$n, terms = k))
  }
  tab_refuse_aliased(design)

  # ── separation, then the fit, on positional names ─────────────────────────
  X <- design$X
  Xn <- if (has_int) X[, -1L, drop = FALSE] else X
  dat <- data.frame(y = y, Xn, check.names = FALSE)
  rownames(dat) <- NULL
  dat$.w <- w
  form <- stats::reformulate(colnames(Xn), response = "y", intercept = has_int)
  fam <- stats::binomial(link)
  separated <- logistic_separation(form, fam, dat, design$terms)
  if (!firth && length(separated$terms)) {
    tab_refuse("separation", paste0("the term «", separated$terms[[1L]], "» separates the outcome: use estimation = firth, ",
                                    "or a penalty"), list(terms = as.list(separated$terms)))
  }
  m <- logistic_glm(form, fam, dat, firth)
  b <- stats::coef(m)
  n_rows <- design$n
  n <- sum(w)
  events <- sum(w * y)

  V0 <- stats::vcov(m)
  V <- switch(se,
    classical = V0,
    hc0 = sandwich::vcovHC(m, type = "HC0"),
    hc1 = sandwich::vcovHC(m, type = "HC1"),
    hc2 = sandwich::vcovHC(m, type = "HC2"),
    hc3 = sandwich::vcovHC(m, type = "HC3"),
    cluster = sandwich::vcovCL(m, cluster = design$data[[cluster_col]]),
    tab_refuse("se", paste0("unknown standard errors «", se, "»")))
  ct <- lmtest::coeftest(m, vcov. = V)
  profile <- identical(p$ci, "profile") && !firth
  ci <- if (profile) {
    suppressMessages(stats::confint(m, level = 1 - alpha))
  } else {
    lmtest::coefci(m, vcov. = V, level = 1 - alpha)
  }
  ci <- matrix(ci, ncol = 2L)

  # ── the model section ─────────────────────────────────────────────────────
  prob <- unname(stats::fitted(m))
  eta <- unname(m$linear.predictors)
  tab_text(rows, "model", "formula", design$formula)
  tab_text(rows, "model", "family", "binomial")
  tab_text(rows, "model", "link", link)
  tab_text(rows, "model", "estimation", if (firth) "firth" else "ml")
  tab_text(rows, "model", "se_type", se)
  tab_text(rows, "model", "event_value", ev$event)
  tab_text(rows, "model", "software", tab_software(LOGISTIC_PACKAGES))
  auc <- logistic_auc(prob, y, w, alpha)
  cls <- logistic_classes(prob, y, w, threshold)
  dev <- m$deviance
  null_dev <- m$null.deviance
  lr <- null_dev - dev
  lr_df <- m$df.null - m$df.residual
  cox_snell <- 1 - exp((dev - null_dev) / n)
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "events", events)
  tab_number(rows, "model", "n_missing", design$n_missing)
  tab_number(rows, "model", "auc", auc$auc)
  tab_number(rows, "model", "auc_ci_low", auc$low)
  tab_number(rows, "model", "auc_ci_high", auc$high)
  tab_number(rows, "model", "brier", sum(w * (prob - y)^2) / n)
  tab_number(rows, "model", "threshold", threshold)
  tab_number(rows, "model", "accuracy", cls$accuracy)
  tab_number(rows, "model", "sensitivity", cls$sensitivity)
  tab_number(rows, "model", "specificity", cls$specificity)
  tab_number(rows, "model", "precision", cls$precision)
  tab_number(rows, "model", "deviance", dev)
  tab_number(rows, "model", "null_deviance", null_dev)
  tab_number(rows, "model", "lr_statistic", lr)
  tab_number(rows, "model", "lr_df", lr_df)
  tab_p(rows, "model", if (lr_df > 0) stats::pchisq(lr, lr_df, lower.tail = FALSE) else NA_real_, stat = "lr_p_value")
  tab_number(rows, "model", "log_lik", as.numeric(stats::logLik(m)))
  tab_number(rows, "model", "aic", stats::AIC(m))
  tab_number(rows, "model", "bic", stats::BIC(m))
  tab_number(rows, "model", "mcfadden_r2", 1 - dev / null_dev)
  tab_number(rows, "model", "nagelkerke_r2", cox_snell / (1 - exp(-null_dev / n)))
  tab_number(rows, "model", "tjur_r2", sum(w * y * prob) / events - sum(w * (1 - y) * prob) / (n - events))

  # ── coefficients ──────────────────────────────────────────────────────────
  vif <- linear_vif(V0, design$terms, has_int)
  terms <- design$terms
  odds <- identical(link, "logit")
  for (j in seq_along(b)) {
    level <- terms$term[[j]]
    level_2 <- if (is.na(terms$level[[j]])) NULL else terms$level[[j]]
    tab_number(rows, "coef", "estimate", ct[j, 1L], level, level_2)
    tab_number(rows, "coef", "std_error", ct[j, 2L], level, level_2)
    tab_number(rows, "coef", "statistic", ct[j, 3L], level, level_2)
    tab_p(rows, "coef", ct[j, 4L], level = level, level_2 = level_2)
    tab_number(rows, "coef", "ci_low", ci[j, 1L], level, level_2)
    tab_number(rows, "coef", "ci_high", ci[j, 2L], level, level_2)
    if (odds) {
      tab_number(rows, "coef", "odds_ratio", exp(ct[j, 1L]), level, level_2)
      tab_number(rows, "coef", "or_ci_low", exp(ci[j, 1L]), level, level_2)
      tab_number(rows, "coef", "or_ci_high", exp(ci[j, 2L]), level, level_2)
    }
    at <- match(terms$term_index[[j]], vif$term_index)
    if (!is.na(at)) tab_number(rows, "coef", "vif", vif$value[[at]], level, level_2)
  }

  # ── the assumptions ───────────────────────────────────────────────────────
  infl <- list(h = unname(stats::hatvalues(m)), cooks = unname(stats::cooks.distance(m)),
               dev = unname(stats::residuals(m, type = "deviance")), pearson = unname(stats::residuals(m, type = "pearson")))
  hl <- logistic_hosmer_lemeshow(prob, y, w)
  logistic_assumptions(rows, m = m, p = p, alpha = alpha, design = design, separated = separated, hl = hl, vif = vif,
                       events = events, n = n, infl = infl, form = form, fam = fam, dat = dat, firth = firth)

  # ── the charts' points ────────────────────────────────────────────────────
  logistic_charts(rows, task, prob, y, w, eta, infl, hl, ev, design$rows)
  logistic_cells(rows, cls, ev)

  # ── notes ─────────────────────────────────────────────────────────────────
  if (firth) logistic_note(rows, "firth")
  if (firth && identical(p$ci, "profile")) logistic_note(rows, "firth_profile")
  if (firth && !identical(se, "classical")) logistic_note(rows, "firth_robust")
  if (!odds) logistic_note(rows, "probit")
  if (!is.null(weights_col)) logistic_note(rows, "weights")
  if (!is.null(weights_col) && !identical(se, "classical")) logistic_note(rows, "robust_weights")
  if (zero_weights > 0L) logistic_note(rows, "zero_weights")
  if (length(p$interactions %||% list())) logistic_note(rows, "interactions")
  if (!isTRUE(m$converged)) logistic_note(rows, "not_converged")

  # ── the card, and the rows output ─────────────────────────────────────────
  card <- tab_card(task, design, LOGISTIC_PACKAGES)
  card$outcome <- list(name = outcome, event_value = ev$event, levels = list(ev$other, ev$event))
  card$coefficients <- as.list(unname(b))
  card$vcov <- tab_card_vcov(task, unname(V))
  card$link <- link
  card$threshold <- threshold
  card$alpha <- alpha
  logistic_check_card(task, card, design, prob)

  output <- NULL
  if (identical(task$output %||% p$output, "rows")) {
    output <- logistic_rows_output(df, card, design, y, prob, eta, infl)
  }
  list(rows = rows, output = output, card = card, native = m)
}

# The fit: glm by maximum likelihood, or brglm2's Firth fit. A warning of glm (fitted
# probabilities 0 or 1) is kept for run.json, not a failure.
logistic_glm <- function(form, fam, dat, firth) {
  if (firth) {
    stats::glm(form, family = fam, data = dat, weights = .w, method = brglm2::brglmFit, type = "AS_mean")
  } else {
    stats::glm(form, family = fam, data = dat, weights = .w)
  }
}

# Separation, by detectseparation's linear program: the terms whose maximum likelihood estimate
# is infinite (their labels, the intercept last), or the error that kept it from running.
logistic_separation <- function(form, fam, dat, terms) {
  r <- tryCatch(stats::glm(form, family = fam, data = dat, weights = .w, method = detectseparation::detect_separation),
                error = function(e) e)
  if (inherits(r, "error")) return(list(terms = character(), error = conditionMessage(r)))
  beta <- r$coefficients
  infinite <- which(is.infinite(beta))
  labels <- terms$label[infinite]
  list(terms = c(labels[labels != "(Intercept)"], labels[labels == "(Intercept)"]), error = NULL, outcome = isTRUE(r$outcome))
}

# The AUC (the probability that a case's predicted risk is above a non-case's, ties a half) and
# DeLong's interval at 1 − alpha, as pROC's ci.auc(method = "delong") computes it: the
# variance from the placement values of the cases and of the non-cases, the interval normal,
# cut at 0 and 1. Frequency weights count each row as many times as its weight.
logistic_auc <- function(prob, y, w, alpha) {
  cases <- y == 1
  x1 <- prob[cases]
  w1 <- w[cases]
  x0 <- prob[!cases]
  w0 <- w[!cases]
  W1 <- sum(w1)
  W0 <- sum(w0)
  # the placement of each case among the non-cases, and of each non-case among the cases
  o0 <- order(x0)
  s0 <- x0[o0]
  c0 <- c(0, cumsum(w0[o0]))
  below0 <- c0[findInterval(x1, s0, left.open = TRUE) + 1L]
  upto0 <- c0[findInterval(x1, s0) + 1L]
  v10 <- (below0 + (upto0 - below0) / 2) / W0
  o1 <- order(x1)
  s1 <- x1[o1]
  c1 <- c(0, cumsum(w1[o1]))
  above1 <- W1 - c1[findInterval(x0, s1) + 1L]
  from1 <- W1 - c1[findInterval(x0, s1, left.open = TRUE) + 1L]
  v01 <- (above1 + (from1 - above1) / 2) / W1
  auc <- sum(w1 * v10) / W1
  if (W1 < 2 || W0 < 2) return(list(auc = auc, low = NA_real_, high = NA_real_))
  s10 <- sum(w1 * (v10 - auc)^2) / (W1 - 1)
  s01 <- sum(w0 * (v01 - auc)^2) / (W0 - 1)
  se <- sqrt(s10 / W1 + s01 / W0)
  z <- stats::qnorm(1 - alpha / 2)
  list(auc = auc, low = max(0, auc - z * se), high = min(1, auc + z * se))
}

# The classes at the threshold (an event predicted when its probability is at least the
# threshold) and their rates; the counts weighted.
logistic_classes <- function(prob, y, w, threshold) {
  hat <- as.numeric(prob >= threshold)
  tp <- sum(w * (y == 1 & hat == 1))
  fn <- sum(w * (y == 1 & hat == 0))
  fp <- sum(w * (y == 0 & hat == 1))
  tn <- sum(w * (y == 0 & hat == 0))
  ratio <- function(a, b) if (b > 0) a / b else NA_real_
  list(tp = tp, fn = fn, fp = fp, tn = tn, accuracy = (tp + tn) / (tp + fn + fp + tn), sensitivity = ratio(tp, tp + fn),
       specificity = ratio(tn, tn + fp), precision = ratio(tp, tp + fp), hat = hat)
}

# The type-7 quantiles of `x` as stats::quantile computes them on the rows repeated `w` times.
logistic_quantile <- function(x, w, probs) {
  if (all(w == 1)) return(stats::quantile(x, probs, type = 7, names = FALSE))
  o <- order(x)
  xs <- x[o]
  cw <- cumsum(w[o])
  N <- cw[[length(cw)]]
  at <- function(i) xs[findInterval(i - 1, cw) + 1L]  # the i-th of the repeated, sorted values
  index <- 1 + (N - 1) * probs
  lo <- floor(index)
  hi <- ceiling(index)
  qs <- at(lo)
  xh <- at(hi)
  i <- which(index > lo & xh != qs)
  h <- (index - lo)[i]
  qs[i] <- (1 - h) * qs[i] + h * xh[i]
  qs
}

# Hosmer–Lemeshow, as ResourceSelection::hoslem.test: groups cut at the deciles of the predicted
# risk (the breaks unique, the lowest included), observed against expected events and
# non-events. df = the groups made − 2 (hoslem.test says 10 − 2 even when tied risks make fewer
# groups). The groups also make the calibration chart.
logistic_hosmer_lemeshow <- function(prob, y, w, g = LOGISTIC_HL_GROUPS) {
  breaks <- unique(logistic_quantile(prob, w, seq(0, 1, 1 / g)))
  if (length(breaks) < 4L) return(list(error = "too few distinct predicted risks to make groups"))
  group <- cut(prob, breaks = breaks, include.lowest = TRUE)
  o1 <- tapply(w * y, group, sum)
  o0 <- tapply(w * (1 - y), group, sum)
  e1 <- tapply(w * prob, group, sum)
  e0 <- tapply(w * (1 - prob), group, sum)
  size <- tapply(w, group, sum)
  keep <- !is.na(size) & size > 0
  o1 <- o1[keep]
  o0 <- o0[keep]
  e1 <- e1[keep]
  e0 <- e0[keep]
  size <- size[keep]
  groups <- length(size)
  if (groups < 3L) return(list(error = "too few distinct predicted risks to make groups"))
  stat <- sum((o1 - e1)^2 / e1 + (o0 - e0)^2 / e0)
  df <- groups - 2L
  list(statistic = stat, df = df, p = stats::pchisq(stat, df, lower.tail = FALSE), error = NULL,
       predicted = unname(e1 / size), observed = unname(o1 / size))
}

logistic_assumptions <- function(rows, m, p, alpha, design, separated, hl, vif, events, n, infl, form, fam, dat, firth) {
  # separation: with maximum likelihood the node stopped before; with Firth each separating term
  if (!is.null(separated$error)) {
    linear_check(rows, "separation", "No combination of predictors separates the outcome completely.", "not_computed",
                 paste0("It could not be computed on these rows: ", separated$error))
  } else if (length(separated$terms)) {
    for (term in separated$terms) {
      linear_check(rows, "separation", "No combination of predictors separates the outcome completely.", "doubtful",
                   paste0("The term separates the outcome: Firth's estimate stays finite, but its odds ratio is large and ",
                          "its interval wide."), level_2 = term)
    }
  } else {
    linear_check(rows, "separation", "No combination of predictors separates the outcome completely.", "ok",
                 "Nothing to do.")
  }

  hl_h0 <- "The predicted probabilities match the observed rates in 10 groups of risk."
  if (!is.null(hl$error)) {
    linear_check(rows, "hosmer_lemeshow", hl_h0, "not_computed", paste0("It could not be computed on these rows: ", hl$error, "."))
  } else {
    linear_check(rows, "hosmer_lemeshow", hl_h0, linear_verdict_p(hl$p, alpha),
                 "The predicted probabilities do not match the observed rates: read the calibration chart.",
                 statistic = hl$statistic, df = hl$df, p = hl$p)
  }

  logistic_box_tidwell(rows, p, alpha, design, form, fam, dat, firth)

  collinear <- "Drop or combine the correlated predictors, or turn on a penalty."
  for (i in seq_len(nrow(vif))) {
    value <- vif$value[[i]]
    verdict <- if (!is.finite(value)) "not_computed" else if (value > LINEAR_VIF_FAILED) "failed" else
      if (value > LINEAR_VIF_DOUBTFUL) "doubtful" else "ok"
    linear_check(rows, "vif", "Each term is not explained by the other terms (VIF 1).", verdict, collinear,
                 level_2 = vif$term[[i]], statistic = if (vif$df[[i]] > 1L) vif$gvif[[i]] else NULL,
                 df = if (vif$df[[i]] > 1L) vif$df[[i]] else NULL, threshold = LINEAR_VIF_DOUBTFUL, value = value)
  }

  k <- length(stats::coef(m)) - as.integer(!isFALSE(p$intercept))
  epv <- min(events, n - events) / max(k, 1L)
  linear_check(rows, "epv", "At least 10 events for each term of the model.", if (epv < LOGISTIC_EPV) "doubtful" else "ok",
               "Few events for the number of terms: fewer terms, or Firth or a penalty help.", threshold = LOGISTIC_EPV,
               value = epv)

  row_numbers <- design$rows
  n_rows <- length(row_numbers)
  named <- function(score, above) {
    top <- head(order(-score), 5L)
    row_numbers[top[score[top] > above]]
  }
  cooks <- infl$cooks
  limit <- 4 / n_rows
  over <- sum(cooks > limit, na.rm = TRUE)
  linear_check(rows, "cooks_distance", "No row moves the fit much (Cook's distance under 4/n).",
               if (any(cooks > 1, na.rm = TRUE)) "failed" else if (over > 0L) "doubtful" else "ok",
               "Look at the rows named: a mistake in the data, or a case the model does not describe.",
               threshold = limit, count = over, at_rows = named(cooks, limit))
  h <- infl$h
  limit <- 2 * ncol(design$X) / n_rows
  over <- sum(h > limit, na.rm = TRUE)
  linear_check(rows, "leverage", "No row has an unusual combination of predictors (leverage under 2p/n).",
               if (over > 0L) "doubtful" else "ok", "Look at the rows named: their predictors are far from the others'.",
               threshold = limit, count = over, at_rows = named(h, limit))
  d <- abs(infl$dev)
  over <- sum(d > LOGISTIC_DEV_RESIDUAL, na.rm = TRUE)
  linear_check(rows, "deviance_residuals", "No row is far from what the model predicts for it (|deviance residual| under 3).",
               if (over > 0L) "doubtful" else "ok", "Look at the rows named: the model predicts them badly.",
               threshold = LOGISTIC_DEV_RESIDUAL, count = over, at_rows = named(d, LOGISTIC_DEV_RESIDUAL))
  invisible(rows)
}

# Box–Tidwell for the logit: for each number predictor whose values are all above 0, its
# x·log(x) added to the model (one at a time, by the model's own estimation), the Wald test of
# that term. A number predictor with a value at or below 0, or fewer than three distinct values
# (x·log(x) is then a line in x), is «not computed» with the reason; a model with no number
# predictor has one row that says so.
logistic_box_tidwell <- function(rows, p, alpha, design, form, fam, dat, firth) {
  h0 <- "The predictor's effect is linear in the logit (its x·log(x) term adds nothing)."
  advice <- "That predictor's effect is not linear in the logit: transform it."
  numbers <- Filter(function(i) identical(i$type, "number"), design$inputs)
  if (!length(numbers)) {
    return(linear_check(rows, "box_tidwell", h0, "not_computed", "The model has no number predictor to check."))
  }
  for (input in numbers) {
    name <- input$name
    x <- as.numeric(design$data[[name]])
    if (any(x <= 0)) {
      linear_check(rows, "box_tidwell", h0, "not_computed", "The predictor has values at or below 0: x·log(x) needs values above 0.",
                   level_2 = name)
      next
    }
    if (length(unique(x)) < 3L) {
      linear_check(rows, "box_tidwell", h0, "not_computed", "The predictor has fewer than three distinct values.", level_2 = name)
      next
    }
    d2 <- dat
    d2$bt <- x * log(x)
    r <- tryCatch({
      fit <- logistic_glm(stats::update(form, . ~ . + bt), fam, d2, firth)
      s <- summary(fit)$coefficients
      if (!"bt" %in% rownames(s)) stop("x·log(x) is a combination of the other terms")
      list(statistic = s["bt", 3L], p = s["bt", 4L])
    }, error = function(e) e)
    if (inherits(r, "error")) {
      linear_check(rows, "box_tidwell", h0, "not_computed", paste0("It could not be computed on these rows: ", conditionMessage(r)),
                   level_2 = name)
    } else {
      linear_check(rows, "box_tidwell", h0, linear_verdict_p(r$p, alpha), advice, level_2 = name, statistic = r$statistic,
                   p = r$p)
    }
  }
  invisible(rows)
}

# The points and lines of the diagnostics, each chart at most max_chart_points points.
logistic_charts <- function(rows, task, prob, y, w, eta, infl, hl, ev, row_numbers) {
  most <- tab_limit(task, "max_chart_points")
  keep <- tab_limit(task, "extreme_points")
  logistic_classification_charts(rows, task, prob, y, w, hl, ev)
  # deviance residuals against the linear predictor, the largest named
  i <- tab_cap(length(eta), max = most, score = infl$dev, keep = keep)
  labels <- tab_extreme_labels(infl$dev, row_numbers, keep = LOGISTIC_LABELLED)[i]
  tab_points(rows, "point", "resid_linear", "y", x = eta[i], y = infl$dev[i], labels = labels)
  invisible(rows)
}

# The ROC curve, the calibration on the Hosmer–Lemeshow groups and the predicted probabilities
# by observed outcome (penalized.R draws them from the out-of-fold probabilities).
logistic_classification_charts <- function(rows, task, prob, y, w, hl, ev) {
  most <- tab_limit(task, "max_chart_points")
  # ROC: (1 − specificity, sensitivity) at every distinct threshold, from (0, 0) to (1, 1)
  cuts <- sort(unique(prob), decreasing = TRUE)
  tpr <- c(0, vapply(cuts, function(t) sum(w[y == 1 & prob >= t]), 0) / sum(w[y == 1]))
  fpr <- c(0, vapply(cuts, function(t) sum(w[y == 0 & prob >= t]), 0) / sum(w[y == 0]))
  if (length(tpr) > most) {
    i <- unique(round(seq(1, length(tpr), length.out = most)))
    tpr <- tpr[i]
    fpr <- fpr[i]
  }
  tab_points(rows, "curve", "roc", "y", x = fpr, y = tpr)
  # calibration: the Hosmer–Lemeshow groups, the mean predicted risk against the observed rate
  if (is.null(hl$error)) tab_points(rows, "point", "calibration", "y", x = hl$predicted, y = hl$observed)
  # the predicted probabilities by observed outcome, on the same intervals of [0, 1]
  breaks <- seq(0, 1, length.out = LOGISTIC_PROB_BINS + 1L)
  bin <- findInterval(prob, breaks, rightmost.closed = TRUE, all.inside = TRUE)
  for (class in list(list(label = ev$event, is = 1), list(label = ev$other, is = 0))) {
    count <- vapply(seq_len(LOGISTIC_PROB_BINS), function(b) sum(w[bin == b & y == class$is]), 0)
    tab_points(rows, "hist", "prob", "count", x = head(breaks, -1L), y = count, x1 = tail(breaks, -1L), level_2 = class$label)
  }
  invisible(rows)
}

# The confusion matrix at the threshold: observed class (level) by predicted class (level_2),
# both in the text order of the classes (the report's table sorts its rows so; its columns
# follow the rows' order: the same order both ways).
logistic_cells <- function(rows, cls, ev) {
  count <- function(observed, predicted) {
    if (observed == ev$event) {
      if (predicted == ev$event) cls$tp else cls$fn
    } else {
      if (predicted == ev$event) cls$fp else cls$tn
    }
  }
  classes <- tab_sort_labels(c(ev$event, ev$other))
  for (observed in classes) {
    for (predicted in classes) tab_number(rows, "cell", "observed", count(observed, predicted), observed, predicted)
  }
  invisible(rows)
}

# The probability of a card's model on rows: the linear predictor and its inverse link; NA where
# a value is empty or a level was not seen when fitting.
logistic_predict_rows <- function(card, X, ok) {
  b <- vapply(card$coefficients, function(x) if (is.null(x)) NA_real_ else as.numeric(x), numeric(1))
  eta <- as.vector(X %*% b)
  prob <- stats::binomial(card$link %||% "logit")$linkinv(eta)
  eta[!ok] <- NA_real_
  prob[!ok] <- NA_real_
  list(eta = eta, prob = prob)
}

logistic_class_of <- function(card, prob, threshold) {
  levels <- vapply(card$outcome$levels, as.character, "")
  ifelse(is.na(prob), NA_character_, ifelse(prob >= threshold, levels[[2L]], levels[[1L]]))
}

# Predicting the rows the model was fitted on from its card gives its fitted probabilities:
# checked in the self-test (a reference case), where a difference is a failure.
logistic_check_card <- function(task, card, design, fitted) {
  if (!identical(task$group_version, "selftest")) return(invisible(TRUE))
  d <- tab_design_rows(card, design$data)
  again <- logistic_predict_rows(card, d$X, d$ok)$prob
  gap <- max(abs(again - fitted))
  if (!all(d$ok) || !is.finite(gap) || gap > 1e-9) stop("the card does not give back the fitted probabilities (", gap, ")")
  invisible(TRUE)
}

# The rows output: the input's columns, then the model's (docs: «Rows output»). Every row whose
# predictors are present gets its probability, its class at the threshold and its linear
# predictor; the rows the fit used also their residuals, leverage and Cook's distance.
logistic_rows_output <- function(df, card, design, y, prob, eta, infl) {
  made <- c("pred_prob", "pred_class", "pred_linear", "pred_deviance_residual", "pred_pearson_residual", "pred_leverage",
            "pred_cooks_distance")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- tab_design_rows(card, df)
  pr <- logistic_predict_rows(card, d$X, d$ok)
  used <- design$rows
  blank <- rep(NA_real_, nrow(df))
  out <- df
  out$pred_prob <- pr$prob
  out$pred_prob[used] <- prob
  out$pred_class <- logistic_class_of(card, out$pred_prob, card$threshold)
  out$pred_linear <- pr$eta
  out$pred_linear[used] <- eta
  out$pred_deviance_residual <- blank
  out$pred_deviance_residual[used] <- infl$dev
  out$pred_pearson_residual <- blank
  out$pred_pearson_residual[used] <- infl$pearson
  out$pred_leverage <- blank
  out$pred_leverage[used] <- infl$h
  out$pred_cooks_distance <- blank
  out$pred_cooks_distance[used] <- infl$cooks
  rownames(out) <- NULL
  out
}

# Predict: the card applied to new rows (mode predict): `prediction`, the probability of the
# event, and with `threshold` the class it predicts. A level the fit did not see gives an empty
# prediction, counted.
predict_logistic <- function(task, card) {
  df <- tab_input(task)
  threshold <- task$params$threshold
  made <- c("prediction", if (!is.null(threshold)) "prediction_class")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- tab_design_rows(card, df)
  if (d$unseen > 0L) warning(d$unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  pr <- logistic_predict_rows(card, d$X, d$ok)
  out <- df
  out$prediction <- pr$prob
  if (!is.null(threshold)) out$prediction_class <- logistic_class_of(card, pr$prob, as.numeric(threshold))
  rownames(out) <- NULL
  list(output = out)
}
