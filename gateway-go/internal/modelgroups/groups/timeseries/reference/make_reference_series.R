# The tables of the series and the checks of the decomposition (docs/specs/ts-decomposition.md, «Self-test,
# reference values, witnesses»; lot D1), synthetic, with fixed seeds. In the group's image, from the repository's
# root:
#
#   docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" -w /ref \
#     tabularia-models-timeseries:dev Rscript make_reference_series.R
#
#   dec_types.csv   100 days; the same instants as an integer above 2^31, an integer, a date and an hour (the
#                   self-test writes each as its own Arrow type: Int64, Int32, Int16, date32, timestamp[us, UTC],
#                   timestamp[ns], timestamp[ms, Europe/Rome]); two empty values inside (filled, flagged)
#   dec_gaps.csv    120 months from 2015-01, a pattern proportional to the level; the first month empty (before the
#                   span), two months without a row and one with an empty value inside it
#   dec_checks.csv  400 days from 2024-01-01: white noise, an AR(1), a random walk, a weekly pattern on a trend, a
#                   random walk with noise (no season), a white noise with a mild trend (KPSS inside its table)
#
# Their witnesses are computed outside R by make_witness_series.py (statsmodels, SciPy, pandas) into
# data/series_witness.json, which the self-test (ts_series_tests.R, ts_checks_tests.R) reads.

here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")
w <- function(df, name) utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", fileEncoding = "UTF-8")

set.seed(20261201)
n <- 100
days <- seq(as.Date("2026-01-05"), by = "day", length.out = n)
value <- round(50 + 5 * sin(2 * pi * seq_len(n) / 7) + stats::rnorm(n), 4)
value[c(30, 31)] <- NA
w(data.frame(t_int64 = format(5e9 + seq_len(n) - 1, scientific = FALSE), t_int = seq_len(n) - 1L,
             t_date = format(days, "%Y-%m-%d"),
             t_hour = format(as.POSIXct("2026-01-05 00:00:00", tz = "UTC") + 3600 * (seq_len(n) - 1), "%Y-%m-%d %H:%M:%S"),
             value = value), "dec_types.csv")

set.seed(20261202)
n <- 120
months <- seq(as.Date("2015-01-01"), by = "month", length.out = n)
level <- 200 * exp(0.004 * seq_len(n))
season <- 1 + 0.25 * sin(2 * pi * seq_len(n) / 12) + 0.08 * cos(2 * pi * seq_len(n) / 6)
boxes <- round(level * season * exp(stats::rnorm(n, sd = 0.03)), 2)
df <- data.frame(month = format(months, "%Y-%m-%d"), boxes = boxes)
df$boxes[1] <- NA                                   # before the span
df$boxes[months == as.Date("2021-03-01")] <- NA     # inside: an empty value
df <- df[!months %in% as.Date(c("2016-05-01", "2019-08-01")), ]  # inside: no row
w(df, "dec_gaps.csv")

set.seed(20261203)
n <- 400
t <- seq_len(n)
w(data.frame(day = format(seq(as.Date("2024-01-01"), by = "day", length.out = n), "%Y-%m-%d"),
             white = round(stats::rnorm(n), 6),
             ar = round(as.numeric(stats::arima.sim(list(ar = 0.5), n = n)), 6),
             walk = round(cumsum(stats::rnorm(n)), 6),
             weekly = round(10 + 0.02 * t + rep(c(1.2, 0.8, 0.5, 0.3, 0.9, -1.5, -2.2), length.out = n) + stats::rnorm(n, sd = 0.7), 6),
             noseason = round(cumsum(stats::rnorm(n, sd = 0.3)) + stats::rnorm(n), 6),
             mild = round(stats::rnorm(n) + 0.0006 * t, 6)), "dec_checks.csv")
cat("written: dec_types.csv, dec_gaps.csv, dec_checks.csv\n")
