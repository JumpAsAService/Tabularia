# ARIMA with automatic orders (docs/specs/statistical-models.md, `arima`, «Automatic orders»,
# lane M7): the search of forecast::auto.arima (Hyndman–Khandakar), its candidates recorded,
# then the chosen orders fitted by arima.R's arima_fit_orders, so that both variants write the
# same rows, checks and charts.
#
#   fit_arima_auto(task)   the orders auto.arima chooses on the training part of the series
#
# The search. auto.arima itself is called, once, with the node's settings: the seasonal test
# (`seasonal_test`, nsdiffs) chooses D, the unit-root test (`unit_root_test`, ndiffs, at 0.05)
# chooses d, then the stepwise search (or every order up to p + q + P + Q ≤ 5 with stepwise
# off) minimizes the criterion; the drift and the mean are tried where the node allows them
# (`drift`, `mean`: auto.arima's allowdrift, allowmean) and the differences let them. Its
# own internals are not copied: the call runs auto.arima's code with its helpers seen through
# one environment, where myarima (the fit of a candidate), ndiffs and nsdiffs are wrapped to
# record what they did. So the candidates are exactly the models auto.arima fitted, in its
# order, with its criterion (an approximation of the likelihood, CSS, when auto.arima uses
# one: a series longer than 150 or a season longer than 12), and the chosen one is its
# choice. The tests' statistics are computed again by the steps of ndiffs and nsdiffs (the
# same functions, the same arguments), and their d and D must be the wrapped functions' own:
# the self-test compares the chosen orders with auto.arima called directly.
#
# What it writes, besides arima_fit_orders's rows: the text `criterion`; one `orders_reason_k`
# per step, whose text is a key the report says in its language with the numbers of the step
# (`order_test_*`): «D_seas_yes» with order_test_seasonal_strength, «d_diff_0» with
# order_test_kpss_d0_statistic and order_test_kpss_d0_p_value, «search_stepwise» with
# `candidates` (the keys: ARIMA_AUTO_KEYS below); and per candidate (section `candidate`,
# level «ARIMA(1,1,0)(0,1,1)[12]», « with drift» or « with mean» after it when it has one):
# aicc, aic, bic (empty when auto.arima left the model out: the fit failed, a root is within
# 1.01 of the unit circle, a standard error is not a number), converged (the fit succeeded and
# its optimizer converged), chosen.

ARIMA_AUTO_SEASONAL_STRENGTH <- 0.64 # nsdiffs' threshold of the seasonal strength
ARIMA_AUTO_ALPHA <- 0.05             # ndiffs' and nsdiffs' level

# The keys of the steps (the texts of orders_reason_k), for the report's words:
#   exog_residuals                  with exogenous columns the tests read the regression's residuals
#   D_short                         too short to test a season (at most two seasons)
#   D_seas_yes, D_seas_no, D_seas_capped     the seasonal strength above 0.64 or not; above, but max_D = 0
#   D_ocsb_yes, D_ocsb_no, D_ocsb_capped     the OCSB statistic above its critical value or not
#   D_error                         the seasonal test failed: no seasonal difference
#   D_dropped                       the seasonal difference left out (an exogenous column constant after it)
#   d_diff_k, d_keep_k, d_max_k     the unit-root test at difference k asks for one more, does not, asks at max_d
#   d_constant_k, d_error_k         the k-th difference is constant; the test failed there
#   d_dropped_k                     one difference left out (an exogenous column constant after it): d = k
#   search_stepwise, search_all     the chosen model has the lowest criterion of the candidates
#   search_stepwise_approx, search_all_approx   the same, the candidates compared by an approximation
ARIMA_AUTO_KEYS <- c("exog_residuals", "D_short", "D_seas_yes", "D_seas_no", "D_seas_capped", "D_ocsb_yes", "D_ocsb_no",
                     "D_ocsb_capped", "D_error", "D_dropped", paste0("d_", rep(c("diff", "keep", "max", "constant", "error",
                     "dropped"), each = 3L), "_", 0:2), "search_stepwise", "search_all", "search_stepwise_approx",
                     "search_all_approx")

fit_arima_auto <- function(task) {
  prep <- arima_prepare(task)
  o <- prep$params
  s <- o$s
  lambda <- arima_lambda(prep)
  y <- stats::ts(prep$y[prep$train], frequency = max(1L, s))
  X <- NULL
  if (length(o$exog)) {
    X <- prep$X[prep$train, , drop = FALSE]
    colnames(X) <- paste0("xreg", seq_along(o$exog))
  }
  search <- arima_auto_search(y, X, lambda, o)
  fit <- search$fit
  a <- fit$arma
  orders <- list(p = a[[1L]], d = a[[6L]], q = a[[2L]], P = a[[3L]], D = a[[7L]], Q = a[[4L]])
  orders <- lapply(orders, as.integer)
  dd <- orders$d + orders$D
  constant <- any(c("drift", "intercept") %in% names(fit$coef))
  # the chosen model's mean or drift, as arima_spec reads them
  prep$params$mean <- constant && dd == 0L
  prep$params$drift <- constant && dd == 1L
  extra <- function(rows) arima_auto_rows(rows, o, search, orders, constant)
  arima_fit_orders(task, prep, orders, extra)
}

# ── the search ──────────────────────────────────────────────────────────────
# auto.arima run with its helpers wrapped (see the header): the fit, the candidates in the order
# fitted, the steps of D and d.
arima_auto_search <- function(y, X, lambda, o) {
  ns <- asNamespace("forecast")
  env <- new.env(parent = ns)
  real <- list(myarima = get("myarima", ns), ndiffs = get("ndiffs", ns), nsdiffs = get("nsdiffs", ns))
  calls <- list()
  steps <- list(seasonal = NULL, nonseasonal = NULL)
  env$myarima <- function(x, order = c(0, 0, 0), seasonal = c(0, 0, 0), constant = TRUE, ic = "aic", trace = FALSE,
                          approximation = FALSE, offset = 0, xreg = NULL, method = NULL, ...) {
    fit <- real$myarima(x, order = order, seasonal = seasonal, constant = constant, ic = ic, trace = trace,
                        approximation = approximation, offset = offset, xreg = xreg, method = method, ...)
    calls[[length(calls) + 1L]] <<- list(order = as.integer(order), seasonal = as.integer(seasonal),
                                         constant = isTRUE(as.logical(constant)), approximation = isTRUE(approximation),
                                         fit = fit[c("ic", "aic", "aicc", "bic", "code")])
    fit
  }
  env$nsdiffs <- function(x, ...) {
    D <- real$nsdiffs(x, ...)
    r <- arima_auto_seasonal_steps(x, list(...)$test, list(...)$max.D)
    r$D <- D
    steps$seasonal <<- r
    D
  }
  env$ndiffs <- function(x, ...) {
    d <- real$ndiffs(x, ...)
    r <- arima_auto_unit_root_steps(x, list(...)$test, list(...)$max.d)
    r$d <- d
    steps$nonseasonal <<- r
    d
  }
  if (!identical(names(formals(env$myarima)), names(formals(real$myarima)))) {
    stop("forecast's myarima has other arguments than the ones arima_auto.R wraps: check the recipe's forecast version")
  }
  auto <- get("auto.arima", ns)
  environment(auto) <- env
  env$search.arima <- get("search.arima", ns)
  environment(env$search.arima) <- env
  fit <- tryCatch(
    auto(y, max.p = as.integer(o$max_p), max.q = as.integer(o$max_q), max.P = as.integer(o$max_P),
         max.Q = as.integer(o$max_Q), max.d = as.integer(o$max_d), max.D = as.integer(o$max_D),
         ic = o$criterion, stepwise = isTRUE(o$stepwise), test = o$unit_root_test, seasonal.test = o$seasonal_test,
         allowdrift = isTRUE(o$drift), allowmean = isTRUE(o$mean), lambda = lambda, xreg = X),
    error = function(e) e)
  if (inherits(fit, "error")) {
    m <- conditionMessage(fit)
    if (grepl("rank deficient", m, fixed = TRUE)) {
      tab_refuse("exog_collinear", paste0("the exogenous columns are collinear (one is constant, or a combination of the ",
                                          "others): leave one out"), list())
    }
    if (grepl("No suitable ARIMA model", m, fixed = TRUE)) {
      tab_refuse("no_model", paste0("no ARIMA model within the orders allowed could be fitted to the series: raise max_p ",
                                    "or max_q, or choose the orders by hand"), list())
    }
    tab_refuse("fit_failed", paste0("the automatic choice of the orders failed (", m, "): choose the orders by hand"),
               list(model = "auto"))
  }
  if (!length(calls)) {
    # auto.arima returns before any search when the series, or its differences, is constant
    tab_refuse("constant_series", paste0("the column «", o$value, "» is constant once differenced (a constant, or a straight ",
                                         "line): there is nothing left for an ARIMA to model"), list(column = o$value))
  }
  list(fit = fit, calls = calls, steps = steps, approximation = any(vapply(calls, `[[`, logical(1), "approximation")),
       exog = !is.null(X) && !all(apply(X, 2L, get("is.constant", ns))))
}

# The test nsdiffs runs on x, again, with its numbers (the strength, or OCSB's statistic and
# critical value): the key of the step. max_D is 0 or 1 (the catalogue's order_seasonal_d).
arima_auto_seasonal_steps <- function(x, test, max_D) {
  ns <- asNamespace("forecast")
  test <- test %||% "seas"
  max_D <- as.integer(max_D %||% 1L)
  if (get("is.constant", ns)(x) || stats::frequency(x) >= length(x)) return(list(key = "D_short"))
  out <- tryCatch({
    if (test == "seas") {
      strength <- suppressWarnings(get("seas.heuristic", ns)(x))
      list(asks = strength > ARIMA_AUTO_SEASONAL_STRENGTH, numbers = c(order_test_seasonal_strength = strength))
    } else {
      o <- suppressWarnings(get("ocsb.test", ns)(x, maxlag = 3, lag.method = "AIC"))
      list(asks = o$statistics > o$critical,
           numbers = c(order_test_ocsb_statistic = unname(o$statistics), order_test_ocsb_critical = unname(o$critical)))
    }
  }, error = function(e) NULL)
  if (is.null(out) || !isTRUE(out$asks %in% c(TRUE, FALSE))) return(list(key = "D_error"))
  prefix <- if (test == "seas") "D_seas_" else "D_ocsb_"
  out$key <- paste0(prefix, if (!out$asks) "no" else if (max_D < 1L) "capped" else "yes")
  out
}

# The unit-root test ndiffs runs at each difference of x, again: per difference its statistic
# and p-value (urca's table interpolated, as ndiffs reads it) and the step's key.
arima_auto_unit_root_steps <- function(x, test, max_d) {
  ns <- asNamespace("forecast")
  test <- test %||% "kpss"
  max_d <- as.integer(max_d %||% 2L)
  x <- c(stats::na.omit(c(x)))
  keys <- character()
  numbers <- numeric()
  k <- 0L
  if (get("is.constant", ns)(x)) return(list(keys = "d_constant_0", numbers = numbers))
  repeat {
    r <- tryCatch(arima_auto_unit_root(x, test), error = function(e) NULL)
    if (is.null(r) || is.na(r$asks)) {
      keys <- c(keys, paste0("d_error_", k))
      break
    }
    numbers[paste0("order_test_", test, "_d", k, "_statistic")] <- r$statistic
    numbers[paste0("order_test_", test, "_d", k, "_p_value")] <- r$p
    if (!r$asks) {
      keys <- c(keys, paste0("d_keep_", k))
      break
    }
    if (k >= max_d) {
      keys <- c(keys, paste0("d_max_", k))
      break
    }
    keys <- c(keys, paste0("d_diff_", k))
    k <- k + 1L
    x <- diff(x)
    if (get("is.constant", ns)(x)) {
      keys <- c(keys, paste0("d_constant_", k))
      break
    }
  }
  list(keys = keys, numbers = numbers)
}

# One test of ndiffs (type «level», alpha 0.05): its statistic, its p-value, whether it asks
# for one more difference.
arima_auto_unit_root <- function(x, test) {
  pval <- function(u) {
    stats::approx(u@cval[1L, ], as.numeric(sub("pct", "", colnames(u@cval), fixed = TRUE)) / 100, xout = u@teststat[1L],
                  rule = 2)$y
  }
  u <- suppressWarnings(switch(test,
    kpss = urca::ur.kpss(x, type = "mu", use.lag = trunc(3 * sqrt(length(x)) / 13)),
    adf = urca::ur.df(x, type = "drift"),
    pp = urca::ur.pp(x, type = "Z-tau", model = "constant")))
  p <- pval(u)
  list(statistic = unname(u@teststat[1L]), p = p, asks = if (test == "kpss") p < ARIMA_AUTO_ALPHA else p > ARIMA_AUTO_ALPHA)
}

# ── the rows of the choice ──────────────────────────────────────────────────
# «ARIMA(1,1,0)(0,1,1)[12]», « with drift» or « with mean» after it when the model has one
# (as auto.arima's own trace names them, the seasonal part only when it has an order).
arima_auto_name <- function(order, seasonal, m, constant) {
  out <- sprintf("ARIMA(%d,%d,%d)", order[[1L]], order[[2L]], order[[3L]])
  if (m > 1L && sum(seasonal) > 0L) out <- paste0(out, sprintf("(%d,%d,%d)[%d]", seasonal[[1L]], seasonal[[2L]], seasonal[[3L]], m))
  dd <- order[[2L]] + seasonal[[2L]]
  if (constant && dd == 1L) out <- paste0(out, " with drift") else if (constant && dd == 0L) out <- paste0(out, " with mean")
  out
}

arima_auto_rows <- function(rows, o, search, orders, constant) {
  m <- max(1L, o$s)
  # the candidates: the search's own fits (a refit without the approximation is not one), each once
  phase <- Filter(function(cl) identical(cl$approximation, search$approximation), search$calls)
  seen <- character()
  cand <- list()
  for (cl in phase) {
    name <- arima_auto_name(cl$order, cl$seasonal, m, cl$constant)
    if (name %in% seen) next
    seen <- c(seen, name)
    cand[[length(cand) + 1L]] <- c(cl, list(name = name))
  }
  chosen <- arima_auto_name(c(orders$p, orders$d, orders$q), c(orders$P, orders$D, orders$Q), m, constant)
  if (!chosen %in% seen) stop("auto.arima's choice ", chosen, " is not one of the candidates recorded")
  # the steps, in their order
  keys <- character()
  numbers <- numeric()
  st <- search$steps
  if (search$exog && (!is.null(st$seasonal) || !is.null(st$nonseasonal))) keys <- "exog_residuals"
  if (m > 1L) {
    if (is.null(st$seasonal)) {
      keys <- c(keys, "D_short")
    } else {
      keys <- c(keys, st$seasonal$key)
      numbers <- c(numbers, st$seasonal$numbers)
      if (orders$D < st$seasonal$D) keys <- c(keys, "D_dropped")
    }
  }
  if (!is.null(st$nonseasonal)) {
    keys <- c(keys, st$nonseasonal$keys)
    numbers <- c(numbers, st$nonseasonal$numbers)
    replayed <- arima_auto_d_of(st$nonseasonal$keys)
    if (!identical(replayed, as.integer(st$nonseasonal$d))) {
      stop("the unit-root steps give d = ", replayed, ", ndiffs ", st$nonseasonal$d)
    }
    if (orders$d < st$nonseasonal$d) keys <- c(keys, paste0("d_dropped_", orders$d))
  }
  keys <- c(keys, paste0("search_", if (isTRUE(o$stepwise)) "stepwise" else "all", if (search$approximation) "_approx" else ""))
  stray <- setdiff(keys, ARIMA_AUTO_KEYS)
  if (length(stray)) stop("a step without words: ", stray[[1L]])
  # model: the criterion, the steps, the numbers of the tests, the count of the candidates
  tab_text(rows, "model", "criterion", o$criterion)
  for (k in seq_along(keys)) tab_text(rows, "model", paste0("orders_reason_", k), keys[[k]])
  tab_number(rows, "model", "candidates", length(cand))
  for (name in names(numbers)) tab_number(rows, "model", name, numbers[[name]])
  # candidate: in the order auto.arima fitted them
  for (cl in cand) {
    fit <- cl$fit
    kept <- is.finite(fit$ic %||% Inf)
    for (stat in c("aicc", "aic", "bic")) tab_number(rows, "candidate", stat, if (kept) fit[[stat]] else NA_real_, level = cl$name)
    converged <- !is.null(fit$aic) && identical(as.integer(fit$code %||% 1L), 0L)
    tab_number(rows, "candidate", "converged", as.numeric(converged), level = cl$name)
    tab_number(rows, "candidate", "chosen", as.numeric(identical(cl$name, chosen)), level = cl$name)
  }
  invisible(rows)
}

# The d the unit-root steps reach: the difference of the last test (one more after d_diff_k).
arima_auto_d_of <- function(keys) {
  last <- keys[[length(keys)]]
  as.integer(sub("^d_[a-z]+_", "", last))
}
