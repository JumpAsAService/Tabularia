# The points of the charts, capped (docs: «Points are capped»). A runner sends numbers and
# points, never pictures: the report engine draws them. Per chart at most max_chart_points;
# above it a sample drawn with the node's seed, always keeping the most extreme points,
# labelled with their row numbers. A Q–Q chart uses qq_points quantiles of all the
# residuals, a histogram counts all of them, a step curve keeps every step up to the cap and
# its value at equally spaced times above it.

# The indices of the points to draw: all up to `max`; above it the `keep` largest by
# `score` (|standardized residual|, Cook's distance…) and a sample of the rest. In
# increasing order. The seed is the node's (set by the caller with tab_seed).
tab_cap <- function(n, max = 2000, score = NULL, keep = 10) {
  if (n <= max) return(seq_len(n))
  extreme <- if (is.null(score)) integer() else head(order(-abs(score), seq_len(n)), keep)
  rest <- setdiff(seq_len(n), extreme)
  sort(c(extreme, rest[sort(sample.int(length(rest), max - length(extreme)))]))
}

# The labels of the extreme points (their row numbers in the input, from 1), NA elsewhere.
tab_extreme_labels <- function(score, rows, keep = 10) {
  labels <- rep(NA_character_, length(score))
  top <- head(order(-abs(score), seq_along(score)), keep)
  labels[top] <- as.character(rows[top])
  labels
}

# A normal Q–Q chart: `points` quantiles of all the values, the theoretical ones in x.
tab_qq <- function(x, points = 200) {
  x <- sort(x[is.finite(x)])
  n <- length(x)
  if (n == 0L) return(data.frame(x = numeric(), y = numeric()))
  p <- if (n <= points) stats::ppoints(n) else stats::ppoints(points)
  y <- if (n <= points) x else stats::quantile(x, p, type = 7, names = FALSE)
  data.frame(x = stats::qnorm(p), y = y)
}

# The line of a Q–Q chart: through the first and third quartiles (as qqline).
tab_qq_line <- function(x) {
  y <- stats::quantile(x[is.finite(x)], c(0.25, 0.75), names = FALSE, type = 7)
  q <- stats::qnorm(c(0.25, 0.75))
  slope <- diff(y) / diff(q)
  c(intercept = y[[1L]] - slope * q[[1L]], slope = slope)
}

# A histogram of all the values: between 5 and 30 intervals (Sturges' rule within them), as
# the statistics' reports count.
tab_hist <- function(x, min_bins = 5, max_bins = 30) {
  x <- x[is.finite(x)]
  if (!length(x)) return(data.frame(x0 = numeric(), x1 = numeric(), count = numeric()))
  bins <- min(max_bins, max(min_bins, grDevices::nclass.Sturges(x)))
  lo <- min(x)
  hi <- max(x)
  if (lo == hi) {
    lo <- lo - 0.5
    hi <- hi + 0.5
  }
  breaks <- seq(lo, hi, length.out = bins + 1L)
  counts <- graphics::hist(x, breaks = breaks, plot = FALSE, include.lowest = TRUE, right = TRUE)$counts
  data.frame(x0 = head(breaks, -1L), x1 = tail(breaks, -1L), count = counts)
}

# A step function (a survival curve) at most `max` points: every step when there are few,
# else its value at `max` equally spaced times (the height there is exact: the last step at
# or before each time).
tab_thin_steps <- function(time, value, max = 2000) {
  if (length(time) <= max) return(data.frame(x = time, y = value))
  at <- seq(min(time), max(time), length.out = max)
  i <- findInterval(at, time)
  data.frame(x = at, y = ifelse(i == 0L, NA_real_, value[pmax(i, 1L)]))
}

# A smooth line through points (lowess, as plot.lm draws it), at most `max` points.
tab_lowess <- function(x, y, max = 200) {
  ok <- is.finite(x) & is.finite(y)
  if (sum(ok) < 3L) return(data.frame(x = numeric(), y = numeric()))
  l <- stats::lowess(x[ok], y[ok])
  keep <- unique(round(seq(1L, length(l$x), length.out = min(max, length(l$x)))))
  data.frame(x = l$x[keep], y = l$y[keep])
}
