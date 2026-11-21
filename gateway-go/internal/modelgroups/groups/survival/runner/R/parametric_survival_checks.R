# Parametric survival: the checks and the charts' points (docs/specs/statistical-models.md, «The
# node «Parametric survival»» → «The checks», lot P2). `ps_checks(task, prepared, fitted, model,
# rows)` is the hook parametric_survival.R finds by name after it has written its own sections; it
# writes the section `assumption` and the points of the charts into `rows`. A check that does not
# apply to the distribution fitted is not written; one that cannot run on these rows is
# «not_computed» with its reason.
#
# Verdicts come from two places only: a test with a p-value against the node's `alpha` (p ≥ alpha
# «ok», p < alpha «failed», alpha in `threshold`; no «doubtful»), and a fact (`convergence`: a
# positive definite Hessian; `separation`: a level with no event). The tests: distribution_vs_gengamma
# (LR against the generalized gamma, for the nested), constant_hazard (exponential: LR against the
# Weibull), ph_cox and ph_cox_global (cox.zph, where the distribution assumes proportional hazards).
# Every other measure is numbers and a sentence (`description`), no verdict: km_agreement (the largest
# gap in survival `value`, where `at`, in standard errors `statistic`), dfbeta (largest |dfbetas|
# `value`, the rows named), epv (`value`, events `count`, parameters `df`), vif, extrapolation
# (`value` the largest number asked over the last time `at`). Cox–Snell is a chart only.
#
# Light: no refit per row, no bootstrap, no simulation; dfbetas is V · U_i with U_i from central
# differences of the vectorized row log-likelihood; curves on a grid of 100 times, the agreement read
# at 60 of Kaplan–Meier's at most; points capped by tab_cap. The fits besides the node's own are the
# Weibull and the generalized gamma of the two LR checks (lot P12): with auto the candidates already
# fitted, else one fit from the point of the node's own estimates nested in it (the Weibull at Q = 1,
# the log-normal at Q = 0, the exponential at shape 1), BFGS to PSC_RELTOL and Newton steps from
# the rows' own derivatives; within ps_extra_allowance's time, checked while it runs, else
# «not_computed» with the reason.
# Charts (level = id, level_2 = the group or stratum): curve fit_km (surv/lower/upper Kaplan–Meier,
# fitted: the mean fitted survival), point cox_snell, point/curve loglog (Weibull, exponential),
# curve profile_survival and hazard (with bands), point deviance_linear.
# Delayed entry (`start`, lot P8): Kaplan–Meier's curves and the Nelson–Aalen estimate of Cox–Snell
# are those of survfit(Surv(start, stop, status)): a row is at risk from its start; the dfbetas, the
# LR checks and the Schoenfeld test follow the conditional likelihood (the row log-likelihood, a
# Cox fit of start–stop rows). Every check still makes sense, but the estimates' first times rest on
# the few rows that have entered (one early event with one row at risk drops Kaplan–Meier's curve to
# 0 for good): they are read from the first time with at least `km_min_at_risk` rows at risk, as
# the curve conditional on lasting until then (psc_entry_floor). Without a start nothing changes.
# Names here start with `psc_` or `PS_CHECK`: the files of the group share one environment.

# What the measures read and draw: not thresholds of a verdict.
PS_CHECK_LIMITS <- list(
  km_min_at_risk = 10L,       # Kaplan–Meier's points with fewer rows at risk are not read (its tail is noise)
  km_max_times = 60L,         # event times the agreement is read at, at most (evenly spread)
  km_max_levels = 6L,         # a categorical predictor with more levels is not a grouping
  dfbeta_named = 5L,          # rows named for each term, the most influential
  curve_points = 100L,        # points of a fitted curve
  labelled = 3L)              # points named on the deviance chart
# The fits the two LR checks add (the generalized gamma, the exponential's Weibull) start from the
# node's own estimates, nested in them, and stop at this relative change of the log-likelihood:
# Newton steps (ps_newton_steps) then take them to the maximum, which is all an LR test reads.
# They run within ps_extra_allowance's time (lot P12: no row limit, the clock).
PSC_RELTOL <- 1e-8

# What a check assumes («H0»), in the words the report may reuse.
PSC_H0 <- list(
  convergence = "A maximum of the likelihood was reached (positive definite Hessian).",
  separation = "Every level has rows with the event.",
  distribution_vs_gengamma = "The generalized gamma describes the times no better.",
  constant_hazard = "The hazard does not change over time (the Weibull's shape is 1).",
  ph_cox = "The effect of the term does not change over time.",
  ph_cox_global = "No effect of the model changes over time.")

# The hook. `rows`: the results being written; nothing is returned.
ps_checks <- function(task, prepared, fitted, model, rows) {
  ctx <- psc_context(task, prepared, fitted, model)
  selftest <- identical(task$group_version, "selftest")
  # a check that fails in the runner's own code is a bug in the self-test; for people it is «not_computed»
  guard <- function(id, f) {
    tryCatch(f(), error = function(e) {
      if (selftest) stop(e)
      if (!is.null(PSC_H0[[id]])) {
        psc_check(rows, id, PSC_H0[[id]], "not_computed", paste0("It could not be computed on these rows: ", conditionMessage(e)))
      }
    })
  }
  charts <- function(f) tryCatch(f(), error = function(e) if (selftest) stop(e))
  tab_number(rows, "model", "alpha", ctx$alpha)
  guard("convergence", function() psc_convergence(ctx, rows))
  guard("epv", function() psc_epv(ctx, rows))
  guard("separation", function() psc_separation(ctx, rows))
  guard("km_agreement", function() psc_km_agreement(ctx, rows))
  if (ctx$distribution %in% c("weibull", "exponential", "lognormal")) {
    guard("distribution_vs_gengamma", function() psc_vs_gengamma(ctx, rows))
  }
  if (identical(ctx$distribution, "exponential")) guard("constant_hazard", function() psc_constant_hazard(ctx, rows))
  if (ctx$has_predictors && ctx$distribution %in% c("weibull", "exponential", "gompertz")) {
    guard("ph_cox", function() psc_ph_cox(ctx, rows))
  }
  if (ctx$has_predictors) guard("dfbeta", function() psc_dfbeta(ctx, rows))
  if (ctx$has_predictors) guard("vif", function() psc_vif(ctx, rows))
  guard("extrapolation", function() psc_extrapolation(ctx, rows))

  charts(function() psc_chart_fit(ctx, rows))
  charts(function() psc_chart_cox_snell(ctx, rows))
  charts(function() psc_chart_profile(ctx, rows))
  if (ctx$has_predictors) charts(function() psc_chart_deviance(ctx, rows))
  invisible(rows)
}

psc_context <- function(task, prepared, fitted, model) {
  ctx <- new.env(parent = emptyenv())
  ctx$task <- task
  ctx$prepared <- prepared
  ctx$fitted <- fitted
  ctx$model <- model
  ctx$m <- fitted$spec
  ctx$distribution <- fitted$distribution
  ctx$alpha <- prepared$alpha
  ctx$lim <- PS_CHECK_LIMITS
  ctx$n <- length(prepared$time)
  ctx$events <- sum(prepared$status)
  ctx$has_predictors <- ncol(prepared$X) > 0L
  ctx$most <- tab_limit(task, "max_chart_points")
  ctx$keep <- tab_limit(task, "extreme_points")
  ctx$auto <- identical(model$selection, "auto")
  ctx
}

# One check's rows, in the order the report reads them. `verdict` NULL: a measure, which has
# `description` instead and no verdict, no advice. `advice_id`: the report's sentence.
psc_check <- function(rows, id, h0 = NULL, verdict = NULL, advice = NULL, advice_id = id, level_2 = NULL, statistic = NULL, df = NULL,
                      p = NULL, threshold = NULL, value = NULL, count = NULL, at = NULL, at_rows = NULL, description = NULL) {
  section <- "assumption"
  if (!is.null(h0)) tab_text(rows, section, "h0", h0, id, level_2)
  if (!is.null(statistic)) tab_number(rows, section, "statistic", statistic, id, level_2)
  if (!is.null(df)) tab_number(rows, section, "df", df, id, level_2)
  if (!is.null(p)) tab_p(rows, section, p, level = id, level_2 = level_2)
  if (!is.null(threshold)) tab_number(rows, section, "threshold", threshold, id, level_2)
  if (!is.null(value)) tab_number(rows, section, "value", value, id, level_2)
  if (!is.null(count)) tab_number(rows, section, "count", count, id, level_2)
  if (!is.null(at)) tab_number(rows, section, "at", at, id, level_2)
  if (!is.null(verdict)) tab_text(rows, section, "verdict", verdict, id, level_2)
  if (!is.null(advice)) {
    tab_text(rows, section, "advice", advice, id, level_2)
    tab_text(rows, section, "advice_id", advice_id, id, level_2)
  }
  if (!is.null(description)) tab_text(rows, section, "description", description, id, level_2)
  if (length(at_rows)) tab_text(rows, section, "rows", paste(at_rows, collapse = ", "), id, level_2)
  invisible(rows)
}

# p against the node's alpha: the only rule that turns a p-value into a verdict.
psc_verdict_p <- function(p, alpha) if (is.na(p)) "not_computed" else if (p < alpha) "failed" else "ok"

# The step's time: the checks that fit more models are not made when more than half of the
# budget has gone (the process's own clock).
psc_time_up <- function(task) {
  budget <- task$limits$time_budget_seconds
  !is.null(budget) && is.finite(as.numeric(budget)) && proc.time()[["elapsed"]] > 0.5 * as.numeric(budget)
}

psc_num <- function(x) tab_number_label(signif(x, 3))

# ── convergence (a fact) ─────────────────────────────────────────────────────
# The optimiser's code is the fit's (a refusal otherwise); a maximum is reached when the Hessian is
# positive definite. The Newton step left at the estimates, in standard errors, is the `value`.
psc_convergence <- function(ctx, rows) {
  V <- ctx$fitted$vcov
  advice <- "The numbers are not a maximum of the likelihood: fewer predictors, merge rare levels, or another distribution."
  if (is.null(V)) {
    psc_check(rows, "convergence", PSC_H0$convergence, "failed", advice)
    return(invisible())
  }
  step <- max(abs(drop(V %*% numDeriv::grad(ctx$fitted$loglik_fn, ctx$fitted$par))) / sqrt(diag(V)))
  psc_check(rows, "convergence", PSC_H0$convergence, "ok", advice, value = step)
}

# ── events per parameter (a measure) ─────────────────────────────────────────
psc_epv <- function(ctx, rows) {
  k <- length(ctx$fitted$par)
  psc_check(rows, "epv", value = ctx$events / k, count = ctx$events, df = k,
            description = paste0("There are ", psc_num(ctx$events / k), " events for each of the ", k, " parameters (", ctx$events, " events)."))
}

# ── separation: a level of a categorical predictor with no event among its rows (a fact) ──
psc_separation <- function(ctx, rows) {
  p <- ctx$prepared
  seen <- FALSE
  failed <- 0L
  for (input in p$design$inputs) {
    if (!identical(input$type, "category")) next
    seen <- TRUE
    labels <- tab_label(p$input[[input$name]][p$rows])
    for (level in vapply(input$levels, as.character, "")) {
      at <- labels == level
      if (!any(at) || any(p$status[at])) next
      failed <- failed + 1L
      psc_check(rows, "separation", PSC_H0$separation, "failed",
                paste0("No row of «", input$name, " = ", level, "» has the event: its coefficient runs to infinity and means ",
                       "nothing; merge the level or leave its rows out."),
                level_2 = paste0(input$name, "=", level), count = sum(at))
    }
  }
  if (seen && failed == 0L) {
    psc_check(rows, "separation", PSC_H0$separation, "ok", "Every level of every categorical predictor has rows with the event.")
  }
  invisible()
}

# ── the groups of the agreement and its charts ───────────────────────────────
# The strata; without strata the levels of the first categorical predictor when it has at most
# `km_max_levels`; else one group, «all». Each: its name, its rows (indices into the rows used).
psc_groups <- function(ctx) {
  if (!is.null(ctx$groups)) return(ctx$groups)
  p <- ctx$prepared
  names_ <- PS_ALL
  index <- rep(1L, ctx$n)
  if (!is.null(p$strata_col)) {
    names_ <- p$strata_names
    index <- p$stratum
  } else {
    for (input in p$design$inputs) {
      if (!identical(input$type, "category") || length(input$levels) > ctx$lim$km_max_levels) next
      levels <- vapply(input$levels, as.character, "")
      index <- match(tab_label(p$input[[input$name]][p$rows]), levels)
      names_ <- paste0(input$name, "=", levels)
      break
    }
  }
  groups <- list()
  for (g in seq_along(names_)) {
    idx <- which(index == g)
    if (length(idx)) groups[[length(groups) + 1L]] <- list(name = names_[[g]], idx = idx)
  }
  ctx$groups <- groups
  groups
}

# Kaplan–Meier's curve of a group (weights as counts), with its log-log band at 1 − alpha.
# With a start: the rows at risk are those that have entered (survfit's counting-process form),
# from `t0` on (psc_entry_floor), each row entering at the later of its start and t0; `km$t0`.
psc_km <- function(ctx, group) {
  p <- ctx$prepared
  frame <- data.frame(time = p$time[group$idx], status = as.integer(p$status[group$idx]), w = p$w[group$idx])
  t0 <- 0
  if (!is.null(p$start)) {
    frame$start <- p$start[group$idx]
    t0 <- psc_entry_floor(frame$start, frame$time, frame$status, frame$w, ctx$lim$km_min_at_risk)
    frame <- frame[frame$time > t0, , drop = FALSE]
    frame$start <- pmax(frame$start, t0)
  }
  form <- stats::as.formula(if (is.null(p$start)) "survival::Surv(time, status) ~ 1" else "survival::Surv(start, time, status) ~ 1",
                            env = new.env(parent = asNamespace("survival")))
  km <- survival::survfit(form, data = frame, weights = w, conf.type = "log-log", conf.int = min(1 - ctx$alpha, 1 - 1e-12)) # alpha below 1e-12 is not a level survfit takes
  km$t0 <- t0
  km
}

# The time from which an estimate with delayed entry is read: the last time before the first one
# with at least `min_risk` rows (weights) at risk; 0 without a start, or when the first time has
# them. NULL start: 0. A table with no such time gives 0 (the readers then find no time to read).
psc_entry_floor <- function(start, time, status, w, min_risk) {
  if (is.null(start)) return(0)
  km <- survival::survfit(survival::Surv(start, time, status) ~ 1, weights = w)
  i <- which(km$n.risk >= min_risk)
  if (!length(i) || i[[1L]] == 1L) return(0)
  km$time[[i[[1L]] - 1L]]
}

# The fitted survival of the rows `idx` at each time, averaged with the rows' weights: the curve
# Kaplan–Meier's estimates for the group when the model is right.
# With `t0` (delayed entry): the survival conditional on lasting until t0, the rows weighed by
# their own chance of that, Σ w S(t) / Σ w S(t0): what survfit from t0 on estimates.
psc_mean_surv <- function(ctx, idx, times, t0 = 0) {
  p <- ctx$prepared
  pars <- ps_natural(ctx$m, ctx$fitted$par, p$X[idx, , drop = FALSE], p$S[idx, , drop = FALSE])
  w <- p$w[idx]
  base <- if (t0 > 0) sum(w * exp(ps_log_survival(ctx$m, rep(t0, length(idx)), pars))) else sum(w)
  vapply(times, function(t) sum(w * exp(ps_log_survival(ctx$m, rep(t, length(idx)), pars))) / base, numeric(1))
}

# ── agreement with Kaplan–Meier (a measure) ──────────────────────────────────
psc_km_agreement <- function(ctx, rows) {
  for (group in psc_groups(ctx)) {
    level_2 <- group$name
    a <- psc_agreement(ctx, group)
    label <- if (identical(level_2, PS_ALL)) "the rows" else paste0("«", level_2, "»")
    if (is.null(a)) {
      psc_check(rows, "km_agreement", level_2 = level_2, count = length(group$idx),
                description = paste0("No event time of ", label, " has ", ctx$lim$km_min_at_risk,
                                     " rows at risk: Kaplan–Meier's curve is too uncertain to compare with the fitted one."))
      next
    }
    psc_check(rows, "km_agreement", level_2 = level_2, value = a$gap, at = a$time, statistic = a$d, count = length(group$idx),
              description = paste0("The mean fitted curve of ", label, " departs from Kaplan–Meier's by at most ", psc_num(a$gap),
                                   " in survival, at t = ", psc_num(a$time), " (", psc_num(a$d), " standard errors of Kaplan–Meier's curve)",
                                   if (a$t0 > 0) paste0(", the rows at risk counted from their start, from t = ", psc_num(a$t0),
                                                        " on: the first time with ", ctx$lim$km_min_at_risk, " rows at risk") else "", "."))
  }
  invisible()
}

# The largest |mean fitted S − Kaplan–Meier S| over the event times with at least km_min_at_risk
# rows at risk (at most km_max_times of them, evenly spread), where it is reached, and the same gap
# in Greenwood's standard errors there. NULL when no time qualifies.
psc_agreement <- function(ctx, group) {
  lim <- ctx$lim
  km <- psc_km(ctx, group)
  se <- km$surv * km$std.err # survfit's std.err is that of the cumulative hazard: Greenwood's, on the log scale
  at <- which(km$n.event > 0 & km$n.risk >= lim$km_min_at_risk & is.finite(se) & se > 0)
  if (!length(at)) return(NULL)
  if (length(at) > lim$km_max_times) at <- at[unique(round(seq(1, length(at), length.out = lim$km_max_times)))]
  gap <- abs(psc_mean_surv(ctx, group$idx, km$time[at], km$t0) - km$surv[at])
  j <- which.max(gap)
  list(gap = gap[[j]], time = km$time[at][[j]], d = gap[[j]] / se[at][[j]], t0 = km$t0)
}

# ── the LR checks: a larger distribution against the one fitted ──────────────
# The point of the larger distribution that is the node's own fit: ps_nested_point
# (parametric_survival.R).
psc_nested_start <- function(ctx, target) ps_nested_point(ctx$fitted, target, ctx$prepared)

# The larger distribution's fit at its maximum, once per check run: list(fit, status, seconds),
# status «ok», «not_converged», «time» (the allowance ran out while it was fitted) or, with auto,
# «candidate_<status>» (the candidate was not fitted: its own status in the table of the choice).
psc_larger_fit <- function(ctx, target) {
  key <- paste0("larger_", target)
  if (!is.null(ctx[[key]])) return(ctx[[key]])
  p <- ctx$prepared
  allowance <- ps_extra_allowance(ctx$task, ctx$fitted)
  if (ctx$auto) {
    candidate <- ctx$fitted$candidates[[target]]
    if (!isTRUE(candidate$converged)) {
      out <- list(fit = NULL, status = "candidate_missing", seconds = allowance)
    } else {
      run <- ps_within(allowance, ps_newton_steps(candidate, p))
      out <- list(fit = run$value, status = if (run$time_up) "time" else "ok", seconds = allowance)
    }
  } else {
    init <- psc_nested_start(ctx, target)
    # Newton steps straight from the nested point; BFGS first only when they do not reach the maximum
    run <- ps_within(allowance, ps_fit_nested(p, target, init, function() {
      f <- ps_fit(p, target, init = init, reltol = PSC_RELTOL, second = FALSE)
      if (isTRUE(f$converged)) ps_newton_steps(f, p) else f
    }))
    status <- if (run$time_up) "time" else if (isTRUE(run$value$converged)) "ok" else "not_converged"
    out <- list(fit = run$value, status = status, seconds = allowance)
  }
  ctx[[key]] <- out
  out
}

# One LR check of the node's distribution against `target`, nested in it: verdict from p against
# alpha; «not_computed» with an `advice_id` the report words when the larger fit is not there.
psc_lr_check <- function(ctx, rows, id, target, advice, advice_auto) {
  f <- ctx$fitted
  label <- PS_DISTRIBUTIONS[[target]]$label
  not_computed <- function(why, advice_id) psc_check(rows, id, PSC_H0[[id]], "not_computed", why, advice_id = advice_id)
  if (psc_time_up(ctx$task)) {
    return(not_computed("The time of the step was used before this comparison could be made.", paste0(id, "_no_time")))
  }
  larger <- psc_larger_fit(ctx, target)
  if (identical(larger$status, "time")) {
    return(not_computed(paste0("The ", label, " took longer to fit than the model itself (", psc_num(larger$seconds), " s): the ",
                               "comparison was stopped; fit ", target, " to see it."), paste0(id, "_time")))
  }
  if (identical(larger$status, "candidate_missing")) {
    return(not_computed(paste0("The ", label, " was not fitted among the candidates (see the choice of the distribution): ",
                               "the comparison is not made."), paste0(id, "_candidate")))
  }
  if (!identical(larger$status, "ok")) {
    return(not_computed(paste0("The ", label, " did not converge on these rows: the comparison is not made."), paste0(id, "_not_converged")))
  }
  stat <- 2 * (larger$fit$loglik - f$loglik)
  if (stat < -1e-6 * max(1, abs(f$loglik))) {
    return(not_computed(paste0("The ", label, " fit stopped below the log-likelihood of the model it contains: the comparison is not made."),
                        paste0(id, "_below")))
  }
  stat <- max(stat, 0)
  df_ <- larger$fit$n_parameters - f$n_parameters
  p_ <- stats::pchisq(stat, df_, lower.tail = FALSE)
  psc_check(rows, id, PSC_H0[[id]], psc_verdict_p(p_, ctx$alpha), if (ctx$auto) advice_auto(larger$fit) else advice(larger$fit),
            advice_id = if (ctx$auto) paste0(id, "_auto") else id, statistic = stat, df = df_, p = p_, threshold = ctx$alpha)
}

psc_vs_gengamma <- function(ctx, rows) {
  q <- function(fit) psc_num(fit$par[[match("Q", fit$spec$pars)]])
  psc_lr_check(ctx, rows, "distribution_vs_gengamma", "gengamma",
               function(fit) paste0("The generalized gamma describes these times better (its Q is ", q(fit),
                                    ", where the Weibull has 1 and the log-normal 0): try gengamma, or auto."),
               function(fit) paste0("The generalized gamma describes these times better (its Q is ", q(fit),
                                    ", where the Weibull has 1 and the log-normal 0), though the choice kept the simpler one: ",
                                    "fit gengamma to use it."))
}

# ── the exponential's constant hazard, against the Weibull ───────────────────
psc_constant_hazard <- function(ctx, rows) {
  psc_lr_check(ctx, rows, "constant_hazard", "weibull",
               function(fit) "The hazard changes over time: use weibull, or auto.",
               function(fit) "The hazard changes over time, though the choice kept the exponential: fit weibull to use it.")
}

# ── proportional hazards: Grambsch–Therneau on the Cox fit of the same predictors ──
psc_ph_cox <- function(ctx, rows) {
  p <- ctx$prepared
  groups <- cox_term_groups(p$design)
  labels <- groups$label
  zph <- tryCatch(psc_zph(ctx, groups), error = function(e) e)
  if (inherits(zph, "error")) {
    reason <- paste0("It could not be computed on these rows: ", conditionMessage(zph))
    for (label in labels) psc_check(rows, "ph_cox", PSC_H0$ph_cox, "not_computed", reason, level_2 = label)
    psc_check(rows, "ph_cox_global", PSC_H0$ph_cox_global, "not_computed", reason)
    return(invisible())
  }
  table_ <- zph$table
  for (j in seq_along(labels)) {
    psc_check(rows, "ph_cox", PSC_H0$ph_cox, psc_verdict_p(table_[j, "p"], ctx$alpha),
              paste0("The effect of «", labels[[j]], "» changes over time: a proportional-hazards distribution does not describe it; ",
                     "an accelerated failure time distribution (lognormal, loglogistic, gengamma), or stratify by it."),
              level_2 = labels[[j]], statistic = table_[j, "chisq"], df = table_[j, "df"], p = table_[j, "p"], threshold = ctx$alpha)
  }
  g <- table_[if ("GLOBAL" %in% rownames(table_)) "GLOBAL" else nrow(table_), ]
  psc_check(rows, "ph_cox_global", PSC_H0$ph_cox_global, psc_verdict_p(g[["p"]], ctx$alpha),
            "Some effect changes over time: read the tests of the terms, and use an accelerated failure time distribution or strata.",
            statistic = g[["chisq"]], df = g[["df"]], p = g[["p"]], threshold = ctx$alpha)
}

# cox.zph of coxph(Surv(time, status) ~ one matrix column a term + strata(g)), Efron's ties, the
# weights: the Cox node's own construction.
psc_zph <- function(ctx, groups) {
  p <- ctx$prepared
  frame <- data.frame(time = p$time, status = as.integer(p$status), w = p$w)
  for (j in seq_along(groups$columns)) frame[[paste0("t", j)]] <- p$X[, groups$columns[[j]], drop = FALSE]
  frame$g <- factor(p$stratum)
  if (!is.null(p$start)) frame$start <- p$start # start–stop rows: cox.zph reads the counting process
  rhs <- c(paste0("t", seq_along(groups$columns)), if (nlevels(frame$g) > 1L) "survival::strata(g)")
  fit <- suppressWarnings(survival::coxph(cox_formula(rhs, !is.null(p$start)), data = frame, weights = w, ties = "efron", model = TRUE, x = TRUE))
  if (anyNA(stats::coef(fit))) stop("the Cox fit has a coefficient that is not estimable")
  survival::cox.zph(fit, transform = "km", terms = TRUE)
}

# ── influence: dfbetas (a measure) ───────────────────────────────────────────
# V · U_i for each row i, U_i the gradient of the row's log-likelihood at the estimates, over each
# coefficient's standard error: survreg's residuals(type = "dfbetas"), unweighted (a weighted row
# is one row), on the optimiser's scale. No refit and no Hessian per row: U comes from central
# differences of the vectorized row log-likelihood, two evaluations of all rows at once per
# parameter, and dfbetas is one matrix product, O(n k²).
psc_row_scores <- function(f) {
  par <- f$par
  U <- matrix(0, length(f$loglik_rows(par)), length(par))
  for (j in seq_along(par)) {
    h <- 1e-5 * max(1, abs(par[[j]]))
    up <- down <- par
    up[[j]] <- par[[j]] + h
    down[[j]] <- par[[j]] - h
    U[, j] <- (f$loglik_rows(up) - f$loglik_rows(down)) / (2 * h)
  }
  U
}

psc_dfbetas <- function(ctx) {
  f <- ctx$fitted
  V <- f$vcov
  if (is.null(V)) stop("the covariance of the estimates is not available")
  sweep(psc_row_scores(f) %*% V, 2L, sqrt(diag(V)), "/")
}

psc_dfbeta <- function(ctx, rows) {
  p <- ctx$prepared
  groups <- cox_term_groups(p$design)
  labels <- groups$label
  dfb <- psc_dfbetas(ctx)
  at <- ctx$m$i_terms
  for (j in seq_along(labels)) {
    score <- apply(abs(dfb[, at[groups$columns[[j]]], drop = FALSE]), 1L, max)
    top <- utils::head(order(-score), ctx$lim$dfbeta_named)
    named <- p$rows[top]
    psc_check(rows, "dfbeta", level_2 = labels[[j]], value = max(score), at_rows = named,
              description = paste0("The row that moves the coefficient of «", labels[[j]], "» most (row ", named[[1L]], ") moves it by ",
                                   psc_num(max(score)), " of its standard error; the most influential rows: ", paste(named, collapse = ", "), "."))
  }
  invisible()
}

# ── multicollinearity, as Cox's (a measure) ──────────────────────────────────
psc_vif <- function(ctx, rows) {
  p <- ctx$prepared
  vif <- cox_vif(p$X, p$design$terms)
  if (nrow(vif) < 2L) return(invisible())
  for (i in seq_len(nrow(vif))) {
    value <- vif$value[[i]]
    if (!is.finite(value)) next
    psc_check(rows, "vif", level_2 = vif$term[[i]], value = value,
              statistic = if (vif$df[[i]] > 1L) vif$gvif[[i]] else NULL, df = if (vif$df[[i]] > 1L) vif$df[[i]] else NULL,
              description = paste0("The variance inflation of «", vif$term[[i]], "» is ", psc_num(value),
                                   ": 1 when no other term explains it, larger the more they do."))
  }
  invisible()
}

# ── extrapolation (a measure) ────────────────────────────────────────────────
psc_extrapolation <- function(ctx, rows) {
  p <- ctx$prepared
  profile <- ps_profile(ctx$model, p)
  many <- length(p$strata_levels) > 1L
  for (s in seq_along(p$strata_levels)) {
    last <- max(p$time[p$stratum == s])
    asked <- c(p$times, profile$median[[s]], profile$quantile[s, ])
    asked <- asked[is.finite(asked)]
    if (!length(asked)) next
    ratio <- max(asked) / last
    psc_check(rows, "extrapolation", level_2 = if (many) p$strata_names[[s]] else NULL, value = ratio, at = last,
              description = if (ratio > 1) {
                paste0("The numbers asked reach ", psc_num(ratio), " times the last time observed (", psc_num(last),
                       "): beyond it they come from the distribution's shape, not from rows.")
              } else {
                paste0("The numbers asked lie within the last time observed (", psc_num(last), "): the largest is ", psc_num(ratio), " of it.")
              })
  }
  invisible()
}

# ── the charts' points ───────────────────────────────────────────────────────
# Kaplan–Meier against the fitted curve, per group; and for the Weibull and the exponential the
# same on the log(−log S) against log t scale.
psc_chart_fit <- function(ctx, rows) {
  p <- ctx$prepared
  loglog <- ctx$distribution %in% c("weibull", "exponential")
  for (group in psc_groups(ctx)) {
    level_2 <- group$name
    km <- psc_km(ctx, group)
    # the steps from time 0 (S = 1), the band where it exists (log-log: from the first event on)
    x <- c(km$t0, km$time) # from 0 (S = 1); with delayed entry from t0, the first time with enough rows at risk
    steps <- tab_thin_steps(x, c(1, km$surv), max = ctx$most)
    tab_points(rows, "curve", "fit_km", "surv", x = steps$x, y = steps$y, level_2 = level_2)
    for (side in c("lower", "upper")) {
      y <- c(1, km[[side]])
      ok <- is.finite(y)
      band <- tab_thin_steps(x[ok], y[ok], max = ctx$most)
      tab_points(rows, "curve", "fit_km", side, x = band$x, y = band$y, level_2 = level_2)
    }
    grid <- seq(km$t0, max(p$time[group$idx]), length.out = min(ctx$lim$curve_points, ctx$most))
    fitted_s <- psc_mean_surv(ctx, group$idx, grid, km$t0)
    tab_points(rows, "curve", "fit_km", "fitted", x = grid, y = fitted_s, level_2 = level_2)
    if (loglog) {
      ev <- which(km$n.event > 0 & km$surv > 0 & km$surv < 1)
      if (length(ev)) {
        i <- ev[tab_cap(length(ev), max = ctx$most, keep = ctx$keep)]
        tab_points(rows, "point", "loglog", "y", x = log(km$time[i]), y = log(-log(km$surv[i])), level_2 = level_2)
      }
      g <- which(grid > 0 & is.finite(fitted_s) & fitted_s > 0 & fitted_s < 1)
      if (length(g)) tab_points(rows, "curve", "loglog", "y", x = log(grid[g]), y = log(-log(fitted_s[g])), level_2 = level_2)
    }
  }
  invisible()
}

# The residuals of the fit: Cox–Snell r = H(t | x), martingale δ − r, deviance (as the rows output).
psc_residuals <- function(ctx) {
  p <- ctx$prepared
  pars <- ps_natural(ctx$m, ctx$fitted$par, p$X, p$S)
  r <- -ps_log_survival(ctx$m, p$time, pars)
  status <- as.integer(p$status)
  # the entry's cumulative hazard H(start), 0 without delayed entry; the hazard accrued while observed is r − r0
  r0 <- if (is.null(p$start)) rep(0, length(r)) else ifelse(p$start == 0, 0, -ps_log_survival(ctx$m, p$start, pars))
  mart <- status - (r - r0)
  dev <- sign(mart) * sqrt(pmax(-2 * (mart + ifelse(status == 1L, log(status - mart), 0)), 0))
  list(cox_snell = r, entry = r0, martingale = mart, deviance = dev)
}

# Cox–Snell: if the model is right the residuals are a censored sample of the unit exponential, so
# the Nelson–Aalen cumulative hazard of (r, δ) lies on the diagonal.
psc_chart_cox_snell <- function(ctx, rows) {
  p <- ctx$prepared
  res <- psc_residuals(ctx)
  r <- res$cox_snell
  ok <- is.finite(r) & r > 0
  frame <- data.frame(r = r[ok], status = as.integer(p$status[ok]), w = p$w[ok])
  base <- 0 # the Nelson–Aalen estimate starts at 0 at r = 0
  if (!is.null(p$start)) {
    # delayed entry: the pairs (H(start), H(time)) are the entry and the exit of a unit exponential;
    # from c0, the first value with enough rows at risk, the estimate conditional on lasting until
    # there, set back on the diagonal (c0 + the hazard accrued since)
    frame$r0 <- res$entry[ok]
    frame <- frame[frame$r > frame$r0, , drop = FALSE] # an interval that rounds to nothing is not one
    c0 <- psc_entry_floor(frame$r0, frame$r, frame$status, frame$w, ctx$lim$km_min_at_risk)
    frame <- frame[frame$r > c0, , drop = FALSE]
    frame$r0 <- pmax(frame$r0, c0)
    base <- c0
  }
  form <- stats::as.formula(if (is.null(p$start)) "survival::Surv(r, status) ~ 1" else "survival::Surv(r0, r, status) ~ 1",
                            env = new.env(parent = asNamespace("survival")))
  na <- survival::survfit(form, data = frame, weights = w, ctype = 1)
  x <- sort(frame$r)
  y <- base + stats::approx(c(0, na$time), c(0, na$cumhaz), xout = x, method = "constant", f = 0, rule = 2)$y
  i <- tab_cap(length(x), max = ctx$most, score = y - x, keep = ctx$keep)
  tab_points(rows, "point", "cox_snell", "y", x = x[i], y = y[i])
  invisible()
}

# The profile's survival and hazard with their bands, per stratum, from 0 to the stratum's last time.
psc_chart_profile <- function(ctx, rows) {
  p <- ctx$prepared
  model <- ctx$model
  L <- length(p$strata_levels)
  X <- if (length(p$profile_row)) {
    one <- as.data.frame(p$profile_row, check.names = FALSE, stringsAsFactors = FALSE)
    tab_design_rows(model$card, one)$X[rep(1L, L), , drop = FALSE]
  } else matrix(numeric(), L, 0L)
  for (s in seq_len(L)) {
    last <- max(p$time[p$stratum == s])
    grid <- seq(0, last, length.out = min(ctx$lim$curve_points, ctx$most - 1L) + 1L)[-1L] # no time 0: S = 1 there, h(0) may not be finite
    pr <- ps_predict(model, X[s, , drop = FALSE], s, numeric(), grid, interval = TRUE, hazard_times = grid)
    name <- p$strata_names[[s]]
    series <- function(level, stat, y, x = grid) {
      ok <- is.finite(y)
      tab_points(rows, "curve", level, stat, x = x[ok], y = y[ok], level_2 = name)
    }
    series("profile_survival", "surv", c(1, pr$surv[1L, ]), c(0, grid))
    if (pr$intervals) {
      series("profile_survival", "lower", c(1, pr$surv_low[1L, ]), c(0, grid))
      series("profile_survival", "upper", c(1, pr$surv_high[1L, ]), c(0, grid))
    }
    series("hazard", "hazard", pr$hazard[1L, ])
    if (pr$intervals) {
      series("hazard", "lower", pr$hazard_low[1L, ])
      series("hazard", "upper", pr$hazard_high[1L, ])
    }
  }
  invisible()
}

# Deviance residuals against the location's linear predictor, the largest named by their row.
psc_chart_deviance <- function(ctx, rows) {
  p <- ctx$prepared
  dev <- psc_residuals(ctx)$deviance
  lp <- ps_linear(ctx$m, ctx$fitted$par, p$X, p$S)
  ok <- is.finite(dev) & is.finite(lp)
  dev <- dev[ok]
  lp <- lp[ok]
  at <- p$rows[ok]
  i <- tab_cap(length(dev), max = ctx$most, score = dev, keep = ctx$keep)
  labels <- tab_extreme_labels(dev, at, keep = ctx$lim$labelled)[i]
  tab_points(rows, "point", "deviance_linear", "y", x = lp[i], y = dev[i], labels = labels)
  invisible()
}

# ── the self-test ────────────────────────────────────────────────────────────
# A table of n rows: a number x, a group g (a, b), a Weibull time of the given shape (a scale
# that falls with x and with b), censored uniformly; `x2` is x plus a little noise (collinear).
.psc_table <- function(n, shape, seed, censor_to = 20) {
  set.seed(seed)
  x <- round(stats::rnorm(n), 2)
  g <- sample(c("a", "b"), n, TRUE)
  t <- stats::rweibull(n, shape, exp(2 + 0.3 * x + 0.25 * (g == "b")))
  cens <- stats::runif(n, 2, censor_to)
  data.frame(t = pmax(round(pmin(t, cens), 2), 0.01), e = as.integer(t <= cens), x = x, g = g, x2 = round(x + stats::rnorm(n, 0, 0.03), 3),
             stringsAsFactors = FALSE)
}

.psc_params <- function(distribution, predictors = list("x", "g"), ...) {
  utils::modifyList(list(time = "t", event = "e", event_value = 1, predictors = predictors, distribution = distribution), list(...))
}

.psc_run <- function(df, params, limits = NULL, extra = NULL) {
  task <- .ps_test_task(df, params)
  if (!is.null(limits)) task$limits <- utils::modifyList(task$limits, limits)
  for (k in names(extra)) task[[k]] <- extra[[k]]
  fit <- fit_parametric_survival(task)
  f <- tab_results_frame(fit$rows)
  problems <- tab_check_declared(f, "parametric_survival", task$variant)
  .tab_expect(!length(problems), paste(problems, collapse = "; "))
  f
}

.psc_stat <- function(f, id, stat, level_2 = NULL) {
  at <- f$section %in% "assumption" & f$level %in% id & f$stat %in% stat & (if (is.null(level_2)) TRUE else f$level_2 %in% level_2)
  v <- ifelse(is.na(f$text[at]), f$value[at], f$text[at])
  stats::setNames(v, ifelse(is.na(f$level_2[at]), "", f$level_2[at]))
}

TAB_COMMON_TESTS[["parametric survival checks: a verdict is a p-value against the node's alpha, both sides; a measure has none"]] <- function(code) {
  verdicts <- c("distribution_vs_gengamma", "constant_hazard", "ph_cox", "ph_cox_global", "convergence", "separation")
  measures <- c("km_agreement", "dfbeta", "epv", "vif", "extrapolation")
  steep <- .psc_table(300, 3, 12)
  # the exponential on Weibull data of shape 3: the tests reject at the default alpha
  bad <- .psc_run(steep, .psc_params("exponential"))
  for (id in c("constant_hazard", "distribution_vs_gengamma")) .tab_expect(.psc_stat(bad, id, "verdict") == "failed", id, " on a wrong fit")
  .tab_expect(all(.psc_stat(bad, "ph_cox", "verdict") %in% c("failed", "ok")), "ph_cox's verdicts")
  # both sides of alpha for each p-value check, the p-value itself unchanged
  set.seed(18)
  cross <- data.frame(g = rep(c("a", "b"), each = 150)) # two groups whose hazards cross (shapes 0.6 and 3)
  tt <- c(stats::rweibull(150, 0.6, 10), stats::rweibull(150, 3, 10))
  cross$t <- pmax(round(pmin(tt, 25), 2), 0.01)
  cross$e <- as.integer(tt <= 25)
  edge <- function(df, params, id) {
    p <- unname(.psc_stat(.psc_run(df, params), id, "p_value"))
    .tab_expect(length(p) >= 1L && all(p > 0 & p < 0.05), id, ": p ", paste(p, collapse = " "))
    p <- max(p)
    above <- .psc_run(df, utils::modifyList(params, list(alpha = min(0.99, p * 2))))
    below <- .psc_run(df, utils::modifyList(params, list(alpha = p / 2)))
    .tab_expect(all(.psc_stat(above, id, "verdict") == "failed") && all(.psc_stat(below, id, "verdict") == "ok"),
                id, ": alpha above p fails it, below p passes it")
    .tab_expect(unname(.psc_stat(above, id, "threshold"))[[1L]] == min(0.99, p * 2), id, ": the threshold is alpha")
  }
  for (id in c("constant_hazard", "distribution_vs_gengamma")) edge(steep, .psc_params("exponential"), id)
  for (id in c("ph_cox", "ph_cox_global")) edge(cross, .psc_params("weibull", list("g")), id)
  .tab_expect("model|||alpha" %in% bad$key, "alpha is written")
  right <- .psc_run(steep, .psc_params("weibull"))
  .tab_expect(all(.psc_stat(right, "distribution_vs_gengamma", "verdict") == "ok") && all(.psc_stat(right, "ph_cox", "verdict") == "ok"),
              "the Weibull on its own data")
  flat <- .psc_run(.psc_table(240, 1, 13), .psc_params("exponential"))
  .tab_expect(.psc_stat(flat, "constant_hazard", "verdict") == "ok", "constant_hazard on exponential data")
  # a measure has no verdict, no advice, and says what it measured
  for (id in measures) {
    at <- right$section == "assumption" & right$level == id
    .tab_expect(any(at) && !any(right$stat[at] %in% c("verdict", "advice", "advice_id")) && any(right$stat[at] == "description"),
                id, " has a verdict or no description")
  }
  .tab_expect(all(.psc_stat(bad, "km_agreement", "value") > 0.05) && all(.psc_stat(right, "km_agreement", "value") < 0.15),
              "the gap of the wrong fit and the right one")
  # events per parameter: a number, 12 events and 4 parameters
  few <- .psc_run(.psc_table(24, 1.5, 14, censor_to = 4), .psc_params("weibull"))
  .tab_expect(abs(.psc_stat(few, "epv", "value") * .psc_stat(few, "epv", "df") - .psc_stat(few, "epv", "count")) < 1e-9, "epv")
  # separation, a fact: a level without events; the fit goes on
  sep <- .psc_table(60, 1.5, 15)
  sep$e[sep$g == "b"] <- 0L
  f_sep <- .psc_run(sep, .psc_params("weibull"))
  .tab_expect(identical(.psc_stat(f_sep, "separation", "verdict"), c("g=b" = "failed")), "separation")
  # influence: one row far from the others in x, with a short time, is named first
  out <- .psc_table(40, 1.5, 16)
  out$x[[1L]] <- 8
  out$t[[1L]] <- 0.05
  out$e[[1L]] <- 1L
  f_out <- .psc_run(out, .psc_params("weibull", list("x")))
  .tab_expect(.psc_stat(f_out, "dfbeta", "value", "x") > 1 && grepl("^1(,|$)", .psc_stat(f_out, "dfbeta", "rows", "x")), "dfbeta names the outlier first")
  # collinear predictors: a large VIF is said, not judged
  col <- .psc_run(.psc_table(200, 1.5, 17), .psc_params("weibull", list("x", "x2")))
  .tab_expect(all(.psc_stat(col, "vif", "value") > 10) && !"verdict" %in% col$stat[col$level == "vif" & col$section == "assumption"], "vif")
  # extrapolation: said, whatever the distance
  far <- .psc_run(.psc_table(240, 1.5, 11), .psc_params("weibull", times = list(1e6)))
  .tab_expect(.psc_stat(far, "extrapolation", "value") > 1 && grepl("beyond it", .psc_stat(far, "extrapolation", "description")), "extrapolation")
  f_ph <- .psc_run(cross, .psc_params("weibull", list("g")))
  .tab_expect(identical(.psc_stat(f_ph, "ph_cox", "verdict"), c(g = "failed")) && .psc_stat(f_ph, "ph_cox_global", "verdict") == "failed", "ph_cox")
  # the step's time used up: the comparison with a fit more is not made
  f_t <- .psc_run(steep, .psc_params("weibull"), limits = list(time_budget_seconds = 0.001))
  .tab_expect(.psc_stat(f_t, "distribution_vs_gengamma", "verdict") == "not_computed", "the time of the step")
  # the fits added given no time (their allowance used up): the comparisons are not made and say
  # why; the likelihood-ratio tests of the model section are not written, and a note says so
  f_big <- .psc_run(steep, .psc_params("weibull"), extra = list(.selftest_allowance = 0))
  .tab_expect(.psc_stat(f_big, "distribution_vs_gengamma", "verdict") == "not_computed" &&
              grepl("longer to fit than the model itself", .psc_stat(f_big, "distribution_vs_gengamma", "advice")) &&
              .psc_stat(f_big, "distribution_vs_gengamma", "advice_id") == "distribution_vs_gengamma_time", "the generalized gamma's time")
  .tab_expect(!any(c("model|||lr_statistic", "model|||exponential_lr_statistic") %in% f_big$key) &&
              identical(f_big$text[match(c("model|||note_lr_time", "model|||note_exponential_lr_time"), f_big$key)],
                        c("@lr_time", "@exponential_lr_time")), "the LR tests out of time")
  ex_t <- .psc_run(steep, .psc_params("exponential"), extra = list(.selftest_allowance = 0))
  .tab_expect(.psc_stat(ex_t, "constant_hazard", "advice_id") == "constant_hazard_time", "the Weibull's time")
  # the clock is read while a fit runs, not only before: a loop stopped at its allowance
  t0 <- proc.time()[["elapsed"]]
  stopped <- ps_within(0.3, repeat sum(stats::runif(100)))
  .tab_expect(isTRUE(stopped$time_up) && is.null(stopped$value) && proc.time()[["elapsed"]] - t0 < 5, "ps_within stops a loop")
  .tab_expect(identical(ps_within(5, 1 + 1), list(value = 2, time_up = FALSE)) && identical(ps_within(Inf, 3)$value, 3), "ps_within's value")
  .tab_expect(inherits(tryCatch(ps_within(5, stop("not the time")), error = function(e) e), "error"), "ps_within raises another error")
  # convergence, a fact: a covariance that is not there fails it
  task <- .ps_test_task(steep, .psc_params("weibull"))
  prepared <- ps_prepare(task)
  fitted <- ps_newton(ps_fit(prepared, "weibull"))
  fitted$vcov <- NULL
  rows <- tab_rows()
  psc_convergence(psc_context(task, prepared, fitted, NULL), rows)
  .tab_expect(.psc_stat(tab_results_frame(rows), "convergence", "verdict") == "failed", "convergence without a covariance")
}

TAB_COMMON_TESTS[["parametric survival checks: a check that does not apply is not written"]] <- function(code) {
  df <- .psc_table(200, 1.5, 21)
  ids <- function(f) unique(f$level[f$section == "assumption"])
  charts <- function(f) unique(f$level[f$section %in% c("curve", "point")])
  ph_aft <- .psc_run(df, .psc_params("lognormal"))
  .tab_expect(!any(c("constant_hazard", "ph_cox", "ph_cox_global") %in% ids(ph_aft)) && "distribution_vs_gengamma" %in% ids(ph_aft) &&
              !"loglog" %in% charts(ph_aft), "the log-normal: ", paste(ids(ph_aft), collapse = ", "))
  gomp <- .psc_run(df, .psc_params("gompertz"))
  .tab_expect(!any(c("constant_hazard", "distribution_vs_gengamma") %in% ids(gomp)) && "ph_cox" %in% ids(gomp) && !"loglog" %in% charts(gomp),
              "Gompertz: ", paste(ids(gomp), collapse = ", "))
  for (d in c("loglogistic", "gengamma")) {
    f <- .psc_run(df, .psc_params(d))
    .tab_expect(!any(c("constant_hazard", "distribution_vs_gengamma", "ph_cox") %in% ids(f)) && !"loglog" %in% charts(f), d, ": ", paste(ids(f), collapse = ", "))
  }
  wb <- .psc_run(df, .psc_params("weibull"))
  .tab_expect(all(c("ph_cox", "distribution_vs_gengamma", "extrapolation") %in% ids(wb)) && !"constant_hazard" %in% ids(wb) && "loglog" %in% charts(wb) &&
              all(c("fit_km", "cox_snell", "profile_survival", "hazard", "deviance_linear") %in% charts(wb)), "the Weibull: ", paste(ids(wb), collapse = ", "))
  ex <- .psc_run(df, .psc_params("exponential"))
  .tab_expect("constant_hazard" %in% ids(ex) && "loglog" %in% charts(ex), "the exponential")
  # no predictors: no proportional hazards, influence, VIF, deviance chart; no category: no separation
  bare <- .psc_run(df, .psc_params("weibull", list()))
  .tab_expect(!any(c("ph_cox", "ph_cox_global", "dfbeta", "vif", "separation") %in% ids(bare)) && !"deviance_linear" %in% charts(bare) &&
              all(c("convergence", "epv", "km_agreement", "extrapolation") %in% ids(bare)), "no predictors: ", paste(ids(bare), collapse = ", "))
  numeric_only <- .psc_run(df, .psc_params("weibull", list("x")))
  .tab_expect(!"separation" %in% ids(numeric_only) && !"vif" %in% ids(numeric_only), "a number only")
  # strata: the groups of the agreement and of the charts are the strata
  df$s <- ifelse(df$x > 0, "up", "down")
  st <- .psc_run(df, .psc_params("weibull", list("x"), strata = "s"))
  .tab_expect(setequal(names(.psc_stat(st, "km_agreement", "value")), c("s=down", "s=up")) &&
              setequal(unique(st$level_2[st$level %in% "profile_survival"]), c("s=down", "s=up")), "strata")
  # a predictor of more than 6 levels is not a grouping: «all»
  many <- df
  many$h <- sample(letters[1:8], nrow(many), TRUE)
  f_many <- .psc_run(many, .psc_params("weibull", list("h")))
  .tab_expect(identical(names(.psc_stat(f_many, "km_agreement", "value")), "all"), "eight levels are not a grouping")
}

TAB_COMMON_TESTS[["parametric survival checks: the charts' points, exact on a table of six rows, capped on a long one"]] <- function(code) {
  # six rows, no predictors, exponential: the closed forms of everything the charts draw
  df <- data.frame(t = c(2, 3, 3, 5, 8, 9), e = c(1, 1, 0, 1, 0, 1))
  f <- .psc_run(df, list(time = "t", event = "e", event_value = 1, distribution = "exponential", quantiles = list(0.25)))
  rate <- 4 / 30
  pts <- function(section, level, stat, level_2 = NA_character_) {
    at <- f$section == section & f$level == level & f$stat == stat & (if (is.na(level_2)) is.na(f$level_2) else f$level_2 %in% level_2)
    f[at, ][order(as.integer(sub(".*[|]", "", f$key[at]))), ]
  }
  km <- pts("curve", "fit_km", "surv", "all")
  .tab_expect(isTRUE(all.equal(km$x0, c(0, 2, 3, 5, 8, 9))) && isTRUE(all.equal(km$value, c(1, 5 / 6, 2 / 3, 4 / 9, 4 / 9, 0))), "Kaplan–Meier's steps")
  fitted <- pts("curve", "fit_km", "fitted", "all")
  .tab_expect(nrow(fitted) == 100L && max(abs(fitted$value - exp(-rate * fitted$x0))) < 1e-12 && fitted$x0[[1L]] == 0 && fitted$x0[[100L]] == 9,
              "the fitted curve is exp(−λt)")
  # Cox–Snell: r = λ t; the Nelson–Aalen estimate of (r, δ) by hand
  cs <- pts("point", "cox_snell", "y")
  .tab_expect(isTRUE(all.equal(cs$x0, rate * sort(df$t), tolerance = 1e-9)) &&
              isTRUE(all.equal(cs$value, c(1 / 6, 1 / 6 + 1 / 5, 1 / 6 + 1 / 5, 1 / 6 + 1 / 5 + 1 / 3, 1 / 6 + 1 / 5 + 1 / 3, 1 / 6 + 1 / 5 + 1 / 3 + 1),
                               tolerance = 1e-9)), "the Cox–Snell points: ", paste(round(cs$value, 4), collapse = ", "))
  # the log-log chart: Kaplan–Meier's points where 0 < S < 1, the fitted line log λ + log t
  ll <- pts("point", "loglog", "y", "all")
  .tab_expect(isTRUE(all.equal(ll$x0, log(c(2, 3, 5)))) && isTRUE(all.equal(ll$value, log(-log(c(5 / 6, 2 / 3, 4 / 9))))), "the log-log points")
  line <- pts("curve", "loglog", "y", "all")
  .tab_expect(max(abs(line$value - (log(rate) + line$x0))) < 1e-9, "the log-log line")
  # the profile: S = exp(−λt) with its band around it, the hazard λ
  ps <- pts("curve", "profile_survival", "surv", "all")
  hz <- pts("curve", "hazard", "hazard", "all")
  lo <- pts("curve", "profile_survival", "lower", "all")
  hi <- pts("curve", "profile_survival", "upper", "all")
  .tab_expect(ps$x0[[1L]] == 0 && ps$value[[1L]] == 1 && max(abs(ps$value - exp(-rate * ps$x0))) < 1e-12 && nrow(ps) == 101L &&
              max(abs(hz$value - rate)) < 1e-9 && all(lo$value[-1L] < ps$value[-1L] & ps$value[-1L] < hi$value[-1L]), "the profile's curves")
  .tab_expect(max(hz$x0) == 9 && all(pts("curve", "hazard", "upper", "all")$value > rate), "the hazard's band")
  # capped: 600 rows, 50 points a chart, the largest residual kept
  long <- .psc_table(600, 1.5, 31)
  f_cap <- .psc_run(long, .psc_params("weibull"), limits = list(max_chart_points = 50L, extreme_points = 3L))
  counts <- table(sub("[|][0-9]+$", "", f_cap$key[f_cap$section %in% c("point", "curve") & f_cap$level %in% c("cox_snell", "deviance_linear", "loglog", "fit_km")]))
  .tab_expect(all(counts <= 50L), "a chart has more than 50 points: ", paste(names(counts)[counts > 50L], collapse = ", "))
  dev <- f_cap[f_cap$level == "deviance_linear" & f_cap$stat == "y", ]
  task <- .ps_test_task(long, .psc_params("weibull"))
  prepared <- ps_prepare(task)
  fitted_ <- ps_newton(ps_fit(prepared, "weibull"))
  worst <- which.max(abs(psc_residuals(psc_context(task, prepared, fitted_, NULL))$deviance))
  .tab_expect(nrow(dev) == 50L && as.character(worst) %in% dev$text, "the largest deviance residual is kept and named")
}

# ── delayed entry (`start`, lot P8) ──────────────────────────────────────────
TAB_COMMON_TESTS[["parametric survival, delayed entry: the refusals, the closed forms of the exponential, the rows output and Predict without a start"]] <- function(code) {
  # seven rows (start, time, event); the exponential's rate is d / Σ (time − start) = 4 / 17.5
  df <- data.frame(s = c(0, 1, 1, 1, 2, 4, 4), t = c(0.5, 2, 3, 3, 5, 8, 9), e = c(0, 1, 1, 0, 1, 0, 1))
  params <- list(time = "t", event = "e", event_value = 1, start = "s", distribution = "exponential", quantiles = list(0.25))
  fit <- fit_parametric_survival(.ps_test_task(df, params, "rows"))
  f <- tab_results_frame(fit$rows)
  .tab_expect(!length(tab_check_declared(f, "parametric_survival", "ph_aft")), paste(tab_check_declared(f, "parametric_survival", "ph_aft"), collapse = "; "))
  lambda <- 4 / 17.5
  get <- function(key) f$value[match(key, f$key)]
  .tab_expect(abs(get("model|||log_lik") - (4 * log(lambda) - lambda * 17.5)) < 1e-10, "the log-likelihood d log λ − λ Σ (t − s)")
  .tab_expect(abs(get("coef|(Intercept)||estimate") + log(lambda)) < 1e-10 && abs(get("coef|(Intercept)||std_error") - 0.5) < 1e-9, "the rate")
  .tab_expect(abs(get("stratum|all||median") - log(2) / lambda) < 1e-9, "the median")
  .tab_expect(get("model|||n_delayed") == 6 && get("model|||n") == 7, "the rows that enter after 0")
  .tab_expect(identical(f$text[match("model|||note_start_rows", f$key)], "@start_rows delayed=6 rows=7") &&
              identical(f$text[match("model|||formula", f$key)], "Surv(s, t, e = 1) ~ 1"), "the note and the formula")
  # the rows output: Cox–Snell is H(time), the martingale the hazard accrued while observed, H(time) − H(start)
  out <- fit$output
  .tab_expect(max(abs(out$pred_cox_snell - lambda * df$t)) < 1e-9 && max(abs(out$pred_martingale - (df$e - lambda * (df$t - df$s)))) < 1e-9,
              "the residuals of the rows output")
  # Predict reads the card, which holds the parameters and the start's column, never a start
  card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
  .tab_expect(identical(card$parametric_survival$start_column, "s"), "the card names the start's column")
  p_ <- predict_parametric_survival(list(params = list(), .frame = data.frame(z = 1:2)), card)$output
  .tab_expect(all(abs(p_$prediction - log(2) / lambda) < 1e-9), "Predict without a start")
  # a start of 0 for every row is the fit without a start
  zero <- df[df$t > 0.5, ]
  zero$s <- 0
  a <- tab_results_frame(fit_parametric_survival(.ps_test_task(zero, utils::modifyList(params, list(distribution = "weibull"))))$rows)
  b <- tab_results_frame(fit_parametric_survival(.ps_test_task(zero, utils::modifyList(params, list(distribution = "weibull", start = NULL))))$rows)
  for (key in c("model|||log_lik", "coef|(Intercept)||estimate", "parameter|shape||estimate", "stratum|all||median")) {
    .tab_expect(abs(a$value[match(key, a$key)] / b$value[match(key, b$key)] - 1) < 1e-9, key, " with a start of 0 is not the fit without")
  }
  # the refusals: Cox's sentence for a start that is not before the time, a negative start, a start that is not a number
  bad <- df
  bad$s[[3L]] <- 3
  said <- .tab_refused_with("start_not_before_time", fit_parametric_survival(.ps_test_task(bad, params)))
  .tab_expect(identical(said, "in row 3 the start «s» (3.0) is not before the time «t» (3.0): each row is an interval (start, time]"), said)
  neg <- df
  neg$s[[2L]] <- -1
  .tab_refused_with("negative_start", fit_parametric_survival(.ps_test_task(neg, params)))
  text <- df
  text$s <- as.character(text$s)
  .tab_refused_with("time_type", fit_parametric_survival(.ps_test_task(text, params)))
  .tab_refused_with("missing_column", fit_parametric_survival(.ps_test_task(df, utils::modifyList(params, list(start = "nowhere")))))
}

TAB_COMMON_TESTS[["parametric survival, delayed entry: Kaplan–Meier with the rows at risk, from the first time with enough of them; Cox–Snell's pairs"]] <- function(code) {
  df <- data.frame(s = c(0, 1, 1, 1, 2, 4, 4), t = c(0.5, 2, 3, 3, 5, 8, 9), e = c(0, 1, 1, 0, 1, 0, 1))
  params <- list(time = "t", event = "e", event_value = 1, start = "s", distribution = "exponential", quantiles = list(0.25))
  old <- get("PS_CHECK_LIMITS", envir = globalenv())
  assign("PS_CHECK_LIMITS", utils::modifyList(old, list(km_min_at_risk = 2L)), envir = globalenv()) # seven rows: two at risk are enough
  f <- tryCatch(.psc_run(df, params), finally = assign("PS_CHECK_LIMITS", old, envir = globalenv()))
  lambda <- 4 / 17.5
  pts <- function(section, level, stat) {
    at <- f$section == section & f$level == level & f$stat == stat
    f[at, ][order(as.integer(sub(".*[|]", "", f$key[at]))), ]
  }
  # the first time, 0.5, has one row at risk (the others enter later): the curve is read from there, the rows that enter later at their start
  km <- pts("curve", "fit_km", "surv")
  .tab_expect(isTRUE(all.equal(km$x0, c(0.5, 2, 3, 5, 8, 9))) && isTRUE(all.equal(km$value, c(1, 2 / 3, 4 / 9, 8 / 27, 8 / 27, 0))),
              "Kaplan–Meier's steps with entry: ", paste(round(km$value, 4), collapse = ", "))
  fitted <- pts("curve", "fit_km", "fitted")
  .tab_expect(fitted$x0[[1L]] == 0.5 && fitted$x0[[nrow(fitted)]] == 9 && max(abs(fitted$value - exp(-lambda * (fitted$x0 - 0.5)))) < 1e-9,
              "the fitted curve conditional on lasting until t0: exp(−λ (t − t0))")
  # Cox–Snell: the pairs (λ s, λ t); Nelson–Aalen with entry from λ · 0.5, set back on the diagonal
  cs <- pts("point", "cox_snell", "y")
  c0 <- lambda * 0.5
  .tab_expect(isTRUE(all.equal(cs$x0, lambda * c(2, 3, 3, 5, 8, 9), tolerance = 1e-9)) &&
              isTRUE(all.equal(cs$value, c0 + c(1 / 3, 2 / 3, 2 / 3, 1, 1, 2), tolerance = 1e-9)), "the Cox–Snell points: ", paste(round(cs$value, 4), collapse = ", "))
  ag <- .psc_stat(f, "km_agreement", "value")
  .tab_expect(length(ag) == 1L && is.finite(ag), "the agreement is read")
  # without enough rows at risk anywhere nothing is read, and it is said
  old <- get("PS_CHECK_LIMITS", envir = globalenv())
  assign("PS_CHECK_LIMITS", utils::modifyList(old, list(km_min_at_risk = 50L)), envir = globalenv())
  g <- tryCatch(.psc_run(df, params), finally = assign("PS_CHECK_LIMITS", old, envir = globalenv()))
  .tab_expect(!"value" %in% g$stat[g$level == "km_agreement"] && grepl("rows at risk", .psc_stat(g, "km_agreement", "description")), "no time with rows at risk")
  # the Weibull's p-value checks and dfbetas follow the conditional likelihood: a table with entries and predictors
  set.seed(5)
  n <- 120
  x <- round(stats::rnorm(n), 2)
  t <- stats::rweibull(n, 1.5, exp(2 + 0.3 * x))
  entry <- ifelse(stats::runif(n) < 0.5, 0, round(stats::runif(n, 0, 5), 2))
  tt <- data.frame(s = entry, t = pmax(round(pmin(t, entry + stats::runif(n, 3, 20)), 2), entry + 0.01), x = x)
  tt$e <- as.integer(t <= tt$t)
  tt <- tt[t > entry, ]
  w <- .psc_run(tt, list(time = "t", event = "e", event_value = 1, start = "s", predictors = list("x"), distribution = "weibull"))
  .tab_expect(all(.psc_stat(w, "ph_cox", "verdict") %in% c("ok", "failed")) && all(.psc_stat(w, "distribution_vs_gengamma", "verdict") %in% c("ok", "failed")) &&
              is.finite(.psc_stat(w, "dfbeta", "value", "x")), "the checks with entry give verdicts from their p-values")
  # the dfbetas of the row log-likelihood with entry: one outlier far from the rest is named first
  tt$x[[1L]] <- 8
  tt$t[[1L]] <- tt$s[[1L]] + 0.05
  tt$e[[1L]] <- 1L
  o <- .psc_run(tt, list(time = "t", event = "e", event_value = 1, start = "s", predictors = list("x"), distribution = "weibull"))
  .tab_expect(.psc_stat(o, "dfbeta", "value", "x") > 1 && grepl("^1(,|$)", .psc_stat(o, "dfbeta", "rows", "x")), "dfbeta names the outlier first")
}
