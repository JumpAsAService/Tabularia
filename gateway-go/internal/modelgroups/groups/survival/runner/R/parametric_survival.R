# Parametric survival (docs/specs/statistical-models.md, «The node «Parametric survival»», lot P1):
# the time to an event as one of six distributions, fitted by flexsurv::flexsurvreg with the
# predictors on the distribution's location (Weibull, log-normal, log-logistic, generalized gamma:
# accelerated failure time; exponential: its rate, read as AFT with the sign turned; Gompertz: its
# rate, proportional hazards), strata with a location and a shape of their own (flexsurv's `anc`),
# case weights, and the intervals at 1 − alpha.
#
# What the runner computes itself, not flexsurv:
#   - the log-likelihood, Σ wᵢ (δᵢ log f(tᵢ) + (1 − δᵢ) log S(tᵢ)) from the distribution's d and p
#     functions, checked equal to flexsurv's at the estimates (a gap is a bug: the Job fails);
#   - the estimates' last digits: Newton steps on that log-likelihood after flexsurv's BFGS;
#   - the covariance: the inverse of the Hessian of that log-likelihood on the optimiser's scale,
#     from each row's derivatives in its own few parameters (lot P12; before, a Richardson Hessian
#     by numDeriv of all the parameters, kept where the rows' fail) (flexsurv's own is optim's finite differences where a distribution has no
#     analytic derivatives: 5e-4 away from survreg's for the log-logistic);
#   - every prediction (the profile in `stratum`, the `rows` output, Predict) from the model card,
#     with the delta method for its intervals: a quantile's on log time, a survival's on
#     log(−log S), the gradient by numDeriv::jacobian over the rows, by chunks.
#
# The pieces, fixed by the spec so that the checks (P2, parametric_survival_checks.R) and the
# automatic choice (P3, parametric_survival_auto.R) build on them:
#   ps_prepare(task)                    the positional frame, the design, the strata's dummies, the
#                                       weights, the rows left out and their counts, the profile;
#   ps_fit(prepared, distribution, starts = NULL)
#                                       flexsurvreg with reltol 1e-15 and a second attempt; the
#                                       optimiser's vector, the log-likelihood and loglik_rows(par);
#   ps_covariance(fitted)               V, the inverse of the Hessian from the rows' own derivatives
#                                       (Richardson's where those fail), or NULL;
#   ps_newton(fitted)                   Newton steps from flexsurv's estimates to the maximum of the
#                                       runner's log-likelihood, and V there (BFGS stops on the
#                                       function's change, 2.5e-9 to 1e-4 away from it);
#   ps_predict(model, X, strata, quantiles, times, interval, alive_at = NULL)
#                                       quantiles, survival and hazard with their intervals, where
#                                       `model` is ps_model(card): the card's numbers, never a row;
#   ps_write(task, prepared, fitted, rows, …)
#                                       the sections model, coef, parameter and stratum.
# Delayed entry (lot P8): `start` (a number column, Cox's key) puts each row's observation at
# (start, time]: the subject was not seen before `start`. Each row's log-likelihood then loses
# log S(start), the likelihood conditional on lasting until the start (Surv(start, time, status)
# is flexsurv's left truncation); with several rows of one subject (start–stop rows) they are
# taken as independent. Predict needs no start: the card holds the distribution's parameters, and
# the time already survived of one unit is Predict's `alive_at`, a different thing (below).
# Not here yet: the checks (`assumption`) and the charts' points (P2: ps_checks), the automatic
# choice (P3: ps_auto). Conditional survival (P9) is ps_predict's alive_at. Their hooks are below,
# found by name, so that a lot adds its file without touching this one.
#
# Columns never become code: the design has positional columns, copied with the strata's dummies
# into a frame of fixed names (time, status, w, d1, d2…, s1, s2…) before flexsurv, survreg or
# coxph see them; a column's name is only ever written as a label.

PS_PACKAGES <- c("survival", "flexsurv", "numDeriv")
PS_RELTOL <- 1e-15           # flexsurv's default (1e-8) leaves a Gompertz coefficient 7.5e-6 away
PS_RELTOL_START <- 1e-10     # with a start BFGS runs on numerical gradients (flexsurv has no analytic ones for left truncation; a fit costs 5.8 s on 88,000 rows at 1e-10, 10.7 s at 1e-15): it stops sooner, the Newton steps do the rest
PS_MAXIT <- 2000L
PS_MAXIT_SECOND <- 5000L
PS_LOGLIK_CHECK <- 1e-10     # the runner's log-likelihood against flexsurv's, relative
PS_CARD_CHECK <- 1e-9        # the card's predictions against flexsurv's own, in the self-test
PS_CHUNK <- 100000L          # rows a delta-method jacobian takes at once
PS_NEWTON_STEPS <- 4L        # Newton steps after flexsurv's BFGS, at most
PS_NEWTON_DONE <- 1e-8       # a step below this many standard errors ends them
PS_NEWTON_STALE <- 3L        # with a start: steps after one with a Hessian that reuse it
PS_ALL <- "all"              # the stratum's name without strata

# The six distributions: flexsurv's name, the parameter the strata's shape is (none for the
# exponential), how a location coefficient reads (+1: log time ratio; −1: the exponential's rate,
# turned so that it reads as one; 0: Gompertz, a log hazard ratio), the parameters' printed names.
PS_DISTRIBUTIONS <- list(
  weibull = list(flexsurv = "weibull", anc = "shape", aft = 1, label = "Weibull"),
  exponential = list(flexsurv = "exp", anc = NA_character_, aft = -1, label = "exponential"),
  lognormal = list(flexsurv = "lnorm", anc = "sdlog", aft = 1, label = "log-normal"),
  loglogistic = list(flexsurv = "llogis", anc = "shape", aft = 1, label = "log-logistic"),
  gengamma = list(flexsurv = "gengamma", anc = "sigma", aft = 1, label = "generalized gamma"),
  gompertz = list(flexsurv = "gompertz", anc = "shape", aft = 0, label = "Gompertz"))

# The notes, by id (model|||note_<id>): the report says them in the page's language.
PS_NOTE_HR_INTERVAL <- paste0(
  "The hazard ratio's interval is computed on its own scale (the delta method), the p-value on the coefficient's: near the ",
  "edge the interval can exclude 1 while p is just above alpha, or the other way round.")
PS_NOTE_STRATA_NO_HR <- paste0(
  "With strata, a Weibull model has a shape per stratum: the hazard ratio of a predictor differs in each stratum while its ",
  "time ratio is common, so only the time ratios are given.")
PS_NOTE_WEIGHTS_COUNTS <- "@weights_counts" # the report words it (a note is `@id name=value …`)

ps_params <- function(params) {
  p <- tab_model_spec("parametric_survival")$defaults
  for (k in names(params)) p[k] <- list(params[[k]])
  p
}

fit_parametric_survival <- function(task) {
  prepared <- ps_prepare(task)
  started <- proc.time()[["elapsed"]]
  if (identical(prepared$distribution, "auto")) {
    auto <- get0("ps_auto", mode = "function") # lot P3, parametric_survival_auto.R
    if (is.null(auto)) tab_not_built("The automatic choice of the distribution")
    chosen <- auto(task, prepared)
    fitted <- chosen$fitted
  } else {
    chosen <- NULL
    # the self-test forces the first attempt to fail, so that the second start is tested
    fail_first <- identical(task$group_version, "selftest") && isTRUE(task$.selftest_fail_first)
    ps_refuse_too_few_rows(prepared, prepared$distribution)
    fitted <- ps_fit(prepared, prepared$distribution, fail_first = fail_first)
  }
  ps_refuse_not_converged(fitted)
  fitted <- ps_newton(fitted)
  # what the node's own fit cost (with auto, the choice): the fits the checks add are given as
  # much time, never more (PS_CHECK_LIMITS$extra_fit_min_seconds at least)
  fitted$seconds <- proc.time()[["elapsed"]] - started
  # with auto, the candidates already fitted: the checks reuse them instead of fitting them again
  if (!is.null(chosen)) fitted$candidates <- chosen$fits
  card <- ps_card(task, prepared, fitted, selection = if (is.null(chosen)) "manual" else "auto")
  model <- ps_model(card)
  rows <- tab_rows_start(task)
  ps_write(task, prepared, fitted, rows, model)
  if (!is.null(chosen) && is.function(chosen$write)) chosen$write(rows) # the candidates (P3)
  checks <- get0("ps_checks", mode = "function") # lot P2, parametric_survival_checks.R
  if (!is.null(checks)) checks(task, prepared, fitted, model, rows)
  ps_check_card(task, card, prepared, fitted)
  output <- NULL
  if (identical(task$output %||% prepared$p$output, "rows")) output <- ps_rows_output(prepared$input, model, prepared, fitted)
  list(rows = rows, output = output, card = card, native = ps_native(fitted, prepared$alpha))
}

# ── the rows, the design, the strata ─────────────────────────────────────────
ps_prepare <- function(task) {
  p <- ps_params(task$params)
  distribution <- p$distribution %||% "weibull"
  if (!distribution %in% c(names(PS_DISTRIBUTIONS), "auto")) {
    tab_refuse("distribution", paste0("there is no distribution «", distribution, "»: weibull, exponential, lognormal, loglogistic, ",
                                      "gengamma, gompertz or auto"), list(distribution = distribution))
  }
  time_col <- p$time
  event_col <- p$event
  strata_col <- p$strata
  weights_col <- p$weights
  start_col <- p$start
  predictors <- unlist(p$predictors)
  interactions <- p$interactions %||% list()
  alpha <- as.numeric(p$alpha %||% 0.05)
  quantiles <- ps_quantiles(p$quantiles)
  times <- sort(unique(as.numeric(unlist(p$times %||% list()))))

  df <- tab_input(task)
  for (column in c(time_col, event_col, strata_col, weights_col, start_col, predictors)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  kind <- tab_kind(df[[time_col]])
  if (!kind %in% c("number", "integer")) {
    tab_refuse("time_type", paste0("the column «", time_col, "» is a ", kind, ": a time must be a number, the time from the ",
                                   "start (compute it from the dates first)"), list(column = time_col))
  }
  if (!is.null(start_col)) {
    kind <- tab_kind(df[[start_col]])
    if (!kind %in% c("number", "integer")) {
      tab_refuse("time_type", paste0("the column «", start_col, "» is a ", kind, ": a time must be a number, the time from the ",
                                     "start (compute it from the dates first)"), list(column = start_col))
    }
  }
  if (!is.null(weights_col) && !tab_kind(df[[weights_col]]) %in% c("number", "integer")) {
    tab_refuse("weights_type", paste0("the column «", weights_col, "» is a ", tab_kind(df[[weights_col]]), ": weights must be ",
                                      "numbers, 0 or more"), list(column = weights_col))
  }
  ps_alive_column(df, p$alive_at)
  design <- tab_design(df, predictors, reference = p$reference %||% list(), interactions = interactions, intercept = FALSE,
                       also = c(time_col, event_col, strata_col, weights_col, start_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  d <- design$data
  time <- as.numeric(d[[time_col]])
  status <- km_is_event(d[[event_col]], p$event_value)
  value_label <- km_value_label(p$event_value)
  w <- if (is.null(weights_col)) rep(1, nrow(d)) else as.numeric(d[[weights_col]])
  start <- if (is.null(start_col)) NULL else as.numeric(d[[start_col]])
  if (any(time < 0)) {
    tab_refuse("negative_time", paste0("the column «", time_col, "» holds a negative time (", tab_number_label(min(time)),
                                       "): a time is counted from the start, 0 or more"), list(column = time_col))
  }
  # a time of 0 has no density under any of the six distributions: its rows are left out (and
  # counted) before anything else is asked of them, with a start or without
  zero_time <- time == 0
  if (!is.null(start)) {
    if (any(start < 0)) {
      tab_refuse("negative_start", paste0("the column «", start_col, "» holds a negative start (", tab_number_label(min(start)),
                                          "): a time is counted from the start, 0 or more"), list(column = start_col))
    }
    # as Cox's: each row is an interval (start, time]
    if (any(start >= time & !zero_time)) {
      i <- which(start >= time & !zero_time)[[1L]]
      tab_refuse("start_not_before_time", paste0("in row ", design$rows[[i]], " the start «", start_col, "» (",
                                                 tab_number_label(start[[i]]), ") is not before the time «", time_col, "» (",
                                                 tab_number_label(time[[i]]), "): each row is an interval (start, time]"),
                 list(row = design$rows[[i]], start = start_col, time = time_col))
    }
  }
  if (any(w < 0)) {
    tab_refuse("negative_weight", paste0("the column «", weights_col, "» holds a negative weight (", tab_number_label(min(w)),
                                         "): a weight is 0 or more"), list(column = weights_col))
  }
  # a weight of 0 adds nothing
  zero_weight <- w == 0 & !zero_time
  keep <- !zero_time & !zero_weight
  rows_used <- design$rows[keep]
  X <- design$X[keep, , drop = FALSE]
  time <- time[keep]
  status <- status[keep]
  w <- w[keep]
  if (!is.null(start)) start <- start[keep]
  d <- d[keep, , drop = FALSE]
  if (!length(time)) tab_refuse("no_rows", "no row has a value in every column the model uses and a time above 0: nothing to fit")
  if (!any(status)) {
    tab_refuse("no_events", paste0("the column «", event_col, "» is never «", value_label, "» in the rows the model uses: ",
                                   "with no event there is nothing to fit; check event_value"),
               list(column = event_col, event_value = value_label))
  }

  # strata: a location and a shape of their own, the first level in text order the reference
  if (is.null(strata_col)) {
    labels <- rep(PS_ALL, length(time))
    levels <- PS_ALL
    names_ <- PS_ALL
  } else {
    labels <- tab_label(d[[strata_col]])
    levels <- tab_sort_labels(labels)
    if (length(levels) > tab_limit(task, "max_strata")) {
      tab_refuse("too_many_strata", paste0("the column «", strata_col, "» makes ", length(levels), " strata (at most ",
                                           tab_limit(task, "max_strata"), "): is it the right column?"),
                 list(column = strata_col, strata = length(levels)))
    }
    names_ <- paste0(strata_col, "=", levels)
    for (i in seq_along(levels)) {
      if (!any(status[labels == levels[[i]]])) {
        tab_refuse("stratum_no_events", paste0("the stratum «", names_[[i]], "» has no event: its shape cannot be estimated; ",
                                               "merge it with another, or leave its rows out"),
                   list(column = strata_col, stratum = levels[[i]]))
      }
    }
  }
  stratum <- match(labels, levels)
  S <- ps_strata_dummies(stratum, length(levels))

  # the design's columns and the strata's dummies against a column of ones: the baseline takes any constant
  q <- qr(cbind(1, X, S), tol = 1e-7)
  if (q$rank < ncol(X) + ncol(S) + 1L) {
    bad <- q$pivot[seq.int(q$rank + 1L, ncol(X) + ncol(S) + 1L)] - 1L
    bad <- bad[bad > 0L & bad <= ncol(X)]
    label <- if (length(bad)) design$terms$label[[bad[[1L]]]] else if (ncol(X)) design$terms$label[[1L]] else strata_col
    tab_refuse("aliased", paste0("the term «", label, "» is constant, or a combination of the others and the strata (the design ",
                                 "is singular): drop it, or a predictor that repeats it"),
               list(terms = as.list(design$terms$label[bad])))
  }
  frame <- data.frame(time = time, status = as.integer(status), w = w)
  if (!is.null(start)) frame$start <- start
  for (j in seq_len(ncol(X))) frame[[paste0("d", j)]] <- X[, j]
  for (j in seq_len(ncol(S))) frame[[paste0("s", j)]] <- S[, j]

  # the profile: every numeric predictor at its weighted mean over the rows used, every category at
  # its reference level, interactions from those; one row per stratum
  profile_row <- list()
  profile_text <- character()
  for (input in design$inputs) {
    if (identical(input$type, "number")) {
      v <- sum(w * as.numeric(d[[input$name]])) / sum(w)
      profile_row[[input$name]] <- v
      profile_text <- c(profile_text, paste0(input$name, " = ", format(signif(v, 4), trim = TRUE), " (mean)"))
    } else {
      profile_row[[input$name]] <- input$reference
      profile_text <- c(profile_text, paste0(input$name, " = ", input$reference, " (reference)"))
    }
  }

  list(p = p, task = task, distribution = distribution, input = df, design = design, X = X, S = S, frame = frame,
       time = time, status = status, w = w, start = start, start_col = start_col, rows = rows_used, stratum = stratum, strata_col = strata_col,
       strata_levels = levels, strata_names = names_, time_col = time_col, event_col = event_col, weights_col = weights_col,
       value_label = value_label, predictors = predictors, interactions = interactions, alpha = alpha,
       quantiles = quantiles, times = times, n_missing = design$n_missing, n_zero_time = sum(zero_time),
       n_zero_weight = sum(zero_weight), profile_row = profile_row,
       profile_text = if (length(profile_text)) paste(profile_text, collapse = "; ") else NA_character_)
}

# The quantiles asked besides the median: in (0, 1), sorted, 0.5 not repeated.
ps_quantiles <- function(x) {
  q <- sort(unique(as.numeric(unlist(x %||% list()))))
  q[q > 0 & q < 1 & q != 0.5]
}

# The dummies of the strata after the first: a matrix of n rows and (levels − 1) columns; NA rows
# for an unknown stratum.
ps_strata_dummies <- function(stratum, k) {
  S <- matrix(0, length(stratum), max(k - 1L, 0L))
  for (j in seq_len(ncol(S))) S[, j] <- as.numeric(stratum == j + 1L)
  S[is.na(stratum), ] <- NA_real_
  S
}

# ── the model, as the runner reads it: a fit's or a card's numbers ───────────
# `m`: the distribution's functions (d, p, q), its parameters in flexsurv's order, which are on
# the log scale, the location's and the shape's positions, and where each coefficient sits in the
# optimiser's vector `par`: the parameters first, then the location's (the design's columns, then
# the strata's), then the shape's (the strata's).
ps_spec <- function(distribution, n_terms, n_strata) {
  info <- PS_DISTRIBUTIONS[[distribution]]
  dl <- flexsurv::flexsurv.dists[[info$flexsurv]]
  fn <- function(prefix) {
    name <- paste0(prefix, info$flexsurv)
    f <- get0(name, envir = asNamespace("flexsurv"), mode = "function")
    if (is.null(f)) f <- get(name, envir = asNamespace("stats"), mode = "function")
    f
  }
  nb <- length(dl$pars)
  loc <- match(dl$location, dl$pars)
  anc <- if (is.na(info$anc) || n_strata < 2L) NA_integer_ else match(info$anc, dl$pars)
  n_s <- max(n_strata - 1L, 0L)
  list(distribution = distribution, flexsurv = info$flexsurv, aft = info$aft, label = info$label, pars = dl$pars,
       location = dl$location, nb = nb, loc = loc, anc = anc, anc_name = if (is.na(anc)) NA_character_ else info$anc,
       log = vapply(dl$transforms, function(f) identical(f, base::log), logical(1)),
       n_terms = n_terms, n_strata = n_strata,
       i_terms = nb + seq_len(n_terms), i_strata = nb + n_terms + seq_len(n_s),
       i_anc = if (is.na(anc)) integer() else nb + n_terms + n_s + seq_len(n_s),
       d = fn("d"), p = fn("p"), q = fn("q"))
}

# The distribution's parameters of each row, from the optimiser's vector: the location's linear
# predictor and the shape's, added column by column in a fixed order (the same bits for a row
# whatever the rows around it: Predict on the rows of the fit gives the rows output's numbers).
ps_natural <- function(m, par, X, S) {
  n <- nrow(X)
  out <- vector("list", m$nb)
  names(out) <- m$pars
  for (j in seq_len(m$nb)) {
    eta <- rep(par[[j]], n)
    if (j == m$loc) {
      for (c in seq_along(m$i_terms)) eta <- eta + X[, c] * par[[m$i_terms[[c]]]]
      for (c in seq_along(m$i_strata)) eta <- eta + S[, c] * par[[m$i_strata[[c]]]]
    }
    if (!is.na(m$anc) && j == m$anc) for (c in seq_along(m$i_anc)) eta <- eta + S[, c] * par[[m$i_anc[[c]]]]
    out[[j]] <- if (m$log[[j]]) exp(eta) else eta
  }
  out
}

# The location's linear predictor (on flexsurv's scale: log scale, meanlog, mu, log rate).
ps_linear <- function(m, par, X, S) {
  eta <- rep(par[[m$loc]], nrow(X))
  for (c in seq_along(m$i_terms)) eta <- eta + X[, c] * par[[m$i_terms[[c]]]]
  for (c in seq_along(m$i_strata)) eta <- eta + S[, c] * par[[m$i_strata[[c]]]]
  eta
}

ps_log_density <- function(m, t, pars) do.call(m$d, c(list(x = t, log = TRUE), pars))
ps_log_survival <- function(m, t, pars) do.call(m$p, c(list(q = t, lower.tail = FALSE, log.p = TRUE), pars))
ps_quantile <- function(m, prob, pars) do.call(m$q, c(list(p = prob), pars))

# Each row's log-likelihood, δ log f(t) + (1 − δ) log S(t): the sum weighted by w is the model's.
# With a start (delayed entry) the row's likelihood is conditional on lasting until it: minus
# log S(start), exactly 0 where the start is 0 (the row is observed from the origin).
ps_loglik_rows <- function(m, par, time, status, X, S, start = NULL) {
  pars <- ps_natural(m, par, X, S)
  ld <- ps_log_density(m, time, pars)
  ls <- ps_log_survival(m, time, pars)
  out <- ifelse(status == 1L, ld, ls)
  if (!is.null(start)) {
    late <- which(start > 0) # only these rows have a log S(start) other than 0: the others are not computed
    if (length(late)) {
      here <- lapply(pars, function(p) p[late])
      out[late] <- out[late] - ps_log_survival(m, start[late], here)
    }
  }
  out
}

# ── the fit ──────────────────────────────────────────────────────────────────
# flexsurvreg on the positional frame. `starts`: fits already made on the same rows (the
# generalized gamma starts from the best of flexsurv's own point, the Weibull's and the
# log-normal's; with `auto` P3 passes those it has). A second attempt when the first stops with an
# error or a convergence code: the next start (generalized gamma), the exponential's rate and a
# flat shape (Gompertz), survreg's estimates (the four survreg knows), with maxit 5000.
# `init` (the optimiser's scale) replaces the starts, `reltol` the tolerance, and `second = FALSE`
# makes one attempt only: the fits of the checks, which start from a point of the node's own fit
# and take Newton steps after (ps_newton_steps).
ps_fit <- function(prepared, distribution, starts = NULL, hessian = FALSE, fail_first = FALSE, init = NULL, reltol = NULL,
                   second = TRUE) {
  m <- ps_spec(distribution, ncol(prepared$X), length(prepared$strata_levels))
  frame <- prepared$frame
  form <- ps_formula(prepared)
  anc <- ps_anc_formula(prepared, m)
  loglik_rows <- function(par) ps_loglik_rows(m, par, prepared$time, prepared$status, prepared$X, prepared$S, prepared$start)
  loglik <- function(par) sum(prepared$w * loglik_rows(par))
  inits <- if (is.null(init)) ps_starts(prepared, m, distribution, starts, loglik) else list(first = init, second = NULL)
  reltol <- reltol %||% (if (is.null(prepared$start)) PS_RELTOL else PS_RELTOL_START)
  fit_once <- function(init, maxit) {
    tryCatch(suppressWarnings(ps_flexsurvreg(form, anc, frame, m$flexsurv, init = if (is.null(init)) NULL else ps_to_natural(m, init),
                                             control = list(reltol = reltol, maxit = maxit), hessian = hessian)),
             error = function(e) e)
  }
  failed <- function(f) inherits(f, "error") || !identical(as.integer(f$opt$convergence %||% 1L), 0L) || !is.finite(f$loglik)
  attempts <- 1L
  first <- if (fail_first) simpleError("forced by the self-test") else fit_once(inits$first, PS_MAXIT)
  fit <- first
  if (failed(fit) && second) {
    attempts <- 2L
    second <- inits$second %||% ps_second_start(prepared, m, distribution, form, frame)
    fit <- fit_once(second, PS_MAXIT_SECOND)
  }
  if (failed(fit)) {
    reason <- if (inherits(fit, "error")) conditionMessage(fit) else paste0("optim's code ", fit$opt$convergence)
    return(list(distribution = distribution, spec = m, converged = FALSE, status = "not_converged", attempts = attempts,
                reason = reason, loglik = if (inherits(fit, "error")) NA_real_ else fit$loglik, fit = NULL,
                loglik_rows = loglik_rows, loglik_fn = loglik))
  }
  par <- fit$opt$par
  own <- loglik(par)
  if (!is.finite(own) || abs(own - fit$loglik) > PS_LOGLIK_CHECK * max(1, abs(fit$loglik))) {
    stop("the runner's log-likelihood (", format(own, digits = 17), ") is not flexsurv's (", format(fit$loglik, digits = 17), ")")
  }
  list(distribution = distribution, spec = m, converged = TRUE, status = "ok", attempts = attempts, fit = fit, delayed = !is.null(prepared$start),
       par = unname(par), names = names(par), loglik = fit$loglik, n_parameters = length(par),
       loglik_rows = loglik_rows, loglik_fn = loglik, centring = ps_centring(m, prepared$X, prepared$S), prepared = prepared)
}

# flexsurvreg called with symbols bound in an environment of its own, so that the call it keeps
# names the frame (`data = frame`) instead of holding it: native.rds then carries no row through
# its call. `init`: natural-scale starting values, or NULL for flexsurv's own (an explicit NULL
# is refused by flexsurvreg, so the argument is left out).
ps_flexsurvreg <- function(form, anc, frame, dist, init = NULL, control = list(), hessian = FALSE, fixedpars = NULL) {
  env <- new.env(parent = asNamespace("flexsurv"))
  assign("form", form, envir = env)
  assign("anc", anc, envir = env)
  assign("frame", frame, envir = env)
  assign("init", init, envir = env)
  assign("control", control, envir = env)
  assign("fixedpars", fixedpars, envir = env)
  call <- quote(flexsurv::flexsurvreg(form, anc = anc, data = frame, weights = w, dist = DIST, control = control, hessian = HESSIAN))
  call$dist <- dist
  call$hessian <- isTRUE(hessian)
  if (!is.null(init)) call$inits <- quote(init)
  if (!is.null(fixedpars)) call$fixedpars <- quote(fixedpars)
  eval(call, env)
}

# The optimiser's vector (log for the positive parameters) as flexsurvreg's `inits` (natural).
ps_to_natural <- function(m, par) {
  out <- par
  for (j in seq_len(m$nb)) if (m$log[[j]]) out[[j]] <- exp(par[[j]])
  out
}

# The first start (NULL: flexsurv's own) and, for the generalized gamma, the next best one.
ps_starts <- function(prepared, m, distribution, starts, loglik) {
  if (distribution != "gengamma") return(list(first = NULL, second = NULL))
  weibull <- starts$weibull %||% ps_fit(prepared, "weibull")
  lognormal <- starts$lognormal %||% ps_fit(prepared, "lognormal")
  points <- list()
  own <- tryCatch(suppressWarnings(ps_flexsurvreg(ps_formula(prepared), ps_anc_formula(prepared, m), prepared$frame, "gengamma",
                                                   fixedpars = TRUE)), error = function(e) NULL)
  if (!is.null(own)) points$flexsurv <- unname(own$opt$par %||% own$res.t[, "est"])
  n_s <- length(m$i_strata)
  shift <- function(fit) fit$par[c(fit$spec$i_terms, fit$spec$i_strata)]
  if (isTRUE(weibull$converged)) {
    # μ = log scale, log σ = −log shape, Q = 1; the shape's strata turn sign on σ
    points$weibull <- c(weibull$par[[2L]], -weibull$par[[1L]], 1, shift(weibull), -weibull$par[weibull$spec$i_anc])
  }
  if (isTRUE(lognormal$converged)) {
    points$lognormal <- c(lognormal$par[[1L]], lognormal$par[[2L]], 0, shift(lognormal), lognormal$par[lognormal$spec$i_anc])
  }
  points <- Filter(function(x) length(x) == m$nb + length(m$i_terms) + n_s + length(m$i_anc), points)
  if (!length(points)) return(list(first = NULL, second = NULL))
  ll <- vapply(points, function(x) tryCatch(loglik(x), error = function(e) NA_real_), numeric(1))
  ll[!is.finite(ll)] <- -Inf
  by <- order(-ll)
  list(first = points[[by[[1L]]]], second = if (length(by) > 1L) points[[by[[2L]]]] else NULL)
}

ps_formula <- function(prepared) {
  rhs <- c(.ps_cols("d", ncol(prepared$X)), .ps_cols("s", ncol(prepared$S)))
  stats::as.formula(paste(ps_surv(prepared$start), "~", if (length(rhs)) paste(rhs, collapse = " + ") else "1"),
                    env = new.env(parent = asNamespace("survival")))
}

# The response on the positional frame: left truncated at `start` when there is one.
ps_surv <- function(start) if (is.null(start)) "survival::Surv(time, status)" else "survival::Surv(start, time, status)"

ps_anc_formula <- function(prepared, m) {
  if (is.na(m$anc)) return(NULL)
  stats::setNames(list(stats::as.formula(paste("~", paste(.ps_cols("s", ncol(prepared$S)), collapse = " + ")),
                                         env = new.env(parent = asNamespace("survival")))), m$anc_name)
}

# The second start on the optimiser's scale: survreg's estimates for the four it fits (the strata
# with a scale each), the exponential's rate and a flat shape for Gompertz. survreg takes no
# start–stop rows: with a start its estimates are those of the rows observed from 0, a start only.
ps_second_start <- function(prepared, m, distribution, form, frame) {
  n_t <- length(m$i_terms) + length(m$i_strata)
  if (distribution == "gompertz") {
    rate <- sum(prepared$w * prepared$status) / sum(prepared$w * (prepared$time - (prepared$start %||% 0)))
    return(c(0, log(rate), rep(0, n_t + length(m$i_anc))))
  }
  if (distribution == "gengamma") return(NULL)
  sr_dist <- c(weibull = "weibull", exponential = "exponential", lognormal = "lognormal", loglogistic = "loglogistic")[[distribution]]
  rhs <- c(.ps_cols("d", ncol(prepared$X)), .ps_cols("s", ncol(prepared$S)),
           if (length(m$i_anc)) "survival::strata(g)")
  frame$g <- factor(prepared$stratum)
  f <- stats::as.formula(paste("survival::Surv(time, status) ~", if (length(rhs)) paste(rhs, collapse = " + ") else "1"),
                         env = new.env(parent = asNamespace("survival")))
  sr <- tryCatch(suppressWarnings(survival::survreg(f, data = frame, weights = w, dist = sr_dist)), error = function(e) NULL)
  if (is.null(sr) || anyNA(stats::coef(sr))) return(NULL)
  b <- unname(stats::coef(sr))
  scale <- unname(sr$scale)
  if (distribution == "exponential") return(-b)
  if (distribution == "lognormal") return(c(b[[1L]], log(scale[[1L]]), b[-1L], log(scale[-1L]) - log(scale[[1L]])))
  # Weibull and log-logistic: shape = 1/σ, log scale = the intercept
  c(-log(scale[[1L]]), b[[1L]], b[-1L], -(log(scale[-1L]) - log(scale[[1L]])))
}

# Rows fewer than the parameters + 2: nothing is left to tell a fit from the data.
ps_refuse_too_few_rows <- function(prepared, distribution) {
  m <- ps_spec(distribution, ncol(prepared$X), length(prepared$strata_levels))
  k <- m$nb + length(m$i_terms) + length(m$i_strata) + length(m$i_anc)
  n <- length(prepared$time)
  if (n < k + 2L) {
    tab_refuse("too_few_rows", paste0("the model has ", k, " parameters and ", n, " rows it can use: it needs at least ", k + 2L,
                                      " rows (the parameters + 2)"), list(rows = n, parameters = k))
  }
  invisible(TRUE)
}

ps_refuse_not_converged <- function(fitted) {
  if (isTRUE(fitted$converged)) return(invisible(TRUE))
  label <- PS_DISTRIBUTIONS[[fitted$distribution]]$label
  tab_refuse("not_converged", paste0("the fit of the ", label, " distribution did not converge from two starting points: fewer ",
                                     "predictors, merge rare levels, or another distribution"),
             list(distribution = fitted$distribution, reason = fitted$reason %||% ""))
}

# The covariance on the optimiser's scale: the inverse of the Hessian of the runner's
# log-likelihood at `par`, from the rows' own derivatives (ps_structured_derivatives; lot P12),
# Richardson's on all the parameters (numDeriv) only where those fail. NULL when the Hessian is not
# negative definite (the estimates are not a maximum): every standard error and interval is then
# empty. Against Richardson's on the reference tables (six distributions, with strata, with a
# start) the standard errors move by 3e-8 relative at most, but for the log-logistic with strata,
# 9e-7, where the rows' derivatives are the closer to survreg's analytic standard errors (1e-9
# against Richardson's 9e-7).
# The Hessian is taken with the location's and the shape's columns centred (their intercepts at
# the columns' means: par = A · par_c), then brought back exactly, V = A V_c A'. Uncentred, a
# predictor of values far from 0 makes the Hessian ill-conditioned and its numerical error grows
# in the inverse (7e-7 relative against survreg's analytic standard errors on a table of 80 rows
# with a temperature near 20; 1e-9 centred).
ps_covariance <- function(fitted, par = fitted$par) {
  A <- fitted$centring
  # the Hessian from the rows' own derivatives (lot P12: on 5,000 rows and 32 parameters the node's
  # Newton took 31 s with Richardson's, 1.7 s with these); Richardson's only when a row's derivative
  # is not finite or that Hessian is not negative definite
  if (!is.null(fitted$prepared)) {
    dv <- ps_structured_derivatives(fitted, fitted$prepared, par)
    if (!is.null(dv) && all(is.finite(dv$hessian))) {
      info <- -crossprod(A, dv$hessian %*% A)
      ch <- tryCatch(chol((info + t(info)) / 2), error = function(e) NULL)
      if (!is.null(ch)) {
        V <- A %*% chol2inv(ch) %*% t(A)
        V <- (V + t(V)) / 2
        if (all(is.finite(V)) && all(diag(V) > 0)) return(V)
      }
    }
  }
  par_c <- drop(solve(A, par))
  H <- tryCatch(numDeriv::hessian(function(b) fitted$loglik_fn(drop(A %*% b)), par_c), error = function(e) NULL)
  if (is.null(H) || !all(is.finite(H))) return(NULL)
  info <- -(H + t(H)) / 2
  ch <- tryCatch(chol(info), error = function(e) NULL)
  if (is.null(ch)) return(NULL)
  V <- A %*% chol2inv(ch) %*% t(A)
  V <- (V + t(V)) / 2
  if (!all(is.finite(V)) || any(diag(V) <= 0)) return(NULL)
  V
}

# A with par = A · par_c: the identity, but for the location's and the shape's intercepts, which
# take minus each column's mean times its coefficient.
ps_centring <- function(m, X, S) {
  k <- m$nb + length(m$i_terms) + length(m$i_strata) + length(m$i_anc)
  A <- diag(k)
  loc <- c(m$i_terms, m$i_strata)
  if (length(loc)) A[m$loc, loc] <- -colMeans(cbind(X, S))
  if (length(m$i_anc)) A[m$anc, m$i_anc] <- -colMeans(S)
  A
}

# Newton steps on the runner's log-likelihood from flexsurv's estimates, each with the Hessian of
# ps_covariance and numDeriv's Richardson gradient, while a step is at least PS_NEWTON_DONE standard errors and raises the
# log-likelihood (up to its rounding); V is the covariance at the estimates kept. optim's BFGS stops on the function's
# relative change, so its estimates are known to about the square root of that (2.5e-9 relative on
# the exponential's rate; 1e-4 on a Gompertz intercept with a predictor of values near 50):
# the steps bring them to the maximum within the gradient's precision, which the closed forms and
# a second optimiser of the reference values check.
ps_newton <- function(fitted) {
  if (isTRUE(fitted$delayed)) return(ps_newton_stale(fitted))
  par <- fitted$par
  ll <- fitted$loglik_fn(par)
  V <- ps_covariance(fitted, par)
  steps <- 0L
  while (!is.null(V) && steps < PS_NEWTON_STEPS) {
    g <- tryCatch(numDeriv::grad(fitted$loglik_fn, par), error = function(e) NULL)
    if (is.null(g) || !all(is.finite(g))) break
    step <- drop(V %*% g)
    small <- max(abs(step) / sqrt(diag(V))) < PS_NEWTON_DONE
    ll_new <- fitted$loglik_fn(par + step)
    # near the maximum a step changes the log-likelihood by less than its rounding: not lower is enough
    if (!is.finite(ll_new) || ll_new < ll - 1e-12 * max(1, abs(ll))) break
    par <- par + step
    ll <- ll_new
    steps <- steps + 1L
    if (small) break # V at the point before differs by less than the step: kept
    V <- ps_covariance(fitted, par)
  }
  fitted$par <- par
  fitted$loglik <- ll
  fitted$vcov <- V
  fitted$newton_steps <- steps
  fitted
}

# With a start (lot P8) the same, but the steps after a Richardson Hessian reuse it: a Hessian costs
# 2 s on 88,000 rows, a gradient 0.7 s, and four exact steps were needed. A round: V at the point, one
# step, then up to PS_NEWTON_STALE more steps with that V and the gradient of each new point, until
# one is below PS_NEWTON_DONE standard errors; a step that does not raise the log-likelihood ends the
# round. At most PS_NEWTON_STEPS rounds; V at the end is the Hessian of the point kept, taken once
# more only when the last round did not start there.
ps_newton_stale <- function(fitted) {
  par <- fitted$par
  ll <- fitted$loglik_fn(par)
  V <- ps_covariance(fitted, par)
  steps <- 0L
  rounds <- 0L
  fresh <- TRUE # V is the covariance at `par`
  done <- FALSE
  while (!is.null(V) && rounds < PS_NEWTON_STEPS && !done) {
    rounds <- rounds + 1L
    for (k in seq_len(1L + PS_NEWTON_STALE)) {
      g <- tryCatch(numDeriv::grad(fitted$loglik_fn, par), error = function(e) NULL)
      if (is.null(g) || !all(is.finite(g))) {
        done <- TRUE
        break
      }
      step <- drop(V %*% g)
      small <- max(abs(step) / sqrt(diag(V))) < PS_NEWTON_DONE
      ll_new <- fitted$loglik_fn(par + step)
      if (!is.finite(ll_new) || ll_new < ll - 1e-12 * max(1, abs(ll))) {
        done <- k == 1L && fresh # not even a step with the Hessian of here: the maximum, as far as the gradient tells
        break
      }
      par <- par + step
      ll <- ll_new
      steps <- steps + 1L
      fresh <- fresh && small && k == 1L # a first step below the criterion leaves V (at the point before) as it is: kept
      if (small) {
        done <- TRUE
        break
      }
    }
    if (done) break
    V <- ps_covariance(fitted, par)
    fresh <- TRUE
  }
  if (!fresh) V <- ps_covariance(fitted, par)
  fitted$par <- par
  fitted$loglik <- ll
  fitted$vcov <- V
  fitted$newton_steps <- steps
  fitted
}

# ── Newton steps from each row's own parameters (lot P12) ───────────────────
# The fits a check or a test adds (the generalized gamma of distribution_vs_gengamma, the
# exponential's Weibull, the model without predictors and the exponential of the likelihood-ratio
# tests) need their log-likelihood at its maximum, not their covariance: BFGS, then Newton steps
# whose gradient and Hessian come from the likelihood's structure, not from a Richardson Hessian of
# all k parameters (O(k²) evaluations of every row: about 100 s for the generalized gamma of 33
# parameters on 5,000 rows). A row's log-likelihood depends on the parameters only through its nb
# linear predictors η_j (the distribution's own parameters on the optimiser's scale),
# η_j = B_j · par[idx_j], so
#   g = Σ_j B_j' (w ∘ ∂ℓ/∂η_j),   H = Σ_j Σ_l B_j' diag(w ∘ ∂²ℓ/∂η_j ∂η_l) B_l,
# with the rows' derivatives by central differences at two steps and one Richardson extrapolation
# (1 + 4 nb + 4 nb (nb − 1) evaluations of the rows: 37 for three parameters) and O(n k²) to add
# them up, by chunks of rows. A step is taken on the centred
# scale (ps_centring) when −H is positive definite and it raises the log-likelihood, halved up to
# three times; the steps go on while one is at least PS_NEWTON_DONE standard errors, at most
# PS_STEPS_MAX. One step is not enough: from the Weibull's point (Q = 1) BFGS hardly moves the
# generalized gamma (13 evaluations, Q still 0.9999999 on those 236 rows) and the first step leaves
# its LR statistic at 0.6687 against the maximum's 0.6726; the second and third take it there.
PS_STEPS_MAX <- 10L
PS_STEP_H <- 1e-3 # the rows' central differences (then h / 2 and the extrapolation), relative to max(1, |η|)

# The columns of B_j (the design of η_j: a column of ones, then the location's columns and the
# strata's dummies, or the shape's) and where each sits in `par`.
.ps_eta_design <- function(m, X, S) {
  lapply(seq_len(m$nb), function(j) {
    cols <- list(ones = TRUE, X = FALSE, S = FALSE)
    idx <- j
    if (j == m$loc) {
      cols$X <- length(m$i_terms) > 0L
      cols$S <- length(m$i_strata) > 0L
      idx <- c(idx, m$i_terms, m$i_strata)
    } else if (!is.na(m$anc) && j == m$anc && length(m$i_anc)) {
      cols$S <- TRUE
      idx <- c(idx, m$i_anc)
    }
    list(cols = cols, idx = idx)
  })
}

.ps_eta_block <- function(d, X, S, rows) {
  cbind(rep(1, length(rows)), if (d$cols$X) X[rows, , drop = FALSE], if (d$cols$S) S[rows, , drop = FALSE])
}

# Each row's log-likelihood from its linear predictors E (rows × nb), as ps_loglik_rows from par.
ps_loglik_eta <- function(m, E, time, status, start = NULL) {
  pars <- vector("list", m$nb)
  names(pars) <- m$pars
  for (j in seq_len(m$nb)) pars[[j]] <- if (m$log[[j]]) exp(E[, j]) else E[, j]
  out <- ifelse(status == 1L, ps_log_density(m, time, pars), ps_log_survival(m, time, pars))
  if (!is.null(start)) {
    late <- which(start > 0)
    if (length(late)) out[late] <- out[late] - ps_log_survival(m, start[late], lapply(pars, function(p) p[late]))
  }
  out
}

# The gradient and the Hessian of the log-likelihood at `par` (the optimiser's scale), or NULL when
# a row's derivative is not finite.
ps_structured_derivatives <- function(fitted, prepared, par = fitted$par) {
  m <- fitted$spec
  design <- .ps_eta_design(m, prepared$X, prepared$S)
  k <- length(par)
  g <- numeric(k)
  H <- matrix(0, k, k)
  n <- length(prepared$time)
  for (first in seq.int(1L, n, by = PS_CHUNK)) {
    rows <- seq.int(first, min(first + PS_CHUNK - 1L, n))
    B <- lapply(design, function(d) .ps_eta_block(d, prepared$X, prepared$S, rows))
    E <- vapply(seq_len(m$nb), function(j) drop(B[[j]] %*% par[design[[j]]$idx]), numeric(length(rows)))
    if (!is.matrix(E)) E <- matrix(E, nrow = length(rows))
    h0 <- PS_STEP_H * pmax(abs(E), 1) # (pmax keeps the first argument's dimensions)
    start <- if (is.null(prepared$start)) NULL else prepared$start[rows]
    ell <- function(Ex) ps_loglik_eta(m, Ex, prepared$time[rows], prepared$status[rows], start)
    l0 <- ell(E)
    w <- prepared$w[rows]
    # the rows' first and second derivatives by central differences of step h; one Richardson
    # extrapolation from h and h / 2, (4 D(h / 2) − D(h)) / 3, takes their error from O(h²) to O(h⁴)
    differences <- function(h) {
      moved <- function(signs) {
        Ex <- E
        for (j in seq_along(signs)) if (signs[[j]] != 0) Ex[, j] <- Ex[, j] + signs[[j]] * h[, j]
        ell(Ex)
      }
      d1 <- matrix(0, length(rows), m$nb)
      d2 <- array(0, c(length(rows), m$nb, m$nb))
      for (j in seq_len(m$nb)) {
        up <- moved(replace(numeric(m$nb), j, 1))
        down <- moved(replace(numeric(m$nb), j, -1))
        d1[, j] <- (up - down) / (2 * h[, j])
        d2[, j, j] <- (up - 2 * l0 + down) / h[, j]^2
      }
      for (j in seq_len(m$nb)) for (l in seq_len(m$nb)) if (j < l) {
        both <- function(a, b) moved(replace(numeric(m$nb), c(j, l), c(a, b)))
        d2[, j, l] <- d2[, l, j] <- (both(1, 1) - both(1, -1) - both(-1, 1) + both(-1, -1)) / (4 * h[, j] * h[, l])
      }
      list(d1 = d1, d2 = d2)
    }
    coarse <- differences(h0)
    fine <- differences(h0 / 2)
    d1 <- (4 * fine$d1 - coarse$d1) / 3
    d2 <- (4 * fine$d2 - coarse$d2) / 3
    if (!all(is.finite(d1[w > 0, ])) || !all(is.finite(d2[w > 0, , ]))) return(NULL)
    for (j in seq_len(m$nb)) {
      ij <- design[[j]]$idx
      g[ij] <- g[ij] + drop(crossprod(B[[j]], w * d1[, j]))
      for (l in seq_len(m$nb)) {
        il <- design[[l]]$idx
        H[ij, il] <- H[ij, il] + crossprod(B[[j]], (w * d2[, j, l]) * B[[l]])
      }
    }
  }
  list(gradient = g, hessian = (H + t(H)) / 2)
}

# Newton steps of a fit (from ps_fit) to the maximum of its log-likelihood: `loglik` there,
# `newton_steps` the steps taken. No covariance is kept (`vcov` NULL).
ps_newton_steps <- function(fitted, prepared) {
  fitted$newton_steps <- 0L
  if (!isTRUE(fitted$converged)) return(fitted)
  par <- fitted$par
  ll <- fitted$loglik_fn(par)
  A <- fitted$centring
  steps <- 0L
  while (steps < PS_STEPS_MAX) {
    dv <- ps_structured_derivatives(fitted, prepared, par)
    if (is.null(dv)) break
    info <- -crossprod(A, dv$hessian %*% A)
    ch <- tryCatch(chol((info + t(info)) / 2), error = function(e) NULL)
    if (is.null(ch)) break
    V_c <- chol2inv(ch)
    step_c <- drop(V_c %*% crossprod(A, dv$gradient))
    if (!all(is.finite(step_c))) break
    small <- max(abs(step_c) / sqrt(diag(V_c))) < PS_NEWTON_DONE
    step <- drop(A %*% step_c)
    taken <- FALSE
    for (t_ in c(1, 0.5, 0.25, 0.125)) {
      ll_new <- fitted$loglik_fn(par + t_ * step)
      if (is.finite(ll_new) && ll_new >= ll) {
        par <- par + t_ * step
        ll <- ll_new
        taken <- TRUE
        break
      }
    }
    if (!taken) break
    steps <- steps + 1L
    if (small) break
  }
  fitted$par <- par
  fitted$loglik <- ll
  fitted$newton_steps <- steps
  fitted
}

# ── the time of the fits a check or a test adds ──────────────────────────────
# The seconds left before half of the step's time budget has gone (the process's clock; with
# auto, task$.selftest_clock in the self-test), Inf without a budget.
ps_time_left <- function(task) {
  budget <- suppressWarnings(as.numeric(task$limits$time_budget_seconds %||% NA_real_))
  if (length(budget) != 1L || !is.finite(budget) || budget <= 0) return(Inf)
  0.5 * budget - proc.time()[["elapsed"]]
}

# The time a fit besides the node's own may take: what the node's own fit took (with auto, the
# whole choice), at least PS_EXTRA_FIT_MIN_SECONDS so that a small table is never cut short by
# the machine's speed, never past half of the step's budget. Not a threshold of any verdict: it
# decides only whether a number is computed. `task$.selftest_allowance` replaces it in the self-test.
PS_EXTRA_FIT_MIN_SECONDS <- 10
ps_extra_allowance <- function(task, fitted) {
  if (!is.null(task$.selftest_allowance)) return(as.numeric(task$.selftest_allowance))
  min(ps_time_left(task), max(fitted$seconds %||% 0, PS_EXTRA_FIT_MIN_SECONDS))
}

# `expr` with at most `seconds` of elapsed time (R's own limit, checked while R code runs, the
# likelihood included): list(value, time_up). On time_up `value` is NULL. An error that is not the
# time's is raised as it is.
ps_within <- function(seconds, expr) {
  if (!is.finite(seconds)) return(list(value = expr, time_up = FALSE))
  if (seconds <= 0) return(list(value = NULL, time_up = TRUE))
  t0 <- proc.time()[["elapsed"]]
  setTimeLimit(elapsed = seconds, transient = TRUE)
  on.exit(setTimeLimit(elapsed = Inf), add = TRUE)
  value <- tryCatch(expr, error = function(e) e)
  setTimeLimit(elapsed = Inf)
  # the limit raises an error wherever R is: inside the fit (which ps_fit reads as a failed attempt)
  # or here; either way the clock says it
  if (proc.time()[["elapsed"]] - t0 >= seconds) return(list(value = NULL, time_up = TRUE))
  if (inherits(value, "error")) stop(value)
  list(value = value, time_up = FALSE)
}

# The flexsurvreg object for native.rds, with the runner's estimates and covariance in place of
# flexsurv's (summary(fit, newdata = …) reads res.t and res: it then gives the runner's numbers).
ps_native <- function(fitted, alpha) {
  fit <- fitted$fit
  if (is.null(fit)) return(NULL)
  m <- fitted$spec
  par <- stats::setNames(fitted$par, fitted$names)
  z <- stats::qnorm(1 - alpha / 2)
  se <- if (is.null(fitted$vcov)) rep(NA_real_, length(par)) else sqrt(diag(fitted$vcov))
  fit$opt$par <- par
  fit$coefficients <- par
  fit$loglik <- fitted$loglik
  fit$AIC <- -2 * fitted$loglik + 2 * length(par)
  if (!is.null(fitted$vcov)) fit$cov <- matrix(fitted$vcov, length(par), dimnames = list(names(par), names(par)))
  fit$res.t[, "est"] <- par
  fit$res.t[, "se"] <- se
  fit$res.t[, "L95%"] <- par - z * se
  fit$res.t[, "U95%"] <- par + z * se
  nat <- par
  lo <- par - z * se
  hi <- par + z * se
  for (j in seq_len(m$nb)) if (m$log[[j]]) {
    nat[[j]] <- exp(par[[j]])
    lo[[j]] <- exp(lo[[j]])
    hi[[j]] <- exp(hi[[j]])
    se[[j]] <- nat[[j]] * se[[j]]
  }
  fit$res[, "est"] <- nat
  fit$res[, "se"] <- se
  fit$res[, "L95%"] <- lo
  fit$res[, "U95%"] <- hi
  fit$cl <- 1 - alpha
  fit
}

# ── the sections ─────────────────────────────────────────────────────────────
ps_write <- function(task, prepared, fitted, rows, model) {
  m <- fitted$spec
  par <- fitted$par
  V <- fitted$vcov
  alpha <- prepared$alpha
  z <- stats::qnorm(1 - alpha / 2)
  se <- if (is.null(V)) rep(NA_real_, length(par)) else sqrt(diag(V))
  n <- length(prepared$time)
  events <- sum(prepared$status)
  k <- length(par)
  ll <- fitted$loglik
  distribution <- fitted$distribution
  has_predictors <- ncol(prepared$X) > 0L
  # a strata column of one level is no strata: one shape, the Weibull's hazard ratios stand
  has_strata <- length(prepared$strata_levels) > 1L

  # ── the model section ─────────────────────────────────────────────────────
  tab_text(rows, "model", "formula", ps_formula_text(prepared))
  tab_text(rows, "model", "software", tab_software(PS_PACKAGES))
  tab_text(rows, "model", "event_value", prepared$value_label)
  tab_text(rows, "model", "distribution", distribution)
  tab_text(rows, "model", "selection", model$selection)
  tab_text(rows, "model", "coefficient_scale", model$coefficient_scale)
  if (has_predictors) tab_text(rows, "model", "profile", prepared$profile_text)
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "events", events)
  tab_number(rows, "model", "censored", n - events)
  tab_number(rows, "model", "n_missing", prepared$n_missing)
  tab_number(rows, "model", "n_zero_time", prepared$n_zero_time)
  if (!is.null(prepared$start)) tab_number(rows, "model", "n_delayed", sum(prepared$start > 0))
  tab_number(rows, "model", "n_strata", length(prepared$strata_levels))
  tab_number(rows, "model", "n_parameters", k)
  tab_number(rows, "model", "log_lik", ll)
  tab_number(rows, "model", "aic", -2 * ll + 2 * k)
  tab_number(rows, "model", "bic", -2 * ll + k * log(n))
  # the two likelihood-ratio tests fit a model more each, at its maximum (BFGS and one Newton
  # step), within the time ps_extra_allowance gives it; out of time, its rows are not written and a
  # note says so
  out_of_time <- character()
  nested_fit <- function(p, d, what) {
    run <- ps_within(ps_extra_allowance(task, fitted), {
      f <- ps_fit(p, d)
      if (isTRUE(f$converged)) ps_newton_steps(f, p) else f
    })
    if (run$time_up) out_of_time <<- c(out_of_time, what)
    run$value
  }
  if (has_predictors) {
    without <- ps_without_predictors(prepared)
    null <- nested_fit(without, distribution, "predictors")
    if (isTRUE(null$converged)) {
      lr <- 2 * (ll - null$loglik)
      tab_number(rows, "model", "lr_statistic", lr)
      tab_number(rows, "model", "lr_df", ncol(prepared$X))
      tab_p(rows, "model", stats::pchisq(lr, ncol(prepared$X), lower.tail = FALSE), stat = "lr_p_value")
    }
  }
  if (distribution %in% c("weibull", "gompertz")) {
    # the shape against the exponential's constant hazard: a number of the fit, not a check
    # with auto the exponential is a candidate already fitted: only its Newton step is taken
    ex <- if (isTRUE(fitted$candidates$exponential$converged)) ps_newton_steps(fitted$candidates$exponential, prepared) else
      nested_fit(prepared, "exponential", "exponential")
    if (isTRUE(ex$converged)) {
      lr <- 2 * (ll - ex$loglik)
      df_ <- k - ex$n_parameters
      tab_number(rows, "model", "exponential_lr_statistic", lr)
      tab_number(rows, "model", "exponential_lr_df", df_)
      tab_p(rows, "model", stats::pchisq(lr, df_, lower.tail = FALSE), stat = "exponential_lr_p_value")
    }
  }
  if (has_predictors) {
    # Harrell's C of the location's linear predictor within strata, turned where a larger one is a
    # shorter time (a rate: exponential, Gompertz)
    lp <- ps_linear(m, par, prepared$X, prepared$S)
    score <- if (m$aft > 0) lp else -lp
    cf <- data.frame(time = prepared$time, status = prepared$status, score = score, g = prepared$stratum, w = prepared$w)
    if (!is.null(prepared$start)) cf$start <- prepared$start
    form <- stats::as.formula(paste(sub("survival::", "", ps_surv(prepared$start)), "~ score + strata(g)"),
                              env = new.env(parent = asNamespace("survival")))
    cc <- tryCatch(survival::concordance(form, data = cf, weights = w), error = function(e) NULL)
    if (!is.null(cc)) {
      tab_number(rows, "model", "concordance", cc$concordance)
      tab_number(rows, "model", "concordance_se", sqrt(cc$var))
    }
  }
  tab_number(rows, "model", "last_time", max(prepared$time))
  tab_number(rows, "model", "last_event_time", max(prepared$time[prepared$status == 1L]))

  # ── coefficients ──────────────────────────────────────────────────────────
  sign <- if (m$aft < 0) -1 else 1
  hr_kind <- ps_hazard_ratio_kind(distribution, has_strata)
  cox <- if (!is.na(hr_kind) && has_predictors) ps_cox(prepared, alpha) else NULL
  vif <- if (has_predictors) cox_vif(prepared$X, prepared$design$terms) else NULL
  terms <- prepared$design$terms
  interval_disagrees <- FALSE
  write_coef <- function(level, level_2, i, intercept) {
    est <- sign * par[[i]]
    s <- se[[i]]
    zz <- est / s
    p_ <- 2 * stats::pnorm(-abs(zz))
    tab_number(rows, "coef", "estimate", est, level, level_2)
    tab_number(rows, "coef", "std_error", s, level, level_2)
    tab_number(rows, "coef", "statistic", zz, level, level_2)
    tab_p(rows, "coef", p_, level = level, level_2 = level_2)
    if (intercept) return(invisible(NULL))
    if (m$aft != 0) {
      tab_number(rows, "coef", "time_ratio", exp(est), level, level_2)
      tab_number(rows, "coef", "tr_ci_low", exp(est - z * s), level, level_2)
      tab_number(rows, "coef", "tr_ci_high", exp(est + z * s), level, level_2)
    }
    if (!is.na(hr_kind)) {
      if (hr_kind == "weibull") {
        # log HR = −β · shape = −β · exp(log shape): its gradient on (log shape, β), the delta method
        shape <- exp(par[[1L]])
        log_hr <- -par[[i]] * shape
        g <- c(-par[[i]] * shape, -shape)
        s_hr <- if (is.null(V)) NA_real_ else sqrt(drop(t(g) %*% V[c(1L, i), c(1L, i)] %*% g))
        lo <- exp(log_hr - z * s_hr)
        hi <- exp(log_hr + z * s_hr)
        if (is.finite(p_) && is.finite(lo) && ((p_ < alpha) != (lo > 1 || hi < 1))) interval_disagrees <<- TRUE
      } else {
        log_hr <- par[[i]] # the exponential's and Gompertz's coefficients are on the log rate
        lo <- exp(log_hr - z * s)
        hi <- exp(log_hr + z * s)
      }
      tab_number(rows, "coef", "hazard_ratio", exp(log_hr), level, level_2)
      tab_number(rows, "coef", "hr_ci_low", lo, level, level_2)
      tab_number(rows, "coef", "hr_ci_high", hi, level, level_2)
      if (!is.null(cox)) {
        j <- i - m$nb
        tab_number(rows, "coef", "cox_hazard_ratio", cox$hr[[j]], level, level_2)
        tab_number(rows, "coef", "cox_hr_ci_low", cox$low[[j]], level, level_2)
        tab_number(rows, "coef", "cox_hr_ci_high", cox$high[[j]], level, level_2)
      }
    }
    invisible(NULL)
  }
  write_coef("(Intercept)", NULL, m$loc, TRUE)
  for (j in seq_len(ncol(prepared$X))) {
    level_2 <- if (is.na(terms$level[[j]])) NULL else terms$level[[j]]
    write_coef(terms$term[[j]], level_2, m$i_terms[[j]], FALSE)
    at <- match(terms$term_index[[j]], vif$term_index)
    if (!is.null(vif) && !is.na(at)) tab_number(rows, "coef", "vif", vif$value[[at]], terms$term[[j]], level_2)
  }

  # ── the distribution's parameters other than the location, per stratum ───
  for (j in seq_len(m$nb)) {
    if (j == m$loc) next
    name <- m$pars[[j]]
    per <- if (!is.na(m$anc) && j == m$anc) seq_along(prepared$strata_levels) else 1L
    for (s in per) {
      idx <- c(j, if (s > 1L) m$i_anc[[s - 1L]])
      value <- sum(par[idx])
      sv <- if (is.null(V)) NA_real_ else sqrt(sum(V[idx, idx]))
      level_2 <- if (length(per) > 1L) prepared$strata_names[[s]] else NULL
      if (m$log[[j]]) {
        tab_number(rows, "parameter", "estimate", exp(value), name, level_2)
        tab_number(rows, "parameter", "std_error", exp(value) * sv, name, level_2)
        tab_number(rows, "parameter", "ci_low", exp(value - z * sv), name, level_2)
        tab_number(rows, "parameter", "ci_high", exp(value + z * sv), name, level_2)
      } else {
        tab_number(rows, "parameter", "estimate", value, name, level_2)
        tab_number(rows, "parameter", "std_error", sv, name, level_2)
        tab_number(rows, "parameter", "ci_low", value - z * sv, name, level_2)
        tab_number(rows, "parameter", "ci_high", value + z * sv, name, level_2)
      }
    }
  }

  # ── the profile, per stratum ──────────────────────────────────────────────
  profile <- ps_profile(model, prepared)
  infinite <- 0
  for (s in seq_along(prepared$strata_levels)) {
    name <- prepared$strata_names[[s]]
    in_s <- prepared$stratum == s
    tab_number(rows, "stratum", "n", sum(in_s), level = name)
    tab_number(rows, "stratum", "events", sum(prepared$status[in_s]), level = name)
    tab_number(rows, "stratum", "last_time", max(prepared$time[in_s]), level = name)
    tab_number(rows, "stratum", "median", profile$median[[s]], level = name)
    tab_number(rows, "stratum", "median_ci_low", profile$median_low[[s]], level = name)
    tab_number(rows, "stratum", "median_ci_high", profile$median_high[[s]], level = name)
    for (k_ in seq_along(prepared$quantiles)) {
      stat <- paste0("quantile_", km_time_label(prepared$quantiles[[k_]]))
      x0 <- prepared$quantiles[[k_]]
      tab_add(rows, "stratum", stat, value = profile$quantile[s, k_], level = name, x0 = x0)
      tab_add(rows, "stratum", paste0(stat, "_ci_low"), value = profile$quantile_low[s, k_], level = name, x0 = x0)
      tab_add(rows, "stratum", paste0(stat, "_ci_high"), value = profile$quantile_high[s, k_], level = name, x0 = x0)
    }
    for (k_ in seq_along(prepared$times)) {
      stat <- paste0("surv_at_", km_time_label(prepared$times[[k_]]))
      x0 <- prepared$times[[k_]]
      tab_add(rows, "stratum", stat, value = profile$surv[s, k_], level = name, x0 = x0)
      tab_add(rows, "stratum", paste0(stat, "_low"), value = profile$surv_low[s, k_], level = name, x0 = x0)
      tab_add(rows, "stratum", paste0(stat, "_high"), value = profile$surv_high[s, k_], level = name, x0 = x0)
    }
    km <- if (is.null(prepared$start)) {
      survival::survfit(survival::Surv(prepared$time[in_s], prepared$status[in_s]) ~ 1, weights = prepared$w[in_s])
    } else {
      survival::survfit(survival::Surv(prepared$start[in_s], prepared$time[in_s], prepared$status[in_s]) ~ 1, weights = prepared$w[in_s])
    }
    tab_number(rows, "stratum", "km_median", unname(summary(km)$table[["median"]]), level = name)
    if (!is.finite(profile$median[[s]]) || any(!is.finite(profile$quantile[s, ]))) infinite <- max(infinite, profile$cured[[s]])
  }

  # ── notes ─────────────────────────────────────────────────────────────────
  if (prepared$n_zero_time > 0) {
    tab_text(rows, "model", "note_zero_times", paste0(
      prepared$n_zero_time, " rows have a time of 0 and are left out: a parametric distribution gives an event at 0 no density; ",
      "if those events are real, count time from a moment before them."))
  }
  if (prepared$n_zero_weight > 0) {
    tab_text(rows, "model", "note_zero_weights", paste0(prepared$n_zero_weight, " rows have a weight of 0 and are left out."))
  }
  if (!is.null(prepared$weights_col) && any(prepared$w != round(prepared$w))) tab_text(rows, "model", "note_weights_counts", PS_NOTE_WEIGHTS_COUNTS)
  if (fitted$attempts > 1L) {
    tab_text(rows, "model", "note_second_start", "@second_start")
  }
  # the likelihood-ratio tests not made for want of time
  if ("predictors" %in% out_of_time) tab_text(rows, "model", "note_lr_time", "@lr_time")
  if ("exponential" %in% out_of_time) tab_text(rows, "model", "note_exponential_lr_time", "@exponential_lr_time")
  # delayed entry: said whenever there is a start column, with the rows that enter after 0
  if (!is.null(prepared$start)) tab_text(rows, "model", "note_start_rows", paste0("@start_rows delayed=", sum(prepared$start > 0), " rows=", n))
  if (distribution == "weibull" && has_strata && has_predictors) tab_text(rows, "model", "note_strata_no_hr", PS_NOTE_STRATA_NO_HR)
  if (interval_disagrees) tab_text(rows, "model", "note_hr_interval_scale", PS_NOTE_HR_INTERVAL)
  if (infinite > 0) {
    tab_text(rows, "model", "note_quantile_infinite", paste0("@quantile_infinite share=", format(round(100 * infinite, 1), nsmall = 1)))
  }
  invisible(rows)
}

# Which hazard ratio a distribution has: the exponential's and Gompertz's exp(β) on the rate; the
# Weibull's exp(−β · shape) only without strata (with a shape per stratum it differs by stratum).
ps_hazard_ratio_kind <- function(distribution, has_strata) {
  switch(distribution, exponential = "rate", gompertz = "rate", weibull = if (has_strata) NA_character_ else "weibull",
         NA_character_)
}

# The same rows without predictors (the strata kept): the null fit of the likelihood-ratio test.
ps_without_predictors <- function(prepared) {
  out <- prepared
  out$X <- prepared$X[, 0L, drop = FALSE]
  out$frame <- prepared$frame[, !grepl("^d[0-9]+$", names(prepared$frame)), drop = FALSE]
  out
}

# The Cox node's hazard ratios on the same rows, weights and strata (Efron's ties).
ps_cox <- function(prepared, alpha) {
  frame <- prepared$frame
  frame$g <- factor(prepared$stratum)
  rhs <- c(.ps_cols("d", ncol(prepared$X)), if (length(prepared$strata_levels) > 1L) "survival::strata(g)")
  f <- stats::as.formula(paste(ps_surv(prepared$start), "~", paste(rhs, collapse = " + ")),
                         env = new.env(parent = asNamespace("survival")))
  fit <- tryCatch(suppressWarnings(survival::coxph(f, data = frame, weights = w, ties = "efron")), error = function(e) NULL)
  if (is.null(fit)) return(NULL)
  ci <- summary(fit, conf.int = 1 - alpha)$conf.int
  list(hr = unname(ci[, 1L]), low = unname(ci[, 3L]), high = unname(ci[, 4L]))
}

# «Surv(days, status = failed) ~ load + line + strata(plant)»: for people, never parsed.
ps_formula_text <- function(prepared) {
  rhs <- c(prepared$predictors, vapply(prepared$interactions, function(x) paste(x[[1L]], x[[2L]], sep = " × "), ""),
           if (!is.null(prepared$strata_col)) paste0("strata(", prepared$strata_col, ")"))
  paste0("Surv(", if (!is.null(prepared$start_col)) paste0(prepared$start_col, ", ") else "", prepared$time_col, ", ",
         prepared$event_col, " = ", prepared$value_label, ") ~ ",
         if (length(rhs)) paste(rhs, collapse = " + ") else "1")
}

# The profile's median, quantiles and survival with their intervals, one row per stratum.
ps_profile <- function(model, prepared) {
  L <- length(prepared$strata_levels)
  X <- if (length(prepared$profile_row)) {
    one <- as.data.frame(prepared$profile_row, check.names = FALSE, stringsAsFactors = FALSE)
    tab_design_rows(model$card, one)$X[rep(1L, L), , drop = FALSE]
  } else matrix(numeric(), L, 0L)
  pr <- ps_predict(model, X, seq_len(L), prepared$quantiles, prepared$times, interval = TRUE)
  pars <- ps_natural(model$spec, model$par, X, ps_strata_dummies(seq_len(L), L))
  cured <- exp(ps_log_survival(model$spec, rep(Inf, L), pars)) # the share never reaching the event (Gompertz, shape < 0)
  list(median = pr$median, median_low = pr$median_low, median_high = pr$median_high, quantile = pr$quantile,
       quantile_low = pr$quantile_low, quantile_high = pr$quantile_high, surv = pr$surv, surv_low = pr$surv_low,
       surv_high = pr$surv_high, cured = cured)
}

# ── the card ─────────────────────────────────────────────────────────────────
ps_card <- function(task, prepared, fitted, selection = "manual") {
  m <- fitted$spec
  card <- tab_card(task, prepared$design, PS_PACKAGES)
  card$formula <- ps_formula_text(prepared)
  card$outcome <- list(name = prepared$event_col, event_value = prepared$value_label)
  card$coefficients <- as.list(fitted$par)
  card$vcov <- if (is.null(fitted$vcov)) NULL else tab_card_vcov(task, unname(fitted$vcov[, , drop = FALSE]))
  card$alpha <- prepared$alpha
  levels <- prepared$strata_levels
  strata <- NULL
  if (!is.null(prepared$strata_col)) {
    card$inputs[[length(card$inputs) + 1L]] <- list(name = prepared$strata_col, type = "category", levels = as.list(levels),
                                                    reference = levels[[1L]])
    strata <- list(column = prepared$strata_col, levels = as.list(levels))
  }
  order <- c(lapply(m$pars, function(p) list(role = "parameter", name = p)),
             lapply(seq_along(m$i_terms), function(j) list(role = "location", term = j)),
             lapply(seq_along(m$i_strata), function(j) list(role = "location", stratum = levels[[j + 1L]])),
             lapply(seq_along(m$i_anc), function(j) list(role = "anc", name = m$anc_name, stratum = levels[[j + 1L]])))
  last_by <- NULL
  if (!is.null(strata)) {
    last_by <- stats::setNames(lapply(seq_along(levels), function(s) max(prepared$time[prepared$stratum == s])), levels)
  }
  card$parametric_survival <- list(
    distribution = fitted$distribution, selection = selection,
    coefficient_scale = if (m$aft == 0) "log_hazard_ratio" else "log_time_ratio",
    parameters = as.list(m$pars), location = m$location,
    transforms = as.list(ifelse(m$log, "log", "identity")), order = order, strata = strata,
    time_column = prepared$time_col, event_column = prepared$event_col, event_value = prepared$value_label,
    start_column = prepared$start_col %||% NULL,
    last_time = max(prepared$time), last_time_by_stratum = last_by,
    quantiles = as.list(prepared$quantiles), times = as.list(prepared$times), alpha = prepared$alpha,
    flexsurv = as.character(utils::packageVersion("flexsurv")))
  card
}

# d1, d2…: the positional names of k columns (none for k = 0).
.ps_cols <- function(prefix, k) if (k > 0L) paste0(prefix, seq_len(k)) else character()

# quantile_0.25, survival_at_30…: a column or a stat per number, as km_time_label writes it (none
# for no number: paste0 alone would give the prefix).
.ps_names <- function(prefix, values) if (length(values)) paste0(prefix, vapply(values, km_time_label, "")) else character()

.ps_num <- function(x) vapply(x, function(v) if (is.null(v)) NA_real_ else as.numeric(v), numeric(1))

# The model a card holds, as ps_predict reads it: the card's numbers only. A card that does not
# hold together (altered by hand, or not this node's) is refused with a sentence, never an R error.
ps_model <- function(card) {
  bad <- function(what) {
    tab_refuse("card_invalid", paste0("the model card is not a parametric survival model's as the node writes it: ", what,
                                      "; fit the model again"), list(problem = what))
  }
  ps <- card$parametric_survival
  if (!is.list(ps)) bad("it has no «parametric_survival» part")
  distribution <- ps$distribution
  if (!is.character(distribution) || length(distribution) != 1L || !distribution %in% names(PS_DISTRIBUTIONS)) {
    bad(paste0("its distribution «", paste(unlist(distribution), collapse = ", "), "» is none of ", paste(names(PS_DISTRIBUTIONS), collapse = ", ")))
  }
  levels <- if (is.null(ps$strata)) PS_ALL else vapply(ps$strata$levels, function(x) as.character(x %||% NA_character_), "")
  if (!length(levels) || anyNA(levels)) bad("its strata have no levels")
  order <- ps$order
  if (!is.list(order)) bad("it has no order of its coefficients")
  n_terms <- sum(vapply(order, function(o) is.list(o) && identical(o$role, "location") && !is.null(o$term), logical(1)))
  m <- ps_spec(distribution, n_terms, length(levels))
  par <- tryCatch(.ps_num(card$coefficients), error = function(e) NA_real_)
  k <- m$nb + n_terms + length(m$i_strata) + length(m$i_anc)
  if (length(par) != length(order) || length(par) != k) bad(paste0("it has ", length(par), " coefficients where its order says ", k))
  if (!all(is.finite(par))) bad("a coefficient is empty or not finite")
  V <- NULL
  if (length(card$vcov)) {
    rows_v <- tryCatch(lapply(card$vcov, .ps_num), error = function(e) NULL)
    V <- if (length(rows_v) == k && all(lengths(rows_v) == k)) do.call(rbind, rows_v) else NULL
    if (!is.matrix(V)) bad(paste0("its covariance is not ", k, " × ", k, ", one row and column per coefficient"))
    if (!all(is.finite(V))) bad("its covariance has an empty or infinite number")
  }
  alpha <- suppressWarnings(as.numeric(ps$alpha %||% card$alpha %||% 0.05))
  if (length(alpha) != 1L || !is.finite(alpha) || alpha <= 0 || alpha >= 1) bad("its alpha is not a number between 0 and 1")
  plain <- card
  plain$params$intercept <- FALSE # the distribution's location takes the intercept
  list(card = plain, spec = m, par = par, vcov = V, alpha = alpha,
       strata_col = ps$strata$column %||% NULL, strata_levels = levels, selection = ps$selection %||% "manual",
       coefficient_scale = ps$coefficient_scale, quantiles = .ps_num(ps$quantiles), times = .ps_num(ps$times),
       last_time = as.numeric(ps$last_time), last_time_by_stratum = ps$last_time_by_stratum)
}

# The design of rows for a card's model, and each row's stratum (NA: a stratum the fit did not
# see, or none).
ps_design <- function(model, df) {
  d <- tab_design_rows(model$card, df)
  stratum <- rep(1L, nrow(df))
  if (!is.null(model$strata_col)) {
    if (!model$strata_col %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows have no column «", model$strata_col, "», the strata the model was fitted on"),
                 list(column = model$strata_col))
    }
    labels <- tab_label(df[[model$strata_col]])
    stratum <- match(labels, model$strata_levels)
    d$unseen <- d$unseen + sum(!is.na(labels) & is.na(stratum))
  }
  d$stratum <- stratum
  d$ok <- d$ok & !is.na(stratum)
  d
}

# ── conditional survival's column ────────────────────────────────────────────
# The time each row has already survived (`alive_at`, lot P9): a numeric column of the rows, empty
# where it is unknown. NULL when the node or Predict does not name one. A negative, empty or
# infinite value is the row's, not the table's: ps_predict gives NA there and Predict counts them.
ps_alive_column <- function(df, column) {
  if (is.null(column) || !length(column) || (is.character(column) && !nzchar(column))) return(NULL)
  if (!column %in% names(df)) {
    tab_refuse("missing_column", paste0("the rows have no column «", column, "», the time already survived (alive_at)"),
               list(column = column))
  }
  kind <- tab_kind(df[[column]])
  if (!kind %in% c("number", "integer")) {
    tab_refuse("alive_at_type", paste0("the column «", column, "» is a ", kind, ": the time already survived (alive_at) must be a number, ",
                                       "in the unit of the model's times"), list(column = column))
  }
  as.numeric(df[[column]])
}

# ── predictions ──────────────────────────────────────────────────────────────
# For rows of the design X (the location's columns, as the card's terms) in strata `strata`
# (indices of model$strata_levels, NA unknown): the median, the quantiles and the survival at the
# times, with their intervals at the card's 1 − alpha when `interval` and the card has a vcov; the
# hazard at `hazard_times` (the hazard chart, P2) likewise. A row with an empty value or an
# unknown stratum gets NA everywhere.
#
# alive_at (lot P9, conditional survival): for each row the time a ≥ 0 it has already survived
# without the event (NULL: from the start). The answers are then about the time left: the median
# and the quantiles q of S(a + q) = (1 − p) S(a), i.e. Q(1 − (1 − p) S(a)) − a, and the survival at t
# as S(a + t) / S(a), the chance of lasting t more; the intervals by the same delta method, on
# log q and on log(−log S) as ever (the gradient goes through S(a) too, as it depends on the
# parameters). A row whose a is empty, negative or not finite, or whose S(a) is 0 (no one lasts
# that long under the model: the condition has no meaning), gets NA, counted in `alive_empty` and
# `alive_dead`. The hazard at `hazard_times` is the unconditional one.
ps_predict <- function(model, X, strata, quantiles = numeric(), times = numeric(), interval = FALSE, alive_at = NULL,
                       hazard_times = numeric()) {
  m <- model$spec
  par <- model$par
  n <- nrow(X)
  ok <- !is.na(strata) & apply(is.finite(cbind(X, 0)), 1L, all)
  alive_empty <- alive_dead <- rep(FALSE, n)
  usable <- ok # the rows the design and the stratum allow: the linear predictor does not depend on alive_at
  if (!is.null(alive_at)) {
    alive_at <- as.numeric(alive_at)
    if (length(alive_at) != n) stop("alive_at has ", length(alive_at), " values for ", n, " rows")
    alive_empty <- ok & (!is.finite(alive_at) | alive_at < 0)
    ok <- ok & !alive_empty
    rows_ <- which(ok)
    if (length(rows_)) {
      S0 <- ps_strata_dummies(strata, length(model$strata_levels))
      ls_a <- ps_log_survival(m, alive_at[rows_], ps_natural(m, par, X[rows_, , drop = FALSE], S0[rows_, , drop = FALSE]))
      alive_dead[rows_] <- !is.finite(ls_a) | exp(ls_a) == 0
      ok <- ok & !alive_dead
    }
  }
  S <- ps_strata_dummies(strata, length(model$strata_levels))
  V <- if (isTRUE(interval)) model$vcov else NULL
  z <- stats::qnorm(1 - model$alpha / 2)
  probs <- c(0.5, quantiles)
  na_matrix <- function(k) matrix(NA_real_, n, k)
  out <- list(linear = rep(NA_real_, n), ok = ok, quantile_all = na_matrix(length(probs)), quantile_all_low = na_matrix(length(probs)),
              quantile_all_high = na_matrix(length(probs)), surv = na_matrix(length(times)), surv_low = na_matrix(length(times)),
              surv_high = na_matrix(length(times)), hazard = na_matrix(length(hazard_times)),
              hazard_low = na_matrix(length(hazard_times)), hazard_high = na_matrix(length(hazard_times)))
  out$linear[usable] <- ps_linear(m, par, X[usable, , drop = FALSE], S[usable, , drop = FALSE])
  idx <- which(ok)
  for (start in seq.int(1L, max(length(idx), 1L), by = PS_CHUNK)) {
    rows_ <- idx[seq.int(start, min(start + PS_CHUNK - 1L, length(idx)))]
    if (!length(rows_) || anyNA(rows_)) break
    Xc <- X[rows_, , drop = FALSE]
    Sc <- S[rows_, , drop = FALSE]
    if (is.null(alive_at)) {
      log_q <- function(p_) function(b) log(ps_quantile(m, p_, ps_natural(m, b, Xc, Sc)))
      log_cumhaz <- function(t) function(b) log(-ps_log_survival(m, rep(t, length(rows_)), ps_natural(m, b, Xc, Sc)))
    } else {
      a_c <- alive_at[rows_]
      # the time left at probability p: the unconditional quantile at 1 − (1 − p) S(a), less a
      log_q <- function(p_) function(b) {
        pars <- ps_natural(m, b, Xc, Sc)
        up_to <- -expm1(log1p(-p_) + ps_log_survival(m, a_c, pars))
        log(ps_quantile(m, up_to, pars) - a_c)
      }
      # the cumulative hazard accrued between a and a + t: S(a + t) / S(a) = exp(−that)
      log_cumhaz <- function(t) function(b) {
        pars <- ps_natural(m, b, Xc, Sc)
        # (a difference below rounding, a cure far on: no hazard accrued, S stays at 1)
        log(pmax(ps_log_survival(m, a_c, pars) - ps_log_survival(m, a_c + t, pars), 0))
      }
    }
    log_hazard <- function(t) function(b) {
      pars <- ps_natural(m, b, Xc, Sc)
      tt <- rep(t, length(rows_))
      ps_log_density(m, tt, pars) - ps_log_survival(m, tt, pars)
    }
    for (k in seq_along(probs)) {
      f <- log_q(probs[[k]])
      v <- f(par)
      out$quantile_all[rows_, k] <- exp(v)
      if (!is.null(V)) {
        s <- ps_delta_se(f, par, V, v)
        out$quantile_all_low[rows_, k] <- exp(v - z * s)
        out$quantile_all_high[rows_, k] <- exp(v + z * s)
      }
    }
    for (k in seq_along(times)) {
      f <- log_cumhaz(times[[k]])
      v <- f(par)
      surv <- exp(-exp(v))
      out$surv[rows_, k] <- surv
      if (!is.null(V)) {
        s <- ps_delta_se(f, par, V, v)
        flat <- !is.finite(v) # S = 1 at time 0 (or 0 at the end): no interval to spread
        out$surv_low[rows_, k] <- ifelse(flat, surv, exp(-exp(v + z * s)))
        out$surv_high[rows_, k] <- ifelse(flat, surv, exp(-exp(v - z * s)))
      }
    }
    for (k in seq_along(hazard_times)) {
      f <- log_hazard(hazard_times[[k]])
      v <- f(par)
      out$hazard[rows_, k] <- exp(v)
      if (!is.null(V)) {
        s <- ps_delta_se(f, par, V, v)
        out$hazard_low[rows_, k] <- exp(v - z * s)
        out$hazard_high[rows_, k] <- exp(v + z * s)
      }
    }
  }
  # a quantile the distribution never reaches (a share of the units never has the event) is empty, not infinite
  for (k in c("quantile_all", "quantile_all_low", "quantile_all_high")) out[[k]][!is.finite(out[[k]])] <- NA_real_
  out$median <- out$quantile_all[, 1L]
  out$median_low <- out$quantile_all_low[, 1L]
  out$median_high <- out$quantile_all_high[, 1L]
  keep <- seq_along(quantiles) + 1L
  out$quantile <- out$quantile_all[, keep, drop = FALSE]
  out$quantile_low <- out$quantile_all_low[, keep, drop = FALSE]
  out$quantile_high <- out$quantile_all_high[, keep, drop = FALSE]
  out$intervals <- !is.null(V)
  out$alive_empty <- alive_empty
  out$alive_dead <- alive_dead
  out
}

# The delta method's standard error of f(par) for each row: the gradient by numDeriv's
# Richardson extrapolation, the quadratic form g' V g summed column by column (no BLAS: a row's
# number does not depend on the rows beside it). NA where f is not finite.
ps_delta_se <- function(f, par, V, value) {
  G <- suppressWarnings(numDeriv::jacobian(f, par))
  if (!is.matrix(G)) G <- matrix(G, nrow = length(value))
  k <- ncol(G)
  total <- numeric(nrow(G))
  for (j in seq_len(k)) {
    a <- numeric(nrow(G))
    for (l in seq_len(k)) a <- a + G[, l] * V[l, j]
    total <- total + a * G[, j]
  }
  out <- sqrt(total)
  out[!is.finite(value) | !is.finite(out)] <- NA_real_
  out
}

# ── the rows output ──────────────────────────────────────────────────────────
# The input's rows with the model's columns, from the card (Predict on the same rows gives the
# same numbers to the bit): the location's linear predictor, the median, the quantiles and the
# survival at the node's times with their intervals, and on the rows of the fit the Cox–Snell,
# martingale and deviance residuals.
ps_rows_output <- function(df, model, prepared, fitted) {
  alive_at <- ps_alive_column(df, prepared$p$alive_at)
  q_names <- .ps_names("pred_quantile_", model$quantiles)
  s_names <- .ps_names("pred_surv_at_", model$times)
  with_bounds <- function(x) c(x, if (length(x) && !is.null(model$vcov)) c(paste0(x, "_low"), paste0(x, "_high")))
  made <- c("pred_linear", with_bounds("pred_median"), with_bounds(q_names), with_bounds(s_names), "pred_cox_snell",
            "pred_martingale", "pred_deviance")
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- ps_design(model, df)
  pr <- ps_predict(model, d$X, ifelse(d$ok, d$stratum, NA_integer_), model$quantiles, model$times, interval = TRUE,
                   alive_at = alive_at)
  out <- df
  out$pred_linear <- pr$linear
  put <- function(name, v, lo, hi) {
    out[[name]] <<- v
    if (pr$intervals) {
      out[[paste0(name, "_low")]] <<- lo
      out[[paste0(name, "_high")]] <<- hi
    }
  }
  put("pred_median", pr$median, pr$median_low, pr$median_high)
  for (k in seq_along(q_names)) put(q_names[[k]], pr$quantile[, k], pr$quantile_low[, k], pr$quantile_high[, k])
  for (k in seq_along(s_names)) put(s_names[[k]], pr$surv[, k], pr$surv_low[, k], pr$surv_high[, k])
  # residuals on the rows of the fit: r = H(t | x), m = δ − r, the deviance residual
  m <- fitted$spec
  pars <- ps_natural(m, fitted$par, prepared$X, prepared$S)
  r <- -ps_log_survival(m, prepared$time, pars)
  # with a start the hazard that counts is the one accrued while the row was observed, H(time) − H(start)
  accrued <- if (is.null(prepared$start)) r else r + ifelse(prepared$start == 0, 0, ps_log_survival(m, prepared$start, pars))
  mart <- prepared$status - accrued
  dev <- sign(mart) * sqrt(-2 * (mart + ifelse(prepared$status == 1L, log(prepared$status - mart), 0)))
  for (name in c("pred_cox_snell", "pred_martingale", "pred_deviance")) out[[name]] <- NA_real_
  out$pred_cox_snell[prepared$rows] <- r
  out$pred_martingale[prepared$rows] <- mart
  out$pred_deviance[prepared$rows] <- dev
  rownames(out) <- NULL
  out
}

# ── Predict: the median time, the quantiles and the survival at the times ────
predict_parametric_survival <- function(task, card) {
  df <- tab_input(task)
  model <- ps_model(card)
  given <- function(x) length(unlist(x)) > 0L
  quantiles <- if (given(task$params$quantiles)) ps_quantiles(task$params$quantiles) else model$quantiles
  times <- if (given(task$params$at_times)) sort(unique(as.numeric(unlist(task$params$at_times)))) else model$times
  interval <- isTRUE(task$params$interval)
  if (interval && is.null(model$vcov)) {
    # two reasons for a card without a covariance: too many parameters to keep it, or none to keep
    # (the fit's Hessian was not positive definite: its report says it under «Convergence»)
    if (length(model$par) > tab_limit(task, "max_vcov_terms")) {
      warning("the model card keeps no covariance (more than ", tab_limit(task, "max_vcov_terms"), " parameters): no interval is given")
    } else {
      warning("the fit had no covariance (its Hessian was not positive definite: the estimates are not a maximum of the likelihood): ",
              "no interval is given")
    }
    interval <- FALSE
  }
  q_names <- .ps_names("quantile_", quantiles)
  s_names <- .ps_names("survival_at_", times)
  bounds <- function(x) if (interval && length(x)) c(paste0(x, "_low"), paste0(x, "_high")) else character()
  made <- c("prediction", if (interval) c("prediction_low", "prediction_high"), q_names, bounds(q_names), s_names, bounds(s_names))
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  d <- ps_design(model, df)
  alive_at <- ps_alive_column(df, task$params$alive_at)
  pr <- ps_predict(model, d$X, ifelse(d$ok, d$stratum, NA_integer_), quantiles, times, interval = interval, alive_at = alive_at)
  if (d$unseen > 0L) warning(d$unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  empty <- sum(!d$ok) - d$unseen
  if (empty > 0L) warning(empty, " rows have an empty value in a column the model needs: their prediction is empty")
  # beyond the last time observed (in the row's stratum) the numbers come from the distribution's shape
  last <- if (is.null(model$strata_col)) rep(model$last_time, nrow(df)) else
    .ps_num(model$last_time_by_stratum[model$strata_levels])[ifelse(d$ok, d$stratum, 1L)]
  furthest <- pmax(pr$median, if (length(quantiles)) apply(pr$quantile, 1L, max) else -Inf, if (length(times)) max(times) else -Inf,
                   na.rm = TRUE)
  # with alive_at the numbers are the time left: the time on the model's scale is a more
  if (!is.null(alive_at)) furthest <- alive_at + furthest
  beyond <- sum(d$ok & !pr$alive_empty & !pr$alive_dead & is.finite(furthest) & furthest > last)
  if (sum(pr$alive_empty) > 0L) {
    warning(sum(pr$alive_empty), " rows have an empty, negative or infinite time already survived (alive_at): their prediction is empty")
  }
  if (sum(pr$alive_dead) > 0L) {
    warning(sum(pr$alive_dead), " rows have survived beyond what the model allows (the probability of lasting that long is 0): their prediction is empty")
  }
  if (beyond > 0L) {
    warning(beyond, " rows are predicted beyond the last time observed when fitting (", tab_number_label(model$last_time),
            if (!is.null(model$strata_col)) ", or their stratum's" else "", "): an extrapolation")
  }
  out <- df
  out$prediction <- pr$median
  if (interval) {
    out$prediction_low <- pr$median_low
    out$prediction_high <- pr$median_high
  }
  put <- function(name, v, lo, hi) {
    out[[name]] <<- v
    if (interval) {
      out[[paste0(name, "_low")]] <<- lo
      out[[paste0(name, "_high")]] <<- hi
    }
  }
  for (k in seq_along(q_names)) put(q_names[[k]], pr$quantile[, k], pr$quantile_low[, k], pr$quantile_high[, k])
  for (k in seq_along(s_names)) put(s_names[[k]], pr$surv[, k], pr$surv_low[, k], pr$surv_high[, k])
  rownames(out) <- NULL
  list(output = out)
}

# ── the card against flexsurv, in the self-test ──────────────────────────────
# The card read back from its JSON gives flexsurv's own quantiles and survival of the rows of the
# fit (summary.flexsurvreg with newdata) within 1e-9; a gap is a failure.
ps_check_card <- function(task, card, prepared, fitted) {
  if (!identical(task$group_version, "selftest")) return(invisible(TRUE))
  model <- ps_model(jsonlite::fromJSON(tab_card_text(card), simplifyVector = FALSE))
  if (!identical(model$par, fitted$par)) stop("the card's coefficients are not the fit's to the bit")
  probs <- c(0.5, prepared$quantiles)
  times <- if (length(prepared$times)) prepared$times else stats::quantile(prepared$time, c(0.25, 0.5), names = FALSE)
  pr <- ps_predict(model, prepared$X, prepared$stratum, prepared$quantiles, times)
  nd <- prepared$frame
  native <- ps_native(fitted, prepared$alpha)
  sq <- summary(native, newdata = nd, type = "quantile", quantiles = probs, ci = FALSE, tidy = TRUE, cross = TRUE)
  ss <- summary(native, newdata = nd, type = "survival", t = times, ci = FALSE, tidy = TRUE, cross = TRUE)
  want_q <- matrix(sq$est, nrow = nrow(nd), byrow = TRUE)
  want_s <- matrix(ss$est, nrow = nrow(nd), byrow = TRUE)
  got_q <- cbind(pr$median, pr$quantile)
  fin <- is.finite(want_q) | is.finite(got_q)
  gap_q <- if (any(fin)) max(abs(got_q[fin] - want_q[fin]) / pmax(1, abs(want_q[fin]))) else 0
  gap_s <- max(abs(pr$surv - want_s))
  if (!is.finite(gap_q) || gap_q > PS_CARD_CHECK || !is.finite(gap_s) || gap_s > PS_CARD_CHECK) {
    stop("the card does not give back flexsurv's predictions (quantiles ", format(gap_q, digits = 3), ", survival ",
         format(gap_s, digits = 3), ")")
  }
  invisible(TRUE)
}

# ── the runner's own tests, run by the group's self-test with the common library's ──
.ps_test_table <- function(code) {
  path <- file.path(code, "reference", "data", "ps_weibull.csv")
  if (!file.exists(path)) stop("no reference table ", path)
  tab_read_csv(path, list(days = "number", status = "text", load = "number", line = "text", plant = "text", w = "integer"))
}

.ps_test_task <- function(frame, params, output = "results") {
  list(model = "parametric_survival", variant = tab_variant_of("parametric_survival", params), group = "survival",
       group_version = "selftest", params = params, output = output, rows_from = "run", limits = tab_catalog()$limits,
       .frame = frame)
}

# Runs `expr`, returning its value and the warnings it gave.
.ps_with_warnings <- function(expr) {
  said <- character()
  value <- withCallingHandlers(expr, warning = function(w) {
    said <<- c(said, conditionMessage(w))
    invokeRestart("muffleWarning")
  })
  list(value = value, warnings = said)
}

TAB_COMMON_TESTS[["parametric survival: column names that look like code, and Predict from the card equal to the rows output to the bit"]] <- function(code) {
  df <- .ps_test_table(code)
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                 weights = "w", distribution = "weibull", quantiles = list(0.25), times = list(10, 30), output = "rows")
  plain <- fit_parametric_survival(.ps_test_task(df, params, "rows"))
  odd <- df
  weird <- "plant); q(\"no\")"
  names(odd) <- c(TAB_A, TAB_X, "load", TAB_U, weird, "w")[match(names(df), c("days", "status", "load", "line", "plant", "w"))]
  odd_params <- utils::modifyList(params, list(time = TAB_A, event = TAB_X, strata = weird))
  odd_params$predictors <- list("load", TAB_U)
  coded <- fit_parametric_survival(.ps_test_task(odd, odd_params, "rows"))
  a <- tab_results_frame(plain$rows)
  b <- tab_results_frame(coded$rows)
  .tab_expect(identical(a$value, b$value), "the numbers differ when the columns are renamed")
  .tab_expect(identical(b$text[b$key == "model|||formula"], paste0("Surv(", TAB_A, ", ", TAB_X, " = failed) ~ load + ", TAB_U, " + strata(",
                                                                    weird, ")")), "formula: ", b$text[b$key == "model|||formula"])
  .tab_expect(paste0("coef|", TAB_U, "|B|time_ratio") %in% b$key && paste0("stratum|", weird, "=Q||median") %in% b$key &&
              paste0("parameter|shape|", weird, "=R|estimate") %in% b$key, "the terms' and strata's names")
  .tab_expect(!any(grepl("hazard_ratio", a$stat)) && "model|||note_strata_no_hr" %in% a$key, "a Weibull with strata has no hazard ratio")
  # the rows output: every row, empty where a predictor is, the residuals on the rows of the fit only
  out <- plain$output
  .tab_expect(nrow(out) == nrow(df) && all(c("pred_median_low", "pred_quantile_0.25_high", "pred_surv_at_30_low") %in% names(out)),
              "the rows output's columns: ", paste(names(out), collapse = ", "))
  .tab_expect(is.na(out$pred_median[is.na(df$load)][[1L]]) && is.na(out$pred_cox_snell[df$days == 0][[1L]]) &&
              is.finite(out$pred_median[df$days == 0][[1L]]), "the rows output's empty values")
  .tab_expect(all(out$pred_median_low < out$pred_median & out$pred_median < out$pred_median_high, na.rm = TRUE), "intervals around the median")
  # Predict from the card read back from its JSON, on the same rows: the same numbers to the bit
  card <- jsonlite::fromJSON(tab_card_text(plain$card), simplifyVector = FALSE)
  got <- predict_parametric_survival(list(params = list(interval = TRUE), .frame = df), card)$output
  same <- c(prediction = "pred_median", prediction_low = "pred_median_low", prediction_high = "pred_median_high",
            quantile_0.25 = "pred_quantile_0.25", quantile_0.25_low = "pred_quantile_0.25_low", survival_at_10 = "pred_surv_at_10",
            survival_at_30_high = "pred_surv_at_30_high")
  for (k in names(same)) .tab_expect(identical(got[[k]], out[[same[[k]]]]), k, " differs from the rows output's ", same[[k]])
  # a level and a stratum the fit did not see, an empty stratum: empty predictions, counted
  rows_ <- df[c(1L, 2L, 4L, 5L), ]
  rows_$line[[1L]] <- "Z"
  rows_$plant[[2L]] <- "S"
  rows_$plant[[3L]] <- NA
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(), .frame = rows_), card))
  p_ <- run$value$output
  .tab_expect(all(is.na(p_$prediction[1:3])) && is.finite(p_$prediction[[4L]]) && all(is.na(p_$survival_at_10[1:3])), "unseen values")
  .tab_expect(any(grepl("^2 rows hold a value the model did not see", run$warnings)) &&
              any(grepl("^1 rows have an empty value", run$warnings)), "the warnings: ", paste(run$warnings, collapse = " | "))
  .tab_refused_with("missing_column", predict_parametric_survival(list(params = list(), .frame = df[, c("load", "line")]), card))
}

TAB_COMMON_TESTS[["parametric survival: the delta method's interval of the median against survreg's, Weibull and log-normal"]] <- function(code) {
  df <- .ps_test_table(code)
  used <- df[stats::complete.cases(df[, c("days", "status", "load", "line")]) & df$days > 0, ]
  rows_ <- used[c(1L, 30L, 77L, 150L), ]
  for (d in c("weibull", "lognormal")) {
    params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), weights = "w",
                   distribution = d, alpha = 0.1)
    fit <- fit_parametric_survival(.ps_test_task(df, params))
    card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
    got <- predict_parametric_survival(list(params = list(interval = TRUE), .frame = rows_), card)$output
    sr <- survival::survreg(survival::Surv(days, status == "failed") ~ load + line, data = used, weights = w, dist = d,
                            control = survival::survreg.control(rel.tolerance = 1e-13, maxiter = 200))
    uq <- stats::predict(sr, newdata = rows_, type = "uquantile", p = 0.5, se.fit = TRUE)
    se <- (log(got$prediction_high) - log(got$prediction)) / stats::qnorm(0.95)
    .tab_expect(max(abs(log(got$prediction) - uq$fit)) < 1e-7, d, ": the median differs from survreg's")
    .tab_expect(max(abs(se / uq$se.fit - 1)) < 1e-6, d, ": the median's standard error differs from survreg's by ",
                format(max(abs(se / uq$se.fit - 1)), digits = 3))
  }
}

TAB_COMMON_TESTS[["parametric survival: native.rds holds no row of the input, and answers summary(newdata) as the card"]] <- function(code) {
  df <- .ps_test_table(code)
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                 distribution = "gengamma")
  task <- .ps_test_task(df, params)
  fit <- fit_parametric_survival(task)
  full <- fit$native
  lean <- unserialize(serialize(tab_native_lean(full), NULL))
  n <- nrow(full$data$m)
  rows_in <- function(x, path = "fit") {
    if (is.environment(x) || is.function(x)) return(character())
    found <- if ((is.data.frame(x) && nrow(x) > 0L) || (is.atomic(x) && NROW(x) >= n)) path else character()
    if (is.list(x) && !is.data.frame(x)) for (k in seq_along(x)) found <- c(found, rows_in(x[[k]], paste0(path, "$", names(x)[k] %||% k)))
    found
  }
  left <- rows_in(lean)
  .tab_expect(!length(left), "rows left in ", paste(left, collapse = ", "))
  .tab_expect(length(serialize(lean, NULL)) < length(serialize(full, NULL)) / 2, "not smaller than the fit")
  prepared <- ps_prepare(task)
  nd <- prepared$frame[c(1L, 50L, 99L), ]
  a <- summary(lean, newdata = nd, type = "median", ci = FALSE, tidy = TRUE)$est
  b <- summary(full, newdata = nd, type = "median", ci = FALSE, tidy = TRUE)$est
  model <- ps_model(fit$card)
  pr <- ps_predict(model, prepared$X[c(1L, 50L, 99L), , drop = FALSE], prepared$stratum[c(1L, 50L, 99L)])
  .tab_expect(identical(a, b) && max(abs(a / pr$median - 1)) < 1e-12, "summary(newdata) of native.rds differs from the card's")
}

TAB_COMMON_TESTS[["parametric survival: a first attempt that fails is followed by a second start"]] <- function(code) {
  df <- .ps_test_table(code)
  gompertz <- tab_read_csv(file.path(code, "reference", "data", "ps_gompertz.csv"),
                           list(years = "number", died = "integer", age = "number", smoker = "boolean"))
  for (d in c("weibull", "lognormal", "gengamma", "gompertz")) {
    params <- if (d == "gompertz") list(time = "years", event = "died", event_value = 1, predictors = list("age", "smoker"), distribution = d) else
      list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant", distribution = d)
    frame <- if (d == "gompertz") gompertz else df
    first <- tab_results_frame(fit_parametric_survival(.ps_test_task(frame, params))$rows)
    task <- .ps_test_task(frame, params)
    task$.selftest_fail_first <- TRUE
    second <- tab_results_frame(fit_parametric_survival(task)$rows)
    ll <- function(x) x$value[x$key == "model|||log_lik"]
    .tab_expect("model|||note_second_start" %in% second$key && !"model|||note_second_start" %in% first$key, d, ": the note")
    .tab_expect(abs(ll(second) / ll(first) - 1) < 1e-9, d, ": the second start ends elsewhere (", ll(second), " against ", ll(first), ")")
  }
}

TAB_COMMON_TESTS[["parametric survival: Predict's options, its warnings and the refusals of the rows"]] <- function(code) {
  df <- .ps_test_table(code)
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load"), distribution = "loglogistic",
                 times = list(10, 30), quantiles = list(0.25), output = "rows")
  fit <- fit_parametric_survival(.ps_test_task(df, params, "rows"))
  card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
  rows_ <- df[1:5, ]
  # the card's quantiles and times by default; Predict's own replace them, 0.5 not repeated
  p_ <- predict_parametric_survival(list(params = list(), .frame = rows_), card)$output
  .tab_expect(identical(setdiff(names(p_), names(rows_)), c("prediction", "quantile_0.25", "survival_at_10", "survival_at_30")),
              "the card's defaults: ", paste(setdiff(names(p_), names(rows_)), collapse = ", "))
  p_ <- predict_parametric_survival(list(params = list(quantiles = list(0.9, 0.5, 0.1), at_times = list(2.5)), .frame = rows_), card)$output
  .tab_expect(identical(setdiff(names(p_), names(rows_)), c("prediction", "quantile_0.1", "quantile_0.9", "survival_at_2.5")),
              "Predict's options: ", paste(setdiff(names(p_), names(rows_)), collapse = ", "))
  .tab_expect(all(p_$quantile_0.1 < p_$prediction & p_$prediction < p_$quantile_0.9, na.rm = TRUE), "the quantiles' order")
  # beyond the last time observed: predicted, with a warning
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(at_times = list(1000)), .frame = rows_), card))
  .tab_expect(all(is.finite(run$value$output$survival_at_1000[!is.na(rows_$load)])) &&
              any(grepl("beyond the last time observed when fitting", run$warnings)), "the extrapolation")
  # a card without its covariance: no interval, a warning
  bare <- card
  bare$vcov <- NULL
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(interval = TRUE), .frame = rows_), bare))
  .tab_expect(!"prediction_low" %in% names(run$value$output) && any(grepl("no interval is given", run$warnings)), "no covariance")
  # a column the step would write is already there
  clash <- rows_
  clash$quantile_0.25 <- 1
  .tab_refused_with("output_column_exists", predict_parametric_survival(list(params = list(), .frame = clash), card))
  clash <- df
  clash$pred_surv_at_30 <- 1
  .tab_refused_with("output_column_exists", fit_parametric_survival(.ps_test_task(clash, params, "rows")))
  # weights below 0, a time that is a text
  bad <- df
  bad$w[[3L]] <- -1
  .tab_refused_with("negative_weight", fit_parametric_survival(.ps_test_task(bad, utils::modifyList(params, list(weights = "w")))))
  .tab_refused_with("time_type", fit_parametric_survival(.ps_test_task(df, utils::modifyList(params, list(time = "line")))))
}

# ── conditional survival (alive_at, lot P9) ──────────────────────────────────
TAB_COMMON_TESTS[["parametric survival: alive_at on the exponential, which has no memory: the time left does not depend on it"]] <- function(code) {
  d <- tab_read_csv(file.path(code, "reference", "data", "ps_exponential.csv"), list(hours = "number", broke = "boolean", shift = "text"))
  params <- list(time = "hours", event = "broke", event_value = TRUE, distribution = "exponential", quantiles = list(0.25))
  card <- jsonlite::fromJSON(tab_card_text(fit_parametric_survival(.ps_test_task(d, params))$card), simplifyVector = FALSE)
  events <- sum(d$broke)
  lambda <- events / sum(d$hours)
  z <- stats::qnorm(0.975)
  se <- 1 / sqrt(events) # the exponential's standard error on log rate, in closed form
  rows_ <- d[1:7, ]
  rows_$age <- c(0, 7.5, 40, 120, 250, 400, 600) # S(a) from 1 to 5e-5 (the rate is about 1/200)
  got <- predict_parametric_survival(list(params = list(interval = TRUE, alive_at = "age", quantiles = list(0.25, 0.9),
                                                         at_times = list(10, 50)), .frame = rows_), card)$output
  near <- function(x, want, tol, what) .tab_expect(max(abs(x / want - 1)) < tol, "alive_at, ", what, ": off by ", format(max(abs(x / want - 1)), digits = 3))
  # the median and the quantiles of the time left are the same whatever has been survived (closed forms, 1e-10)
  near(got$prediction, rep(log(2) / lambda, 7L), 1e-10, "the median time left")
  near(got$quantile_0.25, rep(-log(0.75) / lambda, 7L), 1e-10, "quantile 0.25")
  near(got$quantile_0.9, rep(-log(0.1) / lambda, 7L), 1e-10, "quantile 0.9")
  near(got$survival_at_10, rep(exp(-lambda * 10), 7L), 1e-10, "S(a + 10) / S(a)")
  near(got$survival_at_50, rep(exp(-lambda * 50), 7L), 1e-10, "S(a + 50) / S(a)")
  # and so are the intervals, by the delta method (1e-6: the gradient is numerical)
  near(got$prediction_low, rep(log(2) / lambda * exp(-z * se), 7L), 1e-6, "the median's lower bound")
  near(got$prediction_high, rep(log(2) / lambda * exp(z * se), 7L), 1e-6, "the median's upper bound")
  near(got$survival_at_50_low, rep(exp(-exp(log(lambda * 50) + z * se)), 7L), 1e-6, "S's lower bound")
  near(got$survival_at_50_high, rep(exp(-exp(log(lambda * 50) - z * se)), 7L), 1e-6, "S's upper bound")
  # without alive_at the same rows give the same numbers again, and the survival of the first time unchanged
  plain <- predict_parametric_survival(list(params = list(quantiles = list(0.25, 0.9), at_times = list(10, 50)), .frame = rows_), card)$output
  near(plain$prediction, got$prediction, 1e-10, "the median from the start")
}

TAB_COMMON_TESTS[["parametric survival: alive_at against flexsurv's survival and quantiles and a root of S, six distributions"]] <- function(code) {
  df <- .ps_test_table(code)
  gompertz <- tab_read_csv(file.path(code, "reference", "data", "ps_gompertz.csv"),
                           list(years = "number", died = "integer", age = "number", smoker = "boolean"))
  for (dist in c("weibull", "exponential", "lognormal", "loglogistic", "gengamma", "gompertz")) {
    if (dist == "gompertz") {
      frame <- gompertz
      params <- list(time = "years", event = "died", event_value = 1, predictors = list("age", "smoker"), distribution = dist)
    } else {
      frame <- df
      params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                     distribution = dist)
    }
    task <- .ps_test_task(frame, params)
    prepared <- ps_prepare(task)
    fit <- fit_parametric_survival(task)
    model <- ps_model(fit$card)
    pick <- c(1L, 17L, 40L, 63L)
    last <- max(prepared$time)
    ages <- c(0, 0.2, 0.6, 1.1) * last
    # the card's, with the intervals
    got <- ps_predict(model, prepared$X[pick, , drop = FALSE], prepared$stratum[pick], c(0.1, 0.9), c(last / 20, last / 3), interval = TRUE,
                      alive_at = ages)
    native <- fit$native
    for (k in seq_along(pick)) {
      nd <- prepared$frame[pick[[k]], ]
      a <- ages[[k]]
      surv <- function(t) summary(native, newdata = nd, type = "survival", t = t, ci = FALSE, tidy = TRUE)$est
      s_a <- if (a > 0) surv(a) else 1
      # S(a + t) / S(a): flexsurv's own survival, to 1e-9
      want_s <- vapply(c(last / 20, last / 3), function(t) surv(a + t) / s_a, 0)
      .tab_expect(max(abs(got$surv[k, ] - want_s)) < 1e-9, dist, ": S(a + t) / S(a) off by ", format(max(abs(got$surv[k, ] - want_s)), digits = 3))
      # the remaining median and quantiles: flexsurv's quantile at 1 − (1 − p) S(a), less a, to 1e-9 relative
      probs <- c(0.5, 0.1, 0.9)
      up_to <- 1 - (1 - probs) * s_a
      want_q <- summary(native, newdata = nd, type = "quantile", quantiles = up_to, ci = FALSE, tidy = TRUE)$est - a
      mine <- c(got$median[[k]], got$quantile[k, ])
      fin <- is.finite(want_q)
      .tab_expect(identical(is.finite(mine), fin), dist, ": the quantiles that exist differ")
      .tab_expect(!any(fin) || max(abs(mine[fin] / want_q[fin] - 1)) < 1e-9, dist, ": the time left off by ",
                  format(max(abs(mine[fin] / want_q[fin] - 1)), digits = 3))
      # a root of S(a + q) = ½ S(a), found numerically from the survival alone, for the median
      if (is.finite(mine[[1L]]) && s_a > 1e-6) {
        root <- stats::uniroot(function(q) surv(a + q) - 0.5 * s_a, c(0, 1000 * last), tol = 1e-12)$root
        .tab_expect(abs(root / mine[[1L]] - 1) < 1e-7, dist, ": the median against a root of S: ", format(abs(root / mine[[1L]] - 1), digits = 3))
      }
      # the interval holds the estimate, and is not wider than the unconditional one is at a = 0 ... only the order
      ok_ <- is.finite(got$median_low[[k]])
      .tab_expect(!ok_ || (got$median_low[[k]] < got$median[[k]] && got$median[[k]] < got$median_high[[k]]), dist, ": the interval around the median")
      .tab_expect(all(got$surv_low[k, ] <= got$surv[k, ] & got$surv[k, ] <= got$surv_high[k, ]), dist, ": the interval around S")
    }
  }
}

TAB_COMMON_TESTS[["parametric survival: alive_at, what has no meaning is empty and counted, a cure never reaches the median, the column is checked"]] <- function(code) {
  df <- .ps_test_table(code)
  gompertz <- tab_read_csv(file.path(code, "reference", "data", "ps_gompertz.csv"),
                           list(years = "number", died = "integer", age = "number", smoker = "boolean"))
  fitted <- fit_parametric_survival(.ps_test_task(gompertz, list(time = "years", event = "died", event_value = 1,
                                                                 predictors = list("age", "smoker"), distribution = "gompertz")))
  card <- jsonlite::fromJSON(tab_card_text(fitted$card), simplifyVector = FALSE)
  rows_ <- gompertz[1:5, ]
  # empty, negative, infinite, and so old that nobody lasts (S(a) = 0 in double precision)
  rows_$years_lived <- c(2, NA, -1, Inf, 1e6)
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(alive_at = "years_lived", interval = TRUE, at_times = list(3)),
                                                            .frame = rows_), card))
  out <- run$value$output
  .tab_expect(is.finite(out$prediction[[1L]]) && is.finite(out$survival_at_3[[1L]]) && all(is.na(out$prediction[2:5])) &&
              all(is.na(out$survival_at_3[2:5])) && all(is.na(out$prediction_low[2:5])), "the empty rows: ", paste(out$prediction, collapse = " "))
  .tab_expect(any(grepl("^3 rows have an empty, negative or infinite time already survived", run$warnings)) &&
              any(grepl("^1 rows have survived beyond what the model allows", run$warnings)), "the warnings: ", paste(run$warnings, collapse = " | "))
  # the conditional answer is not the unconditional one
  plain <- predict_parametric_survival(list(params = list(at_times = list(3)), .frame = rows_[1, ]), card)$output
  .tab_expect(abs(out$prediction[[1L]] - plain$prediction[[1L]]) > 1e-3, "alive_at changed nothing")
  # beyond the last time observed: the extrapolation warning counts the row by the time already survived plus what is left
  late <- rows_[1, ]
  late$years_lived <- 1.05 * card$parametric_survival$last_time
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(alive_at = "years_lived"), .frame = late), card))
  .tab_expect(any(grepl("^1 rows are predicted beyond the last time observed", run$warnings)), "the extrapolation: ", paste(run$warnings, collapse = " | "))
  # a column that is not there, or is not a number
  .tab_refused_with("missing_column", predict_parametric_survival(list(params = list(alive_at = "nope"), .frame = rows_), card))
  .tab_refused_with("alive_at_type", predict_parametric_survival(list(params = list(alive_at = "smoker"), .frame = rows_), card))
  .tab_refused_with("missing_column", fit_parametric_survival(.ps_test_task(df, list(time = "days", event = "status", event_value = "failed",
                                                                                    alive_at = "nope"))))
  .tab_refused_with("alive_at_type", fit_parametric_survival(.ps_test_task(df, list(time = "days", event = "status", event_value = "failed",
                                                                                   alive_at = "line"))))
  # a falling hazard: a share never has the event. The time left has a median only while ½ S(a) is above that share
  cure <- tab_read_csv(file.path(code, "reference", "data", "ps_gompertz_cure.csv"), list(months = "number", left = "integer"))
  cured <- fit_parametric_survival(.ps_test_task(cure, list(time = "months", event = "left", event_value = 1, distribution = "gompertz")))
  cmodel <- ps_model(cured$card)
  share <- exp(exp(cmodel$par[[2L]]) / cmodel$par[[1L]]) # S(∞) = exp(rate / shape); flexsurv's order: shape, log rate
  x <- matrix(0, 3L, 0L)
  a_ok <- 0
  a_gone <- 1e3 # long past the point where ½ S(a) is below the share
  pr <- ps_predict(cmodel, x, rep(1L, 3L), c(0.1), c(12), alive_at = c(a_ok, a_gone, a_gone))
  .tab_expect(is.na(pr$median[[1L]]) == (0.5 > 1 - share) && is.na(pr$median[[2L]]) && is.finite(pr$surv[[2L]]) && pr$surv[[2L]] > 0.99 &&
              !any(pr$alive_dead), "the cure: ", paste(pr$median, collapse = " "), " ", share)
}

TAB_COMMON_TESTS[["parametric survival: the rows output with alive_at and Predict give the same numbers to the bit"]] <- function(code) {
  df <- .ps_test_table(code)
  df$age <- rep(c(0, 3, 8.5, NA, 15, 30, -2, 1), length.out = nrow(df))
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                 distribution = "lognormal", quantiles = list(0.25), times = list(10, 30), alive_at = "age", output = "rows")
  fit <- fit_parametric_survival(.ps_test_task(df, params, "rows"))
  out <- fit$output
  card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
  got <- predict_parametric_survival(list(params = list(interval = TRUE, alive_at = "age"), .frame = df), card)$output
  same <- c(prediction = "pred_median", prediction_low = "pred_median_low", prediction_high = "pred_median_high",
            quantile_0.25 = "pred_quantile_0.25", quantile_0.25_low = "pred_quantile_0.25_low", quantile_0.25_high = "pred_quantile_0.25_high",
            survival_at_10 = "pred_surv_at_10", survival_at_10_low = "pred_surv_at_10_low", survival_at_30_high = "pred_surv_at_30_high")
  for (k in names(same)) .tab_expect(identical(got[[k]], out[[same[[k]]]]), k, " differs from the rows output's ", same[[k]])
  # the empty and negative ages are empty rows, the others are not; the node's own estimates do not depend on alive_at
  bad <- is.na(df$age) | df$age < 0
  .tab_expect(all(is.na(out$pred_median[bad])) && any(is.finite(out$pred_median[!bad])), "the rows output's empty ages")
  without <- fit_parametric_survival(.ps_test_task(df, utils::modifyList(params, list(alive_at = NULL)), "rows"))
  .tab_expect(identical(tab_results_frame(fit$rows)$value, tab_results_frame(without$rows)$value), "alive_at changed the fit")
  .tab_expect(identical(out$pred_cox_snell, without$output$pred_cox_snell) && identical(out$pred_linear, without$output$pred_linear),
              "alive_at changed the residuals or the linear predictor")
}

# ── the final review's corrections (lot P12) ─────────────────────────────────
TAB_COMMON_TESTS[["parametric survival: a strata column of one level is no strata; a time of 0 with a start is left out, not refused"]] <- function(code) {
  df <- .ps_test_table(code)
  base <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), distribution = "weibull")
  one <- df
  one$plant <- "P"
  a <- tab_results_frame(fit_parametric_survival(.ps_test_task(one, c(base, list(strata = "plant"))))$rows)
  b <- tab_results_frame(fit_parametric_survival(.ps_test_task(df, base))$rows)
  get <- function(f, key) f$value[match(key, f$key)]
  # one shape: the Weibull's hazard ratios are written, and the note that there are none is not
  .tab_expect(get(a, "model|||n_strata") == 1 && "coef|load||hazard_ratio" %in% a$key && !"model|||note_strata_no_hr" %in% a$key,
              "a stratum of one level: ", paste(a$key[a$section == "model" & grepl("note", a$key)], collapse = ", "))
  for (key in c("model|||log_lik", "coef|load||hazard_ratio", "coef|line|B|time_ratio")) {
    .tab_expect(abs(get(a, key) / get(b, key) - 1) < 1e-9, key, " with a stratum of one level is not the fit without strata")
  }
  # a start column of 0 everywhere, rows with a time of 0 among them: they are left out and counted, as without a start
  zero <- df
  zero$entry <- 0
  c_ <- tab_results_frame(fit_parametric_survival(.ps_test_task(zero, c(base, list(start = "entry"))))$rows)
  .tab_expect(get(c_, "model|||n_zero_time") == get(b, "model|||n_zero_time") && get(b, "model|||n_zero_time") > 0 &&
              "model|||note_zero_times" %in% c_$key, "the rows with a time of 0 and a start")
  .tab_expect(abs(get(c_, "model|||log_lik") / get(b, "model|||log_lik") - 1) < 1e-9, "a start of 0 is the fit without a start")
}

TAB_COMMON_TESTS[["parametric survival: an altered card is refused with a sentence; Predict says why a card has no interval"]] <- function(code) {
  df <- .ps_test_table(code)
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), distribution = "weibull")
  card <- jsonlite::fromJSON(tab_card_text(fit_parametric_survival(.ps_test_task(df, params))$card), simplifyVector = FALSE)
  rows_ <- df[1:3, ]
  predict_with <- function(c_, p = list(interval = TRUE)) predict_parametric_survival(list(params = p, .frame = rows_), c_)
  alter <- function(f) {
    c_ <- card
    f(c_)
  }
  .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$vcov <- c_$vcov[1:2]; c_ })))
  .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$vcov[[1L]] <- c_$vcov[[1L]][-1L]; c_ })))
  .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$parametric_survival$distribution <- "nope"; c_ })))
  .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$parametric_survival$alpha <- 2; c_ })))
  .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$coefficients <- c_$coefficients[-1L]; c_ })))
  said <- .tab_refused_with("card_invalid", predict_with(alter(function(c_) { c_$parametric_survival <- NULL; c_ })))
  .tab_expect(grepl("^the model card is not a parametric survival model's", said), said)
  # no covariance: too many parameters to keep it, or a fit whose Hessian was not positive definite
  bare <- card
  bare$vcov <- NULL
  run <- .ps_with_warnings(predict_with(bare))
  .tab_expect(any(grepl("Hessian was not positive definite", run$warnings)) && !"prediction_low" %in% names(run$value$output), run$warnings)
  run <- .ps_with_warnings(predict_parametric_survival(list(params = list(interval = TRUE), .frame = rows_, limits = list(max_vcov_terms = 2)), bare))
  .tab_expect(any(grepl("keeps no covariance \\(more than 2 parameters\\)", run$warnings)), run$warnings)
}

TAB_COMMON_TESTS[["parametric survival: Newton steps from the rows' own derivatives, and auto's candidates reused by the checks"]] <- function(code) {
  df <- .ps_test_table(code)
  for (d in c("weibull", "gengamma", "gompertz")) {
    task <- .ps_test_task(df, list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                                   distribution = d))
    prepared <- ps_prepare(task)
    fitted <- ps_fit(prepared, d)
    full <- ps_newton(fitted)
    # the structured gradient and Hessian against numDeriv's on all the parameters, at the maximum
    dv <- ps_structured_derivatives(full, prepared)
    H <- numDeriv::hessian(full$loglik_fn, full$par)
    .tab_expect(max(abs(dv$hessian - H)) / max(abs(H)) < 1e-5, d, ": the Hessian off by ", format(max(abs(dv$hessian - H)) / max(abs(H)), digits = 3))
    # from a point half a standard error away (20 to 110 below in log-likelihood) the steps reach the
    # maximum (the Richardson Newton's)
    away <- fitted
    away$par <- full$par + 0.5 * sqrt(diag(full$vcov))
    away$loglik <- away$loglik_fn(away$par)
    step <- ps_newton_steps(away, prepared)
    .tab_expect(step$newton_steps >= 1L && abs(step$loglik - full$loglik) < 1e-9 * abs(full$loglik), d, ": the steps end ",
                format(full$loglik - step$loglik, digits = 3), " below the maximum")
  }
  # auto: the generalized gamma of the comparison is the candidate's, at its maximum; no fit is made again
  params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), weights = "w", distribution = "auto")
  calls <- 0L
  real <- ps_fit
  assign("ps_fit", function(...) { calls <<- calls + 1L; real(...) }, envir = globalenv())
  f <- tryCatch(tab_results_frame(fit_parametric_survival(.ps_test_task(df, params))$rows), finally = assign("ps_fit", real, envir = globalenv()))
  # the six candidates and the model without predictors: nothing for the checks
  .tab_expect(calls == 7L, "auto fitted ", calls, " models")
  manual <- tab_results_frame(fit_parametric_survival(.ps_test_task(df, utils::modifyList(params, list(distribution = "weibull"))))$rows)
  at <- function(x) x$value[x$section == "assumption" & x$level == "distribution_vs_gengamma" & x$stat == "statistic"]
  .tab_expect(abs(at(f) - at(manual)) < 1e-6, "the comparison with auto ", at(f), " and without ", at(manual))
  .tab_expect(identical(f$text[f$section == "assumption" & f$level == "distribution_vs_gengamma" & f$stat == "advice_id"], "distribution_vs_gengamma_auto"),
              "the advice of auto")
}

TAB_COMMON_TESTS[["parametric survival: the covariance from the rows' own derivatives against survreg's analytic one and numDeriv's"]] <- function(code) {
  df <- .ps_test_table(code)
  base <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"))
  for (d in c("weibull", "lognormal", "loglogistic")) {
    task <- .ps_test_task(df, c(base, list(distribution = d, strata = "plant")))
    prepared <- ps_prepare(task)
    fitted <- ps_newton(ps_fit(prepared, d))
    # the location's standard errors are survreg's, a scale per stratum (analytic derivatives): 1e-7 relative
    frame <- prepared$frame
    frame$g <- factor(prepared$stratum)
    sr <- survival::survreg(survival::Surv(time, status) ~ d1 + d2 + d3 + s1 + s2 + strata(g), data = frame, dist = d,
                            control = survival::survreg.control(rel.tolerance = 1e-13, maxiter = 200))
    i <- c(fitted$spec$i_terms, fitted$spec$i_strata)
    gap <- max(abs(sqrt(diag(fitted$vcov))[i] / sqrt(diag(stats::vcov(sr)))[2:6] - 1))
    .tab_expect(gap < 1e-7, d, ": the standard errors off survreg's by ", format(gap, digits = 3))
    # without the rows (a fit that does not carry them) the covariance is numDeriv's, within 1e-6
    bare <- fitted
    bare$prepared <- NULL
    gap <- max(abs(sqrt(diag(ps_covariance(bare))) / sqrt(diag(fitted$vcov)) - 1))
    .tab_expect(gap < 1e-6, d, ": numDeriv's standard errors off by ", format(gap, digits = 3))
  }
}
