# The explanation of an EMD's report, chosen by the runner (docs/specs/ts-decomposition.md, «Explaining the method in
# the report»; lot D3). No sentence is written here: each row of the section `reading` is a template's id with the
# run's raw numbers and names,
#
#   reading|<block>|<period or empty>|line_<k>  =  @<id> name=value name=value…
#
# in the notes' format (a space, a comma, a percent inside a value are %20, %2C, %25; a number keeps the digits that
# read back as the same double), said in the page's language by the layout from report_texts/decomposition.json
# (`reading_texts`, `formula_texts`; lot D4). Each line carries exactly the names its template reads. A test's line
# says its verdict; a measure's (the IMFs' periods, the strengths) only its numbers.

# ── the line's format ───────────────────────────────────────────────────────────
.emd_number <- function(x) {
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

.emd_text <- function(x) {
  x <- enc2utf8(as.character(x))
  x <- gsub("%", "%25", x, fixed = TRUE)
  x <- gsub(" ", "%20", x, fixed = TRUE)
  gsub(",", "%2C", x, fixed = TRUE)
}

# «@id name=value …»: NULL and NA values are left out.
emd_line <- function(id, ...) {
  v <- list(...)
  parts <- character()
  for (name in names(v)) {
    x <- v[[name]]
    if (is.null(x) || !length(x) || all(is.na(x))) next
    said <- if (is.numeric(x)) .emd_number(x) else .emd_text(x)
    said[is.na(said)] <- ""
    parts <- c(parts, paste0(name, "=", paste(said, collapse = ",")))
  }
  paste0("@", id, if (length(parts)) paste0(" ", paste(parts, collapse = " ")) else "")
}

.emd_block <- function(block, lines, level_2 = NULL) list(block = block, level_2 = level_2, lines = unlist(lines))

emd_write_reading <- function(rows, blocks) {
  for (b in blocks) {
    if (is.null(b) || !length(b$lines)) next
    for (k in seq_along(b$lines)) tab_text(rows, "reading", paste0("line_", k), b$lines[[k]], b$block, b$level_2)
  }
  invisible(rows)
}

# ── the blocks ──────────────────────────────────────────────────────────────────
# r: the run as fit_decomposition_emd holds it (o the options, s the series, d the decomposition, k noise_imfs,
# checks, season, measures, p the period or NULL).
emd_reading <- function(r) {
  list(emd_reading_summary(r), emd_reading_strength(r), emd_reading_seasonality(r), emd_reading_season(r),
       emd_reading_remainder(r), emd_reading_stationarity(r), emd_reading_imfs(r), emd_reading_method(r),
       emd_reading_formulas(r))
}

emd_reading_summary <- function(r) {
  s <- r$s
  lines <- list(emd_line("summary_emd", value = r$o$value, n = s$n))
  if (identical(r$o$transform, "log")) lines <- c(lines, emd_line("transform_log"))
  if (identical(r$o$transform, "box_cox")) lines <- c(lines, emd_line("transform_box_cox", lambda = s$lambda))
  if (s$n_filled > 0L) lines <- c(lines, emd_line(paste0("filled_", r$o$missing), count = s$n_filled))
  .emd_block("summary", lines)
}

# The strengths need a remainder (noise_imfs > 0): F_T of the residue, F_S of the oscillations, said with the period
# the seasonality test reads; without a period, F_T alone (strength_trend). Without noise_imfs they are not computed.
emd_reading_strength <- function(r) {
  if (r$k == 0L) return(.emd_block("strength", list(emd_line("strength_not_computed"))))
  trend <- r$checks$trend_strength$value
  seasonal <- r$checks$seasonal_strength$value
  if (is.null(trend) || is.na(trend)) return(NULL)
  if (is.null(seasonal) || is.na(seasonal)) return(.emd_block("strength", list(emd_line("strength_trend", trend = trend))))
  .emd_block("strength", list(emd_line("strength", trend = trend, seasonal = seasonal, period = r$p)))
}

emd_reading_seasonality <- function(r) {
  c <- r$checks$seasonality
  if (is.null(c)) return(NULL)
  line <- switch(c$verdict,
    ok = emd_line("seasonality_supported", period = r$p, p_value = c$p_value, alpha = r$o$alpha, cycles = c$count,
                  test = r$o$seasonality_test),
    failed = emd_line("seasonality_not_supported", period = r$p, p_value = c$p_value, alpha = r$o$alpha, cycles = c$count,
                      test = r$o$seasonality_test),
    emd_line("seasonality_not_computed", period = r$p, cycles = c$count))
  .emd_block("seasonality", list(line), as.character(r$p))
}

emd_reading_season <- function(r) {
  x <- r$season
  if (is.null(x)) return(NULL)
  .emd_block("season", list(emd_line("season_shape", period = r$p, peak = x$peak_label, trough = x$trough_label,
                                     peak_value = x$peak_value, trough_value = x$trough_value, position = x$position_kind)),
             as.character(r$p))
}

emd_reading_remainder <- function(r) {
  if (r$k == 0L) return(.emd_block("remainder", list(emd_line("remainder_not_computed_emd"))))
  lb <- r$checks$ljung_box
  big <- r$checks$largest_acf
  if (!lb$verdict %in% c("ok", "failed")) return(NULL)
  .emd_block("remainder", list(emd_line(if (lb$verdict == "ok") "remainder_white" else "remainder_structure", lag = lb$lag,
                                        p_value = lb$p_value, alpha = r$o$alpha, largest_acf = big$value, at_lag = big$lag)))
}

# ADF and KPSS read together (four cases), or KPSS at an end of its table. ADF without a verdict says nothing here.
emd_reading_stationarity <- function(r) {
  a <- r$checks$adf
  k <- r$checks$kpss
  if (identical(k$verdict, "not_computed") && identical(k$advice_id, "kpss_table_end")) {
    return(.emd_block("stationarity", list(emd_line("kpss_table_end", bound = if (identical(k$p_side, "below")) 0.01 else 0.1,
                                                    alpha = r$o$alpha, statistic = k$statistic))))
  }
  if (!a$verdict %in% c("ok", "failed") || !k$verdict %in% c("ok", "failed")) return(NULL)
  id <- if (a$verdict == "ok") {
    if (k$verdict == "ok") "stationarity_both_ok" else "stationarity_adf_only"
  } else {
    if (k$verdict == "ok") "stationarity_kpss_only" else "stationarity_neither"
  }
  # KPSS at an end of its table with a verdict: p ≥ 0.10 (it keeps) or p ≤ 0.01 (it rejects), said by the id («As built /
  # D4»), without KPSS's clipped p-value
  side <- k$p_side %||% "inside"
  if (!identical(side, "inside")) {
    return(.emd_block("stationarity", list(emd_line(paste0(id, if (side == "above") "_kpss_above" else "_kpss_below"),
                                                    p_adf = a$p_value, alpha = r$o$alpha))))
  }
  .emd_block("stationarity", list(emd_line(id, p_adf = a$p_value, p_kpss = k$p_value, alpha = r$o$alpha)))
}

# The IMFs found of those allowed; the cut when the residue still oscillates, else the empty columns; each IMF's mean
# period (a measure).
emd_reading_imfs <- function(r) {
  d <- r$d
  lines <- list(emd_line("imfs_found", n_imfs = d$n_imfs, max_imf = r$o$max_imf))
  if (d$cut && d$columns - 1L < r$o$max_imf) {  # the series' own limit (libeemd's floor(log2 n) - 1), not max_imf
    lines <- c(lines, emd_line("imfs_cut_most", n = r$s$n, most = d$most, extrema = d$residue_extrema))
  } else if (d$cut) {
    lines <- c(lines, emd_line("imfs_cut", max_imf = r$o$max_imf, extrema = d$residue_extrema))
  } else if (d$n_imfs < r$o$max_imf) {
    lines <- c(lines, emd_line("imfs_empty", first = d$n_imfs + 1L, last = r$o$max_imf))
  }
  for (j in seq_len(d$n_imfs)) {
    period <- r$measures[[j]]$mean_period
    if (!is.na(period)) lines <- c(lines, emd_line("imf_mode", imf = j, mean_period = period))
  }
  .emd_block("imfs", lines)
}

# «How this report was computed»: a paragraph per part that ran.
emd_reading_method <- function(r) {
  lines <- list(emd_line("method_emd"))
  if (!identical(r$o$transform, "none")) lines <- c(lines, emd_line("method_transform"))
  if (r$s$n_filled > 0L) lines <- c(lines, emd_line("method_filled"))
  if (!is.null(r$checks$seasonality)) lines <- c(lines, emd_line("method_seasonality"))
  if (r$k > 0L) lines <- c(lines, emd_line("method_strength"), emd_line("method_remainder"))
  lines <- c(lines, emd_line("method_stationarity"))
  .emd_block("method", lines)
}

emd_reading_formulas <- function(r) {
  lines <- list(emd_line("formula_emd"), emd_line("formula_orthogonality"))
  if (!identical(r$o$transform, "none")) lines <- c(lines, emd_line("formula_box_cox"))
  if (!is.null(r$checks$seasonality)) lines <- c(lines, emd_line(paste0("formula_", r$o$seasonality_test)))
  if (r$k > 0L) lines <- c(lines, emd_line("formula_strength"), emd_line("formula_ljung_box"))
  lines <- c(lines, emd_line("formula_adf"), emd_line("formula_kpss"))
  .emd_block("formulas", lines)
}
