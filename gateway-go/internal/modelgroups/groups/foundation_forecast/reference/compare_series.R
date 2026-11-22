# The R side of the comparison of the series (compare_series.py, docs/specs/foundation-forecast.md, lot F2): ts_series.R
# of the group «timeseries», its library sourced as run.R sources it, run on each case of <work>/cases.json, in the
# timeseries image:
#
#   Rscript compare_series.R <work>        (the modelgroups folder: TABULARIA_MODELGROUPS, else three folders up)
#
# writes <work>/r_out.json (per case: the counts, or the refusal's code, sentence and params; the envelopes' places)
# and <work>/r_<case>.arrow (the span's times as ts_time_array writes them, y, z, filled, x0).

args <- commandArgs(TRUE)
work <- args[[1L]]
here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
mg <- Sys.getenv("TABULARIA_MODELGROUPS", normalizePath(file.path(here, "..", "..", "..")))
for (file in sort(list.files(file.path(mg, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
source(file.path(mg, "groups", "timeseries", "runner", "R", "ts_series.R"))

spec <- jsonlite::fromJSON(file.path(work, "cases.json"), simplifyVector = FALSE)
out <- list()
for (case in spec$cases) {
  if (identical(case$missing, "model")) next  # Python only: compared with the linear case
  Sys.setenv(TABULARIA_INPUT = file.path(work, case$input))
  task <- list(protocol = 1L, mode = "fit", params = list())
  max_periods <- if (is.null(case$max_periods)) TS_MAX_PERIODS else as.integer(case$max_periods)
  out[[case$id]] <- tryCatch({
    s <- ts_series(task, case$time, case$value, case$frequency, missing = case$missing, max_periods = max_periods)
    arrow::write_ipc_file(arrow::arrow_table(time = ts_time_array(s$times, s$type), y = arrow::Array$create(as.numeric(s$y)),
                                            z = arrow::Array$create(as.numeric(s$z)), filled = arrow::Array$create(s$filled),
                                            x0 = arrow::Array$create(as.numeric(s$x0))),
                         file.path(work, paste0("r_", case$id, ".arrow")))
    list(ok = TRUE, kind = s$kind, n = s$n, n_missing = s$n_missing, n_filled = s$n_filled)
  }, tabularia_refusal = function(e) list(ok = FALSE, code = e$code, message = conditionMessage(e), params = e$params))
}
env <- arrow::read_ipc_file(file.path(work, "envelopes.arrow"), as_data_frame = TRUE)
out$envelopes <- list()
for (e in spec$envelopes) out$envelopes[[e$id]] <- as.list(ts_envelope(env[[e$id]][seq_len(e$n)], e$max))
w <- warnings()
if (length(w)) print(unique(names(w)))
writeLines(tab_json(out), file.path(work, "r_out.json"), useBytes = TRUE)
cat("R:", length(out) - 1L, "series\n")
