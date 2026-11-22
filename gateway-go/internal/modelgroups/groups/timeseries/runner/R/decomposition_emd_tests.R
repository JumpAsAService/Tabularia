# The self-test of the decomposition's EMD (decomposition_emd.R, lot D3): added to TAB_COMMON_TESTS. The witnesses are
# reference/data/emd_witness.json (make_witness_emd.py: a transcription of libeemd in Python with SciPy's splines, the
# runner's cut written again, numpy's measures, PyEMD), the tables make_reference_emd.R's.

.emd_witness <- function(code) tab_ref_json(file.path(code, "reference", "data", "emd_witness.json"), simplifyVector = FALSE)

.emd_near <- function(got, want, rel = 1e-12, abs = 1e-15) {
  got <- as.numeric(got)
  want <- as.numeric(vapply(want, function(v) if (is.null(v)) NA_real_ else as.numeric(v), 0))
  length(got) == length(want) && all((is.na(got) & is.na(want)) | (!is.na(got) & !is.na(want) & abs(got - want) <= pmax(abs, rel * abs(want))))
}

.emd_task <- function(df, params = list()) {
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "decomposition",
       variant = "emd", params = c(list(method = "emd"), params), output = "results", write_output = FALSE, rows_from = "run",
       limits = tab_catalog()$limits, .frame = df)
}

.emd_table <- function(code, table) {
  types <- if (identical(table, "emd_short.csv")) list(day = "date", visits = "number") else list(t = "integer", signal = "number")
  tab_read_csv(file.path(code, "reference", "data", table), types)
}

TAB_COMMON_TESTS[["timeseries: EMD equals a transcription of libeemd, and stops where the residue has at most one extremum (emd_witness)"]] <- function(code) {
  for (name in names(.emd_witness(code)$cases)) {
    w <- .emd_witness(code)$cases[[name]]
    x <- .emd_table(code, w$table)[[w$column]]
    d <- emd_decompose(x, w$max_imf, w$s_number, w$max_siftings)
    .tab_expect(d$columns == w$columns && d$n_imfs == w$n_imfs && d$residue_extrema == w$residue_extrema,
                sprintf("%s: %d columns, %d IMFs, a residue of %d extrema; the witness %d, %d, %d", name, d$columns, d$n_imfs,
                        d$residue_extrema, w$columns, w$n_imfs, w$residue_extrema))
    # the IMFs kept and the residue: Rlibeemd's against the transcription (measured: 1.3e-14 at most, «As built / D3»)
    modes <- cbind(d$imfs, d$residue)
    scale <- max(abs(x))
    at <- unlist(w$at)
    for (j in seq_len(ncol(modes))) {
      m <- w$modes[[j]]
      .tab_expect(.emd_near(modes[at, j], m$values, rel = 0, abs = 1e-10 * scale) &&
                  .emd_near(sum(modes[, j]), m$sum, rel = 1e-9, abs = 1e-10 * scale * length(x)) &&
                  .emd_near(sum(modes[, j]^2), m$sum_squares, rel = 1e-10),
                  sprintf("%s: mode %d differs from the transcription by %.3g", name, j, max(abs(modes[at, j] - unlist(m$values)))))
    }
    .tab_expect(max(abs(rowSums(modes) - x)) <= 1e-10 * scale, name, ": Σ IMF + residue is not the series")
    # the measures and the orthogonality index, by numpy on the transcription's modes
    for (j in seq_len(d$n_imfs)) {
      got <- emd_mode_measures(d$imfs[, j], x)
      want <- w$imfs[[j]]
      .tab_expect(.emd_near(got$mean_period, list(want$mean_period)) && got$mean_period_time == got$mean_period &&
                  .emd_near(got$variance_share, list(want$variance_share), rel = 1e-10) &&
                  .emd_near(got$energy_share, list(want$energy_share), rel = 1e-10) &&
                  got$extrema == want$extrema && got$zero_crossings == want$zero_crossings, name, ": the measures of IMF ", j)
    }
    got <- emd_mode_measures(d$residue, x, residue = TRUE)
    .tab_expect(is.na(got$mean_period) && .emd_near(got$variance_share, list(w$residue$variance_share), rel = 1e-10) &&
                got$extrema == w$residue$extrema && got$zero_crossings == w$residue$zero_crossings, name, ": the residue's measures")
    io <- emd_orthogonality(d$imfs, d$residue, x)
    .tab_expect(abs(io - w$orthogonality) <= 1e-12, sprintf("%s: IO %.17g, the witness %.17g", name, io, w$orthogonality))
  }
  # the counts are libeemd's: a flat run is one extremum, the ends are none
  .tab_expect(emd_extrema(c(1, 2, 2, 2, 1, 0, 0, 3)) == 2L && emd_maxima(c(1, 2, 2, 2, 1, 0, 0, 3)) == 1L &&
              emd_extrema(c(3, 2, 1)) == 0L && emd_extrema(c(1, 2, 1)) == 1L && emd_zero_crossings(c(1, 0, -1, 0, 2)) == 2L, "the counts")
}

TAB_COMMON_TESTS[["timeseries: EMD's IMFs repeat as PyEMD's do, within 5 % (emd_tones)"]] <- function(code) {
  w <- .emd_witness(code)
  x <- .emd_table(code, "emd_tones.csv")$signal
  d <- emd_decompose(x, 10L, 4L, 50L)
  ours <- vapply(seq_len(d$n_imfs), function(j) emd_mode_measures(d$imfs[, j], x)$mean_period, 0)
  theirs <- unlist(w$pyemd$tones)
  for (target in c(8, 64)) {
    a <- ours[[which.min(abs(ours - target))]]
    b <- theirs[[which.min(abs(theirs - target))]]
    .tab_expect(abs(a - b) <= 0.05 * b && abs(a - target) <= 0.05 * target, sprintf("near %g: %.4g here, %.4g by PyEMD", target, a, b))
  }
}

TAB_COMMON_TESTS[["timeseries: EMD's output: the IMFs and the residue add up to the series, the columns after the last IMF are empty, the time keeps its type"]] <- function(code) {
  df <- .emd_table(code, "emd_short.csv")
  for (transform in c("none", "log")) {
    out <- fit_decomposition(.emd_task(df, list(time = "day", value = "visits", frequency = "day", transform = transform)))
    t <- as.data.frame(out$output)
    want <- c("time", "observed", if (transform != "none") "transformed", "filled", paste0("imf_", 1:10), "residue")
    .tab_expect(identical(names(t), want), "columns: ", paste(names(t), collapse = ","))
    .tab_expect(out$output$schema$GetFieldByName("time")$type == arrow::date32(), "the time is not a date32")
    rows <- tab_results_frame(out$rows)
    k <- rows$value[rows$key == "model|||n_imfs"]
    .tab_expect(k == 3L && all(is.na(as.matrix(t[paste0("imf_", 4:10)]))) && !anyNA(as.matrix(t[paste0("imf_", 1:3)])), "the empty columns")
    z <- if (transform == "none") t$observed else t$transformed
    .tab_expect(max(abs(rowSums(as.matrix(t[c(paste0("imf_", 1:3), "residue")])) - z)) <= 1e-10 * max(abs(z)), transform, ": the identity")
    .tab_expect(identical(rows$text[rows$key == "reading|imfs||line_2"], "@imfs_empty first=4 last=10"), "the reading of the empty columns")
    problems <- tab_check_declared(rows, "decomposition", "emd")
    .tab_expect(!length(problems), problems[1])
  }
  # an Int64 time stays Int64
  big <- data.frame(t = bit64::as.integer64("5000000000") + 0:39, visits = df$visits)
  out <- fit_decomposition(.emd_task(big, list(time = "t", value = "visits", frequency = "index")))
  .tab_expect(out$output$schema$GetFieldByName("time")$type == arrow::int64() &&
              identical(as.character(as.data.frame(out$output)$time[[40]]), "5000000039"), "an Int64 time")
}

TAB_COMMON_TESTS[["timeseries: EMD's checks: no remainder without noise_imfs, the fastest IMFs as the remainder with it, the facts"]] <- function(code) {
  df <- .emd_table(code, "emd_tones.csv")
  x <- df$signal
  d <- emd_decompose(x, 10L, 4L, 50L)
  get <- function(rows, key) rows$value[rows$key == key]
  txt <- function(rows, key) rows$text[rows$key == key]
  # noise_imfs = 0: Ljung–Box not computed, ADF on x − r, no strength, no remainder chart
  rows <- tab_results_frame(fit_decomposition(.emd_task(df, list(time = "t", value = "signal", frequency = "index", period = 8L)))$rows)
  .tab_expect(identical(txt(rows, "assumption|ljung_box||verdict"), "not_computed") && identical(txt(rows, "assumption|ljung_box||advice_id"), "emd_no_noise"),
              "Ljung–Box without noise_imfs")
  .tab_expect(identical(get(rows, "assumption|adf||statistic"), ts_adf(rowSums(d$imfs), 0.05)$statistic), "ADF on the oscillating part")
  .tab_expect(!any(rows$section %in% c("point", "hist", "lag")) && !"assumption|trend_strength||value" %in% rows$key, "a remainder without noise_imfs")
  .tab_expect(identical(txt(rows, "reading|remainder||line_1"), "@remainder_not_computed_emd") &&
              identical(txt(rows, "reading|strength||line_1"), "@strength_not_computed"), "the readings without noise_imfs")
  # noise_imfs = 1: R = c₁, T = r, S = c₂
  rows <- tab_results_frame(fit_decomposition(.emd_task(df, list(time = "t", value = "signal", frequency = "index", period = 8L, noise_imfs = 1L)))$rows)
  lb <- ts_ljung_box(d$imfs[, 1], get(rows, "model|||lb_lag_used"), 0.05)
  .tab_expect(identical(get(rows, "assumption|ljung_box||statistic"), lb$statistic), "Ljung–Box on IMF 1")
  .tab_expect(identical(get(rows, "assumption|trend_strength||value"), ts_strength(d$residue, d$imfs[, 1])) &&
              identical(get(rows, "assumption|seasonal_strength|8|value"), ts_strength(d$imfs[, 2], d$imfs[, 1])), "the strengths")
  .tab_expect(sum(rows$section == "point") > 0 && sum(rows$section == "lag") > 0, "the remainder's charts")
  # KPSS at an end of its table with a verdict is said by the id, without its clipped p-value («As built / D4»)
  k <- ts_kpss(d$imfs[, 1], 0.05)
  line <- txt(rows, "reading|stationarity||line_1")
  .tab_expect(identical(k$p_side, "above") && startsWith(line, "@stationarity_") && endsWith(sub(" .*", "", line), "_kpss_above") &&
              !grepl("p_kpss=", line, fixed = TRUE), "the stationarity reading at KPSS's table end: ", line)
  # the facts: every IMF of emd_tones is one; emd_cut with max_imf 3 leaves a residue that turns 8 times
  .tab_expect(all(txt(rows, c("assumption|imf_condition|imf_1|verdict", "assumption|imf_condition|imf_2|verdict")) == "ok"), "the IMF condition")
  cut <- .emd_table(code, "emd_cut.csv")
  rows <- tab_results_frame(fit_decomposition(.emd_task(cut, list(time = "t", value = "signal", frequency = "index", max_imf = 3L)))$rows)
  .tab_expect(identical(txt(rows, "assumption|residue_monotonic||verdict"), "failed") && get(rows, "assumption|residue_monotonic||count") == 8 &&
              identical(txt(rows, "assumption|residue_monotonic||advice_id"), "residue_monotonic_failed") &&
              identical(txt(rows, "reading|imfs||line_2"), "@imfs_cut max_imf=3 extrema=8"), "the residue of emd_cut")
}

TAB_COMMON_TESTS[["timeseries: EMD refuses with a sentence: too short, a constant, more noise IMFs than IMFs found"]] <- function(code) {
  task <- function(v, params = list()) .emd_task(data.frame(t = seq_along(v), v = v), c(list(time = "t", value = "v", frequency = "index"), params))
  m <- .tab_refused_with("too_short", fit_decomposition(task(c(1, 3, 2))))
  .tab_expect(identical(m, "the series has 3 values; EMD needs at least 4: a longer series"), m)
  m <- .tab_refused_with("constant", fit_decomposition(task(rep(2, 10))))
  .tab_expect(identical(m, "the column «v» has the same value everywhere: there is nothing to decompose"), m)
  df <- .emd_table(code, "emd_short.csv")
  m <- .tab_refused_with("noise_imfs_above", fit_decomposition(.emd_task(df, list(time = "day", value = "visits", frequency = "day", noise_imfs = 5L))))
  .tab_expect(identical(m, "EMD found 3 IMFs; 5 cannot be noise: choose at most 3"), m)
  # a straight line has no IMF: everything is the residue
  rows <- tab_results_frame(fit_decomposition(task(seq(1, 20)))$rows)
  .tab_expect(rows$value[rows$key == "model|||n_imfs"] == 0 && identical(rows$text[rows$key == "assumption|residue_monotonic||verdict"], "ok"),
              "a straight line")
}

TAB_COMMON_TESTS[["timeseries: EMD's reading says F_T alone without a period, and the series' own limit of IMFs (lot D4)"]] <- function(code) {
  b <- emd_reading_strength(list(k = 1L, checks = list(trend_strength = list(value = 0.5)), p = NULL))
  .tab_expect(identical(b$lines, "@strength_trend trend=0.5"), "F_T without a period: ", paste(b$lines, collapse = " | "))
  d <- list(n_imfs = 4L, cut = TRUE, columns = 5L, most = 4L, residue_extrema = 3L)
  b <- emd_reading_imfs(list(o = list(max_imf = 10L), s = list(n = 64L), d = d, measures = rep(list(list(mean_period = NA)), 4)))
  .tab_expect(identical(b$lines, c("@imfs_found n_imfs=4 max_imf=10", "@imfs_cut_most n=64 most=4 extrema=3")),
              "the series' own limit: ", paste(b$lines, collapse = " | "))
  b <- emd_reading_imfs(list(o = list(max_imf = 4L), s = list(n = 64L), d = d, measures = rep(list(list(mean_period = NA)), 4)))
  .tab_expect(identical(b$lines[2], "@imfs_cut max_imf=4 extrema=3"), "max_imf's limit: ", paste(b$lines, collapse = " | "))
}
