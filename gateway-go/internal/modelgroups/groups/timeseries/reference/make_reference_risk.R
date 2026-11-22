# The reference cases of the risk simulation node (docs/specs/garch-risk.md, «Self-test, reference values, witnesses»; lot G3),
# computed OUTSIDE the runner: the model fitted by rugarch::ugarchfit or forecast::Arima called directly, the paths drawn from the
# same L'Ecuyer-CMRG streams (one a seed block of 1,000 paths) but made by rugarch::ugarchpath and forecast:::simulate.Arima
# (future = TRUE) given those innovations, every measure computed on the whole matrix of the paths by its definition. In the
# group's image, from the repository's root:
#
#   docker run --rm --network none -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" -w /ref \
#     tabularia-models-timeseries:dev Rscript make_reference_risk.R
#
# It reads the GARCH node's and ARIMA's data (data/garch_returns.csv, garch_prices.csv, ar_index.csv, sarima_monthly.csv,
# drift_quarterly.csv) and MERGES its cases (ids «risk_*») into expected.json, textually, right after the GARCH node's cases
# (ids «garch_*»): every other case stays byte for byte, whoever wrote it.

suppressMessages({
  library(rugarch)
  library(forecast)
  library(jsonlite)
})

here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")
BLOCK <- 1000L

expect <- list()
add <- function(key, value, rel = NULL, abs = NULL, text = NULL) {
  e <- if (!is.null(text)) list(key = key, text = text) else list(key = key, value = value)
  if (!is.null(rel)) e$rel <- rel
  if (!is.null(abs)) e$abs <- abs
  expect[[length(expect) + 1L]] <<- e
}
cases <- list()

# ── the streams: block b draws from the b-th L'Ecuyer-CMRG stream of the seed ───────────────────────────────────────
blocks <- function(N, seed, f) {
  set.seed(seed, kind = "L'Ecuyer-CMRG", normal.kind = "Inversion", sample.kind = "Rejection")
  s <- .Random.seed
  out <- list()
  for (b in seq_len(ceiling(N / BLOCK))) {
    if (b > 1L) s <- parallel::nextRNGStream(s)
    assign(".Random.seed", s, envir = globalenv())
    n <- as.integer(min(BLOCK, N - (b - 1) * BLOCK))
    out[[b]] <- f(n)
  }
  set.seed(1, kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rejection")
  do.call(cbind, out)
}

# ── the measures on the whole matrix H (h × N), by their definitions ─────────────────────────────────────────────────
measures <- function(H, o, thr = NULL, drawdown = FALSE) {
  h <- nrow(H)
  N <- ncol(H)
  sgn <- if (identical(o$tail, "upper")) -1 else 1
  x <- sort(sgn * H[h, ])
  p <- round(1 - o$var_level, 12)
  pe <- round(1 - o$es_level, 12)
  alpha <- o$alpha
  r <- ceiling(round(N * p, 6))
  l <- qbinom(alpha / 2, N, p)
  u <- qbinom(1 - alpha / 2, N, p) + 1
  ke <- round(N * pe, 6)
  fl <- floor(ke)
  es <- (sum(x[seq_len(fl)]) + (ke - fl) * (if (ke > fl) x[[fl + 1L]] else 0)) / ke
  kv <- ceiling(ke)
  se <- sqrt((var(x[seq_len(kv)]) + (1 - pe) * (es - x[[kv]])^2) / ke)
  z <- qnorm(1 - alpha / 2)
  # the order statistics' interval [X(l), X(u)] of X = sgn·H, brought back to H (the upper tail's bounds swap)
  lo <- if (l >= 1) x[[l]] else NA
  hi <- if (u <= N) x[[u]] else NA
  out <- list(var = sgn * x[[r]], var_low = if (sgn > 0) lo else -hi, var_high = if (sgn > 0) hi else -lo, es = sgn * es,
              es_low = sgn * es - z * se, es_high = sgn * es + z * se)
  y <- H[h, ]
  d <- y - mean(y)
  out$mean <- mean(y)
  out$sd <- sd(y)
  out$skewness <- sqrt(N) * sum(d^3) / sum(d^2)^1.5
  out$kurtosis <- N * sum(d^4) / sum(d^2)^2 - 3
  if (!is.null(thr)) {
    beyond <- if (sgn > 0) H < thr else H > thr
    out$count_beyond <- sum(beyond[h, ])
    out$count_crossed <- sum(colSums(beyond) > 0)
    cp <- binom.test(out$count_crossed, N, conf.level = 1 - alpha)$conf.int
    out$crossed_low <- cp[[1L]]
    out$crossed_high <- cp[[2L]]
  }
  if (drawdown) {
    D <- apply(1 + H, 2L, function(w) max(0, 1 - w / cummax(pmax(w, 1))))
    ds <- sort(D)
    out$drawdown_median <- ds[[ceiling(N / 2)]]
    out$drawdown_var <- ds[[ceiling(round(N * o$var_level, 6))]]
  }
  out
}

add_measures <- function(m, thr, drawdown) {
  add("risk|var||value", m$var, rel = 1e-8, abs = 1e-12)
  if (!is.na(m$var_low)) add("risk|var||low", m$var_low, rel = 1e-8, abs = 1e-12)
  if (!is.na(m$var_high)) add("risk|var||high", m$var_high, rel = 1e-8, abs = 1e-12)
  add("risk|es||value", m$es, rel = 1e-8, abs = 1e-12)
  add("risk|es||low", m$es_low, rel = 1e-7, abs = 1e-12)
  add("risk|es||high", m$es_high, rel = 1e-7, abs = 1e-12)
  add("risk|moments||mean", m$mean, rel = 1e-8, abs = 1e-12)
  add("risk|moments||sd", m$sd, rel = 1e-8)
  add("risk|moments||skewness", m$skewness, rel = 1e-6, abs = 1e-9)
  add("risk|moments||kurtosis", m$kurtosis, rel = 1e-6, abs = 1e-9)
  if (!is.null(thr)) {
    add("risk|prob_beyond||count", m$count_beyond, abs = 0.5)
    add("risk|prob_crossed||count", m$count_crossed, abs = 0.5)
    add("risk|prob_crossed||low", m$crossed_low, rel = 1e-9)
    add("risk|prob_crossed||high", m$crossed_high, rel = 1e-9)
  }
  if (drawdown) {
    add("risk|drawdown||median", m$drawdown_median, rel = 1e-8, abs = 1e-12)
    add("risk|drawdown||value", m$drawdown_var, rel = 1e-8, abs = 1e-12)
  }
}

nlog <- function(n, q) if (n == 0) 0 else n * log(q)
backtest <- function(r, var, level) {
  hit <- as.integer(r < var)
  T <- length(hit); x <- sum(hit); p <- 1 - level
  out <- c(violations = x, kupiec = -2 * (nlog(T - x, 1 - p) + nlog(x, p)) + 2 * (nlog(T - x, 1 - x / T) + nlog(x, x / T)))
  if (x > 0) {
    n <- table(factor(hit[-T], 0:1), factor(hit[-1L], 0:1))
    p01 <- n[1, 2] / sum(n[1, ]); p11 <- n[2, 2] / sum(n[2, ]); pi <- sum(n[, 2]) / (T - 1)
    mk <- nlog(n[1, 1], 1 - p01) + nlog(n[1, 2], p01) + nlog(n[2, 1], 1 - p11) + nlog(n[2, 2], p11)
    out <- c(out, ind = -2 * (nlog(sum(n[, 1]), 1 - pi) + nlog(sum(n[, 2]), pi)) + 2 * mk)
  }
  out
}
add_backtest <- function(bt) {
  add("backtest|counts||violations", bt[["violations"]])
  add("backtest|kupiec||statistic", bt[["kupiec"]], rel = 1e-7, abs = 1e-9)
  if ("ind" %in% names(bt)) add("backtest|christoffersen_ind||statistic", bt[["ind"]], rel = 1e-7, abs = 1e-9)
}

DEFAULTS <- list(paths = 10000, horizon = 10, innovations = "parametric", var_level = 0.99, es_level = 0.975, tail = "lower",
                 alpha = 0.05, seed = 1L, holdout = 0, drawdown = TRUE, aggregate = "last", threshold = NULL, position = NULL)
read <- function(file, types) {
  df <- utils::read.csv(file.path(data_dir, file), stringsAsFactors = FALSE)
  for (k in names(types)) if (types[[k]] == "date") df[[k]] <- as.Date(df[[k]])
  df
}

# ── GARCH ───────────────────────────────────────────────────────────────────────────────────────────────────────────
RUGARCH <- c(arch = "sGARCH", garch = "sGARCH", gjr = "gjrGARCH", egarch = "eGARCH")
garch_case <- function(id, data, types, params) {
  expect <<- list()
  o <- modifyList(c(DEFAULTS, list(input_kind = "returns", returns_type = "log", variance_model = "garch", arch_order = 1,
                                   garch_order = 1, ar_order = 0, ma_order = 0, mean = TRUE, distribution = "std")), params)
  df <- read(data, types)
  y <- df[[o$value]]
  r <- if (o$input_kind == "returns") y else if (o$returns_type == "log") diff(log(y)) else y[-1] / y[-length(y)] - 1
  n <- length(r); k <- o$holdout; tr <- n - k
  s <- sd(r[seq_len(tr)])
  p <- if (o$variance_model == "arch") 0 else o$garch_order
  vm <- list(model = RUGARCH[[o$variance_model]], garchOrder = c(o$arch_order, p))
  mm <- list(armaOrder = c(o$ar_order, o$ma_order), include.mean = o$mean)
  spec <- ugarchspec(variance.model = vm, mean.model = mm, distribution.model = o$distribution)
  set.seed(1, kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rejection")
  fit <- ugarchfit(spec, r / s, out.sample = k, solver = "hybrid")
  est <- coef(fit)
  nm <- names(est)
  theta <- est * ifelse(nm == "mu", s, ifelse(nm == "omega" & o$variance_model != "egarch", s^2, 1))
  if (o$variance_model == "egarch") theta[["omega"]] <- est[["omega"]] + (1 - sum(est[startsWith(nm, "beta")])) * log(s^2)
  for (i in seq_along(nm)) add(paste0("coef|", nm[[i]], "||estimate"), theta[[i]], rel = 1e-6, abs = 1e-12)
  # the filter of the whole span with the parameters fixed, on the series' units: the end state of the paths
  fixed <- ugarchspec(variance.model = vm, mean.model = mm, distribution.model = o$distribution, fixed.pars = as.list(theta))
  flt <- ugarchfilter(fixed, r, n.old = tr)
  sig <- as.numeric(sigma(flt))
  mu <- as.numeric(fitted(flt))
  eps <- r - mu
  M <- max(o$arch_order, p, o$ar_order, o$ma_order, 1)
  shape <- if ("shape" %in% nm) est[["shape"]] else 0
  skew <- if ("skew" %in% nm) est[["skew"]] else 0
  zt <- (eps / sig)[seq_len(tr)]
  h <- o$horizon
  R <- blocks(o$paths, o$seed, function(nb) {
    Z <- if (o$innovations == "bootstrap") matrix(zt[sample.int(length(zt), nb * h, replace = TRUE)], nrow = h) else
      if (o$distribution == "norm") matrix(rnorm(nb * h), nrow = h) else
        matrix(rdist(o$distribution, nb * h, mu = 0, sigma = 1, skew = skew, shape = shape), nrow = h)
    pth <- ugarchpath(fixed, n.sim = h, m.sim = nb, presigma = tail(sig, M), prereturns = tail(r, M), preresiduals = tail(eps, M),
                      custom.dist = list(name = "sample", distfit = Z))
    matrix(as.numeric(fitted(pth)), nrow = h)
  })
  H <- if (o$returns_type == "simple") apply(1 + R, 2L, cumprod) - 1 else expm1(apply(R, 2L, cumsum))
  if (h == 1L) H <- matrix(H, nrow = 1L)
  last <- y[[length(y)]]
  thr <- if (is.null(o$threshold)) NULL else if (o$input_kind == "prices") o$threshold / last - 1 else o$threshold
  m <- measures(H, o, thr, isTRUE(o$drawdown))
  add("model|||model_name", text = params$name)
  add("model|||risk_variable", text = "cumulative_return")
  add("model|||last_value", last)
  add("model|||simulated_values", o$paths * h)
  add_measures(m, thr, isTRUE(o$drawdown))
  if (k > 0) {
    q <- qdist(o$distribution, if (o$tail == "upper") o$var_level else 1 - o$var_level, 0, 1, skew = skew, shape = shape)
    held <- (tr + 1):n
    var <- (mu + sig * q)[held]
    add_backtest(if (o$tail == "upper") backtest(-r[held], -var, o$var_level) else backtest(r[held], var, o$var_level))
  }
  params$name <- NULL
  cases[[length(cases) + 1L]] <<- list(id = id, model = "risk_simulation", data = file.path("data", data), types = types,
                                       params = c(list(model = "garch"), params), expect = expect)
}

# ── ARIMA ───────────────────────────────────────────────────────────────────────────────────────────────────────────
SEASONS <- c(hour = 24L, day = 7L, week = 52L, month = 12L, quarter = 4L, year = 1L, index = 1L)
arima_case <- function(id, data, types, params) {
  expect <<- list()
  o <- modifyList(c(DEFAULTS, list(p = 0, d = 0, q = 0, P = 0, D = 0, Q = 0, mean = TRUE, drift = FALSE, transform = "none")), params)
  df <- read(data, types)
  y <- df[[o$value]]
  n <- length(y); k <- o$holdout; tr <- n - k
  s <- o[["s"]] %||% SEASONS[[o$frequency]]
  lambda <- switch(o$transform, log = 0, box_cox = BoxCox.lambda(ts(y[seq_len(tr)], frequency = s)), NULL)
  dd <- o$d + o$D
  drift <- isTRUE(o$drift) && dd <= 1
  args <- list(order = c(o$p, o$d, o$q), seasonal = list(order = c(o$P, o$D, o$Q), period = s), include.mean = isTRUE(o$mean) && dd == 0,
               method = "CSS-ML")
  if (!is.null(lambda)) args$lambda <- lambda
  call <- function(i, fixed = NULL) {
    a <- c(list(y = ts(y[i], frequency = s)), args)
    if (drift) a$xreg <- cbind(xdrift = as.numeric(i))
    if (!is.null(fixed)) a$fixed <- fixed
    do.call(Arima, a)
  }
  fit <- call(seq_len(tr))
  refit <- call(seq_len(n), fit$coef)
  refit$sigma2 <- fit$sigma2
  for (nm in names(fit$coef)) add(paste0("coef|", if (nm == "xdrift") "drift" else nm, "||estimate"), fit$coef[[nm]], rel = 1e-7, abs = 1e-12)
  h <- o$horizon
  res <- as.numeric(residuals(fit))
  res <- res[is.finite(res)]
  sd <- sqrt(fit$sigma2)
  xreg <- if (drift) cbind(xdrift = as.numeric(n + seq_len(h))) else NULL
  Y <- blocks(o$paths, o$seed, function(nb) {
    E <- if (o$innovations == "bootstrap") matrix(res[sample.int(length(res), nb * h, replace = TRUE)], nrow = h) else
      matrix(rnorm(nb * h, 0, sd), nrow = h)
    vapply(seq_len(nb), function(j) as.numeric(forecast:::simulate.Arima(refit, nsim = h, future = TRUE, innov = E[, j], xreg = xreg)),
           numeric(h))
  })
  if (h == 1L) Y <- matrix(Y, nrow = 1L)
  last <- y[[n]]
  H <- switch(o$aggregate, last = Y, sum = apply(Y, 2L, cumsum), relative_change = Y / last - 1)
  if (h == 1L) H <- matrix(H, nrow = 1L)
  thr <- if (is.null(o$threshold)) NULL else if (o$aggregate == "relative_change") o$threshold / last - 1 else o$threshold
  rel <- o$aggregate == "relative_change"
  m <- measures(H, o, thr, rel && isTRUE(o$drawdown))
  add("model|||risk_variable", text = c(last = "value", sum = "running_total", relative_change = "relative_change")[[o$aggregate]])
  add("model|||last_value", last)
  add_measures(m, thr, rel && isTRUE(o$drawdown))
  if (k > 0) {
    x <- if (is.null(lambda)) y else as.numeric(BoxCox(y, lambda))
    held <- (tr + 1):n
    q <- qnorm(if (o$tail == "upper") o$var_level else 1 - o$var_level)
    vx <- x[held] - as.numeric(residuals(refit))[held] + sd * q
    var <- if (is.null(lambda)) vx else InvBoxCox(vx, lambda)
    add_backtest(if (o$tail == "upper") backtest(-y[held], -var, o$var_level) else backtest(y[held], var, o$var_level))
  }
  cases[[length(cases) + 1L]] <<- list(id = id, model = "risk_simulation", data = file.path("data", data), types = types,
                                       params = c(list(model = "arima"), params), expect = expect)
}
`%||%` <- function(a, b) if (is.null(a)) b else a

R_TYPES <- list(t = "integer", ar_t = "number", gjr = "number", brk = "number")
P_TYPES <- list(day = "date", price = "number")

garch_case("risk_garch_prices_threshold", "garch_prices.csv", P_TYPES,
           list(time = "day", value = "price", frequency = "index", input_kind = "prices", holdout = 50, threshold = 100, position = 100000,
                name = "GARCH(1,1), t"))
garch_case("risk_garch_gjr_sstd_holdout", "garch_returns.csv", R_TYPES,
           list(time = "t", value = "gjr", frequency = "index", variance_model = "gjr", distribution = "sstd", holdout = 200, paths = 5000,
                name = "GJR-GARCH(1,1), skewed t"))
garch_case("risk_garch_egarch_bootstrap_upper", "garch_returns.csv", R_TYPES,
           list(time = "t", value = "ar_t", frequency = "index", variance_model = "egarch", distribution = "norm", ar_order = 1, ma_order = 1,
                innovations = "bootstrap", tail = "upper", threshold = 0.04, horizon = 5, paths = 5000, seed = 7L,
                name = "ARMA(1,1)-EGARCH(1,1), normal"))
arima_case("risk_arima_sarima_sum_upper", "sarima_monthly.csv", list(month = "date", boxes = "number"),
           list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 1, P = 0, D = 1, Q = 1, transform = "log",
                aggregate = "sum", tail = "upper", horizon = 12, holdout = 12, threshold = 20000, paths = 3000))
arima_case("risk_arima_ar_relative_bootstrap", "ar_index.csv", list(t = "integer", level = "number"),
           list(time = "t", value = "level", frequency = "index", p = 2, aggregate = "relative_change", position = 1000,
                innovations = "bootstrap", var_level = 0.995, es_level = 0.99, threshold = 40, holdout = 20, paths = 3000))
arima_case("risk_arima_drift_box_cox_last", "drift_quarterly.csv", list(q = "integer", revenue = "number"),
           list(time = "q", value = "revenue", frequency = "quarter", p = 0, d = 1, q = 1, drift = TRUE, transform = "box_cox", horizon = 8,
                paths = 2000))
refusal <- function(id, data, types, params, code, message = NULL) {
  cases[[length(cases) + 1L]] <<- list(id = id, model = "risk_simulation", data = file.path("data", data), types = types, params = params,
                                       expect = list(), refuses = c(list(code = code), if (!is.null(message)) list(message = message)))
}
refusal("risk_refuse_position_not_relative", "ar_index.csv", list(t = "integer", level = "number"),
        list(model = "arima", time = "t", value = "level", frequency = "index", p = 1, position = 1000), "position_not_relative",
        paste0("a position is an amount invested: it needs a relative change (aggregate = relative_change); with aggregate = last the ",
               "measures are in the units of «level»: leave the position empty"))
refusal("risk_refuse_too_few_paths", "garch_prices.csv", P_TYPES,
        list(model = "garch", time = "day", value = "price", frequency = "index", input_kind = "prices", paths = 100, var_level = 0.999),
        "too_few_paths", "with 100 paths and a 99.9 % level no simulated value lies beyond the VaR: at least 1,000 paths")

# ── the file: compact items, merged by id right after the GARCH node's cases ────────────────────────────────────────
j <- function(x) as.character(toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = NA))
case_text <- function(cs) {
  c("    {", sprintf('      "id": %s,', j(cs$id)), sprintf('      "model": %s,', j(cs$model)), sprintf('      "data": %s,', j(cs$data)),
    sprintf('      "types": %s,', j(cs$types)), sprintf('      "params": %s,', j(cs$params)),
    if (length(cs$expect)) c('      "expect": [', paste0("  ", vapply(cs$expect, j, ""), c(rep(",", length(cs$expect) - 1L), "")), "      ],") else
      '      "expect": [],',
    sprintf('      "refuses": %s', if (is.null(cs$refuses)) "null" else j(cs$refuses)), "    },")
}
path <- file.path(here, "expected.json")
txt <- readLines(path, encoding = "UTF-8")
start <- grep('^  "cases": \\[$', txt)
ids <- grep('^      "id": ', txt)
is_id <- function(i, prefix) grepl(paste0('^      "id": "', prefix), txt[i])
# the case lines begin one line above their id; the risk cases go after the last GARCH case, replacing the risk cases already there
first_risk <- ids[is_id(ids, "risk_")]
after_garch <- ids[!is_id(ids, "garch_") & !is_id(ids, "risk_")][[1L]] - 1L  # the «{» of the first case of another model
from <- if (length(first_risk)) min(first_risk) - 1L else after_garch
out <- c(txt[seq_len(from - 1L)], unlist(lapply(cases, case_text)), txt[after_garch:length(txt)])
writeLines(out, path, useBytes = TRUE)
cat(length(cases), "cases merged into expected.json\n")
