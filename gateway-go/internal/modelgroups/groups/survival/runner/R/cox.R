# Cox proportional hazards (docs/specs/statistical-models.md, `cox`, lane M5): survival::coxph
# with Efron's, Breslow's or the exact partial likelihood for ties, strata (a baseline each),
# counting-process rows (`start`: start–stop, time-varying covariates), classical or robust
# standard errors (clustered with `cluster`). The coefficients as log hazard ratios and hazard
# ratios with their intervals at 1 − alpha (summary.coxph's), the likelihood-ratio, Wald and
# score tests, the concordance.
#
# The checks (section `assumption`), reported, never enforced:
#   schoenfeld         proportional hazards per term: the Grambsch–Therneau test on the scaled
#                      Schoenfeld residuals (survival::cox.zph, transform = "km", one test a
#                      term: a category's columns together); level_2 the term;
#   schoenfeld_global  the same, every term at once;
#   martingale_form    the linear form of each numeric predictor: the likelihood-ratio test of
#                      a natural spline of it (splines::ns, 3 df) added to the model, with the
#                      martingale residuals of the null model against it on its chart;
#   dfbeta             influence per term: failed when a row's removal moves a coefficient by
#                      more than its standard error (|dfbetas| above 1); the five largest rows
#                      above 2/√n named, whatever the verdict;
#   vif                the variance inflation of each term on the design (GVIF^(1/df) for a
#                      term of several columns, read against 5 and 10);
#   epv                events per coefficient, at least 10.
# A check that cannot run on the rows is «not_computed» with the reason, never a crash.
#
# Charts (level = the chart's id, level_2 = the series): point/curve `schoenfeld` (level_2 the
# predictor as params.predictors names it; points y, curves smooth, lower, upper: the smooth of
# plot.cox.zph, a natural spline of 4 df on the KM scale with its ± 2 SE band, drawn against
# time), point `martingale` (level_2 the numeric predictor; curve lowess), point
# `deviance_linear` (deviance residuals against the centred linear predictor, the largest
# named), curve `adjusted_survival` (survfit at the fit's means, level_2 the stratum: «all»
# without strata). At most max_chart_points a chart, sampled with the node's seed, the extremes
# kept.
#
# The card: the coefficients, the centring (the fit's means: survival centres a 0/1 column at
# 0), the baseline cumulative hazard per stratum at its event times (at most
# max_baseline_times), where each stratum's follow-up ends. Predict: the risk (exp of the
# centred linear predictor) and the survival at the times asked, exp(−H0(t) · risk).
#
# Columns never become code: the design (tab_design) has positional columns, grouped into the
# matrix columns t1, t2… of a frame of fixed names (time, status, start, g, cl) before coxph,
# cox.zph or survfit see them; a column's name is only ever written as a label.

COX_PACKAGES <- c("survival", "splines")
COX_EPV <- 10
COX_VIF_DOUBTFUL <- 5
COX_VIF_FAILED <- 10
COX_DFBETAS_FAILED <- 1   # |dfbetas| above which a row moves a coefficient by more than its standard error
COX_FORM_DF <- 3          # the spline of the linear-form test
COX_FORM_MIN_VALUES <- 5  # a predictor with fewer distinct values has no curve to tell from a line
COX_SMOOTH_DF <- 4        # plot.cox.zph's smooth
COX_SMOOTH_POINTS <- 40
COX_MAX_TERMS_CHARTED <- 12
COX_LABELLED <- 3         # points named on the deviance chart
COX_ALL <- "all"          # the stratum's name without strata

cox_params <- function(params) {
  p <- tab_model_spec("cox")$defaults
  for (k in names(params)) p[k] <- list(params[[k]])
  p
}

fit_cox <- function(task) {
  p <- cox_params(task$params)
  alpha <- as.numeric(p$alpha %||% 0.05)
  ties <- p$ties %||% "efron"
  time_col <- p$time
  event_col <- p$event
  start_col <- p$start
  strata_col <- p$strata
  robust <- identical(p$se, "robust")
  cluster_col <- if (robust) p$cluster else NULL
  predictors <- unlist(p$predictors)
  interactions <- p$interactions %||% list()

  df <- tab_input(task)
  for (column in c(time_col, event_col, start_col, strata_col, cluster_col, predictors)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  for (column in c(time_col, start_col)) {
    kind <- tab_kind(df[[column]])
    if (!kind %in% c("number", "integer")) {
      tab_refuse("time_type", paste0("the column «", column, "» is a ", kind, ": a time must be a number, the time from the ",
                                     "start (compute it from the dates first)"), list(column = column))
    }
  }
  design <- tab_design(df, predictors, reference = p$reference %||% list(), interactions = interactions, intercept = FALSE,
                       also = c(time_col, event_col, start_col, strata_col, cluster_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  d <- design$data
  time <- as.numeric(d[[time_col]])
  start <- if (is.null(start_col)) NULL else as.numeric(d[[start_col]])
  status <- km_is_event(d[[event_col]], p$event_value)
  value_label <- km_value_label(p$event_value)
  if (!nrow(d)) tab_refuse("no_rows", "no row has a value in every column the model uses: nothing to fit")
  if (!any(status)) {
    tab_refuse("no_events", paste0("the column «", event_col, "» is never «", value_label, "» in the rows the model uses: ",
                                   "with no event there is nothing to fit; check event_value"),
               list(column = event_col, event_value = value_label))
  }
  if (is.null(start) && any(time < 0)) {
    tab_refuse("negative_time", paste0("the column «", time_col, "» holds a negative time (", tab_number_label(min(time)),
                                       "): a time is counted from the start, 0 or more"), list(column = time_col))
  }
  if (!is.null(start)) cox_refuse_start(start, time, design$rows, start_col, time_col)
  if (is.null(strata_col)) {
    strata_labels <- rep(COX_ALL, nrow(d))
    strata_levels <- COX_ALL
    strata_names <- COX_ALL
  } else {
    strata_labels <- tab_label(d[[strata_col]])
    strata_levels <- tab_sort_labels(strata_labels)
    if (length(strata_levels) > tab_limit(task, "max_strata")) {
      tab_refuse("too_many_strata", paste0("the column «", strata_col, "» makes ", length(strata_levels), " strata (at most ",
                                           tab_limit(task, "max_strata"), "): is it the right column?"),
                 list(column = strata_col, strata = length(strata_levels)))
    }
    strata_names <- paste0(strata_col, "=", strata_levels)
  }
  X <- design$X
  k <- ncol(X)
  if (design$n < k + 2L) {
    tab_refuse("too_few_rows", paste0("the model has ", k, " terms and ", design$n, " rows it can use: it needs at least ", k + 2L,
                                      " rows (more rows than terms + 1)"), list(rows = design$n, terms = k))
  }
  cox_refuse_aliased(design)

  # ── the fit, on a frame of fixed names ────────────────────────────────────
  groups <- cox_term_groups(design)
  frame <- data.frame(time = time, status = as.integer(status))
  if (!is.null(start)) frame$start <- start
  for (j in seq_along(groups$columns)) frame[[paste0("t", j)]] <- X[, groups$columns[[j]], drop = FALSE]
  frame$g <- factor(strata_labels, levels = strata_levels)
  if (!is.null(cluster_col)) frame$cl <- tab_label(d[[cluster_col]])
  rhs <- c(paste0("t", seq_along(groups$columns)), if (!is.null(strata_col)) "survival::strata(g)")
  form <- cox_formula(rhs, !is.null(start))
  notes <- character()
  fit <- cox_coxph(form, frame, ties, robust, !is.null(cluster_col), notes = function(n) notes <<- c(notes, n))
  b <- stats::coef(fit)
  # the exact partial likelihood sums over every way a time's tied events could be ordered: with
  # many events at one time it overflows, and coxph gives empty coefficients and a log-likelihood
  # of −Inf without a warning. The design is not singular (cox_refuse_aliased said so): not «aliased»
  if (identical(ties, "exact") && (anyNA(b) || !all(is.finite(fit$loglik)))) {
    largest <- max(table(paste(as.integer(frame$g), time)[status]))
    tab_refuse("exact_failed", paste0("the exact partial likelihood cannot be computed on these rows: up to ", largest,
                                      " events share one time, too many to order every way; fit with ties = efron, which ",
                                      "approximates it closely"), list(largest = largest))
  }
  if (anyNA(b)) {
    tab_refuse("aliased", paste0("the term «", design$terms$label[which(is.na(b))[[1L]]], "» is a combination of the others ",
                                 "(the design is singular): drop it, or a predictor that repeats it"))
  }
  s <- summary(fit, conf.int = 1 - alpha)
  coefs <- s$coefficients
  ci <- s$conf.int
  n <- fit$n
  events <- fit$nevent
  rows <- tab_rows_start(task)

  # ── the model section ─────────────────────────────────────────────────────
  formula <- cox_formula_text(time_col, event_col, start_col, value_label, predictors, interactions, strata_col, cluster_col)
  tab_text(rows, "model", "formula", formula)
  tab_text(rows, "model", "software", tab_software(COX_PACKAGES))
  tab_text(rows, "model", "event_value", value_label)
  tab_text(rows, "model", "ties", ties)
  tab_text(rows, "model", "se_type", if (robust) "robust" else "classical")
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "events", events)
  tab_number(rows, "model", "n_missing", design$n_missing)
  # summary.coxph rounds the Wald statistic to two decimals for printing: the fit's own is used
  wald <- c(test = unname(fit$wald.test), df = s$waldtest[["df"]],
            pvalue = stats::pchisq(unname(fit$wald.test), s$waldtest[["df"]], lower.tail = FALSE))
  for (test in list(c("lr", "logtest"), c("wald", "waldtest"), c("score", "sctest"))) {
    x <- if (test[[1L]] == "wald") wald else s[[test[[2L]]]]
    tab_number(rows, "model", paste0(test[[1L]], "_statistic"), x[["test"]])
    tab_number(rows, "model", paste0(test[[1L]], "_df"), x[["df"]])
    tab_p(rows, "model", x[["pvalue"]], stat = paste0(test[[1L]], "_p_value"))
  }
  tab_number(rows, "model", "concordance", s$concordance[[1L]])
  tab_number(rows, "model", "concordance_se", s$concordance[[2L]])
  tab_number(rows, "model", "log_lik", fit$loglik[[2L]])
  tab_number(rows, "model", "aic", stats::AIC(fit))

  # ── coefficients ──────────────────────────────────────────────────────────
  vif <- cox_vif(X, design$terms)
  terms <- design$terms
  for (j in seq_len(k)) {
    level <- terms$term[[j]]
    level_2 <- if (is.na(terms$level[[j]])) NULL else terms$level[[j]]
    tab_number(rows, "coef", "estimate", coefs[j, "coef"], level, level_2)
    tab_number(rows, "coef", "hazard_ratio", coefs[j, "exp(coef)"], level, level_2)
    tab_number(rows, "coef", "hr_ci_low", ci[j, 3L], level, level_2)
    tab_number(rows, "coef", "hr_ci_high", ci[j, 4L], level, level_2)
    tab_number(rows, "coef", "std_error", coefs[j, "se(coef)"], level, level_2)
    if (robust) tab_number(rows, "coef", "robust_se", coefs[j, "robust se"], level, level_2)
    tab_number(rows, "coef", "statistic", coefs[j, "z"], level, level_2)
    tab_p(rows, "coef", coefs[j, "Pr(>|z|)"], level = level, level_2 = level_2)
    at <- match(terms$term_index[[j]], vif$term_index)
    if (!is.na(at)) tab_number(rows, "coef", "vif", vif$value[[at]], level, level_2)
  }

  # ── the checks and the charts ─────────────────────────────────────────────
  lp <- as.vector(X %*% b) - sum(b * fit$means)
  zph <- tryCatch(survival::cox.zph(fit, transform = "km", terms = TRUE), error = function(e) e)
  cox_assumptions(rows, fit = fit, frame = frame, form_rhs = rhs, start = !is.null(start), ties = ties, alpha = alpha,
                  design = design, groups = groups, zph = zph, vif = vif, events = events)
  cox_charts(rows, task, fit = fit, frame = frame, design = design, groups = groups, zph = zph, lp = lp,
             strata_names = strata_names, start = !is.null(start))

  # ── notes ─────────────────────────────────────────────────────────────────
  for (n_ in notes) tab_note(rows, n_)
  epv <- events / k
  if (epv < COX_EPV) {
    tab_note(rows, paste0("There are ", format(round(epv, 1), nsmall = 1), " events for each coefficient (", events, " events, ",
                          k, " coefficients): the estimates are unstable below ", COX_EPV, "; fewer terms, or more rows."))
  }
  if (robust) {
    tab_note(rows, paste0("The standard errors are robust", if (!is.null(cluster_col)) paste0(", clustered by «", cluster_col, "»") else "",
                          ": the intervals, the z tests and the Wald test use them; the likelihood-ratio and score tests assume ",
                          "the rows independent."))
  }
  if (robust && is.null(cluster_col) && !is.null(start)) {
    tab_note(rows, paste0("With start–stop rows, several rows are usually one subject's: name the subject's column as cluster, ",
                          "or the robust errors treat each row as independent."))
  }
  if (length(interactions)) {
    tab_note(rows, paste0("The terms of an interaction are correlated with their parts by construction: their VIFs are large ",
                          "for that reason; centre the predictors to read them."))
  }

  # ── the card, and the rows output ─────────────────────────────────────────
  card <- cox_card(task, design, fit, formula, time_col, event_col, start_col, strata_col, value_label, ties, alpha, time,
                   strata_labels, strata_levels, notes = function(n) tab_note(rows, n))
  cox_check_card(task, card, design, fit, frame)
  output <- NULL
  if (identical(task$output %||% p$output, "rows")) output <- cox_rows_output(df, card, design, fit)
  list(rows = rows, output = output, card = card, native = fit)
}

# The design's columns grouped by term (a predictor, an interaction): one matrix column of the
# frame each, so that coxph and cox.zph read a category's columns as one term.
cox_term_groups <- function(design) {
  index <- design$terms$term_index
  ids <- unique(index)
  list(columns = lapply(ids, function(t) which(index == t)),
       label = vapply(ids, function(t) design$terms$term[index == t][[1L]], ""),
       many = vapply(ids, function(t) sum(index == t) > 1L, logical(1)))
}

cox_formula <- function(rhs, start) {
  lhs <- if (start) "survival::Surv(start, time, status)" else "survival::Surv(time, status)"
  stats::as.formula(paste(lhs, "~", paste(rhs, collapse = " + ")), env = new.env(parent = asNamespace("survival")))
}

# «Surv(days, status = failed) ~ load + line + strata(plant)»: for people, never parsed.
cox_formula_text <- function(time_col, event_col, start_col, value_label, predictors, interactions, strata_col, cluster_col) {
  lhs <- paste0("Surv(", if (!is.null(start_col)) paste0(start_col, ", ") else "", time_col, ", ", event_col, " = ", value_label, ")")
  rhs <- c(predictors, vapply(interactions, function(x) paste(x[[1L]], x[[2L]], sep = " × "), ""),
           if (!is.null(strata_col)) paste0("strata(", strata_col, ")"), if (!is.null(cluster_col)) paste0("cluster(", cluster_col, ")"))
  paste(lhs, "~", paste(rhs, collapse = " + "))
}

# coxph, its warnings turned into the notes they mean.
cox_coxph <- function(form, frame, ties, robust, clustered, notes = function(n) NULL) {
  infinite <- FALSE
  handler <- function(w) {
    m <- conditionMessage(w)
    if (grepl("coefficient may be infinite", m, fixed = TRUE)) infinite <<- TRUE
    else if (grepl("did not converge", m, fixed = TRUE)) notes("The fit did not converge in the iterations allowed: read its numbers with care.")
    invokeRestart("muffleWarning")
  }
  fit <- withCallingHandlers(
    if (clustered) survival::coxph(form, data = frame, ties = ties, cluster = cl, model = TRUE, x = TRUE)
    else survival::coxph(form, data = frame, ties = ties, robust = robust, model = TRUE, x = TRUE),
    warning = handler)
  if (infinite) {
    notes(paste0("The likelihood keeps growing as a coefficient grows (monotone likelihood: a predictor separates the rows that ",
                 "have the event from the others at some time): that coefficient and its interval mean nothing; drop or ",
                 "merge the predictor, or its rare levels."))
  }
  fit
}

# Each row is an interval (start, time]: a start at or after its time is refused, naming the row.
# So is a start that survival cannot tell from its time: its aeqSurv merges times closer than
# sqrt(.Machine$double.eps) (absolute, or relative to the mean of the times) before coxph, survfit
# or flexsurv see them, and stops with an R error, «an interval has effective length 0», when a
# start and its time fall together (cox_empty_interval: the same rule on the same rows). Shared
# with the parametric node. `rows`: the input's row numbers of the rows given.
cox_refuse_start <- function(start, time, rows, start_col, time_col) {
  i <- which(start >= time)
  close <- !length(i)
  i <- if (close) cox_empty_interval(start, time) else i[[1L]]
  if (is.na(i)) return(invisible(TRUE))
  tab_refuse("start_not_before_time", paste0("in row ", rows[[i]], " the start «", start_col, "» (", tab_number_label(start[[i]]),
                                             ") is not before the time «", time_col, "» (", tab_number_label(time[[i]]), ")",
                                             if (close) ", by more than survival tells apart" else "",
                                             ": each row is an interval (start, time]"),
             list(row = rows[[i]], start = start_col, time = time_col))
}

# The first of the intervals (start, time] that survival::aeqSurv makes empty, NA when none:
# aeqSurv's own rule (survival 3.8), on the same rows.
cox_empty_interval <- function(start, time) {
  tolerance <- sqrt(.Machine$double.eps)
  y <- sort(unique(c(start, time)))
  y <- y[is.finite(y)]
  if (length(y) < 2L) return(NA_integer_)
  dy <- diff(y)
  tied <- dy <= tolerance | dy / mean(abs(y)) <= tolerance
  if (!any(tied)) return(NA_integer_)
  cuts <- y[c(TRUE, !tied)]
  empty <- which(findInterval(start, cuts) == findInterval(time, cuts) & start != time)
  if (length(empty)) empty[[1L]] else NA_integer_
}

# A column constant over the rows, or a combination of the others and the baseline (which
# takes any constant): named, so that the refusal says which to drop.
cox_refuse_aliased <- function(design) {
  X <- design$X
  q <- qr(cbind(1, X), tol = 1e-7)
  if (q$rank == ncol(X) + 1L) return(invisible(NULL))
  bad <- q$pivot[seq.int(q$rank + 1L, ncol(X) + 1L)] - 1L
  bad <- bad[bad > 0L]
  label <- design$terms$label[[if (length(bad)) bad[[1L]] else 1L]]
  tab_refuse("aliased", paste0("the term «", label, "» is constant, or a combination of the others (the design is singular): ",
                               "drop it, or a predictor that repeats it"), list(terms = as.list(design$terms$label[bad])))
}

# The variance inflation of each term on the design (no intercept in a Cox model: the
# correlations of the design's columns), Fox and Monette's generalized VIF for a term of
# several columns; the value read against 5 and 10 is GVIF^(1/df) (the VIF itself for one).
cox_vif <- function(X, terms) {
  out <- data.frame(term_index = integer(), term = character(), gvif = numeric(), df = integer(), value = numeric())
  if (!ncol(X)) return(out)
  R <- stats::cor(X)
  det_r <- det(R)
  index <- terms$term_index
  for (t in unique(index)) {
    sub <- which(index == t)
    gvif <- det(R[sub, sub, drop = FALSE]) * det(R[-sub, -sub, drop = FALSE]) / det_r
    out[nrow(out) + 1L, ] <- list(t, terms$term[sub[[1L]]], gvif, length(sub), gvif^(1 / length(sub)))
  }
  out
}

# One check's rows, in the order the report reads them.
cox_check <- function(rows, id, h0, verdict, advice, level_2 = NULL, statistic = NULL, df = NULL, p = NULL, threshold = NULL,
                      value = NULL, count = NULL, at_rows = NULL) {
  section <- "assumption"
  tab_text(rows, section, "h0", h0, id, level_2)
  if (!is.null(statistic)) tab_number(rows, section, "statistic", statistic, id, level_2)
  if (!is.null(df)) tab_number(rows, section, "df", df, id, level_2)
  if (!is.null(p)) tab_p(rows, section, p, level = id, level_2 = level_2)
  if (!is.null(threshold)) tab_number(rows, section, "threshold", threshold, id, level_2)
  if (!is.null(value)) tab_number(rows, section, "value", value, id, level_2)
  if (!is.null(count)) tab_number(rows, section, "count", count, id, level_2)
  tab_text(rows, section, "verdict", verdict, id, level_2)
  tab_text(rows, section, "advice", advice, id, level_2)
  if (length(at_rows)) tab_text(rows, section, "rows", paste(at_rows, collapse = ", "), id, level_2)
  invisible(rows)
}

cox_verdict_p <- function(p, alpha) if (is.na(p)) "not_computed" else if (p < alpha) "failed" else "ok"

cox_assumptions <- function(rows, fit, frame, form_rhs, start, ties, alpha, design, groups, zph, vif, events) {
  labels <- groups$label
  # proportional hazards, per term and global
  h0 <- "The effect of the term does not change over time."
  h0_global <- "No effect of the model changes over time."
  if (inherits(zph, "error")) {
    reason <- paste0("It could not be computed on these rows: ", conditionMessage(zph))
    for (label in labels) cox_check(rows, "schoenfeld", h0, "not_computed", reason, level_2 = label)
    cox_check(rows, "schoenfeld_global", h0_global, "not_computed", reason)
  } else {
    table_ <- zph$table
    for (j in seq_along(labels)) {
      cox_check(rows, "schoenfeld", h0, cox_verdict_p(table_[j, "p"], alpha),
                paste0("The effect of «", labels[[j]], "» changes over time: stratify by it, or let it interact with time."),
                level_2 = labels[[j]], statistic = table_[j, "chisq"], df = table_[j, "df"], p = table_[j, "p"])
    }
    g <- table_["GLOBAL", ]
    cox_check(rows, "schoenfeld_global", h0_global, cox_verdict_p(g[["p"]], alpha),
              "Some effect changes over time: look at the terms' tests and charts.",
              statistic = g[["chisq"]], df = g[["df"]], p = g[["p"]])
  }

  # the linear form of each numeric predictor: a natural spline of it added, the LR test
  h0 <- "The numeric predictor acts linearly on the log hazard."
  for (j in seq_along(labels)) {
    input <- cox_numeric_input(design, labels[[j]])
    if (is.null(input)) next
    x <- as.numeric(design$data[[input]])
    if (length(unique(x)) < COX_FORM_MIN_VALUES) {
      cox_check(rows, "martingale_form", h0, "not_computed",
                paste0("It needs at least ", COX_FORM_MIN_VALUES, " distinct values of the predictor: a curve cannot be told from a line."),
                level_2 = input)
      next
    }
    r <- tryCatch({
      frame$ns_ <- splines::ns(x, df = COX_FORM_DF)
      wider <- suppressWarnings(survival::coxph(cox_formula(c(form_rhs, "ns_"), start), data = frame, ties = ties))
      stat <- 2 * (wider$loglik[[2L]] - fit$loglik[[2L]])
      df_ <- sum(!is.na(stats::coef(wider))) - sum(!is.na(stats::coef(fit)))
      if (df_ < 1L) stop("the spline adds nothing to the model")
      list(statistic = stat, df = df_, p = stats::pchisq(stat, df_, lower.tail = FALSE))
    }, error = function(e) e)
    if (inherits(r, "error")) {
      cox_check(rows, "martingale_form", h0, "not_computed", paste0("It could not be computed on these rows: ", conditionMessage(r)),
                level_2 = input)
    } else {
      cox_check(rows, "martingale_form", h0, cox_verdict_p(r$p, alpha),
                paste0("The effect of «", input, "» is not linear on the log hazard: read its chart; a log, a square or a spline of it."),
                level_2 = input, statistic = r$statistic, df = r$df, p = r$p)
    }
  }

  # influence: dfbetas of each term's columns. The verdict by the classical threshold, |dfbetas|
  # above 1 (a row that moves a coefficient by more than its standard error): failed. The wide
  # one, 2/√n, which nearly every fit passes, only names the rows to look at (the largest first).
  n <- fit$n
  limit <- 2 / sqrt(n)
  dfb <- if (identical(fit$method, "exact")) {
    simpleError("survival gives no dfbeta for the exact partial likelihood; fit with ties = efron to see the influence")
  } else tryCatch(as.matrix(stats::residuals(fit, type = "dfbetas")), error = function(e) e)
  h0 <- "No row moves a coefficient by more than its standard error (|dfbetas| under 1)."
  for (j in seq_along(labels)) {
    if (inherits(dfb, "error")) {
      cox_check(rows, "dfbeta", h0, "not_computed", paste0("It was not computed: ", conditionMessage(dfb), "."),
                level_2 = labels[[j]])
      next
    }
    score <- apply(abs(dfb[, groups$columns[[j]], drop = FALSE]), 1L, max)
    top <- head(order(-score), 5L)
    cox_check(rows, "dfbeta", h0, if (any(score > COX_DFBETAS_FAILED)) "failed" else "ok",
              "Look at the rows named: each moves a coefficient by much; a mistake in the data, or a case the model does not describe.",
              level_2 = labels[[j]], threshold = COX_DFBETAS_FAILED, value = max(score), count = sum(score > COX_DFBETAS_FAILED),
              at_rows = design$rows[top[score[top] > limit]])
  }

  # multicollinearity
  h0 <- "Each term is not explained by the other terms (VIF 1)."
  for (i in seq_len(nrow(vif))) {
    value <- vif$value[[i]]
    verdict <- if (!is.finite(value)) "not_computed" else if (value > COX_VIF_FAILED) "failed" else
      if (value > COX_VIF_DOUBTFUL) "doubtful" else "ok"
    cox_check(rows, "vif", h0, verdict, "Drop or combine the correlated predictors.", level_2 = vif$term[[i]],
              statistic = if (vif$df[[i]] > 1L) vif$gvif[[i]] else NULL, df = if (vif$df[[i]] > 1L) vif$df[[i]] else NULL,
              threshold = COX_VIF_DOUBTFUL, value = value)
  }

  # enough events
  k <- ncol(design$X)
  cox_check(rows, "epv", paste0("At least ", COX_EPV, " events for each coefficient of the model."),
            if (events / k < COX_EPV) "doubtful" else "ok",
            "Few events for the number of coefficients: fewer terms, or merge rare levels.",
            threshold = COX_EPV, value = events / k, count = events)
  invisible(rows)
}

# The input column of a term that is one numeric predictor, else NULL.
cox_numeric_input <- function(design, label) {
  for (i in design$inputs) if (identical(i$name, label) && identical(i$type, "number")) return(label)
  NULL
}

cox_charts <- function(rows, task, fit, frame, design, groups, zph, lp, strata_names, start) {
  most <- tab_limit(task, "max_chart_points")
  keep <- tab_limit(task, "extreme_points")
  predictors <- unlist(cox_params(task$params)$predictors)
  charted <- head(predictors, COX_MAX_TERMS_CHARTED)

  # the scaled Schoenfeld residuals of each predictor against time, plot.cox.zph's smooth
  if (!inherits(zph, "error")) {
    xx <- zph$x
    tt <- zph$time
    first <- !duplicated(xx)
    for (j in seq_along(groups$label)) {
      label <- groups$label[[j]]
      if (!label %in% charted) next
      y <- zph$y[, j]
      ok <- is.finite(y)
      i <- which(ok)[tab_cap(sum(ok), max = most, score = y[ok] - stats::median(y[ok]), keep = keep)]
      tab_points(rows, "point", "schoenfeld", "y", x = tt[i], y = y[i], level_2 = label)
      smooth <- tryCatch(suppressWarnings(graphics::plot(zph, var = j, df = COX_SMOOTH_DF, nsmo = COX_SMOOTH_POINTS, plot = FALSE)),
                         error = function(e) NULL)
      if (!is.null(smooth) && sum(first) >= 2L) {
        at <- stats::approx(xx[first], tt[first], smooth$x, ties = "ordered")$y
        tab_points(rows, "curve", "schoenfeld", "smooth", x = at, y = smooth$y[, 1L], level_2 = label)
        tab_points(rows, "curve", "schoenfeld", "upper", x = at, y = smooth$y[, 2L], level_2 = label)
        tab_points(rows, "curve", "schoenfeld", "lower", x = at, y = smooth$y[, 3L], level_2 = label)
      }
    }
  }

  # martingale residuals of the null model against each numeric predictor
  numeric <- Filter(function(p) !is.null(cox_numeric_input(design, p)), charted)
  if (length(numeric)) {
    null_rhs <- if ("g" %in% names(frame) && nlevels(frame$g) > 1L) "survival::strata(g)" else "1"
    null <- tryCatch(survival::coxph(cox_formula(null_rhs, start), data = frame), error = function(e) NULL)
    if (!is.null(null)) {
      m <- stats::residuals(null, type = "martingale")
      for (p in numeric) {
        x <- as.numeric(design$data[[p]])
        i <- tab_cap(length(m), max = most, score = m - mean(m), keep = keep)
        tab_points(rows, "point", "martingale", "y", x = x[i], y = m[i], level_2 = p)
        l <- tab_lowess(x, m)
        if (nrow(l)) tab_points(rows, "curve", "martingale", "lowess", x = l$x, y = l$y, level_2 = p)
      }
    }
  }

  # deviance residuals against the centred linear predictor, the largest named
  dev <- stats::residuals(fit, type = "deviance")
  i <- tab_cap(length(dev), max = most, score = dev, keep = keep)
  labels <- tab_extreme_labels(dev, design$rows, keep = COX_LABELLED)[i]
  tab_points(rows, "point", "deviance_linear", "y", x = lp[i], y = dev[i], labels = labels)

  # survival at the fit's means, a curve a stratum
  sf <- suppressWarnings(survival::survfit(fit)) # at the column means, interactions included: said in the report
  counts <- if (is.null(sf$strata)) length(sf$time) else unname(sf$strata)
  at <- rep(seq_along(counts), counts)
  for (s in seq_along(counts)) {
    idx <- which(at == s)
    x <- c(0, sf$time[idx])
    y <- c(1, sf$surv[idx])
    steps <- tab_thin_steps(x, y, max = most)
    tab_points(rows, "curve", "adjusted_survival", "surv", x = steps$x, y = steps$y, level_2 = strata_names[[s]])
  }
  invisible(rows)
}

# ── the card ─────────────────────────────────────────────────────────────────
cox_card <- function(task, design, fit, formula, time_col, event_col, start_col, strata_col, value_label, ties, alpha, time,
                     strata_labels, strata_levels, notes = function(n) NULL) {
  card <- tab_card(task, design, COX_PACKAGES)
  card$formula <- formula
  card$outcome <- list(name = event_col, event_value = value_label)
  card$coefficients <- as.list(unname(stats::coef(fit)))
  card$vcov <- tab_card_vcov(task, unname(fit$var))
  card$link <- "log"
  card$alpha <- alpha
  if (!is.null(strata_col)) {
    card$inputs[[length(card$inputs) + 1L]] <- list(name = strata_col, type = "category", levels = as.list(strata_levels),
                                                    reference = strata_levels[[1L]])
  }
  most <- tab_limit(task, "max_baseline_times")
  sf <- suppressWarnings(survival::survfit(fit))
  counts <- if (is.null(sf$strata)) length(sf$time) else unname(sf$strata)
  at <- rep(seq_along(counts), counts)
  thinned <- FALSE
  baseline <- lapply(seq_along(counts), function(s) {
    idx <- which(at == s & sf$n.event > 0)
    if (length(idx) > most) {
      idx <- idx[unique(round(seq(1, length(idx), length.out = most)))]
      thinned <<- TRUE
    }
    list(stratum = if (is.null(strata_col)) NULL else strata_levels[[s]],
         last_time = max(time[strata_labels == (if (is.null(strata_col)) COX_ALL else strata_levels[[s]])]),
         time = as.list(sf$time[idx]), cumhaz = as.list(sf$cumhaz[idx]))
  })
  if (thinned) {
    notes(paste0("The card keeps the baseline hazard at ", most, " of its event times per stratum: survival predicted between ",
                 "them is read at the nearest earlier one."))
  }
  card$cox <- list(centring = as.list(unname(fit$means)), ties = ties, time_column = time_col, start_column = start_col %||% NULL,
                   strata = strata_col %||% NULL, baseline = baseline)
  card
}

.cox_num <- function(x) vapply(x, function(v) if (is.null(v)) NA_real_ else as.numeric(v), numeric(1))

# The risk of rows (exp of the linear predictor centred on the card's means) and their
# survival at the times asked, in each row's stratum.
cox_predict_rows <- function(card, df, at_times = numeric()) {
  plain <- card
  plain$params$intercept <- FALSE # a Cox model has no intercept: the baseline takes it
  d <- tab_design_rows(plain, df)
  b <- .cox_num(card$coefficients)
  centre <- sum(b * .cox_num(card$cox$centring))
  lp <- as.vector(d$X %*% b) - centre
  lp[!d$ok] <- NA_real_
  risk <- exp(lp)
  surv <- matrix(NA_real_, nrow(df), length(at_times))
  if (length(at_times)) {
    strata_col <- card$cox$strata
    if (!is.null(strata_col) && !strata_col %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows have no column «", strata_col, "», the strata the model was fitted on"),
                 list(column = strata_col))
    }
    labels <- if (is.null(strata_col)) rep("", nrow(df)) else tab_label(df[[strata_col]])
    for (s in card$cox$baseline) {
      rows_ <- which(if (is.null(strata_col)) rep(TRUE, nrow(df)) else !is.na(labels) & labels == s$stratum)
      if (!length(rows_)) next
      times <- .cox_num(s$time)
      cumhaz <- .cox_num(s$cumhaz)
      for (k in seq_along(at_times)) {
        t <- at_times[[k]]
        h <- if (t > as.numeric(s$last_time)) NA_real_ else {
          i <- findInterval(t, times)
          if (i == 0L) 0 else cumhaz[[i]]
        }
        surv[rows_, k] <- exp(-h * risk[rows_])
      }
    }
  }
  list(lp = lp, risk = risk, surv = surv, ok = d$ok, unseen = d$unseen)
}

# Predicting the rows the model was fitted on from its card gives its fitted risk: checked in
# the self-test (a reference case), where a difference is a failure.
cox_check_card <- function(task, card, design, fit, frame) {
  if (!identical(task$group_version, "selftest")) return(invisible(TRUE))
  back <- jsonlite::fromJSON(tab_card_text(card), simplifyVector = FALSE)
  pr <- cox_predict_rows(back, design$data)
  want <- unname(stats::predict(fit, type = "risk", reference = "sample"))
  gap <- max(abs(pr$risk - want) / pmax(1, abs(want)))
  if (!all(pr$ok) || !is.finite(gap) || gap > 1e-9) stop("the card does not give back the fitted risk (", gap, ")")
  invisible(TRUE)
}

# The rows output: the input's columns, then the model's (docs: «Rows output»). Every row whose
# predictors are present gets its linear predictor and risk; the rows the fit used also their
# martingale and deviance residuals.
cox_rows_output <- function(df, card, design, fit) {
  made <- c("pred_linear", "pred_risk", "pred_martingale", "pred_deviance")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  pr <- cox_predict_rows(card, df)
  out <- df
  used <- design$rows
  out$pred_linear <- pr$lp
  out$pred_risk <- pr$risk
  out$pred_martingale <- NA_real_
  out$pred_martingale[used] <- unname(stats::residuals(fit, type = "martingale"))
  out$pred_deviance <- NA_real_
  out$pred_deviance[used] <- unname(stats::residuals(fit, type = "deviance"))
  rownames(out) <- NULL
  out
}

# ── Predict: the risk, and the survival at the times asked ──────────────────
predict_cox <- function(task, card) {
  df <- tab_input(task)
  at_times <- sort(unique(as.numeric(unlist(task$params$at_times %||% list()))))
  names_ <- if (length(at_times)) paste0("survival_at_", vapply(at_times, km_time_label, "")) else character()
  made <- c("prediction", names_)
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  pr <- cox_predict_rows(card, df, at_times)
  if (pr$unseen > 0L) warning(pr$unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  out <- df
  out$prediction <- pr$risk
  for (k in seq_along(at_times)) out[[names_[[k]]]] <- pr$surv[, k]
  rownames(out) <- NULL
  list(output = out)
}

# ── the runner's own tests, run by the group's self-test with the common library's ──
TAB_COMMON_TESTS[["cox: column names that look like code, and the card's survival against survfit"]] <- function(code) {
  path <- file.path(code, "reference", "data", "cox_categorical.csv")
  if (!file.exists(path)) stop("no reference table ", path)
  df <- tab_read_csv(path, list(days = "number", status = "text", load = "number", line = "text", night = "boolean"))
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), output = "rows")
  task <- function(frame, params) list(model = "cox", variant = "default", group = "survival", group_version = "selftest",
                                       params = params, output = "rows", rows_from = "run", limits = tab_catalog()$limits,
                                       .frame = frame)
  plain <- fit_cox(task(df, params))
  odd <- df
  names(odd) <- c(TAB_A, TAB_X, "load", TAB_U, "night")[match(names(df), c("days", "status", "load", "line", "night"))]
  odd_params <- utils::modifyList(params, list(time = TAB_A, event = TAB_X))
  odd_params$predictors <- list("load", TAB_U)
  coded <- fit_cox(task(odd, odd_params))
  a <- tab_results_frame(plain$rows)
  b <- tab_results_frame(coded$rows)
  .tab_expect(identical(a$value, b$value), "the numbers differ when the columns are renamed")
  .tab_expect(identical(b$text[b$key == "model|||formula"], paste0("Surv(", TAB_A, ", ", TAB_X, " = failed) ~ load + ", TAB_U)),
              "formula: ", b$text[b$key == "model|||formula"])
  .tab_expect(paste0("coef|", TAB_U, "|B|estimate") %in% b$key && paste0("point|schoenfeld|", TAB_U, "|y|0") %in% b$key,
              "the terms' names")
  # the rows output: the fitted rows' risk, and an empty one where a predictor is empty
  out <- plain$output
  .tab_expect(nrow(out) == nrow(df) && is.na(out$pred_risk[is.na(df$load)][[1L]]) &&
              all(abs(out$pred_risk - exp(out$pred_linear)) < 1e-12, na.rm = TRUE), "the rows output")
  # the card's survival at two times against survfit called on the same rows by its formula
  card <- jsonlite::fromJSON(tab_card_text(plain$card), simplifyVector = FALSE)
  rows_ <- df[stats::complete.cases(df[, c("days", "status", "load", "line")]), ][c(1L, 7L, 20L), ]
  got <- predict_cox(list(params = list(at_times = list(30, 120)), .frame = rows_), card)$output
  ref <- survival::coxph(survival::Surv(days, status == "failed") ~ load + line,
                         data = df[stats::complete.cases(df[, c("days", "status", "load", "line")]), ])
  sf <- summary(survival::survfit(ref, newdata = rows_), times = c(30, 120))
  .tab_expect(max(abs(c(got$survival_at_30, got$survival_at_120) - c(t(sf$surv)))) < 1e-9, "survival differs from survfit's")
  .tab_expect(max(abs(got$prediction - stats::predict(ref, newdata = rows_, type = "risk", reference = "sample"))) < 1e-9,
              "the risk differs from predict's")
  late <- predict_cox(list(params = list(at_times = list(1e6)), .frame = rows_), card)$output
  .tab_expect(all(is.na(late$survival_at_1000000)), "a time past the follow-up is not estimated")
}

TAB_COMMON_TESTS[["cox: strata, and the survival of each row in its stratum from the card"]] <- function(code) {
  path <- file.path(code, "reference", "data", "cox_strata.csv")
  if (!file.exists(path)) stop("no reference table ", path)
  df <- tab_read_csv(path, list(months = "number", churned = "integer", price = "number", premium = "boolean",
                                region = "text", customer = "text"))
  params <- list(time = "months", event = "churned", event_value = 1, predictors = list("price", "premium"), strata = "region")
  fit <- fit_cox(list(model = "cox", variant = "default", group = "survival", group_version = "selftest", params = params,
                      rows_from = "run", limits = tab_catalog()$limits, .frame = df))
  card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
  rows_ <- df[c(2L, 50L, 101L), ]
  got <- predict_cox(list(params = list(at_times = list(6, 18)), .frame = rows_), card)$output
  ref <- survival::coxph(survival::Surv(months, churned) ~ price + premium + survival::strata(region), data = df)
  for (r in seq_len(nrow(rows_))) {
    sf <- summary(survival::survfit(ref, newdata = rows_[r, ]), times = c(6, 18), extend = TRUE)
    .tab_expect(max(abs(c(got$survival_at_6[[r]], got$survival_at_18[[r]]) - sf$surv)) < 1e-9,
                "survival in the stratum of row ", r, " differs from survfit's")
  }
  away <- predict_cox(list(params = list(at_times = list(6)), .frame = transform(rows_, region = "west")), card)$output
  .tab_expect(all(is.na(away$survival_at_6)) && all(is.finite(away$prediction)), "an unseen stratum: survival empty, risk given")
}

TAB_COMMON_TESTS[["cox: native.rds holds no row of the input, and gives the fit's linear predictor and risk"]] <- function(code) {
  df <- tab_read_csv(file.path(code, "reference", "data", "cox_strata.csv"),
                     list(months = "number", churned = "integer", price = "number", premium = "boolean", region = "text",
                          customer = "text"))
  params <- list(time = "months", event = "churned", event_value = 1, predictors = list("price", "premium"), strata = "region")
  fit <- fit_cox(list(model = "cox", variant = "default", group = "survival", group_version = "selftest", params = params,
                      rows_from = "run", limits = tab_catalog()$limits, .frame = df))
  full <- fit$native
  lean <- unserialize(serialize(tab_native_lean(full), NULL))
  left <- tab_native_rows_left(lean, full$n)
  .tab_expect(!length(left), "rows left in ", paste(left, collapse = ", "))
  .tab_expect(length(serialize(lean, NULL)) < length(serialize(full, NULL)) / 2, "not smaller than the fit")
  frame <- stats::model.frame(full)
  for (type in c("lp", "risk")) {
    .tab_expect(isTRUE(all.equal(unname(stats::predict(lean, newdata = frame, type = type, reference = "sample")),
                                 unname(stats::predict(full, newdata = frame, type = type, reference = "sample")), tolerance = 1e-12)),
                type, ": predict differs from the fit's")
  }
}

.cox_test_task <- function(frame, params, limits = NULL) {
  task <- list(model = "cox", variant = "default", group = "survival", group_version = "selftest", params = params, rows_from = "run",
               limits = tab_catalog()$limits, .frame = frame)
  if (!is.null(limits)) task$limits <- utils::modifyList(task$limits, limits)
  task
}

TAB_COMMON_TESTS[["cox: a start that survival cannot tell from its time is refused with a sentence, here and in the parametric node"]] <- function(code) {
  set.seed(4)
  n <- 80L
  x <- round(stats::rnorm(n), 2)
  t <- stats::rweibull(n, 1.5, exp(3 + 0.3 * x))
  cens <- stats::runif(n, 5, 60)
  df <- data.frame(days = round(pmin(t, cens), 2), status = as.integer(t <= cens), x = x)
  # a third of the rows enter 1e-9 before their time (86 µs in days: a start and an end taken from timestamps):
  # survival's aeqSurv merges the two and stopped with an R error, «an interval has effective length 0»
  df$s <- ifelse(seq_len(n) %% 3 == 0, df$days - 1e-9, 0)
  params <- list(time = "days", event = "status", event_value = 1, predictors = list("x"), start = "s")
  m <- .tab_refused_with("start_not_before_time", fit_cox(.cox_test_task(df, params)))
  .tab_expect(grepl("^in row 3 the start «s» \\(13\\.729999999\\) is not before the time «days» \\(13\\.73\\), by more than survival tells apart", m), m)
  m <- .tab_refused_with("start_not_before_time", fit_parametric_survival(.ps_test_task(df, c(params, list(distribution = "weibull")))))
  .tab_expect(grepl("^in row 3 the start", m), m)
  # 1e-6 before is an interval survival keeps: fitted
  df$s <- ifelse(seq_len(n) %% 3 == 0, df$days - 1e-6, 0)
  .tab_expect(!is.null(fit_cox(.cox_test_task(df, params))$card), "a start 1e-6 before its time was not fitted")
}

TAB_COMMON_TESTS[["cox: the exact partial likelihood that overflows is refused as such, never as a term «aliased»"]] <- function(code) {
  # times in whole days 1 to 12: up to a few hundred events at one time, which the exact likelihood cannot order
  set.seed(11)
  n <- 3000L
  x <- stats::rnorm(n)
  t <- ceiling(stats::rweibull(n, 1.2, exp(1.5 + 0.3 * x)))
  cens <- sample(4:12, n, TRUE)
  df <- data.frame(days = pmin(t, cens), status = as.integer(t <= cens), x = x)
  m <- .tab_refused_with("exact_failed", fit_cox(.cox_test_task(df, list(time = "days", event = "status", event_value = 1,
                                                                        predictors = list("x"), ties = "exact"))))
  largest <- max(table(df$days[df$status == 1L]))
  .tab_expect(grepl(paste0("up to ", largest, " events share one time"), m, fixed = TRUE) && grepl("ties = efron", m, fixed = TRUE), m)
}

