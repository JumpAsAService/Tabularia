# The reference cases of the volatility (GARCH) node (docs/specs/garch-risk.md, «Self-test, reference values, witnesses»; lot G1),
# computed OUTSIDE the runner: rugarch::ugarchfit called directly on the returns divided by their standard deviation, the
# parameters brought back by hand, the checks by their definitions (FinTS::ArchTest for the ARCH-LM). In the group's image, from the
# repository's root:
#
#   docker run --rm --network none -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" -w /ref \
#     tabularia-models-timeseries:dev Rscript make_reference_garch.R
#
# It writes data/garch_returns.csv and data/garch_prices.csv (fixed seeds) and MERGES its cases (ids «garch_*») into expected.json
# by id, textually, before the first case: every other case stays byte for byte, whoever wrote it.
#
#   garch_returns.csv  800 instants (t = 1…800), three series of daily returns in fractions, 4 significant digits:
#                      ar_t    an AR(1) mean (φ = 0.15) and a GARCH(1,1) with Student t errors (ν = 6)
#                      gjr     a GJR-GARCH(1,1) with skewed t errors (γ = 0.1, ξ = 0.85, ν = 7)
#                      brk     a GARCH(1,1) normal whose mean falls from +0.002 to −0.002 at the 401st instant: Nyblom's test
#                              sees it and names μ (a break of ω is absorbed by the persistence, which the fit takes to its bound)
#   garch_prices.csv   301 prices on trading days of 2025 (Monday–Friday without 1 Jan, Good Friday, Easter Monday, 1 May,
#                      25–26 Dec), from a GARCH(1,1) t: `index` with dates, log and simple returns
#
# The second witness is outside R: make_witness_garch.py (Python `arch` and transcriptions of the likelihoods).

suppressMessages({
  library(rugarch)
  library(jsonlite)
})

here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")

# ── the tables ──────────────────────────────────────────────────────────────────────────────────────────────────
path_of <- function(vm, dist, fixed, n, seed, arma = c(0, 0)) {
  sp <- ugarchspec(variance.model = list(model = vm, garchOrder = c(1, 1)), mean.model = list(armaOrder = arma),
                   distribution.model = dist, fixed.pars = fixed)
  as.numeric(fitted(ugarchpath(sp, n.sim = n, rseed = seed)))
}
n <- 800
ar_t <- path_of("sGARCH", "std", list(mu = 4e-4, ar1 = 0.15, omega = 3e-6, alpha1 = 0.07, beta1 = 0.9, shape = 6), n, 101, c(1, 0))
gjr <- path_of("gjrGARCH", "sstd", list(mu = 2e-4, omega = 4e-6, alpha1 = 0.03, gamma1 = 0.1, beta1 = 0.88, skew = 0.85, shape = 7), n, 202)
set.seed(303)
e <- stats::rnorm(n)
brk <- numeric(n)
s2 <- 2e-6 / 0.04
for (t in seq_len(n)) {
  if (t > 1) s2 <- 2e-6 + 0.06 * brk[t - 1]^2 + 0.9 * s2
  brk[t] <- sqrt(s2) * e[t]
}
brk <- brk + ifelse(seq_len(n) <= n / 2, 0.002, -0.002)
sig <- function(x) format(signif(x, 4), scientific = FALSE, trim = TRUE, drop0trailing = TRUE)
utils::write.csv(data.frame(t = seq_len(n), ar_t = sig(ar_t), gjr = sig(gjr), brk = sig(brk)), file.path(data_dir, "garch_returns.csv"),
                 row.names = FALSE, quote = FALSE)
days <- seq(as.Date("2025-01-02"), as.Date("2026-06-30"), by = "day")
holidays <- as.Date(c("2025-01-01", "2025-04-18", "2025-04-21", "2025-05-01", "2025-12-25", "2025-12-26", "2026-01-01"))
days <- days[!format(days, "%u") %in% c("6", "7") & !days %in% holidays][seq_len(301)]
rp <- path_of("sGARCH", "std", list(mu = 3e-4, omega = 5e-6, alpha1 = 0.08, beta1 = 0.88, shape = 5), 300, 404)
price <- round(100 * exp(cumsum(c(0, rp))), 4)
utils::write.csv(data.frame(day = format(days), price = format(price, nsmall = 4, trim = TRUE)), file.path(data_dir, "garch_prices.csv"),
                 row.names = FALSE, quote = FALSE)


# ── the numbers, by hand ────────────────────────────────────────────────────────────────────────────────────────
# P(W_k > x) by Gil-Pelaez's inversion of the characteristic function (√(2it) / sin √(2it))^{k/2}, integrated numerically: another
# road than the runner's Talbot inversion of the Laplace transform (ts_nyblom.R)
bridge_p <- function(x, k) {
  f <- function(u) {
    t <- u^2
    w <- sqrt(t) * (1 + 1i)
    v <- Im(exp(-1i * t * x + (k / 2) * (log(w) - (-1i * w + log((exp(2i * w) - 1) / (2i)))))) / t * 2 * u
    v[u == 0] <- 0
    v
  }
  edges <- c(seq(0, 20, by = 0.25), seq(21, 120, by = 1))
  0.5 + sum(vapply(seq_len(length(edges) - 1L), function(i) integrate(f, edges[i], edges[i + 1L], rel.tol = 1e-10)$value, 0)) / pi
}
nlog <- function(n, q) if (n == 0) 0 else n * log(q)
weighted_lb <- function(x, m, K) {
  n <- length(x)
  rho <- acf(x, lag.max = m, plot = FALSE)$acf[-1L]
  qw <- n * (n + 2) * sum((m - seq_len(m) + 1) / m * rho^2 / (n - seq_len(m)))
  d <- 2 * m^2 + 3 * m + 1 - 6 * m * K
  c(qw, pgamma(qw, shape = 0.75 * (m + 1)^2 * m / d, scale = (2 / 3) * d / (m * (m + 1)), lower.tail = FALSE))
}
sign_bias <- function(z) {
  n <- length(z)
  lag <- z[-n]
  neg <- as.numeric(lag < 0)
  f <- lm(I(z[-1]^2) ~ neg + I(neg * lag) + I((1 - neg) * lag))
  b <- coef(f)[2:4]
  w <- as.numeric(b %*% solve(vcov(f)[2:4, 2:4], b))
  c(w, pchisq(w, 3, lower.tail = FALSE))
}
backtest <- function(r, var, level) {
  hit <- as.integer(r < var)
  T <- length(hit); x <- sum(hit); p <- 1 - level
  n <- table(factor(head(hit, -1), 0:1), factor(tail(hit, -1), 0:1))
  uc <- -2 * (nlog(T - x, 1 - p) + nlog(x, p)) + 2 * (nlog(T - x, 1 - x / T) + nlog(x, x / T))
  out <- c(violations = x, kupiec = uc)
  if (x > 0) {
    p01 <- n[1, 2] / sum(n[1, ]); p11 <- n[2, 2] / sum(n[2, ]); pi <- sum(n[, 2]) / (T - 1)
    mk <- nlog(n[1, 1], 1 - p01) + nlog(n[1, 2], p01) + nlog(n[2, 1], 1 - p11) + nlog(n[2, 2], p11)
    out <- c(out, ind = -2 * (nlog(sum(n[, 1]), 1 - pi) + nlog(sum(n[, 2]), pi)) + 2 * mk,
             cc = -2 * (nlog(sum(n[, 1]), 1 - p) + nlog(sum(n[, 2]), p)) + 2 * mk)
  }
  out
}

expect <- list()
add <- function(key, value, rel = NULL, abs = NULL, text = NULL) {
  e <- if (!is.null(text)) list(key = key, text = text) else list(key = key, value = value)
  if (!is.null(rel)) e$rel <- rel
  if (!is.null(abs)) e$abs <- abs
  expect[[length(expect) + 1L]] <<- e
}
cases <- list()

returns <- utils::read.csv(file.path(data_dir, "garch_returns.csv"))
prices <- utils::read.csv(file.path(data_dir, "garch_prices.csv"))
R_TYPES <- list(t = "integer", ar_t = "number", gjr = "number", brk = "number")
P_TYPES <- list(day = "date", price = "number")
RUGARCH <- c(arch = "sGARCH", garch = "sGARCH", gjr = "gjrGARCH", egarch = "eGARCH")

garch_case <- function(id, data, types, params, name) {
  expect <<- list()
  o <- modifyList(list(input_kind = "returns", returns_type = "log", variance_model = "garch", arch_order = 1, garch_order = 1,
                       ar_order = 0, ma_order = 0, mean = TRUE, distribution = "std", se_type = "robust", horizon = 10, holdout = 0,
                       var_level = 0.99, alpha = 0.05), params)
  x <- if (data == "garch_prices.csv") prices[[o$value]] else returns[[o$value]]
  r <- if (o$input_kind == "returns") x else if (o$returns_type == "log") diff(log(x)) else x[-1] / x[-length(x)] - 1
  n <- length(r); k <- o$holdout; tr <- n - k
  s <- sd(r[seq_len(tr)])
  p <- if (o$variance_model == "arch") 0 else o$garch_order
  spec <- ugarchspec(variance.model = list(model = RUGARCH[[o$variance_model]], garchOrder = c(o$arch_order, p)),
                     mean.model = list(armaOrder = c(o$ar_order, o$ma_order), include.mean = o$mean), distribution.model = o$distribution)
  set.seed(1, kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rejection")
  fit <- ugarchfit(spec, r / s, out.sample = k, solver = "hybrid")
  est <- coef(fit)
  nm <- names(est)
  # the parameters on the series' units, their covariance by the same linear map
  J <- diag(ifelse(nm == "mu", s, ifelse(nm == "omega" & o$variance_model != "egarch", s^2, 1)), length(nm))
  theta <- est * diag(J)
  if (o$variance_model == "egarch") {
    J[which(nm == "omega"), startsWith(nm, "beta")] <- -log(s^2)
    theta[["omega"]] <- est[["omega"]] + (1 - sum(est[startsWith(nm, "beta")])) * log(s^2)
  }
  # the sandwich from rugarch's Hessian and its per-observation scores (rugarch's robust.cvar takes other scores: see garch.R)
  Hi <- solve(fit@fit$hessian)
  V <- J %*% (if (o$se_type == "robust") Hi %*% crossprod(fit@fit$scores) %*% Hi else fit@fit$cvar) %*% t(J)
  ll <- likelihood(fit) - tr * log(s)
  # κ = E[z² 1(z < 0)] by quadrature of the fitted density (rugarch's persistence() weighs γ by P(z < 0): the same for a symmetric
  # law, not for the skewed t)
  kap <- if (o$distribution == "sstd") integrate(function(z) z^2 * ddist("sstd", z, 0, 1, skew = est[["skew"]], shape = est[["shape"]]),
                                                  -Inf, 0, rel.tol = 1e-12)$value else 0.5
  P <- if (o$variance_model == "egarch") sum(est[startsWith(nm, "beta")]) else
    sum(est[grepl("^(alpha|beta)", nm)]) + kap * sum(est[startsWith(nm, "gamma")])
  add("model|||model_name", text = name)
  add("model|||n", n)
  add("model|||scale", s, rel = 1e-12)
  add("model|||log_lik", ll, rel = 1e-8)
  add("model|||aic", -2 * ll + 2 * length(est), rel = 1e-8)
  for (i in seq_along(nm)) {
    add(paste0("coef|", nm[[i]], "||estimate"), theta[[i]], rel = 1e-6, abs = 1e-12)
    add(paste0("coef|", nm[[i]], "||std_error"), sqrt(V[i, i]), rel = 1e-4)
  }
  add("model|||persistence", P, rel = 1e-6)
  if (P < 1) add("model|||half_life", log(0.5) / log(P), rel = 1e-5)
  unc <- if (o$variance_model == "egarch") sqrt(exp(theta[["omega"]] / (1 - P))) else sqrt(theta[["omega"]] / (1 - P))
  add("model|||uncond_sigma", unc, rel = 1e-5)
  if (o$variance_model == "gjr") add("model|||kappa", kap, rel = 1e-8)
  # the filter of the whole span with the parameters fixed (its recursion started as the fit's), the forecast after the last instant
  fixed <- spec
  setfixed(fixed) <- as.list(est)
  flt <- ugarchfilter(fixed, r / s, n.old = tr)
  sig <- as.numeric(sigma(flt)) * s
  add("model|||sigma_last", sig[[n]], rel = 1e-6)
  fc <- ugarchforecast(fit, n.ahead = o$horizon, n.roll = k)
  add("model|||sigma_next", sigma(fc)[1, k + 1] * s, rel = 1e-6)
  # the checks of the training part's z
  z <- fit@fit$z
  kz <- o$ar_order + o$ma_order; kz2 <- o$arch_order + p; K <- max(kz, kz2)
  m <- max(min(10, tr %/% 5) + K, 3 * K - 1)
  w1 <- weighted_lb(z, m, kz); w2 <- weighted_lb(z^2, m, kz2)
  add("model|||lb_lag_used", m)
  add("assumption|ljung_box_z||statistic", w1[[1]], rel = 1e-6); add("assumption|ljung_box_z||p_value", w1[[2]], abs = 1e-6)
  add("assumption|ljung_box_z2||statistic", w2[[1]], rel = 1e-6); add("assumption|ljung_box_z2||p_value", w2[[2]], abs = 1e-6)
  a <- FinTS::ArchTest(z, lags = 12)
  add("assumption|arch_lm||statistic", unname(a$statistic), rel = 1e-6); add("assumption|arch_lm||p_value", a$p.value, abs = 1e-6)
  ny <- nyblom(fit)
  pj <- bridge_p(ny$JointStat, length(nm))
  add("assumption|nyblom||statistic", ny$JointStat, rel = 1e-5); add("assumption|nyblom||p_value", pj, abs = 1e-5)
  add("assumption|nyblom||verdict", text = if (pj < o$alpha) "failed" else "ok")
  pi <- vapply(as.numeric(ny$IndividualStat), bridge_p, 0, k = 1)
  if (o$variance_model != "egarch") {
    add(paste0("assumption|nyblom|", nm[[1]], "|p_adjusted"), p.adjust(pi, "holm")[[1]], abs = 1e-5)
    add(paste0("assumption|nyblom|", nm[[1]], "|verdict"), text = if (p.adjust(pi, "holm")[[1]] < o$alpha) "failed" else "ok")
  }
  sb <- sign_bias(z)
  add("assumption|sign_bias||statistic", sb[[1]], rel = 1e-6); add("assumption|sign_bias||p_value", sb[[2]], abs = 1e-6)
  if (k > 0) {
    mu <- as.numeric(fitted(flt)) * s
    q <- qdist(o$distribution, 1 - o$var_level, 0, 1, skew = if ("skew" %in% nm) est[["skew"]] else 0,
               shape = if ("shape" %in% nm) est[["shape"]] else 0)
    held <- (tr + 1):n
    bt <- backtest(r[held], (mu + sig * q)[held], o$var_level)
    add("backtest|counts||violations", bt[["violations"]])
    add("backtest|kupiec||statistic", bt[["kupiec"]], rel = 1e-8, abs = 1e-10)
    if ("ind" %in% names(bt)) {
      add("backtest|christoffersen_ind||statistic", bt[["ind"]], rel = 1e-8, abs = 1e-10)
      add("backtest|christoffersen_cc||statistic", bt[["cc"]], rel = 1e-8, abs = 1e-10)
    }
  }
  cases[[length(cases) + 1L]] <<- list(id = id, model = "garch", data = file.path("data", data), types = types,
                                       params = c(list(time = if (data == "garch_prices.csv") "day" else "t"), params), expect = expect)
}

garch_case("garch_ar_t_holdout", "garch_returns.csv", R_TYPES, list(value = "ar_t", frequency = "index", ar_order = 1, holdout = 200),
           "AR(1)-GARCH(1,1), t")
garch_case("garch_gjr_sstd_classical", "garch_returns.csv", R_TYPES,
           list(value = "gjr", frequency = "index", variance_model = "gjr", distribution = "sstd", se_type = "classical", horizon = 5),
           "GJR-GARCH(1,1), skewed t")
garch_case("garch_egarch_norm", "garch_returns.csv", R_TYPES, list(value = "gjr", frequency = "index", variance_model = "egarch",
                                                                   distribution = "norm"), "EGARCH(1,1), normal")
garch_case("garch_arch2_ma1_no_mean", "garch_returns.csv", R_TYPES, list(value = "ar_t", frequency = "index", variance_model = "arch",
                                                                         arch_order = 2, ma_order = 1, mean = FALSE, distribution = "norm"),
           "MA(1)-ARCH(2), normal")
garch_case("garch_break_norm", "garch_returns.csv", R_TYPES, list(value = "brk", frequency = "index", distribution = "norm"),
           "GARCH(1,1), normal")
garch_case("garch_prices_log_holdout", "garch_prices.csv", P_TYPES,
           list(value = "price", frequency = "index", input_kind = "prices", holdout = 50), "GARCH(1,1), t")
garch_case("garch_prices_simple", "garch_prices.csv", P_TYPES,
           list(value = "price", frequency = "index", input_kind = "prices", returns_type = "simple", distribution = "norm"),
           "GARCH(1,1), normal")
refusal <- function(id, data, types, params, code, message = NULL) {
  cases[[length(cases) + 1L]] <<- list(id = id, model = "garch", data = file.path("data", data), types = types, params = params,
                                       expect = list(), refuses = c(list(code = code), if (!is.null(message)) list(message = message)))
}
refusal("garch_refuse_too_short", "garch_prices.csv", P_TYPES,
        list(time = "day", value = "price", frequency = "index", input_kind = "prices", holdout = 250), "too_short",
        "the series has 300 returns, 50 after the holdout of 250: a GARCH needs at least 100 to fit; a longer series or a shorter holdout")
refusal("garch_refuse_not_positive", "dec_no_season.csv", list(month = "date", level = "number"),
        list(time = "month", value = "level", frequency = "month", input_kind = "prices"), "not_positive",
        "the column «level» holds 0 or a negative price: returns need positive prices; choose input_kind = returns if it holds returns")
refusal("garch_refuse_gaps", "dec_gaps.csv", list(month = "date", boxes = "number"),
        list(time = "month", value = "boxes", frequency = "month", input_kind = "prices"), "gaps")
refusal("garch_refuse_duplicate_time", "duplicate_monthly.csv", list(month = "date", boxes = "number"),
        list(time = "month", value = "boxes", frequency = "month"), "duplicate_time")

# ── the file: compact items, merged by id before the first case ─────────────────────────────────────────────────
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
# the cases already merged (the first ones whose id begins with garch_) are replaced; nothing after them is touched
rest <- txt[(start + 1L):length(txt)]
first_other <- which(grepl('^      "id": ', rest) & !grepl('^      "id": "garch_', rest))[[1L]] - 1L
out <- c(txt[seq_len(start)], unlist(lapply(cases, case_text)), rest[first_other:length(rest)])
writeLines(out, path, useBytes = TRUE)
cat(length(cases), "cases merged into expected.json\n")
