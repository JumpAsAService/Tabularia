# The reference values of the group «regression», computed OUTSIDE Tabularia
# (docs/specs/statistical-models.md, «Reference values»): by hand, with R and the pinned
# packages of ../recipe.json plus the witnesses of witness.json, never by the runner. Nothing
# here sources the runner or the common library: every number comes from the packages called
# directly on the CSV tables (lm, sandwich, lmtest, nortest), or from a second construction
# of the same quantity (the variance inflation factors from the regressors' correlations, not
# from the coefficients' as car::vif and the runner do).
#
#   Rscript gateway-go/internal/modelgroups/groups/regression/reference/make_reference.R
#
# The packages the group's image holds are enough; without R and these packages on the
# machine, run it in the group's image with this folder mounted writable:
#
#   docker run --rm --memory=2g -v "$PWD/gateway-go/internal/modelgroups/groups/regression/reference:/ref" \
#     -w /ref tabularia-models-regression:dev Rscript make_reference.R
#
# It writes the small tables of data/ when they are missing (synthetic, fixed seeds; a table
# once written is the data, the seeds only made it), then expected.json in the format
# common/R/selftest.R reads: {"format_version": 1, "generated": {"by", "R", "packages"},
# "cases": [{"id", "model", "data", "types", "params", "expect": [{"key", "value" | "text",
# "rel", "abs"}], "refuses"}]}.
#
# Witnesses not used, and why (said, not hidden): `car` is not installed (it pulls lme4 and a
# long compile, witness.json): the VIFs are checked against Fox and Monette's formula on the
# regressors' correlation matrix and, for a term of one column, against 1/(1 − R²) of the
# regression of that column on the others. `statsmodels` (the second witness, 1e-6) is not
# available on the machine that wrote these values: no witness file yet. `ResourceSelection`
# and `pROC` are not installed either: the logistic cases compute Hosmer–Lemeshow and DeLong's
# interval by a second construction (see «the logistic regression» below).
#
# Lane M1 writes the cases of the linear regression; the lanes of the logistic regression and
# of the penalties add theirs below, each in a function of its own.

suppressPackageStartupMessages({
  library(stats)
  library(sandwich)
  library(lmtest)
  library(nortest)
  library(brglm2)
  library(detectseparation)
  library(glmnet)
  library(jsonlite)
})

here <- local({
  file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  if (length(file) == 1L) dirname(normalizePath(file)) else normalizePath(".")
})
data_dir <- file.path(here, "data")
dir.create(data_dir, showWarnings = FALSE)

write_table <- function(df, name) {
  path <- file.path(data_dir, name)
  if (!file.exists(path)) utils::write.csv(df, path, row.names = FALSE, na = "", fileEncoding = "UTF-8")
  utils::read.csv(path, check.names = FALSE, na.strings = "", encoding = "UTF-8", stringsAsFactors = FALSE)
}

# ── the tables ───────────────────────────────────────────────────────────────
# 120 days of a shop: quantity sold against price and channel, the noise growing with the square
# of the price (heteroscedastic), correlated from one day to the next, with a store effect (clusters); weights
# 1/price² (the inverse of the noise's variance); two outcomes and a channel left empty.
heteroscedastic <- function() {
  set.seed(20261120)
  n <- 120
  price <- round(stats::runif(n, 1, 4), 2)
  channel <- sample(c("shop", "web"), n, TRUE)
  store <- sprintf("s%d", sample(1:8, n, TRUE))
  store_effect <- stats::rnorm(8, 0, 1)
  u <- stats::rnorm(n)
  e <- numeric(n)
  for (t in seq_len(n)) e[t] <- (if (t > 1) 0.35 * e[t - 1] else 0) + u[t]
  web <- channel == "web"
  quantity <- round(60 - 7 * price + 5 * web - 1.8 * price * web + store_effect[as.integer(sub("s", "", store))] +
                      0.5 * price^2 * e, 3)
  quantity[c(7, 58)] <- NA
  channel[33] <- NA
  data.frame(day = seq_len(n), price = price, channel = channel, store = store, weight = round(1 / price^2, 6),
             quantity = quantity)
}

# 60 sales of three regions, with a price, a month and a promotion (more often when the price
# is low: correlated predictors), and the price again in cents (an aliased column).
categorical <- function() {
  set.seed(20261121)
  n <- 60
  region <- sample(c("north", "south", "east"), n, TRUE)
  price <- round(stats::runif(n, 2, 6), 2)
  month <- sample(1:12, n, TRUE)
  promo <- ifelse(stats::runif(n) < stats::plogis(3 - price), "true", "false")
  effect <- c(north = 0, south = 15, east = -10)[region]
  sales <- round(200 - 12 * price + effect + 3 * price * (region == "south") + 1.5 * month + 20 * (promo == "true") +
                   stats::rnorm(n, 0, 8), 2)
  data.frame(region = region, price = price, price_cents = round(price * 100), month = month, promo = promo, sales = sales)
}

# 30 rows whose column names look like code: they must come back as labels.
code_names <- function() {
  set.seed(20261122)
  n <- 30
  a <- round(stats::rnorm(n, 10, 2), 3)
  g <- sample(c("east", "north", "south"), n, TRUE)
  b <- sample(c("true", "false"), n, TRUE)
  y <- round(3 + 0.8 * a + c(east = 0, north = 2, south = -1)[g] + 1.5 * (b == "true") + stats::rnorm(n), 3)
  df <- data.frame(y = y, a = a, g = g, b = b)
  names(df) <- c("y", "a`b", "x); system(\"id\")", "ü ñ")
  df
}

# 4 rows for 4 terms: too few.
tiny <- function() data.frame(y = c(1.5, 2.1, 2.9, 4.2), x1 = c(1, 2, 3, 4), x2 = c(0.5, 0.1, 0.9, 0.3), g = c("a", "b", "a", "b"))

# ── the linear regression, by the packages ───────────────────────────────────
num <- function(key, value, rel = 1e-9) list(key = key, value = if (is.finite(value)) unname(value) else NULL, rel = rel)
txt <- function(key, text) list(key = key, text = text)
stars <- function(p) if (p < 0.001) "***" else if (p < 0.01) "**" else if (p < 0.05) "*" else if (p < 0.1) "." else ""

# `map`: lm's name of each coefficient → c(term, level) as the results name them.
coef_key <- function(map, name, stat) {
  t <- map[[name]]
  paste0("coef|", t[[1]], "|", if (is.na(t[[2]])) "" else t[[2]], "|", stat)
}

# GVIF of each term, from the regressors' correlations (weighted when the fit is): Fox and
# Monette, det(R11)·det(R22)/det(R); a term of one column also as 1/(1 − R²).
vif_by_correlation <- function(m, w = NULL) {
  X <- stats::model.matrix(m)
  assign <- attr(X, "assign")
  X <- X[, assign > 0, drop = FALSE]
  assign <- assign[assign > 0]
  R <- if (is.null(w)) stats::cor(X) else stats::cov.wt(X, wt = w / sum(w), cor = TRUE)$cor
  out <- list()
  for (a in unique(assign)) {
    sub <- which(assign == a)
    gvif <- det(R[sub, sub, drop = FALSE]) * det(R[-sub, -sub, drop = FALSE]) / det(R)
    if (length(sub) == 1L) {
      r2 <- summary(stats::lm(X[, sub] ~ X[, -sub], weights = w))$r.squared
      stopifnot(abs(gvif - 1 / (1 - r2)) < 1e-9 * gvif)
    }
    out[[attr(stats::terms(m), "term.labels")[[a]]]] <- list(gvif = gvif, df = length(sub))
  }
  out
}

linear_case <- function(id, data, file, types, params, formula, map, term_names, row_numbers, outcome, weights = NULL,
                        order_by = NULL, cluster = NULL, full = TRUE, n_missing = 0) {
  alpha <- params$alpha %||% 0.05
  se <- params$se %||% "classical"
  w <- if (is.null(weights)) NULL else data[[weights]]
  data$.w <- if (is.null(w)) 1 else w  # lm reads its weights in the data
  m <- if (is.null(w)) stats::lm(formula, data = data) else stats::lm(formula, data = data, weights = .w)
  s <- summary(m)
  V <- switch(se, classical = stats::vcov(m), hc0 = sandwich::vcovHC(m, type = "HC0"), hc1 = sandwich::vcovHC(m, type = "HC1"),
              hc2 = sandwich::vcovHC(m, type = "HC2"), hc3 = sandwich::vcovHC(m, type = "HC3"),
              hac = sandwich::NeweyWest(m, order.by = data[[order_by]]),
              cluster = sandwich::vcovCL(m, cluster = data[[cluster]]))
  ct <- lmtest::coeftest(m, vcov. = V)
  ci <- lmtest::coefci(m, vcov. = V, level = 1 - alpha)
  expect <- list(txt("model|||variant", "unpenalized"), num("model|||n", nrow(data)), num("model|||n_missing", n_missing))
  for (name in rownames(ct)) {
    expect <- c(expect, list(
      num(coef_key(map, name, "estimate"), ct[name, 1]), num(coef_key(map, name, "std_error"), ct[name, 2]),
      num(coef_key(map, name, "statistic"), ct[name, 3]), num(coef_key(map, name, "p_value"), ct[name, 4]),
      txt(coef_key(map, name, "significance"), stars(ct[name, 4])),
      num(coef_key(map, name, "ci_low"), ci[name, 1]), num(coef_key(map, name, "ci_high"), ci[name, 2])))
  }
  if (se == "classical") {
    f <- s$fstatistic
  } else {
    null <- if (is.null(w)) stats::lm(stats::update(formula, . ~ 1), data = data) else
      stats::lm(stats::update(formula, . ~ 1), data = data, weights = .w)
    wt <- lmtest::waldtest(null, m, vcov = V, test = "F")
    f <- c(value = wt$F[[2]], numdf = abs(wt$Df[[2]]), dendf = wt$Res.Df[[2]])
  }
  expect <- c(expect, list(num("model|||f_statistic", f[["value"]]), num("model|||f_df_1", f[["numdf"]]),
                           num("model|||f_df_2", f[["dendf"]]),
                           num("model|||f_p_value", stats::pf(f[["value"]], f[["numdf"]], f[["dendf"]], lower.tail = FALSE))))
  if (!full) return(list(id = id, model = "linear", data = file, types = types, params = params, expect = expect))

  e <- data[[outcome]] - stats::fitted(m)
  Xw <- stats::model.matrix(m) * if (is.null(w)) 1 else sqrt(w)
  scaled <- sweep(Xw, 2, sqrt(colSums(Xw^2)), "/")
  kappa_ <- kappa(scaled, exact = TRUE)
  expect <- c(expect, list(
    num("model|||df_model", length(stats::coef(m)) - attr(stats::terms(m), "intercept")),
    num("model|||df_residual", m$df.residual), num("model|||r_squared", s$r.squared),
    num("model|||adj_r_squared", s$adj.r.squared), num("model|||sigma", s$sigma),
    num("model|||log_lik", as.numeric(stats::logLik(m))), num("model|||aic", stats::AIC(m)), num("model|||bic", stats::BIC(m)),
    num("model|||rmse", sqrt(mean(e^2))), num("model|||mae", mean(abs(e))), num("model|||condition_number", kappa_),
    txt("model|||se_type", se), txt("model|||family", "gaussian")))

  test <- function(check, t, df = TRUE) {
    out <- list(num(paste0("assumption|", check, "||statistic"), unname(t$statistic)),
                num(paste0("assumption|", check, "||p_value"), t$p.value),
                txt(paste0("assumption|", check, "||verdict"), if (t$p.value < alpha) "failed" else "ok"))
    if (df) out <- c(out, list(num(paste0("assumption|", check, "||df"), unname(t$parameter[[1]]))))
    out
  }
  # RESET: lmtest's on an unweighted fit; the F test of the nested weighted fits otherwise
  if (is.null(w)) {
    reset <- lmtest::resettest(m, power = 2:3, type = "fitted")
  } else {
    d2 <- data
    d2$f2 <- stats::fitted(m)^2
    d2$f3 <- stats::fitted(m)^3
    big <- stats::lm(stats::update(formula, . ~ . + f2 + f3), data = d2, weights = .w)
    a <- stats::anova(m, big)
    reset <- list(statistic = a$F[[2]], parameter = c(a$Df[[2]], a$Res.Df[[2]]), p.value = a$`Pr(>F)`[[2]])
  }
  expect <- c(expect, test("reset", reset), list(num("assumption|reset||df_2", reset$parameter[[2]])))
  expect <- c(expect, test("breusch_pagan", lmtest::bptest(m)))
  fitted_frame <- data.frame(fitted = stats::fitted(m), row.names = names(stats::fitted(m)))
  expect <- c(expect, test("white", lmtest::bptest(m, ~ fitted + I(fitted^2), data = fitted_frame)))
  r <- if (is.null(w)) stats::residuals(m) else sqrt(w) * stats::residuals(m)
  expect <- c(expect, test("shapiro_wilk", stats::shapiro.test(r), df = FALSE))
  d <- r - mean(r)
  jb <- length(r) * (mean(d^3)^2 / mean(d^2)^3) / 6 + length(r) * (mean(d^4) / mean(d^2)^2 - 3)^2 / 24
  expect <- c(expect, test("jarque_bera", list(statistic = jb, parameter = 2, p.value = stats::pchisq(jb, 2, lower.tail = FALSE))))
  if (is.null(order_by)) {
    for (check in c("durbin_watson", "breusch_godfrey_1", "breusch_godfrey_4")) {
      expect <- c(expect, list(txt(paste0("assumption|", check, "||verdict"), "not_computed")))
    }
  } else {
    z <- data[[order_by]]
    mt <- if (is.null(w)) m else {
      ys <- sqrt(w) * data[[outcome]]
      Xs <- stats::model.matrix(m) * sqrt(w)
      stats::lm(ys ~ 0 + Xs)
    }
    expect <- c(expect, test("durbin_watson", lmtest::dwtest(mt, order.by = z), df = FALSE),
                test("breusch_godfrey_1", lmtest::bgtest(mt, order = 1, order.by = z)),
                test("breusch_godfrey_4", lmtest::bgtest(mt, order = 4, order.by = z)))
  }
  if (attr(stats::terms(m), "intercept") == 1) {
    for (term in names(vif <- vif_by_correlation(m, w))) {
      v <- vif[[term]]
      label <- term_names[[term]]
      value <- v$gvif^(1 / v$df)
      expect <- c(expect, list(num(paste0("assumption|vif|", label, "|value"), value),
                               txt(paste0("assumption|vif|", label, "|verdict"),
                                   if (value > 10) "failed" else if (value > 5) "doubtful" else "ok")))
      if (v$df > 1) expect <- c(expect, list(num(paste0("assumption|vif|", label, "|statistic"), v$gvif)))
      first <- names(map)[vapply(map, function(t) identical(t[[1]], label), TRUE)][[1]]
      expect <- c(expect, list(num(coef_key(map, first, "vif"), value)))
    }
  }
  n <- nrow(data)
  cooks <- stats::cooks.distance(m)
  h <- stats::hatvalues(m)
  t <- stats::rstudent(m)
  named <- function(score, above) {
    top <- head(order(-score), 5L)
    paste(row_numbers[top[score[top] > above]], collapse = ", ")
  }
  i <- which.max(abs(t))
  p_bonf <- min(1, n * 2 * stats::pt(abs(t[[i]]), m$df.residual - 1, lower.tail = FALSE))
  expect <- c(expect, list(
    num("assumption|cooks_distance||count", sum(cooks > 4 / n)), num("assumption|cooks_distance||threshold", 4 / n),
    num("assumption|leverage||count", sum(h > 2 * length(stats::coef(m)) / n)),
    num("assumption|leverage||threshold", 2 * length(stats::coef(m)) / n),
    num("assumption|outlier_test||statistic", unname(t[[i]])), num("assumption|outlier_test||p_value", p_bonf),
    num("assumption|outlier_test||df", m$df.residual - 1),
    txt("assumption|outlier_test||rows", as.character(row_numbers[[i]]))))
  if (sum(cooks > 4 / n) > 0) expect <- c(expect, list(txt("assumption|cooks_distance||rows", named(cooks, 4 / n))))
  if (sum(h > 2 * length(stats::coef(m)) / n) > 0) {
    expect <- c(expect, list(txt("assumption|leverage||rows", named(h, 2 * length(stats::coef(m)) / n))))
  }
  list(id = id, model = "linear", data = file, types = types, params = params, expect = expect)
}

`%||%` <- function(a, b) if (is.null(a)) b else a

linear_cases <- function() {
  cases <- list()
  # the heteroscedastic table: rows with an empty value in a column the model uses left out
  het <- write_table(heteroscedastic(), "linear_heteroscedastic.csv")
  het_types <- list(day = "integer", price = "number", channel = "text", store = "text", weight = "number", quantity = "number")
  used <- stats::complete.cases(het[, c("quantity", "price", "channel")])
  h <- het[used, ]
  rownames(h) <- NULL
  h$channel <- factor(h$channel, levels = c("shop", "web"))
  rows_h <- which(used)
  map_h <- list("(Intercept)" = c("(Intercept)", NA), price = c("price", NA), channelweb = c("channel", "web"),
                "price:channelweb" = c("price × channel", "web"))
  names_h <- list(price = "price", channel = "channel", "price:channel" = "price × channel")
  base <- list(outcome = "quantity", predictors = list("price", "channel"), interactions = list(list("price", "channel")))
  f_h <- quantity ~ price + channel + price:channel
  one <- function(id, params, full = TRUE, ...) {
    linear_case(id, h, "data/linear_heteroscedastic.csv", het_types, c(base, params), f_h, map_h, names_h, rows_h,
                "quantity", full = full, n_missing = sum(!used), ...)
  }
  cases[[length(cases) + 1L]] <- one("linear_ols", list(order_by = "day"), order_by = "day")
  cases[[length(cases) + 1L]] <- one("linear_ols_unordered", list())
  for (type in c("hc0", "hc1", "hc2")) cases[[length(cases) + 1L]] <- one(paste0("linear_", type), list(se = type), full = FALSE)
  cases[[length(cases) + 1L]] <- one("linear_hc3", list(se = "hc3", order_by = "day"), order_by = "day")
  cases[[length(cases) + 1L]] <- one("linear_hac", list(se = "hac", order_by = "day"), full = FALSE, order_by = "day")
  cases[[length(cases) + 1L]] <- one("linear_cluster", list(se = "cluster", cluster = "store"), full = FALSE, cluster = "store")
  hw <- h[stats::complete.cases(h$weight), ]
  cases[[length(cases) + 1L]] <- linear_case("linear_wls", hw, "data/linear_heteroscedastic.csv", het_types,
                                             c(base, list(estimation = "wls", weights = "weight", order_by = "day")), f_h, map_h,
                                             names_h, rows_h, "quantity", weights = "weight", order_by = "day", n_missing = sum(!used))
  cases[[length(cases) + 1L]] <- linear_case("linear_wls_hc3", hw, "data/linear_heteroscedastic.csv", het_types,
                                             c(base, list(estimation = "wls", weights = "weight", se = "hc3")), f_h, map_h,
                                             names_h, rows_h, "quantity", weights = "weight", full = FALSE, n_missing = sum(!used))

  # the categorical table: three regions, the reference given, an interaction, a boolean
  cat_ <- write_table(categorical(), "linear_categorical.csv")
  cat_types <- list(region = "text", price = "number", price_cents = "number", month = "integer", promo = "boolean",
                    sales = "number")
  c2 <- cat_
  c2$region <- factor(c2$region, levels = c("north", "east", "south"))
  c2$promo <- factor(tolower(as.character(c2$promo)), levels = c("false", "true"))
  map_c <- list("(Intercept)" = c("(Intercept)", NA), price = c("price", NA), regioneast = c("region", "east"),
                regionsouth = c("region", "south"), month = c("month", NA), promotrue = c("promo", "true"),
                "price:regioneast" = c("price × region", "east"), "price:regionsouth" = c("price × region", "south"))
  names_c <- list(price = "price", region = "region", month = "month", promo = "promo", "price:region" = "price × region")
  cases[[length(cases) + 1L]] <- linear_case(
    "linear_categorical", c2, "data/linear_categorical.csv", cat_types,
    list(outcome = "sales", predictors = list("price", "region", "month", "promo"), reference = list(region = "north"),
         interactions = list(list("price", "region"))),
    sales ~ price + region + month + promo + price:region, map_c, names_c, seq_len(nrow(c2)), "sales")
  cases[[length(cases) + 1L]] <- linear_case(
    "linear_no_intercept", c2, "data/linear_categorical.csv", cat_types,
    list(outcome = "sales", predictors = list("price", "month"), intercept = FALSE),
    sales ~ 0 + price + month, list(price = c("price", NA), month = c("month", NA)), list(), seq_len(nrow(c2)), "sales")

  # names that look like code, renamed here to plain ones for lm and named back in the keys
  odd <- write_table(code_names(), "linear_names.csv")
  odd_types <- stats::setNames(list("number", "number", "text", "boolean"), names(odd))
  plain <- odd
  names(plain) <- c("y", "a", "g", "b")
  plain$g <- factor(plain$g, levels = c("east", "north", "south"))
  plain$b <- factor(tolower(as.character(plain$b)), levels = c("false", "true"))
  A <- "a`b"
  G <- "x); system(\"id\")"
  B <- "ü ñ"
  map_o <- list("(Intercept)" = c("(Intercept)", NA), a = c(A, NA), gnorth = c(G, "north"), gsouth = c(G, "south"),
                btrue = c(B, "true"))
  case <- linear_case("linear_names", plain, "data/linear_names.csv", odd_types,
                      list(outcome = "y", predictors = list(A, G, B)), y ~ a + g + b, map_o, list(a = A, g = G, b = B),
                      seq_len(nrow(plain)), "y")
  case$expect <- c(case$expect, list(txt("model|||formula", paste0("y ~ ", A, " + ", G, " + ", B))))
  cases[[length(cases) + 1L]] <- case

  # refusals, with their sentences
  write_table(tiny(), "linear_tiny.csv")
  refusal <- function(id, file, types, params, code, message) {
    list(id = id, model = "linear", data = file, types = types, params = params, expect = list(),
         refuses = list(code = code, message = message))
  }
  cases[[length(cases) + 1L]] <- refusal(
    "linear_refuses_aliased", "data/linear_categorical.csv", cat_types,
    list(outcome = "sales", predictors = list("price", "price_cents")), "aliased",
    "the term «price_cents» is a combination of the others (the design is singular): drop it, or a predictor that repeats it")
  cases[[length(cases) + 1L]] <- refusal(
    "linear_refuses_text_outcome", "data/linear_categorical.csv", cat_types,
    list(outcome = "region", predictors = list("price")), "outcome_not_number",
    "the outcome «region» is not a number: a linear regression explains a number (for two values use the logistic regression)")
  cases[[length(cases) + 1L]] <- refusal(
    "linear_refuses_few_rows", "data/linear_tiny.csv", list(y = "number", x1 = "number", x2 = "number", g = "text"),
    list(outcome = "y", predictors = list("x1", "x2", "g")), "too_few_rows",
    "the model has 4 terms and 4 rows it can use: it needs at least 6 rows (more rows than terms + 1)")
  cases
}

# ── the logistic regression (lane M2) ────────────────────────────────────────
# By the packages called directly: glm, brglm2::brglmFit, detectseparation, sandwich, lmtest,
# stats::confint (profile). The witnesses of the specification for the runner's own pieces,
# ResourceSelection::hoslem.test and pROC's DeLong interval, are not installed in the image
# (witness.json): the reference computes them by a second construction, on the rows repeated
# as many times as their weight (so a weighted case checks the frequency weights too):
#   - Hosmer–Lemeshow: hoslem.test's own steps (unique deciles of the predicted risk, cut with
#     the lowest included, xtabs of observed and expected) on those rows, df = groups − 2;
#   - the AUC and DeLong's variance: the pairwise kernel of every case against every non-case
#     (an n1 × n0 matrix), its row and column means the placement values, var/n1 + var/n0;
#   - the VIFs: Fox and Monette's formula on the regressors' correlations weighted by the
#     fit's working weights (not on the coefficients' as the runner computes them);
#   - McFadden's and Nagelkerke's R² from the log-likelihood of the intercept-only model
#     fitted again (not from the deviances); Tjur's R², the Brier score and the rates from
#     the repeated rows.

# 200 customers of a subscription: churned (yes or no) against tenure in months (its effect is
# log-linear in the logit: Box–Tidwell sees it), the monthly fee, the change of the fee (numbers
# around 0: no Box–Tidwell), the plan, a store effect (clusters), a frequency weight; a tenure,
# a plan and an outcome left empty.
churn <- function() {
  set.seed(20261123)
  n <- 200
  tenure <- round(stats::runif(n, 1, 72))
  fee <- round(stats::runif(n, 15, 120), 2)
  change <- round(stats::rnorm(n, 0, 5), 1)
  plan <- sample(c("basic", "plus", "premium"), n, TRUE, prob = c(0.5, 0.3, 0.2))
  store <- sprintf("s%02d", sample(1:10, n, TRUE))
  store_effect <- stats::rnorm(10, 0, 0.5)
  eta <- 0.8 - 0.9 * log(tenure) + 0.025 * fee + 0.05 * change + c(basic = 0, plus = -0.5, premium = -1.1)[plan] +
    store_effect[as.integer(sub("s", "", store))]
  churned <- ifelse(stats::runif(n) < stats::plogis(eta), "yes", "no")
  weight <- sample(1:3, n, TRUE)
  tenure[15] <- NA
  plan[88] <- NA
  churned[150] <- NA
  data.frame(customer = seq_len(n), tenure = tenure, fee = fee, change = change, plan = plan, store = store, weight = weight,
             churned = churned)
}

# 40 patients: above a dose of 5 every one is sick, below none (complete separation), and a
# group that explains nothing.
separated <- function() {
  set.seed(20261124)
  n <- 40
  dose <- round(stats::runif(n, 0, 10), 1)
  group <- sample(c("a", "b"), n, TRUE)
  data.frame(dose = dose, group = group, status = ifelse(dose > 5, "sick", "well"))
}

logistic_case <- function(id, data, file, types, params, formula, map, term_names, row_numbers, event, outcome,
                          weights = NULL, cluster = NULL, full = TRUE, n_missing = 0) {
  alpha <- params$alpha %||% 0.05
  se <- params$se %||% "classical"
  link <- params$link %||% "logit"
  firth <- identical(params$estimation, "firth")
  threshold <- params$threshold %||% 0.5
  data$.y <- as.numeric(tolower(as.character(data[[outcome]])) == tolower(event))
  data$.w <- if (is.null(weights)) 1 else data[[weights]]
  formula <- stats::update(formula, .y ~ .)
  fam <- stats::binomial(link)
  fit <- function(f, d) if (firth) stats::glm(f, family = fam, data = d, weights = .w, method = brglm2::brglmFit, type = "AS_mean") else
    stats::glm(f, family = fam, data = d, weights = .w)
  m <- fit(formula, data)
  V <- switch(se, classical = stats::vcov(m), hc0 = sandwich::vcovHC(m, type = "HC0"), hc1 = sandwich::vcovHC(m, type = "HC1"),
              hc2 = sandwich::vcovHC(m, type = "HC2"), hc3 = sandwich::vcovHC(m, type = "HC3"),
              cluster = sandwich::vcovCL(m, cluster = data[[cluster]]))
  ct <- if (se == "classical") summary(m)$coefficients else lmtest::coeftest(m, vcov. = V)
  z <- stats::qnorm(1 - alpha / 2)
  ci <- if (identical(params$ci, "profile") && !firth) suppressMessages(stats::confint(m, level = 1 - alpha)) else
    cbind(ct[, 1] - z * ct[, 2], ct[, 1] + z * ct[, 2])
  w <- data$.w
  n <- sum(w)
  events <- sum(w * data$.y)
  expect <- list(txt("model|||variant", "unpenalized"), num("model|||n", n), num("model|||events", events),
                 num("model|||n_missing", n_missing), txt("model|||event_value", event))
  for (name in rownames(ct)) {
    expect <- c(expect, list(
      num(coef_key(map, name, "estimate"), ct[name, 1]), num(coef_key(map, name, "std_error"), ct[name, 2]),
      num(coef_key(map, name, "statistic"), ct[name, 3]), num(coef_key(map, name, "p_value"), ct[name, 4]),
      txt(coef_key(map, name, "significance"), stars(ct[name, 4])),
      num(coef_key(map, name, "ci_low"), ci[name, 1]), num(coef_key(map, name, "ci_high"), ci[name, 2])))
    if (link == "logit") {
      expect <- c(expect, list(num(coef_key(map, name, "odds_ratio"), exp(ct[name, 1])),
                               num(coef_key(map, name, "or_ci_low"), exp(ci[name, 1])),
                               num(coef_key(map, name, "or_ci_high"), exp(ci[name, 2]))))
    }
  }
  if (!full) return(list(id = id, model = "logistic", data = file, types = types, params = params, expect = expect))

  # the fit's numbers; the classification's on the rows repeated as many times as their weight
  # the intercept-only model fitted again; with Firth's, brglm2's own null deviance (its null fit
  # converges to brglm2's tolerance, 1e-6 on the estimate: fitted again it differs by 1e-10)
  m0 <- fit(.y ~ 1, data)
  ll <- as.numeric(stats::logLik(m))
  ll0 <- if (firth) -m$null.deviance / 2 else as.numeric(stats::logLik(m0))
  rep_ <- rep(seq_len(nrow(data)), w)
  yy <- data$.y[rep_]
  pp <- unname(stats::fitted(m))[rep_]
  if (!is.null(weights) && !firth) {  # the weighted fit is the fit of the repeated rows
    big <- stats::glm(formula, family = fam, data = data[rep_, setdiff(names(data), ".w")])
    stopifnot(max(abs(stats::coef(big) - stats::coef(m))) < 1e-8)
  }
  hat <- as.numeric(pp >= threshold)
  confusion <- table(factor(yy, levels = c(1, 0)), factor(hat, levels = c(1, 0)))
  other <- setdiff(sort(unique(tolower(as.character(data[[outcome]])))), tolower(event))
  if (is.logical(data[[outcome]])) other <- tolower(other)
  # AUC and DeLong's variance from the pairwise kernel
  x1 <- pp[yy == 1]
  x0 <- pp[yy == 0]
  psi <- outer(x1, x0, ">") + 0.5 * outer(x1, x0, "==")
  auc <- mean(psi)
  v10 <- rowMeans(psi)
  v01 <- colMeans(psi)
  s <- sqrt(stats::var(v10) / length(x1) + stats::var(v01) / length(x0))
  lr <- m$null.deviance - m$deviance
  lr_df <- m$df.null - m$df.residual
  cs <- 1 - exp(2 * (ll0 - ll) / n)
  expect <- c(expect, list(
    txt("model|||family", "binomial"), txt("model|||link", link), txt("model|||estimation", if (firth) "firth" else "ml"),
    txt("model|||se_type", se),
    num("model|||auc", auc), num("model|||auc_ci_low", max(0, auc - z * s)), num("model|||auc_ci_high", min(1, auc + z * s)),
    num("model|||brier", mean((pp - yy)^2)), num("model|||threshold", threshold),
    num("model|||accuracy", mean(hat == yy)), num("model|||sensitivity", mean(hat[yy == 1] == 1)),
    num("model|||specificity", mean(hat[yy == 0] == 0)), num("model|||precision", mean(yy[hat == 1] == 1)),
    num("model|||deviance", m$deviance), num("model|||null_deviance", m$null.deviance), num("model|||lr_statistic", lr),
    num("model|||lr_df", lr_df), num("model|||lr_p_value", stats::pchisq(lr, lr_df, lower.tail = FALSE)),
    num("model|||log_lik", ll), num("model|||aic", stats::AIC(m)), num("model|||bic", stats::BIC(m)),
    num("model|||mcfadden_r2", 1 - ll / ll0), num("model|||nagelkerke_r2", cs / (1 - exp(2 * ll0 / n))),
    num("model|||tjur_r2", mean(pp[yy == 1]) - mean(pp[yy == 0])),
    num(paste0("cell|", event, "|", event, "|observed"), confusion[1, 1]),
    num(paste0("cell|", event, "|", other, "|observed"), confusion[1, 2]),
    num(paste0("cell|", other, "|", event, "|observed"), confusion[2, 1]),
    num(paste0("cell|", other, "|", other, "|observed"), confusion[2, 2])))
  if (firth) expect <- c(expect, list(txt("model|||note_firth", paste0(
    "Firth's penalized likelihood (brglm2, AS_mean): the estimates are pulled towards 0 and stay finite under separation; ",
    "the deviance, the likelihood-ratio test and the AIC are the ordinary likelihood's at these estimates."))))

  # separation, by detectseparation called directly
  sep <- stats::glm(formula, family = fam, data = data, weights = .w, method = detectseparation::detect_separation)
  infinite <- names(sep$coefficients)[is.infinite(sep$coefficients)]
  infinite <- c(setdiff(infinite, "(Intercept)"), intersect(infinite, "(Intercept)"))
  if (length(infinite)) {
    for (name in infinite) {
      t <- map[[name]]
      label <- if (is.na(t[[2]])) t[[1]] else paste0(t[[1]], ": ", t[[2]])
      expect <- c(expect, list(txt(paste0("assumption|separation|", label, "|verdict"), "doubtful")))
    }
  } else {
    expect <- c(expect, list(txt("assumption|separation||verdict", "ok")))
  }

  # Hosmer–Lemeshow, hoslem.test's steps on the repeated rows
  qq <- unique(stats::quantile(pp, probs = seq(0, 1, 0.1)))
  cut_ <- cut(pp, breaks = qq, include.lowest = TRUE)
  observed <- stats::xtabs(cbind(y0 = 1 - yy, y1 = yy) ~ cut_)
  expected <- stats::xtabs(cbind(yhat0 = 1 - pp, yhat1 = pp) ~ cut_)
  chisq <- sum((observed - expected)^2 / expected)
  groups <- nlevels(cut_)
  p_hl <- stats::pchisq(chisq, groups - 2, lower.tail = FALSE)
  expect <- c(expect, list(num("assumption|hosmer_lemeshow||statistic", chisq), num("assumption|hosmer_lemeshow||df", groups - 2),
                           num("assumption|hosmer_lemeshow||p_value", p_hl),
                           txt("assumption|hosmer_lemeshow||verdict", if (p_hl < alpha) "failed" else "ok")))

  # Box–Tidwell: each number predictor above 0 with its x·log(x), the Wald test of that term
  for (name in names(term_names)) {
    x <- data[[name]]
    if (!is.numeric(x)) next
    label <- term_names[[name]]
    if (any(x <= 0) || length(unique(x)) < 3) {
      expect <- c(expect, list(txt(paste0("assumption|box_tidwell|", label, "|verdict"), "not_computed")))
      next
    }
    d2 <- data
    d2$bt <- x * log(x)
    s2 <- summary(fit(stats::update(formula, . ~ . + bt), d2))$coefficients
    expect <- c(expect, list(num(paste0("assumption|box_tidwell|", label, "|statistic"), s2["bt", 3]),
                             num(paste0("assumption|box_tidwell|", label, "|p_value"), s2["bt", 4]),
                             txt(paste0("assumption|box_tidwell|", label, "|verdict"), if (s2["bt", 4] < alpha) "failed" else "ok")))
  }

  # the VIFs, from the regressors' correlations weighted by the working weights
  X <- stats::model.matrix(m)
  assign <- attr(X, "assign")
  Xs <- X[, assign > 0, drop = FALSE]
  assign <- assign[assign > 0]
  R <- stats::cov.wt(Xs, wt = m$weights / sum(m$weights), cor = TRUE)$cor
  labels <- attr(stats::terms(m), "term.labels")
  for (a in unique(assign)) {
    sub <- which(assign == a)
    gvif <- det(R[sub, sub, drop = FALSE]) * det(R[-sub, -sub, drop = FALSE]) / det(R)
    value <- gvif^(1 / length(sub))
    label <- term_names[[labels[[a]]]]
    expect <- c(expect, list(num(paste0("assumption|vif|", label, "|value"), value, rel = 1e-8),
                             txt(paste0("assumption|vif|", label, "|verdict"),
                                 if (value > 10) "failed" else if (value > 5) "doubtful" else "ok")))
    if (length(sub) > 1) expect <- c(expect, list(num(paste0("assumption|vif|", label, "|statistic"), gvif, rel = 1e-8)))
  }

  # events per variable
  k <- ncol(Xs)
  epv <- min(events, n - events) / k
  expect <- c(expect, list(num("assumption|epv||value", epv), txt("assumption|epv||verdict", if (epv < 10) "doubtful" else "ok")))

  # influence, by stats
  rows_n <- nrow(data)
  cooks <- stats::cooks.distance(m)
  h <- stats::hatvalues(m)
  dr <- abs(stats::residuals(m, type = "deviance"))
  named <- function(score, above) {
    top <- head(order(-score), 5L)
    paste(row_numbers[top[score[top] > above]], collapse = ", ")
  }
  limit_h <- 2 * ncol(X) / rows_n
  expect <- c(expect, list(
    num("assumption|cooks_distance||count", sum(cooks > 4 / rows_n)), num("assumption|cooks_distance||threshold", 4 / rows_n),
    num("assumption|leverage||count", sum(h > limit_h)), num("assumption|leverage||threshold", limit_h),
    num("assumption|deviance_residuals||count", sum(dr > 3))))
  if (sum(cooks > 4 / rows_n) > 0) expect <- c(expect, list(txt("assumption|cooks_distance||rows", named(cooks, 4 / rows_n))))
  if (sum(h > limit_h) > 0) expect <- c(expect, list(txt("assumption|leverage||rows", named(h, limit_h))))
  if (sum(dr > 3) > 0) expect <- c(expect, list(txt("assumption|deviance_residuals||rows", named(dr, 3))))
  list(id = id, model = "logistic", data = file, types = types, params = params, expect = expect)
}

logistic_cases <- function() {
  cases <- list()
  ch <- write_table(churn(), "logistic_churn.csv")
  ch_types <- list(customer = "integer", tenure = "number", fee = "number", change = "number", plan = "text", store = "text",
                   weight = "integer", churned = "text")
  used <- stats::complete.cases(ch[, c("churned", "tenure", "fee", "change", "plan")])
  d <- ch[used, ]
  rownames(d) <- NULL
  d$plan <- factor(d$plan, levels = c("basic", "plus", "premium"))
  rows_d <- which(used)
  map <- list("(Intercept)" = c("(Intercept)", NA), tenure = c("tenure", NA), fee = c("fee", NA), change = c("change", NA),
              planplus = c("plan", "plus"), planpremium = c("plan", "premium"))
  names_ <- list(tenure = "tenure", fee = "fee", change = "change", plan = "plan")
  base <- list(outcome = "churned", event_value = "yes", predictors = list("tenure", "fee", "change", "plan"))
  f <- churned ~ tenure + fee + change + plan
  one <- function(id, params, full = TRUE, ...) {
    logistic_case(id, d, "data/logistic_churn.csv", ch_types, c(base, params), f, map, names_, rows_d, "yes", "churned",
                  full = full, n_missing = sum(!used), ...)
  }
  cases[[length(cases) + 1L]] <- one("logistic_ml", list())
  cases[[length(cases) + 1L]] <- one("logistic_probit", list(link = "probit", threshold = 0.3))
  cases[[length(cases) + 1L]] <- one("logistic_firth", list(estimation = "firth"))
  for (type in c("hc0", "hc1", "hc2", "hc3")) cases[[length(cases) + 1L]] <- one(paste0("logistic_", type), list(se = type), full = FALSE)
  cases[[length(cases) + 1L]] <- one("logistic_cluster", list(se = "cluster", cluster = "store"), full = FALSE, cluster = "store")
  cases[[length(cases) + 1L]] <- one("logistic_profile", list(ci = "profile", alpha = 0.1), full = FALSE)
  case <- one("logistic_firth_profile", list(estimation = "firth", ci = "profile"), full = FALSE)
  case$expect <- c(case$expect, list(txt("model|||note_firth_profile",
                                         "brglm2 has no profile likelihood intervals for Firth's fit: the intervals are Wald's.")))
  cases[[length(cases) + 1L]] <- case
  case <- one("logistic_weights", list(weights = "weight"), weights = "weight")
  case$expect <- c(case$expect, list(txt("model|||note_weights", paste0(
    "The weights are frequencies: n, the events, the rates and the checks count the cases the rows stand for."))))
  cases[[length(cases) + 1L]] <- case

  # the separated table: Firth fits, maximum likelihood refuses
  sp <- write_table(separated(), "logistic_separated.csv")
  sp_types <- list(dose = "number", group = "text", status = "text")
  sp$group <- factor(sp$group, levels = c("a", "b"))
  cases[[length(cases) + 1L]] <- logistic_case(
    "logistic_firth_separated", sp, "data/logistic_separated.csv", sp_types,
    list(outcome = "status", event_value = "sick", predictors = list("dose", "group"), estimation = "firth"),
    status ~ dose + group, list("(Intercept)" = c("(Intercept)", NA), dose = c("dose", NA), groupb = c("group", "b")),
    list(dose = "dose", group = "group"), seq_len(nrow(sp)), "sick", "status")

  # names that look like code, and a boolean outcome whose event is true
  odd <- utils::read.csv(file.path(data_dir, "linear_names.csv"), check.names = FALSE, na.strings = "", encoding = "UTF-8",
                         stringsAsFactors = FALSE)
  odd_types <- stats::setNames(list("number", "number", "text", "boolean"), names(odd))
  plain <- odd
  names(plain) <- c("y", "a", "g", "b")
  plain$g <- factor(plain$g, levels = c("east", "north", "south"))
  A <- "a`b"
  G <- "x); system(\"id\")"
  B <- "ü ñ"
  cases[[length(cases) + 1L]] <- logistic_case(
    "logistic_names", plain, "data/linear_names.csv", odd_types, list(outcome = B, event_value = TRUE, predictors = list(A, G)),
    b ~ a + g, list("(Intercept)" = c("(Intercept)", NA), a = c(A, NA), gnorth = c(G, "north"), gsouth = c(G, "south")),
    list(a = A, g = G), seq_len(nrow(plain)), "true", "b")

  refusal <- function(id, file, types, params, code, message) {
    list(id = id, model = "logistic", data = file, types = types, params = params, expect = list(),
         refuses = list(code = code, message = message))
  }
  cases[[length(cases) + 1L]] <- refusal(
    "logistic_refuses_separation", "data/logistic_separated.csv", sp_types,
    list(outcome = "status", event_value = "sick", predictors = list("dose", "group")), "separation",
    "the term «dose» separates the outcome: use estimation = firth, or a penalty")
  cases[[length(cases) + 1L]] <- refusal(
    "logistic_refuses_three_values", "data/logistic_churn.csv", ch_types,
    list(outcome = "plan", event_value = "basic", predictors = list("tenure")), "outcome_not_binary",
    "the outcome «plan» has 3 values: a logistic regression explains two (an event and its absence)")
  cases[[length(cases) + 1L]] <- refusal(
    "logistic_refuses_event_value", "data/logistic_churn.csv", ch_types,
    list(outcome = "churned", event_value = "maybe", predictors = list("tenure")), "event_value",
    "the outcome «churned» has no value «maybe» to be the event: its values are «no», «yes»")
  cases[[length(cases) + 1L]] <- refusal(
    "logistic_refuses_weights", "data/logistic_churn.csv", ch_types,
    list(outcome = "churned", event_value = "yes", predictors = list("tenure"), weights = "fee"), "weights_not_frequencies",
    "the weights «fee» are frequencies: whole numbers, 0 or more")
  cases
}

# ── the penalties (lane M3) ──────────────────────────────────────────────────
# By glmnet::cv.glmnet called directly, on the design stats::model.matrix makes from a formula
# (the same columns in the same order as the runner's, the intercept left to glmnet), with the
# folds drawn the way the specification says: the node's seed set (Mersenne-Twister, Inversion,
# Rejection), then sample(rep(1:K, length.out = n)), cv.glmnet's own draw; grouped, the values
# of the fold_by column in text order drawn into the folds the same way, every row going with
# its value. That foldid is handed to cv.glmnet, which then computes the very numbers the runner
# reads: the path, the curve, λ min and 1se, the coefficients (coef(cv, s = …)), the
# out-of-fold predictions (keep = TRUE). A fixed λ is added to glmnet's own path for the rows;
# mixing = cv runs the grid on the same folds and keeps the least error at each one's best λ.
# The out-of-fold numbers are computed here from fit.preval: R², RMSE and MAE; for the logistic
# the Brier score, the rates at the threshold and the deviance from dbinom on all the rows, and
# the AUC fold by fold (the folds' fits have different intercepts: their predictions are not
# ranked together) by the pairwise kernel on each fold's rows repeated by their weight, the mean
# weighted by the folds' weights and the standard error between folds as cv.glmnet's cvsd; with
# measure = auc and no weights that mean is cv.glmnet's own cvm, checked here.

# 40 rows of four numbers and an outcome that none of them explains (the lasso keeps nothing).
noise <- function() {
  set.seed(20261125)
  n <- 40
  data.frame(x1 = round(stats::rnorm(n), 3), x2 = round(stats::rnorm(n), 3), x3 = round(stats::rnorm(n), 3),
             x4 = round(stats::runif(n, 0, 10), 2), y = round(stats::rnorm(n, 10, 2), 3))
}

penalized_foldid <- function(n, folds, seed = 1, groups = NULL) {
  set.seed(seed, kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rejection")
  if (is.null(groups)) return(sample(rep(seq_len(folds), length.out = n)))
  values <- sort(unique(groups), method = "radix")
  of_value <- sample(rep(seq_len(folds), length.out = length(values)))
  of_value[match(groups, values)]
}

penalized_case <- function(id, model, data, file, types, params, formula, map, event = NULL, n_missing = 0,
                           notes = character(), rel = 1e-9) {
  logistic <- model == "logistic"
  penalty <- params$penalty
  folds <- params$folds %||% 10
  measure <- params$measure %||% (if (logistic) "deviance" else "mse")
  standardize <- params$standardize %||% TRUE
  choice <- params$lambda %||% "cv_1se"
  fixed <- if (is.numeric(choice)) choice else NULL
  threshold <- params$threshold %||% 0.5
  outcome <- all.vars(formula)[[1]]
  X <- stats::model.matrix(formula, data)[, -1, drop = FALSE]
  y <- if (logistic) as.numeric(as.character(data[[outcome]]) == event) else data[[outcome]]
  w <- if (is.null(params$weights)) NULL else data[[params$weights]]
  ww <- if (is.null(w)) rep(1, nrow(X)) else w
  groups <- if (is.null(params$fold_by)) NULL else as.character(data[[params$fold_by]])
  foldid <- penalized_foldid(nrow(X), folds, params$seed %||% 1, groups)
  family <- if (logistic) "binomial" else "gaussian"
  one <- function(a) {
    lambda <- NULL
    if (!is.null(fixed)) {
      path <- glmnet::glmnet(X, y, weights = w, family = family, alpha = a, standardize = standardize)$lambda
      lambda <- sort(unique(c(path, fixed)), decreasing = TRUE)
    }
    glmnet::cv.glmnet(X, y, weights = w, family = family, alpha = a, standardize = standardize, lambda = lambda,
                      foldid = foldid, type.measure = measure, keep = TRUE)
  }
  grid <- c(0.1, 0.25, 0.5, 0.75, 0.9, 1)
  mixing <- switch(penalty, ridge = 0, lasso = 1, elastic_net = params$mixing %||% 0.5)
  if (identical(mixing, "cv")) {
    fits <- lapply(grid, one)
    at <- function(f) if (is.null(fixed)) which(f$lambda == f$lambda.min) else which(f$lambda == fixed)
    score <- vapply(fits, function(f) f$cvm[[at(f)]], 0)
    best <- if (measure == "auc") which.max(score) else which.min(score)
    cv <- fits[[best]]
    mixing <- grid[[best]]
  } else {
    cv <- one(mixing)
  }
  lam <- if (!is.null(fixed)) fixed else if (choice == "cv_min") cv$lambda.min else cv$lambda.1se
  idx <- which(cv$lambda == lam)
  i_min <- which(cv$lambda == cv$lambda.min)
  b <- as.matrix(stats::coef(cv, s = lam))[, 1]
  n_selected <- sum(b[-1] != 0)
  expect <- list(txt("model|||variant", "penalized"), txt("model|||penalty", penalty), txt("model|||cv_measure", measure),
                 txt("model|||family", family), num("model|||n_missing", n_missing),
                 num("model|||lambda", lam, rel), num("model|||log_lambda", log(lam), rel),
                 num("model|||lambda_min", cv$lambda.min, rel), num("model|||lambda_1se", cv$lambda.1se, rel),
                 num("model|||log_lambda_min", log(cv$lambda.min), rel), num("model|||log_lambda_1se", log(cv$lambda.1se), rel),
                 num("model|||mixing", mixing), num("model|||cv_value", cv$cvm[[idx]], rel), num("model|||cv_se", cv$cvsd[[idx]], rel),
                 num("model|||n_selected", n_selected))
  for (name in names(b)) {
    expect <- c(expect, list(num(coef_key(map, name, "estimate"), b[[name]], rel),
                             num(coef_key(map, name, "selected"), as.numeric(b[[name]] != 0 || name == "(Intercept)"))))
    if (logistic) expect <- c(expect, list(num(coef_key(map, name, "odds_ratio"), exp(b[[name]]), rel)))
  }
  # the curve: its first and last λ, the least error's and the 1se's
  for (i in unique(c(1L, i_min, which(cv$lambda == cv$lambda.1se), length(cv$lambda)))) {
    k <- i - 1L
    expect <- c(expect, list(num(paste0("cv|||cv_mean|", k), cv$cvm[[i]], rel), num(paste0("cv|||cv_low|", k), cv$cvlo[[i]], rel),
                             num(paste0("cv|||cv_high|", k), cv$cvup[[i]], rel), num(paste0("cv|||n_selected|", k), cv$nzero[[i]])))
  }
  pre <- cv$fit.preval[, idx]
  if (logistic) {
    p <- 1 / (1 + exp(-pre))
    rep_ <- rep(seq_along(y), ww)
    yy <- y[rep_]
    pp <- p[rep_]
    ff <- foldid[rep_]
    per <- t(vapply(sort(unique(foldid)), function(k) {
      x1 <- pp[ff == k & yy == 1]
      x0 <- pp[ff == k & yy == 0]
      a <- if (length(x1) && length(x0)) mean(outer(x1, x0, ">") + 0.5 * outer(x1, x0, "==")) else NA_real_
      c(weight = sum(ff == k), auc = a, brier = mean((pp[ff == k] - yy[ff == k])^2))
    }, numeric(3)))
    between <- function(x, wt) {
      ok <- !is.na(x)
      m <- sum(x[ok] * wt[ok]) / sum(wt[ok])
      c(mean = m, se = sqrt(sum(wt[ok] * (x[ok] - m)^2) / sum(wt[ok]) / (sum(ok) - 1)), n = sum(ok))
    }
    auc <- between(per[, "auc"], per[, "weight"])
    brier <- between(per[, "brier"], per[, "weight"])
    if (measure == "auc" && is.null(w)) stopifnot(abs(auc[["mean"]] - cv$cvm[[idx]]) < 1e-12, abs(auc[["se"]] - cv$cvsd[[idx]]) < 1e-12)
    hat <- as.numeric(pp >= threshold)
    expect <- c(expect, list(
      txt("model|||link", "logit"), txt("model|||event_value", event),
      num("model|||n", sum(ww)), num("model|||events", sum(ww * y)), num("model|||auc", auc[["mean"]], rel),
      num("model|||auc_se", auc[["se"]], rel), num("model|||auc_folds", auc[["n"]]),
      num("model|||brier", mean((pp - yy)^2), rel), num("model|||brier_se", brier[["se"]], rel), num("model|||threshold", threshold),
      num("model|||accuracy", mean(hat == yy)), num("model|||sensitivity", mean(hat[yy == 1] == 1)),
      num("model|||specificity", mean(hat[yy == 0] == 0)), num("model|||precision", mean(yy[hat == 1] == 1)),
      num("model|||deviance", -2 * sum(stats::dbinom(yy, 1, pp, log = TRUE)), rel)))
    # separation by detectseparation, the events per variable
    d2 <- data.frame(.y = y, X)
    sep <- stats::glm(.y ~ ., family = stats::binomial(), data = d2, weights = ww, method = detectseparation::detect_separation)
    expect <- c(expect, list(txt("assumption|hosmer_lemeshow||verdict", "not_computed")))
    if (!any(is.infinite(sep$coefficients))) expect <- c(expect, list(txt("assumption|separation||verdict", "ok")))
    infinite <- sub("^X[.]", "", names(sep$coefficients)[is.infinite(sep$coefficients)])
    for (name in setdiff(names(b), "(Intercept)")) {
      if (!make.names(name) %in% infinite) next
      t <- map[[name]]
      label <- if (is.na(t[[2]])) t[[1]] else paste0(t[[1]], ": ", t[[2]])
      expect <- c(expect, list(txt(paste0("assumption|separation|", label, "|verdict"), "doubtful")))
    }
    epv <- min(sum(ww * y), sum(ww) - sum(ww * y)) / ncol(X)
    expect <- c(expect, list(num("assumption|epv||value", epv), txt("assumption|epv||verdict", if (epv < 10) "doubtful" else "ok")))
  } else {
    e <- y - pre
    expect <- c(expect, list(
      num("model|||n", nrow(X)),
      num("model|||rmse", sqrt(stats::weighted.mean(e^2, ww)), rel), num("model|||mae", stats::weighted.mean(abs(e), ww), rel),
      num("model|||r_squared", 1 - sum(ww * e^2) / sum(ww * (y - stats::weighted.mean(y, ww))^2), rel)))
  }
  edge <- i_min == length(cv$lambda) || i_min == 1L
  expect <- c(expect, list(txt("assumption|lambda_choice||verdict", if (edge || n_selected == 0 || idx == 1L) "doubtful" else "ok")))
  for (note in names(notes)) expect <- c(expect, list(txt(paste0("model|||note_", note), notes[[note]])))
  list(id = id, model = model, data = file, types = types, params = params, expect = expect)
}

penalized_cases <- function() {
  cases <- list()
  edge_small <- paste0("The least cross-validated error is at the smallest λ of the path: the best penalty may be smaller ",
                       "still; widen the path with a fixed λ below it, or compare with the fit without a penalty.")
  # the categorical table: lasso at λ 1se; ridge at λ min, the least error at the path's smallest λ
  cat_ <- utils::read.csv(file.path(data_dir, "linear_categorical.csv"), na.strings = "", encoding = "UTF-8", stringsAsFactors = FALSE)
  cat_types <- list(region = "text", price = "number", price_cents = "number", month = "integer", promo = "boolean",
                    sales = "number")
  cat_$region <- factor(cat_$region, levels = c("north", "east", "south"))
  cat_$promo <- factor(tolower(as.character(cat_$promo)), levels = c("false", "true"))
  map_c <- list("(Intercept)" = c("(Intercept)", NA), price = c("price", NA), regioneast = c("region", "east"),
                regionsouth = c("region", "south"), month = c("month", NA), promotrue = c("promo", "true"),
                "price:regioneast" = c("price × region", "east"), "price:regionsouth" = c("price × region", "south"))
  base_c <- list(outcome = "sales", predictors = list("price", "region", "month", "promo"), reference = list(region = "north"),
                 interactions = list(list("price", "region")))
  f_c <- sales ~ price + region + month + promo + price:region
  cases[[length(cases) + 1L]] <- penalized_case("penalized_linear_lasso", "linear", cat_, "data/linear_categorical.csv", cat_types,
                                                c(base_c, list(penalty = "lasso")), f_c, map_c)
  cases[[length(cases) + 1L]] <- penalized_case("penalized_linear_ridge_edge", "linear", cat_, "data/linear_categorical.csv",
                                                cat_types, c(base_c, list(penalty = "ridge", lambda = "cv_min")), f_c, map_c,
                                                notes = c(edge_small = edge_small))

  # the heteroscedastic table: elastic net, its mixing by cross-validation, folds grouped by store, case weights, MAE
  het <- utils::read.csv(file.path(data_dir, "linear_heteroscedastic.csv"), na.strings = "", encoding = "UTF-8",
                         stringsAsFactors = FALSE)
  het_types <- list(day = "integer", price = "number", channel = "text", store = "text", weight = "number", quantity = "number")
  used <- stats::complete.cases(het[, c("quantity", "price", "channel", "store", "weight")])
  h <- het[used, ]
  rownames(h) <- NULL
  h$channel <- factor(h$channel, levels = c("shop", "web"))
  map_h <- list("(Intercept)" = c("(Intercept)", NA), price = c("price", NA), channelweb = c("channel", "web"),
                "price:channelweb" = c("price × channel", "web"))
  cases[[length(cases) + 1L]] <- penalized_case(
    "penalized_linear_enet_grouped", "linear", h, "data/linear_heteroscedastic.csv", het_types,
    list(outcome = "quantity", predictors = list("price", "channel"), interactions = list(list("price", "channel")),
         penalty = "elastic_net", mixing = "cv", fold_by = "store", folds = 4, weights = "weight", measure = "mae"),
    quantity ~ price + channel + price:channel, map_h, n_missing = sum(!used),
    notes = c(mixing_cv = paste0("The mixing was chosen by cross-validation among 0.1, 0.25, 0.5, 0.75, 0.9 and 1, on the same ",
                                 "folds: the one with the least error at its best λ."),
              grouped_folds = "The folds are grouped: all the rows of a value of the column fold_by fall in the same fold."))

  # noise: at λ 1se the lasso keeps no term (at λ min two, by chance)
  nz <- write_table(noise(), "penalized_noise.csv")
  nz_types <- list(x1 = "number", x2 = "number", x3 = "number", x4 = "number", y = "number")
  cases[[length(cases) + 1L]] <- penalized_case(
    "penalized_linear_noise", "linear", nz, "data/penalized_noise.csv", nz_types,
    list(outcome = "y", predictors = list("x1", "x2", "x3", "x4"), penalty = "lasso", folds = 5, seed = 2),
    y ~ x1 + x2 + x3 + x4, list("(Intercept)" = c("(Intercept)", NA), x1 = c("x1", NA), x2 = c("x2", NA), x3 = c("x3", NA),
                                x4 = c("x4", NA)),
    notes = c(all_zero_1se = paste0("Every coefficient is zero at the chosen λ, the largest within one standard error of the ",
                                    "least error: lambda = cv_min keeps the terms of the least error.")))

  # the churn table: lasso at λ min; ridge by the AUC; elastic net at a fixed λ with frequency weights
  ch <- utils::read.csv(file.path(data_dir, "logistic_churn.csv"), na.strings = "", encoding = "UTF-8", stringsAsFactors = FALSE)
  ch_types <- list(customer = "integer", tenure = "number", fee = "number", change = "number", plan = "text", store = "text",
                   weight = "integer", churned = "text")
  used <- stats::complete.cases(ch[, c("churned", "tenure", "fee", "change", "plan")])
  d <- ch[used, ]
  rownames(d) <- NULL
  d$plan <- factor(d$plan, levels = c("basic", "plus", "premium"))
  map_d <- list("(Intercept)" = c("(Intercept)", NA), tenure = c("tenure", NA), fee = c("fee", NA), change = c("change", NA),
                planplus = c("plan", "plus"), planpremium = c("plan", "premium"))
  base_d <- list(outcome = "churned", event_value = "yes", predictors = list("tenure", "fee", "change", "plan"))
  f_d <- churned ~ tenure + fee + change + plan
  one <- function(id, params, ...) penalized_case(id, "logistic", d, "data/logistic_churn.csv", ch_types, c(base_d, params), f_d,
                                                  map_d, event = "yes", n_missing = sum(!used), ...)
  cases[[length(cases) + 1L]] <- one("penalized_logistic_lasso", list(penalty = "lasso", lambda = "cv_min"))
  cases[[length(cases) + 1L]] <- one("penalized_logistic_enet", list(penalty = "elastic_net", mixing = 0.5))
  cases[[length(cases) + 1L]] <- one("penalized_logistic_ridge_auc", list(penalty = "ridge", measure = "auc", folds = 5))
  cases[[length(cases) + 1L]] <- one(
    "penalized_logistic_enet_fixed",
    list(penalty = "elastic_net", mixing = 0.5, lambda = 0.02, measure = "class", weights = "weight", threshold = 0.4,
         standardize = FALSE),
    notes = c(fixed_lambda = "λ is fixed: it is added to glmnet's path, and the coefficients and the out-of-fold numbers are read at it.",
              not_standardized = paste0("The predictors are penalized on their own scales (standardize = false): a predictor with ",
                                        "large values is penalized less than one with small values.")))

  # the separated table: the lasso keeps the estimates finite, its least error at the path's smallest λ
  sp <- utils::read.csv(file.path(data_dir, "logistic_separated.csv"), na.strings = "", encoding = "UTF-8", stringsAsFactors = FALSE)
  sp$group <- factor(sp$group, levels = c("a", "b"))
  cases[[length(cases) + 1L]] <- penalized_case(
    "penalized_logistic_separated", "logistic", sp, "data/logistic_separated.csv", list(dose = "number", group = "text", status = "text"),
    list(outcome = "status", event_value = "sick", predictors = list("dose", "group"), penalty = "lasso", folds = 5),
    status ~ dose + group, list("(Intercept)" = c("(Intercept)", NA), dose = c("dose", NA), groupb = c("group", "b")),
    event = "sick", notes = c(edge_small = edge_small))

  refusal <- function(id, model, file, types, params, code, message) {
    list(id = id, model = model, data = file, types = types, params = params, expect = list(),
         refuses = list(code = code, message = message))
  }
  cases[[length(cases) + 1L]] <- refusal(
    "penalized_refuses_few_groups", "linear", "data/linear_heteroscedastic.csv", het_types,
    list(outcome = "quantity", predictors = list("price", "channel"), penalty = "lasso", fold_by = "store"), "too_few_groups",
    "the column «store» has 8 values: grouped folds need at least as many values as folds (10): choose fewer folds, or another column")
  cases[[length(cases) + 1L]] <- refusal(
    "penalized_refuses_one_term", "linear", "data/linear_categorical.csv", cat_types,
    list(outcome = "sales", predictors = list("price"), penalty = "ridge"), "one_term",
    "a penalty needs at least two terms besides the intercept, and the model has 1: add a predictor, or turn the penalty off")
  cases[[length(cases) + 1L]] <- refusal(
    "penalized_refuses_few_rows", "linear", "data/linear_tiny.csv", list(y = "number", x1 = "number", x2 = "number", g = "text"),
    list(outcome = "y", predictors = list("x1", "x2", "g"), penalty = "lasso", folds = 3), "too_few_rows",
    "the model has 4 rows it can use: cross-validation in 3 folds needs at least 9 (3 in each fold): choose fewer folds")
  cases[[length(cases) + 1L]] <- refusal(
    "penalized_refuses_auc_few_rows", "logistic", "data/logistic_separated.csv", list(dose = "number", group = "text", status = "text"),
    list(outcome = "status", event_value = "sick", predictors = list("dose", "group"), penalty = "lasso", measure = "auc", folds = 5),
    "auc_few_rows", "the AUC of a fold needs at least 10 rows in it: with 40 rows choose at most 4 folds, or measure = deviance")
  cases
}

cases <- c(linear_cases(), logistic_cases(), penalized_cases())

packages <- c("stats", "sandwich", "lmtest", "nortest", "brglm2", "detectseparation", "glmnet", "jsonlite")
out <- list(format_version = 1L,
            generated = list(by = "make_reference.R", R = paste(R.version$major, R.version$minor, sep = "."),
                             packages = as.list(stats::setNames(vapply(packages, function(p) as.character(utils::packageVersion(p)), ""),
                                                                packages)),
                             command = "Rscript gateway-go/internal/modelgroups/groups/regression/reference/make_reference.R"),
            cases = cases)
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE),
           file.path(here, "expected.json"), useBytes = TRUE)
cat("expected.json written:", length(cases), "cases\n")
