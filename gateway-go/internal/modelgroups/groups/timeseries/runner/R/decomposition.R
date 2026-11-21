# Decomposition (docs/specs/ts-decomposition.md, «The node `decomposition`»): fit_decomposition(task).
#
# Lot D2 writes STL (stats::stl), the classical decomposition (stats::decompose) and MSTL (forecast::mstl): the rows,
# the output, the notes; their explanation is decomposition_reading.R's. EMD is lot D3's (decomposition_emd.R:
# `fit_decomposition_emd(task, o)`). It returns no card:
# `card = NULL` writes no model.json, so nothing can be registered or predicted. Nothing here is random.
#
#   fit_decomposition(task)            the node: the method's route
#   dec_params(params)                 the node's parameters, the catalogue's defaults where a key is not given
#   dec_seasonal(task, o)              STL, classical and MSTL: series, fit, checks, rows, output
#   dec_model_rows(...), dec_checks(...), dec_charts(...)   what EMD may reuse: the model, the remainder's checks, the charts
#
# Each method is called as a person would call it directly on the series transformed and complete (`z`): stats::stl, stats::decompose,
# forecast::mstl (the self-test compares them with the direct calls on co2, AirPassengers and taylor).

DEC_PACKAGES <- c("forecast", "Rlibeemd", "urca", "tseries")

fit_decomposition <- function(task) {
  o <- dec_params(task$params)
  if (identical(o$method, "emd")) return(fit_decomposition_emd(task, o))
  dec_seasonal(task, o)
}

dec_params <- function(params) {
  out <- tab_model_spec("decomposition")$defaults
  for (k in names(params)) if (!is.null(params[[k]])) out[[k]] <- params[[k]]
  out$alpha <- as.numeric(out$alpha)
  out$robust <- isTRUE(out$robust)
  ps <- as.numeric(unlist(out$periods))
  sw <- as.numeric(unlist(out$s_windows))
  # the windows follow their periods when the periods are sorted; a period given twice is kept once, with its window
  if (length(sw) == length(ps)) sw <- sw[order(ps)]
  keep <- !duplicated(sort(ps))
  out$periods <- sort(ps)[keep]
  out$s_windows <- if (length(sw) == length(ps)) sw[keep] else sw
  out
}

# ── the periods and the windows ─────────────────────────────────────────────────────────────────────────────────
# The seasonal periods of the node, increasing: MSTL's `periods`, else `period`, else the frequency's season. A year or
# an index has none of its own (the catalogue asks for `period`; the runner says it too).
dec_periods <- function(o) {
  p <- if (identical(o$method, "mstl")) o$periods else if (!is.null(o$period)) as.numeric(o$period) else
    as.numeric(TS_SEASONS[o$frequency])
  p <- p[!is.na(p)]
  if (!length(p)) {
    tab_refuse("period_needed", paste0("a yearly or index series has no season of its own: give the period, in steps ('",
                                       if (identical(o$method, "mstl")) "periods" else "period", "')"))
  }
  if (any(p != round(p))) {
    tab_refuse("period_whole", paste0("a period is a whole number of steps: ", format(round(p[p != round(p)][[1L]]), scientific = FALSE),
                                      ", not ", format(p[p != round(p)][[1L]], scientific = FALSE)))
  }
  if (any(p < 2)) tab_refuse("period_one", "a period of 1 is no season: give a period of 2 or more, or choose EMD")
  sort(unique(as.integer(p)))
}

# STL's own odd window (the Fortran adds 1 to an even one, and as.integer cuts a fraction): the one passed and written.
dec_odd <- function(w) {
  w <- as.integer(w)
  max(3L, if (w %% 2L == 0L) w + 1L else w)
}

# R's `nextodd`, and stl's trend window when none is given: nextodd(⌈1.5 p / (1 − 1.5 / s.window)⌉).
.dec_nextodd <- function(x) {
  x <- round(x)
  as.integer(if (x %% 2 == 0) x + 1 else x)
}
dec_auto_t_window <- function(p, s_window) .dec_nextodd(ceiling(1.5 * p / (1 - 1.5 / s_window)))

# The windows of the run, as stl and mstl take them, and what they said about them: `s` (one a period; NA for a
# periodic STL), `t` (NULL: left to stl's own rule), `t_used`, and `odd` (given and used, for the note) where a window
# was made odd.
dec_windows <- function(o, periods, n) {
  k <- length(periods)
  odd <- list()
  made <- function(given) {
    used <- dec_odd(given)
    if (!isTRUE(as.numeric(given) == used)) odd[[length(odd) + 1L]] <<- list(given = as.numeric(given), used = used)
    used
  }
  if (identical(o$method, "mstl")) {
    sw <- if (length(o$s_windows)) o$s_windows else 7 + 4 * seq_len(k)
    if (length(sw) != k) {
      tab_refuse("windows_count", paste0("give one seasonal window per period (", k, " period", if (k == 1L) "" else "s", ", ", length(sw),
                                         " window", if (length(sw) == 1L) "" else "s", "), or none"), list(periods = k, windows = length(sw)))
    }
    s <- vapply(sw, made, 0L)
  } else if (identical(o$method, "stl") && identical(o$s_window, "periodic")) {
    s <- NA_integer_
  } else if (identical(o$method, "stl")) {
    s <- made(o$s_window)
  } else {
    s <- NA_integer_
  }
  t <- NULL
  if (o$method %in% c("stl", "mstl") && !identical(o$t_window, "auto")) t <- made(o$t_window)
  last_s <- if (identical(o$method, "stl") && identical(o$s_window, "periodic")) 10L * n + 1L else s[[length(s)]]
  t_used <- if (!is.null(t)) t else if (o$method %in% c("stl", "mstl")) dec_auto_t_window(periods[[length(periods)]], last_s) else NA_integer_
  list(s = s, t = t, t_used = t_used, odd = odd)
}

# ── the node ────────────────────────────────────────────────────────────────────────────────────────────────────
dec_seasonal <- function(task, o) {
  method <- o$method
  periods <- dec_periods(o)
  if (identical(method, "classical") && identical(o$type, "multiplicative") && !identical(o$transform, "none")) {
    tab_refuse("multiplicative_transform", paste0("a multiplicative decomposition already works in ratios: choose no transform, ",
                                                  "or an additive decomposition of the log"))
  }
  s <- ts_series(task, o$time, o$value, o$frequency, missing = o$missing, transform = o$transform, box_cox_lambda = o$box_cox_lambda,
                 period = min(periods))
  n <- s$n
  if (.ts_constant(s$y[!s$filled])) {
    tab_refuse("constant", paste0("the column «", o$value, "» has the same value everywhere: there is nothing to decompose"), list(column = o$value))
  }
  unit <- TS_UNIT[[o$frequency]]
  for (p in periods) {
    need <- if (identical(method, "classical")) 2L * p else 2L * p + 1L
    if (n < need) {
      tab_refuse("too_short", paste0("the series has ", n, " ", unit, "; a period of ", p, " needs ", if (identical(method, "classical")) "at least " else "more than ",
                                     2L * p, ": drop it, or a longer series"), list(n = n, period = p))
    }
  }
  z <- s$z
  mult <- identical(method, "classical") && identical(o$type, "multiplicative")
  if (mult && any(z <= 0)) {
    tab_refuse("not_positive", paste0("the column «", o$value, "» holds 0 or a negative value: a multiplicative decomposition needs ",
                                      "positive values; use type = additive"), list(column = o$value))
  }
  w <- dec_windows(o, periods, n)
  fit <- switch(method, stl = dec_fit_stl(z, periods, o, w), classical = dec_fit_classical(z, periods, o), mstl = dec_fit_mstl(z, periods, o, w))
  ctx <- list(o = o, s = s, periods = periods, fit = fit, w = w, mult = mult, n = n)
  ctx$deseasonalized <- dec_deseasonalized(z, fit, s$lambda, mult)

  rows <- tab_rows_start(task)
  ctx$checks <- dec_checks(task, ctx)
  dec_model_rows(rows, task, ctx)
  ctx$shapes <- dec_season_rows(rows, ctx)
  for (check in ctx$checks) ts_check_rows(rows, check)
  dec_write_reading(rows, dec_reading(ctx))
  dec_charts(task, rows, ctx)
  for (odd in w$odd) tab_note(rows, dec_note("window_made_odd", given = odd$given, used = odd$used))
  output <- if (identical(o$output, "components")) dec_output(ctx) else NULL
  list(rows = rows, output = output, card = NULL)
}

# A note the report says in the page's language (layouts/common.py, structured_note): «@id name=value …».
dec_note <- function(id, ...) {
  values <- list(...)
  paste(c(paste0("@", id), vapply(names(values), function(k) paste0(k, "=", format(values[[k]], digits = 17, scientific = FALSE, trim = TRUE)), "")),
        collapse = " ")
}

# ── the three fits ──────────────────────────────────────────────────────────────────────────────────────────────
# Each returns trend (NA where the method gives none), seasonal (a list, one numeric vector a period, in `periods`'
# order), remainder, and `weights_zero` (the instants whose final robustness weight is 0; NA without `robust`).
dec_fit_stl <- function(z, periods, o, w) {
  p <- periods[[1L]]
  sw <- if (identical(o$s_window, "periodic")) "periodic" else w$s
  f <- stats::stl(stats::ts(z, frequency = p), s.window = sw, t.window = w$t, robust = o$robust)
  x <- f$time.series
  list(trend = as.numeric(x[, "trend"]), seasonal = list(as.numeric(x[, "seasonal"])), remainder = as.numeric(x[, "remainder"]),
       weights_zero = if (o$robust) sum(f$weights == 0) else NA_integer_)
}

dec_fit_classical <- function(z, periods, o) {
  d <- stats::decompose(stats::ts(z, frequency = periods[[1L]]), type = o$type)
  list(trend = as.numeric(d$trend), seasonal = list(as.numeric(d$seasonal)), remainder = as.numeric(d$random), weights_zero = NA_integer_)
}

# forecast::mstl on the series already transformed and complete, so that its own interpolation and transform never run
# (forecast 9.0.2: a `ts` that is not an `msts` runs one pass; an `msts` of one period runs `iterate`). The weights of
# the robust fit are those of the last STL of the last pass, whose input is trend + the last seasonal + remainder, i.e.
# the series without the other seasonals: mstl keeps no fit, so that one STL is run again on that input.
dec_fit_mstl <- function(z, periods, o, w) {
  args <- list(forecast::msts(z, seasonal.periods = periods), iterate = o$iterate, s.window = w$s)
  if (o$robust) args$robust <- TRUE
  if (!is.null(w$t)) args$t.window <- w$t
  m <- do.call(forecast::mstl, args)
  k <- length(periods)
  seasonal <- lapply(seq_len(k), function(i) as.numeric(m[, 2L + i]))
  zero <- NA_integer_
  if (o$robust) {
    last <- k
    input <- z - Reduce(`+`, seasonal[-last], 0)
    f <- stats::stl(stats::ts(input, frequency = periods[[last]]), s.window = w$s[[last]], t.window = w$t, robust = TRUE)
    zero <- sum(f$weights == 0)
  }
  list(trend = as.numeric(m[, "Trend"]), seasonal = seasonal, remainder = as.numeric(m[, "Remainder"]), weights_zero = zero)
}

# The series without its seasons, on the original scale: y − ΣS, or y / S (multiplicative); with a transform z − ΣS
# taken back by the inverse (exp for the log, forecast::InvBoxCox).
dec_deseasonalized <- function(z, fit, lambda, mult) {
  out <- if (mult) z / fit$seasonal[[1L]] else z - Reduce(`+`, fit$seasonal)
  if (!is.null(lambda)) out <- as.numeric(forecast::InvBoxCox(out, lambda))
  out[!is.finite(out)] <- NA_real_
  out
}

# ── the checks of the remainder ─────────────────────────────────────────────────────────────────────────────────
# The checks in the table's order (ts_checks.R's lists; a verdict only from a p-value against alpha). The Ljung–Box lag and the ACF
# the chart draws travel as attributes. The classical remainder is empty at its ends; strengths of a multiplicative one are on logs.
dec_checks <- function(task, ctx) {
  o <- ctx$o
  fit <- ctx$fit
  r <- fit$remainder
  nr <- length(.ts_values(r))
  most <- tab_limit(task, "max_lb_lags")
  h <- ts_lb_lag(o$lb_lag, nr, ctx$periods, most)
  acf <- ts_acf(r, min(max(h, 40L), most))
  out <- list()
  for (p in ctx$periods) {
    out[[length(out) + 1L]] <- ts_seasonality(ctx$s$z, p, o$seasonality_test, o$alpha, if (ctx$mult) "multiplicative" else "additive")
  }
  out[[length(out) + 1L]] <- ts_ljung_box(r, h, o$alpha, acf)
  out[[length(out) + 1L]] <- ts_largest_acf(acf, h)
  out[[length(out) + 1L]] <- ts_adf(r, o$alpha)
  out[[length(out) + 1L]] <- ts_kpss(r, o$alpha)
  as_log <- function(x) if (ctx$mult) log(x) else x
  rl <- as_log(r)
  out[[length(out) + 1L]] <- ts_measure("trend_strength", ts_strength(as_log(fit$trend), rl))
  for (i in seq_along(ctx$periods)) {
    out[[length(out) + 1L]] <- ts_measure("seasonal_strength", ts_strength(as_log(fit$seasonal[[i]]), rl), level_2 = as.character(ctx$periods[[i]]))
  }
  if (o$robust && o$method %in% c("stl", "mstl")) {
    out[[length(out) + 1L]] <- ts_measure("robust_weights", fit$weights_zero, count = fit$weights_zero)
  }
  out <- Filter(Negate(is.null), out)
  names(out) <- vapply(out, function(x) paste(c(x$id, x$level_2), collapse = "|"), "")
  attr(out, "lb_lag") <- h
  attr(out, "acf") <- acf
  out
}

# ── the rows ────────────────────────────────────────────────────────────────────────────────────────────────────
dec_formula <- function(ctx) {
  o <- ctx$o
  p <- ctx$periods
  label <- c(stl = "STL", classical = "classical", mstl = "MSTL", emd = "EMD")[[o$method]]
  lhs <- switch(o$transform, log = paste0("log(", o$value, ")"), box_cox = paste0("boxcox(", o$value, ")"), o$value)
  rhs <- if (ctx$mult) paste0("trend · seasonal[", p, "] · remainder") else
    paste0("trend + ", paste0("seasonal[", p, "]", collapse = " + "), " + remainder")
  paste0(lhs, " = ", rhs, " · ", label, if (o$robust && o$method %in% c("stl", "mstl")) ", robust" else "")
}

# The model section, in the order the fixtures have it. `extra` (a function of rows) lets a method add its own numbers.
dec_model_rows <- function(rows, task, ctx) {
  o <- ctx$o
  s <- ctx$s
  tab_text(rows, "model", "formula", dec_formula(ctx))
  tab_text(rows, "model", "software", tab_software(DEC_PACKAGES))
  tab_text(rows, "model", "method", o$method)
  tab_text(rows, "model", "frequency", o$frequency)
  tab_text(rows, "model", "transform", o$transform)
  tab_text(rows, "model", "time_kind", s$kind)
  tab_text(rows, "model", "missing", o$missing)
  tab_text(rows, "model", "seasonality_test", o$seasonality_test)
  tab_text(rows, "model", "periods", paste(ctx$periods, collapse = ","))
  if (identical(o$method, "classical")) tab_text(rows, "model", "type", o$type)
  tab_number(rows, "model", "n", s$n)
  tab_number(rows, "model", "n_missing", s$n_missing)
  tab_number(rows, "model", "n_filled", s$n_filled)
  tab_number(rows, "model", "first", s$x0[[1L]])
  tab_number(rows, "model", "last", s$x0[[s$n]])
  tab_number(rows, "model", "alpha", o$alpha)
  if (!is.null(s$lambda)) tab_number(rows, "model", "box_cox_lambda", s$lambda)
  tab_number(rows, "model", "lb_lag_used", attr(ctx$checks, "lb_lag"))
  if (o$method %in% c("stl", "mstl")) tab_number(rows, "model", "t_window_used", ctx$w$t_used)
  if (identical(o$method, "mstl")) tab_number(rows, "model", "iterate", o$iterate)
  invisible(rows)
}

# One block of rows per period (section `season`): the seasonal component averaged by position (the calendar's where it
# names them), its peak and trough with their values and names, the amplitude (peak − trough; peak / trough for a
# ratio), the complete cycles and the window used. Returns the shapes, by period, for the reading and the chart.
dec_season_rows <- function(rows, ctx) {
  o <- ctx$o
  s <- ctx$s
  shapes <- list()
  for (i in seq_along(ctx$periods)) {
    p <- ctx$periods[[i]]
    pos <- ts_positions(s$times, s$kind, o$frequency, p)
    S <- ctx$fit$seasonal[[i]]
    by <- split(S, factor(pos$position, levels = seq_len(p)))
    mean_shape <- vapply(by, mean, 0)
    lo <- which.min(mean_shape)
    hi <- which.max(mean_shape)
    lv <- as.character(p)
    window <- if (o$method %in% c("stl", "mstl")) ctx$w$s[[min(i, length(ctx$w$s))]] else NA_integer_
    if (!is.na(window)) tab_number(rows, "season", "s_window_used", window, level = lv)
    tab_number(rows, "season", "cycles", s$n %/% p, level = lv)
    tab_number(rows, "season", "amplitude", if (ctx$mult) mean_shape[[hi]] / mean_shape[[lo]] else mean_shape[[hi]] - mean_shape[[lo]], level = lv)
    tab_number(rows, "season", "peak_position", hi, level = lv)
    tab_number(rows, "season", "peak_value", mean_shape[[hi]], level = lv)
    tab_number(rows, "season", "trough_position", lo, level = lv)
    tab_number(rows, "season", "trough_value", mean_shape[[lo]], level = lv)
    tab_text(rows, "season", "position_kind", pos$kind, level = lv)
    tab_text(rows, "season", "peak_label", pos$codes[[hi]], level = lv)
    tab_text(rows, "season", "trough_label", pos$codes[[lo]], level = lv)
    shapes[[lv]] <- list(kind = pos$kind, codes = pos$codes, peak = hi, trough = lo, mean = unname(mean_shape),
                         low = unname(vapply(by, min, 0)), high = unname(vapply(by, max, 0)), peak_value = mean_shape[[hi]],
                         trough_value = mean_shape[[lo]], cycles = s$n %/% p)
  }
  shapes
}

# ── the charts ──────────────────────────────────────────────────────────────────────────────────────────────────
# The series of the chart «components» (what was decomposed, the trend, each seasonal, the remainder), the seasonally
# adjusted series, the remainder as points, its autocorrelations with the node's own bound, its histogram, and for each
# period the sub-series (the mean of the seasonal at each position, and the lowest and highest cycle there). A long series
# is thinned by envelope (ts_envelope: each bucket's lowest and highest), never sampled; the numbers of the output and of
# the tests always use every instant. `curve` rows: level = the chart, level_2 = the series.
dec_charts <- function(task, rows, ctx) {
  max_points <- tab_limit(task, "max_chart_points")
  o <- ctx$o
  s <- ctx$s
  fit <- ctx$fit
  curve <- function(chart, name, y) {
    keep <- ts_envelope(y, max_points)
    tab_points(rows, "curve", chart, "y", x = s$x0[keep], y = y[keep], level_2 = name)
    keep
  }
  curve("components", "observed", s$z)
  curve("components", "trend", fit$trend)
  for (i in seq_along(ctx$periods)) curve("components", paste0("seasonal_", ctx$periods[[i]]), fit$seasonal[[i]])
  keep <- curve("components", "remainder", fit$remainder)
  curve("adjusted", "deseasonalized", ctx$deseasonalized)
  tab_points(rows, "point", "remainder", "y", x = s$x0[keep], y = fit$remainder[keep])
  for (lv in names(ctx$shapes)) {
    sh <- ctx$shapes[[lv]]
    at <- ts_envelope(sh$mean, max_points)
    for (stat in c("mean", "low", "high")) {
      tab_points(rows, "curve", "subseries", stat, x = at, y = sh[[stat]][at], level_2 = paste0("subseries_", lv))
    }
  }
  acf <- attr(ctx$checks, "acf")
  if (length(acf)) {
    k <- seq_along(acf)
    tab_points(rows, "lag", "resid_acf", "acf", x = k, y = acf)
    tab_number(rows, "lag", "bound", stats::qnorm(1 - o$alpha / 2) / sqrt(length(.ts_values(fit$remainder))), level = "resid_acf")
  }
  e <- .ts_values(fit$remainder)
  e <- e[is.finite(e)]
  if (length(e)) {
    h <- tab_hist(e)
    tab_points(rows, "hist", "remainder", "count", x = h$x0, y = h$count, x1 = h$x1)
  }
  invisible(rows)
}

# ── the output ──────────────────────────────────────────────────────────────────────────────────────────────────
# `components`: one row per instant of the span; `time` in the input field's own Arrow type (ts_time_array), the filled
# instants with `observed` empty and `filled` true; `transformed` only with a transform.
dec_output <- function(ctx) {
  s <- ctx$s
  fit <- ctx$fit
  cols <- list(time = ts_time_array(s$times, s$type), observed = s$y)
  if (!identical(ctx$o$transform, "none")) cols$transformed <- s$z
  cols$filled <- s$filled
  cols$trend <- fit$trend
  for (i in seq_along(ctx$periods)) cols[[paste0("seasonal_", ctx$periods[[i]])]] <- fit$seasonal[[i]]
  cols$remainder <- fit$remainder
  cols$deseasonalized <- ctx$deseasonalized
  for (k in names(cols)) if (is.numeric(cols[[k]])) cols[[k]][!is.finite(cols[[k]])] <- NA_real_
  do.call(arrow::arrow_table, cols)
}
