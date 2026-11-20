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
# past the follow-up), kaplan_meier_no_events (refused). Cox (lane M5) adds its cases below.

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
  expect = c(km_expect(x, "line", "log-log", 0.1, 1, 60, max(x$time), FALSE, sum(!ok)), list(txt("model|||test", "peto"))))

# two strata whose curves cross; the event a 0/1 number
d <- read_table("kaplan_meier_crossing.csv")
x <- data.frame(time = d$months, status = d$churned, plan = d$plan)
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_crossing", model = "kaplan_meier", data = "data/kaplan_meier_crossing.csv",
  types = list(months = "number", churned = "integer", plan = "text"),
  params = list(time = "months", event = "churned", event_value = 1, strata = "plan", times = as.list(c(6, 24))),
  expect = c(km_expect(x, "plan", "log", 0.05, 0, c(6, 24), max(x$time), FALSE, 0),
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

# no row is the event: refused
cases[[length(cases) + 1L]] <- list(
  id = "kaplan_meier_no_events", model = "kaplan_meier", data = "data/kaplan_meier_single.csv",
  types = list(hours = "number", broke = "boolean"),
  params = list(time = "hours", event = "broke", event_value = "maybe"),
  expect = list(), refuses = list(code = "no_events"))

out <- list(format_version = 1L,
            generated = list(by = "make_reference.R", R = paste(R.version$major, R.version$minor, sep = "."),
                             packages = list(survival = as.character(packageVersion("survival")),
                                             jsonlite = as.character(packageVersion("jsonlite")))),
            cases = cases)
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, null = "null", na = "null", digits = NA, pretty = TRUE),
           file.path(here, "expected.json"))
cat(length(cases), "cases written,", sum(vapply(cases, function(x) length(x$expect), 0L)), "values\n")
