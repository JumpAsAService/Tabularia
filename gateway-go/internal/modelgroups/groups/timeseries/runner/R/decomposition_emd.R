# The decomposition's EMD (docs/specs/ts-decomposition.md, «EMD — Huang et al. (1998), by Rlibeemd::emd»; lot D3):
# fit_decomposition_emd(task, params), which fit_decomposition (decomposition.R) calls for method = emd; the options
# are read again here from the task, the period implied by the frequency when none is given (`params`, the caller's
# reading, is not needed).
#
# x = c₁ + c₂ + … + c_K + r. Rlibeemd 1.4.4 (libeemd in C: Luukko, Helske & Räsänen 2016) sifts the IMFs: envelopes by
# cubic splines with not-a-knot ends through the maxima and through the minima, the ends extended by the line
# through the two nearest extrema when that is more extreme (Wu & Huang 2009), a sifting stopped by the S-number
# rule or after max_siftings. Nothing is random: Rlibeemd::emd is eemd with one member and no noise (EEMD and
# CEEMDAN are never called).
#
# Where it stops. libeemd never stops by itself: emd(x, num_imfs = M) always returns M columns (M − 1 «IMFs» and the
# residual) and, once the residual has no oscillation left, goes on sifting it into small modes of one extremum,
# which are not IMFs (spec, «As built / D0»). The runner asks for M = min(max_imf, ⌊log₂ n⌋ − 1) + 1 columns
# (⌊log₂ n⌋, libeemd's emd_num_imfs, is the number of modes a dyadic filter bank gives: Wu & Huang 2004, 2009) and
# stops the decomposition at the first k ≥ 0 whose residual x − c₁ − … − c_k has at most one extremum — Huang et
# al.'s (1998) «the residue becomes a monotonic function from which no more IMF can be extracted», in the form of
# Huang & Wu (2008) and Wu & Huang (2009): «a monotonic function or a function with only one extremum» — and adds
# every later column back into the residue. n_imfs is that k; the output's imf_<j> columns after it are empty. When
# no k qualifies, n_imfs is M − 1 and the fact residue_monotonic fails.
#
# The remainder the checks test (spec, «The remainder of EMD»): with noise_imfs = k > 0, R = c₁ + … + c_k (the
# fastest modes, read as noise), T = r, the oscillations S = c_{k+1} + … + c_K; Ljung–Box, ADF and KPSS test R and the
# strengths compare R with T and with S. With noise_imfs = 0 EMD separates no noise: Ljung–Box is not computed, ADF and
# KPSS test the oscillating part x − r, there are no strengths and no remainder charts. The seasonality test is the
# data's (ts_seasonality on the series), with `period` when given or implied by the frequency; the season's shape is
# that of S (x − r with noise_imfs = 0) averaged by position.
#
# Measures, never verdicts: per IMF the mean period n / maxima, the variance and energy shares, extrema and zero
# crossings; the orthogonality index. Facts (counts, by definition): the IMF condition |extrema − zero crossings| ≤ 1
# (Huang et al. 1998) and the residue's at most one extremum. Extrema are counted as libeemd counts them (a change of
# the slope's sign, a flat run once), the ends left out.

EMD_PACKAGES <- c("forecast", "Rlibeemd", "urca", "tseries")
EMD_MIN_VALUES <- 4L     # libeemd's emd_num_imfs(n) ≥ 2 (one IMF and the residual) from n = 4
EMD_BOUNDARY <- "wu_huang_2009"

# ── counts ──────────────────────────────────────────────────────────────────────
.emd_slopes <- function(x) {
  d <- sign(diff(as.numeric(x)))
  d[d != 0]
}

# Inner extrema: the slope's sign changes, a flat run counted once (libeemd's emd_find_extrema without its ends).
emd_extrema <- function(x) {
  d <- .emd_slopes(x)
  if (length(d) < 2L) return(0L)
  sum(d[-1L] != d[-length(d)])
}

emd_maxima <- function(x) {
  d <- .emd_slopes(x)
  if (length(d) < 2L) return(0L)
  sum(d[-length(d)] > 0 & d[-1L] < 0)
}

emd_zero_crossings <- function(x) {
  s <- sign(as.numeric(x))
  s <- s[s != 0]
  if (length(s) < 2L) return(0L)
  sum(s[-1L] != s[-length(s)])
}

# ── the decomposition ───────────────────────────────────────────────────────────
# z: the series (complete, transformed). Returns imfs (n × n_imfs), residue, n_imfs, columns (M asked of libeemd),
# most (the IMFs libeemd's emd_num_imfs allows this n), cut (TRUE when the residue still oscillates).
emd_decompose <- function(z, max_imf, s_number, max_siftings) {
  n <- length(z)
  most <- as.integer(Rlibeemd::emd_num_imfs(n)) - 1L
  columns <- min(as.integer(max_imf), most) + 1L
  m <- Rlibeemd::emd(z, num_imfs = columns, S_number = as.integer(s_number), num_siftings = as.integer(max_siftings))
  m <- matrix(as.numeric(m), nrow = n)
  # the residuals x − c₁ − … − c_k, subtracted one by one as libeemd does: the first with at most one extremum ends it
  res <- as.numeric(z)
  k <- 0L
  while (k < columns - 1L && emd_extrema(res) > 1L) {
    k <- k + 1L
    res <- res - m[, k]
  }
  list(imfs = m[, seq_len(k), drop = FALSE], residue = res, n_imfs = k, columns = columns, most = most,
       residue_extrema = emd_extrema(res), cut = emd_extrema(res) > 1L)
}

# The measures of one mode: mean period n / maxima (none for the residue, or a mode without a maximum), its shares of
# the series' variance and energy, its extrema and zero crossings.
emd_mode_measures <- function(c, z, residue = FALSE) {
  maxima <- emd_maxima(c)
  period <- if (residue || maxima == 0L) NA_real_ else length(c) / maxima
  list(mean_period = period, mean_period_time = period, variance_share = stats::var(c) / stats::var(z),
       energy_share = sum(c * c) / sum(z * z), extrema = emd_extrema(c), zero_crossings = emd_zero_crossings(c))
}

# IO = Σ_t Σ_{j≠k} c_j(t) c_k(t) / Σ_t x(t)² over the IMFs and the residue (the cross products of the Gram matrix).
emd_orthogonality <- function(imfs, residue, z) {
  modes <- cbind(imfs, residue)
  g <- crossprod(modes)
  (sum(g) - sum(diag(g))) / sum(as.numeric(z)^2)
}

# ── the options ─────────────────────────────────────────────────────────────────
.emd_options <- function(task) {
  defaults <- tab_model_spec("decomposition")$defaults
  p <- task$params %||% list()
  get <- function(key) p[[key]] %||% defaults[[key]]
  frequency <- get("frequency")
  period <- get("period")
  if (!is.null(period)) period <- as.integer(period)
  if (is.null(period) && !is.null(frequency) && frequency %in% names(TS_SEASONS)) period <- TS_SEASONS[[frequency]]
  list(time = get("time"), value = get("value"), frequency = frequency, period = period,
       max_imf = as.integer(get("max_imf")), s_number = as.integer(get("s_number")),
       max_siftings = as.integer(get("max_siftings")), noise_imfs = as.integer(get("noise_imfs")),
       transform = get("transform") %||% "none", box_cox_lambda = get("box_cox_lambda") %||% "auto",
       missing = get("missing") %||% "refuse", seasonality_test = get("seasonality_test") %||% "friedman",
       lb_lag = get("lb_lag") %||% "auto", alpha = as.numeric(get("alpha")), output = get("output") %||% "components")
}

# ── the fit ─────────────────────────────────────────────────────────────────────
fit_decomposition_emd <- function(task, params = NULL) {
  o <- .emd_options(task)
  s <- ts_series(task, o$time, o$value, o$frequency, o$missing, o$transform, o$box_cox_lambda, o$period)
  z <- s$z
  n <- s$n
  if (n < EMD_MIN_VALUES) {
    tab_refuse("too_short", paste0("the series has ", n, " values; EMD needs at least ", EMD_MIN_VALUES, ": a longer series"),
               list(n = n, min = EMD_MIN_VALUES))
  }
  if (all(z == z[[1L]])) {
    tab_refuse("constant", paste0("the column «", o$value, "» has the same value everywhere: there is nothing to decompose"),
               list(column = o$value))
  }
  d <- emd_decompose(z, o$max_imf, o$s_number, o$max_siftings)
  k <- o$noise_imfs
  if (k > d$n_imfs) {
    tab_refuse("noise_imfs_above", paste0("EMD found ", d$n_imfs, " IMFs; ", k, " cannot be noise: choose at most ", d$n_imfs),
               list(found = d$n_imfs, noise_imfs = k))
  }
  imfs <- d$imfs
  sum_of <- function(j) if (length(j)) rowSums(imfs[, j, drop = FALSE]) else rep(0, n)
  noise <- if (k > 0L) sum_of(seq_len(k)) else NULL
  oscillation <- sum_of(setdiff(seq_len(d$n_imfs), seq_len(k)))  # S: the IMFs not read as noise
  tested <- if (k > 0L) noise else sum_of(seq_len(d$n_imfs))      # R, or x − r
  p <- o$period
  periods <- if (is.null(p)) integer() else p

  rows <- tab_rows_start(task)
  r <- list(task = task, o = o, s = s, d = d, k = k, periods = periods, p = p, n = n)

  # ── the model ──
  tab_text(rows, "model", "formula", .emd_formula(o, d$n_imfs))
  tab_text(rows, "model", "software", tab_software(EMD_PACKAGES))
  tab_text(rows, "model", "method", "emd")
  tab_text(rows, "model", "frequency", o$frequency)
  tab_text(rows, "model", "transform", o$transform)
  tab_text(rows, "model", "time_kind", s$kind)
  tab_text(rows, "model", "missing", o$missing)
  tab_text(rows, "model", "seasonality_test", o$seasonality_test)
  if (length(periods)) tab_text(rows, "model", "periods", as.character(p))
  tab_text(rows, "model", "boundary", EMD_BOUNDARY)
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "n_missing", s$n_missing)
  tab_number(rows, "model", "n_filled", s$n_filled)
  tab_number(rows, "model", "first", s$x0[[1L]])
  tab_number(rows, "model", "last", s$x0[[n]])
  tab_number(rows, "model", "alpha", o$alpha)
  if (!is.null(s$lambda)) tab_number(rows, "model", "box_cox_lambda", s$lambda)
  h <- ts_lb_lag(o$lb_lag, n, periods, tab_limit(task, "max_lb_lags"))
  tab_number(rows, "model", "lb_lag_used", h)
  tab_number(rows, "model", "max_imf", o$max_imf)
  tab_number(rows, "model", "n_imfs", d$n_imfs)
  tab_number(rows, "model", "s_number", o$s_number)
  tab_number(rows, "model", "max_siftings", o$max_siftings)
  tab_number(rows, "model", "noise_imfs", k)
  if (s$n_missing > 0L) tab_note(rows, paste0("@rows n=", n, " missing=", s$n_missing))

  # ── the season (a period given or implied, two complete cycles) ──
  season <- NULL
  if (length(periods) && n %/% p >= 2L) {
    season <- emd_season(s, oscillation, p)
    for (stat in c("cycles", "amplitude", "peak_position", "peak_value", "trough_position", "trough_value")) {
      tab_number(rows, "season", stat, season[[stat]], level = as.character(p))
    }
    tab_text(rows, "season", "position_kind", season$position_kind, level = as.character(p))
    tab_text(rows, "season", "peak_label", season$peak_label, level = as.character(p))
    tab_text(rows, "season", "trough_label", season$trough_label, level = as.character(p))
  }

  # ── the IMFs: measures ──
  measures <- lapply(seq_len(d$n_imfs), function(j) emd_mode_measures(imfs[, j], z))
  residue_measures <- emd_mode_measures(d$residue, z, residue = TRUE)
  stats_ <- c("mean_period", "mean_period_time", "variance_share", "energy_share", "extrema", "zero_crossings")
  for (j in seq_len(d$n_imfs)) for (stat in stats_) tab_number(rows, "imf", stat, measures[[j]][[stat]], level = paste0("imf_", j))
  for (stat in stats_) tab_number(rows, "imf", stat, residue_measures[[stat]], level = "residue")

  # ── the checks ──
  checks <- list()
  if (length(periods)) checks$seasonality <- ts_seasonality(z, p, o$seasonality_test, o$alpha)
  lag_max <- min(max(h, tab_limit(task, "max_lags")), tab_limit(task, "max_lb_lags"))
  acf <- if (k > 0L) ts_acf(noise, lag_max) else NULL
  checks$ljung_box <- if (k > 0L) ts_ljung_box(noise, h, o$alpha, acf) else
    .ts_not_computed("ljung_box", paste0("the remainder is not autocorrelated up to lag ", h),
                     "EMD does not separate a noise: choose how many of the fastest IMFs are noise", "emd_no_noise", lag = h)
  if (k > 0L) checks$largest_acf <- ts_largest_acf(acf, h)
  checks$adf <- ts_adf(tested, o$alpha)
  checks$kpss <- ts_kpss(tested, o$alpha)
  if (k > 0L) {
    checks$trend_strength <- ts_measure("trend_strength", ts_strength(d$residue, noise))
    if (length(periods)) checks$seasonal_strength <- ts_measure("seasonal_strength", ts_strength(oscillation, noise), level_2 = as.character(p))
  }
  for (j in seq_len(d$n_imfs)) {
    ok <- abs(measures[[j]]$extrema - measures[[j]]$zero_crossings) <= 1L
    checks[[paste0("imf_condition_", j)]] <- list(
      id = "imf_condition", level_2 = paste0("imf_", j), kind = "fact", h0 = "the mode is an intrinsic mode function",
      verdict = if (ok) "ok" else "failed", advice_id = "imf_condition_failed",
      advice = paste0("sifting stopped before IMF ", j, " was one: raise max_siftings or the S-number"))
  }
  checks$residue_monotonic <- list(
    id = "residue_monotonic", kind = "fact", h0 = "the residue has at most one extremum", count = d$residue_extrema,
    verdict = if (d$cut) "failed" else "ok",
    advice_id = if (d$columns - 1L < o$max_imf) "emd_most_imfs" else "residue_monotonic_failed",
    advice = if (d$columns - 1L < o$max_imf) {
      paste0("a series of ", n, " values gives at most ", d$most, " IMFs (libeemd's ⌊log2 n⌋ − 1): the residue still oscillates; ",
             "a longer series")
    } else "max_imf IMFs were not enough: the residue still oscillates; raise max_imf")
  checks$orthogonality <- ts_measure("orthogonality", emd_orthogonality(imfs, d$residue, z))
  for (check in checks) ts_check_rows(rows, check)

  # ── the charts ──
  series <- d$n_imfs + 2L
  after <- 400L + (if (length(periods)) 3L * p else 0L) + (if (k > 0L) lag_max + 32L else 0L)
  per <- tab_chart_points(task, rows, series = series + (k > 0L), after = after)
  chart <- function(y, name) {
    at <- ts_envelope(y, per)
    tab_points(rows, "curve", "imfs", "y", x = s$x0[at], y = y[at], level_2 = name)
  }
  chart(s$z, "observed")  # what was decomposed: the IMFs and the residue add up to it
  for (j in seq_len(d$n_imfs)) chart(imfs[, j], paste0("imf_", j))
  chart(d$residue, "residue")
  if (!is.null(season)) {
    at <- ts_envelope(season$shape$mean, tab_limit(task, "max_chart_points"))
    for (stat in c("mean", "low", "high")) {
      tab_points(rows, "curve", "subseries", stat, x = at, y = season$shape[[stat]][at], level_2 = paste0("subseries_", p))
    }
  }
  if (k > 0L) {
    at <- ts_envelope(noise, per)
    tab_points(rows, "point", "remainder", "y", x = s$x0[at], y = noise[at])
    hist <- tab_hist(noise)
    tab_points(rows, "hist", "remainder", "count", x = hist$x0, y = hist$count, x1 = hist$x1)
    if (length(acf)) {
      tab_points(rows, "lag", "resid_acf", "acf", x = seq_along(acf), y = acf)
      tab_number(rows, "lag", "bound", stats::qnorm(1 - o$alpha / 2) / sqrt(n), level = "resid_acf")
    }
  }

  # ── the reading ──
  r$checks <- checks
  r$season <- season
  r$measures <- measures
  emd_write_reading(rows, emd_reading(r))

  output <- if (identical(o$output, "components")) emd_output(s, o, d) else NULL
  list(rows = rows, output = output, card = NULL, native = NULL)
}

# «signal = imf_1 + imf_2 + residue · EMD» (log(signal), boxcox(signal) with a transform).
.emd_formula <- function(o, n_imfs) {
  left <- switch(o$transform, log = paste0("log(", o$value, ")"), box_cox = paste0("boxcox(", o$value, ")"), o$value)
  modes <- if (n_imfs <= 3L) paste0("imf_", seq_len(n_imfs)) else c("imf_1", "…", paste0("imf_", n_imfs))
  paste0(left, " = ", paste(c(modes, "residue"), collapse = " + "), " · EMD")
}

# The season of the oscillations: their mean, lowest and highest value at each position of a cycle of p (positions
# named by the calendar, ts_positions), the highest and lowest mean, the amplitude, the complete cycles.
emd_season <- function(s, oscillation, p) {
  pos <- ts_positions(s$times, s$kind, s$frequency, p)
  at <- factor(pos$position, levels = seq_len(p))
  mean_ <- as.numeric(tapply(oscillation, at, mean))
  low <- as.numeric(tapply(oscillation, at, min))
  high <- as.numeric(tapply(oscillation, at, max))
  peak <- which.max(mean_)
  trough <- which.min(mean_)
  list(cycles = s$n %/% p, amplitude = mean_[[peak]] - mean_[[trough]], peak_position = peak, peak_value = mean_[[peak]],
       trough_position = trough, trough_value = mean_[[trough]], position_kind = pos$kind, peak_label = pos$codes[[peak]],
       trough_label = pos$codes[[trough]], shape = list(mean = mean_, low = low, high = high))
}

# The components output: time (the input's own type), observed, transformed (with a transform), filled,
# imf_1 … imf_<max_imf> (empty after the last IMF found), residue.
emd_output <- function(s, o, d) {
  cols <- list(time = ts_time_array(s$times, s$type), observed = as.numeric(s$y))
  if (!identical(o$transform, "none")) cols$transformed <- as.numeric(s$z)
  cols$filled <- s$filled
  for (j in seq_len(o$max_imf)) cols[[paste0("imf_", j)]] <- if (j <= d$n_imfs) d$imfs[, j] else rep(NA_real_, s$n)
  cols$residue <- d$residue
  do.call(arrow::arrow_table, cols)
}
