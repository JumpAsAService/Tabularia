# The reference values of ARIMA's automatic orders (lane M7), computed OUTSIDE Tabularia:
# sourced by make_reference.R (its helpers add, add_text, case, read), after the manual cases.
#
# Each case calls forecast::auto.arima directly, with the node's settings and trace = TRUE,
# on the series built here from the CSV, and expects:
#
#   - the chosen orders, exactly, and the chosen model's name;
#   - every candidate auto.arima printed (its trace, before any refit without the
#     approximation), by name, with its criterion as printed (7 significant digits, relative
#     1e-6; empty when auto.arima printed Inf) and chosen 1 or 0; the count of candidates;
#   - without the approximation, each kept candidate's AIC, AICc and BIC by their definitions
#     (stats::arima with CSS-ML, the drift a regressor 1…n, forecast's npar and n*), 1e-9;
#   - the tests that chose D and d, by their packages: the seasonal strength by its definition
#     on forecast::mstl (as nsdiffs' «seas»), OCSB's statistic and critical value,
#     urca::ur.kpss / ur.df / ur.pp at each difference with the p-value read from urca's
#     table as ndiffs reads it; D and d checked against nsdiffs and ndiffs themselves; the
#     steps' keys (orders_reason_k);
#   - the chosen fit's coefficients (auto.arima's own fit) and, without a holdout, its
#     forecast.
#
# The cases, on the tables of data/ (no new table):
#   arima_auto_nonseasonal   ar_index.csv, default settings (KPSS, AICc, stepwise)
#   arima_auto_seasonal      sarima_monthly.csv on the log, holdout 12: D = 1 by the seasonal strength
#   arima_auto_exog          arimax_weekly.csv with its two exogenous columns, s = 1
#   arima_auto_drift_ocsb    drift_quarterly.csv: drift allowed, Box–Cox, OCSB, ADF, BIC
#   arima_auto_approx_all    arimax_weekly.csv's sales alone, s = 52: too short to test the season,
#                            the approximation (s > 12), every order (stepwise off), PP, AIC

auto_case <- function(id, data, types, params, y, X = NULL, lambda = NULL, h = 0L) {
  o <- list(criterion = "aicc", stepwise = TRUE, unit_root_test = "kpss", seasonal_test = "seas", drift = FALSE, mean = TRUE)
  for (k in names(o)) if (!is.null(params[[k]])) o[[k]] <- params[[k]]
  m <- frequency(y)
  # by value (do.call): forecast() evaluates the call's xreg again, and a name would find another table's
  args <- list(y, ic = o$criterion, stepwise = o$stepwise, test = o$unit_root_test, seasonal.test = o$seasonal_test,
               allowdrift = o$drift, allowmean = o$mean, lambda = lambda, trace = TRUE)
  if (!is.null(X)) args$xreg <- X
  out <- capture.output(fit <- do.call(auto.arima, args))
  refit <- grep("re-fitting", out, fixed = TRUE)
  approximation <- any(grepl("approximations", out, fixed = TRUE))
  lines <- if (length(refit)) out[seq_len(refit[[1L]] - 1L)] else out
  pattern <- "^ *(Regression with )?(ARIMA\\([0-9]+,[0-9]+,[0-9]+\\)(\\([0-9]+,[0-9]+,[0-9]+\\)\\[[0-9]+\\])?) *(errors)? *(with drift|with non-zero mean|with zero mean)? *: *(\\S+) *$"
  lines <- grep(pattern, lines, value = TRUE)
  # the transformed series and the regressors as auto.arima's candidates see them
  x <- if (is.null(lambda)) y else BoxCox(y, lambda)
  n_obs <- sum(!is.na(x))
  ic_of <- function(order, seasonal, constant) {
    dd <- order[[2L]] + seasonal[[2L]]
    xr <- X
    if (constant && dd == 1L) xr <- cbind(drift = seq_along(x), X)
    f <- tryCatch(suppressWarnings(stats::arima(x, order = order, seasonal = list(order = seasonal, period = m),
                                                xreg = xr, include.mean = constant, method = "CSS-ML")),
                  error = function(e) NULL)
    if (is.null(f)) return(NULL)
    npar <- length(f$coef[f$mask]) + 1
    nstar <- n_obs - order[[2L]] - seasonal[[2L]] * m
    c(aic = f$aic, aicc = f$aic + 2 * npar * (npar + 1) / (nstar - npar - 1), bic = f$aic + npar * (log(nstar) - 2))
  }
  seen <- character()
  unnamed <- 0L
  for (line in lines) {
    g <- regmatches(line, regexec(pattern, line))[[1L]]
    nums <- as.integer(regmatches(g[[3L]], gregexpr("[0-9]+", g[[3L]]))[[1L]])
    order <- nums[1:3]
    seasonal <- if (length(nums) > 3L) nums[4:6] else c(0L, 0L, 0L)
    dd <- order[[2L]] + seasonal[[2L]]
    printed <- as.numeric(g[[7L]])
    base <- paste0("ARIMA(", order[1], ",", order[2], ",", order[3], ")",
                   if (m > 1 && sum(seasonal) > 0) paste0("(", seasonal[1], ",", seasonal[2], ",", seasonal[3], ")[", m, "]") else "")
    constant <- if (nzchar(g[[6L]])) g[[6L]] %in% c("with drift", "with non-zero mean") else if (nzchar(g[[2L]])) NA else FALSE
    if (is.na(constant)) {
      # «Regression with … errors» does not say whether the model has a mean: the fit whose criterion
      # is the printed one says it (a model printed Inf is left out of the expectations)
      if (!is.finite(printed) || dd > 1L) {
        unnamed <- unnamed + 1L
        next
      }
      hits <- Filter(function(k) { v <- ic_of(order, seasonal, k); !is.null(v) && abs(v[[o$criterion]] - printed) <= 1e-6 * abs(printed) },
                     c(TRUE, FALSE))
      if (length(hits) != 1L) stop(id, ": cannot tell whether ", line, " has a mean")
      constant <- hits[[1L]]
    }
    name <- paste0(base, if (constant && dd == 1L) " with drift" else if (constant && dd == 0L) " with mean" else "")
    if (name %in% seen) next
    seen <- c(seen, name)
    key <- function(stat) paste0("candidate|", name, "||", stat)
    if (!is.finite(printed)) {
      add(key(o$criterion), NA)
      next
    }
    add(key(o$criterion), printed, rel = 1e-6)
    if (!approximation) {
      v <- ic_of(order, seasonal, constant)
      if (abs(v[[o$criterion]] - printed) > 1e-6 * abs(printed)) stop(id, ": ", name, " gives ", v[[o$criterion]], ", printed ", printed)
      for (s in c("aic", "aicc", "bic")) add(key(s), v[[s]])
    }
  }
  # the chosen model
  a <- fit$arma
  orders <- c(p = a[1], d = a[6], q = a[2], P = a[3], D = a[7], Q = a[4])
  constant <- any(c("drift", "intercept") %in% names(coef(fit)))
  dd <- orders[["d"]] + orders[["D"]]
  chosen <- paste0("ARIMA(", orders[["p"]], ",", orders[["d"]], ",", orders[["q"]], ")",
                   if (m > 1 && sum(orders[c("P", "D", "Q")]) > 0) paste0("(", orders[["P"]], ",", orders[["D"]], ",", orders[["Q"]], ")[", m, "]") else "",
                   if (constant && dd == 1L) " with drift" else if (constant && dd == 0L) " with mean" else "")
  if (!chosen %in% seen) stop(id, ": the chosen ", chosen, " is not among the printed candidates")
  for (name in seen) add(paste0("candidate|", name, "||chosen"), as.numeric(name == chosen))
  add("model|||candidates", length(seen) + unnamed)
  for (k in names(orders)) add(paste0("model|||", k), orders[[k]])
  seasonal <- m > 1 && sum(orders[c("P", "D", "Q")]) > 0
  model_name <- if (seasonal) {
    sprintf("%s(%d,%d,%d)(%d,%d,%d)[%d]", if (is.null(X)) "SARIMA" else "SARIMAX", orders[["p"]], orders[["d"]], orders[["q"]],
            orders[["P"]], orders[["D"]], orders[["Q"]], m)
  } else if (!is.null(X)) {
    sprintf("ARIMAX(%d,%d,%d)", orders[["p"]], orders[["d"]], orders[["q"]])
  } else if (orders[["d"]] == 0 && orders[["q"]] == 0) {
    sprintf("AR(%d)", orders[["p"]])
  } else if (orders[["d"]] == 0 && orders[["p"]] == 0) {
    sprintf("MA(%d)", orders[["q"]])
  } else if (orders[["d"]] == 0) {
    sprintf("ARMA(%d,%d)", orders[["p"]], orders[["q"]])
  } else sprintf("ARIMA(%d,%d,%d)", orders[["p"]], orders[["d"]], orders[["q"]])
  add_text("model|||model_name", model_name)
  add_text("model|||criterion", o$criterion)
  labels <- names(coef(fit))
  if (!is.null(X)) labels[match(colnames(X), labels)] <- colnames(X)
  for (i in seq_along(labels)) add(paste0("coef|", labels[[i]], "||estimate"), coef(fit)[[i]])
  if (!approximation) for (s in c("aic", "aicc", "bic")) add(paste0("model|||", s), fit[[s]])
  if (h > 0L) {
    fc <- forecast(fit, h = h, level = c(80, 95))
    add("curve|forecast||forecast|1", fc$mean[1])
    add(paste0("curve|forecast||forecast|", h), fc$mean[h])
    add(paste0("curve|forecast||upper|", h), fc$upper[h, 2])
  }
  # the steps: the regression's residuals with exogenous columns, D, d, the search
  xx <- x
  keys <- character()
  if (!is.null(X)) {
    j <- !is.na(x) & !is.na(rowSums(X))
    xx[j] <- residuals(lm(x ~ X))
    keys <- "exog_residuals"
  }
  if (m > 1) {
    if (length(xx) <= 2 * m) {
      keys <- c(keys, "D_short")
      D <- 0
    } else {
      D <- nsdiffs(xx, test = o$seasonal_test)
      if (o$seasonal_test == "seas") {
        st <- mstl(xx)
        remainder <- st[, "Remainder"]
        strength <- max(0, min(1, 1 - var(remainder, na.rm = TRUE) / var(remainder + st[, grep("Season", colnames(st))[1]], na.rm = TRUE)))
        add("model|||order_test_seasonal_strength", strength)
        asks <- strength > 0.64
      } else {
        oc <- forecast:::ocsb.test(xx, maxlag = 3, lag.method = "AIC")
        add("model|||order_test_ocsb_statistic", oc$statistics)
        add("model|||order_test_ocsb_critical", oc$critical)
        asks <- oc$statistics > oc$critical
      }
      if (asks != (D == 1)) stop(id, ": the seasonal test says ", asks, ", nsdiffs D = ", D)
      keys <- c(keys, paste0(if (o$seasonal_test == "seas") "D_seas_" else "D_ocsb_", if (asks) "yes" else "no"))
    }
    if (D != orders[["D"]]) stop(id, ": D = ", D, " by the test, ", orders[["D"]], " chosen")
  }
  dx <- if (orders[["D"]] > 0) diff(xx, lag = m, differences = orders[["D"]]) else xx
  z <- c(na.omit(c(dx)))
  k <- 0
  repeat {
    u <- suppressWarnings(switch(o$unit_root_test,
      kpss = urca::ur.kpss(z, type = "mu", use.lag = trunc(3 * sqrt(length(z)) / 13)),
      adf = urca::ur.df(z, type = "drift"),
      pp = urca::ur.pp(z, type = "Z-tau", model = "constant")))
    p <- approx(u@cval[1, ], as.numeric(sub("pct", "", colnames(u@cval))) / 100, xout = u@teststat[1], rule = 2)$y
    add(sprintf("model|||order_test_%s_d%d_statistic", o$unit_root_test, k), u@teststat[1])
    add(sprintf("model|||order_test_%s_d%d_p_value", o$unit_root_test, k), p)
    asks <- if (o$unit_root_test == "kpss") p < 0.05 else p > 0.05
    if (!asks) {
      keys <- c(keys, paste0("d_keep_", k))
      break
    }
    if (k >= 2) {
      keys <- c(keys, paste0("d_max_", k))
      break
    }
    keys <- c(keys, paste0("d_diff_", k))
    k <- k + 1
    z <- diff(z)
  }
  if (k != ndiffs(dx, test = o$unit_root_test) || k != orders[["d"]]) stop(id, ": d = ", k, " by the steps")
  keys <- c(keys, paste0("search_", if (o$stepwise) "stepwise" else "all", if (approximation) "_approx" else ""))
  for (i in seq_along(keys)) add_text(paste0("model|||orders_reason_", i), keys[[i]])
  case(id, data, types, c(params, list(orders = "auto")))
}

# ── non-seasonal, default settings ──────────────────────────────────────────
d <- read("ar_index.csv")
auto_case("arima_auto_nonseasonal", "ar_index.csv", list(t = "integer", level = "number"),
          list(time = "t", value = "level", frequency = "index"), ts(num(d$level)), h = 12L)

# ── seasonal on the log, holdout 12: D = 1 ──────────────────────────────────
d <- read("sarima_monthly.csv")
auto_case("arima_auto_seasonal", "sarima_monthly.csv", list(month = "date", boxes = "number"),
          list(time = "month", value = "boxes", frequency = "month", transform = "log", holdout = 12),
          ts(num(d$boxes)[1:132], frequency = 12), lambda = 0)

# ── two exogenous columns, weekly, s = 1 ────────────────────────────────────
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
y <- sales[span]
y[!complete.cases(X[span, ])] <- NA
auto_case("arima_auto_exog", "arimax_weekly.csv", list(week = "date", sales = "number", price = "number", "promo`flag" = "number"),
          list(time = "week", value = "sales", frequency = "week", s = 1, exog = list("price", "promo`flag")),
          ts(y), X = X[span, , drop = FALSE])

# ── drift, Box–Cox, OCSB, ADF, BIC, quarters counted by an integer ──────────
d <- read("drift_quarterly.csv")
y <- ts(num(d$revenue), frequency = 4)
auto_case("arima_auto_drift_ocsb", "drift_quarterly.csv", list(q = "integer", revenue = "number"),
          list(time = "q", value = "revenue", frequency = "quarter", drift = TRUE, transform = "box_cox", criterion = "bic",
               unit_root_test = "adf", seasonal_test = "ocsb", horizon = 8), y, lambda = BoxCox.lambda(y), h = 8L)

# ── the approximation and every order: weekly sales alone, s = 52 ───────────
auto_case("arima_auto_approx_all", "arimax_weekly.csv",
          list(week = "date", sales = "number", price = "number", "promo`flag" = "number"),
          list(time = "week", value = "sales", frequency = "week", stepwise = FALSE, criterion = "aic", unit_root_test = "pp"),
          ts(sales[span], frequency = 52), h = 12L)
