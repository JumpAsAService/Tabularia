# The tables of the decomposition's EMD (docs/specs/ts-decomposition.md, «Self-test, reference values, witnesses»;
# lot D3), synthetic, with fixed seeds. In the group's image, from the repository's root (the directory must be
# writable by the image's user):
#
#   docker run --rm --network none -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" -w /ref \
#     tabularia-models-timeseries:dev Rscript make_reference_emd.R
#
#   emd_tones.csv   1,024 steps: sines of period 8 (amplitude 2) and 64 (amplitude 4), a linear trend, a small noise:
#                   the IMFs' mean periods near 8 and 64, the residue monotonic
#   emd_short.csv   40 days from 2026-01-05 (a Monday): a weekly sine and a noise. libeemd gives a series of 40 values
#                   at most ⌊log2 40⌋ − 1 = 4 IMFs, fewer than max_imf 10: the empty columns, n_imfs
#   emd_cut.csv     1,024 steps (the spec said 2,048: half, for the self-test's ConfigMap): five sines (periods 6, 20,
#                   70, 250, 900) and a small noise; with max_imf 3 the residue still oscillates: residue_monotonic failed
#
# Values are rounded to 4 decimals, so that the CSV is the series every witness reads. Their witnesses are computed
# outside R by make_witness_emd.py (a transcription of libeemd, PyEMD) into data/emd_witness.json, which the self-test
# (decomposition_emd_tests.R) reads.

here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")
w <- function(df, name) utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", fileEncoding = "UTF-8")

set.seed(20261211)
t <- seq_len(1024)
w(data.frame(t = t, signal = round(2 * sin(2 * pi * t / 8) + 4 * sin(2 * pi * t / 64) + 0.01 * t + stats::rnorm(1024, sd = 0.05), 4)),
  "emd_tones.csv")

set.seed(20261212)
t <- seq_len(40)
w(data.frame(day = format(as.Date("2026-01-05") + t - 1L, "%Y-%m-%d"),
             visits = round(20 + 3 * sin(2 * pi * t / 7) + stats::rnorm(40), 4)), "emd_short.csv")

set.seed(20261213)
t <- seq_len(1024)
w(data.frame(t = t, signal = round(sin(2 * pi * t / 6) + 1.5 * sin(2 * pi * t / 20) + 2 * sin(2 * pi * t / 70) +
                                     3 * sin(2 * pi * t / 250) + 4 * sin(2 * pi * t / 900) + stats::rnorm(1024, sd = 0.05), 4)),
  "emd_cut.csv")
cat("written: emd_tones.csv, emd_short.csv, emd_cut.csv\n")
