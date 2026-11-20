# The reference values of the delayed entry of the node «Parametric survival»
# (docs/specs/statistical-models.md, «The node «Parametric survival»» → «Delayed entry» and
# «Reference values», lot P8), sourced by make_reference.R after make_reference_checks.R, in the
# group's image: flexsurv with Surv(start, stop, event) (left truncation), survreg, coxph, cox.zph
# and survfit called directly on the tables with their own column names, never the runner. It adds
# its cases to `cases` and writes its tables with `write_table` when they are missing or with
# --data. It uses the helpers of make_reference_parametric.R (`num`, `txt`, `ps_polish`,
# `ps_expect`, `ps_expect_survreg`, PS_CONTROL, PS_SR_CONTROL).
#
# What each case checks, and at which tolerance:
#   ps_delayed_exponential_exact  60 rows, no predictors: with a start the rate is
#                  events / Σ (time − start), se(log rate) = 1/√events, ℓ = d log λ − λ Σ (t − s),
#                  the median log 2 / λ: closed forms                    1e-10 (the standard error 1e-8: the runner's is the inverse
#                  of a numerical Hessian, 6.9e-10 from 1/√events here, 2.4e-11 without a start);
#   ps_delayed_<distribution>  the six distributions on 150 rows (a number, a category of two
#                  levels, case weights, a row with a NULL; half the rows enter after 0):
#                  flexsurvreg(Surv(start, stop, event) ~ …), brought to its maximum by the Newton
#                  steps of ps_polish, with the tolerances of make_reference_parametric.R (1e-9 the
#                  log-likelihood, a millionth of a standard error the estimates, 1e-6 the standard
#                  errors, 1e-8 the profile);
#   a second construction  the Weibull's and Gompertz's truncated log-likelihood written here,
#                  Σ w (δ log h(t) − H(t) + H(s)), maximized by nlm and Newton steps: the
#                  estimates and the log-likelihood                         1e-6 (and a millionth of
#                  a standard error);
#   ps_delayed_zero  the same rows with a start of 0 for each: no truncation, survreg (which takes
#                  no start–stop rows) gives the estimates, standard errors, log-likelihood, shape and
#                  median                                                 1e-9 to 1e-6, as without a start;
#   ps_delayed_checks  the Weibull's checks with entry: dfbetas from the hand-written truncated
#                  log-likelihood (V · U_i, U_i by numDeriv's Richardson jacobian), the rows named
#                  exactly                                                 1e-5; ph_cox from cox.zph of
#                  coxph(Surv(start, stop, event) ~ …, weights), Efron, transform km, terms 1e-9
#                  (p-values 1e-8); the generalized gamma's LR statistic against flexsurv 1e-8;
#                  km_agreement from a second construction: survfit(Surv(start, stop, event)) read from
#                  the first time with 10 rows at risk (the group whose rows all enter late is read
#                  from t0 > 0), the mean fitted survival from flexsurv's summary(newdata) weighted
#                  as Σ w S(t) / Σ w S(t0)                                 1e-6;
#   ps_delayed_auto  the six candidates by flexsurv with the start: the choice, the marks, the log-likelihoods
#                  and the AIC as P3's (1e-9 the chosen one, 1e-7 the others, 1e-4 the generalized gamma);
#   refusals       a start not before the time, a negative start, a start that is text.
#
# The tables (fixed seeds): ps_delayed_exp (60 rows, an exponential with rate 1/40, entries 0 or
# 0–30, left truncated, censored), ps_delayed (150 rows from a Weibull AFT with a number and a
# category; the rows of line A enter at 0 for 60 % and before 6 otherwise, those of line B between 2
# and 10 but for a few that enter early and leave soon, so that line B's first times have fewer than
# 10 rows at risk; weights 1 to 3; a NULL; a column `zero` of zeros).

make_ps_delayed_data <- function() {
  set.seed(20261201)
  n <- 160
  entry <- ifelse(stats::runif(n) < 0.5, 0, round(stats::runif(n, 0, 30), 1))
  t <- stats::rexp(n, 1 / 40)
  keep <- t > entry # left truncation: only the units that outlast their entry are seen
  censor <- entry + stats::runif(n, 5, 80)
  exp_ <- data.frame(entry = entry, hours = pmax(round(pmin(t, censor), 1), entry + 0.1), broke = ifelse(t <= censor, "true", "false"),
                     stringsAsFactors = FALSE)
  exp_ <- exp_[keep, ][seq_len(60), ]
  write_table(exp_, "ps_delayed_exp.csv")

  set.seed(20261202)
  n <- 240
  line <- sample(c("A", "B"), n, TRUE)
  load <- round(stats::rnorm(n, 50, 10), 1)
  eta <- 3 + 0.02 * (load - 50) + ifelse(line == "B", 0.35, 0)
  t <- exp(eta) * stats::rweibull(n, shape = 1.6)
  # line A: 60 % enter at 0, the others before 6; line B: a few (6 %) enter before 1.5 and leave within a time (withdrawn),
  # the others enter between 2 and 10: its first times have fewer than 10 rows at risk
  early <- line == "B" & stats::runif(n) < 0.06
  entry <- ifelse(line == "A", ifelse(stats::runif(n) < 0.6, 0, round(stats::runif(n, 0, 6), 1)),
                  ifelse(early, round(stats::runif(n, 0, 1.5), 1), round(stats::runif(n, 2, 10), 1)))
  keep <- t > entry
  censor <- entry + ifelse(early, stats::runif(n, 0.2, 1), stats::runif(n, 4, 40))
  w <- ifelse(early, 1L, sample(1:3, n, TRUE))
  d <- data.frame(entry = entry, days = pmax(round(pmin(t, censor), 2), entry + 0.01), status = ifelse(t <= censor, "failed", "running"),
                  load = load, line = line, w = w, zero = 0L, stringsAsFactors = FALSE)
  d <- d[keep, ][seq_len(150), ]
  d$load[[7]] <- NA
  write_table(d, "ps_delayed.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "ps_delayed.csv"))) make_ps_delayed_data()

# ── the exponential: closed forms ──
d <- read_table("ps_delayed_exp.csv")
d$event <- as.integer(d$broke == "true")
total <- sum(d$hours - d$entry)
events <- sum(d$event)
lambda <- events / total
delayed <- sum(d$entry > 0)
cases[[length(cases) + 1L]] <- list(
  id = "ps_delayed_exponential_exact", model = "parametric_survival", data = "data/ps_delayed_exp.csv",
  types = list(entry = "number", hours = "number", broke = "boolean"),
  params = list(time = "hours", event = "broke", event_value = TRUE, start = "entry", distribution = "exponential", quantiles = list(0.25)),
  expect = list(
    num("model|||log_lik", events * log(lambda) - lambda * total, 1e-10),
    num("coef|(Intercept)||estimate", -log(lambda), 1e-10),
    num("coef|(Intercept)||std_error", 1 / sqrt(events), 1e-8), # a numerical Hessian (6.9e-10 measured), not a closed form of the optimiser
    num("stratum|all||median", log(2) / lambda, 1e-10),
    num("stratum|all||quantile_0.25", -log(0.75) / lambda, 1e-10),
    num("model|||n", nrow(d)), num("model|||events", events), num("model|||n_parameters", 1), num("model|||n_delayed", delayed),
    txt("model|||formula", "Surv(entry, hours, broke = true) ~ 1"), txt("model|||distribution", "exponential"),
    txt("model|||note_start_rows", paste0("@start_rows delayed=", delayed, " rows=", nrow(d)))))

# ── the 150 rows: the six distributions against flexsurv, Surv(start, stop, event) ──
d <- read_table("ps_delayed.csv")
d$event <- as.integer(d$status == "failed")
used <- complete.cases(d[, c("entry", "days", "status", "load", "line", "w")])
x <- d[used, ]
x$line <- factor(x$line, levels = c("A", "B"))
x$w <- as.numeric(x$w)
pd_types <- list(entry = "number", days = "number", status = "text", load = "number", line = "text", w = "integer", zero = "integer")
pd_params <- list(time = "days", event = "status", event_value = "failed", start = "entry", predictors = list("load", "line"), weights = "w",
                  quantiles = list(0.25, 0.75), times = list(10, 30))
pd_terms <- data.frame(term = c("load", "line"), level = c(NA, "B"), name = c("load", "lineB"), sr = c("load", "lineB"))
pd_profile <- data.frame(load = sum(x$w * x$load) / sum(x$w), line = factor("A", levels = c("A", "B")))
pd_counts <- list(num("model|||n_missing", sum(!used)), num("model|||events", sum(x$event)), num("model|||censored", sum(1 - x$event)),
                  num("model|||last_time", max(x$days)), num("model|||last_event_time", max(x$days[x$event == 1])),
                  num("model|||n_delayed", sum(x$entry > 0)), num("model|||n_strata", 1),
                  txt("model|||note_start_rows", paste0("@start_rows delayed=", sum(x$entry > 0), " rows=", nrow(x))),
                  txt("model|||formula", "Surv(entry, days, status = failed) ~ load + line"),
                  txt("model|||profile", paste0("load = ", format(signif(pd_profile$load, 4), trim = TRUE), " (mean); line = A (reference)")))
pd_form <- Surv(entry, days, event) ~ load + line
pd_fit <- function(dist, data = x, form = pd_form) {
  fit <- flexsurvreg(form, data = data, weights = w, dist = dist, control = PS_CONTROL)
  ps_polish(fit, function(nat) flexsurvreg(form, data = data, weights = w, dist = dist, inits = nat, fixedpars = TRUE)$loglik)
}
pd_flex <- c(weibull = "weibull", exponential = "exp", lognormal = "lnorm", loglogistic = "llogis", gengamma = "gengamma", gompertz = "gompertz")
pd_fits <- lapply(pd_flex, pd_fit)

# the truncated log-likelihood written by hand, for a second construction. `par`: the log shape
# (Weibull) or the shape (Gompertz), the intercept, then the load's and line B's coefficients.
pd_hand_weibull <- function(b, data = x, rows = FALSE) {
  shape <- exp(b[[1L]])
  scale <- exp(b[[2L]] + b[[3L]] * data$load + b[[4L]] * (data$line == "B"))
  h <- shape / scale * (data$days / scale)^(shape - 1)
  ll <- data$event * log(h) - (data$days / scale)^shape + (data$entry / scale)^shape
  if (rows) ll else sum(data$w * ll)
}
pd_hand_gompertz <- function(b, data = x, rows = FALSE) {
  shape <- b[[1L]]
  rate <- exp(b[[2L]] + b[[3L]] * data$load + b[[4L]] * (data$line == "B"))
  ll <- data$event * (log(rate) + shape * data$days) - rate / shape * (exp(shape * data$days) - 1) + rate / shape * (exp(shape * data$entry) - 1)
  if (rows) ll else sum(data$w * ll)
}
# the maximum of a hand-written log-likelihood: nlm from `start`, then Newton steps; its covariance
pd_hand_max <- function(f, start) {
  nl <- nlm(function(b) -f(b), start, gradtol = 1e-12, steptol = 1e-15, iterlim = 1000)
  b <- nl$estimate
  for (i in 1:8) {
    step <- solve(numDeriv::hessian(function(v) -f(v), b), numDeriv::grad(function(v) -f(v), b))
    b <- b - step
    if (max(abs(step)) < 1e-12) break
  }
  list(par = b, loglik = f(b), V = solve(-numDeriv::hessian(f, b)))
}
pd_hand_num <- function(key, value, se) list(key = key, value = unname(value), rel = 1e-6, abs = 1e-6 * unname(se))

for (dist in names(pd_flex)) {
  fit <- pd_fits[[dist]]
  V <- attr(fit, "V")
  null <- pd_fit(pd_flex[[dist]], form = Surv(entry, days, event) ~ 1)
  lr <- 2 * (fit$loglik - null$loglik)
  e <- c(ps_expect(fit, V, pd_terms, hr = if (dist == "weibull") "weibull" else if (dist %in% c("exponential", "gompertz")) "rate" else NA,
                   sign = if (dist == "exponential") -1 else 1, n = nrow(x), times = c(10, 30), profile = pd_profile),
         pd_counts,
         list(num("model|||lr_statistic", lr, 1e-8), num("model|||lr_df", 2), num("model|||lr_p_value", pchisq(lr, 2, lower.tail = FALSE), 1e-6),
              txt("model|||distribution", dist)))
  # Harrell's C of the location's linear predictor: with start–stop rows, concordance's counting-process form
  lp <- model.matrix(~ load + line, x) %*% c(fit$opt$par[[fit$dlist$location]], fit$opt$par[pd_terms$name])
  lp <- if (dist %in% c("exponential", "gompertz")) -lp[, 1] else lp[, 1]
  e <- c(e, list(num("model|||concordance", concordance(Surv(entry, days, event) ~ lp, data = cbind(x, lp = lp), weights = w)$concordance, 1e-8)))
  if (dist == "weibull") {
    h <- pd_hand_max(pd_hand_weibull, c(fit$opt$par[["shape"]], fit$opt$par[["scale"]], fit$opt$par[["load"]], fit$opt$par[["lineB"]]))
    se <- sqrt(diag(h$V))
    e <- c(e, list(num("model|||log_lik", h$loglik, 1e-6),
                   pd_hand_num("coef|(Intercept)||estimate", h$par[[2L]], se[[2L]]), pd_hand_num("coef|load||estimate", h$par[[3L]], se[[3L]]),
                   pd_hand_num("coef|line|B|estimate", h$par[[4L]], se[[4L]]),
                   num("coef|load||std_error", se[[3L]], 1e-6), pd_hand_num("parameter|shape||estimate", exp(h$par[[1L]]), se[[1L]] * exp(h$par[[1L]]))))
    ex <- pd_fits$exponential
    e <- c(e, list(num("model|||exponential_lr_statistic", 2 * (fit$loglik - ex$loglik), 1e-8), num("model|||exponential_lr_df", 1),
                   txt("model|||variant", "ph_aft")))
    e <- c(e, ps_expect_cox(coxph(Surv(entry, days, event) ~ load + line, data = x, weights = w, ties = "efron"), pd_terms))
  }
  if (dist == "gompertz") {
    h <- pd_hand_max(pd_hand_gompertz, c(fit$opt$par[["shape"]], fit$opt$par[["rate"]], fit$opt$par[["load"]], fit$opt$par[["lineB"]]))
    se <- sqrt(diag(h$V))
    e <- c(e, list(num("model|||log_lik", h$loglik, 1e-6),
                   pd_hand_num("coef|(Intercept)||estimate", h$par[[2L]], se[[2L]]), pd_hand_num("coef|load||estimate", h$par[[3L]], se[[3L]]),
                   pd_hand_num("coef|line|B|estimate", h$par[[4L]], se[[4L]]), pd_hand_num("parameter|shape||estimate", h$par[[1L]], se[[1L]]),
                   num("model|||exponential_lr_statistic", 2 * (fit$loglik - pd_fits$exponential$loglik), 1e-8),
                   txt("model|||variant", "ph")))
    e <- c(e, ps_expect_cox(coxph(Surv(entry, days, event) ~ load + line, data = x, weights = w, ties = "efron"), pd_terms))
  }
  if (dist %in% c("lognormal", "loglogistic", "gengamma")) e <- c(e, list(txt("model|||variant", "aft")))
  if (dist == "exponential") e <- c(e, list(txt("model|||variant", "ph_aft")))
  cases[[length(cases) + 1L]] <- list(id = paste0("ps_delayed_", dist), model = "parametric_survival", data = "data/ps_delayed.csv",
                                      types = pd_types, params = c(pd_params, list(distribution = dist)), expect = e)
}

# ── a start of 0 for every row: no truncation, so survreg can be the witness ──
for (dist in c("weibull", "lognormal")) {
  sr <- survreg(Surv(days, event) ~ load + line, data = x, weights = w, dist = dist, control = PS_SR_CONTROL)
  e <- c(ps_expect_survreg(sr, pd_terms, dist, profile = pd_profile, hr = dist == "weibull"),
         list(num("model|||n_delayed", 0), txt("model|||formula", "Surv(zero, days, status = failed) ~ load + line"),
              txt("model|||note_start_rows", paste0("@start_rows delayed=0 rows=", nrow(x)))))
  cases[[length(cases) + 1L]] <- list(id = paste0("ps_delayed_zero_", dist), model = "parametric_survival", data = "data/ps_delayed.csv",
                                      types = pd_types, params = c(utils::modifyList(pd_params, list(start = "zero")), list(distribution = dist)),
                                      expect = e)
}

# ── the checks with entry ──
pd_weibull <- pd_fits$weibull
pd_h <- pd_hand_max(pd_hand_weibull, c(pd_weibull$opt$par[["shape"]], pd_weibull$opt$par[["scale"]], pd_weibull$opt$par[["load"]], pd_weibull$opt$par[["lineB"]]))
# dfbetas: V · U_i, the rows' score by Richardson's jacobian of the unweighted row log-likelihood, over the standard errors
pd_U <- numDeriv::jacobian(function(b) pd_hand_weibull(b, rows = TRUE), pd_h$par)
pd_dfb <- abs(pd_U %*% pd_h$V) # the columns: log shape, intercept, load, line B
pd_dfb <- sweep(pd_dfb, 2L, sqrt(diag(pd_h$V)), "/")
pd_score <- cbind(load = pd_dfb[, 3L], line = pd_dfb[, 4L])
pd_rows_named <- function(score) paste(rownames(x)[head(order(-score), 5L)], collapse = ", ")
pd_zph <- cox.zph(coxph(Surv(entry, days, event) ~ load + line, data = x, weights = w, ties = "efron"), transform = "km", terms = TRUE)$table
pd_gg <- pd_fits$gengamma

# the group's Kaplan–Meier with entry, from the first time with at least 10 rows at risk (the last time
# before it is t0, 0 when the first time has them), and the gap to the mean fitted survival
# conditional on lasting until t0
pd_agreement <- function(fit, rows) {
  km <- survfit(Surv(entry, days, event) ~ 1, data = rows, weights = w)
  first <- which(km$n.risk >= 10)[[1L]]
  t0 <- if (first == 1L) 0 else km$time[[first - 1L]]
  late <- rows[rows$days > t0, ]
  late$entry <- pmax(late$entry, t0)
  s <- summary(survfit(Surv(entry, days, event) ~ 1, data = late, weights = w))
  at <- which(s$n.event > 0 & s$n.risk >= 10 & s$std.err > 0)
  if (length(at) > 60) at <- at[unique(round(seq(1, length(at), length.out = 60)))]
  grid <- c(t0, s$time[at])
  m <- matrix(summary(fit, newdata = rows, type = "survival", t = grid, ci = FALSE, tidy = TRUE, cross = TRUE)$est, nrow = nrow(rows), byrow = TRUE)
  fitted <- colSums(rows$w * m[, -1L, drop = FALSE]) / sum(rows$w * m[, 1L]) # m[, 1] is S(t0): 1 when t0 is 0
  gap <- abs(fitted - s$surv[at])
  j <- which.max(gap)
  list(gap = gap[[j]], time = s$time[at][[j]], d = gap[[j]] / s$std.err[at][[j]], t0 = t0)
}

e <- list(num("assumption|epv||value", sum(x$event) / 4), num("assumption|epv||count", sum(x$event)),
          txt("assumption|convergence||verdict", "ok"), txt("assumption|separation||verdict", "ok"), num("model|||n_delayed", sum(x$entry > 0)))
for (term in c("load", "line")) {
  e <- c(e, list(num(paste0("assumption|dfbeta|", term, "|value"), max(pd_score[, term]), 1e-5),
                 txt(paste0("assumption|dfbeta|", term, "|rows"), pd_rows_named(pd_score[, term]))))
  row <- pd_zph[term, ]
  e <- c(e, list(num(paste0("assumption|ph_cox|", term, "|statistic"), row[["chisq"]]), num(paste0("assumption|ph_cox|", term, "|df"), row[["df"]]),
                 num(paste0("assumption|ph_cox|", term, "|p_value"), row[["p"]], 1e-8),
                 txt(paste0("assumption|ph_cox|", term, "|verdict"), if (row[["p"]] < 0.05) "failed" else "ok")))
}
e <- c(e, list(num("assumption|ph_cox_global||statistic", pd_zph["GLOBAL", "chisq"]), num("assumption|ph_cox_global||p_value", pd_zph["GLOBAL", "p"], 1e-8),
               txt("assumption|ph_cox_global||verdict", if (pd_zph["GLOBAL", "p"] < 0.05) "failed" else "ok")))
lr_gg <- 2 * (pd_gg$loglik - pd_weibull$loglik)
e <- c(e, list(num("assumption|distribution_vs_gengamma||statistic", lr_gg, 1e-8), num("assumption|distribution_vs_gengamma||df", 1),
               num("assumption|distribution_vs_gengamma||p_value", pchisq(lr_gg, 1, lower.tail = FALSE), 1e-6)))
pd_t0 <- numeric()
for (g in levels(x$line)) {
  a <- pd_agreement(pd_weibull, x[x$line == g, ])
  pd_t0[[g]] <- a$t0
  key <- function(stat) paste0("assumption|km_agreement|line=", g, "|", stat)
  e <- c(e, list(num(key("value"), a$gap, 1e-6), num(key("at"), a$time, 1e-9), num(key("statistic"), a$d, 1e-6), num(key("count"), sum(x$line == g))))
}
if (!(pd_t0[["A"]] == 0 && pd_t0[["B"]] > 0)) stop("the table of the delayed entry should read line B from a time above 0 and line A from 0")
# the times asked (10, 30) count as the quantiles do in the extrapolation
q <- summary(pd_weibull, newdata = pd_profile, type = "quantile", quantiles = c(0.5, 0.25, 0.75), ci = FALSE, tidy = TRUE, cross = TRUE)$est
e <- c(e, list(num("assumption|extrapolation||value", max(c(10, 30, q)) / max(x$days), 1e-8), num("assumption|extrapolation||at", max(x$days))))
cases[[length(cases) + 1L]] <- list(id = "ps_delayed_checks", model = "parametric_survival", data = "data/ps_delayed.csv", types = pd_types,
                                    params = c(pd_params, list(distribution = "weibull")), expect = e)

# ── auto with a start: the six candidates by flexsurv, the rule written here ──
pd_x <- x
names(pd_x)[names(pd_x) == "days"] <- "time"
pd_cand <- list()
for (d_ in PS_AUTO_ORDER_REF) pd_cand[[d_]] <- list(converged = TRUE, k = length(pd_fits[[d_]]$opt$par), loglik = pd_fits[[d_]]$loglik)
pd_a <- ps_auto_expect(pd_cand, nrow(x), vapply(pd_cand, function(f) f$k, numeric(1)))
e <- c(pd_a$expect, list(txt("model|||distribution", pd_a$chosen), txt("model|||selection", "auto"), num("model|||n_delayed", sum(x$entry > 0))))
if (pd_a$chosen != pd_a$lowest) e <- c(e, list(txt("model|||note_auto_close", ps_auto_close_note(pd_a))))
cases[[length(cases) + 1L]] <- list(id = "ps_delayed_auto", model = "parametric_survival", data = "data/ps_delayed.csv", types = pd_types,
                                    params = c(pd_params, list(distribution = "auto")), expect = e)

# ── refusals ──
cases[[length(cases) + 1L]] <- refusal("ps_start_after_time", list(time = "t", start = "x2"), "start_not_before_time",
                                       "in row 1 the start «x2» (3.0) is not before the time «t» (3.0): each row is an interval (start, time]")
cases[[length(cases) + 1L]] <- refusal("ps_start_negative", list(time = "t", start = "t_neg"), "negative_start",
                                       "the column «t_neg» holds a negative start (-1.0): a time is counted from the start, 0 or more")
cases[[length(cases) + 1L]] <- refusal("ps_start_type", list(time = "t", start = "kind"), "time_type")
