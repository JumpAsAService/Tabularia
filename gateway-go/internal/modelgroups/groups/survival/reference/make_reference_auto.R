# The reference values of the automatic choice of the node «Parametric survival»
# (docs/specs/statistical-models.md, «The node «Parametric survival»» → «Automatic choice» and
# «Reference values», lot P3), sourced at the end of make_reference_parametric.R, in the group's
# image: flexsurv called directly on the tables with their own column names, never the runner.
# It adds its cases to `cases` and writes its tables with `write_table` when they are missing or
# with --data. It uses the helpers of make_reference_parametric.R (`num`, `txt`, `ps_polish`,
# PS_CONTROL).
#
# Every candidate is fitted with flexsurvreg (reltol 1e-15) and brought to the maximum of its own
# log-likelihood by the Newton steps of `ps_polish`; the runner's candidates come from the BFGS
# alone (the chosen one, from its Newton steps): the chosen one is held to 1e-9, the others to what
# BFGS leaves, which the tolerances below say:
#   log-likelihood, AIC, BIC                                1e-9 (chosen), 1e-7 (the others) relative;
#   (the generalized gamma, when it is not the chosen one: 1e-4, BFGS stops short of its maximum)
#   ΔAIC, the Akaike weights                                0.05 and 0.005 absolute (derived from the AICs);
#   the choice, the marks, the statuses, the sentences      exact.
# The choice is written here by the rule of the spec, independently of the runner: the lowest AIC,
# then, if a simpler nested distribution (exponential in the Weibull and in the Gompertz, whose shape
# 0 it is; exponential, Weibull and log-normal in the generalized gamma) is within 2, the simplest.
#
# The tables (fixed seeds):
#   ps_auto           240 rows drawn from a Weibull AFT (a number, a category of three levels,
#                     uniform censoring, 161 events), the seed chosen so that the generalized gamma has
#                     the lowest AIC by 0.29 over the Weibull, and every other distribution is
#                     further than 2 away: the rule chooses the Weibull and marks it;
#   ps_auto_failing   23 rows, 10 events, no predictors: the generalized gamma does not converge from
#                     its starts (found by looking among small tables, with times rounded to 2 decimals
#                     so that the CSV holds the very numbers: no hook is needed).

make_ps_auto_data <- function() {
  gen <- function(seed) {
    set.seed(seed)
    n <- 240
    load <- round(stats::rnorm(n, 50, 10), 1)
    line <- sample(c("A", "B", "C"), n, TRUE)
    eta <- 3 + 0.02 * (load - 50) + c(A = 0, B = 0.4, C = -0.3)[line]
    t <- exp(eta) * stats::rweibull(n, shape = 1.5)
    censor_time <- stats::runif(n, 5, 60)
    data.frame(days = pmax(round(pmin(t, censor_time), 2), 0.01), status = ifelse(t <= censor_time, "failed", "running"),
               load = load, line = unname(line), stringsAsFactors = FALSE)
  }
  write_table(gen(439), "ps_auto.csv")
  set.seed(54)
  n <- sample(12:30, 1)
  t <- round(stats::rweibull(n, shape = stats::runif(1, 0.5, 3), scale = 10), 2) + 0.01
  censor_time <- round(stats::runif(n, 2, 15), 2)
  write_table(data.frame(t = round(pmin(t, censor_time), 2), e = as.integer(t <= censor_time), x = round(stats::rnorm(n), 2),
                         g = sample(c("a", "b"), n, TRUE), stringsAsFactors = FALSE), "ps_auto_failing.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "ps_auto.csv"))) make_ps_auto_data()

PS_AUTO_ORDER_REF <- c("exponential", "weibull", "lognormal", "loglogistic", "gompertz", "gengamma")
PS_AUTO_FLEXSURV <- c(exponential = "exp", weibull = "weibull", gompertz = "gompertz", loglogistic = "llogis", lognormal = "lnorm",
                      gengamma = "gengamma")
# the parameter that takes the strata's effects (flexsurv's `anc`)
PS_AUTO_ANC <- c(weibull = "shape", gompertz = "shape", loglogistic = "shape", lognormal = "sdlog", gengamma = "sigma")
PS_AUTO_LABEL <- c(exponential = "exponential", weibull = "Weibull", gompertz = "Gompertz", loglogistic = "log-logistic",
                   lognormal = "log-normal", gengamma = "generalized gamma")
PS_AUTO_NESTED_REF <- list(weibull = "exponential", gompertz = "exponential", gengamma = c("exponential", "weibull", "lognormal"))

# Every candidate by flexsurvreg on `x` (columns time, event, w and the predictors), polished; NULL
# when flexsurv itself does not reach a maximum. `rhs`: the location's formula after the «~»;
# `strata`: the column whose levels get a shape of their own, or NULL.
ps_auto_fits <- function(x, rhs, strata = NULL) {
  out <- list()
  form <- stats::as.formula(paste("Surv(time, event) ~", rhs))
  for (d in PS_AUTO_ORDER_REF) {
    anc <- if (!is.null(strata) && d != "exponential") stats::setNames(list(stats::as.formula(paste("~", strata))), PS_AUTO_ANC[[d]])
    mk <- function(...) flexsurvreg(form, anc = anc, data = x, weights = w, dist = PS_AUTO_FLEXSURV[[d]], ...)
    fit <- tryCatch(suppressWarnings(mk(control = PS_CONTROL)), error = function(e) NULL)
    if (is.null(fit) || fit$opt$convergence != 0) {
      out[[d]] <- list(converged = FALSE, k = if (is.null(fit)) NA_integer_ else length(fit$opt$par))
      next
    }
    fit <- ps_polish(fit, function(nat) mk(inits = nat, fixedpars = TRUE)$loglik)
    out[[d]] <- list(converged = TRUE, k = length(fit$opt$par), loglik = fit$loglik, fit = fit)
  }
  out
}

# The expected rows of the `candidate` section and the choice, from fits made by ps_auto_fits.
# `status`: the status of the candidates that are not in `fits` as admissible (named by
# distribution); `ks`: the parameters of each candidate (the failed ones have none from flexsurv).
ps_auto_expect <- function(fits, n, ks, status = character(), margin = 2) {
  adm <- PS_AUTO_ORDER_REF[vapply(PS_AUTO_ORDER_REF, function(d) isTRUE(fits[[d]]$converged) && is.na(status[d]), logical(1))]
  aic <- vapply(adm, function(d) -2 * fits[[d]]$loglik + 2 * fits[[d]]$k, numeric(1))
  by <- order(round(aic, 9L), vapply(adm, function(d) fits[[d]]$k, numeric(1)), seq_along(adm))
  lowest <- adm[[by[[1L]]]]
  chosen <- lowest
  near <- intersect(PS_AUTO_NESTED_REF[[lowest]], adm)
  near <- near[aic[near] - aic[[lowest]] <= margin]
  if (length(near)) chosen <- near[order(vapply(near, function(d) fits[[d]]$k, numeric(1)), aic[near])][[1L]]
  delta <- aic - min(aic)
  weight <- exp(-delta / 2) / sum(exp(-delta / 2))
  e <- list()
  for (d in PS_AUTO_ORDER_REF) {
    key <- function(stat) paste0("candidate|", d, "||", stat)
    ok <- d %in% adm
    e <- c(e, list(num(key("n_parameters"), ks[[d]]), txt(key("status"), if (ok) "ok" else status[[d]]),
                   num(key("admissible"), as.integer(ok)), num(key("chosen"), as.integer(d == chosen)),
                   num(key("parsimony"), as.integer(d == chosen && chosen != lowest)),
                   num(key("converged"), as.integer(isTRUE(fits[[d]]$converged)))))
    if (ok) {
      # the chosen fit is brought to the maximum by Newton steps; the others stop where BFGS does, which is
      # 1e-8 relative off for most and up to 2e-5 (a log-likelihood 0.012 low) for the generalized gamma
      r <- if (d == chosen) 1e-9 else if (d == "gengamma") 1e-4 else 1e-7
      e <- c(e, list(num(key("log_lik"), fits[[d]]$loglik, r), num(key("aic"), aic[[d]], r),
                     num(key("bic"), -2 * fits[[d]]$loglik + ks[[d]] * log(n), r),
                     list(key = key("delta_aic"), value = unname(delta[[d]]), rel = 1e-9, abs = 0.05),
                     list(key = key("akaike_weight"), value = unname(weight[[d]]), rel = 1e-9, abs = 0.005)))
    }
  }
  list(expect = e, margin = margin, adm = adm, aic = aic, delta = delta, weight = weight, lowest = lowest, chosen = chosen)
}

# The note of the parsimony rule, as the runner writes it (`@id name=value …`).
ps_auto_close_note <- function(a) {
  paste0("@auto_close distribution=", a$chosen, " other=", a$lowest, " margin=", format(a$margin), " delta_aic=",
         formatC(a$delta[[a$chosen]], format = "f", digits = 2L), " weight=", formatC(a$weight[[a$chosen]], format = "f", digits = 2L))
}

# ── the Weibull table where the generalized gamma has the lowest AIC: the parsimony rule ──
d <- read_table("ps_auto.csv")
d$event <- as.integer(d$status == "failed")
x <- data.frame(time = d$days, event = d$event, w = 1, load = d$load, line = factor(d$line, levels = c("A", "B", "C")))
fits <- ps_auto_fits(x, "load + line")
ks <- vapply(fits, function(f) f$k, numeric(1))
a <- ps_auto_expect(fits, nrow(x), ks)
if (!(a$lowest == "gengamma" && a$chosen == "weibull" && a$delta[["weibull"]] > 0 && a$delta[["weibull"]] < 0.5 &&
      all(a$delta[setdiff(a$adm, c("gengamma", "weibull"))] > 2))) stop("ps_auto: the table no longer has the shape the case needs")
d0 <- d
ks0 <- ks
terms_auto <- data.frame(term = c("load", "line", "line"), level = c(NA, "B", "C"), name = c("load", "lineB", "lineC"))
wb <- fits$weibull$fit
V <- attr(wb, "V")
e <- c(a$expect,
       list(txt("model|||distribution", "weibull"), txt("model|||selection", "auto"), txt("model|||variant", "auto"),
            num("model|||log_lik", fits$weibull$loglik), num("model|||n_parameters", ks[["weibull"]]),
            num("model|||aic", -2 * fits$weibull$loglik + 2 * ks[["weibull"]]), num("model|||n", nrow(x)), num("model|||aic_margin", 2),
            est_num("coef|load||estimate", wb$opt$par[["load"]], sqrt(V["load", "load"])),
            est_num("coef|line|B|estimate", wb$opt$par[["lineB"]], sqrt(V["lineB", "lineB"])),
            txt("model|||note_auto_close", ps_auto_close_note(a))))
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto_parsimony", model = "parametric_survival", data = "data/ps_auto.csv",
  types = list(days = "number", status = "text", load = "number", line = "text"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), distribution = "auto"),
  expect = e)

# ── the 240 rows of ps_weibull, case weights, rows with a NULL, a time of 0: the lowest AIC chosen ──
d <- read_table("ps_weibull.csv")
d$event <- as.integer(d$status == "failed")
used <- complete.cases(d[, c("days", "status", "load", "line", "w")]) & d$days > 0
d <- d[used, ]
x <- data.frame(time = d$days, event = d$event, w = d$w, load = d$load, line = factor(d$line, levels = c("A", "B", "C")),
                plant = factor(d$plant, levels = c("P", "Q", "R")))
fits <- ps_auto_fits(x, "load + line")
ks <- vapply(fits, function(f) f$k, numeric(1))
a <- ps_auto_expect(fits, nrow(x), ks)
e <- c(a$expect, list(txt("model|||distribution", a$chosen), txt("model|||selection", "auto"),
                      num("model|||log_lik", fits[[a$chosen]]$loglik)))
if (a$chosen == a$lowest) e <- c(e, list(num("model|||n", nrow(x))))
if (a$chosen != a$lowest) e <- c(e, list(txt("model|||note_auto_close", ps_auto_close_note(a))))
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto", model = "parametric_survival", data = "data/ps_weibull.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", plant = "text", w = "integer"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), weights = "w",
                distribution = "auto"),
  expect = e)
cat("ps_auto (the 240 rows of ps_weibull):", a$chosen, "chosen, lowest", a$lowest, "\n")

# the same rows with a shape per plant: the parameters of each candidate count the strata's
fits <- ps_auto_fits(x, "load + line + plant", strata = "plant")
ks <- vapply(fits, function(f) f$k, numeric(1))
a <- ps_auto_expect(fits, nrow(x), ks)
e <- c(a$expect, list(txt("model|||distribution", a$chosen), txt("model|||selection", "auto"),
                      num("model|||log_lik", fits[[a$chosen]]$loglik)))
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto_strata", model = "parametric_survival", data = "data/ps_weibull.csv",
  types = list(days = "number", status = "text", load = "number", line = "text", plant = "text", w = "integer"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), strata = "plant",
                weights = "w", distribution = "auto"),
  expect = e)
cat("ps_auto_strata:", a$chosen, "chosen, lowest", a$lowest, "\n")

# ── a table where the generalized gamma does not converge ──
d <- read_table("ps_auto_failing.csv")
x <- data.frame(time = d$t, event = d$e, w = 1)
fits <- ps_auto_fits(x, "1")
if (isTRUE(fits$gengamma$converged)) stop("ps_auto_failing: flexsurv itself converges now, the table must be chosen again")
ks <- c(exponential = 1, weibull = 2, gompertz = 2, loglogistic = 2, lognormal = 2, gengamma = 3)
a <- ps_auto_expect(fits, nrow(x), ks, status = c(gengamma = "not_converged"))
if (!all(setdiff(PS_AUTO_ORDER_REF, "gengamma") %in% a$adm)) stop("ps_auto_failing: another candidate failed too")
e <- c(a$expect, list(txt("model|||distribution", a$chosen), num("model|||log_lik", fits[[a$chosen]]$loglik),
                      txt("model|||note_auto_not_converged", "@auto_not_converged names=gengamma")))
if (a$chosen != a$lowest) e <- c(e, list(txt("model|||note_auto_close", ps_auto_close_note(a))))
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto_failing", model = "parametric_survival", data = "data/ps_auto_failing.csv",
  types = list(t = "number", e = "integer", x = "number", g = "text"),
  params = list(time = "t", event = "e", event_value = 1, distribution = "auto"), expect = e)
cat("ps_auto_failing:", a$chosen, "chosen, lowest", a$lowest, ", weights", paste(round(a$weight, 4), collapse = " "), "\n")

# the same table, the generalized gamma asked for by itself: both starts fail, the refusal
cases[[length(cases) + 1L]] <- list(
  id = "ps_not_converged", model = "parametric_survival", data = "data/ps_auto_failing.csv",
  types = list(t = "number", e = "integer", x = "number", g = "text"),
  params = list(time = "t", event = "e", event_value = 1, distribution = "gengamma"), expect = list(),
  refuses = list(code = "not_converged", message = paste0("the fit of the generalized gamma distribution did not converge from two ",
                                                          "starting points: fewer predictors, merge rare levels, or another distribution")))

# only the exponential has the rows it needs (8 rows, a category of 6 levels: 6 parameters; the
# Weibull needs 9 rows): the other five are listed with the status too_few_rows
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto_too_few_rows", model = "parametric_survival", data = "data/ps_small.csv", types = small_types,
  params = list(time = "t", event = "e", event_value = 1, predictors = list("kind"), distribution = "auto"),
  expect = list(txt("candidate|weibull||status", "too_few_rows"), txt("candidate|gengamma||status", "too_few_rows"),
                num("candidate|exponential||n_parameters", 6), num("candidate|weibull||admissible", 0),
                num("candidate|weibull||n_parameters", 7), num("candidate|gengamma||n_parameters", 8)))

# the same table with a margin of 0.1 (the option `aic_margin`): the Weibull is 0.29 above, so the
# generalized gamma is chosen, nothing is marked and no other candidate is close
a <- ps_auto_expect(ps_auto_fits(data.frame(time = d0$days, event = d0$event, w = 1, load = d0$load, line = factor(d0$line, levels = c("A", "B", "C"))),
                                 "load + line"), nrow(d0), ks0, margin = 0.1)
if (a$chosen != "gengamma") stop("ps_auto_margin: the margin of 0.1 should leave the generalized gamma")
cases[[length(cases) + 1L]] <- list(
  id = "ps_auto_margin", model = "parametric_survival", data = "data/ps_auto.csv",
  types = list(days = "number", status = "text", load = "number", line = "text"),
  params = list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"), distribution = "auto",
                aic_margin = 0.1),
  expect = list(txt("model|||distribution", "gengamma"), num("model|||aic_margin", 0.1), num("candidate|gengamma||chosen", 1),
                num("candidate|gengamma||parsimony", 0), num("candidate|weibull||chosen", 0),
                num("candidate|gengamma||log_lik", a$expect[[which(vapply(a$expect, function(z) identical(z$key, "candidate|gengamma||log_lik"), NA))]]$value)))
