# A/B test planning (docs/specs/ab-testing.md, «The node ab_plan»): fit_ab_plan(task).
#
# The history of the metric gives the baseline m0 and the per-unit standard deviation s0 (ab_read's sufficient
# statistics, with one variant standing for the whole history); then, with the normal approximation of the test
# ab_test runs, so that plan and test agree (ab_compare's mde is the same equation):
#
#   N = (z_a + z_b)^2 (v_c / w_c + v_t / w_t) / (m0 L)^2      the units of the whole experiment, for one comparison
#   n_c = ceil(N w_c), n_t = ceil(N w_t), total = n_c + (arms - 1) n_t, days = ceil(total / daily units)
#   mde: the same equation solved for L, closed for a continuous and a ratio metric, uniroot() for a binary one
#
# w: the shares of the whole experiment (control_share, the rest equally); v: s0^2 (binary: the treatment's
# p1 (1 - p1), p1 = p0 (1 + L)); alpha' = alpha / (arms - 1) unless the correction is `none`; CUPED: v x (1 - r).
# Every number is a measure, nothing here is a test. No random number is drawn. It returns no card.

AB_PLAN_GRID <- 30L

.abp_ceiling <- function(x) ceiling(x - 1e-9)

.abp_context <- function(task) {
  p <- tab_model_spec("ab_plan")$defaults
  for (k in names(task$params)) p[k] <- list(task$params[[k]])
  num <- function(x) as.numeric(x %||% NA_real_)
  arms <- as.integer(num(p$arms))
  wc <- if (is.na(num(p$control_share))) 1 / arms else num(p$control_share)
  correction <- p$correction %||% "holm"
  alpha <- num(p$alpha)
  a1 <- if (identical(correction, "none")) alpha else alpha / (arms - 1L)
  alt <- p$alternative %||% "two_sided"
  list(alpha = alpha, power = num(p$power), alternative = alt, correction = correction, arms = arms, wc = wc, wt = (1 - wc) / (arms - 1L),
       a1 = a1, za = if (alt == "two_sided") stats::qnorm(1 - a1 / 2) else stats::qnorm(1 - a1), zb = stats::qnorm(num(p$power)),
       solve_for = p$solve_for %||% "sample_size", min_lift = num(p$min_lift), units = num(p$units_per_variant), days = num(p$days),
       traffic = if (.ab_given(p$date)) num(p$traffic_share) else 1, covariate = if (.ab_given(p$covariate)) p$covariate else NULL,
       date = if (.ab_given(p$date)) p$date else NULL, output = p$output %||% "results", params = p)
}

# The history: ab_read's own steps (the columns, the rows that count, the covariate, the cap, the days) with the
# whole table as one variant.
ab_plan_read <- function(task) {
  p <- task$params
  asked <- task
  asked$params$variant <- "(all)"
  cols <- ab_columns(asked)
  cols$variant <- NULL
  df <- .ab_input(task, cols)
  type <- p$metric_type %||% task$variant
  shape <- p$input_shape %||% "units"
  var <- list(labels = "history", values = "history", type = NULL, k = 1L, control = 1L, expected = 1, code = rep(1L, nrow(df)))
  d <- if (identical(shape, "aggregated")) .ab_read_aggregated(task, df, cols, var, type) else .ab_read_units(task, df, cols, var, type)
  d$shape <- shape
  d$metric_type <- type
  d$variants <- var[c("labels", "values", "type", "k", "control", "expected")]
  d
}

# m0, s0 (the unadjusted ones, whatever the covariate), the share of the variance CUPED takes out (the measured one:
# rho^2 when every unit has the covariate, less where empty values were filled), the days and the units a day.
.abp_baseline <- function(d, ctx) {
  est <- ab_estimates(d)
  cu <- est$cuped
  m0 <- if (is.null(cu)) est$m else cu$raw$m
  s0 <- if (is.null(cu)) est$sd else cu$raw$sd
  type <- d$metric_type
  if (!is.finite(m0) || m0 == 0) {
    tab_refuse("baseline_zero", "the history's mean is 0: a lift is a share of the baseline, and a share of 0 is nothing to detect", list(mean = m0))
  }
  if (!(s0 > 0)) tab_refuse("variance_zero", "the metric has the same value on every unit of the history: nothing to plan", list())
  if (!is.null(cu) && !is.finite(cu$rho)) tab_refuse("covariate_constant", paste0("the covariate «", ctx$covariate, "» has no spread: CUPED needs one"))
  b <- list(type = type, m0 = m0, s0 = s0, n = sum(d$n), n_used = sum(d$n_used), est = est, reduction = if (is.null(cu)) NULL else cu$reduction[[1L]],
            rho = if (is.null(cu)) NULL else cu$rho)
  if (!is.null(ctx$date)) {
    b$history_days <- d$days$span
    b$daily <- b$n_used / d$days$span * ctx$traffic  # the units the experiment gets each day
  }
  b
}

# v_c and v_t of one unit, for a lift L (a vector): a binary treatment's variance moves with its rate.
.abp_variances <- function(b, L, scale = 1) {
  list(c = rep(b$s0^2 * scale, length(L)), t = if (b$type == "binary") {
    p1 <- b$m0 * (1 + L)
    ifelse(p1 > 1 + 1e-12, NA_real_, pmin(p1, 1) * (1 - pmin(p1, 1)) * scale)  # a rate cannot pass 1 (a rounding at the edge is 1)
  } else rep(b$s0^2 * scale, length(L)))
}

# N: the units of the whole experiment that detect the lift L (one comparison); NA where a rate would pass 1.
.abp_total <- function(b, ctx, L, scale = 1) {
  v <- .abp_variances(b, L, scale)
  (ctx$za + ctx$zb)^2 * (v$c / ctx$wc + v$t / ctx$wt) / (b$m0 * L)^2
}

# The lift detectable with n_c units of the control and n_t of a treatment.
.abp_mde <- function(b, ctx, n_c, n_t, scale = 1) {
  k <- ctx$za + ctx$zb
  if (b$type != "binary") return(k * sqrt(b$s0^2 * scale * (1 / n_c + 1 / n_t)) / abs(b$m0))
  f <- function(L) {
    v <- .abp_variances(b, L, scale)
    abs(b$m0) * L - k * sqrt(v$c / n_c + v$t / n_t)
  }
  top <- (1 - b$m0) / b$m0
  if (f(top) < 0) return(NA_real_)
  stats::uniroot(f, c(0, top), tol = 1e-12)$root
}

# The sizes that detect L: the units of the control and of a treatment, in all, and the days.
.abp_sizes <- function(b, ctx, L, scale = 1) {
  N <- .abp_total(b, ctx, L, scale)
  n_t <- .abp_ceiling(N * ctx$wt)
  n_c <- .abp_ceiling(N * ctx$wc)
  total <- n_c + (ctx$arms - 1L) * n_t
  days <- if (is.null(b$daily)) NULL else .abp_ceiling(total / b$daily)
  list(n_t = n_t, n_c = n_c, total = total, days = days, weeks = if (is.null(days)) NULL else ceiling(days / 7))
}

fit_ab_plan <- function(task) {
  jit <- compiler::enableJIT(0L)
  on.exit(compiler::enableJIT(jit), add = TRUE)
  ctx <- .abp_context(task)
  task$params <- ctx$params  # with the catalogue's defaults: `p$unit` would read `units_per_variant` of a task that gives no `unit`
  d <- ab_plan_read(task)
  b <- .abp_baseline(d, ctx)
  dated <- !is.null(ctx$date)
  scale <- if (is.null(b$reduction)) NULL else 1 - b$reduction
  if (ctx$solve_for == "mde_for_days" && !dated) tab_refuse("days_need_date", "the lift detectable in a number of days needs the column of the day each unit first appeared ('date')")
  # the answer: the lift L the report is about, the units it uses, and the sizes
  if (ctx$solve_for == "sample_size") {
    L <- ctx$min_lift
    n_t <- NA_real_
    n_c <- NA_real_
  } else {
    n_t <- if (ctx$solve_for == "mde_for_units") ctx$units else ctx$days * b$daily * ctx$wt
    n_c <- n_t * ctx$wc / ctx$wt
    L <- .abp_mde(b, ctx, n_c, n_t)
    if (is.na(L)) tab_refuse("mde_unreachable", "with so few units not even a rise of the rate to 100 % is detected: plan more units", list(units = n_t))
  }
  if (b$type == "binary" && b$m0 * (1 + L) > 1) {
    tab_refuse("lift_too_large", paste0("a rate of ", format(signif(b$m0, 6), scientific = FALSE), " cannot rise by ", format(signif(L, 6), scientific = FALSE), ": it would pass 100 %"), list(lift = L))
  }
  # the units as the table shows them: whole units (a control's share of a treatment's units, or of the days' traffic, rounded as
  # the sizes are: up for the units asked, down for the units the days bring)
  at <- .abp_sizes(b, ctx, L)
  if (ctx$solve_for == "sample_size") {
    n_t_shown <- at$n_t
    n_c_shown <- at$n_c
  } else if (ctx$solve_for == "mde_for_units") {
    n_t_shown <- n_t
    n_c_shown <- .abp_ceiling(n_c)
  } else {
    n_t_shown <- floor(n_t)
    n_c_shown <- floor(n_c)
  }
  if (ctx$solve_for == "sample_size") {
    n_t <- at$n_t
    n_c <- at$n_c
  }
  total <- n_c_shown + (ctx$arms - 1L) * n_t_shown
  days <- if (!dated) NULL else if (ctx$solve_for == "mde_for_days") ctx$days else .abp_ceiling(total / b$daily)
  cu <- if (is.null(scale)) NULL else .abp_sizes(b, ctx, L, scale)
  mde_cuped <- if (is.null(scale)) NULL else .abp_mde(b, ctx, n_c, n_t, scale)
  # the grid: 30 lifts from L/4 to 4L on a log scale and the lift of the answer, the ones a rate can make
  grid <- exp(seq(log(L / 4), log(4 * L), length.out = AB_PLAN_GRID))
  if (b$type == "binary") grid <- grid[b$m0 * (1 + grid) <= 1 + 1e-12]
  lifts <- sort(c(grid, L))
  labels <- tab_label(signif(lifts, 4))
  asked <- match(L, lifts)
  grid_at <- .abp_sizes(b, ctx, lifts)
  grid_cuped <- if (is.null(scale)) NULL else .abp_sizes(b, ctx, lifts, scale)

  rows <- tab_rows_start(task)
  .abp_model_rows(rows, task, ctx, d, b, L, n_t_shown, n_c_shown, total, days, cu, mde_cuped)
  .abp_grid_rows(rows, b, lifts, labels, asked, grid_at, grid_cuped)
  .abp_curves(rows, ctx, d, b, lifts, n_c, n_t, grid_at, days)
  ab_write_reading(rows, .abp_reading(ctx, b, L, n_t_shown, total, days, cu))
  output <- if (identical(ctx$output, "plan")) .abp_output(b, lifts, asked, grid_at, grid_cuped) else NULL
  list(rows = rows, output = output, card = NULL, native = NULL)
}

.abp_model_rows <- function(rows, task, ctx, d, b, L, n_t, n_c, total, days, cu, mde_cuped) {
  sec <- "model"
  dated <- !is.null(ctx$date)
  tab_text(rows, sec, "formula", paste0("plan for ", b$type, " metric · ", ctx$arms, " variants · ", ctx$correction,
                                        if (!is.null(ctx$covariate)) paste0(" · CUPED on «", ctx$covariate, "»") else ""))
  tab_text(rows, sec, "software", tab_software("arrow"))
  tab_text(rows, sec, "metric_type", b$type)
  tab_text(rows, sec, "input_shape", d$shape)
  tab_text(rows, sec, "solve_for", ctx$solve_for)
  tab_text(rows, sec, "alternative", ctx$alternative)
  tab_text(rows, sec, "correction", ctx$correction)
  if (!is.null(b$est$cuped)) tab_text(rows, sec, "covariate_missing", b$est$cuped$mode)
  if (dated && !is.na(days) && days > b$history_days) tab_text(rows, sec, "history_short", ab_line("history_short", history_days = b$history_days, planned_days = days))
  num <- c(alpha = ctx$alpha, n = b$n, n_missing = sum(d$n_missing), min_lift = if (ctx$solve_for == "sample_size") L, arms = ctx$arms,
           control_share = ctx$wc, traffic_share = if (dated) ctx$traffic, baseline_mean = b$m0, baseline_sd = b$s0, n_history = b$n_used,
           history_days = b$history_days, daily_units = b$daily, alpha_per_comparison = ctx$a1, n_per_variant = n_t, n_control = n_c,
           n_total = total, days = days, weeks = if (dated) ceiling(days / 7), mde = L, mde_absolute = abs(b$m0) * L, power = ctx$power)
  if (!is.null(cu)) {
    num <- c(num, covariate_correlation = b$rho, variance_reduction = b$reduction, n_covariate_filled = sum(d$covariate$n_filled),
             n_covariate_excluded = sum(d$covariate$n_excluded), n_per_variant_cuped = cu$n_t, n_total_cuped = cu$total, days_cuped = cu$days,
             mde_cuped = mde_cuped)
  }
  if (!is.null(d$cap)) num <- c(num, cap_value = d$cap$value, n_capped = sum(d$cap$n_capped))
  for (k in names(num)) tab_number(rows, sec, k, num[[k]])
  for (note in unname(unlist(d$notes))) tab_note(rows, note)
}

.abp_grid_rows <- function(rows, b, lifts, labels, asked, at, cu) {
  for (i in seq_along(lifts)) {
    v <- c(lift = lifts[[i]], mde_absolute = abs(b$m0) * lifts[[i]], n_per_variant = at$n_t[[i]], n_control = at$n_c[[i]], n_total = at$total[[i]],
           days = at$days[[i]], weeks = at$weeks[[i]], n_per_variant_cuped = cu$n_t[[i]], days_cuped = cu$days[[i]], asked = as.numeric(i == asked))
    for (k in names(v)) tab_number(rows, "plan", k, v[[k]], level = labels[[i]])
  }
}

# Power against the lift at the units of the answer; the units of the plan against the lift; the smallest lift
# against the days (a whole number of weeks of the history's traffic, up to twice the answer's days).
.abp_curves <- function(rows, ctx, d, b, lifts, n_c, n_t, at, days) {
  v <- .abp_variances(b, lifts)
  se <- sqrt(v$c / n_c + v$t / n_t)
  tab_points(rows, "curve", "power_lift", "y", x = lifts, y = ab_power_at(lifts, se, b$m0, ctx$za, ctx$alternative == "two_sided"))
  tab_points(rows, "curve", "n_lift", "y", x = lifts, y = at$total)
  if (!is.null(b$daily)) {
    top <- max(28, 2 * days)
    step <- 7 * ceiling(top / 7 / AB_PLAN_GRID)
    x <- step * seq_len(min(AB_PLAN_GRID, floor(top / step)))
    y <- vapply(x, function(day) .abp_mde(b, ctx, day * b$daily * ctx$wc, day * b$daily * ctx$wt), 0)
    tab_points(rows, "curve", "mde_days", "y", x = x[!is.na(y)], y = y[!is.na(y)])  # no point where the days cannot detect any lift
  }
  h <- d$hist
  if (!is.null(h)) tab_points(rows, "hist", "metric", "count", x = c(NA_real_, h$breaks), y = h$counts[1L, ], x1 = c(h$breaks, NA_real_))
}

# The reading: the answer, the baseline, CUPED, the correction, the method (the ids of the words in experiments.json,
# ab_plan.json). A sentence that needs the days of a history is said where the table has a date.
.abp_reading <- function(ctx, b, L, n_t, total, days, cu) {
  dated <- !is.null(ctx$date)
  plan <- switch(ctx$solve_for,
    sample_size = if (dated) ab_line("plan_size", units = n_t, total = total, days = days, daily = b$daily, lift = L, power = ctx$power,
                                     alpha = ctx$alpha, baseline = b$m0)
                  else ab_line("plan_size_no_days", units = n_t, total = total, lift = L, power = ctx$power, alpha = ctx$alpha, baseline = b$m0),
    mde_for_units = ab_line("plan_mde_units", units = n_t, mde = L, power = ctx$power, alpha = ctx$alpha, baseline = b$m0),
    mde_for_days = ab_line("plan_mde_days", days = ctx$days, daily = b$daily, units = n_t, mde = L, power = ctx$power, alpha = ctx$alpha))
  correction <- if (identical(ctx$correction, "none")) ab_line("correction_none", alpha = ctx$alpha) else
    ab_line(paste0("correction_", ctx$correction), arms = ctx$arms, alpha = ctx$alpha, alpha_per_comparison = ctx$a1)
  list(ab_block("plan", plan),
       ab_block("baseline", if (dated) ab_line("baseline_read", mean = b$m0, sd = b$s0, history_days = b$history_days, units = b$n_used) else NA_character_),
       ab_block("cuped", if (is.null(cu) || !dated) NA_character_ else ab_line("cuped_plan", reduction = b$reduction, units = cu$total, days = cu$days)),
       ab_block("correction", correction),
       ab_block("method", c(ab_line("method_plan"), ab_line("method_power"), if (!is.null(cu)) ab_line("method_cuped"), if (dated) ab_line("method_weeks"))),
       ab_block("formulas", c(ab_line("formula_sample_size"), if (!is.null(cu)) ab_line("formula_cuped"))))
}

# The `plan` output: the grid, every column a double, `days` and the CUPED columns NULL where there is no date or covariate.
.abp_output <- function(b, lifts, asked, at, cu) {
  col <- function(x) arrow::Array$create(if (is.null(x)) rep(NA_real_, length(lifts)) else as.numeric(x), type = arrow::float64())
  arrow::Table$create(lift = col(lifts), mde_absolute = col(abs(b$m0) * lifts), n_per_variant = col(at$n_t), n_control = col(at$n_c),
                      n_total = col(at$total), days = col(at$days), weeks = col(at$weeks), n_per_variant_cuped = col(cu$n_t),
                      days_cuped = col(cu$days), asked = arrow::Array$create(seq_along(lifts) == asked, type = arrow::boolean()))
}
