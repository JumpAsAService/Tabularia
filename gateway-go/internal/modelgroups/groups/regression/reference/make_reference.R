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
# available on the machine that wrote these values: no witness file yet.
#
# Lane M1 writes the cases of the linear regression; the lanes of the logistic regression and
# of the penalties add theirs below, each in a function of its own.

suppressPackageStartupMessages({
  library(stats)
  library(sandwich)
  library(lmtest)
  library(nortest)
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

cases <- linear_cases()

packages <- c("stats", "sandwich", "lmtest", "nortest", "jsonlite")
out <- list(format_version = 1L,
            generated = list(by = "make_reference.R", R = paste(R.version$major, R.version$minor, sep = "."),
                             packages = as.list(stats::setNames(vapply(packages, function(p) as.character(utils::packageVersion(p)), ""),
                                                                packages)),
                             command = "Rscript gateway-go/internal/modelgroups/groups/regression/reference/make_reference.R"),
            cases = cases)
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE),
           file.path(here, "expected.json"), useBytes = TRUE)
cat("expected.json written:", length(cases), "cases\n")
