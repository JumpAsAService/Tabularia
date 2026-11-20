# The reference values of the group «survival», computed OUTSIDE Tabularia
# (docs/specs/statistical-models.md, «Reference values»): by hand, with R and the pinned
# packages of ../recipe.json, never by the runner. Run it in the group's image, so that the
# versions are the recipe's:
#
#   docker run --rm --memory=2g --user "$(id -u)" -v "$PWD/gateway-go/internal/modelgroups/groups/survival/reference:/ref" \
#     tabularia-models-survival:dev Rscript /ref/make_reference.R [--data]
#
# --data writes the small tables of data/ again (synthetic, fixed seeds; R's default
# generators); without it they are read as they are. It calls survival's functions directly
# (survfit, survdiff, quantile, summary, coxph, cox.zph) on the tables with their own column
# names, and writes expected.json in the format common/R/selftest.R reads: {"format_version":
# 1, "generated": {"by", "R", "packages"}, "cases": [{"id", "model", "data", "types", "params",
# "expect": [{"key", "value" | "text", "rel", "abs"}], "refuses"}]}.
#
# The second witness, lifelines, is make_witness.py (witness.json says its versions): it
# compares its own estimates with the values written here.
#
# Kaplan–Meier (lane M4): kaplan_meier_strata (three strata, ties, censoring, rows with a
# NULL; log-rank, pairs, times, a restricted mean), kaplan_meier_peto (the same table, Peto's
# test, log-log intervals at 90%), kaplan_meier_crossing (two strata whose curves cross; a
# 0/1 event column), kaplan_meier_single (no strata, a boolean event, plain intervals, a time
# past the follow-up), kaplan_meier_no_events (refused).
#
# Cox (lane M5): cox_categorical (a number and a category of three levels, ties of whole days,
# rows with a NULL; Efron), cox_breslow (a boolean, the category's reference given, an
# interaction, Breslow's ties, intervals at 90%), cox_exact (the exact partial likelihood),
# cox_strata (three strata), cox_robust (strata and errors clustered by customer),
# cox_robust_rows (robust errors without a cluster), cox_nonph (a treatment whose hazards are
# not proportional, a dose with a U-shaped effect), cox_start_stop (counting-process rows,
# clustered by patient), cox_no_events and cox_start_after_time (refused). coxph is called with
# the columns' own names and R's formula interface (factor, relevel, strata, cluster); the VIF of
# a term of one column is 1/(1 − R²) of its regression on the others, the linear form the
# likelihood-ratio test of ns(x, df = 3) added.
#
# Parametric survival (lot P1): make_reference_parametric.R, sourced at the end (its own tables,
# flexsurv, survreg and coxph called directly; the header of that file says what each case
# checks and at which tolerance).

suppressPackageStartupMessages(library(survival))

here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
args <- commandArgs(trailingOnly = TRUE)
data_dir <- file.path(here, "data")

# ── the tables ──
write_table <- function(df, name) {
  dir.create(data_dir, showWarnings = FALSE)
  utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", quote = TRUE, fileEncoding = "UTF-8")
}

make_data <- function() {
  set.seed(20261120)
  n <- c(A = 60, B = 50, C = 40)
  rate <- c(A = 1 / 150, B = 1 / 80, C = 1 / 45)
  line <- rep(names(n), n)
  event_time <- stats::rexp(sum(n), rate[line])
  censor_time <- stats::runif(sum(n), 20, 260)
  days <- ceiling(pmin(event_time, censor_time) / 2) * 2 # whole days, even: ties
  status <- ifelse(event_time <= censor_time, "failed", "running")
  strata <- data.frame(days = days, status = status, line = line, stringsAsFactors = FALSE)
  strata$days[c(7, 88)] <- NA # rows left out: no time, no stratum
  strata$line[[121]] <- NA
  strata <- strata[sample.int(nrow(strata)), ]
  write_table(strata, "kaplan_meier_strata.csv")

  set.seed(20261121)
  # basic: many leave early, then few; premium: a steady pace. The curves cross.
  basic <- ifelse(stats::runif(70) < 0.5, stats::rexp(70, 1 / 1.5), stats::rexp(70, 1 / 80))
  premium <- stats::rexp(70, 1 / 12)
  t <- c(basic, premium)
  censor_time <- stats::runif(140, 10, 48)
  crossing <- data.frame(months = round(pmin(t, censor_time), 1), churned = as.integer(t <= censor_time),
                         plan = rep(c("basic", "premium"), each = 70), stringsAsFactors = FALSE)
  crossing <- crossing[sample.int(nrow(crossing)), ]
  write_table(crossing, "kaplan_meier_crossing.csv")

  set.seed(20261122)
  event_time <- stats::rweibull(48, shape = 1.6, scale = 14)
  censor_time <- stats::runif(48, 4, 30)
  single <- data.frame(hours = round(pmin(event_time, censor_time), 2),
                       broke = ifelse(event_time <= censor_time, "true", "false"), stringsAsFactors = FALSE)
  write_table(single, "kaplan_meier_single.csv")
}

if ("--data" %in% args || !file.exists(file.path(data_dir, "kaplan_meier_strata.csv"))) make_data()

make_cox_data <- function() {
  # a categorical predictor (three lines), a number, a boolean; whole days in steps of 3: ties
  set.seed(20261123)
  n <- 150
  load <- round(stats::runif(n, 20, 90), 1)
  line <- sample(c("A", "B", "C"), n, TRUE, prob = c(0.4, 0.35, 0.25))
  night <- stats::runif(n) < 0.4
  lp <- 0.025 * (load - 50) + c(A = 0, B = 0.6, C = -0.5)[line] + 0.35 * night
  event_time <- stats::rweibull(n, shape = 1.3, scale = 120 * exp(-lp / 1.3))
  censor_time <- stats::runif(n, 30, 250)
  categorical <- data.frame(days = ceiling(pmin(event_time, censor_time) / 3) * 3,
                            status = ifelse(event_time <= censor_time, "failed", "running"), load = load, line = unname(line),
                            night = ifelse(night, "true", "false"), stringsAsFactors = FALSE)
  categorical$load[[5]] <- NA
  categorical$line[[40]] <- NA
  categorical$days[[77]] <- NA
  write_table(categorical, "cox_categorical.csv")

  # three regions with baselines of their own (strata), 60 customers of 3 rows each sharing a frailty (cluster)
  set.seed(20261124)
  customer <- rep(sprintf("c%02d", 1:60), each = 3)
  region <- rep(sample(c("north", "south", "east"), 60, TRUE), each = 3)
  frailty <- rep(stats::rnorm(60, 0, 0.6), each = 3)
  price <- round(stats::runif(180, 5, 40), 2)
  premium <- stats::runif(180) < 0.5
  lp <- 0.04 * (price - 20) - 0.6 * premium + frailty
  shape <- c(north = 1, south = 1.5, east = 0.8)[region]
  scale <- c(north = 20, south = 12, east = 30)[region]
  t <- stats::rweibull(180, shape = shape, scale = scale * exp(-lp / shape))
  censor_time <- stats::runif(180, 6, 48)
  strata <- data.frame(months = round(pmin(t, censor_time), 1), churned = as.integer(t <= censor_time), price = price,
                       premium = ifelse(premium, "true", "false"), region = region, customer = customer, stringsAsFactors = FALSE)
  write_table(strata, "cox_strata.csv")

  # a treatment that helps early and harms late (its hazards are not proportional), a dose with a U-shaped effect
  set.seed(20261125)
  n <- 160
  treatment <- rep(c("drug", "placebo"), each = n / 2)
  dose <- round(stats::runif(n, 0, 10), 2)
  base <- 0.08 * exp(0.12 * (dose - 5)^2 - 0.8)
  early <- base * ifelse(treatment == "drug", 0.25, 1)
  late <- base * ifelse(treatment == "drug", 2, 1)
  e1 <- stats::rexp(n, early)
  e2 <- 6 + stats::rexp(n, late)
  event_time <- ifelse(e1 < 6, e1, e2)
  censor_time <- stats::runif(n, 8, 40)
  nonph <- data.frame(weeks = round(pmin(event_time, censor_time), 2), dead = ifelse(event_time <= censor_time, "true", "false"),
                      treatment = treatment, dose = dose, stringsAsFactors = FALSE)
  nonph <- nonph[sample.int(n), ]
  write_table(nonph, "cox_nonph.csv")

  # start–stop rows: a dose that changes at each visit, a group; up to three intervals a patient
  set.seed(20261126)
  parts <- list()
  for (id in 1:90) {
    group <- sample(c("a", "b"), 1)
    visits <- sort(unique(c(0, round(stats::runif(sample(1:3, 1), 2, 20), 1))))
    end <- round(stats::runif(1, 15, 30), 1)
    starts <- visits[visits < end]
    stops <- c(starts[-1], end)
    for (j in seq_along(starts)) {
      dose <- round(stats::runif(1, 0, 4), 1)
      rate <- 0.04 * exp(0.35 * dose + 0.5 * (group == "b"))
      e <- starts[[j]] + stats::rexp(1, rate)
      if (e <= stops[[j]]) {
        parts[[length(parts) + 1L]] <- data.frame(patient = sprintf("p%02d", id), start = starts[[j]], stop = max(round(e, 2), starts[[j]] + 0.01),
                                                  event = 1L, dose = dose, group = group)
        break
      }
      parts[[length(parts) + 1L]] <- data.frame(patient = sprintf("p%02d", id), start = starts[[j]], stop = stops[[j]],
                                                event = 0L, dose = dose, group = group)
    }
  }
  write_table(do.call(rbind, parts), "cox_start_stop.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "cox_categorical.csv"))) make_cox_data()

read_table <- function(name) utils::read.csv(file.path(data_dir, name), na.strings = "", stringsAsFactors = FALSE)

# ── the expected values ──
num <- function(key, value, rel = 1e-9) list(key = key, value = if (is.finite(value)) unname(value) else NULL, rel = rel)
txt <- function(key, text) list(key = key, text = text)
sig <- function(p) if (p < 0.001) "***" else if (p < 0.01) "**" else if (p < 0.05) "*" else if (p < 0.1) "." else ""
label <- function(t) if (t == trunc(t)) format(t, scientific = FALSE) else format(t, digits = 15)

# The Kaplan–Meier values of a table: `d` with columns time, status (0/1) and, with strata,
# the strata column under its own name; `names` the strata as the runner names them.
km_expect <- function(d, strata, conf_type, alpha, rho, times, tau, pairwise, n_missing) {
  f <- if (is.null(strata)) Surv(time, status) ~ 1 else stats::as.formula(paste("Surv(time, status) ~", strata))
  fit <- survfit(f, data = d, conf.type = conf_type, conf.int = 1 - alpha)
  k <- if (is.null(fit$strata)) 1L else length(fit$strata)
  names_ <- if (is.null(fit$strata)) "all" else names(fit$strata)
  e <- list(num("model|||n", nrow(d)), num("model|||events", sum(d$status)), num("model|||censored", sum(d$status == 0)),
            num("model|||n_missing", n_missing), num("model|||n_strata", k), num("model|||rmst_tau", tau),
            txt("model|||conf_type", conf_type))
  q <- quantile(fit, probs = c(0.25, 0.5, 0.75), conf.int = TRUE)
  as_rows <- function(x) if (is.matrix(x)) x else matrix(x, nrow = 1L)
  qq <- lapply(q, as_rows)
  s_at <- if (length(times)) summary(fit, times = times, extend = TRUE) else NULL
  rm <- summary(fit, rmean = tau)$table
  rm <- if (is.matrix(rm)) rm else matrix(rm, nrow = 1L, dimnames = list(NULL, names(rm)))
  for (i in seq_len(k)) {
    name <- names_[[i]]
    rows_i <- if (k == 1L) seq_len(nrow(d)) else which(d[[strata]] == sub("^[^=]*=", "", name))
    key <- function(stat) paste0("stratum|", name, "||", stat)
    e <- c(e, list(num(key("n"), length(rows_i)), num(key("events"), sum(d$status[rows_i])),
                   num(key("censored"), sum(d$status[rows_i] == 0))))
    for (j in 1:3) {
      stat <- c("q25", "median", "q75")[[j]]
      e <- c(e, list(num(key(stat), qq$quantile[i, j]), num(key(paste0(stat, "_ci_low")), qq$lower[i, j]),
                     num(key(paste0(stat, "_ci_high")), qq$upper[i, j])))
    }
    last <- max(d$time[rows_i])
    for (t in times) {
      at <- if (k == 1L) which(s_at$time == t) else which(s_at$time == t & as.integer(s_at$strata) == i)
      surv <- s_at$surv[at]
      past <- t > last && surv > 0
      stat <- paste0("surv_at_", label(t))
      e <- c(e, list(num(key(stat), if (past) NA else surv), num(key(paste0(stat, "_low")), if (past) NA else s_at$lower[at]),
                     num(key(paste0(stat, "_high")), if (past) NA else s_at$upper[at])))
    }
    e <- c(e, list(num(key("rmst"), rm[i, "rmean"]), num(key("rmst_se"), rm[i, "se(rmean)"])))
  }
  if (k >= 2L) {
    sd <- survdiff(f, data = d, rho = rho)
    df_ <- sum(sd$exp > 0) - 1
    p <- pchisq(sd$chisq, df_, lower.tail = FALSE)
    o <- survdiff(f, data = d, rho = 0)
    e <- c(e, list(num("model|||test_statistic", sd$chisq), num("model|||test_df", df_), num("model|||test_p_value", p),
                   txt("model|||test_significance", sig(p))))
    for (i in seq_len(k)) {
      e <- c(e, list(num(paste0("stratum|", names_[[i]], "||observed"), o$obs[[i]]),
                     num(paste0("stratum|", names_[[i]], "||expected"), o$exp[[i]])))
    }
    if (pairwise) {
      levels <- sub("^[^=]*=", "", names_)
      pairs <- combn(k, 2L)
      ps <- apply(pairs, 2L, function(ij) {
        sub_ <- d[d[[strata]] %in% levels[ij], ]
        x <- survdiff(f, data = sub_, rho = rho)
        c(x$chisq, pchisq(x$chisq, 1, lower.tail = FALSE))
      })
      adj <- p.adjust(ps[2L, ], "holm")
      for (j in seq_len(ncol(pairs))) {
        key <- paste0("pair|", names_[[pairs[1L, j]]], "|", names_[[pairs[2L, j]]], "|")
        e <- c(e, list(num(paste0(key, "statistic"), ps[1L, j]), num(paste0(key, "p_value"), ps[2L, j]),
                       num(paste0(key, "p_adjusted"), adj[[j]]), txt(paste0(key, "significance"), sig(adj[[j]]))))
      }
    }
    # the spies of informative censoring
    ct <- suppressWarnings(chisq.test(table(d$status == 0, d[[strata]]), correct = FALSE))
    e <- c(e, list(num("assumption|censoring_by_stratum||statistic", ct$statistic),
                   num("assumption|censoring_by_stratum||p_value", ct$p.value),
                   txt("assumption|censoring_by_stratum||verdict", if (ct$p.value < alpha) "doubtful" else "ok")))
    fu <- survdiff(stats::as.formula(paste("Surv(time, 1 - status) ~", strata)), data = d)
    fu_p <- pchisq(fu$chisq, sum(fu$exp > 0) - 1, lower.tail = FALSE)
    e <- c(e, list(num("assumption|follow_up_by_stratum||statistic", fu$chisq),
                   num("assumption|follow_up_by_stratum||p_value", fu_p),
                   txt("assumption|follow_up_by_stratum||verdict", if (fu_p < alpha) "doubtful" else "ok")))
    zph <- cox.zph(coxph(f, data = d), transform = "km")$table
    e <- c(e, list(num("assumption|proportional_hazards||statistic", zph["GLOBAL", "chisq"]),
                   num("assumption|proportional_hazards||df", zph["GLOBAL", "df"]),
                   num("assumption|proportional_hazards||p_value", zph["GLOBAL", "p"])))
  }
  e <- c(e, list(txt("assumption|censoring_informative||verdict", "not_computed"),
                 num("assumption|censoring_informative||value", mean(d$status == 0))))
  # a few points of the charts: the first stratum's steps (the curve starts at 0, 1)
  s1 <- if (k == 1L) fit else fit[1]
  for (j in c(1L, 5L, 10L)) {
    i <- j - 1L + as.integer(s1$time[[1L]] > 0)
    e <- c(e, list(num(paste0("curve|km|", names_[[1L]], "|surv|", i), s1$surv[[j]]),
                   num(paste0("curve|km|", names_[[1L]], "|lower|", i), s1$lower[[j]]),
                   num(paste0("curve|cumhaz|", names_[[1L]], "|cumhaz|", i), s1$cumhaz[[j]])))
  }
  e
}

cases <- list()

# three strata, the log-rank test and its pairs
d <- read_table("kaplan_meier_strata.csv")
ok <- stats::complete.cases(d)
x <- data.frame(time = d$days[ok], status = as.integer(d$status[ok] == "failed"), line = d$line[ok])
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_strata", model = "kaplan_meier", data = "data/kaplan_meier_strata.csv",
  types = list(days = "number", status = "text", line = "text"),
  params = list(time = "days", event = "status", event_value = "failed", strata = "line", pairwise = TRUE, times = as.list(c(30, 90)),
                rmst_tau = 180),
  expect = c(km_expect(x, "line", "log", 0.05, 0, c(30, 90), 180, TRUE, sum(!ok)),
             list(txt("model|||formula", "Surv(days, status = failed) ~ line"), txt("model|||test", "logrank"))))

# the same table: Peto's test, log-log intervals at 90%, no pairs
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_peto", model = "kaplan_meier", data = "data/kaplan_meier_strata.csv",
  types = list(days = "number", status = "text", line = "text"),
  params = list(time = "days", event = "status", event_value = "failed", strata = "line", test = "peto", conf_type = "log-log",
                alpha = 0.1, times = list(60)),
  expect = c(km_expect(x, "line", "log-log", 0.1, 1, 60, min(tapply(x$time, x$line, max)), FALSE, sum(!ok)),
             list(txt("model|||test", "peto"))))

# two strata whose curves cross; the event a 0/1 number
d <- read_table("kaplan_meier_crossing.csv")
x <- data.frame(time = d$months, status = d$churned, plan = d$plan)
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_crossing", model = "kaplan_meier", data = "data/kaplan_meier_crossing.csv",
  types = list(months = "number", churned = "integer", plan = "text"),
  params = list(time = "months", event = "churned", event_value = 1, strata = "plan", times = as.list(c(6, 24))),
  expect = c(km_expect(x, "plan", "log", 0.05, 0, c(6, 24), min(tapply(x$time, x$plan, max)), FALSE, 0),
             list(txt("assumption|proportional_hazards||verdict", "failed"), num("assumption|proportional_hazards||count", 1))))

# no strata, a boolean event, plain intervals, a time past the follow-up
d <- read_table("kaplan_meier_single.csv")
x <- data.frame(time = d$hours, status = as.integer(d$broke == "true"))
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_single", model = "kaplan_meier", data = "data/kaplan_meier_single.csv",
  types = list(hours = "number", broke = "boolean"),
  params = list(time = "hours", event = "broke", event_value = TRUE, conf_type = "plain", times = as.list(c(5, 12.5, 1000)), rmst_tau = 20),
  expect = c(km_expect(x, NULL, "plain", 0.05, 0, c(5, 12.5, 1000), 20, FALSE, 0),
             list(txt("model|||formula", "Surv(hours, broke = true) ~ 1"))))

# the node's defaults: no time asked, no restricted mean (found on kind: the empty `times`
# broke the runner). The same table as above, so the same numbers but those that follow `times` and `rmst_tau`.
single_case <- cases[[length(cases)]]
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_default", model = "kaplan_meier", data = "data/kaplan_meier_single.csv",
  types = list(hours = "number", broke = "boolean"),
  params = list(time = "hours", event = "broke", event_value = TRUE, conf_type = "plain"),
  expect = Filter(function(e) !grepl("surv_at|rmst", e$key), single_case$expect))

# no row is the event: refused
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_no_events", model = "kaplan_meier", data = "data/kaplan_meier_single.csv",
  types = list(hours = "number", broke = "boolean"),
  params = list(time = "hours", event = "broke", event_value = "maybe"),
  expect = list(), refuses = list(code = "no_events"))

# ── Cox ──
# The values of a coxph fit called directly: `keys` names its coefficients as the runner's coef
# rows (term, level), `labels` its terms as the checks' level_2, `numeric` the numeric predictors
# (the linear form), `data` the rows used with their own column names, `strata_names` the
# adjusted curves' series.
cox_expect <- function(fit, data, keys, labels, numeric = character(), alpha = 0.05, n_missing = 0, strata_names = "all") {
  s <- summary(fit, conf.int = 1 - alpha)
  cf <- s$coefficients
  ci <- s$conf.int
  robust <- "robust se" %in% colnames(cf)
  e <- list(num("model|||n", fit$n), num("model|||events", fit$nevent), num("model|||n_missing", n_missing),
            txt("model|||se_type", if (robust) "robust" else "classical"), txt("model|||ties", fit$method))
  # summary rounds the Wald statistic for printing: the fit's own
  s$waldtest <- c(test = unname(fit$wald.test), df = s$waldtest[["df"]],
                  pvalue = pchisq(unname(fit$wald.test), s$waldtest[["df"]], lower.tail = FALSE))
  for (test in list(c("lr", "logtest"), c("wald", "waldtest"), c("score", "sctest"))) {
    x <- s[[test[[2L]]]]
    e <- c(e, list(num(paste0("model|||", test[[1L]], "_statistic"), x[["test"]]), num(paste0("model|||", test[[1L]], "_df"), x[["df"]]),
                   num(paste0("model|||", test[[1L]], "_p_value"), x[["pvalue"]]),
                   txt(paste0("model|||", test[[1L]], "_significance"), sig(x[["pvalue"]]))))
  }
  e <- c(e, list(num("model|||concordance", s$concordance[[1L]]), num("model|||concordance_se", s$concordance[[2L]]),
                 num("model|||log_lik", fit$loglik[[2L]]), num("model|||aic", AIC(fit))))
  key <- function(j, stat) paste0("coef|", keys$term[[j]], "|", if (is.na(keys$level[[j]])) "" else keys$level[[j]], "|", stat)
  for (j in seq_len(nrow(cf))) {
    e <- c(e, list(num(key(j, "estimate"), cf[j, "coef"]), num(key(j, "hazard_ratio"), cf[j, "exp(coef)"]),
                   num(key(j, "hr_ci_low"), ci[j, 3L]), num(key(j, "hr_ci_high"), ci[j, 4L]), num(key(j, "std_error"), cf[j, "se(coef)"]),
                   num(key(j, "statistic"), cf[j, "z"]), num(key(j, "p_value"), cf[j, "Pr(>|z|)"]),
                   txt(key(j, "significance"), sig(cf[j, "Pr(>|z|)"]))))
    if (robust) e <- c(e, list(num(key(j, "robust_se"), cf[j, "robust se"])))
  }
  # the VIF of each term on the design: 1/(1 − R²) for one column, Fox and Monette's GVIF^(1/df) for several
  X <- model.matrix(fit)
  assign <- attr(X, "assign")
  terms_ <- unique(assign)
  R <- cor(X)
  for (t in seq_along(terms_)) {
    cols <- which(assign == terms_[[t]])
    v <- if (length(cols) == 1L) {
      if (ncol(X) == 1L) 1 else 1 / (1 - summary(lm(X[, cols] ~ X[, -cols]))$r.squared)
    } else (det(R[cols, cols, drop = FALSE]) * det(R[-cols, -cols, drop = FALSE]) / det(R))^(1 / length(cols))
    e <- c(e, list(num(paste0("assumption|vif|", labels[[t]], "|value"), v),
                   txt(paste0("assumption|vif|", labels[[t]], "|verdict"), if (v > 10) "failed" else if (v > 5) "doubtful" else "ok")))
    for (j in cols) e <- c(e, list(num(key(j, "vif"), v)))
  }
  epv <- fit$nevent / ncol(X)
  e <- c(e, list(num("assumption|epv||value", epv), txt("assumption|epv||verdict", if (epv < 10) "doubtful" else "ok")))
  # proportional hazards
  z <- cox.zph(fit, transform = "km")
  for (t in seq_along(labels)) {
    k <- paste0("assumption|schoenfeld|", labels[[t]], "|")
    e <- c(e, list(num(paste0(k, "statistic"), z$table[t, "chisq"]), num(paste0(k, "df"), z$table[t, "df"]),
                   num(paste0(k, "p_value"), z$table[t, "p"]), txt(paste0(k, "verdict"), if (z$table[t, "p"] < alpha) "failed" else "ok")))
  }
  g <- z$table["GLOBAL", ]
  e <- c(e, list(num("assumption|schoenfeld_global||statistic", g[["chisq"]]), num("assumption|schoenfeld_global||df", g[["df"]]),
                 num("assumption|schoenfeld_global||p_value", g[["p"]]),
                 txt("assumption|schoenfeld_global||verdict", if (g[["p"]] < alpha) "failed" else "ok")))
  e <- c(e, list(num(paste0("point|schoenfeld|", labels[[1L]], "|y|0"), z$y[1L, 1L])))
  smooth <- plot(z, var = 1, df = 4, nsmo = 40, plot = FALSE)
  e <- c(e, list(num(paste0("curve|schoenfeld|", labels[[1L]], "|smooth|0"), smooth$y[1L, 1L]),
                 num(paste0("curve|schoenfeld|", labels[[1L]], "|upper|39"), smooth$y[40L, 2L])))
  # the linear form: a natural spline of 3 df added, the likelihood-ratio test
  for (v in numeric) {
    wider <- coxph(update(formula(fit), stats::as.formula(paste0(". ~ . + splines::ns(", v, ", df = 3)"))), data = data,
                   ties = fit$method)
    stat <- 2 * (wider$loglik[[2L]] - fit$loglik[[2L]])
    p <- pchisq(stat, 2, lower.tail = FALSE)
    k <- paste0("assumption|martingale_form|", v, "|")
    e <- c(e, list(num(paste0(k, "statistic"), stat), num(paste0(k, "df"), 2), num(paste0(k, "p_value"), p),
                   txt(paste0(k, "verdict"), if (p < alpha) "failed" else "ok")))
  }
  # the charts' first points: deviance residuals, the adjusted survival of the first stratum
  e <- c(e, list(num("point|deviance_linear||y|0", residuals(fit, type = "deviance")[[1L]])))
  sf <- suppressWarnings(survfit(fit))
  e <- c(e, list(num(paste0("curve|adjusted_survival|", strata_names[[1L]], "|surv|0"), 1),
                 num(paste0("curve|adjusted_survival|", strata_names[[1L]], "|surv|1"), sf$surv[[1L]]),
                 num(paste0("curve|adjusted_survival|", strata_names[[1L]], "|surv|5"), sf$surv[[5L]])))
  # influence: the largest |dfbetas| of each term, failed above 1, the rows above 2/√n named
  if (fit$method == "exact") { # survival gives no dfbetas for the exact partial likelihood
    return(c(e, lapply(labels, function(l) txt(paste0("assumption|dfbeta|", l, "|verdict"), "not_computed"))))
  }
  dfb <- as.matrix(residuals(fit, type = "dfbetas"))
  for (t in seq_along(terms_)) {
    score <- apply(abs(dfb[, which(assign == terms_[[t]]), drop = FALSE]), 1L, max)
    k <- paste0("assumption|dfbeta|", labels[[t]], "|")
    e <- c(e, list(num(paste0(k, "value"), max(score)), num(paste0(k, "count"), sum(score > 1)), num(paste0(k, "threshold"), 1),
                   txt(paste0(k, "verdict"), if (any(score > 1)) "failed" else "ok")))
  }
  e
}

# a number and a category of three levels, ties: Efron (the report's fixture)
d <- read_table("cox_categorical.csv")
used <- stats::complete.cases(d[, c("days", "status", "load", "line")])
x <- d[used, ]
fit <- coxph(Surv(days, status == "failed") ~ load + line, data = x, ties = "efron")
keys <- data.frame(term = c("load", "line", "line"), level = c(NA, "B", "C"))
null <- coxph(Surv(days, status == "failed") ~ 1, data = x)
cases[[length(cases) + 1L]] <- list(
  id = "cox_categorical", model = "cox", data = "data/cox_categorical.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", night = "boolean"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line")),
  expect = c(cox_expect(fit, x, keys, c("load", "line"), numeric = "load", n_missing = sum(!used)),
             list(txt("model|||formula", "Surv(days, status = failed) ~ load + line"), txt("model|||event_value", "failed"),
                  num("point|martingale|load|y|0", residuals(null, type = "martingale")[[1L]]))))

# a boolean, the category's reference given, an interaction; Breslow; intervals at 90%
used <- stats::complete.cases(d[, c("days", "status", "load", "line", "night")])
x <- d[used, ]
x$line <- relevel(factor(x$line), "C")
x$night <- x$night == "true"
fit <- coxph(Surv(days, status == "failed") ~ load + line + night + load:night, data = x, ties = "breslow")
keys <- data.frame(term = c("load", "line", "line", "night", "load × night"), level = c(NA, "A", "B", "true", "true"))
cases[[length(cases) + 1L]] <- list(
  id = "cox_breslow", model = "cox", data = "data/cox_categorical.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", night = "boolean"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line", "night"),
                reference = list(line = "C"), interactions = list(list("load", "night")), ties = "breslow", alpha = 0.1),
  expect = c(cox_expect(fit, x, keys, c("load", "line", "night", "load × night"), numeric = "load", alpha = 0.1, n_missing = sum(!used)),
             list(txt("model|||formula", "Surv(days, status = failed) ~ load + line + night + load × night"))))

# the exact partial likelihood
used <- stats::complete.cases(d[, c("days", "status", "load", "line")])
x <- d[used, ]
fit <- coxph(Surv(days, status == "failed") ~ load + line, data = x, ties = "exact")
cases[[length(cases) + 1L]] <- list(
  id = "cox_exact", model = "cox", data = "data/cox_categorical.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", night = "boolean"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), ties = "exact"),
  expect = cox_expect(fit, x, data.frame(term = c("load", "line", "line"), level = c(NA, "B", "C")), c("load", "line"),
                      numeric = "load", n_missing = sum(!used)))

# no row is the event: refused
cases[[length(cases) + 1L]] <- list(
  id = "cox_no_events", model = "cox", data = "data/cox_categorical.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", night = "boolean"),
  params = list(time = "days", event = "status", event_value = "lost", predictors = list("load")),
  expect = list(), refuses = list(code = "no_events"))

# three strata; then with errors clustered by customer; then robust errors without a cluster (Breslow)
d <- read_table("cox_strata.csv")
d$premium <- d$premium == "true"
keys <- data.frame(term = c("price", "premium"), level = c(NA, "true"))
types <- list(months = "number", churned = "integer", price = "number", premium = "boolean", region = "text", customer = "text")
strata_names <- paste0("region=", sort(unique(d$region)))
fit <- coxph(Surv(months, churned) ~ price + premium + strata(region), data = d)
cases[[length(cases) + 1L]] <- list(
  id = "cox_strata", model = "cox", data = "data/cox_strata.csv", types = types,
  params = list(time = "months", event = "churned", event_value = 1, predictors = list("price", "premium"), strata = "region"),
  expect = c(cox_expect(fit, d, keys, c("price", "premium"), numeric = "price", strata_names = strata_names),
             list(txt("model|||formula", "Surv(months, churned = 1) ~ price + premium + strata(region)"))))
fit <- coxph(Surv(months, churned) ~ price + premium + strata(region), data = d, cluster = customer)
cases[[length(cases) + 1L]] <- list(
  id = "cox_robust", model = "cox", data = "data/cox_strata.csv", types = types,
  params = list(time = "months", event = "churned", event_value = 1, predictors = list("price", "premium"), strata = "region",
                se = "robust", cluster = "customer"),
  expect = c(cox_expect(fit, d, keys, c("price", "premium"), numeric = "price", strata_names = strata_names),
             list(txt("model|||formula", "Surv(months, churned = 1) ~ price + premium + strata(region) + cluster(customer)"))))
fit <- coxph(Surv(months, churned) ~ price + premium, data = d, robust = TRUE, ties = "breslow")
cases[[length(cases) + 1L]] <- list(
  id = "cox_robust_rows", model = "cox", data = "data/cox_strata.csv", types = types,
  params = list(time = "months", event = "churned", event_value = 1, predictors = list("price", "premium"), se = "robust",
                ties = "breslow"),
  expect = cox_expect(fit, d, keys, c("price", "premium"), numeric = "price"))

# hazards that are not proportional, a form that is not linear
d <- read_table("cox_nonph.csv")
d$dead <- d$dead == "true"
fit <- coxph(Surv(weeks, dead) ~ treatment + dose, data = d)
cases[[length(cases) + 1L]] <- list(
  id = "cox_nonph", model = "cox", data = "data/cox_nonph.csv",
  types = list(weeks = "number", dead = "boolean", treatment = "text", dose = "number"),
  params = list(time = "weeks", event = "dead", event_value = TRUE, predictors = list("treatment", "dose")),
  expect = c(cox_expect(fit, d, data.frame(term = c("treatment", "dose"), level = c("placebo", NA)), c("treatment", "dose"),
                        numeric = "dose"),
             list(txt("assumption|schoenfeld|treatment|verdict", "failed"), txt("assumption|martingale_form|dose|verdict", "failed"))))

# start–stop rows, errors clustered by patient
d <- read_table("cox_start_stop.csv")
fit <- coxph(Surv(start, stop, event) ~ dose + group, data = d, cluster = patient)
types <- list(patient = "text", start = "number", stop = "number", event = "integer", dose = "number", group = "text")
cases[[length(cases) + 1L]] <- list(
  id = "cox_start_stop", model = "cox", data = "data/cox_start_stop.csv", types = types,
  params = list(time = "stop", event = "event", event_value = 1, start = "start", predictors = list("dose", "group"),
                se = "robust", cluster = "patient"),
  expect = c(cox_expect(fit, d, data.frame(term = c("dose", "group"), level = c(NA, "b")), c("dose", "group"), numeric = "dose"),
             list(txt("model|||formula", "Surv(start, stop, event = 1) ~ dose + group + cluster(patient)"))))

# a start after the time: refused
cases[[length(cases) + 1L]] <- list(
  id = "cox_start_after_time", model = "cox", data = "data/cox_start_stop.csv", types = types,
  params = list(time = "start", event = "event", event_value = 1, start = "stop", predictors = list("dose")),
  expect = list(), refuses = list(code = "start_not_before_time"))

source(file.path(here, "make_reference_parametric.R"), local = TRUE)

out <- list(format_version = 1L,
            generated = list(by = "make_reference.R", R = paste(R.version$major, R.version$minor, sep = "."),
                             packages = list(survival = as.character(packageVersion("survival")),
                                             splines = as.character(packageVersion("splines")),
                                             flexsurv = as.character(packageVersion("flexsurv")),
                                             numDeriv = as.character(packageVersion("numDeriv")),
                                             jsonlite = as.character(packageVersion("jsonlite")))),
            cases = cases)
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, null = "null", na = "null", digits = NA, pretty = TRUE),
           file.path(here, "expected.json"))
cat(length(cases), "cases written,", sum(vapply(cases, function(x) length(x$expect), 0L)), "values\n")
