# The reference values of the checks of the node «Parametric survival» (docs/specs/statistical-models.md,
# «The node «Parametric survival»» → «The checks» and «Reference values», lot P2), sourced by
# make_reference.R after make_reference_parametric.R, in the group's image: survreg, coxph,
# cox.zph, survfit and flexsurv called directly on the tables with their own column names, never
# the runner. It adds its cases to `cases`; the tables are those of make_reference_parametric.R.
#
# What each case checks, and at which tolerance:
#   dfbeta         the largest |dfbetas| of each term and the rows named, against
#                  residuals(survreg, type = "dfbetas") (the same V · U_i; unweighted, as survreg's)
#                                                                  1e-6 relative (1.6e-10 measured);
#   ph_cox         the statistic, degrees of freedom and p-value of each term and of the whole,
#                  against cox.zph(coxph(…), transform = "km", terms = TRUE) on the same
#                  predictors, weights and ties                    1e-9 (the same construction);
#   distribution_vs_gengamma, constant_hazard
#                  2 (ℓ_gengamma − ℓ) and 2 (ℓ_weibull − ℓ_exponential) from flexsurv's fits brought
#                  to their maxima (ps_polish), its degrees of freedom and p-value 1e-8 (two maxima
#                  of one likelihood: a second-order quantity);
#   km_agreement   the largest gap in survival, where it is reached and the gap in Greenwood's
#                  standard errors there, from a second construction: survfit per group with
#                  summary.survfit's standard error, the mean fitted survival from flexsurv's
#                  summary(newdata) over the group's rows weighted                 1e-6;
#   epv, extrapolation
#                  events / parameters; the largest of the profile's median and quartiles over the
#                  last time, flexsurv's own quantiles                              1e-9; 1e-8.
# The verdicts that follow a p-value are written (alpha is the node's, 0.05 here); the measures
# (km_agreement, dfbeta, epv, extrapolation) have a number and no verdict.

ck_types <- list(days = "number", status = "text", load = "number", line = "text", plant = "text", w = "integer")
ck_d <- read_table("ps_weibull.csv")
ck_d$event <- as.integer(ck_d$status == "failed")
ck_used <- complete.cases(ck_d[, c("days", "status", "load", "line", "w")]) & ck_d$days > 0
ck_x <- ck_d[ck_used, ]
ck_x$line <- factor(ck_x$line, levels = c("A", "B", "C"))
ck_x$w <- as.numeric(ck_x$w) # cox.zph refuses integer weights
ck_events <- sum(ck_x$event)

# D and the gap of one group: Kaplan–Meier's curve (weights as counts) at its event times with at
# least 10 rows at risk, the fitted survival averaged over the group's rows.
ck_agreement <- function(fit, rows) {
  s <- summary(survfit(Surv(days, event) ~ 1, data = rows, weights = w))
  at <- which(s$n.event > 0 & s$n.risk >= 10 & s$std.err > 0)
  if (length(at) > 60) at <- at[unique(round(seq(1, length(at), length.out = 60)))] # the runner reads 60 times at most
  m <- matrix(summary(fit, newdata = rows, type = "survival", t = s$time[at], ci = FALSE, tidy = TRUE, cross = TRUE)$est,
              nrow = nrow(rows), byrow = TRUE)
  gap <- abs(colSums(rows$w * m) / sum(rows$w) - s$surv[at])
  j <- which.max(gap)
  list(gap = gap[[j]], time = s$time[at][[j]], d = gap[[j]] / s$std.err[at][[j]])
}

ck_polish <- function(formula, dist, data) {
  fit <- flexsurvreg(formula, data = data, weights = w, dist = dist, control = PS_CONTROL)
  ps_polish(fit, function(nat) flexsurvreg(formula, data = data, weights = w, dist = dist, inits = nat, fixedpars = TRUE)$loglik)
}

ck_agreement_expect <- function(fit, x, by = "line") {
  e <- list()
  for (g in levels(x[[by]])) {
    rows <- x[x[[by]] == g, ]
    a <- ck_agreement(fit, rows)
    key <- function(stat) paste0("assumption|km_agreement|", by, "=", g, "|", stat)
    e <- c(e, list(num(key("value"), a$gap, 1e-6), num(key("at"), a$time, 1e-9), num(key("statistic"), a$d, 1e-6), num(key("count"), nrow(rows))))
  }
  e
}

# ── the Weibull on the 240 rows, no interaction: every check ──
ck_form <- Surv(days, event) ~ load + line
ck_params <- list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), weights = "w")
ck_weibull <- ck_polish(ck_form, "weibull", ck_x)
ck_expo <- ck_polish(ck_form, "exp", ck_x)
ck_gg <- ck_polish(ck_form, "gengamma", ck_x)
ck_sr <- survreg(Surv(days, event) ~ load + line, data = ck_x, weights = w, dist = "weibull", control = PS_SR_CONTROL)
ck_dfbetas <- residuals(ck_sr, type = "dfbetas")
colnames(ck_dfbetas) <- c(names(coef(ck_sr)), "Log(scale)") # survreg leaves them unnamed
ck_score <- cbind(load = abs(ck_dfbetas[, "load"]), line = pmax(abs(ck_dfbetas[, "lineB"]), abs(ck_dfbetas[, "lineC"])))
ck_named <- function(score) {
  paste(rownames(ck_x)[head(order(-score), 5L)], collapse = ", ")
}
ck_zph <- cox.zph(coxph(Surv(days, event) ~ load + line, data = ck_x, weights = w, ties = "efron"), transform = "km", terms = TRUE)$table
ck_profile <- data.frame(load = sum(ck_x$w * ck_x$load) / sum(ck_x$w), line = factor("A", levels = c("A", "B", "C")))
ck_last <- max(ck_x$days)

e <- list(num("assumption|epv||value", ck_events / length(ck_weibull$opt$par)), num("assumption|epv||count", ck_events),
          txt("assumption|convergence||verdict", "ok"), txt("assumption|separation||verdict", "ok"))
for (term in c("load", "line")) {
  e <- c(e, list(num(paste0("assumption|dfbeta|", term, "|value"), max(ck_score[, term]), 1e-6),
                 txt(paste0("assumption|dfbeta|", term, "|rows"), ck_named(ck_score[, term]))))
  row <- ck_zph[term, ]
  e <- c(e, list(num(paste0("assumption|ph_cox|", term, "|statistic"), row[["chisq"]]), num(paste0("assumption|ph_cox|", term, "|df"), row[["df"]]),
                 num(paste0("assumption|ph_cox|", term, "|p_value"), row[["p"]], 1e-8),
                 txt(paste0("assumption|ph_cox|", term, "|verdict"), if (row[["p"]] < 0.05) "failed" else "ok")))
}
e <- c(e, list(num("assumption|ph_cox_global||statistic", ck_zph["GLOBAL", "chisq"]), num("assumption|ph_cox_global||df", ck_zph["GLOBAL", "df"]),
               num("assumption|ph_cox_global||p_value", ck_zph["GLOBAL", "p"], 1e-8),
               txt("assumption|ph_cox_global||verdict", if (ck_zph["GLOBAL", "p"] < 0.05) "failed" else "ok")))
lr_gg <- 2 * (ck_gg$loglik - ck_weibull$loglik)
e <- c(e, list(num("assumption|distribution_vs_gengamma||statistic", lr_gg, 1e-8), num("assumption|distribution_vs_gengamma||df", 1),
               num("assumption|distribution_vs_gengamma||p_value", pchisq(lr_gg, 1, lower.tail = FALSE), 1e-6),
               txt("assumption|distribution_vs_gengamma||verdict", if (pchisq(lr_gg, 1, lower.tail = FALSE) < 0.05) "failed" else "ok")),
       ck_agreement_expect(ck_weibull, ck_x))
q <- summary(ck_weibull, newdata = ck_profile, type = "quantile", quantiles = c(0.5, 0.25, 0.75), ci = FALSE, tidy = TRUE, cross = TRUE)$est
e <- c(e, list(num("assumption|extrapolation||value", max(q) / ck_last, 1e-8), num("assumption|extrapolation||at", ck_last)))
cases[[length(cases) + 1L]] <- list(id = "ps_checks_weibull", model = "parametric_survival", data = "data/ps_weibull.csv", types = ck_types,
                                    params = c(ck_params, list(distribution = "weibull")), expect = e)

# ── the exponential on the same rows (a wrong model): the constant hazard, the generalized gamma on 2 df ──
lr_w <- 2 * (ck_weibull$loglik - ck_expo$loglik)
lr_g2 <- 2 * (ck_gg$loglik - ck_expo$loglik)
e <- c(list(num("assumption|constant_hazard||statistic", lr_w, 1e-8), num("assumption|constant_hazard||df", 1),
            num("assumption|constant_hazard||p_value", pchisq(lr_w, 1, lower.tail = FALSE), 1e-6),
            txt("assumption|constant_hazard||verdict", if (pchisq(lr_w, 1, lower.tail = FALSE) < 0.05) "failed" else "ok"),
            num("assumption|distribution_vs_gengamma||statistic", lr_g2, 1e-8), num("assumption|distribution_vs_gengamma||df", 2),
            num("assumption|distribution_vs_gengamma||p_value", pchisq(lr_g2, 2, lower.tail = FALSE), 1e-6),
            txt("assumption|distribution_vs_gengamma||verdict", if (pchisq(lr_g2, 2, lower.tail = FALSE) < 0.05) "failed" else "ok"),
            num("assumption|epv||value", ck_events / length(ck_expo$opt$par))),
       ck_agreement_expect(ck_expo, ck_x))
cases[[length(cases) + 1L]] <- list(id = "ps_checks_exponential", model = "parametric_survival", data = "data/ps_weibull.csv", types = ck_types,
                                    params = c(ck_params, list(distribution = "exponential")), expect = e)

# ── Gompertz: the proportional-hazards test of the Cox fit on its predictors ──
ck_g <- read_table("ps_gompertz.csv")
ck_g$smoker_f <- factor(ck_g$smoker, levels = c("false", "true"))
ck_zg <- cox.zph(coxph(Surv(years, died) ~ age + smoker_f, data = ck_g, ties = "efron"), transform = "km", terms = TRUE)$table
e <- list(txt("assumption|convergence||verdict", "ok"))
for (r in list(c("age", "age"), c("smoker", "smoker_f"))) {
  row <- ck_zg[r[[2L]], ]
  e <- c(e, list(num(paste0("assumption|ph_cox|", r[[1L]], "|statistic"), row[["chisq"]]), num(paste0("assumption|ph_cox|", r[[1L]], "|df"), row[["df"]]),
                 num(paste0("assumption|ph_cox|", r[[1L]], "|p_value"), row[["p"]], 1e-8)))
}
e <- c(e, list(num("assumption|ph_cox_global||statistic", ck_zg["GLOBAL", "chisq"]), num("assumption|ph_cox_global||p_value", ck_zg["GLOBAL", "p"], 1e-8)))
cases[[length(cases) + 1L]] <- list(
  id = "ps_checks_gompertz", model = "parametric_survival", data = "data/ps_gompertz.csv",
  types = list(years = "number", died = "integer", age = "number", smoker = "boolean"),
  params = list(time = "years", event = "died", event_value = 1, predictors = list("age", "smoker"), distribution = "gompertz"),
  expect = e)

# ── no censoring, no weights: dfbetas against survreg's on a second table ──
ck_a <- read_table("ps_all_events.csv")
ck_sa <- survreg(Surv(cycles, failed) ~ temp, data = ck_a, dist = "weibull", control = PS_SR_CONTROL)
ck_da <- abs(residuals(ck_sa, type = "dfbetas")[, 2L]) # (Intercept), temp, Log(scale)
ck_top <- head(order(-ck_da), 5L)
e <- list(num("assumption|dfbeta|temp|value", max(ck_da), 1e-6), txt("assumption|dfbeta|temp|rows", paste(ck_top, collapse = ", ")),
          num("assumption|epv||value", nrow(ck_a) / 3))
cases[[length(cases) + 1L]] <- list(
  id = "ps_checks_all_events", model = "parametric_survival", data = "data/ps_all_events.csv",
  types = list(cycles = "number", failed = "integer", temp = "number"),
  params = list(time = "cycles", event = "failed", event_value = 1, predictors = list("temp"), distribution = "weibull"), expect = e)
