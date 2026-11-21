# Writes the reference cases of the group customer_lifetime: the CSVs of reference/data/ and
# reference/expected.json (docs/specs/customer-lifetime-models.md, «Reference values»). Run by hand,
# inside the group's image, with the reference folder mounted writable:
#
#   docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/customer_lifetime/reference:/ref" \
#     tabularia-models-customer_lifetime:dev Rscript /ref/make_reference.R
#
# No copy of a public data set. Lot B1 wrote the refusal cases: tables built, not drawn (a wrong summary
# stops with a sentence that says what to change; the sentences are written out here by hand, so that
# they are checked word for word). Lot B2 adds the fitted cases of the node `clv` (clv_*.csv): customers
# drawn from the models' own processes with fixed seeds, in whole days, and their expected values
# computed here without the runner — CLVTools' public pnbd(), bgnbd(), gg() and predict() on the
# transaction log the summary comes from (route A), an MBG/NBD likelihood written here and maximized
# to the end, Pearson's r and the HC3 test through lm(). Verdicts, counts and texts are the cases'
# purpose, written by hand.
#
# expected.json is shared: each case says which script wrote it (`by`), and this script replaces only
# its own cases, keeping those of another script (BG/BB's, lot B3: make_reference_bgbb.R does the same).

args <- commandArgs(FALSE)
here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", args, value = TRUE))))
dir.create(file.path(here, "data"), showWarnings = FALSE)

# ── the refusal tables ───────────────────────────────────────────────────────────
# 30 customers, valid: x = 0…4 in turn (24 repeaters), t_x at 0.4…0.7 of the age, a spend for the repeaters, a holdout.
# Every other column is the same column with one defect, so that a case chooses its defect by the columns it names.
refusals <- function(n) {
  i <- seq_len(n)
  x <- i %% 5
  age <- 20 + i
  tx <- ifelse(x > 0, round(age * (0.3 + 0.1 * x), 1), 0)
  d <- data.frame(id = paste0("c", i), x = x, tx = tx, T = age, m = ifelse(x > 0, 10 + i, NA_real_), h = i %% 3, stringsAsFactors = FALSE)
  with_ <- function(col, row, value) {
    v <- d[[col]]
    v[[row]] <- value
    v
  }
  d$id_dup <- with_("id", 7, "c3")
  d$x_frac <- with_("x", 4, 2.5)
  d$tx_neg <- with_("tx", 11, -3)
  d$T_zero <- with_("T", 6, 0)
  d$tx_after <- with_("tx", 17, d$T[[17]] + 2)
  d$tx_nopurchase <- with_("tx", 15, 12)      # x = 0 but a recency
  d$x_same_moment <- with_("x", 10, 2)        # tx = 0 but two repeat purchases
  d$m_zero <- with_("m", 3, 0)
  d$h_neg <- with_("h", 20, -1)
  d$x_none <- 0
  d$tx_none <- 0
  d$x_few <- ifelse(i %in% c(2, 7, 13), 1, 0)
  d$tx_few <- ifelse(i %in% c(2, 7, 13), 5, 0)
  d$m_few <- ifelse(i %in% c(2, 7, 13), 20, NA_real_)
  # BG/BB: 6 opportunities
  d$n <- 6
  d$n_zero <- with_("n", 12, 0)
  d$x_bb <- pmin(x, 3)
  d$tx_bb <- ifelse(d$x_bb > 0, pmin(d$x_bb + 2, 6), 0)
  d$tx_bb_low <- with_("tx_bb", 8, 1)         # the customer 8 has x_bb = 3 and a last transaction at the opportunity 1
  d$tx_bb_late <- with_("tx_bb", 9, 8)        # an opportunity after the last
  d$h_bb <- i %% 3
  d$h_bb_over <- with_("h_bb", 14, 9)
  d
}
write_table <- function(d, name) utils::write.csv(d, file.path(here, "data", name), row.names = FALSE, na = "")
write_table(refusals(30), "clv_refusals.csv")
write_table(refusals(30)[1:12, ], "clv_refusals_small.csv")

# ── the cases ────────────────────────────────────────────────────────────────────
numbers <- c("x", "tx", "T", "m", "h", "x_frac", "tx_neg", "T_zero", "tx_after", "tx_nopurchase", "x_same_moment", "m_zero", "h_neg", "x_none", "tx_none",
             "x_few", "tx_few", "m_few", "n", "n_zero", "x_bb", "tx_bb", "tx_bb_low", "tx_bb_late", "h_bb", "h_bb_over")
types <- c(stats::setNames(rep("number", length(numbers)), numbers), id = "text", id_dup = "text")
base <- list(customer = "id", frequency = "x", recency = "tx", age = "T", time_unit = "week", seed = 1)
bb <- list(customer = "id", frequency = "x_bb", recency = "tx_bb", opportunities = "n", period_unit = "month", seed = 1)

refusal <- function(id, params, code, message, data = "data/clv_refusals.csv", model = "clv", variant = "purchases") {
  roles <- c("customer", "frequency", "recency", "age", "opportunities", "spend", "holdout_transactions")
  used <- unique(unlist(params[intersect(roles, names(params))]))
  list(id = id, model = model, variant = variant, data = data, types = as.list(types[used]), params = params, refuses = list(code = code, message = message))
}
over <- function(p, ...) utils::modifyList(p, list(...))

cases <- list(
  refusal("refusal duplicate customer", over(base, customer = "id_dup"), "duplicate_customer",
          "the customer c3 appears on 2 rows: one row per customer; group the transactions by customer first"),
  refusal("refusal frequency not whole", over(base, frequency = "x_frac"), "frequency_not_whole", "frequency must count purchases: the customer c4 has 2.5"),
  refusal("refusal negative recency", over(base, recency = "tx_neg"), "negative_value",
          "recency, age and frequency are 0 or more: the customer c11 has a recency of −3"),
  refusal("refusal age zero", over(base, age = "T_zero"), "age_zero", "the customer c6 has an age of 0: age is the time since the first purchase, more than 0"),
  refusal("refusal recency after age", over(base, recency = "tx_after"), "recency_after_age",
          "the customer c17 bought last (recency 39) after the end of the period (age 37): recency is at most age"),
  refusal("refusal recency without purchase", over(base, recency = "tx_nopurchase"), "recency_without_purchase",
          "the customer c15 has no repeat purchase but a recency of 12: recency is 0 without repeat purchases"),
  refusal("refusal same moment", over(base, frequency = "x_same_moment"), "same_moment",
          paste0("the customer c10 has 2 repeat purchases at recency 0: purchases of the same day must be one purchase; ",
                 "group by customer and day first")),
  refusal("refusal spend not positive", over(base, spend = "m_zero"), "spend_not_positive",
          paste0("the customer c3 has an average spend of 0 with repeat purchases: the spend model needs amounts above 0; ",
                 "leave out returns and refunds, or those customers"), variant = "value"),
  refusal("refusal holdout negative", over(base, holdout_transactions = "h_neg", holdout_length = 26), "holdout_negative",
          "holdout purchases are whole numbers, 0 or more: the customer c20 has −1"),
  refusal("refusal few customers", base, "few_customers", "12 customers: these models need at least 20", data = "data/clv_refusals_small.csv"),
  refusal("refusal no repeaters", over(base, frequency = "x_none", recency = "tx_none"), "no_repeaters",
          "no customer bought again: the models cannot tell the purchase rate from the dropout without repeat purchases"),
  refusal("refusal few repeaters", over(base, frequency = "x_few", recency = "tx_few"), "few_repeaters",
          "3 customers bought again: these models need at least 5"),
  refusal("refusal few spenders", over(base, spend = "m_few"), "few_spenders", "3 repeat customers have a spend: the spend model needs at least 5", variant = "value"),
  refusal("refusal BG/BB opportunities", over(bb, opportunities = "n_zero"), "bgbb_opportunities", "opportunities run from 1 to 1,000: the customer c12 has 0",
          model = "bg_bb"),
  refusal("refusal BG/BB frequency", over(bb, recency = "tx_bb_low"), "bgbb_frequency",
          "the customer c8 has 3 transactions in 1 opportunities up to its last: at most one transaction per opportunity", model = "bg_bb"),
  refusal("refusal BG/BB recency after opportunities", over(bb, recency = "tx_bb_late"), "recency_after_age",
          "the customer c9 bought last at the opportunity 8, after the last of its 6 opportunities: recency is at most opportunities", model = "bg_bb"),
  refusal("refusal BG/BB holdout", over(bb, holdout_transactions = "h_bb_over", holdout_length = 5), "bgbb_holdout",
          "a customer has at most one transaction per opportunity: the customer c14 has 9 in the 5 opportunities of the holdout", model = "bg_bb")
)

# ── the fitted cases of `clv` (lot B2) ───────────────────────────────────────────
suppressMessages({
  library(CLVTools)
  library(data.table)
})

# Numbers written with 17 significant digits, so that the runner reads back exactly the summary the
# expected values were computed on (a week is a whole number of days over 7).
write_exact <- function(d, name) {
  for (k in names(d)) if (is.double(d[[k]])) d[[k]] <- ifelse(is.na(d[[k]]), NA_character_, formatC(d[[k]], digits = 17, format = "g"))
  utils::write.csv(d, file.path(here, "data", name), row.names = FALSE, na = "")
}

# One customer drawn from a purchase model: the days of the repeat purchases after the first one (day 0),
# up to `end` days. Purchases of the same day are one, as the summary counts them.
draw_days <- function(model, par, end) {
  weeks <- end / 7
  lambda <- stats::rgamma(1, par[[1L]], rate = par[[2L]])
  t <- numeric()
  cur <- 0
  if (model == "pareto_nbd") {
    life <- stats::rexp(1, stats::rgamma(1, par[[3L]], rate = par[[4L]]))
    repeat {
      cur <- cur + stats::rexp(1, lambda)
      if (cur > min(life, weeks)) break
      t <- c(t, cur)
    }
  } else if (model == "poisson") {
    repeat {
      cur <- cur + stats::rexp(1, lambda)
      if (cur > weeks) break
      t <- c(t, cur)
    }
  } else {
    p <- stats::rbeta(1, par[[3L]], par[[4L]])
    alive <- if (model == "mbg_nbd") stats::runif(1) > p else TRUE
    while (alive) {
      cur <- cur + stats::rexp(1, lambda)
      if (cur > weeks) break
      t <- c(t, cur)
      if (stats::runif(1) < p) alive <- FALSE
    }
  }
  d <- unique(pmax(ceiling(t * 7), 1))
  sort(d[d <= end])
}

# n customers: their ages in days (`ages`, recycled), the days of their purchases up to the end of a
# holdout of `holdout` days after their age; a Gamma–Gamma spend (p, q, γ) for the repeaters, or `spend`
# a function of the frequency. Returns the summary (weeks) and the purchase days, for the transaction log.
draw_customers <- function(seed, n, model, par, ages, holdout = 0, gg = c(6, 4, 15), spend = NULL, prefix = "c") {
  set.seed(seed)
  age <- if (length(ages) == 1L) rep(ages, n) else sample(ages, n, replace = TRUE)
  days <- lapply(seq_len(n), function(i) draw_days(model, par, age[[i]] + holdout))
  cal <- lapply(seq_len(n), function(i) days[[i]][days[[i]] <= age[[i]]])
  x <- vapply(cal, length, 0)
  nu <- stats::rgamma(n, gg[[2L]], rate = gg[[3L]])
  m <- if (is.null(spend)) stats::rgamma(n, shape = gg[[1L]] * pmax(x, 1), rate = nu * pmax(x, 1)) else spend(x)
  d <- data.frame(customer = paste0(prefix, seq_len(n)), frequency = x,
                  recency = vapply(cal, function(z) if (length(z)) max(z) / 7 else 0, 0), age = age / 7,
                  avg_spend = ifelse(x > 0, m, NA_real_), stringsAsFactors = FALSE)
  if (holdout > 0) d$holdout_purchases <- vapply(seq_len(n), function(i) sum(days[[i]] > age[[i]]), 0)
  list(summary = d, days = days, age = age)
}

# Route A: the transaction log the summary comes from (the first purchase `age` days before the split,
# the repeat purchases on their days, each at the customer's average spend; the holdout's after the
# split), and CLVTools' public objects on it.
clv_log <- function(drawn) {
  d <- drawn$summary
  split <- as.Date("2020-01-01")
  log_ <- rbindlist(lapply(seq_len(nrow(d)), function(i) {
    dd <- c(0, drawn$days[[i]])
    price <- if (is.null(d$avg_spend) || is.na(d$avg_spend[[i]])) 1 else d$avg_spend[[i]]
    data.table(Id = d$customer[[i]], Date = split - drawn$age[[i]] + dd, Price = c(10, rep(price, length(dd) - 1L)))
  }))
  if (max(log_$Date) <= split) log_ <- rbind(log_, data.table(Id = d$customer[[1L]], Date = split + 30, Price = 5))
  clvdata(log_, date.format = "ymd", time.unit = "weeks", estimation.split = split, name.id = "Id", name.date = "Date", name.price = "Price")
}

# The MBG/NBD log-likelihood (Batislam, Denizel, Filiztekin 2007), written here from the article, and its
# maximum: Nelder–Mead, then BFGS and nlminb to the end (a tolerance far below the runner's L-BFGS-B).
mbgnbd_ll <- function(par, x, tx, T) {
  r <- par[[1L]]; al <- par[[2L]]; a <- par[[3L]]; b <- par[[4L]]
  A <- lbeta(a, b + x + 1) - lbeta(a, b) - (r + x) * log(al + T)
  B <- lbeta(a + 1, b + x) - lbeta(a, b) - (r + x) * log(al + tx)
  M <- pmax(A, B)
  sum(lgamma(r + x) - lgamma(r) + r * log(al) + M + log(exp(A - M) + exp(B - M)))
}
mbgnbd_max <- function(d) {
  f <- function(lp) -mbgnbd_ll(exp(lp), d$frequency, d$recency, d$age)
  o <- stats::optim(log(c(1, 3, 1, 3)), f, method = "Nelder-Mead", control = list(maxit = 20000, reltol = 1e-14))
  for (k in 1:3) {
    o <- stats::optim(o$par, f, method = "BFGS", control = list(maxit = 10000, reltol = 1e-16))
    n <- stats::nlminb(o$par, f, control = list(eval.max = 10000, iter.max = 10000, rel.tol = 1e-15, x.tol = 1e-12))
    o$par <- n$par
  }
  list(par = stats::setNames(exp(o$par), c("r", "alpha", "a", "b")), log_lik = -f(o$par))
}

# The CLV from its definition, Σ_k margin · E[M] · [CET(k) − CET(k − 1)] / (1 + d)^k over the horizon's weeks, d the
# week's rate of the annual one; `cet(k)` the customers' expected purchases in the next k weeks, computed here WITHOUT
# the runner's CET (CLVTools' predict(), or the integral form by integrate()), and E[M] Gamma–Gamma's expected spend.
clv_by_definition <- function(cet, horizon, annual, margin, gg, d) {
  rate <- (1 + annual)^(7 / 365.25) - 1
  g <- stats::setNames(as.numeric(gg), c("p", "q", "gamma"))
  em <- rep(g[["p"]] * g[["gamma"]] / (g[["q"]] - 1), nrow(d))
  u <- d$frequency > 0 & !is.na(d$avg_spend)
  em[u] <- (g[["gamma"]] + d$frequency[u] * d$avg_spend[u]) * g[["p"]] / (g[["p"]] * d$frequency[u] + g[["q"]] - 1)
  prev <- 0
  disc <- 0
  for (k in seq_len(horizon)) {
    cur <- cet(k)
    disc <- disc + (cur - prev) / (1 + rate)^k
    prev <- cur
  }
  sum(margin * em * disc)
}

# MBG/NBD's expected purchases in the next t weeks from the integral form (the model's definition: P(alive) times the
# NBD's expectation averaged over the dropout probability p ~ Beta(a, b + x + 1)), by R's integrate() on u = log p.
mbgnbd_cet_integrate <- function(par, t, x, tx, T) {
  r <- par[[1L]]; al <- par[[2L]]; a <- par[[3L]]; b <- par[[4L]]
  palive <- 1 / (1 + a / (b + x) * ((al + T) / (al + tx))^(r + x))
  vapply(seq_along(x), function(i) {
    f <- function(u) {
      p <- exp(u)
      g <- 1 - ((al + T[[i]]) / (al + T[[i]] + p * t))^(r + x[[i]])
      exp(log(g) + (a - 1) * u + (b + x[[i]]) * log1p(-p) - lbeta(a, b + x[[i]] + 1))
    }
    palive[[i]] * stats::integrate(f, -200, 0, rel.tol = 1e-12, subdivisions = 5000L)$value
  }, 0)
}

# The BG/NBD log-likelihood (Fader, Hardie, Lee 2005), written here from the article in the form that keeps its digits
# where r and α are huge (Γ(r + x)/Γ(r) as a product, r·log(α/(α + t)) as −r·log1p(t/α)), and its maximum to the end.
bgnbd_ll <- function(par, x, tx, T) {
  r <- par[[1L]]; al <- par[[2L]]; a <- par[[3L]]; b <- par[[4L]]
  g <- vapply(x, function(k) if (k > 0) sum(log(r + seq_len(k) - 1)) else 0, 0)
  A <- g - r * log1p(T / al) - x * log(al + T) + lbeta(a, b + x) - lbeta(a, b)
  B <- ifelse(x > 0, g - r * log1p(tx / al) - x * log(al + tx) + lbeta(a + 1, pmax(b + x - 1, 1e-300)) - lbeta(a, b), -Inf)
  M <- pmax(A, B)
  sum(M + log(exp(A - M) + exp(B - M)))
}
bgnbd_max <- function(d) {
  f <- function(lp) -bgnbd_ll(exp(lp), d$frequency, d$recency, d$age)
  o <- stats::optim(log(c(1, 3, 1, 3)), f, method = "Nelder-Mead", control = list(maxit = 20000, reltol = 1e-14))
  for (k in 1:3) {
    o <- stats::optim(o$par, f, method = "BFGS", control = list(maxit = 10000, reltol = 1e-16))
    n <- stats::nlminb(o$par, f, control = list(eval.max = 10000, iter.max = 10000, rel.tol = 1e-15, x.tol = 1e-12))
    o$par <- n$par
  }
  list(par = stats::setNames(exp(o$par), c("r", "alpha", "a", "b")), log_lik = -f(o$par))
}

# BG/NBD customers in continuous time (no whole days), ages from 0 to 158 weeks, more of them young (the density
# falls linearly, as in a base that acquires more customers every year): the process the BG/NBD describes, drawn
# without the rounding of a transaction log.
draw_continuous <- function(seed, n, par) {
  set.seed(seed)
  T <- 158 * (1 - sqrt(stats::runif(n)))
  lam <- stats::rgamma(n, par[[1L]], rate = par[[2L]])
  p <- stats::rbeta(n, par[[3L]], par[[4L]])
  x <- tx <- numeric(n)
  for (i in seq_len(n)) {
    cur <- 0
    repeat {
      cur <- cur + stats::rexp(1, lam[[i]])
      if (cur > T[[i]]) break
      x[[i]] <- x[[i]] + 1
      tx[[i]] <- cur
      if (stats::runif(1) < p[[i]]) break
    }
  }
  data.frame(customer = paste0("j", seq_len(n)), frequency = x, recency = tx, age = T, stringsAsFactors = FALSE)
}

# Pearson's r of the frequency and the average spend over the repeaters, and the slope's HC3 test, through lm().
hc3 <- function(d) {
  u <- d[d$frequency > 0 & !is.na(d$avg_spend), ]
  fit <- stats::lm(avg_spend ~ frequency, data = u)
  X <- stats::model.matrix(fit)
  e <- stats::residuals(fit) / (1 - stats::hatvalues(fit))
  bread <- solve(crossprod(X))
  V <- bread %*% crossprod(X * e) %*% bread
  z <- stats::coef(fit)[[2L]] / sqrt(V[2L, 2L])
  list(r = stats::cor(u$frequency, u$avg_spend), z = z, p = 2 * stats::pnorm(-abs(z)), n = nrow(u))
}

clv_types <- function(d) {
  t <- as.list(stats::setNames(rep("number", ncol(d)), names(d)))
  t[[names(d)[[1L]]]] <- "text"
  t
}

# The expectations: a number with its tolerances, a text exactly.
num_ <- function(key, value, rel = 1e-9, abs = 1e-12, from = NULL) {
  out <- list(key = key, value = unname(value), rel = rel, abs = abs)
  if (!is.null(from)) out$from <- from
  out
}
txt_ <- function(key, text) list(key = key, text = text)
params_of <- function(prefix, par, rel, from) unname(Map(function(k, v) num_(paste0(prefix, k), v, rel = rel, from = from), names(par), par))

# The tolerances (docs/specs/customer-lifetime-models.md, «Reference values», as measured by lot B2 and B10): the
# runner's estimates against CLVTools' public fit on the same customers, relative 1e-7 (measured: at most
# 8.7e-9 for Pareto/NBD, 3.4e-8 for Gamma-Gamma; the two minimize the same function from the same start, and
# a last-bit difference in the summary CLVTools derives from the dates moves where L-BFGS-B stops along a flat
# direction); the log-likelihoods 1e-6 absolute (measured 3e-12); the expectations against CLVTools'
# predict(), relative 1e-7 (measured 6e-10; the CLV of case A from predict() week by week 4.3e-12). The MBG/NBD
# against this script's own maximum: B10 runs the runner's L-BFGS-B to its tight tolerances (CLV_TIGHT), so the
# parameters are within 1.8e-6 relative (5.3e-4 with optim's defaults), tolerance 1e-5; the log-likelihood
# 1e-6 absolute (measured 6e-12); the CLV of case C, whose expectations are integrate()'s at this script's
# maximum, 5.6e-8, tolerance 1e-6. The BG/NBD of case J, found by the runner's fallback on the stable form,
# against the maximum written here: 3.8e-6 relative on the parameters, tolerance 1e-5.
REL_A <- 1e-7
LL_ABS <- 1e-6
REL_PREDICT <- 1e-7
REL_MBG <- 1e-5
REL_MBG_SUM <- 1e-6
REL_FALLBACK <- 1e-5

fitted_case <- function(id, data, params, expect, variant) {
  list(id = id, model = "clv", variant = variant, data = paste0("data/", data$file), types = clv_types(data$summary),
       params = params, expect = expect, refuses = NULL)
}

clv_params <- function(...) {
  utils::modifyList(list(customer = "customer", frequency = "frequency", recency = "recency", age = "age", time_unit = "week",
                         alpha = 0.05, seed = 1), list(...))
}

checks_ <- function(verdicts) unname(Map(function(k, v) txt_(paste0("assumption|", k, "||verdict"), v), names(verdicts), verdicts))

fitted <- list()

# A. BG/NBD customers (r 0.25, α 4, a 0.8, b 2.5), ages of 20 to 52 weeks in whole days, a 26-week holdout,
#    a Gamma–Gamma spend (6, 4, 15) independent of the frequency; `auto` with a spend: the three candidates
#    against their references, the choice, the spend, the clean checks.
A <- draw_customers(101, 400, "bg_nbd", c(0.25, 4, 0.8, 2.5), 140:364, holdout = 182)
A$file <- "clv_bgnbd.csv"
write_exact(A$summary, A$file)
cdA <- clv_log(A)
pnA <- pnbd(cdA, verbose = FALSE)
bgA <- bgnbd(cdA, verbose = FALSE)
ggA <- suppressWarnings(gg(cdA, verbose = FALSE))
mbA <- mbgnbd_max(A$summary)
llA <- c(pareto_nbd = as.numeric(logLik(pnA)), bg_nbd = as.numeric(logLik(bgA)), mbg_nbd = mbA$log_lik)
chosenA <- names(llA)[llA >= max(llA) - 1e-6][[1L]]
hA <- hc3(A$summary)
# the CLV of the model chosen (BG/NBD), its expectations from CLVTools' predict() week by week, not the runner's CET
stopifnot(identical(chosenA, "bg_nbd"))
cetA <- function(k) {
  pr <- predict(bgA, prediction.end = k, predict.spending = FALSE, verbose = FALSE)
  pr$CET[match(A$summary$customer, pr$Id)]
}
clvA <- clv_by_definition(cetA, 52, 0.1, 1, coef(ggA), A$summary)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv BG/NBD customers, auto with a spend and a holdout", A,
  clv_params(spend = "avg_spend", purchase_model = "auto", holdout_transactions = "holdout_purchases", holdout_length = 26, horizon = 52),
  c(list(txt_("model|||purchase_model", chosenA), txt_("model|||purchase_model_asked", "auto"),
         num_("model|||n", 400), num_("model|||n_repeaters", sum(A$summary$frequency > 0)),
         num_("model|||holdout_observed", sum(A$summary$holdout_purchases)), num_("model|||spend_n", sum(A$summary$frequency > 0)),
         num_("candidate|pareto_nbd||log_lik", llA[["pareto_nbd"]], abs = LL_ABS, from = "CLVTools pnbd()"),
         num_("candidate|bg_nbd||log_lik", llA[["bg_nbd"]], abs = LL_ABS, from = "CLVTools bgnbd()"),
         num_("candidate|mbg_nbd||log_lik", llA[["mbg_nbd"]], abs = LL_ABS, from = "MBG/NBD maximized here")),
    params_of("candidate|pareto_nbd||param_", coef(pnA), REL_A, "CLVTools pnbd()"),
    params_of("candidate|bg_nbd||param_", coef(bgA), REL_A, "CLVTools bgnbd()"),
    params_of("candidate|mbg_nbd||param_", mbA$par, REL_MBG, "MBG/NBD maximized here"),
    params_of("model|||spend_param_", stats::setNames(coef(ggA), c("p", "q", "gamma")), REL_A, "CLVTools gg()"),
    list(num_("model|||spend_log_lik", as.numeric(logLik(ggA)), abs = LL_ABS, from = "CLVTools gg()"),
         num_("assumption|spend_independence||value", hA$r, from = "cor()"), num_("assumption|spend_independence||p_value", hA$p, rel = 1e-8, from = "lm(), HC3"),
         num_("assumption|sample_size||value", 400), num_("assumption|sample_size||count", sum(A$summary$frequency > 0)),
         num_("assumption|horizon_length||value", 52 / max(A$summary$age)),
         num_("model|||clv_total", clvA, rel = REL_PREDICT, from = "CLVTools predict() week by week, the CLV's definition here")),
    checks_(c(convergence = "ok", frequency_fit = "ok", spend_independence = "ok", spend_tail = "ok"))),
  "value")

# B. Pareto/NBD customers (r 0.55, α 10, s 0.6, β 12), no spend, no holdout: the `purchases` variant; P(alive)
#    and the expected purchases against CLVTools' predict().
B <- draw_customers(202, 400, "pareto_nbd", c(0.55, 10, 0.6, 12), 140:364)
B$summary$avg_spend <- NULL
B$file <- "clv_pnbd.csv"
write_exact(B$summary, B$file)
pnB <- pnbd(clv_log(B), verbose = FALSE)
prB <- predict(pnB, prediction.end = 52, predict.spending = FALSE, verbose = FALSE)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv Pareto/NBD customers, the purchases variant", B, clv_params(purchase_model = "pareto_nbd", horizon = 52),
  c(list(txt_("model|||variant", "purchases"), txt_("model|||purchase_model", "pareto_nbd"), txt_("model|||optimizer", "L-BFGS-B"),
         num_("model|||log_lik", as.numeric(logLik(pnB)), abs = LL_ABS, from = "CLVTools pnbd()"),
         num_("model|||expected_transactions", sum(prB$CET), rel = REL_PREDICT, from = "CLVTools predict()"),
         num_("model|||mean_p_alive", mean(prB$PAlive), rel = REL_PREDICT, from = "CLVTools predict()")),
    params_of("model|||param_", coef(pnB), REL_A, "CLVTools pnbd()"),
    checks_(c(convergence = "ok", frequency_fit = "ok"))),
  "purchases")

# C. MBG/NBD customers (r 0.5, α 6, a 0.9, b 1.6) with a spend, the model asked for: against the likelihood
#    maximized here (and BTYDplus in make_witness.R); the spend against gg().
C <- draw_customers(303, 400, "mbg_nbd", c(0.5, 6, 0.9, 1.6), 140:364)
C$file <- "clv_mbgnbd.csv"
write_exact(C$summary, C$file)
mbC <- mbgnbd_max(C$summary)
ggC <- suppressWarnings(gg(clv_log(C), verbose = FALSE))
clvC <- clv_by_definition(function(k) mbgnbd_cet_integrate(mbC$par, k, C$summary$frequency, C$summary$recency, C$summary$age), 52, 0.08, 0.4,
                          coef(ggC), C$summary)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv MBG/NBD customers with a spend", C, clv_params(spend = "avg_spend", purchase_model = "mbg_nbd", discount_rate = 0.08, margin = 0.4),
  c(list(txt_("model|||purchase_model", "mbg_nbd"), num_("model|||log_lik", mbC$log_lik, abs = LL_ABS, from = "MBG/NBD maximized here"),
         num_("model|||horizon", 52), num_("model|||margin", 0.4), num_("model|||discount_per_period", 1.08^(7 / 365.25) - 1, rel = 1e-12),
         num_("model|||clv_total", clvC, rel = REL_MBG_SUM, from = "MBG/NBD maximized here, its CET by integrate(), the CLV's definition here")),
    params_of("model|||param_", mbC$par, REL_MBG, "MBG/NBD maximized here"),
    params_of("model|||spend_param_", stats::setNames(coef(ggC), c("p", "q", "gamma")), REL_A, "CLVTools gg()"),
    checks_(c(convergence = "ok", spend_independence = "ok"))),
  "value")

# D. One cohort: every customer known for 39 weeks (BG/NBD customers), a 13-week holdout; BG/NBD asked for,
#    its expectations against CLVTools' predict().
D <- draw_customers(404, 300, "bg_nbd", c(0.25, 4, 0.8, 2.5), 273, holdout = 91)
D$summary$avg_spend <- NULL
D$file <- "clv_equal_age.csv"
write_exact(D$summary, D$file)
bgD <- bgnbd(clv_log(D), verbose = FALSE)
prD <- predict(bgD, prediction.end = 13, predict.spending = FALSE, verbose = FALSE)
prD52 <- predict(bgD, prediction.end = 52, predict.spending = FALSE, verbose = FALSE)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv one cohort of equal ages, BG/NBD with a holdout", D,
  clv_params(purchase_model = "bg_nbd", holdout_transactions = "holdout_purchases", holdout_length = 13),
  c(list(txt_("model|||purchase_model", "bg_nbd"), txt_("model|||optimizer", "Nelder-Mead, BFGS"), num_("model|||median_age", 39, rel = 1e-12),
         num_("model|||log_lik", as.numeric(logLik(bgD)), abs = LL_ABS, from = "CLVTools bgnbd()"),
         num_("model|||holdout_observed", sum(D$summary$holdout_purchases)),
         num_("model|||holdout_expected", sum(prD$CET), rel = REL_PREDICT, from = "CLVTools predict()"),
         num_("assumption|holdout||value", (sum(prD$CET) - sum(D$summary$holdout_purchases)) / sum(D$summary$holdout_purchases), rel = REL_PREDICT * 10,
              from = "CLVTools predict()"),
         num_("model|||expected_transactions", sum(prD52$CET), rel = REL_PREDICT, from = "CLVTools predict()"),
         num_("model|||mean_p_alive", mean(prD52$PAlive), rel = REL_PREDICT, from = "CLVTools predict()")),
    params_of("model|||param_", coef(bgD), REL_A, "CLVTools bgnbd()"),
    checks_(c(convergence = "ok"))),
  "purchases")

# E. The average spend rises with the frequency (m̄ = 10 + 4x, times a log-normal noise): Gamma–Gamma's
#    assumption broken, spend_independence failed.
E <- draw_customers(505, 400, "mbg_nbd", c(0.5, 6, 0.9, 1.6), 140:364,
                    spend = function(x) (10 + 4 * x) * stats::rlnorm(length(x), 0, 0.3))
E$file <- "clv_dependent_spend.csv"
write_exact(E$summary, E$file)
hE <- hc3(E$summary)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv a spend that rises with the frequency", E, clv_params(spend = "avg_spend", purchase_model = "mbg_nbd"),
  c(list(num_("assumption|spend_independence||value", hE$r, from = "cor()"),
         num_("assumption|spend_independence||statistic", hE$z, rel = 1e-8, from = "lm(), HC3"),
         num_("assumption|spend_independence||p_value", hE$p, rel = 1e-6, abs = 1e-300, from = "lm(), HC3"),
         num_("assumption|spend_independence||count", hE$n), num_("assumption|spend_independence||threshold", 0.05),
         txt_("assumption|spend_independence||advice_id", "spend_independence")),
    checks_(c(spend_independence = "failed"))),
  "value")

# F. Customers who never drop out (Poisson purchases, rate ~ Gamma(0.6, 6)): MBG/NBD's a goes to its edge,
#    the boundary measure reads it «no dropout» (a value and no verdict), and the Hessian is not positive
#    definite there: the convergence check is the verdict, doubtful.
F <- draw_customers(606, 300, "poisson", c(0.6, 6), 140:364)
F$summary$avg_spend <- NULL
F$file <- "clv_no_dropout.csv"
write_exact(F$summary, F$file)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv customers who never drop out", F, clv_params(purchase_model = "mbg_nbd"),
  c(list(txt_("assumption|boundary|a|advice_id", "boundary_no_dropout"),
         txt_("assumption|convergence||verdict", "doubtful"), txt_("assumption|convergence||advice_id", "convergence_edge"))),
  "purchases")

# G. Two customers of 5,000 and 20,000 repeat purchases among 48 ordinary ones, average spends of millions (the
#    48 drawn from Gamma-Gamma and scaled by 1e6, the two at 1e7 and 3e6): the quadrature where GSL gives no
#    number, every number finite.
G <- draw_customers(707, 48, "bg_nbd", c(0.25, 4, 0.8, 2.5), 140:364)
G$summary$avg_spend <- G$summary$avg_spend * 1e6
G$summary <- rbind(data.frame(customer = c("huge1", "huge2"), frequency = c(5000, 20000), recency = c(99.9, 100), age = c(100, 100),
                              avg_spend = c(1e7, 3e6)), G$summary)
G$file <- "clv_huge.csv"
write_exact(G$summary, G$file)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv two customers of 5,000 and 20,000 purchases", G, clv_params(spend = "avg_spend", purchase_model = "bg_nbd", horizon = 26),
  list(txt_("model|||note_cet_quadrature", "@cet_quadrature n=2"), num_("model|||n", 50), num_("customer|huge2||rank", 1),
       num_("customer|huge1||rank", 2)),
  "value")

# H. Column names that look like code, 30 customers.
H <- draw_customers(808, 30, "mbg_nbd", c(0.5, 6, 0.9, 1.6), 140:364, prefix = "k")
names(H$summary) <- c("ü ñ", "x); system(\"id\")", "a`b", "T", "m")
H$file <- "clv_names.csv"
write_exact(H$summary, H$file)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv column names that look like code", H,
  list(customer = "ü ñ", frequency = "x); system(\"id\")", recency = "a`b", age = "T", spend = "m", time_unit = "month", purchase_model = "mbg_nbd",
       seed = 1),
  list(num_("model|||n", 30), txt_("model|||time_unit", "month"), txt_("model|||formula", "x: frequency, t_x: recency, T: age, in months")),
  "value")

# J. BG/NBD customers whose fit by CLVTools' own settings ends in numerical noise (B10): optimx's Nelder–Mead reaches the
#    maximum, then its BFGS steps to r ≈ 1e35, α ≈ 1e67, where CLVTools' sum r·log α − (r + x)·log(α + T), two terms of
#    1e37 that cancel, returns a log-likelihood of noise far above the maximum, and the expected purchases are 0. The
#    parameters are those of the sample data's customers (r 0.9, α 7.9, a 0.37, b 6.45), the ages more of them young;
#    seed 21 is the first of the draws below that fails this way (measured on CLVTools 0.12.1: a numerical accident, so a
#    given draw is what reproduces it). The runner's guard sees it (the sum is not the customers' sum in the stable
#    form), falls back on the stable likelihood and finds the maximum computed here to the end, with a form written here.
J <- list(summary = draw_continuous(21, 300, c(0.9, 7.9, 0.37, 6.45)), file = "clv_bgnbd_noise.csv")
write_exact(J$summary, J$file)
mJ <- bgnbd_max(J$summary)
fitted[[length(fitted) + 1L]] <- fitted_case(
  "clv BG/NBD where CLVTools' sum turns to numerical noise", J, clv_params(purchase_model = "bg_nbd"),
  c(list(txt_("model|||purchase_model", "bg_nbd"), txt_("model|||optimizer", "Nelder-Mead, L-BFGS-B (fallback)"),
         txt_("model|||note_optimizer_fallback", "@optimizer_fallback"), num_("model|||converged", 1),
         num_("model|||log_lik", mJ$log_lik, abs = LL_ABS, from = "BG/NBD maximized here, the stable form")),
    params_of("model|||param_", mJ$par, REL_FALLBACK, "BG/NBD maximized here, the stable form"),
    checks_(c(convergence = "ok"))),
  "purchases")

# I. A spend drawn with q = 0.8: no finite mean, the node stops; q as CLVTools' gg() finds it.
I <- draw_customers(909, 200, "mbg_nbd", c(0.5, 6, 0.9, 1.6), 140:364, gg = c(6, 0.8, 15))
I$file <- "clv_spend_no_mean.csv"
write_exact(I$summary, I$file)
qI <- coef(suppressWarnings(gg(clv_log(I), verbose = FALSE)))[[2L]]
fitted[[length(fitted) + 1L]] <- list(
  id = "refusal spend without a finite mean", model = "clv", variant = "value", data = paste0("data/", I$file), types = clv_types(I$summary),
  params = clv_params(spend = "avg_spend", purchase_model = "mbg_nbd"),
  refuses = list(code = "spend_no_mean", message = paste0("the spend model's q is ", format(round(qI, 2), trim = TRUE, scientific = FALSE), ", at most 1: the average spend has no ",
                                                          "finite mean, and the CLV cannot be computed; look for extreme amounts, or leave out the spend column")))

cases <- c(cases, fitted)
cases <- lapply(cases, function(k) c(k, list(by = "make_reference.R")))

# expected.json keeps the cases another script wrote (`by`), after this script's
path <- file.path(here, "expected.json")
others <- if (file.exists(path)) Filter(function(k) !identical(k$by, "make_reference.R") && !is.null(k$by),
                                        jsonlite::fromJSON(path, simplifyVector = FALSE)$cases) else list()
expected <- list(
  format_version = 1,
  generated = list(by = "make_reference.R", note = paste0("lot B1 wrote the refusal cases, lot B2 the fitted cases of clv; lot B3 adds BG/BB's with its own ",
                                                           "script (docs/specs/customer-lifetime-models.md, «Reference values»)"),
                   R = paste(R.version$major, R.version$minor, sep = "."),
                   packages = list(jsonlite = as.character(utils::packageVersion("jsonlite")), CLVTools = as.character(utils::packageVersion("CLVTools")),
                                   data.table = as.character(utils::packageVersion("data.table")))),
  cases = c(cases, others))
writeLines(jsonlite::toJSON(expected, auto_unbox = TRUE, pretty = TRUE, null = "null", digits = NA), path, useBytes = TRUE)
cat("wrote", length(cases), "cases of make_reference.R and kept", length(others), "of other scripts\n")
