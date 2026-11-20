# The reference values of the group «timeseries», computed OUTSIDE Tabularia
# (docs/specs/statistical-models.md, «Reference values»): by hand, with R and the pinned
# packages of ../recipe.json, never by the runner. In the group's image, from the repository's
# root:
#
#   docker run --rm --memory=2g -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" \
#     -w /ref tabularia-models-timeseries:dev Rscript make_reference.R          # expected.json
#   (… Rscript make_reference.R --data)                                        # data/*.csv, once
#
# The tables of data/ are synthetic, simulated with fixed seeds (tens to a few hundred rows):
#
#   ar_index.csv          AR(2) around 50, an integer time 1…120, frequency index
#   sarima_monthly.csv    144 months from 2014-01, trend and a season of 12, positive (log)
#   arimax_weekly.csv     104 weeks and 8 future rows, two exogenous columns (one named like
#                         code), a week without a row and a week with an empty value
#   drift_quarterly.csv   80 quarters counted by an integer, a drift, Box–Cox
#   duplicate_monthly.csv a month held twice (refused)
#   short_monthly.csv     20 months, too few for a season of 12 (refused)
#
# Each case calls forecast::Arima, forecast::forecast, forecast::accuracy, tseries,
# FinTS and stats directly, on the series built here from the CSV (its own grid, its own
# missing weeks), and writes the numbers the runner must reproduce (relative 1e-9). The
# runner's own pieces (the four cases of ADF and KPSS, the roots' moduli, the first third
# against the last third) are computed here by their definitions. The second witness,
# statsmodels' SARIMAX, is make_witness.py (1e-4 relative on the coefficients). The cases of
# the automatic orders (forecast::auto.arima called directly) are in arima_auto.R, sourced
# below.
#
# The format is common/R/selftest.R's: {"format_version": 1, "generated": {…}, "cases": [{"id",
# "model", "data", "types", "params", "expect": [{"key", "value" | "text", "rel", "abs"}],
# "refuses"}]}.

suppressMessages({
  library(forecast)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")

# ── the tables (run once with --data) ───────────────────────────────────────
write_data <- function() {
  dir.create(data_dir, showWarnings = FALSE)
  w <- function(df, name) utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", fileEncoding = "UTF-8")
  set.seed(20261120)
  level <- 50 + as.numeric(arima.sim(list(ar = c(0.6, -0.3)), n = 120, sd = 2))
  w(data.frame(t = 1:120, level = round(level, 4)), "ar_index.csv")

  set.seed(20261121)
  n <- 144
  # a SARIMA(1,1,1)(0,1,1)[12] on the log: (1 − 0.6B)(1 − B)(1 − B¹²) log y = (1 + 0.4B)(1 − 0.6B¹²) ε
  innov <- as.numeric(arima.sim(list(ar = 0.6, ma = c(0.4, rep(0, 10), -0.6, -0.24)), n = n - 13, sd = 0.02))
  season <- 0.12 * sin(2 * pi * (1:12) / 12) + 0.05 * cos(2 * pi * (1:12) / 6)
  log_y <- 6 + as.numeric(diffinv(diffinv(innov, lag = 12, xi = rep(0, 12)), xi = 0)) + rep(season, length.out = n)
  months <- seq(as.Date("2014-01-01"), by = "month", length.out = n)
  w(data.frame(month = format(months, "%Y-%m-%d"), boxes = round(exp(log_y), 2)), "sarima_monthly.csv")

  set.seed(20261122)
  n <- 112
  weeks <- seq(as.Date("2023-01-02"), by = "week", length.out = n)
  price <- round(3 + 0.4 * sin(seq_len(n) / 5) + runif(n, -0.2, 0.2), 2)
  promo <- as.integer(runif(n) < 0.25)
  err <- as.numeric(arima.sim(list(ar = 0.5, ma = 0.3), n = n, sd = 6))
  sales <- round(200 - 30 * price + 25 * promo + err, 2)
  df <- data.frame(week = format(weeks, "%Y-%m-%d"), sales = sales, price = price, check.names = FALSE)
  df[["promo`flag"]] <- promo
  df$sales[105:112] <- NA          # the future rows: exogenous given, value empty
  df$sales[70] <- NA               # a week with an empty value
  df <- df[-40, ]                  # a week without a row
  w(df, "arimax_weekly.csv")

  set.seed(20261123)
  n <- 80
  e <- as.numeric(arima.sim(list(ma = -0.4), n = n, sd = 0.04))
  log_y <- 3 + cumsum(0.015 + e) + 0.05 * rep(c(1, -0.5, 0.3, -0.8), length.out = n)
  w(data.frame(q = 1:n, revenue = round(exp(log_y), 3)), "drift_quarterly.csv")

  w(data.frame(month = c("2024-01-01", "2024-02-01", "2024-02-15", "2024-03-01"), boxes = c(10, 12, 11, 13)),
    "duplicate_monthly.csv")
  w(data.frame(month = format(seq(as.Date("2024-01-01"), by = "month", length.out = 20), "%Y-%m-%d"),
               boxes = round(100 + 10 * sin(1:20), 2)), "short_monthly.csv")
}

if ("--data" %in% args) {
  write_data()
  cat("data/*.csv written\n")
  quit(save = "no")
}

# ── helpers ─────────────────────────────────────────────────────────────────
read <- function(name) utils::read.csv(file.path(data_dir, name), check.names = FALSE, colClasses = "character",
                                       na.strings = "", encoding = "UTF-8")
num <- function(x) as.numeric(x)
expect <- list()
add <- function(key, value, rel = 1e-9, abs = 1e-12) {
  expect[[length(expect) + 1L]] <<- list(key = key, value = if (is.na(value) || !is.finite(value)) NULL else unname(value),
                                         rel = rel, abs = abs)
}
add_text <- function(key, text) expect[[length(expect) + 1L]] <<- list(key = key, text = text)
sig <- function(p) if (p < 0.001) "***" else if (p < 0.01) "**" else if (p < 0.05) "*" else if (p < 0.1) "." else ""
add_p <- function(prefix, p) {
  add(paste0(prefix, "p_value"), p)
  add_text(paste0(prefix, "significance"), sig(p))
}

# The checks of a fit, by their packages and their definitions.
checks <- function(fit, z, order, seasonal, s, alpha, drift, mean_ = FALSE) {
  res <- as.numeric(residuals(fit))
  zz <- z[!is.na(z)]
  adf <- suppressWarnings(tseries::adf.test(zz))
  kpss <- suppressWarnings(tseries::kpss.test(zz, null = if (drift && order[2] + seasonal[2] == 0) "Trend" else "Level"))
  add("assumption|adf||statistic", adf$statistic)
  add_p("assumption|adf||", adf$p.value)
  add_text("assumption|adf||verdict", if (adf$p.value < alpha) "ok" else "failed")
  add("assumption|kpss||statistic", kpss$statistic)
  add_p("assumption|kpss||", kpss$p.value)
  add_text("assumption|kpss||verdict", if (kpss$p.value < alpha) "failed" else "ok")
  a <- adf$p.value < alpha
  k <- kpss$p.value < alpha
  add_text("assumption|stationarity||verdict", if (a && !k) "ok" else if (!a && k) "failed" else "doubtful")
  fitdf <- order[1] + order[3] + seasonal[1] + seasonal[3]
  b <- Box.test(res, lag = 10, type = "Ljung-Box", fitdf = fitdf)
  add("assumption|ljung_box_10||statistic", b$statistic)
  add("assumption|ljung_box_10||df", b$parameter)
  add_p("assumption|ljung_box_10||", b$p.value)
  if (s > 1) {
    b <- Box.test(res, lag = 2 * s, type = "Ljung-Box", fitdf = fitdf)
    add("assumption|ljung_box_2s||statistic", b$statistic)
    add("assumption|ljung_box_2s||df", b$parameter)
    add_p("assumption|ljung_box_2s||", b$p.value)
  } else {
    add_text("assumption|ljung_box_2s||verdict", "not_computed")
  }
  e <- res[!is.na(res)]
  arch <- FinTS::ArchTest(e, lags = if (s > 1) s else 12)
  add("assumption|arch_lm||statistic", arch$statistic)
  add("assumption|arch_lm||df", arch$parameter)
  add_p("assumption|arch_lm||", arch$p.value)
  # the first third of the residuals against the last, after the d + D·s diffuse ones
  burn <- order[2] + seasonal[2] * s
  e2 <- if (burn > 0) res[-seq_len(burn)] else res
  e2 <- e2[!is.na(e2)]
  h <- round(length(e2) / 3)
  ratio <- sum(e2[(length(e2) - h + 1):length(e2)]^2) / sum(e2[1:h]^2)
  add("assumption|variance_ratio||statistic", ratio)
  add("assumption|variance_ratio||df", h)
  add_p("assumption|variance_ratio||", 2 * min(pf(ratio, h, h), 1 - pf(ratio, h, h)))
  jb <- tseries::jarque.bera.test(e)
  add("assumption|jarque_bera||statistic", jb$statistic)
  add_p("assumption|jarque_bera||", jb$p.value)
  # the roots of each polynomial in its own operator, sorted, AR then MA
  cf <- coef(fit)
  roots <- function(prefix, k, sign) if (k == 0) numeric() else sort(Mod(polyroot(c(1, sign * cf[paste0(prefix, 1:k)]))))
  ar <- c(roots("ar", order[1], -1), roots("sar", seasonal[1], -1))
  ma <- c(roots("ma", order[3], 1), roots("sma", seasonal[3], 1))
  for (i in seq_along(ar)) add(paste0("model|||ar_root_modulus_", i), ar[i])
  for (i in seq_along(ma)) add(paste0("model|||ma_root_modulus_", i), ma[i])
  m <- min(c(ar, ma))
  add("assumption|roots||value", m)
  add_text("assumption|roots||verdict", if (m <= 1 + 1e-8) "failed" else if (m < 1.05) "doubtful" else "ok")
}

fit_numbers <- function(fit, alpha, labels = names(coef(fit))) {
  cf <- coef(fit)
  se <- sqrt(diag(fit$var.coef))
  for (i in seq_along(cf)) {
    add(paste0("coef|", labels[i], "||estimate"), cf[i])
    add(paste0("coef|", labels[i], "||std_error"), se[i])
    z <- cf[i] / se[i]
    add(paste0("coef|", labels[i], "||statistic"), z)
    add_p(paste0("coef|", labels[i], "||"), 2 * pnorm(-abs(z)))
    add(paste0("coef|", labels[i], "||ci_low"), cf[i] - qnorm(1 - alpha / 2) * se[i])
  }
  acc <- accuracy(fit)
  fields <- c(log_lik = "loglik", aic = "aic", aicc = "aicc", bic = "bic", sigma2 = "sigma2")
  for (k in names(fields)) add(paste0("model|||", k), fit[[fields[[k]]]])
  add("model|||rmse", acc[1, "RMSE"])
  add("model|||mae", acc[1, "MAE"])
  add("model|||mape", acc[1, "MAPE"])
  add("model|||mase", acc[1, "MASE"])
}

forecast_points <- function(fc, at, alpha) {
  lv <- c(80, 100 * (1 - alpha))
  for (i in at) {   # index 0 of the chart's forecast is the last observation; step i is index i
    add(paste0("curve|forecast||forecast|", i), fc$mean[i])
    add(paste0("curve|forecast||lower_80|", i), fc$lower[i, match(80, lv)])
    add(paste0("curve|forecast||upper|", i), fc$upper[i, 2])
  }
}

cases <- list()
case <- function(id, data, types, params, refuses = NULL) {
  cases[[length(cases) + 1L]] <<- list(id = id, model = "arima", data = paste0("data/", data), types = types, params = params,
                                       expect = expect, refuses = refuses)
  expect <<- list()
}

# ── AR(2) on an integer time, frequency index ───────────────────────────────
d <- read("ar_index.csv")
y <- ts(num(d$level))
fit <- Arima(y, order = c(2, 0, 0), include.mean = TRUE)
fit_numbers(fit, 0.05)
checks(fit, as.numeric(y), c(2, 0, 0), c(0, 0, 0), 1, 0.05, FALSE)
fc <- forecast(fit, h = 12, level = c(80, 95))
forecast_points(fc, c(1, 12), 0.05)
add("model|||n", 120)
add("model|||n_missing", 0)
add_text("model|||model_name", "AR(2)")
add_text("model|||time_kind", "integer")
add("curve|forecast||observed|0", y[1])
add("curve|forecast||fitted|119", fitted(fit)[120])
case("arima_ar_index", "ar_index.csv", list(t = "integer", level = "number"),
     list(time = "t", value = "level", frequency = "index", p = 2, d = 0, q = 0))

# ── SARIMA(1,1,1)(0,1,1)[12] on the log, holdout of 12 ──────────────────────
d <- read("sarima_monthly.csv")
y_all <- ts(num(d$boxes), frequency = 12)
train <- ts(y_all[1:132], frequency = 12)
fit <- Arima(train, order = c(1, 1, 1), seasonal = list(order = c(0, 1, 1), period = 12), lambda = 0)
fit_numbers(fit, 0.05)
z <- diff(diff(log(as.numeric(train)), lag = 12), differences = 1)
checks(fit, z, c(1, 1, 1), c(0, 1, 1), 12, 0.05, FALSE)
hold <- forecast(fit, h = 12, level = c(80, 95))
acc <- accuracy(hold, y_all[133:144])
add("model|||holdout_rmse", acc["Test set", "RMSE"])
add("model|||holdout_mae", acc["Test set", "MAE"])
add("model|||holdout_mape", acc["Test set", "MAPE"])
add("model|||holdout_mase", acc["Test set", "MASE"])
whole <- Arima(y_all, model = fit)   # forecast's own way of applying a fit to more data
fc <- forecast(whole, h = 12, level = c(80, 95))
forecast_points(fc, c(1, 6, 12), 0.05)
add("curve|forecast||fitted|143", fitted(whole)[144])
add("model|||n", 132)
add("model|||holdout", 12)
add_text("model|||model_name", "SARIMA(1,1,1)(0,1,1)[12]")
add_text("model|||time_kind", "date")
add_text("model|||formula", "log(boxes) ~ SARIMA(1,1,1)(0,1,1)[12]")
case("arima_sarima_log_holdout", "sarima_monthly.csv", list(month = "date", boxes = "number"),
     list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 1, P = 0, D = 1, Q = 1,
          transform = "log", holdout = 12, horizon = 12))

# ── ARIMAX(1,0,1) with two exogenous columns and future rows, weekly ────────
d <- read("arimax_weekly.csv")
weeks <- seq(as.Date(d$week[1]), as.Date(d$week[nrow(d)]), by = "week")
at <- match(as.Date(d$week), weeks)
sales <- rep(NA_real_, length(weeks))
sales[at] <- num(d$sales)
X <- matrix(NA_real_, length(weeks), 2, dimnames = list(NULL, c("price", "promo`flag")))
X[at, 1] <- num(d$price)
X[at, 2] <- num(d[["promo`flag"]])
last <- max(which(!is.na(sales)))
span <- 1:last
future <- (last + 1):length(weeks)
fit <- Arima(ts(sales[span]), order = c(1, 0, 1), xreg = X[span, ], include.mean = TRUE)
fit_numbers(fit, 0.05)
checks(fit, sales[span], c(1, 0, 1), c(0, 0, 0), 1, 0.05, FALSE)
fc <- forecast(fit, xreg = X[future, ], level = c(80, 95))
forecast_points(fc, c(1, 8), 0.05)
add("model|||n", sum(!is.na(sales[span])))
add("model|||n_missing", sum(is.na(sales[span])))
add("model|||horizon", length(future))
add_text("model|||model_name", "ARIMAX(1,0,1)")
add_text("model|||formula", "sales ~ price + promo`flag, errors ARIMA(1,0,1)")
case("arima_arimax_weekly_future_rows", "arimax_weekly.csv",
     list(week = "date", sales = "number", price = "number", "promo`flag" = "number"),
     list(time = "week", value = "sales", frequency = "week", s = 1, p = 1, d = 0, q = 1,
          exog = list("price", "promo`flag")))

# ── SARIMA(0,1,1)(0,0,1)[4] with drift and Box–Cox, an integer time counting quarters ──
d <- read("drift_quarterly.csv")
y <- ts(num(d$revenue), frequency = 4)
lambda <- BoxCox.lambda(y)
fit <- Arima(y, order = c(0, 1, 1), seasonal = list(order = c(0, 0, 1), period = 4), include.drift = TRUE, lambda = lambda)
fit_numbers(fit, 0.1)
z <- diff(as.numeric(BoxCox(as.numeric(y), lambda)))
checks(fit, z, c(0, 1, 1), c(0, 0, 1), 4, 0.1, TRUE)
fc <- forecast(fit, h = 8, level = c(80, 90))
forecast_points(fc, c(1, 8), 0.1)
add("model|||box_cox_lambda", lambda)
add_text("model|||model_name", "SARIMA(0,1,1)(0,0,1)[4]")
add_text("model|||time_kind", "integer")
case("arima_drift_box_cox_quarterly", "drift_quarterly.csv", list(q = "integer", revenue = "number"),
     list(time = "q", value = "revenue", frequency = "quarter", p = 0, d = 1, q = 1, P = 0, D = 0, Q = 1,
          drift = TRUE, transform = "box_cox", horizon = 8, alpha = 0.1))

# ── refusals ────────────────────────────────────────────────────────────────
case("arima_duplicate_time", "duplicate_monthly.csv", list(month = "date", boxes = "number"),
     list(time = "month", value = "boxes", frequency = "month", p = 1),
     refuses = list(code = "duplicate_time", message = paste0("the column «month» holds two rows for the period of 2024-02-15: ",
                                                              "a series has one row per period; aggregate first with Group by")))
case("arima_too_short", "short_monthly.csv", list(month = "date", boxes = "number"),
     list(time = "month", value = "boxes", frequency = "month", p = 1, d = 0, q = 0, P = 0, D = 1, Q = 1),
     refuses = list(code = "too_short", message = paste0("the series has 20 observations; SARIMA(1,0,0)(0,1,1)[12] needs at ",
                                                         "least 35: a longer series, or lower orders")))

# ── automatic orders (lane M7): auto.arima called directly, in arima_auto.R ──
source(file.path(here, "arima_auto.R"), local = TRUE)

versions <- c(R = paste(R.version$major, R.version$minor, sep = "."))
for (p in c("stats", "forecast", "tseries", "FinTS", "urca")) versions[[p]] <- as.character(packageVersion(p))
out <- list(format_version = 1L,
            generated = list(by = "make_reference.R", R = versions[["R"]], packages = as.list(versions[-1])),
            cases = cases)
writeLines(toJSON(out, auto_unbox = TRUE, null = "null", na = "null", digits = NA, pretty = TRUE),
           file.path(here, "expected.json"), useBytes = TRUE)
cat(length(cases), "cases written to expected.json\n")
