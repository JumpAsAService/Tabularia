# Kaplan–Meier (docs/specs/statistical-models.md, `kaplan_meier`, lane M4): the survival
# curve of each stratum with its pointwise interval (`conf_type`, at 1 − alpha), its
# quartiles and median with their intervals, survival at the times asked (`surv_at_<t>`),
# the restricted mean up to `rmst_tau`, the number at risk; with strata the log-rank test
# (`test = logrank`, ρ = 0) or Peto's (`test = peto`, ρ = 1) by survival::survdiff, its
# observed and expected events, and with `pairwise` every pair of strata, Holm-adjusted.
#
# The checks (section `assumption`), reported, never enforced:
#   censoring_informative  cannot be tested from the data: not computed, the share censored
#                          as its value; its spies are the next two;
#   censoring_by_stratum   chi-square of censored against stratum (descriptive);
#   follow_up_by_stratum   the reverse Kaplan–Meier (censoring as the event) compared across
#                          strata by the log-rank test: rows of one stratum followed less long;
#   proportional_hazards   the log-rank test's power needs it: the Grambsch–Therneau test
#                          (survival::cox.zph on the strata, transform = "km") and the curves
#                          that cross (each above the other by at least KM_CROSS_GAP and by
#                          KM_CROSS_Z standard errors of the difference, while both strata keep
#                          KM_MIN_AT_RISK rows at risk: two samples of one curve wander across
#                          each other by less);
#   few_at_risk            a number reported (a quartile, the median, survival at a time, the
#                          restricted mean's horizon when the node gives it) read where fewer
#                          than KM_MIN_AT_RISK rows are still at risk: the tail of a curve is
#                          uncertain.
#
# The restricted mean's horizon, `rmst_tau`, is by default the shortest of the strata's
# follow-ups (the least of their largest times, as survRM2 does): every stratum's mean is then
# read over the same span, inside each one's follow-up; the default is not a number of the
# few_at_risk check (at the end of a follow-up few rows are at risk by construction).
#
# Charts (level = the chart's id, level_2 = the series, the stratum): curve `km` (stats
# surv, lower, upper), `cumhaz` (cumhaz), `loglog` (y = log(−log S) against x = log t);
# points `censored` (y = the curve's height at a censored time, stat surv) and `at_risk`
# (stat n_risk at round times). A curve keeps every step up to max_chart_points, else its
# height at that many equally spaced times; the censored times are sampled with the node's
# seed above it. The seed serves nothing else: the estimate has no random part.
#
# Columns never become code: the input's columns are copied into a frame of fixed names
# (time, status, g) before survfit, survdiff or coxph see them; a column's name is only
# ever written as a label.

KM_MIN_AT_RISK <- 10
KM_CROSS_GAP <- 0.05
KM_CROSS_Z <- 2   # standard errors of the difference of two curves that a crossing must pass, both ways
KM_ALL <- "all" # the stratum's name without strata
KM_CARD_TOLERANCE <- 0.001 # the most a thinned card's curve or interval differs from the fit's
KM_CHART_SERIES <- 7L      # a stratum's series: km's surv, lower, upper; cumhaz; loglog; censored; at_risk

# The note of a thinned card: no number or name in it, so that report_texts/kaplan_meier.json
# says it in the page's language.
KM_NOTE_CARD_THINNED <- paste0(
  "The curves have more steps than the model card keeps: the card keeps a step wherever the survival or an end of its ",
  "interval has moved by 0.001 or more, so that a survival predicted from it differs from the fitted curve by less than 0.001.")

fit_kaplan_meier <- function(task) {
  p <- task$params
  alpha <- as.numeric(p$alpha %||% 0.05)
  conf_type <- p$conf_type %||% "log"
  test <- p$test %||% "logrank"
  rho <- if (identical(test, "peto")) 1 else 0
  time_col <- p$time
  event_col <- p$event
  strata_col <- p$strata
  times <- sort(unique(as.numeric(unlist(p$times %||% list()))))

  df <- tab_input(task, columns = unique(c(time_col, event_col, strata_col)))
  for (name in c(time_col, event_col, strata_col)) {
    if (!name %in% names(df)) tab_refuse("missing_column", paste0("the input has no column «", name, "»"), list(column = name))
  }
  kind <- tab_kind(df[[time_col]])
  if (!kind %in% c("number", "integer")) {
    tab_refuse("time_type", paste0("the column «", time_col, "» is a ", kind, ": the time must be a number, the time from the start ",
                                   "to the event or to the censoring (compute it from the dates first)"), list(column = time_col))
  }
  used <- tab_complete_rows(df, c(time_col, event_col, strata_col))
  d <- df[used$keep, , drop = FALSE]
  if (!nrow(d)) tab_refuse("no_rows", "no row has a time, an event and a stratum: nothing to estimate")
  time <- as.numeric(d[[time_col]])
  if (any(time < 0)) {
    tab_refuse("negative_time", paste0("the column «", time_col, "» holds a negative time (", tab_number_label(min(time)),
                                       "): a time is counted from the start, 0 or more"), list(column = time_col))
  }
  status <- km_is_event(d[[event_col]], p$event_value)
  value_label <- km_value_label(p$event_value)
  if (!any(status)) {
    tab_refuse("no_events", paste0("the column «", event_col, "» is never «", value_label, "» in the rows the model uses: ",
                                   "with no event there is no curve to estimate; check event_value"),
               list(column = event_col, event_value = value_label))
  }
  if (is.null(strata_col)) {
    labels <- rep(KM_ALL, nrow(d))
    levels <- KM_ALL
    names_ <- KM_ALL
  } else {
    labels <- tab_label(d[[strata_col]])
    levels <- tab_sort_labels(labels)
    if (length(levels) > tab_limit(task, "max_strata")) {
      tab_refuse("too_many_strata", paste0("the column «", strata_col, "» makes ", length(levels), " strata (at most ",
                                           tab_limit(task, "max_strata"), "): is it the right column?"),
                 list(column = strata_col, strata = length(levels)))
    }
    names_ <- paste0(strata_col, "=", levels)
  }
  frame <- data.frame(time = time, status = as.integer(status), g = factor(labels, levels = levels))
  fit <- survival::survfit(survival::Surv(time, status) ~ g, data = frame, conf.type = conf_type, conf.int = 1 - alpha)
  curves <- km_curves(fit, length(levels))
  tau <- if (is.null(p$rmst_tau)) min(vapply(curves, function(s) max(s$time), numeric(1))) else as.numeric(p$rmst_tau)
  compare <- length(levels) >= 2L

  rows <- tab_rows_start(task)
  rhs <- if (is.null(strata_col)) "1" else strata_col
  tab_text(rows, "model", "formula", paste0("Surv(", time_col, ", ", event_col, " = ", value_label, ") ~ ", rhs))
  tab_text(rows, "model", "software", tab_software("survival"))
  tab_text(rows, "model", "event_value", value_label)
  tab_text(rows, "model", "conf_type", conf_type)
  if (compare) tab_text(rows, "model", "test", test)
  tab_number(rows, "model", "n", nrow(d))
  tab_number(rows, "model", "events", sum(status))
  tab_number(rows, "model", "censored", sum(!status))
  tab_number(rows, "model", "n_missing", used$n_missing)
  tab_number(rows, "model", "n_strata", length(levels))
  tab_number(rows, "model", "rmst_tau", tau)
  oe <- NULL # observed and expected events per stratum: the log-rank's (unweighted) whatever the test
  if (compare) {
    sd <- survival::survdiff(survival::Surv(time, status) ~ g, data = frame, rho = rho)
    df_ <- sum(sd$exp > 0) - 1
    tab_number(rows, "model", "test_statistic", sd$chisq)
    tab_number(rows, "model", "test_df", df_)
    tab_p(rows, "model", stats::pchisq(sd$chisq, df_, lower.tail = FALSE), stat = "test_p_value")
    oe <- if (rho == 0) sd else survival::survdiff(survival::Surv(time, status) ~ g, data = frame, rho = 0)
  }

  # ── per stratum ──
  reported <- list() # per stratum: the times its numbers are read at, by name
  for (i in seq_along(levels)) {
    s <- curves[[i]]
    name <- names_[[i]]
    tab_number(rows, "stratum", "n", s$n, level = name)
    tab_number(rows, "stratum", "events", sum(s$n.event), level = name)
    tab_number(rows, "stratum", "censored", sum(s$n.censor), level = name)
    q <- stats::quantile(s, probs = c(0.25, 0.5, 0.75), conf.int = TRUE)
    for (j in 1:3) {
      stat <- c("q25", "median", "q75")[[j]]
      tab_number(rows, "stratum", stat, q$quantile[[j]], level = name)
      tab_number(rows, "stratum", paste0(stat, "_ci_low"), q$lower[[j]], level = name)
      tab_number(rows, "stratum", paste0(stat, "_ci_high"), q$upper[[j]], level = name)
    }
    at <- km_surv_at(s, times)
    for (k in seq_along(times)) {
      # the time in x0 too, so that a page writes it as a number of its language
      stat <- paste0("surv_at_", km_time_label(times[[k]]))
      tab_add(rows, "stratum", stat, value = at$surv[[k]], level = name, x0 = times[[k]])
      tab_add(rows, "stratum", paste0(stat, "_low"), value = at$lower[[k]], level = name, x0 = times[[k]])
      tab_add(rows, "stratum", paste0(stat, "_high"), value = at$upper[[k]], level = name, x0 = times[[k]])
    }
    rm <- km_rmst(s, tau)
    tab_number(rows, "stratum", "rmst", rm[["rmean"]], level = name)
    tab_number(rows, "stratum", "rmst_se", rm[["se"]], level = name)
    if (!is.null(oe)) {
      tab_number(rows, "stratum", "observed", oe$obs[[i]], level = name)
      tab_number(rows, "stratum", "expected", oe$exp[[i]], level = name)
    }
    reported[[name]] <- c(q25 = q$quantile[[1L]], median = q$quantile[[2L]], q75 = q$quantile[[3L]],
                          if (length(times)) stats::setNames(times, paste0("surv_at_", vapply(times, km_time_label, ""))),
                          if (!is.null(p$rmst_tau)) c(rmst_tau = tau))
    km_notes(rows, s, name, at, times, q)
  }

  # ── pairs ──
  if (compare && isTRUE(p$pairwise)) {
    pairs <- utils::combn(length(levels), 2L)
    chisq <- p_ <- numeric(ncol(pairs))
    for (k in seq_len(ncol(pairs))) {
      sub <- frame[frame$g %in% levels[pairs[, k]], , drop = FALSE]
      sub$g <- droplevels(sub$g)
      x <- survival::survdiff(survival::Surv(time, status) ~ g, data = sub, rho = rho)
      chisq[[k]] <- x$chisq
      p_[[k]] <- stats::pchisq(x$chisq, sum(x$exp > 0) - 1, lower.tail = FALSE)
    }
    adjusted <- stats::p.adjust(p_, method = "holm")
    for (k in seq_len(ncol(pairs))) {
      a <- names_[[pairs[1L, k]]]
      b <- names_[[pairs[2L, k]]]
      tab_number(rows, "pair", "statistic", chisq[[k]], level = a, level_2 = b)
      tab_number(rows, "pair", "p_value", p_[[k]], level = a, level_2 = b)
      tab_number(rows, "pair", "p_adjusted", adjusted[[k]], level = a, level_2 = b)
      code <- tab_significance(tab_finite(adjusted[[k]]))
      if (!is.na(code)) tab_text(rows, "pair", "significance", code, level = a, level_2 = b)
    }
  }

  km_checks(rows, frame, curves, names_, alpha, reported)
  # the points a series may have: max_chart_points, fewer when the strata's series at that many
  # would pass max_result_rows (one row may follow the charts: the note of a thinned card)
  km_charts(rows, curves, names_, tab_chart_points(task, rows, KM_CHART_SERIES * length(curves), after = 1))

  output <- if (identical(p$output, "table")) km_table(curves, if (is.null(strata_col)) NA_character_ else levels) else NULL
  card <- km_card(task, curves, levels, strata_col, time_col, event_col, value_label, conf_type, alpha)
  if (card$kaplan_meier$tolerance > 0) tab_text(rows, "model", "note_card_thinned", KM_NOTE_CARD_THINNED)
  list(rows = rows, output = output, card = card, native = fit)
}

# Each stratum's curve, a survfit of its own (fit[i] keeps the interval's type and level).
km_curves <- function(fit, k) {
  if (is.null(fit$strata)) return(list(fit))
  lapply(seq_len(k), function(i) fit[i])
}

# Whether each value of the event column is the event: numbers compared as numbers, the rest
# by their labels (a boolean as «true», a date as 2026-11-19), as the engine names values.
km_is_event <- function(x, value) {
  if (is.list(value)) value <- unlist(value)
  if (is.null(value) || !length(value)) return(rep(FALSE, length(x)))
  if (inherits(x, "integer64")) x <- as.numeric(x)
  if (is.numeric(x)) {
    v <- suppressWarnings(as.numeric(value))
    return(!is.na(v) & !is.na(x) & x == v)
  }
  # a number given for a column of text or of another kind: «1» as the column writes it, or «1.0»
  # as the engine labels a number (the refusal says km_value_label's «1»)
  label <- if (is.character(value)) enc2utf8(value) else unique(c(km_value_label(value), tab_label(value)))
  !is.na(x) & tab_label(x) %in% label
}

km_value_label <- function(value) {
  if (is.list(value)) value <- unlist(value)
  if (is.character(value)) return(enc2utf8(value))
  if (is.numeric(value) && value == trunc(value)) return(format(value, scientific = FALSE))
  tab_label(value)
}

# «30», «2.5»: a time as a stat's name says it.
km_time_label <- function(t) {
  if (t == trunc(t) && abs(t) < 1e15) return(format(t, scientific = FALSE, trim = TRUE))
  tab_number_label(t)
}

# Survival at the times asked, with its interval: past the stratum's last time it is unknown
# (NA) unless the curve has reached 0 there.
km_surv_at <- function(s, times) {
  if (!length(times)) return(list(surv = numeric(), lower = numeric(), upper = numeric(), n_risk = numeric()))
  x <- summary(s, times = times, extend = TRUE)
  out <- list(surv = x$surv, lower = x$lower, upper = x$upper, n_risk = x$n.risk)
  past <- times > max(s$time) & out$surv > 0
  for (k in c("surv", "lower", "upper")) out[[k]][past] <- NA_real_
  out$past <- past
  out
}

# The restricted mean up to tau, and its standard error. Before the stratum's first time its
# curve is 1 on [0, tau]: the mean is tau, with no error (survival's summary refuses a tau
# there, «Truncation point for the mean time in state is < smallest survival»).
km_rmst <- function(s, tau) {
  if (tau < min(s$time)) return(c(rmean = tau, se = 0))
  x <- summary(s, rmean = tau)$table
  c(rmean = x[["rmean"]], se = x[["se(rmean)"]])
}

# A note the report says in the page's language (layouts/common.py, structured_note): «@id name=value …»,
# the items of a list joined by «,». A value is a name of the data or a number: «%», « » and «,» in
# it are written %25, %20 and %2C, so that the note splits where it should; the report reads them
# back (URL decoding). Shared with cox.R.
surv_note <- function(id, ...) {
  values <- list(...)
  encode <- function(x) gsub(",", "%2C", gsub(" ", "%20", gsub("%", "%25", enc2utf8(as.character(x)), fixed = TRUE), fixed = TRUE), fixed = TRUE)
  parts <- vapply(names(values), function(k) paste0(k, "=", paste(encode(values[[k]]), collapse = ",")), "")
  paste(c(paste0("@", id), parts), collapse = " ")
}

# The notes of a stratum, by id: stratum_no_events, median_not_reached, survival_not_estimated.
km_notes <- function(rows, s, name, at, times, q) {
  if (sum(s$n.event) == 0) {
    # «The stratum {stratum} has no event: its curve stays at 1 and its median is not reached.»
    tab_note(rows, surv_note("stratum_no_events", stratum = name))
  } else if (is.na(q$quantile[[2L]])) {
    # «The median of {stratum} is not reached: fewer than half its rows had the event by the end of its follow-up;
    # read its survival at a time, or its restricted mean.»
    tab_note(rows, surv_note("median_not_reached", stratum = name))
  }
  if (length(times) && any(at$past)) {
    # «Survival of {stratum} at {times} is not estimated: its follow-up ends at {last}.»
    tab_note(rows, surv_note("survival_not_estimated", stratum = name, times = vapply(times[at$past], km_time_label, ""),
                             last = km_time_label(max(s$time))))
  }
}

# ── the checks ──
km_check <- function(rows, check, h0, verdict, advice = NULL, statistic = NULL, df = NULL, p = NULL, threshold = NULL,
                     value = NULL, count = NULL, look_at = NULL) {
  tab_text(rows, "assumption", "h0", h0, level = check)
  if (!is.null(statistic)) tab_number(rows, "assumption", "statistic", statistic, level = check)
  if (!is.null(df)) tab_number(rows, "assumption", "df", df, level = check)
  if (!is.null(p)) tab_p(rows, "assumption", p, level = check)
  if (!is.null(threshold)) tab_number(rows, "assumption", "threshold", threshold, level = check)
  if (!is.null(value)) tab_number(rows, "assumption", "value", value, level = check)
  if (!is.null(count)) tab_number(rows, "assumption", "count", count, level = check)
  tab_text(rows, "assumption", "verdict", verdict, level = check)
  if (!identical(verdict, "ok") && !is.null(advice)) tab_text(rows, "assumption", "advice", advice, level = check)
  if (length(look_at) && nzchar(look_at)) tab_text(rows, "assumption", "rows", look_at, level = check)
}

km_checks <- function(rows, frame, curves, names_, alpha, reported) {
  censored <- sum(frame$status == 0L)
  km_check(rows, "censoring_informative", "Being censored says nothing of the time to the event.", "not_computed",
           advice = "It cannot be tested from the data: ask how the rows came to be censored.",
           value = censored / nrow(frame), count = censored)
  k <- length(curves)
  if (k >= 2L) {
    # censored against stratum: a chi-square of the 2 × k table, descriptive
    if (censored == 0L || censored == nrow(frame)) {
      km_check(rows, "censoring_by_stratum", "The share censored is the same in every stratum.", "ok", count = censored)
    } else {
      table_ <- table(frame$status == 0L, frame$g)
      x <- suppressWarnings(stats::chisq.test(table_, correct = FALSE))
      km_check(rows, "censoring_by_stratum", "The share censored is the same in every stratum.",
               if (x$p.value < alpha) "doubtful" else "ok", advice = "The share censored differs between strata: expected when their survival differs; the follow-up check says whether censoring itself differs.",
               statistic = unname(x$statistic), df = unname(x$parameter), p = x$p.value, threshold = alpha)
    }
    # the follow-up: the reverse Kaplan–Meier (censoring as the event) compared across strata
    if (censored == 0L) {
      km_check(rows, "follow_up_by_stratum", "The rows of every stratum are followed as long (the reverse Kaplan–Meier).", "ok",
               count = 0)
    } else {
      x <- survival::survdiff(survival::Surv(time, 1L - status) ~ g, data = frame)
      df_ <- sum(x$exp > 0) - 1
      km_check(rows, "follow_up_by_stratum", "The rows of every stratum are followed as long (the reverse Kaplan–Meier).",
               if (stats::pchisq(x$chisq, df_, lower.tail = FALSE) < alpha) "doubtful" else "ok",
               advice = "The follow-up differs between strata: the rows of one are censored sooner; ask why before comparing.",
               statistic = x$chisq, df = df_, p = stats::pchisq(x$chisq, df_, lower.tail = FALSE), threshold = alpha)
    }
    km_check_proportional(rows, frame, curves, names_, alpha)
  }
  # numbers read where few rows are left at risk
  thin <- character()
  smallest <- Inf
  for (i in seq_along(curves)) {
    at <- reported[[names_[[i]]]]
    at <- at[is.finite(at)]
    if (!length(at)) next
    grid <- sort(unique(unname(at)))
    n_risk <- summary(curves[[i]], times = grid, extend = TRUE)$n.risk[match(at, grid)]
    low <- n_risk < KM_MIN_AT_RISK
    smallest <- min(smallest, n_risk)
    if (any(low)) thin <- c(thin, paste0(names_[[i]], ": ", paste(names(at)[low], collapse = ", ")))
  }
  km_check(rows, "few_at_risk", paste0("Every number reported is read where at least ", KM_MIN_AT_RISK, " rows are still at risk."),
           if (length(thin)) "doubtful" else "ok",
           advice = "Few rows are left at risk where these numbers are read: the end of a curve is uncertain; read them with their intervals, or report at an earlier time.",
           threshold = KM_MIN_AT_RISK, value = if (is.finite(smallest)) smallest else NULL, count = length(thin),
           look_at = paste(thin, collapse = "; "))
}

# The strata's hazards in proportion: Grambsch–Therneau on the strata as a Cox model's
# covariate, and the curves that cross.
km_check_proportional <- function(rows, frame, curves, names_, alpha) {
  why <- "the test gives no p-value" # why the Grambsch–Therneau test could not be made
  zph <- tryCatch({
    if (any(vapply(curves, function(s) sum(s$n.event) == 0, logical(1)))) stop("a stratum has no event")
    cfit <- survival::coxph(survival::Surv(time, status) ~ g, data = frame)
    survival::cox.zph(cfit, transform = "km")$table["GLOBAL", ]
  }, error = function(e) {
    why <<- conditionMessage(e)
    NULL
  })
  crossing <- character()
  pairs <- utils::combn(length(curves), 2L)
  for (k in seq_len(ncol(pairs))) {
    a <- curves[[pairs[1L, k]]]
    b <- curves[[pairs[2L, k]]]
    grid <- sort(unique(c(a$time[a$n.event > 0], b$time[b$n.event > 0])))
    if (!length(grid)) next
    sa <- summary(a, times = grid, extend = TRUE)
    sb <- summary(b, times = grid, extend = TRUE)
    keep <- sa$n.risk >= KM_MIN_AT_RISK & sb$n.risk >= KM_MIN_AT_RISK
    gap <- (sa$surv - sb$surv)[keep]
    beyond <- pmax(KM_CROSS_GAP, KM_CROSS_Z * sqrt(sa$std.err^2 + sb$std.err^2)[keep])
    if (any(gap >= beyond, na.rm = TRUE) && any(gap <= -beyond, na.rm = TRUE)) {
      crossing <- c(crossing, paste(names_[pairs[, k]], collapse = " × "))
    }
  }
  p <- if (is.null(zph)) NULL else zph[["p"]]
  # curves that cross fail the check whatever the test; without the test and without a crossing
  # nothing says the ratio holds: not computed, never «ok»
  verdict <- if (length(crossing)) "failed" else if (is.null(p) || !is.finite(p)) "not_computed" else if (p < alpha) "doubtful" else "ok"
  advice <- if (identical(verdict, "not_computed")) {
    paste0("The test of the hazards' ratio could not be computed on these rows (", why, "), and no two curves cross: ",
           "read the curves themselves.")
  } else {
    paste0("The hazards' ratio changes over time, or the curves cross: the log-rank test loses power, and so does ",
           "Peto's; compare the restricted means (rmst_tau), and read the curves themselves.")
  }
  km_check(rows, "proportional_hazards", "The strata's hazards keep the same ratio over time (the curves do not cross).", verdict,
           advice = advice,
           statistic = if (is.null(zph)) NULL else zph[["chisq"]], df = if (is.null(zph)) NULL else zph[["df"]], p = p,
           threshold = alpha, count = if (length(crossing)) length(crossing) else NULL, look_at = paste(crossing, collapse = "; "))
  if (length(crossing)) {
    # «The curves cross ({pairs}): the log-rank and Peto's tests lose power there; compare the restricted means, and
    # read the curves themselves.» Each pair «a × b».
    tab_note(rows, surv_note("curves_cross", pairs = crossing))
  }
}

# ── the charts ──
km_charts <- function(rows, curves, names_, max_points) {
  all_times <- unlist(lapply(curves, function(s) s$time))
  round_times <- pretty(c(0, max(all_times)), n = 6)
  round_times <- round_times[round_times >= 0 & round_times <= max(all_times)]
  for (i in seq_along(curves)) {
    s <- curves[[i]]
    name <- names_[[i]]
    start <- if (s$time[[1L]] > 0) 0 else NULL
    x <- c(start, s$time)
    one <- if (is.null(start)) NULL else 1
    for (stat in c("surv", "lower", "upper")) {
      y <- c(one, s[[stat]])
      steps <- tab_thin_steps(x, y, max = max_points)
      tab_points(rows, "curve", "km", stat, steps$x, steps$y, level_2 = name)
    }
    steps <- tab_thin_steps(x, c(if (is.null(start)) NULL else 0, s$cumhaz), max = max_points)
    tab_points(rows, "curve", "cumhaz", "cumhaz", steps$x, steps$y, level_2 = name)
    ok <- s$time > 0 & s$surv > 0 & s$surv < 1
    if (any(ok)) {
      steps <- tab_thin_steps(log(s$time[ok]), log(-log(s$surv[ok])), max = max_points)
      tab_points(rows, "curve", "loglog", "y", steps$x, steps$y, level_2 = name)
    }
    censored <- which(s$n.censor > 0)
    if (length(censored)) {
      keep <- censored[tab_cap(length(censored), max = max_points)]
      tab_points(rows, "point", "censored", "surv", s$time[keep], s$surv[keep], level_2 = name)
    }
    if (length(round_times)) {
      n_risk <- summary(s, times = round_times, extend = TRUE)$n.risk
      tab_points(rows, "point", "at_risk", "n_risk", round_times, n_risk, level_2 = name)
    }
  }
}

# ── the table output: one row per stratum and time ──
km_table <- function(curves, labels) {
  parts <- lapply(seq_along(curves), function(i) {
    s <- curves[[i]]
    data.frame(stratum = rep(labels[[i]], length(s$time)), time = s$time, n_risk = as.integer(round(s$n.risk)),
               n_event = as.integer(round(s$n.event)), n_censor = as.integer(round(s$n.censor)), surv = s$surv,
               lower = tab_finite(s$lower), upper = tab_finite(s$upper), stringsAsFactors = FALSE)
  })
  do.call(rbind, parts)
}

# ── the card: each stratum's curve at its event times, and where its follow-up ends ──
# Every event time while the strata have at most max_baseline_times of them together (the
# card is then exact); above, a stratum keeps only the steps km_card_steps picks, within
# KM_CARD_TOLERANCE (the card's `tolerance`, 0 when exact), as Cox's card thins its baseline.
km_card <- function(task, curves, levels, strata_col, time_col, event_col, value_label, conf_type, alpha) {
  card <- tab_card(task, NULL, packages = "survival")
  card$inputs <- list(list(name = time_col, type = "number"))
  if (!is.null(strata_col)) {
    card$inputs[[2L]] <- list(name = strata_col, type = "category", levels = as.list(levels), reference = levels[[1L]])
  }
  card$formula <- paste0("Surv(", time_col, ", ", event_col, " = ", value_label, ") ~ ", if (is.null(strata_col)) "1" else strata_col)
  card$outcome <- list(name = event_col, event_value = value_label)
  card$alpha <- alpha
  steps <- sum(vapply(curves, function(s) sum(s$n.event > 0), numeric(1)))
  tolerance <- if (steps > tab_limit(task, "max_baseline_times")) KM_CARD_TOLERANCE else 0
  card$kaplan_meier <- list(
    time_column = time_col, strata_column = strata_col %||% NULL, conf_type = conf_type, tolerance = tolerance,
    strata = lapply(seq_along(curves), function(i) {
      s <- curves[[i]]
      e <- which(s$n.event > 0)
      lower <- tab_finite(s$lower[e])
      upper <- tab_finite(s$upper[e])
      if (tolerance > 0) {
        keep <- km_card_steps(s$surv[e], lower, upper, tolerance)
        e <- e[keep]
        lower <- lower[keep]
        upper <- upper[keep]
      }
      list(stratum = if (is.null(strata_col)) NULL else levels[[i]], last_time = max(s$time), time = as.list(s$time[e]),
           surv = as.list(s$surv[e]), lower = as.list(lower), upper = as.list(upper))
    }))
  card
}

# The steps of a curve a thinned card keeps: a step whenever the survival or an end of its
# interval has moved by `tolerance` or more since the last step kept (from 1, the curve before
# its first event; an end that becomes empty or stops being so counts as a move), and the last
# step. Predict reads a time at the last step kept before it: between two kept steps the fit
# moved by less than `tolerance` on each of the three, so the card differs from the fit by less
# than `tolerance` at every time, and is exact past the last step.
km_card_steps <- function(surv, lower, upper, tolerance) {
  n <- length(surv)
  keep <- logical(n)
  last <- c(1, 1, 1)
  for (i in seq_len(n)) {
    now <- c(surv[[i]], lower[[i]], upper[[i]])
    moved <- ifelse(is.na(now) & is.na(last), 0, ifelse(is.na(now) | is.na(last), Inf, abs(now - last)))
    if (i == n || any(moved >= tolerance)) {
      keep[[i]] <- TRUE
      last <- now
    }
  }
  keep
}

# ── Predict: survival at each row's time, in its stratum ──
predict_kaplan_meier <- function(task, card) {
  km <- card$kaplan_meier
  num <- function(x) vapply(x, function(v) if (is.null(v)) NA_real_ else as.numeric(v), numeric(1))
  df <- tab_input(task)
  made <- c("prediction", if (isTRUE(task$params$interval)) c("prediction_low", "prediction_high"))
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"),
               list(column = clash[[1L]]))
  }
  if (!km$time_column %in% names(df)) {
    tab_refuse("missing_column", paste0("the rows have no column «", km$time_column, "», the time the model was fitted on"),
               list(column = km$time_column))
  }
  # a time of another kind (a date, a text) is refused, as the parametric node's Predict refuses it:
  # a date read as a number would be days since 1970
  if (!tab_kind(df[[km$time_column]]) %in% c("number", "integer")) {
    tab_refuse("column_kind", paste0("the column «", km$time_column, "» must hold numbers, as when the model was fitted"),
               list(column = km$time_column))
  }
  time <- as.numeric(df[[km$time_column]])
  strata_col <- km$strata_column
  if (!is.null(strata_col) && !strata_col %in% names(df)) {
    tab_refuse("missing_column", paste0("the rows have no column «", strata_col, "», the strata the model was fitted on"),
               list(column = strata_col))
  }
  labels <- if (is.null(strata_col)) rep("", nrow(df)) else tab_label(df[[strata_col]])
  # the rows whose survival stays empty, counted and said as the parametric node's Predict says them
  if (!is.null(strata_col)) {
    seen <- vapply(km$strata, function(s) as.character(s$stratum %||% NA_character_), "")
    unseen <- sum(!is.na(labels) & !labels %in% seen)
    if (unseen > 0L) warning(unseen, " rows hold a value the model did not see when fitted: their prediction is empty")
  }
  empty <- sum(is.na(time) | is.na(labels))
  if (empty > 0L) warning(empty, " rows have an empty value in a column the model needs: their prediction is empty")
  negative <- sum(!is.na(time) & time < 0)
  if (negative > 0L) warning(negative, " rows have a negative time: their prediction is empty")
  prediction <- low <- high <- rep(NA_real_, nrow(df))
  for (s in km$strata) {
    rows_ <- which(if (is.null(strata_col)) rep(TRUE, nrow(df)) else !is.na(labels) & labels == s$stratum)
    rows_ <- rows_[!is.na(time[rows_]) & time[rows_] >= 0]
    if (!length(rows_)) next
    t <- time[rows_]
    at <- findInterval(t, num(s$time))
    pick <- function(v) ifelse(at == 0L, 1, num(v)[pmax(at, 1L)])
    surv <- pick(s$surv)
    past <- t > as.numeric(s$last_time) & surv > 0
    surv[past] <- NA_real_
    prediction[rows_] <- surv
    if (!is.null(s$lower)) {
      l <- pick(s$lower)
      h <- pick(s$upper)
      l[past] <- NA_real_
      h[past] <- NA_real_
      low[rows_] <- l
      high[rows_] <- h
    }
  }
  df$prediction <- prediction
  if (isTRUE(task$params$interval)) {
    df$prediction_low <- low
    df$prediction_high <- high
  }
  list(output = df)
}

# ── the runner's own tests, run by the group's self-test with the common library's ──
TAB_COMMON_TESTS[["kaplan_meier: column names that look like code, and predicting the fitted rows"]] <- function(code) {
  path <- file.path(code, "reference", "data", "kaplan_meier_strata.csv")
  if (!tab_ref_exists(path)) stop("no reference table ", path)
  df <- tab_read_csv(path, list(days = "number", status = "text", line = "text"))
  params <- list(time = "days", event = "status", event_value = "failed", strata = "line", times = list(30, 90), output = "table")
  task <- function(frame, params) list(model = "kaplan_meier", variant = "default", group = "survival", group_version = "selftest",
                                       params = params, rows_from = "run", limits = tab_catalog()$limits, .frame = frame)
  plain <- fit_kaplan_meier(task(df, params))
  odd <- df
  names(odd) <- c(TAB_A, TAB_X, TAB_U)[match(names(df), c("days", "status", "line"))]
  odd_params <- utils::modifyList(params, list(time = TAB_A, event = TAB_X, strata = TAB_U))
  coded <- fit_kaplan_meier(task(odd, odd_params))
  a <- tab_results_frame(plain$rows)
  b <- tab_results_frame(coded$rows)
  .tab_expect(identical(a$value, b$value), "the numbers differ when the columns are renamed")
  .tab_expect(identical(b$text[b$key == "model|||formula"], paste0("Surv(", TAB_A, ", ", TAB_X, " = failed) ~ ", TAB_U)),
              "formula: ", b$text[b$key == "model|||formula"])
  .tab_expect(paste0(TAB_U, "=A") %in% b$level, "the strata's names")
  # the card applied to the rows it was fitted on gives the fitted curve, row by row
  card <- jsonlite::fromJSON(tab_card_text(plain$card), simplifyVector = FALSE)
  df <- df[stats::complete.cases(df), ]
  out <- predict_kaplan_meier(list(params = list(interval = TRUE), .frame = df), card)$output
  table_ <- plain$output
  at <- match(paste(df$line, df$days), paste(table_$stratum, table_$time))
  .tab_expect(!anyNA(at), "a fitted row has no step in the table output")
  want <- table_$surv[at]
  low <- table_$lower[at]
  .tab_expect(max(abs(out$prediction - want)) < 1e-9, "predicted survival differs from the fit")
  .tab_expect(isTRUE(all.equal(out$prediction_low, low, tolerance = 1e-9)), "predicted lower end differs from the fit")
  late <- predict_kaplan_meier(list(params = list(), .frame = data.frame(days = c(0, 1e6), line = c("A", "Z"), stringsAsFactors = FALSE)), card)$output
  .tab_expect(late$prediction[[1L]] == 1 && is.na(late$prediction[[2L]]) && is.null(late$prediction_low), "time 0, an unseen stratum")
}

TAB_COMMON_TESTS[["kaplan_meier: a card on many rows is thinned within its tolerance, and predicts the fit"]] <- function(code) {
  # 20 000 rows of continuous times in two strata, drawn here from a seed: no large file in the repository
  set.seed(20261120)
  n <- 20000L
  line <- sample(c("A", "B"), n, replace = TRUE)
  event <- stats::rexp(n, ifelse(line == "A", 1 / 300, 1 / 200))
  censor <- stats::runif(n, 0, 900)
  df <- data.frame(days = pmin(event, censor), status = ifelse(event <= censor, "failed", "running"), line = line,
                   stringsAsFactors = FALSE)
  task <- list(model = "kaplan_meier", variant = "default", group = "survival", group_version = "selftest", rows_from = "run",
               params = list(time = "days", event = "status", event_value = "failed", strata = "line"),
               limits = tab_catalog()$limits, .frame = df)
  fit <- fit_kaplan_meier(task)
  text <- tab_card_fits(task, fit$card, tab_rows())
  .tab_expect(!is.null(text), "the card is over max_card_bytes: ", nchar(tab_card_text(fit$card), type = "bytes"), " bytes")
  card <- jsonlite::fromJSON(text, simplifyVector = FALSE)
  .tab_expect(identical(card$kaplan_meier$tolerance, KM_CARD_TOLERANCE), "the card does not say its tolerance")
  kept <- sum(vapply(card$kaplan_meier$strata, function(s) length(s$time), numeric(1)))
  .tab_expect(kept < sum(df$status == "failed") / 2, "the card was not thinned: ", kept, " steps")
  rows <- tab_results_frame(fit$rows)
  .tab_expect(identical(rows$text[rows$key == "model|||note_card_thinned"], KM_NOTE_CARD_THINNED), "no note of the thinned card")
  # survival and its interval predicted at the fitted rows' times and between them, against survfit's own
  ref <- survival::survfit(survival::Surv(days, status == "failed") ~ line, data = df)
  at <- data.frame(days = c(df$days[1:4000], stats::runif(2000, 0, max(df$days))), line = rep(c("A", "B"), 3000),
                   stringsAsFactors = FALSE)
  got <- predict_kaplan_meier(list(params = list(interval = TRUE), .frame = at), card)$output
  for (g in c("A", "B")) {
    last <- max(df$days[df$line == g])
    past <- which(at$line == g & at$days > last)
    .tab_expect(all(is.na(got$prediction[past])), "survival of ", g, " past its follow-up is not empty")
    i <- which(at$line == g & at$days <= last)
    s <- summary(ref[paste0("line=", g)], times = sort(unique(at$days[i])), extend = TRUE)
    j <- match(at$days[i], s$time)
    for (k in list(c("prediction", "surv"), c("prediction_low", "lower"), c("prediction_high", "upper"))) {
      gap <- max(abs(got[[k[[1L]]]][i] - s[[k[[2L]]]][j]), na.rm = TRUE)
      .tab_expect(gap < KM_CARD_TOLERANCE, k[[1L]], " of ", g, " differs from the fit by ", gap)
      .tab_expect(identical(is.na(got[[k[[1L]]]][i]), is.na(s[[k[[2L]]]][j])), k[[1L]], " of ", g, ": not empty where the fit is")
    }
  }
  # the same fit as a step: results and report whatever the card's size
  .tab_expect(!length(tab_check_declared(fit$rows, "kaplan_meier", "default")), "the results do not match the catalogue")
}

.km_test_task <- function(frame, params, limits = NULL) {
  task <- list(model = "kaplan_meier", variant = "default", group = "survival", group_version = "selftest", rows_from = "run",
               params = params, limits = tab_catalog()$limits, .frame = frame)
  if (!is.null(limits)) task$limits <- utils::modifyList(task$limits, limits)
  task
}

TAB_COMMON_TESTS[["kaplan_meier: the restricted mean up to a tau before a stratum's first time is tau, the default included"]] <- function(code) {
  # B is followed one day, censored before A's first event at 5: the default tau, the shortest follow-up, is 1
  df <- data.frame(days = c(5, 8, 12, 15, 20, 25, 30, 1, 1, 1), status = c(1, 1, 0, 1, 1, 0, 1, 0, 0, 0),
                   plant = c(rep("A", 7), rep("B", 3)), stringsAsFactors = FALSE)
  f <- tab_results_frame(fit_kaplan_meier(.km_test_task(df, list(time = "days", event = "status", event_value = 1, strata = "plant")))$rows)
  v <- function(key) f$value[match(key, f$key)]
  .tab_expect(v("model|||rmst_tau") == 1 && v("stratum|plant=A||rmst") == 1 && v("stratum|plant=A||rmst_se") == 0 &&
              v("stratum|plant=B||rmst") == 1, "the default tau: ", v("stratum|plant=A||rmst"), " ", v("stratum|plant=A||rmst_se"))
  # a tau given before the first time; and one after it, survival's own
  f <- tab_results_frame(fit_kaplan_meier(.km_test_task(df[1:7, ], list(time = "days", event = "status", event_value = 1, rmst_tau = 2)))$rows)
  .tab_expect(f$value[f$key == "stratum|all||rmst"] == 2 && f$value[f$key == "stratum|all||rmst_se"] == 0, "tau = 2")
  s <- survival::survfit(survival::Surv(days, status) ~ 1, data = df[1:7, ])
  f <- tab_results_frame(fit_kaplan_meier(.km_test_task(df[1:7, ], list(time = "days", event = "status", event_value = 1, rmst_tau = 18)))$rows)
  .tab_expect(abs(f$value[f$key == "stratum|all||rmst"] - summary(s, rmean = 18)$table[["rmean"]]) < 1e-12, "tau = 18")
}

TAB_COMMON_TESTS[["kaplan_meier: proportional hazards not tested is not_computed, never ok; a number for a column of text"]] <- function(code) {
  # the stratum b has no event: no Grambsch–Therneau test, and too few rows for a crossing
  df <- data.frame(t = c(5, 5, 5, 8, 10, 12), e = c("1", "0", "0", "0", "0", "0"), g = c("a", "a", "b", "b", "a", "b"),
                   stringsAsFactors = FALSE)
  # event_value given as the number 1 for a column of text holding «1» (the engine's label of 1 is «1.0»)
  fit <- fit_kaplan_meier(.km_test_task(df, list(time = "t", event = "e", event_value = 1, strata = "g")))
  f <- tab_results_frame(fit$rows)
  said <- function(stat) f$text[f$key == paste0("assumption|proportional_hazards||", stat)]
  .tab_expect(identical(said("verdict"), "not_computed") && grepl("a stratum has no event", said("advice"), fixed = TRUE) &&
              !"assumption|proportional_hazards||p_value" %in% f$key, "the verdict: ", said("verdict"))
  .tab_expect(f$value[f$key == "model|||events"] == 1 && identical(f$text[f$key == "model|||event_value"], "1"), "the event value")
}

TAB_COMMON_TESTS[["kaplan_meier: Predict counts the rows it leaves empty and refuses a time that is not a number"]] <- function(code) {
  df <- tab_read_csv(file.path(code, "reference", "data", "kaplan_meier_strata.csv"), list(days = "number", status = "text", line = "text"))
  fit <- fit_kaplan_meier(.km_test_task(df, list(time = "days", event = "status", event_value = "failed", strata = "line")))
  card <- jsonlite::fromJSON(tab_card_text(fit$card), simplifyVector = FALSE)
  rows_ <- data.frame(days = c(5, 5, NA, 5, -1), line = c("A", "Z", "A", NA, "A"), stringsAsFactors = FALSE)
  said <- character()
  out <- withCallingHandlers(predict_kaplan_meier(list(params = list(), .frame = rows_), card)$output, warning = function(w) {
    said <<- c(said, conditionMessage(w))
    invokeRestart("muffleWarning")
  })
  .tab_expect(is.finite(out$prediction[[1L]]) && all(is.na(out$prediction[2:5])), "the predictions: ", paste(out$prediction, collapse = " "))
  .tab_expect(identical(said, c("1 rows hold a value the model did not see when fitted: their prediction is empty",
                                "2 rows have an empty value in a column the model needs: their prediction is empty",
                                "1 rows have a negative time: their prediction is empty")), paste(said, collapse = " | "))
  # a date is not a time: read as a number it would be days since 1970
  m <- .tab_refused_with("column_kind", predict_kaplan_meier(list(params = list(), .frame = data.frame(days = as.Date("1970-01-06"), line = "A")), card))
  .tab_expect(grepl("«days» must hold numbers", m, fixed = TRUE), m)
}

TAB_COMMON_TESTS[["kaplan_meier: the charts share max_result_rows between the strata's series"]] <- function(code) {
  # 2,400 rows of continuous times in 6 strata: at max_chart_points a series, 6 × 6 series of ~400 points; the limit here 5,000 rows
  set.seed(20261121)
  n <- 2400L
  event <- stats::rexp(n, 1 / 100)
  censor <- stats::runif(n, 0, 300)
  df <- data.frame(days = round(pmin(event, censor), 3), status = as.integer(event <= censor),
                   plant = sprintf("s%d", sample(1:6, n, TRUE)), stringsAsFactors = FALSE)
  task <- .km_test_task(df, list(time = "days", event = "status", event_value = 1, strata = "plant"), limits = list(max_result_rows = 5000))
  fit <- fit_kaplan_meier(task)
  f <- tab_results_frame(fit$rows)
  .tab_expect(nrow(f) <= 5000L, nrow(f), " rows, more than the 5000 of the limit")
  .tab_expect(!length(tab_check_declared(f, "kaplan_meier", "default")), "the results do not match the catalogue")
  # every stratum keeps its curve, from 1 to its last step's height
  for (s in sprintf("plant=s%d", 1:6)) {
    km <- f[f$section == "curve" & f$level == "km" & f$level_2 %in% s & f$stat == "surv", ]
    .tab_expect(nrow(km) > 50L && km$value[[1L]] == 1, s, ": ", nrow(km), " points")
  }
}

TAB_COMMON_TESTS[["kaplan_meier: the notes are ids with their values, which the report words in the page's language"]] <- function(code) {
  df <- data.frame(t = c(5, 5, 5, 8, 10, 12, 3, 4), e = c(1, 0, 0, 0, 0, 0, 1, 1), g = c("a", "a", "b c", "b c", "a", "b c", "a", "a"),
                   stringsAsFactors = FALSE)
  f <- tab_results_frame(fit_kaplan_meier(.km_test_task(df, list(time = "t", event = "e", event_value = 1, strata = "g", times = list(11, 20))))$rows)
  notes <- f$text[startsWith(f$stat, "note_")]
  want <- c("@survival_not_estimated stratum=g=a times=11,20 last=10", "@stratum_no_events stratum=g=b%20c",
            "@survival_not_estimated stratum=g=b%20c times=20 last=12")
  .tab_expect(identical(notes, want), paste(notes, collapse = " | "))
  .tab_expect(identical(surv_note("x", names = c("a,b", "50% c"), n = 3), "@x names=a%2Cb,50%25%20c n=3"), "the encoding")
}
