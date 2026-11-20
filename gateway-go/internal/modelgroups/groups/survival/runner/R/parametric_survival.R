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
#   - the covariance: the inverse of a Richardson Hessian (numDeriv) of that log-likelihood on the
#     optimiser's scale (flexsurv's own is optim's finite differences where a distribution has no
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
#   ps_covariance(fitted)               V, the Richardson Hessian's inverse, or NULL;
#   ps_newton(fitted)                   Newton steps from flexsurv's estimates to the maximum of the
#                                       runner's log-likelihood, and V there (BFGS stops on the
#                                       function's change, 2.5e-9 to 1e-4 away from it);
#   ps_predict(model, X, strata, quantiles, times, interval, alive_at = NULL)
#                                       quantiles, survival and hazard with their intervals, where
#                                       `model` is ps_model(card): the card's numbers, never a row;
#   ps_write(task, prepared, fitted, rows, …)
#                                       the sections model, coef, parameter and stratum.
# Not here yet: the checks (`assumption`) and the charts' points (P2: ps_checks), the automatic
# choice (P3: ps_auto). Conditional survival (P9) is ps_predict's alive_at. Their hooks are below,
# found by name, so that a lot adds its file without touching this one.
#
# Columns never become code: the design has positional columns, copied with the strata's dummies
# into a frame of fixed names (time, status, w, d1, d2…, s1, s2…) before flexsurv, survreg or
# coxph see them; a column's name is only ever written as a label.

PS_PACKAGES <- c("survival", "flexsurv", "numDeriv")
PS_RELTOL <- 1e-15           # flexsurv's default (1e-8) leaves a Gompertz coefficient 7.5e-6 away
PS_MAXIT <- 2000L
PS_MAXIT_SECOND <- 5000L
PS_LOGLIK_CHECK <- 1e-10     # the runner's log-likelihood against flexsurv's, relative
PS_CARD_CHECK <- 1e-9        # the card's predictions against flexsurv's own, in the self-test
PS_CHUNK <- 100000L          # rows a delta-method jacobian takes at once
PS_NEWTON_STEPS <- 4L        # Newton steps after flexsurv's BFGS, at most
PS_NEWTON_DONE <- 1e-8       # a step below this many standard errors ends them
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
PS_NOTE_WEIGHTS_COUNTS <- paste0(
  "Some weights are not whole numbers: the standard errors read each weight as a count of rows.")

ps_params <- function(params) {
  p <- tab_model_spec("parametric_survival")$defaults
  for (k in names(params)) p[k] <- list(params[[k]])
  p
}

fit_parametric_survival <- function(task) {
  prepared <- ps_prepare(task)
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
  predictors <- unlist(p$predictors)
  interactions <- p$interactions %||% list()
  alpha <- as.numeric(p$alpha %||% 0.05)
  quantiles <- ps_quantiles(p$quantiles)
  times <- sort(unique(as.numeric(unlist(p$times %||% list()))))

  df <- tab_input(task)
  for (column in c(time_col, event_col, strata_col, weights_col, predictors)) {
    if (!column %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows reaching the node have no column «", column, "»"), list(column = column))
    }
  }
  kind <- tab_kind(df[[time_col]])
  if (!kind %in% c("number", "integer")) {
    tab_refuse("time_type", paste0("the column «", time_col, "» is a ", kind, ": a time must be a number, the time from the ",
                                   "start (compute it from the dates first)"), list(column = time_col))
  }
  if (!is.null(weights_col) && !tab_kind(df[[weights_col]]) %in% c("number", "integer")) {
    tab_refuse("weights_type", paste0("the column «", weights_col, "» is a ", tab_kind(df[[weights_col]]), ": weights must be ",
                                      "numbers, 0 or more"), list(column = weights_col))
  }
  ps_alive_column(df, p$alive_at)
  design <- tab_design(df, predictors, reference = p$reference %||% list(), interactions = interactions, intercept = FALSE,
                       also = c(time_col, event_col, strata_col, weights_col),
                       max_levels = tab_limit(task, "max_levels"), max_terms = tab_limit(task, "max_terms"))
  d <- design$data
  time <- as.numeric(d[[time_col]])
  status <- km_is_event(d[[event_col]], p$event_value)
  value_label <- km_value_label(p$event_value)
  w <- if (is.null(weights_col)) rep(1, nrow(d)) else as.numeric(d[[weights_col]])
  if (any(time < 0)) {
    tab_refuse("negative_time", paste0("the column «", time_col, "» holds a negative time (", tab_number_label(min(time)),
                                       "): a time is counted from the start, 0 or more"), list(column = time_col))
  }
  if (any(w < 0)) {
    tab_refuse("negative_weight", paste0("the column «", weights_col, "» holds a negative weight (", tab_number_label(min(w)),
                                         "): a weight is 0 or more"), list(column = weights_col))
  }
  # a time of 0 has no density under any of the six distributions; a weight of 0 adds nothing
  zero_time <- time == 0
  zero_weight <- w == 0 & !zero_time
  keep <- !zero_time & !zero_weight
  rows_used <- design$rows[keep]
  X <- design$X[keep, , drop = FALSE]
  time <- time[keep]
  status <- status[keep]
  w <- w[keep]
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
       time = time, status = status, w = w, rows = rows_used, stratum = stratum, strata_col = strata_col,
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
ps_loglik_rows <- function(m, par, time, status, X, S) {
  pars <- ps_natural(m, par, X, S)
  ld <- ps_log_density(m, time, pars)
  ls <- ps_log_survival(m, time, pars)
  ifelse(status == 1L, ld, ls)
}

# ── the fit ──────────────────────────────────────────────────────────────────
# flexsurvreg on the positional frame. `starts`: fits already made on the same rows (the
# generalized gamma starts from the best of flexsurv's own point, the Weibull's and the
# log-normal's; with `auto` P3 passes those it has). A second attempt when the first stops with an
# error or a convergence code: the next start (generalized gamma), the exponential's rate and a
# flat shape (Gompertz), survreg's estimates (the four survreg knows), with maxit 5000.
ps_fit <- function(prepared, distribution, starts = NULL, hessian = FALSE, fail_first = FALSE) {
  m <- ps_spec(distribution, ncol(prepared$X), length(prepared$strata_levels))
  frame <- prepared$frame
  form <- ps_formula(prepared)
  anc <- ps_anc_formula(prepared, m)
  loglik_rows <- function(par) ps_loglik_rows(m, par, prepared$time, prepared$status, prepared$X, prepared$S)
  loglik <- function(par) sum(prepared$w * loglik_rows(par))
  inits <- ps_starts(prepared, m, distribution, starts, loglik)
  fit_once <- function(init, maxit) {
    tryCatch(suppressWarnings(ps_flexsurvreg(form, anc, frame, m$flexsurv, init = if (is.null(init)) NULL else ps_to_natural(m, init),
                                             control = list(reltol = PS_RELTOL, maxit = maxit), hessian = hessian)),
             error = function(e) e)
  }
  failed <- function(f) inherits(f, "error") || !identical(as.integer(f$opt$convergence %||% 1L), 0L) || !is.finite(f$loglik)
  attempts <- 1L
  first <- if (fail_first) simpleError("forced by the self-test") else fit_once(inits$first, PS_MAXIT)
  fit <- first
  if (failed(fit)) {
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
  list(distribution = distribution, spec = m, converged = TRUE, status = "ok", attempts = attempts, fit = fit,
       par = unname(par), names = names(par), loglik = fit$loglik, n_parameters = length(par),
       loglik_rows = loglik_rows, loglik_fn = loglik, centring = ps_centring(m, prepared$X, prepared$S))
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
  stats::as.formula(paste("survival::Surv(time, status) ~", if (length(rhs)) paste(rhs, collapse = " + ") else "1"),
                    env = new.env(parent = asNamespace("survival")))
}

ps_anc_formula <- function(prepared, m) {
  if (is.na(m$anc)) return(NULL)
  stats::setNames(list(stats::as.formula(paste("~", paste(.ps_cols("s", ncol(prepared$S)), collapse = " + ")),
                                         env = new.env(parent = asNamespace("survival")))), m$anc_name)
}

# The second start on the optimiser's scale: survreg's estimates for the four it fits (the strata
# with a scale each), the exponential's rate and a flat shape for Gompertz.
ps_second_start <- function(prepared, m, distribution, form, frame) {
  n_t <- length(m$i_terms) + length(m$i_strata)
  if (distribution == "gompertz") {
    rate <- sum(prepared$w * prepared$status) / sum(prepared$w * prepared$time)
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

# The covariance on the optimiser's scale: the inverse of the Richardson Hessian of the runner's
# log-likelihood at `par`. NULL when that Hessian is not negative definite (the estimates are not
# a maximum): every standard error and interval is then empty.
# The Hessian is taken with the location's and the shape's columns centred (their intercepts at
# the columns' means: par = A · par_c), then brought back exactly, V = A V_c A'. Uncentred, a
# predictor of values far from 0 makes the Hessian ill-conditioned and its numerical error grows
# in the inverse (7e-7 relative against survreg's analytic standard errors on a table of 80 rows
# with a temperature near 20; 1e-9 centred).
ps_covariance <- function(fitted, par = fitted$par) {
  A <- fitted$centring
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

# Newton steps on the runner's log-likelihood from flexsurv's estimates, each with the Richardson
# Hessian and gradient, while a step is at least PS_NEWTON_DONE standard errors and raises the
# log-likelihood (up to its rounding); V is the covariance at the estimates kept. optim's BFGS stops on the function's
# relative change, so its estimates are known to about the square root of that (2.5e-9 relative on
# the exponential's rate; 1e-4 on a Gompertz intercept with a predictor of values near 50):
# the steps bring them to the maximum within the gradient's precision, which the closed forms and
# a second optimiser of the reference values check.
ps_newton <- function(fitted) {
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
  has_strata <- !is.null(prepared$strata_col)

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
  tab_number(rows, "model", "n_strata", length(prepared$strata_levels))
  tab_number(rows, "model", "n_parameters", k)
  tab_number(rows, "model", "log_lik", ll)
  tab_number(rows, "model", "aic", -2 * ll + 2 * k)
  tab_number(rows, "model", "bic", -2 * ll + k * log(n))
  if (has_predictors) {
    null <- ps_fit(ps_without_predictors(prepared), distribution)
    if (isTRUE(null$converged)) {
      lr <- 2 * (ll - null$loglik)
      tab_number(rows, "model", "lr_statistic", lr)
      tab_number(rows, "model", "lr_df", ncol(prepared$X))
      tab_p(rows, "model", stats::pchisq(lr, ncol(prepared$X), lower.tail = FALSE), stat = "lr_p_value")
    }
  }
  if (distribution %in% c("weibull", "gompertz")) {
    # the shape against the exponential's constant hazard: a number of the fit, not a check
    ex <- ps_fit(prepared, "exponential")
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
    form <- stats::as.formula("Surv(time, status) ~ score + strata(g)", env = new.env(parent = asNamespace("survival")))
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
    km <- survival::survfit(survival::Surv(prepared$time[in_s], prepared$status[in_s]) ~ 1, weights = prepared$w[in_s])
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
    tab_text(rows, "model", "note_second_start", paste0(
      "The first fit did not converge: the estimates come from a second start", if (distribution == "gengamma") "" else
        if (distribution == "gompertz") " (the exponential's rate, a flat shape)" else " (survreg's estimates)", "."))
  }
  if (distribution == "weibull" && has_strata && has_predictors) tab_text(rows, "model", "note_strata_no_hr", PS_NOTE_STRATA_NO_HR)
  if (interval_disagrees) tab_text(rows, "model", "note_hr_interval_scale", PS_NOTE_HR_INTERVAL)
  if (infinite > 0) {
    tab_text(rows, "model", "note_quantile_infinite", paste0(
      "The fitted distribution leaves ", format(round(100 * infinite, 1), nsmall = 1), "% of the profile without the event for ever: ",
      "a quantile beyond that is infinite and left empty."))
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
  f <- stats::as.formula(paste("survival::Surv(time, status) ~", paste(rhs, collapse = " + ")),
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
  paste0("Surv(", prepared$time_col, ", ", prepared$event_col, " = ", prepared$value_label, ") ~ ",
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

# The model a card holds, as ps_predict reads it: the card's numbers only.
ps_model <- function(card) {
  ps <- card$parametric_survival
  levels <- if (is.null(ps$strata)) PS_ALL else vapply(ps$strata$levels, as.character, "")
  order <- ps$order
  n_terms <- sum(vapply(order, function(o) identical(o$role, "location") && !is.null(o$term), logical(1)))
  m <- ps_spec(ps$distribution, n_terms, length(levels))
  par <- .ps_num(card$coefficients)
  if (length(par) != length(order) || length(par) != m$nb + n_terms + length(m$i_strata) + length(m$i_anc)) {
    stop("the card's coefficients and its order differ")
  }
  V <- NULL
  if (length(card$vcov)) V <- do.call(rbind, lapply(card$vcov, .ps_num))
  plain <- card
  plain$params$intercept <- FALSE # the distribution's location takes the intercept
  list(card = plain, spec = m, par = par, vcov = V, alpha = as.numeric(ps$alpha %||% card$alpha %||% 0.05),
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
  mart <- prepared$status - r
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
    warning("the model card keeps no covariance (more than ", tab_limit(task, "max_vcov_terms"), " parameters): no interval is given")
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
