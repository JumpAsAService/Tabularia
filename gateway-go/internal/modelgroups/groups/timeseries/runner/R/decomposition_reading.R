# The explanation of the decomposition's report (docs/specs/ts-decomposition.md, «Explaining the method in the report»; lot
# D2: STL, classical, MSTL). No sentence is written here: each row of the section `reading` is a template's id with the
# run's raw numbers and names,
#
#   reading|<block>|<period or empty>|line_<k>  =  @<id> name=value name=value…
#
# in the notes' format (a space, a comma and a percent inside a value are %20, %2C and %25; a list of numbers is joined with
# «,»; a number keeps the digits that read back as the same double); the layout says each one in the page's language
# (report_texts/decomposition.json: `reading_texts`, `formula_texts`). The ids and the names each carries are that file's.
# A verdict is said only from a test's p-value against alpha; a measure is said by its number.
#
#   dec_reading(ctx)       the blocks of a run: summary, strength, seasonality, season, remainder, stationarity, method, formulas
#   dec_line(id, …), dec_block(block, lines, level_2), dec_write_reading(rows, blocks)   what EMD's reading uses too

dec_number <- function(x) {
  vapply(x, function(v) {
    if (is.na(v)) return(NA_character_)
    if (v == 0) return("0")
    for (digits in 15:17) {
      s <- sprintf("%.*g", digits, v)
      if (as.numeric(s) == v) break
    }
    s
  }, "", USE.NAMES = FALSE)
}

dec_encode <- function(x) {
  x <- enc2utf8(as.character(x))
  x <- gsub("%", "%25", x, fixed = TRUE)
  x <- gsub(" ", "%20", x, fixed = TRUE)
  gsub(",", "%2C", x, fixed = TRUE)
}

# «@id name=value …»: a NULL or NA value is left out (the template says what it can without it); a list of numbers is
# joined with «,»; a name or a text is encoded, a list of whole numbers sent as one text (`periods`) has its comma encoded.
dec_line <- function(id, ...) {
  v <- list(...)
  parts <- character()
  for (name in names(v)) {
    x <- v[[name]]
    if (is.null(x) || !length(x) || all(is.na(x))) next
    parts <- c(parts, paste0(name, "=", if (is.numeric(x)) paste(dec_number(x), collapse = ",") else paste(dec_encode(x), collapse = ",")))
  }
  paste0("@", id, if (length(parts)) paste0(" ", paste(parts, collapse = " ")) else "")
}

dec_block <- function(block, lines, level_2 = NULL) list(block = block, level_2 = level_2, lines = Filter(Negate(is.na), lines))

dec_write_reading <- function(rows, blocks) {
  for (b in blocks) {
    if (is.null(b) || !length(b$lines)) next
    for (k in seq_along(b$lines)) tab_text(rows, "reading", paste0("line_", k), b$lines[[k]], b$block, b$level_2)
  }
  invisible(rows)
}

# The blocks of a decomposition by STL, the classical method or MSTL, in the order the report reads them.
dec_reading <- function(ctx) {
  c(dec_reading_summary(ctx), dec_reading_strength(ctx), dec_reading_seasonality(ctx), dec_reading_season(ctx),
    dec_reading_remainder(ctx), dec_reading_stationarity(ctx), dec_reading_method(ctx), dec_reading_formulas(ctx))
}

.dec_check <- function(ctx, id, level_2 = NULL) ctx$checks[[paste(c(id, level_2), collapse = "|")]]

dec_reading_summary <- function(ctx) {
  o <- ctx$o
  s <- ctx$s
  lines <- c(dec_line(paste0("summary_", o$method), value = o$value, n = s$n, first = s$x0[[1L]], last = s$x0[[s$n]],
                      periods = ctx$periods))
  if (!identical(o$transform, "none")) {
    lines <- c(lines, if (identical(o$transform, "log")) dec_line("transform_log") else dec_line("transform_box_cox", lambda = s$lambda))
  }
  if (s$n_filled > 0L) lines <- c(lines, dec_line(paste0("filled_", o$missing), count = s$n_filled))
  zero <- ctx$fit$weights_zero
  if (isTRUE(o$robust) && !is.na(zero) && zero > 0L) lines <- c(lines, dec_line("robust_weights", count = zero))
  list(dec_block("summary", lines))
}

# The trend's strength and the seasonal strength of each period, in one line (a list of values and of periods).
dec_reading_strength <- function(ctx) {
  ft <- .dec_check(ctx, "trend_strength")$value
  fs <- vapply(ctx$periods, function(p) .dec_check(ctx, "seasonal_strength", as.character(p))$value %||% NA_real_, 0)
  ok <- !is.na(fs)
  if (is.null(ft) || is.na(ft) || !any(ok)) return(list())
  list(dec_block("strength", dec_line("strength", trend = ft, seasonal = fs[ok], period = ctx$periods[ok])))
}

# One line a period, from its test's verdict: supported (p < alpha), not supported, or not tested (fewer than two cycles).
dec_reading_seasonality <- function(ctx) {
  o <- ctx$o
  out <- list()
  for (p in ctx$periods) {
    ck <- .dec_check(ctx, "seasonality", as.character(p))
    if (is.null(ck)) next
    line <- if (identical(ck$verdict, "not_computed")) {
      dec_line("seasonality_not_computed", period = p, cycles = ck$count)
    } else {
      dec_line(if (identical(ck$verdict, "ok")) "seasonality_supported" else "seasonality_not_supported", period = p,
               p_value = ck$p_value, alpha = o$alpha, cycles = ck$count, test = o$seasonality_test)
    }
    out[[length(out) + 1L]] <- dec_block("seasonality", line, as.character(p))
  }
  out
}

# The shape of each season: where it peaks and where it is lowest, with the values (a measure: no verdict).
dec_reading_season <- function(ctx) {
  out <- list()
  for (p in ctx$periods) {
    sh <- ctx$shapes[[as.character(p)]]
    out[[length(out) + 1L]] <- dec_block("season", dec_line(if (ctx$mult) "season_shape_ratio" else "season_shape", period = p,
                                                           peak = sh$codes[[sh$peak]], trough = sh$codes[[sh$trough]],
                                                           peak_value = sh$peak_value, trough_value = sh$trough_value, position = sh$kind),
                                         as.character(p))
  }
  out
}

# Ljung–Box on the remainder: white or structure (from its p-value against alpha). When it rejects while no single
# autocorrelation passes its own bound z_{1−α/2}/√n (the chart's), the rejection is many small ones together: a
# second line says the size of the largest, so that the reader weighs it. Not computed: no line.
dec_reading_remainder <- function(ctx) {
  o <- ctx$o
  lb <- .dec_check(ctx, "ljung_box")
  if (is.null(lb) || identical(lb$verdict, "not_computed")) return(list())
  big <- .dec_check(ctx, "largest_acf")
  at <- list(largest_acf = big$value, at_lag = big$lag)
  lines <- do.call(dec_line, c(list(if (identical(lb$verdict, "ok")) "remainder_white" else "remainder_structure", lag = lb$lag,
                                     p_value = lb$p_value, alpha = o$alpha), at))
  nr <- length(.ts_values(ctx$fit$remainder))
  if (identical(lb$verdict, "failed") && !is.null(big$value) && !is.na(big$value) && big$value < stats::qnorm(1 - o$alpha / 2) / sqrt(nr)) {
    lines <- c(lines, do.call(dec_line, c(list("remainder_large_n"), at)))
  }
  list(dec_block("remainder", lines))
}

# ADF and KPSS read together (opposite null hypotheses): the four cases in words, each from the two p-values against alpha;
# KPSS at an end of its table with an alpha outside it says that instead (no verdict), and no case is said.
dec_reading_stationarity <- function(ctx) {
  o <- ctx$o
  adf <- .dec_check(ctx, "adf")
  kpss <- .dec_check(ctx, "kpss")
  lines <- character()
  if (!is.null(kpss) && identical(kpss$advice_id, "kpss_table_end")) {
    lines <- dec_line("kpss_table_end", bound = if (identical(kpss$p_side, "below")) 0.01 else 0.1, alpha = o$alpha, statistic = kpss$statistic)
  } else if (!is.null(adf) && !is.null(kpss) && !identical(adf$verdict, "not_computed") && !identical(kpss$verdict, "not_computed")) {
    id <- if (adf$verdict == "ok" && kpss$verdict == "ok") "stationarity_both_ok" else if (adf$verdict == "ok") "stationarity_adf_only" else
      if (kpss$verdict == "ok") "stationarity_kpss_only" else "stationarity_neither"
    # KPSS at an end of its table (p ≤ 0.01 or p ≥ 0.10): its own sentences, which say the bound; its p-value is not sent
    side <- kpss$p_side %||% "inside"
    lines <- if (identical(side, "inside")) dec_line(id, p_adf = adf$p_value, p_kpss = kpss$p_value, alpha = o$alpha) else
      dec_line(paste0(id, if (identical(side, "above")) "_kpss_above" else "_kpss_below"), p_adf = adf$p_value, alpha = o$alpha)
  }
  if (!length(lines)) return(list())
  list(dec_block("stationarity", lines))
}

# «How this report was computed»: a line for each part that ran, in the order the report reads them.
dec_reading_method <- function(ctx) {
  o <- ctx$o
  lines <- dec_line(paste0("method_", o$method), period = ctx$periods[[1L]])
  if (!identical(o$transform, "none")) lines <- c(lines, dec_line("method_transform"))
  if (ctx$s$n_filled > 0L) lines <- c(lines, dec_line("method_filled"))
  if (!identical(o$seasonality_test, "none")) lines <- c(lines, dec_line("method_seasonality"))
  lines <- c(lines, dec_line("method_strength"), dec_line("method_remainder"), dec_line("method_stationarity"))
  list(dec_block("method", lines))
}

dec_reading_formulas <- function(ctx) {
  o <- ctx$o
  lines <- dec_line(paste0("formula_", o$method))
  if (!identical(o$transform, "none")) lines <- c(lines, dec_line("formula_box_cox"))
  lines <- c(lines, dec_line("formula_strength"))
  if (!identical(o$seasonality_test, "none")) lines <- c(lines, dec_line(if (identical(o$seasonality_test, "friedman")) "formula_friedman" else "formula_kruskal_wallis"))
  lines <- c(lines, dec_line("formula_ljung_box"), dec_line("formula_adf"), dec_line("formula_kpss"))
  list(dec_block("formulas", lines))
}
