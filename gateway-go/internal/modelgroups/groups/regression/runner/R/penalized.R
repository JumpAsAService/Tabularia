# Penalties of the linear and logistic regressions: ridge, lasso and elastic net, λ chosen by
# cross-validation (docs/specs/statistical-models.md, `penalty`, lane M3). run.R routes a
# `linear` or `logistic` node here when its `penalty` is not `none`, and a card whose variant is
# `penalized` to predict_penalized.
#
# The fit is glmnet::cv.glmnet (family gaussian or binomial, the logit link) on the design of
# tab_design without its column of ones (glmnet fits the intercept itself, unpenalized): every
# dummy of a categorical predictor is a column of its own and is penalized on its own. The
# folds are drawn here, from the node's seed, the way cv.glmnet draws them
# (sample(rep(1:K, length.out = n))), or by the values of `fold_by` (every row of a value in
# one fold, the values drawn into the folds the same way), and handed to cv.glmnet as `foldid`:
# the reference values call cv.glmnet with the same foldid and get the same numbers.
# Standardization is glmnet's (`standardize`): the penalty acts on the predictors scaled to
# variance 1 with the weights, the coefficients come back on the original scale.
#
# λ: `cv_1se` (the largest λ whose error is within one standard error of the least),
# `cv_min` (the least error), or a fixed value, added to glmnet's own path so that the curve,
# the coefficients and the out-of-fold predictions are read at it exactly. Elastic net mixes
# with `mixing`, or with `mixing = cv` the grid 0.1 … 1 on the same folds, the mixing whose
# best λ (or the fixed one) has the least error kept.
#
# No standard error, p-value or interval: inference after a penalized selection is out of
# scope. The numbers of the fit are the out-of-fold predictions' (cv.glmnet's fit.preval at the
# chosen λ): R², RMSE and MAE for the linear, the AUC with DeLong's interval, the Brier score,
# the deviance and the classification at the threshold for the logistic. The checks are
# diagnostics, not tests: the choice of λ (its least error at an edge of the path, every
# coefficient zero), separation and the events per variable for the logistic; the charts out
# of fold. The tests of an unpenalized fit assume no penalty and are not computed.

PENALIZED_PACKAGES <- c("glmnet")
PENALIZED_MIXING_GRID <- c(0.1, 0.25, 0.5, 0.75, 0.9, 1)
PENALIZED_PATH_TERMS <- 20L       # the paths drawn one by one; the rest summed up
PENALIZED_OTHERS <- "(others)"    # the series of the rest: the sum of their |coefficient|
PENALIZED_PER_FOLD <- 3L          # rows per fold, at least
PENALIZED_AUC_PER_FOLD <- 10L     # cv.glmnet's own least for the AUC of a fold
PENALIZED_LABELLED <- 3L          # points named on a chart

# The notes the runner may write: an id each (note_<id>) and one sentence with no number or
# name, so that report_texts/penalized.json says them in the page's language.
PENALIZED_NOTES <- c(
  edge_small = paste0("The least cross-validated error is at the smallest λ of the path: the best penalty may be smaller ",
                      "still; widen the path with a fixed λ below it, or compare with the fit without a penalty."),
  edge_large = paste0("The least cross-validated error is at the largest λ of the path, where the penalty keeps almost ",
                      "nothing: out of fold, the predictors do not predict the outcome better than its mean."),
  all_zero_1se = paste0("Every coefficient is zero at the chosen λ, the largest within one standard error of the least ",
                        "error: lambda = cv_min keeps the terms of the least error."),
  all_zero = paste0("Every coefficient is zero at the chosen λ: out of fold, the predictors do not predict the outcome ",
                    "better than its mean."),
  largest_1se = paste0("The chosen λ is the largest of the path, within one standard error of the least error: the ",
                       "coefficients are shrunk almost to zero; lambda = cv_min keeps the terms of the least error."),
  largest = paste0("The chosen λ is the largest of the path: the coefficients are shrunk almost to zero, and the model ",
                   "predicts little more than the outcome's mean."),
  mixing_cv = paste0("The mixing was chosen by cross-validation among 0.1, 0.25, 0.5, 0.75, 0.9 and 1, on the same folds: ",
                     "the one with the least error at its best λ."),
  mixing_cv_fixed = paste0("The mixing was chosen by cross-validation among 0.1, 0.25, 0.5, 0.75, 0.9 and 1, on the same ",
                           "folds: the one with the least error at the fixed λ."),
  fixed_lambda = paste0("λ is fixed: it is added to glmnet's path, and the coefficients and the out-of-fold numbers are ",
                        "read at it."),
  grouped_folds = "The folds are grouped: all the rows of a value of the column fold_by fall in the same fold.",
  not_standardized = paste0("The predictors are penalized on their own scales (standardize = false): a predictor with ",
                            "large values is penalized less than one with small values."),
  case_weights = paste0("The weights are case weights: the fit, the cross-validation and the out-of-fold numbers weigh ",
                        "each row by them; the residuals' charts read √w × residual."),
  frequency_weights = "The weights are frequencies: n, the events, the rates and the checks count the cases the rows stand for.",
  zero_weights = "Rows of weight 0 were left out of the fit.",
  class_threshold = paste0("The misclassification error of the cross-validation counts at the threshold 0.5, as glmnet ",
                           "does; the classification of the fit is at the node's threshold.")
)

penalized_note <- function(rows, id) tab_text(rows, "model", paste0("note_", id), PENALIZED_NOTES[[id]])

fit_penalized <- function(task) {
  logistic <- identical(task$model, "logistic")
  p <- if (logistic) logistic_params(task$params) else linear_params(task$params)
  alpha <- as.numeric(p$alpha %||% 0.05)
  penalty <- p$penalty
  outcome <- p$outcome
  predictors <- unlist(p$predictors)
  weights_col <- p$weights
  fold_col <- p$fold_by
  has_int <- !isFALSE(p$intercept)
  standardize <- !isFALSE(p$standardize)
  folds <- as.integer(p$folds %||% 10L)
  measure <- p$measure %||% (if (logistic) "deviance" else "mse")
  threshold <- as.numeric(p$threshold %||% 0.5)
  lambda_choice <- p$lambda %||% "cv_1se"
  fixed <- if (is.numeric(lambda_choice)) as.numeric(lambda_choice) else NULL
  rows <- tab_rows_start(task)

  df <- tab_input(task)
  for (column in c(outcome, predictors, weights_col, fold_col)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  if (!logistic && !tab_kind(df[[outcome]]) %in% c("number", "integer")) {
    tab_refuse("outcome_not_number", paste0("the outcome «", outcome, "» is not a number: a linear regression explains a number ",
                                            "(for two values use the logistic regression)"), list(column = outcome))
  }
  if (!is.null(weights_col) && !tab_kind(df[[weights_col]]) %in% c("number", "integer")) {
    tab_refuse("weights_not_number", paste0("the weights «", weights_col, "» are not numbers"), list(column = weights_col))
  }
  design <- tab_design(df, predictors, outcome = outcome, reference = p$reference %||% list(),
                       interactions = p$interactions %||% list(), intercept = has_int, also = c(weights_col, fold_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  w <- rep(1, design$n)
  zero_weights <- 0L
  if (!is.null(weights_col)) {
    w <- as.numeric(design$data[[weights_col]])
    if (logistic && (any(w < 0) || any(w != round(w)))) {
      tab_refuse("weights_not_frequencies", paste0("the weights «", weights_col, "» are frequencies: whole numbers, 0 or more"),
                 list(column = weights_col))
    }
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
  ev <- NULL
  y <- if (logistic) {
    ev <- logistic_outcome(design$y, p$event_value, outcome)
    ev$y
  } else as.numeric(design$y)
  X <- design$X
  Xn <- if (has_int) X[, -1L, drop = FALSE] else X
  penalized_refuse_small(design, Xn, y, logistic, folds, measure)

  # ── the folds, then the cross-validated fits ──────────────────────────────
  tab_seed(task)
  foldid <- penalized_folds(design, fold_col, folds)
  family <- if (logistic) "binomial" else "gaussian"
  fit_at <- function(mixing) penalized_cv(Xn, y, w, family, mixing, foldid, measure, standardize, has_int, fixed)
  mixing <- switch(penalty, ridge = 0, lasso = 1, elastic_net = p$mixing %||% 0.5,
                   tab_refuse("penalty", paste0("unknown penalty «", penalty, "»")))
  by_cv <- identical(mixing, "cv")
  if (by_cv) {
    fits <- lapply(PENALIZED_MIXING_GRID, fit_at)
    score <- vapply(fits, function(f) f$cvm[[penalized_best(f, fixed)]], numeric(1))
    best <- if (identical(measure, "auc")) which.max(score) else which.min(score)
    cv <- fits[[best]]
    mixing <- PENALIZED_MIXING_GRID[[best]]
  } else {
    mixing <- as.numeric(mixing)
    cv <- fit_at(mixing)
  }
  path <- cv$lambda
  i_min <- cv$index["min", 1L]
  i_1se <- cv$index["1se", 1L]
  idx <- if (!is.null(fixed)) match(fixed, path) else if (identical(lambda_choice, "cv_min")) i_min else i_1se
  lam <- path[[idx]]
  beta <- as.matrix(cv$glmnet.fit$beta)
  b <- c(if (has_int) unname(cv$glmnet.fit$a0[[idx]]), unname(beta[, idx]))
  eta <- as.vector(X %*% b)
  n_selected <- sum(beta[, idx] != 0)

  # ── the out-of-fold predictions at the chosen λ ───────────────────────────
  pre <- unname(cv$fit.preval[, idx])
  sw <- sum(w)

  # ── the model section ─────────────────────────────────────────────────────
  tab_text(rows, "model", "formula", design$formula)
  tab_text(rows, "model", "family", family)
  if (logistic) tab_text(rows, "model", "link", "logit")
  tab_text(rows, "model", "penalty", penalty)
  tab_text(rows, "model", "cv_measure", measure)
  if (logistic) tab_text(rows, "model", "event_value", ev$event)
  tab_text(rows, "model", "software", tab_software(PENALIZED_PACKAGES))
  if (logistic) {
    prob <- stats::plogis(pre)
    auc <- logistic_auc(prob, y, w, alpha)
    cls <- logistic_classes(prob, y, w, threshold)
    tab_number(rows, "model", "n", sw)
    tab_number(rows, "model", "events", sum(w * y))
    tab_number(rows, "model", "n_missing", design$n_missing)
    tab_number(rows, "model", "auc", auc$auc)
    tab_number(rows, "model", "auc_ci_low", auc$low)
    tab_number(rows, "model", "auc_ci_high", auc$high)
    tab_number(rows, "model", "brier", sum(w * (prob - y)^2) / sw)
    tab_number(rows, "model", "threshold", threshold)
    tab_number(rows, "model", "accuracy", cls$accuracy)
    tab_number(rows, "model", "sensitivity", cls$sensitivity)
    tab_number(rows, "model", "specificity", cls$specificity)
    tab_number(rows, "model", "precision", cls$precision)
    # −2 × the log-likelihood of the out-of-fold probabilities, each case's log probability exact
    tab_number(rows, "model", "deviance", -2 * sum(w * ifelse(y == 1, stats::plogis(pre, log.p = TRUE),
                                                                stats::plogis(-pre, log.p = TRUE))))
  } else {
    tab_number(rows, "model", "n", design$n)
    tab_number(rows, "model", "n_missing", design$n_missing)
  }
  tab_number(rows, "model", "lambda", lam)
  tab_number(rows, "model", "log_lambda", log(lam))
  tab_number(rows, "model", "lambda_min", cv$lambda.min)
  tab_number(rows, "model", "lambda_1se", cv$lambda.1se)
  tab_number(rows, "model", "log_lambda_min", log(cv$lambda.min))
  tab_number(rows, "model", "log_lambda_1se", log(cv$lambda.1se))
  tab_number(rows, "model", "mixing", mixing)
  tab_number(rows, "model", "cv_value", cv$cvm[[idx]])
  tab_number(rows, "model", "cv_se", cv$cvsd[[idx]])
  tab_number(rows, "model", "n_selected", n_selected)
  if (!logistic) {
    e <- y - pre
    tab_number(rows, "model", "rmse", sqrt(sum(w * e^2) / sw))
    tab_number(rows, "model", "mae", sum(w * abs(e)) / sw)
    tab_number(rows, "model", "r_squared", 1 - sum(w * e^2) / sum(w * (y - sum(w * y) / sw)^2))
  }

  # ── coefficients at the chosen λ, on the original scale ───────────────────
  terms <- design$terms
  for (j in seq_along(b)) {
    level <- terms$term[[j]]
    level_2 <- if (is.na(terms$level[[j]])) NULL else terms$level[[j]]
    tab_number(rows, "coef", "estimate", b[[j]], level, level_2)
    if (logistic) tab_number(rows, "coef", "odds_ratio", exp(b[[j]]), level, level_2)
    tab_number(rows, "coef", "selected", as.numeric(b[[j]] != 0 || identical(level, "(Intercept)")), level, level_2)
  }

  # ── the cross-validation curve, one row group per λ of the path ───────────
  log_path <- log(path)
  tab_points(rows, "cv", NULL, "cv_mean", x = log_path, y = cv$cvm)
  tab_points(rows, "cv", NULL, "cv_low", x = log_path, y = cv$cvlo)
  tab_points(rows, "cv", NULL, "cv_high", x = log_path, y = cv$cvup)
  tab_points(rows, "cv", NULL, "n_selected", x = log_path, y = cv$nzero)

  # ── the choice of λ, and the other checks ─────────────────────────────────
  edge <- if (i_min == length(path) && length(path) > 1L) "edge_small" else if (i_min == 1L) "edge_large" else NULL
  # every coefficient zero, or (ridge) the largest λ of the path, where they are all but zero
  one_se <- identical(lambda_choice, "cv_1se") && i_min != idx
  zero <- if (n_selected == 0L) {
    if (one_se && sum(beta[, i_min] != 0) > 0) "all_zero_1se" else "all_zero"
  } else if (idx == 1L && length(path) > 1L) {
    if (one_se) "largest_1se" else "largest"
  }
  if (logistic) {
    dat <- data.frame(y = y, Xn, check.names = FALSE)
    rownames(dat) <- NULL
    dat$.w <- w
    form <- stats::reformulate(colnames(Xn), response = "y", intercept = has_int)
    separated <- logistic_separation(form, stats::binomial("logit"), dat, design$terms)
    penalized_logistic_checks(rows, separated, sum(w * y), sw, ncol(Xn))
  }
  if (identical(edge, "edge_large") && idx == 1L) zero <- NULL  # its note says it already
  doubts <- c(if (!is.null(edge)) "the least error is at an edge of the path",
              if (!is.null(zero)) if (startsWith(zero, "all_zero")) "every coefficient is zero" else
                "the chosen λ is the largest of the path")
  linear_check(rows, "lambda_choice", "The least error is inside the path of λ, and the chosen λ keeps some terms.",
               if (length(doubts)) "doubtful" else "ok",
               if (length(doubts)) paste0("Read the notes: ", paste(doubts, collapse = "; "), ".") else "Nothing to do.")

  # ── the charts' points ────────────────────────────────────────────────────
  penalized_paths(rows, cv$glmnet.fit, design$terms[if (has_int) -1L else seq_len(nrow(design$terms)), , drop = FALSE], Xn, w, idx)
  if (logistic) {
    hl <- logistic_hosmer_lemeshow(prob, y, w)
    logistic_classification_charts(rows, task, prob, y, w, hl, ev)
    logistic_cells(rows, cls, ev)
  } else {
    penalized_residual_charts(rows, task, pre, sqrt(w) * (y - pre), design$rows)
  }

  # ── notes ─────────────────────────────────────────────────────────────────
  if (!is.null(edge)) penalized_note(rows, edge)
  if (!is.null(zero)) penalized_note(rows, zero)
  if (by_cv) penalized_note(rows, if (is.null(fixed)) "mixing_cv" else "mixing_cv_fixed")
  if (!is.null(fixed)) penalized_note(rows, "fixed_lambda")
  if (!is.null(fold_col)) penalized_note(rows, "grouped_folds")
  if (!standardize) penalized_note(rows, "not_standardized")
  if (!is.null(weights_col)) penalized_note(rows, if (logistic) "frequency_weights" else "case_weights")
  if (zero_weights > 0L) penalized_note(rows, "zero_weights")
  if (logistic && identical(measure, "class") && threshold != 0.5) penalized_note(rows, "class_threshold")

  # ── the card, and the rows output ─────────────────────────────────────────
  card <- tab_card(task, design, PENALIZED_PACKAGES)
  card$outcome <- if (logistic) list(name = outcome, event_value = ev$event, levels = list(ev$other, ev$event)) else
    list(name = outcome)
  card$coefficients <- as.list(b)
  card$link <- if (logistic) "logit" else "identity"
  if (logistic) card$threshold <- threshold
  card$alpha <- alpha
  card$penalty <- list(penalty = penalty, lambda = lam, mixing = mixing, standardize = standardize,
                       lambda_choice = if (is.null(fixed)) lambda_choice else "fixed", lambda_min = cv$lambda.min,
                       lambda_1se = cv$lambda.1se, measure = measure, folds = folds)
  penalized_check_card(task, card, design, cv$glmnet.fit, Xn, lam, logistic)

  output <- NULL
  if (identical(task$output %||% p$output, "rows")) output <- penalized_rows_output(df, card, design, y, eta, logistic)
  list(rows = rows, output = output, card = card, native = cv)
}

# Refused before any fit, with a sentence: too few rows for the folds, a design of one column
# (glmnet needs two), too few cases of the rarer outcome, the AUC on folds too small.
penalized_refuse_small <- function(design, Xn, y, logistic, folds, measure) {
  n <- design$n
  if (n < PENALIZED_PER_FOLD * folds) {
    tab_refuse("too_few_rows", paste0("the model has ", n, " rows it can use: cross-validation in ", folds, " folds needs at least ",
                                      PENALIZED_PER_FOLD * folds, " (", PENALIZED_PER_FOLD, " in each fold): choose fewer folds"),
               list(rows = n, folds = folds))
  }
  if (ncol(Xn) < 2L) {
    tab_refuse("one_term", paste0("a penalty needs at least two terms besides the intercept, and the model has ", ncol(Xn),
                                  ": add a predictor, or turn the penalty off"), list(terms = ncol(Xn)))
  }
  if (logistic) {
    rarer <- min(sum(y == 1), sum(y == 0))
    if (rarer < folds) {
      tab_refuse("too_few_events", paste0("the rarer value of the outcome is on ", rarer, " rows: cross-validation in ", folds,
                                          " folds needs at least one in each fold: choose fewer folds"),
                 list(rows = rarer, folds = folds))
    }
  }
  if (identical(measure, "auc") && n < PENALIZED_AUC_PER_FOLD * folds) {
    tab_refuse("auc_few_rows", paste0("the AUC of a fold needs at least ", PENALIZED_AUC_PER_FOLD, " rows in it: with ", n,
                                      " rows choose at most ", max(3L, n %/% PENALIZED_AUC_PER_FOLD), " folds, or measure = deviance"),
               list(rows = n, folds = folds))
  }
  invisible(TRUE)
}

# The fold of each row, from the node's seed (the caller set it): as cv.glmnet draws them, or by
# the values of `fold_col` in text order, each value drawn into a fold the same way.
penalized_folds <- function(design, fold_col, folds) {
  if (is.null(fold_col)) return(sample(rep(seq_len(folds), length.out = design$n)))
  labels <- tab_label(design$data[[fold_col]])
  groups <- tab_sort_labels(labels)
  if (length(groups) < folds) {
    tab_refuse("too_few_groups", paste0("the column «", fold_col, "» has ", length(groups), " values: grouped folds need at least ",
                                        "as many values as folds (", folds, "): choose fewer folds, or another column"),
               list(column = fold_col, values = length(groups), folds = folds))
  }
  of_group <- sample(rep(seq_len(folds), length.out = length(groups)))
  of_group[match(labels, groups)]
}

# cv.glmnet on the given folds, the out-of-fold predictions kept. A fixed λ is added to the path
# glmnet makes for these rows, so that every number is read at it.
penalized_cv <- function(X, y, w, family, mixing, foldid, measure, standardize, intercept, fixed) {
  lambda <- NULL
  if (!is.null(fixed)) {
    path <- glmnet::glmnet(X, y, weights = w, family = family, alpha = mixing, standardize = standardize,
                           intercept = intercept)$lambda
    lambda <- sort(unique(c(path, fixed)), decreasing = TRUE)
  }
  glmnet::cv.glmnet(X, y, weights = w, family = family, alpha = mixing, standardize = standardize, intercept = intercept,
                    lambda = lambda, foldid = foldid, type.measure = measure, keep = TRUE)
}

# The λ by which a mixing is judged: its least error, or the fixed λ.
penalized_best <- function(cv, fixed) if (is.null(fixed)) cv$index["min", 1L] else match(fixed, cv$lambda)

# Separation (a note-like row: the penalty keeps the estimates finite), Hosmer–Lemeshow (a test of
# an unpenalized fit: not computed, the calibration chart out of fold is the diagnostic), the
# events per variable.
penalized_logistic_checks <- function(rows, separated, events, n, k) {
  h0 <- "No combination of predictors separates the outcome completely."
  if (!is.null(separated$error)) {
    linear_check(rows, "separation", h0, "not_computed", paste0("It could not be computed on these rows: ", separated$error))
  } else if (length(separated$terms)) {
    for (term in separated$terms) {
      linear_check(rows, "separation", h0, "doubtful",
                   "The term separates the outcome: the penalty keeps its estimate finite, but its size depends on λ.",
                   level_2 = term)
    }
  } else {
    linear_check(rows, "separation", h0, "ok", "Nothing to do.")
  }
  linear_check(rows, "hosmer_lemeshow", "The predicted probabilities match the observed rates in 10 groups of risk.",
               "not_computed", paste0("With a penalty the test is not computed: it assumes an unpenalized fit; the calibration ",
                                      "chart, out of fold, is the diagnostic."))
  epv <- min(events, n - events) / max(k, 1L)
  linear_check(rows, "epv", "At least 10 events for each term of the model.", if (epv < LOGISTIC_EPV) "doubtful" else "ok",
               "Few events for the number of terms: the penalty helps; fewer terms help more.", threshold = LOGISTIC_EPV,
               value = epv)
  invisible(rows)
}

# The paths of the coefficients against log λ, on the original scale: the PENALIZED_PATH_TERMS
# terms that enter first (ties: the larger standardized coefficient at the chosen λ), the rest
# summed up as one series, the sum of their |coefficient|.
penalized_paths <- function(rows, fit, terms, Xn, w, idx) {
  beta <- as.matrix(fit$beta)
  x <- log(fit$lambda)
  enters <- apply(beta != 0, 1L, function(z) if (any(z)) which(z)[[1L]] else .Machine$integer.max)
  sw <- sum(w)
  sd_w <- apply(Xn, 2L, function(col) sqrt(sum(w * (col - sum(w * col) / sw)^2) / sw))
  size <- abs(beta[, idx]) * sd_w
  order_ <- order(enters, -size, seq_len(nrow(beta)))
  shown <- head(order_, PENALIZED_PATH_TERMS)
  for (j in shown) tab_points(rows, "curve", "paths", "y", x = x, y = beta[j, ], level_2 = terms$label[[j]])
  rest <- setdiff(order_, shown)
  if (length(rest)) {
    tab_points(rows, "curve", "paths", "y", x = x, y = colSums(abs(beta[rest, , drop = FALSE])), level_2 = PENALIZED_OTHERS)
  }
  invisible(rows)
}

# The residuals out of fold against the out-of-fold fitted values, their lowess, their normal Q–Q
# and histogram: read as diagnostics. `wr`: √w × residual.
penalized_residual_charts <- function(rows, task, fitted, wr, row_numbers) {
  most <- tab_limit(task, "max_chart_points")
  keep <- tab_limit(task, "extreme_points")
  i <- tab_cap(length(wr), max = most, score = wr, keep = keep)
  labels <- tab_extreme_labels(wr, row_numbers, keep = PENALIZED_LABELLED)[i]
  tab_points(rows, "point", "resid_fitted", "y", x = fitted[i], y = wr[i], labels = labels)
  lo <- tab_lowess(fitted, wr)
  if (nrow(lo)) tab_points(rows, "curve", "resid_fitted", "y", x = lo$x, y = lo$y, level_2 = "lowess")
  qq <- tab_qq(wr, tab_limit(task, "qq_points"))
  if (nrow(qq)) {
    tab_points(rows, "point", "qq", "y", x = qq$x, y = qq$y)
    ab <- tab_qq_line(wr)
    ends <- range(qq$x)
    tab_points(rows, "curve", "qq", "y", x = ends, y = ab[["intercept"]] + ab[["slope"]] * ends, level_2 = "line")
  }
  h <- tab_hist(wr)
  if (nrow(h)) tab_points(rows, "hist", "residual", "count", x = h$x0, y = h$count, x1 = h$x1)
  invisible(rows)
}

# The card's coefficients give glmnet's own predictions at the chosen λ: checked in the self-test
# (a reference case), where a difference is a failure.
penalized_check_card <- function(task, card, design, fit, Xn, lam, logistic) {
  if (!identical(task$group_version, "selftest")) return(invisible(TRUE))
  d <- tab_design_rows(card, design$data)
  b <- unlist(card$coefficients)
  again <- as.vector(d$X %*% b)
  own <- as.vector(stats::predict(fit, newx = Xn, s = lam, type = "link"))
  if (logistic) {
    again <- stats::plogis(again)
    own <- stats::plogis(own)
  }
  gap <- max(abs(again - own) / pmax(1, abs(own)))
  if (!all(d$ok) || !is.finite(gap) || gap > 1e-9) stop("the card does not give back glmnet's predictions (", gap, ")")
  invisible(TRUE)
}

# The rows output: the input's columns, then the model's. Every row whose predictors are present
# gets its prediction from the card; the rows the fit used also their residual (linear).
penalized_rows_output <- function(df, card, design, y, eta, logistic) {
  made <- if (logistic) c("pred_prob", "pred_class", "pred_linear") else c("pred_fitted", "pred_residual")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- tab_design_rows(card, df)
  used <- design$rows
  out <- df
  if (logistic) {
    pr <- logistic_predict_rows(card, d$X, d$ok)
    out$pred_prob <- pr$prob
    out$pred_prob[used] <- stats::plogis(eta)
    out$pred_class <- logistic_class_of(card, out$pred_prob, card$threshold)
    out$pred_linear <- pr$eta
    out$pred_linear[used] <- eta
  } else {
    fit <- as.vector(d$X %*% unlist(card$coefficients))
    fit[!d$ok] <- NA_real_
    out$pred_fitted <- fit
    out$pred_fitted[used] <- eta
    out$pred_residual <- rep(NA_real_, nrow(df))
    out$pred_residual[used] <- y - eta
  }
  rownames(out) <- NULL
  out
}

# Predict: the card applied to new rows (mode predict). The logistic as predict_logistic does it
# (the probability and, with a threshold, the class); the linear its fitted value, with no
# prediction interval (none after a penalized selection).
predict_penalized <- function(task, card) {
  if (identical(card$model, "logistic")) return(predict_logistic(task, card))
  df <- tab_input(task)
  if ("prediction" %in% names(df)) {
    tab_refuse("output_column_exists", "the rows already have a column «prediction»: rename it before the node",
               list(column = "prediction"))
  }
  if (isTRUE(task$params$interval)) warning("a penalized model has no prediction interval: only the prediction is added")
  d <- tab_design_rows(card, df)
  if (d$unseen > 0L) warning(d$unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  fit <- as.vector(d$X %*% unlist(card$coefficients))
  fit[!d$ok] <- NA_real_
  out <- df
  out$prediction <- fit
  rownames(out) <- NULL
  list(output = out)
}
