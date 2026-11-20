# The reference values of the node «Parametric survival» (docs/specs/statistical-models.md,
# «The node «Parametric survival»» → «Reference values», lot P1), sourced by make_reference.R at
# its end, in the group's image: flexsurv, survreg and coxph called directly on the tables with
# their own column names and R's formula interface, never the runner. It adds its cases to
# `cases` and writes its tables with `write_table` when they are missing or with --data.
#
# The tolerances, and why:
#   closed forms (the exponential's rate d / T, its log-likelihood, the standard error 1/√d of
#     its log rate, the hazard ratios of groups)                      1e-10 (no group), 1e-9;
#   an estimate, against any other maximum of the same likelihood     a millionth of its standard
#     error (abs), 1e-9 relative: the runner's maximum and this file's are each found to about
#     1e-8 standard errors (the precision of a Richardson gradient: optim's BFGS alone stops
#     2.5e-9 relative from the exponential's closed form and 1e-4 from a Gompertz maximum), so a
#     relative bound means nothing for a coefficient near 0; a time or hazard ratio exp(β) and
#     the ends of its interval likewise, 1e-6 · se relative;
#   flexsurvreg with the same call, brought to the maximum by Newton steps here (ps_polish):
#     log-likelihood, AIC, BIC                                        1e-9;
#     the profile's quantiles and survival                            1e-8 (they follow the
#     estimates: 1.4e-9 measured);
#   standard errors: the inverse of a Richardson Hessian (numDeriv) of flexsurv's own
#     log-likelihood, written here (flexsurvreg with fixedpars at each point), its covariates
#     centred                                                         1e-6 (two numerical
#     Hessians of two codes of the likelihood: 1.4e-7 measured on the generalized gamma, whose
#     incomplete gamma function is the least precise; 1e-9 to 1e-8 on the others);
#   survreg on the four distributions it knows, with rel.tolerance 1e-13 (its own default, 1e-9,
#     leaves estimates 5e-7 apart): the estimates as above; the log-likelihood 1e-9; the
#     profile's median 1e-8;
#   survreg's standard errors (analytic), the shape's interval, the Weibull's hazard ratio by the
#     delta method on −β/σ, the median's interval from predict(type = "uquantile", se.fit = TRUE)
#                                                                     1e-6 (a numerical Hessian
#     against an analytic one: 7.6e-9 measured);
#   coxph (the Cox node's hazard ratios beside)                       1e-9;
#   a second construction of Gompertz: its log-likelihood written here, maximized by nlm and
#     Newton steps                                                    1e-6, and a millionth of
#     a standard error.
#
# The tables (fixed seeds): ps_exponential (90 rows, three shifts with rates of their own, a
# boolean event), ps_weibull (240 rows drawn from a Weibull with a number, a category of three
# levels and three plants with shapes of their own; case weights 1 to 3; rows with a NULL; two
# rows with a time of 0), ps_gompertz (200 rows drawn from a Gompertz with rising hazard, a
# number and a boolean), ps_gompertz_cure (300 rows, falling hazard: a share never has the
# event), ps_all_events (80 rows, no censoring), ps_small (8 rows for the refusals).

suppressPackageStartupMessages({
  library(flexsurv)
  library(numDeriv)
})

make_ps_data <- function() {
  set.seed(20261127)
  n <- 90
  shift <- rep(c("day", "evening", "night"), each = 30)
  rate <- c(day = 1 / 400, evening = 1 / 250, night = 1 / 150)[shift]
  t <- stats::rexp(n, rate)
  censor_time <- stats::runif(n, 100, 900)
  exp_ <- data.frame(hours = pmax(round(pmin(t, censor_time), 1), 0.1), broke = ifelse(t <= censor_time, "true", "false"),
                     shift = shift, stringsAsFactors = FALSE)
  write_table(exp_[sample.int(n), ], "ps_exponential.csv")

  set.seed(20261128)
  n <- 240
  load <- round(stats::rnorm(n, 50, 10), 1)
  line <- sample(c("A", "B", "C"), n, TRUE)
  plant <- sample(c("P", "Q", "R"), n, TRUE)
  shape <- c(P = 1.6, Q = 1.3, R = 2)[plant]
  eta <- 3 + 0.02 * (load - 50) + c(A = 0, B = 0.4, C = -0.3)[line] + c(P = 0, Q = 0.2, R = -0.1)[plant]
  t <- exp(eta) * stats::rweibull(n, shape = shape)
  censor_time <- stats::runif(n, 5, 60)
  weibull <- data.frame(days = pmax(round(pmin(t, censor_time), 2), 0.01), status = ifelse(t <= censor_time, "failed", "running"),
                        load = load, line = unname(line), plant = unname(plant), w = sample(1:3, n, TRUE), stringsAsFactors = FALSE)
  weibull$days[c(3, 77)] <- 0
  weibull$load[[15]] <- NA
  weibull$line[[120]] <- NA
  write_table(weibull, "ps_weibull.csv")

  set.seed(20261129)
  n <- 200
  age <- round(stats::runif(n, 30, 70))
  smoker <- stats::runif(n) < 0.35
  rate <- 0.01 * exp(0.03 * (age - 50) + 0.5 * smoker)
  t <- flexsurv::rgompertz(n, shape = 0.08, rate = rate)
  censor_time <- stats::runif(n, 5, 40)
  write_table(data.frame(years = round(pmin(t, censor_time), 2), died = as.integer(t <= censor_time), age = age,
                         smoker = ifelse(smoker, "true", "false"), stringsAsFactors = FALSE), "ps_gompertz.csv")

  set.seed(20261130)
  n <- 300
  t <- flexsurv::rgompertz(n, shape = -0.08, rate = 0.035)
  censor_time <- stats::runif(n, 10, 80)
  write_table(data.frame(months = round(pmax(pmin(t, censor_time), 0.01), 2), left = as.integer(t <= censor_time)),
              "ps_gompertz_cure.csv")

  set.seed(20261131)
  n <- 80
  temp <- round(stats::rnorm(n, 20, 4), 1)
  t <- 500 * exp(-0.05 * (temp - 20)) * stats::rweibull(n, shape = 2.2)
  write_table(data.frame(cycles = round(t, 1), failed = 1L, temp = temp), "ps_all_events.csv")

  write_table(data.frame(t = c(3, 5, 7, 9, 11, 13, 15, 17), t_neg = c(-1, 5, 7, 9, 11, 13, 15, 17), e = c(1, 1, 0, 1, 1, 0, 0, 0),
                         plant = c("P", "P", "P", "P", "P", "Z", "Z", "Z"), kind = c("a", "b", "c", "d", "e", "f", "a", "b"),
                         same = "x", x = c(1.5, 2.5, 3, 4.5, 5, 6.5, 7, 8.5), x2 = c(3, 5, 6, 9, 10, 13, 14, 17), stringsAsFactors = FALSE),
              "ps_small.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "ps_weibull.csv"))) make_ps_data()

PS_CONTROL <- list(reltol = 1e-15, maxit = 2000)
PS_SR_CONTROL <- survreg.control(rel.tolerance = 1e-13, maxiter = 200)

# An estimate to a millionth of its standard error; exp(estimate) (a ratio, a positive parameter,
# the end of an interval) to the same, as a relative bound; a z statistic and its p-value.
est_num <- function(key, value, se) list(key = key, value = unname(value), rel = 1e-9, abs = 1e-6 * unname(se))
exp_num <- function(key, value, se) list(key = key, value = unname(value), rel = max(1e-9, 1e-6 * unname(se)))
z_num <- function(key, value) list(key = key, value = unname(value), rel = 1e-7, abs = 1e-6)
p_num <- function(key, value, z) list(key = key, value = unname(value), rel = 1e-5 * max(1, abs(z)) * max(1, abs(z)))

# flexsurv's fit brought to the maximum of its own log-likelihood: Newton steps with the
# Richardson Hessian and gradient (numDeriv) of that log-likelihood, `refit(nat)` giving it at
# natural-scale values (flexsurvreg with fixedpars = TRUE), until a step is below 1e-8 standard
# errors (optim's BFGS stops on the function's change: 2.5e-9 relative from the exponential's
# closed form). The fit's estimates (opt, res.t, res, which summary reads) and its log-likelihood
# are replaced by the polished ones; attr(fit, "V") is the covariance there.
# The Hessian is taken with each parameter's covariates centred (par = A · par_c, the
# intercepts at the columns' means) and brought back, V = A V_c A': uncentred, a covariate far
# from 0 makes it ill-conditioned (7e-7 relative against survreg's analytic errors on
# ps_all_events).
ps_polish <- function(fit, refit) {
  nb <- length(fit$dlist$pars)
  f <- function(par) {
    nat <- par
    for (j in seq_len(nb)) nat[[j]] <- fit$dlist$inv.transforms[[j]](par[[j]])
    refit(nat)
  }
  A <- diag(length(fit$opt$par))
  for (j in seq_len(nb)) {
    covs <- fit$mx[[fit$dlist$pars[[j]]]]
    if (length(covs)) A[j, nb + covs] <- -colMeans(fit$data$mml[[fit$dlist$pars[[j]]]])[-1L]
  }
  vcov_at <- function(par) {
    H <- numDeriv::hessian(function(b) f(drop(A %*% b)), drop(solve(A, par)))
    A %*% solve(-H) %*% t(A)
  }
  par <- unname(fit$opt$par)
  for (i in 1:8) {
    V <- vcov_at(par)
    step <- drop(V %*% numDeriv::grad(f, par))
    par <- par + step
    if (max(abs(step) / sqrt(diag(V))) < 1e-8) break
  }
  V <- vcov_at(par)
  names(par) <- names(fit$opt$par)
  dimnames(V) <- list(names(par), names(par))
  fit$opt$par <- par
  fit$coefficients <- par
  fit$loglik <- f(par)
  fit$res.t[, "est"] <- par
  nat <- par
  for (j in seq_len(nb)) nat[[j]] <- fit$dlist$inv.transforms[[j]](par[[j]])
  fit$res[, "est"] <- nat
  attr(fit, "V") <- V
  fit
}

# The values of a flexsurv fit as the runner writes them. `terms`: a data.frame of the location's
# coefficients other than the intercept (term, level, the coefficient's name in the fit) — the
# strata's location effects are not written; `anc`: the shape's per-stratum coefficients by
# stratum name (the reference stratum first, NA); `V` the covariance from ps_polish; `sign` −1 for
# the exponential (its rate's coefficients turned to read as time ratios), `hr` the hazard ratio's
# kind (NA, "rate", "weibull"); `profile`: newdata of one row per stratum, with `strata_names`.
ps_expect <- function(fit, V, terms, sign = 1, hr = NA, alpha = 0.05, n, quantiles = c(0.25, 0.75), times = numeric(),
                      profile, strata_names = "all", anc = NULL, rel_pred = 1e-8, rel_se = 1e-6) {
  z <- qnorm(1 - alpha / 2)
  par <- fit$opt$par
  se <- sqrt(diag(V))
  names(se) <- names(par)
  k <- length(par)
  e <- list(num("model|||log_lik", fit$loglik), num("model|||aic", -2 * fit$loglik + 2 * k), num("model|||bic", -2 * fit$loglik + k * log(n)),
            num("model|||n_parameters", k), num("model|||n", n))
  loc <- fit$dlist$location
  coef_rows <- function(level, level_2, name, intercept) {
    key <- function(stat) paste0("coef|", level, "|", if (is.na(level_2)) "" else level_2, "|", stat)
    est <- sign * par[[name]]
    s <- se[[name]]
    p <- 2 * pnorm(-abs(est / s))
    out <- list(est_num(key("estimate"), est, s), num(key("std_error"), s, rel_se), z_num(key("statistic"), est / s),
                p_num(key("p_value"), p, est / s), txt(key("significance"), sig(p)))
    if (intercept) return(out)
    if (fit$dlist$name != "gompertz") {
      out <- c(out, list(exp_num(key("time_ratio"), exp(est), s), exp_num(key("tr_ci_low"), exp(est - z * s), s),
                         exp_num(key("tr_ci_high"), exp(est + z * s), s)))
    }
    if (identical(hr, "rate")) {
      out <- c(out, list(exp_num(key("hazard_ratio"), exp(par[[name]]), s), exp_num(key("hr_ci_low"), exp(par[[name]] - z * s), s),
                         exp_num(key("hr_ci_high"), exp(par[[name]] + z * s), s)))
    } else if (identical(hr, "weibull")) {
      shape <- exp(par[["shape"]])
      g <- c(-par[[name]] * shape, -shape)
      ix <- c("shape", name)
      s_hr <- sqrt(drop(t(g) %*% V[match(ix, names(par)), match(ix, names(par))] %*% g))
      log_hr <- -par[[name]] * shape
      out <- c(out, list(exp_num(key("hazard_ratio"), exp(log_hr), s_hr), exp_num(key("hr_ci_low"), exp(log_hr - z * s_hr), s_hr),
                         exp_num(key("hr_ci_high"), exp(log_hr + z * s_hr), s_hr)))
    }
    out
  }
  e <- c(e, coef_rows("(Intercept)", NA, loc, TRUE))
  for (j in seq_len(nrow(terms))) e <- c(e, coef_rows(terms$term[[j]], terms$level[[j]], terms$name[[j]], FALSE))
  # the distribution's parameters other than the location, per stratum with `anc`
  for (p in setdiff(fit$dlist$pars, loc)) {
    tr <- fit$dlist$transforms[[match(p, fit$dlist$pars)]]
    is_log <- identical(tr, base::log)
    per <- if (!is.null(anc) && p == names(anc)[[1L]]) anc[[1L]] else stats::setNames(NA_character_, NA_character_)
    for (s in seq_along(per)) {
      idx <- c(p, if (!is.na(per[[s]])) per[[s]])
      v <- sum(par[idx])
      sv <- sqrt(sum(V[idx, idx]))
      level_2 <- if (length(per) > 1L) names(per)[[s]] else ""
      key <- function(stat) paste0("parameter|", p, "|", level_2, "|", stat)
      if (is_log) {
        e <- c(e, list(exp_num(key("estimate"), exp(v), sv), num(key("std_error"), exp(v) * sv, rel_se),
                       exp_num(key("ci_low"), exp(v - z * sv), sv), exp_num(key("ci_high"), exp(v + z * sv), sv)))
      } else {
        e <- c(e, list(est_num(key("estimate"), v, sv), num(key("std_error"), sv, rel_se),
                       est_num(key("ci_low"), v - z * sv, sv), est_num(key("ci_high"), v + z * sv, sv)))
      }
    }
  }
  # the profile: flexsurv's own quantiles and survival at the newdata rows
  sq <- summary(fit, newdata = profile, type = "quantile", quantiles = c(0.5, quantiles), ci = FALSE, tidy = TRUE, cross = TRUE)
  q <- matrix(sq$est, nrow = nrow(profile), byrow = TRUE)
  for (s in seq_along(strata_names)) {
    key <- function(stat) paste0("stratum|", strata_names[[s]], "||", stat)
    e <- c(e, list(num(key("median"), q[s, 1L], rel_pred)))
    for (j in seq_along(quantiles)) e <- c(e, list(num(key(paste0("quantile_", label(quantiles[[j]]))), q[s, j + 1L], rel_pred)))
  }
  if (length(times)) {
    ss <- summary(fit, newdata = profile, type = "survival", t = times, ci = FALSE, tidy = TRUE, cross = TRUE)
    sv <- matrix(ss$est, nrow = nrow(profile), byrow = TRUE)
    for (s in seq_along(strata_names)) for (j in seq_along(times)) {
      e <- c(e, list(num(paste0("stratum|", strata_names[[s]], "||surv_at_", label(times[[j]])), sv[s, j], rel_pred)))
    }
  }
  e
}

# survreg's numbers for the same model: the estimates (AFT: the runner's own scale), their
# standard errors, the log-likelihood, the shape per stratum, the profile's median and its
# interval (the delta method on the log median: predict's uquantile with se.fit); the Weibull's
# hazard ratios by the delta method on −β/σ with survreg's covariance.
ps_expect_survreg <- function(sr, terms, dist, alpha = 0.05, profile, strata_names = "all", shape_names = NULL, hr = FALSE) {
  z <- qnorm(1 - alpha / 2)
  b <- coef(sr)
  V <- vcov(sr)
  e <- list(num("model|||log_lik", sr$loglik[[2L]], 1e-9))
  key <- function(level, level_2, stat) paste0("coef|", level, "|", if (is.na(level_2)) "" else level_2, "|", stat)
  e <- c(e, list(est_num(key("(Intercept)", NA, "estimate"), b[["(Intercept)"]], sqrt(V["(Intercept)", "(Intercept)"])),
                 num(key("(Intercept)", NA, "std_error"), sqrt(V["(Intercept)", "(Intercept)"]), 1e-6)))
  for (j in seq_len(nrow(terms))) {
    nm <- terms$sr[[j]]
    e <- c(e, list(est_num(key(terms$term[[j]], terms$level[[j]], "estimate"), b[[nm]], sqrt(V[nm, nm])),
                   num(key(terms$term[[j]], terms$level[[j]], "std_error"), sqrt(V[nm, nm]), 1e-6)))
    if (hr) {
      sigma <- sr$scale
      log_hr <- -b[[nm]] / sigma
      g <- c(-1 / sigma, b[[nm]] / sigma) # on (β, log σ)
      s_hr <- sqrt(drop(t(g) %*% V[c(nm, "Log(scale)"), c(nm, "Log(scale)")] %*% g))
      e <- c(e, list(exp_num(key(terms$term[[j]], terms$level[[j]], "hazard_ratio"), exp(log_hr), s_hr),
                     num(key(terms$term[[j]], terms$level[[j]], "hr_ci_low"), exp(log_hr - z * s_hr), 1e-6),
                     num(key(terms$term[[j]], terms$level[[j]], "hr_ci_high"), exp(log_hr + z * s_hr), 1e-6)))
    }
  }
  if (dist != "exponential") {
    # σ per stratum: the shape is 1/σ (Weibull, log-logistic) or σ itself (log-normal's sdlog)
    scale <- sr$scale
    p <- if (dist == "lognormal") "sdlog" else "shape"
    for (s in seq_along(scale)) {
      level_2 <- if (length(scale) > 1L) shape_names[[s]] else ""
      v <- if (dist == "lognormal") scale[[s]] else 1 / scale[[s]]
      at <- length(b) + s # survreg's covariance: the coefficients, then the log scales in the strata's order
      log_se <- sqrt(V[at, at])
      e <- c(e, list(exp_num(paste0("parameter|", p, "|", level_2, "|estimate"), v, log_se)))
    }
  }
  uq <- predict(sr, newdata = profile, type = "uquantile", p = 0.5, se.fit = TRUE)
  for (s in seq_along(strata_names)) {
    k <- function(stat) paste0("stratum|", strata_names[[s]], "||", stat)
    e <- c(e, list(num(k("median"), exp(uq$fit[[s]]), 1e-8), num(k("median_ci_low"), exp(uq$fit[[s]] - z * uq$se.fit[[s]]), 1e-6),
                   num(k("median_ci_high"), exp(uq$fit[[s]] + z * uq$se.fit[[s]]), 1e-6)))
  }
  e
}

# The Cox node's hazard ratios beside, coxph called directly.
ps_expect_cox <- function(cx, terms, alpha = 0.05) {
  ci <- summary(cx, conf.int = 1 - alpha)$conf.int
  e <- list()
  for (j in seq_len(nrow(terms))) {
    key <- function(stat) paste0("coef|", terms$term[[j]], "|", if (is.na(terms$level[[j]])) "" else terms$level[[j]], "|", stat)
    nm <- terms$name[[j]]
    e <- c(e, list(num(key("cox_hazard_ratio"), ci[nm, 1L]), num(key("cox_hr_ci_low"), ci[nm, 3L]), num(key("cox_hr_ci_high"), ci[nm, 4L])))
  }
  e
}

# ── the exponential: closed forms ──
d <- read_table("ps_exponential.csv")
d$event <- as.integer(d$broke == "true")
total <- sum(d$hours)
events <- sum(d$event)
lambda <- events / total
cases[[length(cases) + 1L]] <- list(
  id = "ps_exponential_exact", model = "parametric_survival", data = "data/ps_exponential.csv",
  types = list(hours = "number", broke = "boolean", shift = "text"),
  params = list(time = "hours", event = "broke", event_value = TRUE, distribution = "exponential", quantiles = list(0.25)),
  expect = list(
    num("model|||log_lik", events * log(lambda) - lambda * total, 1e-10),
    num("coef|(Intercept)||estimate", -log(lambda), 1e-10), # the exponential's estimates read as log time: −log rate
    num("coef|(Intercept)||std_error", 1 / sqrt(events), 1e-10),
    num("stratum|all||median", log(2) / lambda, 1e-10),
    num("stratum|all||quantile_0.25", -log(0.75) / lambda, 1e-10),
    num("model|||n", nrow(d)), num("model|||events", events), num("model|||n_parameters", 1),
    txt("model|||formula", "Surv(hours, broke = true) ~ 1"), txt("model|||distribution", "exponential"),
    txt("model|||variant", "ph_aft"), txt("model|||coefficient_scale", "log_time_ratio")))

# three shifts: each its own rate d_g / T_g, the hazard ratios (d_b / T_b) / (d_a / T_a), the
# standard error of a log hazard ratio √(1/d_a + 1/d_b)
rate_g <- tapply(d$event, d$shift, sum) / tapply(d$hours, d$shift, sum)
events_g <- tapply(d$event, d$shift, sum)
# (the estimates and the ratios to a millionth of a standard error: the maximum is found to about
# 1e-8 standard errors, 8e-10 relative here)
e <- list(est_num("coef|(Intercept)||estimate", -log(rate_g[["day"]]), 1 / sqrt(events_g[["day"]])),
          num("coef|(Intercept)||std_error", 1 / sqrt(events_g[["day"]]), 1e-9))
for (g in c("evening", "night")) {
  hr <- rate_g[[g]] / rate_g[["day"]]
  se <- sqrt(1 / events_g[["day"]] + 1 / events_g[[g]])
  key <- function(stat) paste0("coef|shift|", g, "|", stat)
  e <- c(e, list(exp_num(key("hazard_ratio"), hr, se), est_num(key("estimate"), -log(hr), se), exp_num(key("time_ratio"), 1 / hr, se),
                 num(key("std_error"), se, 1e-9), exp_num(key("hr_ci_low"), hr * exp(-qnorm(0.975) * se), se),
                 exp_num(key("hr_ci_high"), hr * exp(qnorm(0.975) * se), se)))
}
e <- c(e, list(num("model|||log_lik", sum(events_g * log(rate_g)) - sum(rate_g * tapply(d$hours, d$shift, sum)), 1e-9),
               num("stratum|all||median", log(2) / rate_g[["day"]], 1e-9)))
d$shift_f <- factor(d$shift)
cx <- coxph(Surv(hours, event) ~ shift_f, data = d)
e <- c(e, ps_expect_cox(cx, data.frame(term = "shift", level = c("evening", "night"), name = c("shift_fevening", "shift_fnight"))))
cases[[length(cases) + 1L]] <- list(
  id = "ps_exponential_groups", model = "parametric_survival", data = "data/ps_exponential.csv",
  types = list(hours = "number", broke = "boolean", shift = "text"),
  params = list(time = "hours", event = "broke", event_value = TRUE, distribution = "exponential", predictors = list("shift")),
  expect = e)

# ── the 240 rows drawn from a Weibull ──
d <- read_table("ps_weibull.csv")
d$event <- as.integer(d$status == "failed")
used <- complete.cases(d[, c("days", "status", "load", "line", "w")]) & d$days > 0
x <- d[used, ]
x$line <- factor(x$line, levels = c("A", "B", "C"))
x$plant <- factor(x$plant, levels = c("P", "Q", "R"))
n_missing <- sum(!complete.cases(d[, c("days", "status", "load", "line", "w")]))
n_zero <- sum(complete.cases(d[, c("days", "status", "load", "line", "w")]) & d$days == 0)
types <- list(days = "number", status = "text", load = "number", line = "text", plant = "text", w = "integer")
profile <- data.frame(load = sum(x$w * x$load) / sum(x$w), line = factor("A", levels = c("A", "B", "C")))
terms <- data.frame(term = c("load", "line", "line", "load × line", "load × line"), level = c(NA, "B", "C", "B", "C"),
                    name = c("load", "lineB", "lineC", "load:lineB", "load:lineC"), sr = c("load", "lineB", "lineC", "load:lineB", "load:lineC"))
base_params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"),
                    interactions = list(list("load", "line")), weights = "w", quantiles = list(0.25, 0.75), times = list(10, 30))
counts <- list(num("model|||n_missing", n_missing), num("model|||n_zero_time", n_zero), num("model|||events", sum(x$event)),
               num("model|||censored", sum(1 - x$event)), num("model|||last_time", max(x$days)),
               num("model|||last_event_time", max(x$days[x$event == 1])), num("model|||n_strata", 1))
for (dist in c("weibull", "lognormal", "loglogistic", "gengamma")) {
  fd <- c(weibull = "weibull", lognormal = "lnorm", loglogistic = "llogis", gengamma = "gengamma")[[dist]]
  fit <- flexsurvreg(Surv(days, event) ~ load * line, data = x, weights = w, dist = fd, control = PS_CONTROL)
  refit <- function(nat) flexsurvreg(Surv(days, event) ~ load * line, data = x, weights = w, dist = fd, inits = nat, fixedpars = TRUE)$loglik
  fit <- ps_polish(fit, refit)
  V <- attr(fit, "V")
  null <- ps_polish(flexsurvreg(Surv(days, event) ~ 1, data = x, weights = w, dist = fd, control = PS_CONTROL),
                    function(nat) flexsurvreg(Surv(days, event) ~ 1, data = x, weights = w, dist = fd, inits = nat, fixedpars = TRUE)$loglik)
  lr <- 2 * (fit$loglik - null$loglik)
  e <- c(ps_expect(fit, V, terms, hr = if (dist == "weibull") "weibull" else NA, n = nrow(x), times = c(10, 30), profile = profile),
         counts,
         list(num("model|||lr_statistic", lr, 1e-8), num("model|||lr_df", 5), num("model|||lr_p_value", pchisq(lr, 5, lower.tail = FALSE), 1e-6),
              txt("model|||distribution", dist), txt("model|||selection", "manual"), txt("model|||coefficient_scale", "log_time_ratio"),
              txt("model|||formula", "Surv(days, status = failed) ~ load + line + load × line"),
              txt("model|||profile", paste0("load = ", format(signif(profile$load, 4), trim = TRUE), " (mean); line = A (reference)")),
              txt("model|||note_zero_times", paste0(n_zero, " rows have a time of 0 and are left out: a parametric distribution gives an ",
                                                    "event at 0 no density; if those events are real, count time from a moment before them."))))
  # Harrell's C of the location's linear predictor (a larger one: a longer time)
  lp <- model.matrix(~ load * line, x) %*% c(fit$opt$par[[fit$dlist$location]], fit$opt$par[terms$name])
  e <- c(e, list(num("model|||concordance", concordance(Surv(days, event) ~ lp, data = cbind(x, lp = lp[, 1]), weights = w)$concordance)))
  if (dist != "gengamma") {
    sd_ <- c(weibull = "weibull", lognormal = "lognormal", loglogistic = "loglogistic")[[dist]]
    sr <- survreg(Surv(days, event) ~ load * line, data = x, weights = w, dist = sd_, control = PS_SR_CONTROL)
    e <- c(e, ps_expect_survreg(sr, terms, dist, profile = profile, hr = dist == "weibull"))
  }
  if (dist == "weibull") {
    e <- c(e, ps_expect_cox(coxph(Surv(days, event) ~ load * line, data = x, weights = w, ties = "efron"), terms))
    ex <- ps_polish(flexsurvreg(Surv(days, event) ~ load * line, data = x, weights = w, dist = "exp", control = PS_CONTROL),
                    function(nat) flexsurvreg(Surv(days, event) ~ load * line, data = x, weights = w, dist = "exp", inits = nat,
                                              fixedpars = TRUE)$loglik)
    lr_e <- 2 * (fit$loglik - ex$loglik)
    e <- c(e, list(num("model|||exponential_lr_statistic", lr_e, 1e-8), num("model|||exponential_lr_df", 1),
                   txt("model|||variant", "ph_aft")))
    weibull_ll <- fit$loglik
  }
  if (dist == "lognormal") lognormal_ll <- fit$loglik
  if (dist == "gengamma") {
    # nested: the generalized gamma's log-likelihood is never below the Weibull's or the log-normal's
    if (fit$loglik < max(weibull_ll, lognormal_ll)) stop("gengamma below the models it nests")
    e <- c(e, list(txt("model|||variant", "aft")))
  }
  cases[[length(cases) + 1L]] <- list(id = paste0("ps_", dist), model = "parametric_survival", data = "data/ps_weibull.csv",
                                      types = types, params = c(base_params, list(distribution = dist)), expect = e)
}

# three plants with a shape each: survreg's `+ plant + strata(plant)`, flexsurv's anc
used <- complete.cases(d[, c("days", "status", "load", "line", "plant")]) & d$days > 0
x <- d[used, ]
x$line <- factor(x$line, levels = c("A", "B", "C"))
x$plant <- factor(x$plant, levels = c("P", "Q", "R"))
fit <- flexsurvreg(Surv(days, event) ~ load + line + plant, anc = list(shape = ~ plant), data = x, dist = "weibull", control = PS_CONTROL)
refit <- function(nat) flexsurvreg(Surv(days, event) ~ load + line + plant, anc = list(shape = ~ plant), data = x, dist = "weibull",
                                   inits = nat, fixedpars = TRUE)$loglik
fit <- ps_polish(fit, refit)
V <- attr(fit, "V")
terms2 <- data.frame(term = c("load", "line", "line"), level = c(NA, "B", "C"), name = c("load", "lineB", "lineC"),
                     sr = c("load", "lineB", "lineC"))
strata_names <- paste0("plant=", c("P", "Q", "R"))
profile <- data.frame(load = mean(x$load), line = factor("A", levels = c("A", "B", "C")), plant = factor(c("P", "Q", "R")))
e <- ps_expect(fit, V, terms2, n = nrow(x), times = 20, profile = profile, strata_names = strata_names,
               anc = list(shape = c("plant=P" = NA, "plant=Q" = "shape(plantQ)", "plant=R" = "shape(plantR)")))
sr <- survreg(Surv(days, event) ~ load + line + plant + strata(plant), data = x, dist = "weibull", control = PS_SR_CONTROL)
e <- c(e, ps_expect_survreg(sr, terms2, "weibull", profile = profile, strata_names = strata_names, shape_names = strata_names),
       list(num("model|||n_strata", 3), txt("model|||formula", "Surv(days, status = failed) ~ load + line + strata(plant)"),
            txt("model|||note_strata_no_hr", paste0("With strata, a Weibull model has a shape per stratum: the hazard ratio of a predictor ",
                                                    "differs in each stratum while its time ratio is common, so only the time ratios are given."))))
for (s in seq_along(strata_names)) {
  rows_s <- x$plant == c("P", "Q", "R")[[s]]
  km <- survfit(Surv(days, event) ~ 1, data = x[rows_s, ])
  e <- c(e, list(num(paste0("stratum|", strata_names[[s]], "||n"), sum(rows_s)), num(paste0("stratum|", strata_names[[s]], "||events"), sum(x$event[rows_s])),
                 num(paste0("stratum|", strata_names[[s]], "||km_median"), unname(summary(km)$table[["median"]]))))
}
cases[[length(cases) + 1L]] <- list(
  id = "ps_weibull_strata", model = "parametric_survival", data = "data/ps_weibull.csv", types = types,
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                times = list(20)),
  expect = e)

# ── Gompertz: rising hazard; flexsurv, and the log-likelihood written here maximized by nlm ──
d <- read_table("ps_gompertz.csv")
d$smoker_f <- factor(d$smoker, levels = c("false", "true"))
fit <- flexsurvreg(Surv(years, died) ~ age + smoker_f, data = d, dist = "gompertz", control = PS_CONTROL)
refit <- function(nat) flexsurvreg(Surv(years, died) ~ age + smoker_f, data = d, dist = "gompertz", inits = nat, fixedpars = TRUE)$loglik
fit <- ps_polish(fit, refit)
V <- attr(fit, "V")
terms3 <- data.frame(term = c("age", "smoker"), level = c(NA, "true"), name = c("age", "smoker_ftrue"))
profile <- data.frame(age = mean(d$age), smoker_f = factor("false", levels = c("false", "true")))
e <- ps_expect(fit, V, terms3, hr = "rate", n = nrow(d), times = c(10, 20), profile = profile)
# h(t) = rate · exp(shape · t), H(t) = rate / shape · (exp(shape · t) − 1), rate = exp(c + b · (age − 50) + …):
# the age centred for nlm (the intercept is c − 50 b)
hand <- function(b) {
  rate <- exp(b[[2L]] + b[[3L]] * (d$age - 50) + b[[4L]] * (d$smoker == "true"))
  shape <- b[[1L]]
  -sum(d$died * (log(rate) + shape * d$years) - rate / shape * (exp(shape * d$years) - 1))
}
nl <- nlm(hand, c(0.05, log(0.01), 0, 0), gradtol = 1e-12, steptol = 1e-15, iterlim = 1000)
b <- nl$estimate
for (i in 1:8) {
  step <- solve(numDeriv::hessian(hand, b), numDeriv::grad(hand, b))
  b <- b - step
  if (max(abs(step)) < 1e-12) break
}
se_hand <- sqrt(diag(solve(numDeriv::hessian(hand, b))))
b[[2L]] <- b[[2L]] - 50 * b[[3L]]
hand_num <- function(key, value, se) list(key = key, value = value, rel = 1e-6, abs = 1e-6 * se)
e <- c(e, list(num("model|||log_lik", -hand(c(b[[1L]], b[[2L]] + 50 * b[[3L]], b[[3L]], b[[4L]])), 1e-6),
               hand_num("coef|(Intercept)||estimate", b[[2L]], sqrt(V["rate", "rate"])),
               hand_num("coef|age||estimate", b[[3L]], se_hand[[3L]]), hand_num("coef|smoker|true|estimate", b[[4L]], se_hand[[4L]]),
               hand_num("parameter|shape||estimate", b[[1L]], se_hand[[1L]]),
               txt("model|||variant", "ph"), txt("model|||coefficient_scale", "log_hazard_ratio")),
       ps_expect_cox(coxph(Surv(years, died) ~ age + smoker_f, data = d), terms3))
ex <- ps_polish(flexsurvreg(Surv(years, died) ~ age + smoker_f, data = d, dist = "exp", control = PS_CONTROL),
                function(nat) flexsurvreg(Surv(years, died) ~ age + smoker_f, data = d, dist = "exp", inits = nat, fixedpars = TRUE)$loglik)
e <- c(e, list(num("model|||exponential_lr_statistic", 2 * (fit$loglik - ex$loglik), 1e-8)))
cases[[length(cases) + 1L]] <- list(
  id = "ps_gompertz", model = "parametric_survival", data = "data/ps_gompertz.csv",
  types = list(years = "number", died = "integer", age = "number", smoker = "boolean"),
  params = list(time = "years", event = "died", event_value = 1, predictors = list("age", "smoker"), distribution = "gompertz",
                times = list(10, 20)),
  expect = e)

# a falling hazard: S(∞) = exp(rate / shape), the median never reached (empty) and the note
d <- read_table("ps_gompertz_cure.csv")
fit <- flexsurvreg(Surv(months, left) ~ 1, data = d, dist = "gompertz", control = PS_CONTROL)
fit <- ps_polish(fit, function(nat) flexsurvreg(Surv(months, left) ~ 1, data = d, dist = "gompertz", inits = nat, fixedpars = TRUE)$loglik)
shape <- fit$res["shape", "est"]
rate <- fit$res["rate", "est"]
cured <- exp(rate / shape)
if (!(shape < 0 && cured > 0.5)) stop("the cure table does not leave half the rows without the event")
cases[[length(cases) + 1L]] <- list(
  id = "ps_gompertz_cure", model = "parametric_survival", data = "data/ps_gompertz_cure.csv",
  types = list(months = "number", left = "integer"),
  params = list(time = "months", event = "left", event_value = 1, distribution = "gompertz", quantiles = list(0.25, 0.75),
                times = list(12, 120)),
  expect = list(num("model|||log_lik", fit$loglik), num("parameter|shape||estimate", shape), num("coef|(Intercept)||estimate", log(rate)),
                num("stratum|all||median", NA), num("stratum|all||quantile_0.75", NA), num("stratum|all||median_ci_low", NA),
                num("stratum|all||quantile_0.25", qgompertz(0.25, shape, rate)),
                num("stratum|all||surv_at_120", pgompertz(120, shape, rate, lower.tail = FALSE)),
                txt("model|||note_quantile_infinite", paste0("@quantile_infinite share=", format(round(100 * cured, 1), nsmall = 1)))))

# no censoring
d <- read_table("ps_all_events.csv")
fit <- flexsurvreg(Surv(cycles, failed) ~ temp, data = d, dist = "weibull", control = PS_CONTROL)
refit <- function(nat) flexsurvreg(Surv(cycles, failed) ~ temp, data = d, dist = "weibull", inits = nat, fixedpars = TRUE)$loglik
fit <- ps_polish(fit, refit)
V <- attr(fit, "V")
terms4 <- data.frame(term = "temp", level = NA, name = "temp", sr = "temp")
profile <- data.frame(temp = mean(d$temp))
sr <- survreg(Surv(cycles, failed) ~ temp, data = d, dist = "weibull", control = PS_SR_CONTROL)
cases[[length(cases) + 1L]] <- list(
  id = "ps_all_events", model = "parametric_survival", data = "data/ps_all_events.csv",
  types = list(cycles = "number", failed = "integer", temp = "number"),
  params = list(time = "cycles", event = "failed", event_value = 1, predictors = list("temp")),
  expect = c(ps_expect(fit, V, terms4, hr = "weibull", n = nrow(d), profile = profile),
             ps_expect_survreg(sr, terms4, "weibull", profile = profile, hr = TRUE),
             list(num("model|||censored", 0))))

# ── refusals ──
small_types <- list(t = "number", t_neg = "number", e = "integer", plant = "text", kind = "text", same = "text", x = "number",
                    x2 = "number")
refusal <- function(id, params, code, message = NULL) {
  list(id = id, model = "parametric_survival", data = "data/ps_small.csv", types = small_types,
       params = utils::modifyList(list(event = "e", event_value = 1), params), expect = list(),
       refuses = list(code = code, message = message))
}
cases[[length(cases) + 1L]] <- refusal("ps_no_events", list(time = "t", event_value = 7), "no_events",
                                       "the column «e» is never «7» in the rows the model uses: with no event there is nothing to fit; check event_value")
cases[[length(cases) + 1L]] <- refusal("ps_negative_time", list(time = "t_neg"), "negative_time",
                                       "the column «t_neg» holds a negative time (-1.0): a time is counted from the start, 0 or more")
cases[[length(cases) + 1L]] <- refusal("ps_stratum_no_events", list(time = "t", strata = "plant"), "stratum_no_events",
                                       "the stratum «plant=Z» has no event: its shape cannot be estimated; merge it with another, or leave its rows out")
cases[[length(cases) + 1L]] <- refusal("ps_too_few_rows", list(time = "t", predictors = list("kind")), "too_few_rows",
                                       "the model has 7 parameters and 8 rows it can use: it needs at least 9 rows (the parameters + 2)")
cases[[length(cases) + 1L]] <- refusal("ps_one_level", list(time = "t", predictors = list("same")), "one_level")
cases[[length(cases) + 1L]] <- refusal("ps_aliased", list(time = "t", predictors = list("x", "x2"), distribution = "exponential"), "aliased")
source(file.path(here, "make_reference_auto.R"), local = TRUE)
