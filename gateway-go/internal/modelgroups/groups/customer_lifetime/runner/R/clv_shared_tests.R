# The self-test of the shared module (docs/specs/customer-lifetime-models.md, lot B1): added to
# TAB_COMMON_TESTS, so that `Rscript runner/run.R --selftest` runs them after the common library's.
# Data are made here from fixed seeds (no CDNOW, no donations: they are never copied into the
# repository); the values «computed outside» are those of BTYD 2.4.3 and BTYDplus 1.2.0, run by hand
# on the same parameters and customers (reference/make_witness.R does the same on a larger scale).

# ── helpers ──────────────────────────────────────────────────────────────────────
.clv_rel <- function(got, want) max(abs(got - want) / pmax(abs(want), 1e-300))

.clv_task <- function(params, frame, node = "clv", model = node, variant = "value", limits = tab_catalog()$limits) {
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "customer_lifetime", group_version = "selftest", model = model, variant = variant,
       params = params, output = params$output %||% "results", write_output = FALSE, rows_from = "run", limits = limits, .frame = frame)
}

# The summary of customers made by the models' own processes, in whole days (so that CLVTools' public fit can be run on
# a transaction log rebuilt from it). Returns the purchase days of each customer and its age in days.
.clv_sim_days <- function(model, n, par, days = 140:364) {
  age <- sample(days, n, replace = TRUE)
  lapply(seq_len(n), function(i) {
    weeks <- age[[i]] / 7
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
    list(age = age[[i]], days = sort(d[d <= age[[i]]]))
  })
}

.clv_summary_of_days <- function(sim) {
  data.frame(x = vapply(sim, function(z) length(z$days), 0), tx = vapply(sim, function(z) if (length(z$days)) max(z$days) / 7 else 0, 0),
             T = vapply(sim, function(z) z$age / 7, 0))
}

# Gamma–Gamma spend of the repeaters: a customer's mean spend is drawn from the model.
.clv_sim_spend <- function(x, p = 6, q = 4, gamma = 15) {
  nu <- stats::rgamma(length(x), q, rate = gamma)
  ifelse(x > 0, stats::rgamma(length(x), shape = p * pmax(x, 1), rate = nu * pmax(x, 1)), NA_real_)
}

.clv_sim_bgbb <- function(n, par, opportunities) {
  p <- stats::rbeta(n, par[[1L]], par[[2L]])
  theta <- stats::rbeta(n, par[[3L]], par[[4L]])
  alive_for <- pmin(stats::rgeom(n, theta), opportunities)
  x <- tx <- numeric(n)
  for (i in seq_len(n)) {
    if (alive_for[[i]] > 0) {
      bought <- stats::rbinom(alive_for[[i]], 1, p[[i]])
      x[[i]] <- sum(bought)
      tx[[i]] <- if (any(bought == 1)) max(which(bought == 1)) else 0
    }
  }
  data.frame(x = x, tx = tx, T = opportunities)
}

.clv_params <- function(extra = list(), node = "clv") {
  base <- if (node == "clv") list(customer = "id", frequency = "x", recency = "tx", age = "T", time_unit = "week", seed = 1) else
    list(customer = "id", frequency = "x", recency = "tx", opportunities = "T", period_unit = "month", seed = 1)
  utils::modifyList(base, extra)
}

# 40 customers, valid, with a spend and a holdout: the table the refusals are made from.
.clv_tiny <- function(node = "clv") {
  set.seed(3)
  n <- 40L
  if (node == "clv") {
    age <- round(stats::runif(n, 10, 50), 1)
    x <- stats::rpois(n, 2)
    tx <- ifelse(x > 0, round(age * stats::runif(n, 0.2, 1), 1), 0)
  } else {
    age <- rep(6, n)
    x <- pmin(stats::rpois(n, 2), 6)
    tx <- ifelse(x > 0, pmin(pmax(x, ceiling(stats::runif(n, 0.2, 1) * 6)), 6), 0)
  }
  data.frame(id = paste0("c", seq_len(n)), x = x, tx = tx, T = age, m = ifelse(x > 0, round(stats::runif(n, 5, 50), 2), NA_real_),
             h = stats::rpois(n, 1), stringsAsFactors = FALSE)
}

.clv_with <- function(df, i, ...) {
  for (nm in names(list(...))) df[[nm]][[i]] <- list(...)[[nm]]
  df
}

.clv_refused <- function(code, params, frame, node = "clv") {
  task <- .clv_task(params, frame, node)
  .tab_refused_with(code, clv_read_summary(task, node, frame))
}

# ── the tests ────────────────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["customer_lifetime: the summary's rules, each refusal with its sentence on a tiny table"]] <- function(code) {
  df <- .clv_tiny()
  base <- .clv_params(list(spend = "m", holdout_transactions = "h", holdout_length = 26))
  s <- clv_read_summary(.clv_task(base, df), "clv", df)
  .tab_expect(s$n == 40L && s$n_missing == 0L && s$has_spend && s$has_holdout && s$unit == "week", "the valid table")
  say <- function(code, frame, params = base, node = "clv") .clv_refused(code, params, frame, node)
  m <- say("duplicate_customer", .clv_with(df, 5, id = "c9"))
  .tab_expect(m == "the customer c9 appears on 2 rows: one row per customer; group the transactions by customer first", m)
  .tab_expect(say("frequency_not_whole", .clv_with(df, 4, x = 2.5)) == paste0("frequency must count purchases: the customer c4 has 2.5"), "frequency_not_whole")
  m <- say("negative_value", .clv_with(df, 17, tx = -3))
  .tab_expect(m == "recency, age and frequency are 0 or more: the customer c17 has a recency of −3", m)
  m <- say("age_zero", .clv_with(df, 6, T = 0, tx = 0, x = 0))
  .tab_expect(m == "the customer c6 has an age of 0: age is the time since the first purchase, more than 0", m)
  d <- .clv_with(.clv_with(df, 17, x = 3, tx = 41), 17, T = 39)
  m <- say("recency_after_age", d)
  .tab_expect(m == "the customer c17 bought last (recency 41) after the end of the period (age 39): recency is at most age", m)
  m <- say("recency_without_purchase", .clv_with(.clv_with(df, 17, x = 0), 17, tx = 12))
  .tab_expect(m == "the customer c17 has no repeat purchase but a recency of 12: recency is 0 without repeat purchases", m)
  m <- say("same_moment", .clv_with(.clv_with(df, 17, x = 2), 17, tx = 0))
  .tab_expect(m == paste0("the customer c17 has 2 repeat purchases at recency 0: purchases of the same day must be one purchase; ",
                          "group by customer and day first"), m)
  r <- which(df$x > 0)[[1L]]
  m <- say("spend_not_positive", .clv_with(df, r, m = 0))
  .tab_expect(grepl("has an average spend of 0 with repeat purchases: the spend model needs amounts above 0", m, fixed = TRUE), m)
  m <- say("holdout_negative", .clv_with(df, 17, h = -1))
  .tab_expect(m == "holdout purchases are whole numbers, 0 or more: the customer c17 has −1", m)
  # the count of customers that break the same rule
  d <- df
  d$x[c(3, 8, 9)] <- 0.5
  m <- say("frequency_not_whole", d)
  .tab_expect(grepl("the customer c3 has 0.5 (3 customers in all break this rule)", m, fixed = TRUE), m)
  # counts
  m <- say("few_customers", df[1:12, ])
  .tab_expect(m == "12 customers: these models need at least 20", m)
  none <- df
  none$x <- 0
  none$tx <- 0
  m <- say("no_repeaters", none)
  .tab_expect(grepl("no customer bought again", m, fixed = TRUE), m)
  few <- df
  few$x[-(1:3)] <- 0
  few$tx[-(1:3)] <- 0
  few$x[1:3] <- 1
  few$tx[1:3] <- 1
  m <- say("few_repeaters", few)
  .tab_expect(m == "3 customers bought again: these models need at least 5", m)
  nospend <- df
  nospend$m[which(nospend$x > 0)[-(1:3)]] <- NA
  m <- say("few_spenders", nospend)
  .tab_expect(m == "3 repeat customers have a spend: the spend model needs at least 5", m)
  # NULLs: a row with a NULL key column is left out and counted; a missing spend is noted
  d <- df
  d$id[2] <- NA
  d$tx[7] <- NA
  d$m[which(d$x > 0)[1]] <- NA
  s <- clv_read_summary(.clv_task(base, d), "clv", d)
  .tab_expect(s$n == 38L && s$n_missing == 2L && !2L %in% s$row && !7L %in% s$row, "the rows with a NULL")
  .tab_expect(identical(s$notes$spend_missing, "@spend_missing n=1"), "the note spend_missing")
  # the rounding of a recency above the age is clamped; an absurd age is questioned
  d <- df
  d$x[1] <- 3
  d$tx[1] <- d$T[1] * (1 + 1e-12)
  s <- clv_read_summary(.clv_task(base, d), "clv", d)
  .tab_expect(s$tx[[1L]] == s$T[[1L]] && identical(s$notes$t_x_clamped, "@t_x_clamped n=1"), "the clamp")
  d$T[2] <- 2000
  d$tx[2] <- 0
  d$x[2] <- 0
  s <- clv_read_summary(.clv_task(base, d), "clv", d)
  .tab_expect(identical(s$notes$unit_check, "@unit_check age=2000 unit=week") && is.null(clv_read_summary(.clv_task(base, df), "clv", df)$notes$unit_check), "the note unit_check")
  # an infinite number stops the node and names the column; a missing column, a text column
  d <- df
  d$m[which(d$x > 0)[1]] <- Inf
  m <- say("infinite", d)
  .tab_expect(grepl("«m»", m, fixed = TRUE), m)
  m <- say("missing_column", df[, setdiff(names(df), "h")])
  .tab_expect(grepl("no column «h»", m, fixed = TRUE), m)
  d <- df
  d$x <- as.character(d$x)
  .tab_expect(grepl("«x» is not a number", say("not_a_number", d), fixed = TRUE), "a text column")
  # BG/BB
  bb <- .clv_tiny("bg_bb")
  pb <- .clv_params(list(spend = "m", holdout_transactions = "h", holdout_length = 5), "bg_bb")
  s <- clv_read_summary(.clv_task(pb, bb, "bg_bb"), "bg_bb", bb)
  .tab_expect(s$n == 40L && s$unit == "month", "the valid BG/BB table")
  m <- say("bgbb_opportunities", .clv_with(bb, 17, T = 0), pb, "bg_bb")
  .tab_expect(m == "opportunities run from 1 to 1,000: the customer c17 has 0", m)
  m <- say("bgbb_frequency", .clv_with(.clv_with(.clv_with(bb, 17, x = 4), 17, tx = 3), 17, T = 6), pb, "bg_bb")
  .tab_expect(m == "the customer c17 has 4 transactions in 3 opportunities up to its last: at most one transaction per opportunity", m)
  m <- say("recency_after_age", .clv_with(.clv_with(bb, 17, x = 1), 17, tx = 8), pb, "bg_bb")
  .tab_expect(grepl("bought last at the opportunity 8, after the last of its 6 opportunities", m, fixed = TRUE), m)
  m <- say("bgbb_holdout", .clv_with(bb, 17, h = 7), pb, "bg_bb")
  .tab_expect(grepl("has 7 in the 5 opportunities of the holdout", m, fixed = TRUE), m)
  .tab_expect(identical(say("bgbb_recency", .clv_with(bb, 17, tx = 2.5), pb, "bg_bb") != "", TRUE), "a recency that is not whole")
}

TAB_COMMON_TESTS[["customer_lifetime: the five models' functions equal the values computed outside (BTYD, BTYDplus)"]] <- function(code) {
  x <- c(0, 1, 3, 7, 12, 2)
  tx <- c(0, 10.5, 30.2, 38.9, 20, 2.5)
  T <- c(39, 39, 38.4, 39, 52, 26.5)
  same <- function(got, want, tol = 1e-12) .tab_expect(.clv_rel(got, want) < tol, "relative difference ", .clv_rel(got, want), " (", paste(signif(got, 6), collapse = " "), ")")
  # MBG/NBD against BTYDplus 1.2.0 (k = 1)
  pm <- c(0.52, 6.2, 0.89, 1.6)
  same(clv_mbg_ll_ind(pm, x, tx, T), c(-0.53419064401754024, -5.2966908900890362, -12.531378125583688, -22.189038635368586, -26.406957801702326, -6.9587006850217303))
  same(clv_palive("mbg_nbd", pm, x, tx, T), c(0.39020099902662153, 0.39140717605493275, 0.71655746104068785, 0.90479194048240374, 0.00069846051156389284, 0.12574198109219067))
  same(as.vector(clv_cet("mbg_nbd", pm, 10, x, tx, T)), c(0.043102324104150068, 0.12493638422927515, 0.52987894309328865, 1.3972160880594311, 0.0014108733247611401, 0.089425028421996414))
  same(vapply(0:3, function(k) clv_pmf("mbg_nbd", pm, k, 39), 0), c(0.58614349696369217, 0.1819879776300091, 0.086154989168441151, 0.048031422282562429))
  same(vapply(0:3, function(k) clv_pmf("mbg_nbd", pm, k, 7.5), 0), c(0.78290038103674875, 0.14559572016677397, 0.044273762068260933, 0.016012207596311813))
  # BG/BB against BTYD 2.4.3
  pb <- c(1.2, 0.75, 0.66, 2.78)
  xb <- c(0, 1, 2, 6, 3, 2)
  txb <- c(0, 1, 4, 6, 3, 6)
  nb <- c(6, 6, 6, 6, 5, 6)
  same(clv_bgbb_ll_ind(pb, xb, txb, nb), c(-1.1625639054542731, -2.2864009934603233, -5.1966762028897007, -2.1883824072657201, -3.3832115186207594, -5.6545351928547101))
  same(clv_palive("bg_bb", pb, xb, txb, nb), c(0.10775606065377981, 0.069188091852525446, 0.58840572385327483, 0.93008474576271161, 0.22250894932209631, 0.9300847457627115))
  same(as.vector(clv_cet("bg_bb", pb, 5, xb, txb, nb)), c(0.07238402793633919, 0.085206716254022666, 1.0540146845232283, 3.7486492998344061, 0.59162974621254716, 1.6660663554819621))
  same(vapply(0:6, function(k) clv_pmf("bg_bb", pb, k, 6), 0), c(0.31268346144170794, 0.1703954898766139, 0.12143022088959676, 0.10004715120659891, 0.091324397225993392, 0.092021348522738369, 0.11209793083675047))
  same(vapply(0:3, function(k) clv_pmf("bg_bb", pb, k, 3), 0), c(0.35313796587583057, 0.22575512631648686, 0.19634414481562756, 0.22476276299205494))
  near <- c(1.2, 0.75, 1.00001, 2.78)
  same(clv_bgbb_cet(near, 5, xb, txb, nb), c(0.03651799424183351, 0.044463616069753117, 0.79876694650102797, 3.4249442562996166, 0.37259403672726271, 1.5221974472442763), 1e-7)
  # the limit at γ = 1: the closed form's value at 1 ± h is the limit within O(h²), and the limit form does not cancel
  lim <- clv_bgbb_cet(c(1.2, 0.75, 1, 2.78), 5, xb, txb, nb)
  mid <- (clv_bgbb_cet(c(1.2, 0.75, 1.002, 2.78), 5, xb, txb, nb) + clv_bgbb_cet(c(1.2, 0.75, 0.998, 2.78), 5, xb, txb, nb)) / 2
  .tab_expect(.clv_rel(lim, mid) < 1e-5 && all(is.finite(lim)), "the limit at gamma = 1: ", .clv_rel(lim, mid))
  for (g in c(1 - 1e-6, 1 + 1e-6, 1 - 5e-5, 1 + 5e-5)) {
    .tab_expect(.clv_rel(clv_bgbb_cet(c(1.2, 0.75, g, 2.78), 5, xb, txb, nb), lim) < 1e-4, "no continuity near gamma = 1: ", g)
  }
  # Pareto/NBD and BG/NBD through CLVTools' functions, against BTYD
  pp <- c(0.55, 10.6, 0.61, 11.7)
  same(clv_ll_ind("pareto_nbd", pp, x, tx, T), c(-0.51734381867525081, -5.355056194336635, -12.477026676993459, -21.918064040538439, -26.965496588005514, -7.0774119964192979))
  same(clv_palive("pareto_nbd", pp, x, tx, T), c(0.29350897785858743, 0.41067114850419145, 0.85971184247323629, 0.99878717379167359, 0.0018226551990295926, 0.17902960131695683))
  same(as.vector(clv_cet("pareto_nbd", pp, 10, x, tx, T)), c(0.030772318192425208, 0.12133945108503891, 0.58853320164837253, 1.4374609093865689, 0.0034924699483862448, 0.11440946577173772))
  same(vapply(0:3, function(k) clv_pmf("pareto_nbd", pp, k, 39), 0), c(0.59610180146520952, 0.16514897070905415, 0.082045412074062296, 0.048682241391075562))
  pg <- c(0.24, 4.4, 0.79, 2.4)
  same(clv_ll_ind("bg_nbd", pg, x, tx, T), c(-0.54932517604371456, -5.2253208115347682, -12.573986467154036, -22.252155243493061, -26.259454108125688, -6.996458578440043))
  same(clv_palive("bg_nbd", pg, x, tx, T), c(1, 0.44658195722329158, 0.73657247480212384, 0.91271561469199269, 0.00059595580059623146, 0.13024569620579945))
  same(as.vector(clv_cet("bg_nbd", pg, 10, x, tx, T)), c(0.053474018491892684, 0.12186725415378225, 0.52553392680170141, 1.4217608272909998, 0.0012210254365844283, 0.087740987955624308))
  same(vapply(0:3, function(k) clv_pmf("bg_nbd", pg, k, 39), 0), c(0.57733928133148982, 0.1983495010047176, 0.08464411232147534, 0.045350616580884517))
  # the negative sum CLVTools minimizes is the sum of the individual log-likelihoods
  comp <- clv_compress(x, tx, T)
  .tab_expect(abs(clv_nll("pareto_nbd", comp)(log(pp)) + sum(clv_ll_ind("pareto_nbd", pp, x, tx, T))) < 1e-9, "LL_sum")
  .tab_expect(abs(clv_nll("mbg_nbd", comp)(log(pm)) + sum(clv_mbg_ll_ind(pm, x, tx, T))) < 1e-9, "the MBG/NBD sum")
  # every PMF adds up to 1
  for (m in c("pareto_nbd", "bg_nbd", "mbg_nbd")) {
    par <- if (m == "pareto_nbd") pp else if (m == "bg_nbd") pg else pm
    total <- Reduce(`+`, lapply(0:150, function(k) clv_pmf(m, par, k, c(5, 39))))
    .tab_expect(max(abs(total - 1)) < 1e-9, m, " PMF adds up to ", paste(total, collapse = ", "))
  }
  total <- Reduce(`+`, lapply(0:6, function(k) clv_pmf("bg_bb", pb, k, 6)))
  .tab_expect(abs(total - 1) < 1e-12, "BG/BB PMF adds up to ", total)
  # Gamma–Gamma's expected spend (BTYD's spend.expected.value)
  same(clv_expected_spend(c(p = 6.25, q = 3.74, gamma = 15.44), c(3, 5, 1), c(1.2, 30, 50)), c(5.5374592833876219, 30.420711974110031, 45.49499443826474))
  .tab_expect(abs(clv_expected_spend(c(p = 6.25, q = 3.74, gamma = 15.44), 0, NA) - 6.25 * 15.44 / 2.74) < 1e-12, "the population's spend")
}

TAB_COMMON_TESTS[["customer_lifetime: GSL's 2F1 and the Gauss-Jacobi quadrature give the same expected transactions"]] <- function(code) {
  g <- clv_gauss_beta(0.9, 2.4)
  .tab_expect(abs(sum(g$w) - 1) < 1e-13 && length(g$p) == 64L && all(g$p > 0 & g$p < 1), "weights and nodes")
  .tab_expect(abs(sum(g$w * g$p) - 0.9 / 3.3) < 1e-13 && abs(sum(g$w * g$p^2) - 0.9 * 1.9 / (3.3 * 4.3)) < 1e-13, "moments of the Beta weight")
  gs <- clv_gauss_beta(0.3, 0.5)
  .tab_expect(abs(sum(gs$w) - 1) < 1e-12 && abs(sum(gs$w * gs$p) - 0.3 / 0.8) < 1e-12, "A + B < 1")
  x <- c(0, 1, 2, 5, 12, 40, 0, 3)
  tx <- c(0, 4.2, 30, 36.5, 38.9, 39, 0, 1)
  T <- c(39, 39, 38.4, 39, 39, 39, 12.5, 52)
  for (par in list(c(0.24, 4.4, 0.79, 2.4), c(0.5, 6, 0.9, 1.6), c(0.3, 3, 120.5, 8))) {
    for (t in c(1, 39, 520)) {
      pa <- clv_palive("bg_nbd", par, x, tx, T)
      closed <- clv_fn("bgnbd_nocov_CET")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], t, x, tx, T)
      quad <- clv_cet_quadrature(par, t, x, T, pa, 0)
      ok <- is.finite(closed)
      .tab_expect(.clv_rel(quad[ok], closed[ok]) < 1e-10, "BG/NBD CET(", t, ") by quadrature: ", .clv_rel(quad[ok], closed[ok]))
      pam <- clv_palive("mbg_nbd", par, x, tx, T)
      closed <- clv_mbg_cet_closed(par, t, x, tx, T)
      quad <- clv_cet_quadrature(par, t, x, T, pam, 1)
      ok <- is.finite(closed)
      .tab_expect(.clv_rel(quad[ok], closed[ok]) < 1e-10, "MBG/NBD CET(", t, ") by quadrature: ", .clv_rel(quad[ok], closed[ok]))
    }
  }
  # where GSL gives NaN (x of thousands), and where a = 1 divides by zero: finite, by the quadrature
  big <- c(5000, 20000, 3)
  txb <- c(99.9, 100, 2)
  Tb <- c(100, 100, 100)
  for (m in c("bg_nbd", "mbg_nbd")) {
    ce <- clv_cet(m, c(0.24, 4.4, 0.79, 2.4), 52, big, txb, Tb)
    .tab_expect(all(is.finite(ce)) && sum(attr(ce, "quadrature")) >= 2L && ce[[1L]] > 2000 && ce[[2L]] > 8000, m, ": huge x ", paste(ce, collapse = " "))
    one <- clv_cet(m, c(0.24, 4.4, 1, 2.4), 52, x, tx, T)
    near <- clv_cet(m, c(0.24, 4.4, 1 + 1e-6, 2.4), 52, x, tx, T)
    .tab_expect(all(attr(one, "quadrature")) && all(is.finite(one)) && .clv_rel(as.vector(one), as.vector(near)) < 1e-4, m, ": a = 1")
  }
  # the likelihood of the huge customers is finite
  .tab_expect(all(is.finite(clv_ll_ind("pareto_nbd", c(0.55, 10.6, 0.61, 11.7), big, txb, Tb))), "the huge customers' likelihood")
  .tab_expect(all(is.finite(clv_ll_ind("bg_nbd", c(0.24, 4.4, 0.79, 2.4), big, txb, Tb))), "BG/NBD's huge customers' likelihood")
}

TAB_COMMON_TESTS[["customer_lifetime: the estimates equal CLVTools' public pnbd(), bgnbd() and gg() on the log rebuilt from a whole-day case"]] <- function(code) {
  suppressMessages({
    library(CLVTools)
    library(data.table)
  })
  set.seed(11)
  sim <- .clv_sim_days("bg_nbd", 260, c(0.25, 4, 0.8, 2.5))
  S <- .clv_summary_of_days(sim)
  m <- .clv_sim_spend(S$x)
  # route A: a transaction log with the same summary (the first purchase at the age before the split, the repeats on their days)
  split <- as.Date("2020-01-01")
  log_ <- rbindlist(lapply(seq_along(sim), function(i) {
    d <- c(0, sim[[i]]$days)
    data.table(Id = as.character(i), Date = split - sim[[i]]$age + d, Price = c(10, rep(if (is.na(m[[i]])) 1 else m[[i]], length(d) - 1L)))
  }))
  log_ <- rbind(log_, data.table(Id = "1", Date = split + 30, Price = 5)) # CLVTools refuses a split after the last transaction
  cd <- clvdata(log_, date.format = "ymd", time.unit = "weeks", estimation.split = split, name.id = "Id", name.date = "Date", name.price = "Price")
  pn <- pnbd(cd, verbose = FALSE)
  bg <- bgnbd(cd, verbose = FALSE)
  gm <- suppressWarnings(gg(cd, verbose = FALSE))
  cb <- pn@cbs[match(as.character(seq_along(sim)), pn@cbs$Id)]
  .tab_expect(isTRUE(all.equal(cb$x, S$x)) && isTRUE(all.equal(cb$t.x, S$tx)) && isTRUE(all.equal(cb$T.cal, S$T)), "the log does not give the summary")
  s <- list(x = S$x, tx = S$tx, T = S$T, m = m, n = nrow(S))
  f <- clv_fit_purchases("pareto_nbd", s)
  .tab_expect(.clv_rel(f$par, coef(pn)) < 1e-9, "Pareto/NBD: ", .clv_rel(f$par, coef(pn)))
  .tab_expect(.clv_rel(f$se, sqrt(diag(vcov(pn)))) < 1e-5, "Pareto/NBD standard errors: ", .clv_rel(f$se, sqrt(diag(vcov(pn)))))
  .tab_expect(abs(f$log_lik - as.numeric(logLik(pn))) < 1e-6, "Pareto/NBD log-likelihood")
  f2 <- clv_fit_purchases("bg_nbd", s)
  .tab_expect(.clv_rel(f2$par, coef(bg)) < 1e-9, "BG/NBD: ", .clv_rel(f2$par, coef(bg)))
  .tab_expect(.clv_rel(f2$se, sqrt(diag(vcov(bg)))) < 1e-5, "BG/NBD standard errors")
  s$has_spend <- TRUE
  f3 <- clv_fit_spend(s)
  .tab_expect(.clv_rel(f3$par, coef(gm)) < 1e-9, "Gamma-Gamma: ", .clv_rel(f3$par, coef(gm)))
  .tab_expect(.clv_rel(f3$se, sqrt(diag(vcov(gm)))) < 1e-5, "Gamma-Gamma standard errors")
  # the same customers, predicted by CLVTools' public predict() and by the functions of the runner
  pr <- predict(bg, prediction.end = 26, verbose = FALSE)
  pr <- pr[match(as.character(seq_along(sim)), pr$Id)]
  ce <- as.vector(clv_cet("bg_nbd", f2$par, 26, S$x, S$tx, S$T))
  .tab_expect(.clv_rel(ce[pr$CET > 1e-6], pr$CET[pr$CET > 1e-6]) < 1e-7, "BG/NBD CET against predict()")
  .tab_expect(.clv_rel(clv_palive("bg_nbd", f2$par, S$x, S$tx, S$T), pr$PAlive) < 1e-7, "BG/NBD P(alive) against predict()")
  # MBG/NBD, written here, recovers its own process and is the BG/NBD where it leaves nobody after the first purchase
  set.seed(12)
  sm <- .clv_summary_of_days(.clv_sim_days("mbg_nbd", 500, c(0.5, 6, 0.9, 1.6)))
  fm <- clv_fit_purchases("mbg_nbd", list(x = sm$x, tx = sm$tx, T = sm$T, n = nrow(sm)))
  .tab_expect(fm$converged && all(is.finite(fm$se)) && fm$par[["r"]] > 0.2 && fm$par[["r"]] < 1.5, "MBG/NBD fit on its own process: ", paste(signif(fm$par, 3), collapse = " "))
}

TAB_COMMON_TESTS[["customer_lifetime: the optimizer's fallback, the standard errors and the choice of the model"]] <- function(code) {
  # customers who all stopped at once after their first purchases: CLVTools' own settings fail on Pareto/NBD
  n <- 200
  x <- rep(c(1, 2), n / 2)
  s <- list(x = x, tx = rep(0.001, n), T = rep(52, n), n = n)
  f <- clv_fit_purchases("pareto_nbd", s)
  .tab_expect(isTRUE(f$fallback) && all(is.finite(f$par)) && is.finite(f$log_lik) && grepl("fallback", f$optimizer, fixed = TRUE), "the fallback was not used")
  # an ordinary case needs none, and the standard errors are the delta method of the log-scale covariance
  set.seed(5)
  sm <- .clv_summary_of_days(.clv_sim_days("bg_nbd", 300, c(0.25, 4, 0.8, 2.5)))
  s2 <- list(x = sm$x, tx = sm$tx, T = sm$T, n = nrow(sm))
  f2 <- clv_fit_purchases("bg_nbd", s2, alpha = 0.1)
  .tab_expect(!f2$fallback && f2$converged && !is.null(f2$vcov_log), "an ordinary fit")
  v <- f2$vcov_log
  .tab_expect(.clv_rel(f2$se, sqrt(diag(diag(f2$par) %*% v %*% diag(f2$par)))) < 1e-12, "delta method")
  z <- stats::qnorm(0.95)
  .tab_expect(.clv_rel(f2$ci_low, exp(f2$log_par - z * sqrt(diag(v)))) < 1e-12 && all(f2$ci_low > 0 & f2$ci_low < f2$par & f2$par < f2$ci_high), "intervals on the log scale")
  .tab_expect(abs(f2$aic - (8 - 2 * f2$log_lik)) < 1e-9 && abs(f2$bic - (4 * log(s2$n) - 2 * f2$log_lik)) < 1e-9, "AIC and BIC")
  # `auto` keeps the highest log-likelihood, a tie the first, and a model out of any plausible range is not chosen
  cand <- function(ll, par = rep(1, 4), conv = TRUE) list(p = list(log_lik = ll, par = par, converged = conv))
  .tab_expect(clv_choose(list(cand(-10), cand(-9), cand(-11)), "auto") == 2L, "the highest")
  .tab_expect(clv_choose(list(cand(-9), cand(-9 + 1e-8), cand(-11)), "auto") == 1L, "a tie")
  .tab_expect(clv_choose(list(cand(-10), cand(7e108, par = c(1e120, 1, 1, 1)), cand(-11)), "auto") == 1L, "noise")
  .tab_expect(clv_choose(list(cand(-10, conv = FALSE), cand(-12), cand(-11, conv = FALSE)), "auto") == 2L, "converged first")
  .tab_expect(clv_choose(list(cand(-10), cand(-9)), "mbg_nbd") == 1L, "a model asked for")
  .tab_expect(identical(clv_candidates("clv", list(purchase_model = "auto")), c("pareto_nbd", "bg_nbd", "mbg_nbd")) &&
              identical(clv_candidates("clv", list(purchase_model = "bg_nbd", compare = TRUE)), c("bg_nbd", "pareto_nbd", "mbg_nbd")) &&
              identical(clv_candidates("clv", list(purchase_model = "bg_nbd")), "bg_nbd") && identical(clv_candidates("bg_bb", list()), "bg_bb"), "the candidates")
  # BG/BB fits its own process
  set.seed(21)
  sb <- .clv_sim_bgbb(1500, c(1.2, 0.75, 0.66, 2.78), 6)
  fb <- clv_fit_purchases("bg_bb", list(x = sb$x, tx = sb$tx, T = sb$T, n = nrow(sb)))
  .tab_expect(fb$converged && max(abs(log(fb$par / c(1.2, 0.75, 0.66, 2.78)))) < 0.5, "BG/BB: ", paste(signif(fb$par, 3), collapse = " "))
  # a spend whose q is at most 1 has no mean: refused with the way out
  set.seed(8)
  xs <- stats::rpois(120, 2) + 1
  nu <- stats::rgamma(120, 0.8, rate = 15)
  ms <- stats::rgamma(120, shape = 6 * xs, rate = nu * xs)
  m <- .tab_refused_with("spend_no_mean", clv_fit_spend(list(x = xs, m = ms, n = 120L)))
  .tab_expect(grepl("at most 1: the average spend has no finite mean", m, fixed = TRUE) && grepl("leave out the spend column", m, fixed = TRUE), m)
}

TAB_COMMON_TESTS[["customer_lifetime: the CLV, the holdout, the classes and the checks' numbers against a hand computation"]] <- function(code) {
  # the discounted sum: a linear expectation, three customers
  rate <- 0.01
  cet <- function(t) structure(c(1, 2, 0.5) * t, quadrature = c(FALSE, TRUE, FALSE))
  d <- clv_discounted(cet, 3, rate)
  want <- c(1, 2, 0.5) * sum(1 / (1 + rate)^(1:3))
  .tab_expect(.clv_rel(d$discounted, want) < 1e-14 && .clv_rel(d$cet, c(3, 6, 1.5)) < 1e-14 && d$steps == 3L && identical(d$quadrature, c(FALSE, TRUE, FALSE)), "the sum")
  d <- clv_discounted(cet, 5, rate, max_steps = 2)
  want <- c(1, 2, 0.5) * (3 / (1 + rate)^3 + 2 / (1 + rate)^5)
  .tab_expect(.clv_rel(d$discounted, want) < 1e-14 && d$steps == 2L && d$step_length == 3L, "the coarse steps")
  .tab_expect(abs(clv_period_rate(0.1, "week") - (1.1^(7 / 365.25) - 1)) < 1e-15 && abs(clv_period_rate(0.1, "year") - 0.1) < 1e-15 &&
              abs(clv_period_rate(0.1, "quarter") - (1.1^0.25 - 1)) < 1e-15 && clv_default_horizon("day") == 365 && clv_default_horizon("week") == 52, "the units")
  # the CLV of three customers: margin × expected spend × the discounted increments of Pareto/NBD's expectation
  par <- c(0.55, 10.6, 0.61, 11.7)
  gg <- c(p = 6.25, q = 3.74, gamma = 15.44)
  s <- list(x = c(0, 3, 7), tx = c(0, 30.2, 38.9), T = c(39, 38.4, 39), m = c(NA, 25, 60))
  plan <- list(horizon = 8, discount_rate = 0.1, unit = "week", margin = 0.4, holdout_length = 4, max_steps = 520)
  ev <- clv_evaluate("pareto_nbd", par, gg, s, plan)
  per <- (1.1)^(7 / 365.25) - 1
  cets <- vapply(0:8, function(k) if (k == 0) rep(0, 3) else clv_fn("pnbd_nocov_CET")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], k, s$x, s$tx, s$T), numeric(3))
  disc <- as.vector(t(diff(t(cets))) %*% (1 + per)^-(1:8))
  spend <- c(15.44 * 6.25 / 2.74, (15.44 + 3 * 25) * 6.25 / (6.25 * 3 + 2.74), (15.44 + 7 * 60) * 6.25 / (6.25 * 7 + 2.74))
  .tab_expect(.clv_rel(ev$discounted_transactions, disc) < 1e-12 && .clv_rel(ev$spend, spend) < 1e-12 && .clv_rel(ev$clv, 0.4 * spend * disc) < 1e-12, "the CLV by hand")
  .tab_expect(.clv_rel(ev$transactions, cets[, 9]) < 1e-14 && .clv_rel(ev$holdout, cets[, 5]) < 1e-14, "CET(horizon) and CET(holdout)")
  # the holdout
  h <- clv_holdout(c(1, 2, 3, 4), c(2, NA, 3, 5))
  .tab_expect(h$n == 3L && h$observed == 10 && h$expected == 8 && abs(h$rel_error + 0.2) < 1e-15 && abs(h$mae - 2 / 3) < 1e-15 &&
              abs(h$rmse - sqrt(2 / 3)) < 1e-15 && h$correlation > 0.9, "holdout numbers")
  .tab_expect(is.null(clv_holdout(c(1, 2), c(NA, NA))), "no holdout")
  # the classes: pooled from the top until the last expects 5
  cl <- clv_pool_classes(c(50, 30, 12, 5, 2, 1), c(48, 33, 10, 6, 2.5, 0.5))
  .tab_expect(identical(cl$x0, c(0, 1, 2, 3)) && identical(cl$x1, c(0, 1, 2, NA)) && cl$observed[[4L]] == 8 && cl$expected[[4L]] == 9 &&
              identical(cl$observed[1:3], c(50, 30, 12)), "pooling")
  cl <- clv_pool_classes(c(10), c(10))
  .tab_expect(length(cl$x0) == 1L, "a single class")
  # the frequency check: observed customers drawn from the model are not rejected, and the degrees of freedom are classes − 1 − 4
  set.seed(31)
  par_m <- c(0.5, 6, 0.9, 1.6)
  sm <- .clv_summary_of_days(.clv_sim_days("mbg_nbd", 1200, par_m))
  s2 <- list(x = sm$x, tx = sm$tx, T = sm$T, n = nrow(sm))
  fit <- clv_fit_purchases("mbg_nbd", s2)
  fq <- clv_frequency_fit("mbg_nbd", fit$par, s2)
  .tab_expect(fq$df == fq$n_classes - 5 && sum(fq$observed) == s2$n && abs(sum(fq$expected) - s2$n) < 1e-6 && fq$expected[[length(fq$expected)]] >= 5 &&
              is.finite(fq$p_value) && fq$max_share_gap < 0.05, "the frequency check: ", fq$statistic, " on ", fq$df, " df, p ", fq$p_value)
  cls <- clv_holdout_by_class(s2$x, rep(1, s2$n), rep(2, s2$n), fq)
  .tab_expect(sum(cls$n) == s2$n && all(cls$observed[cls$n > 0] == 1) && all(cls$expected[cls$n > 0] == 2), "the holdout by class")
  # the slope's HC3 test against its textbook form, Pearson's r
  set.seed(4)
  x <- stats::rpois(80, 3) + 1
  m <- 20 + 0.8 * x + stats::rnorm(80, 0, 1 + 0.5 * x)
  si <- clv_spend_independence(x, m)
  fitlm <- stats::lm(m ~ x)
  X <- stats::model.matrix(fitlm)
  hh <- stats::hatvalues(fitlm)
  v <- solve(crossprod(X)) %*% crossprod(X * (stats::resid(fitlm) / (1 - hh))) %*% solve(crossprod(X))
  .tab_expect(abs(si$statistic - stats::coef(fitlm)[[2L]] / sqrt(v[2L, 2L])) < 1e-10 && abs(si$r - stats::cor(x, m)) < 1e-14 && si$n == 80L, "HC3")
  .tab_expect(is.na(clv_spend_independence(c(1, 1, 1, 1, 1), c(1, 2, 3, 4, 5))$r), "a constant frequency")
}

TAB_COMMON_TESTS[["customer_lifetime: the charts' points, caps and tables"]] <- function(code) {
  # P(alive) histogram, the spend's, the CLV's, the concentration, the top shares
  set.seed(2)
  p <- stats::runif(500)
  h <- clv_palive_hist(c(p, 1, 0))
  .tab_expect(nrow(h) == 20L && sum(h$count) == 502 && h$x1[[20L]] == 1 && h$count[[20L]] >= 1 && h$count[[1L]] >= 1, "P(alive) histogram")
  gg <- c(p = 6, q = 4, gamma = 15)
  x <- c(stats::rpois(400, 2) + 1, rep(0, 30))
  m <- c(.clv_sim_spend(x[1:400], 6, 4, 15), rep(NA, 30))
  sh <- clv_spend_hist(gg, x, m)
  .tab_expect(nrow(sh) >= 15L && sum(sh$observed) == 400 && abs(sum(sh$expected) - 400) < 1e-9 && is.na(sh$x1[[nrow(sh)]]) && all(diff(sh$x0) > 0), "spend histogram")
  .tab_expect(max(abs(sh$observed - sh$expected) / sqrt(pmax(sh$expected, 1))) < 4.5, "the model's expected spend is near the observed one on its own data")
  vh <- clv_value_hist(c(0, 0, 0.5, 2, 10, 1000, 300))
  .tab_expect(vh$x0[[1L]] == 0 && vh$x1[[1L]] == 0 && vh$count[[1L]] == 2 && sum(vh$count) == 7 && nrow(vh) == 21L && abs(vh$x1[[21L]] - 1000) < 1e-9, "value histogram")
  .tab_expect(nrow(clv_value_hist(c(0, 0, 5, 5))) == 2L && nrow(clv_value_hist(c(0, 0))) == 1L, "value histogram, degenerate")
  cc <- clv_concentration(c(10, 1, 1, 0, 4, 4))
  .tab_expect(nrow(cc) == 101L && cc$y[[1L]] == 0 && abs(cc$y[[101L]] - 1) < 1e-15 && abs(cc$y[[51L]] - 18 / 20) < 1e-15, "concentration: ", cc$y[[51L]])
  .tab_expect(abs(clv_top_share(c(10, 1, 1, 0, 4, 4), 0.2) - 14 / 20) < 1e-15 && abs(clv_top_share(c(10, 1, 1, 0, 4, 4), 0.5) - 18 / 20) < 1e-15, "top share")
  # the P(alive) grid, in both processes, and the curves
  par <- c(0.24, 4.4, 0.79, 2.4)
  cells <- clv_palive_cells("bg_nbd", par, 36.4, 10)
  .tab_expect(nrow(cells) == 1 + 10 * 10 && cells$level[[1L]] == "0" && cells$level_2[[1L]] == "0" && cells$level_2[[2L]] == "0.0–3.6" &&
              all(cells$value >= 0 & cells$value <= 1), "the grid")
  one <- cells[cells$level == "3", ]
  .tab_expect(!is.unsorted(one$value), "P(alive) rises with the recency")
  cb <- clv_palive_cells("bg_bb", c(1.2, 0.75, 0.66, 2.78), 6, 4)
  .tab_expect(identical(cb$level_2[cb$level == "3"], c("3", "4", "5", "6")) && cb$level[[1L]] == "0" && all(cb$value > 0 & cb$value < 1), "the BG/BB grid")
  cv <- clv_palive_curves("bg_nbd", par, 36.4, 10)
  .tab_expect(identical(unique(cv$level_2), c("x=1", "x=2", "x=5", "x=10")) && sum(cv$level_2 == "x=1") == 50L && cv$x[[1L]] == 0 && abs(cv$x[[50L]] - 36.4) < 1e-12, "the curves")
  .tab_expect(identical(unique(clv_palive_curves("bg_nbd", par, 36.4, 3)$level_2), c("x=1", "x=2")), "curves up to the largest x")
  .tab_expect(clv_typical_age(c(30, 36.44, 40)) == 36.4, "the typical age")
}

TAB_COMMON_TESTS[["customer_lifetime: a fit gives its rows, its output, its card, and Predict gives the fit's numbers bit for bit"]] <- function(code) {
  set.seed(5)
  sim <- .clv_sim_days("mbg_nbd", 260, c(0.5, 6, 0.9, 1.6))
  S <- .clv_summary_of_days(sim)
  S$m <- .clv_sim_spend(S$x)
  S$h <- stats::rpois(nrow(S), 0.3 * S$x / pmax(S$T, 1) * 26)
  S$id <- paste0("c", seq_len(nrow(S)))
  S$extra <- seq_len(nrow(S))
  S$id[3] <- NA
  base <- .clv_params(list(spend = "m", purchase_model = "mbg_nbd", compare = TRUE, horizon = 40, discount_rate = 0.08, margin = 0.5,
                           holdout_transactions = "h", holdout_length = 26, alpha = 0.05, output = "rows"))
  task <- .clv_task(base, S)
  fit <- clv_fit(task, "clv", S)
  res <- clv_results(task, fit, S)
  problems <- tab_check_declared(res$rows, "clv", "value")
  .tab_expect(!length(problems), paste(head(problems, 3L), collapse = "; "))
  df <- tab_results_frame(res$rows)
  num <- function(key) df$value[df$key == key]
  txt <- function(key) df$text[df$key == key]
  .tab_expect(txt("model|||purchase_model") == "mbg_nbd" && txt("model|||purchase_model_asked") == "mbg_nbd" && num("model|||n") == 259 && num("model|||n_missing") == 1,
              "the model rows")
  .tab_expect(all(c("candidate|pareto_nbd||log_lik", "candidate|bg_nbd||chi_square_p_value", "candidate|mbg_nbd||chosen") %in% df$key) && num("candidate|mbg_nbd||chosen") == 1 &&
              num("candidate|bg_nbd||chosen") == 0, "the comparison with `compare`")
  .tab_expect(all(c("hist|frequency|observed|count|0", "hist|holdout|expected|mean|0", "hist|p_alive||count|0", "hist|spend|observed|count|0", "hist|clv||count|0",
                    "curve|concentration||y|100", "curve|spend_frequency||y|0", "point|spend_frequency||y|0") %in% df$key), "the charts' keys")
  .tab_expect(sum(df$section == "customer") == 20L * 8L && num("model|||clv_total") > 0 && num("model|||margin") == 0.5 &&
              abs(num("model|||discount_per_period") - clv_period_rate(0.08, "week")) < 1e-15, "the top customers and the CLV")
  .tab_expect(!anyDuplicated(df$key), "no key twice")
  # the holdout's classes are the frequency chart's: the same bounds (the last one open, its x1 NULL, which the page prints «k+»), and the
  # customers of each class are the rows of the stat `count`, not the bound
  hm <- df[df$section == "hist" & df$level == "holdout" & df$stat == "mean" & df$level_2 == "observed", ]
  hf <- df[df$section == "hist" & df$level == "frequency" & df$level_2 == "observed", ]
  hn <- df[df$section == "hist" & df$level == "holdout" & df$stat == "count", ]
  k <- nrow(hm)
  .tab_expect(k > 2L && identical(hm$x0, hf$x0) && identical(hm$x1, hf$x1) && is.na(hm$x1[[k]]) && !anyNA(hm$x1[-k]) && identical(hm$x1[-k], hm$x0[-k]),
              "the holdout's classes carry the frequency's bounds, the last open")
  .tab_expect(nrow(hn) == k && identical(hn$x1, hm$x1) && all(hn$level_2 == "customers") && sum(hn$value) == sum(!is.na(S$h[!is.na(S$id)])), "the holdout's customers by class")
  # the output: the input's columns and the predictions, NULL on the row left out
  out <- res$output
  .tab_expect(identical(names(out)[1:ncol(S)], names(S)) && all(c("pred_p_alive", "pred_transactions", "pred_holdout_transactions", "pred_spend", "pred_discounted_transactions", "pred_clv") %in% names(out)) &&
              is.na(out$pred_clv[[3L]]) && !is.na(out$pred_clv[[4L]]) && .clv_rel(sum(out$pred_clv, na.rm = TRUE), num("model|||clv_total")) < 1e-12, "the output rows")
  clash <- S
  clash$pred_clv <- 1
  .tab_expect(grepl("«pred_clv»", .tab_refused_with("output_column_exists", clv_results(task, fit, clash)), fixed = TRUE), "a clash with an input column")
  # the card: the schema's required keys, the numbers as they were, the inputs
  schema <- jsonlite::fromJSON(file.path(code, "card.schema.json"), simplifyVector = FALSE)
  text <- tab_card_text(res$card)
  card <- jsonlite::fromJSON(text, simplifyVector = FALSE)
  need <- unlist(schema$properties$customer_lifetime$required)
  .tab_expect(!length(setdiff(need, names(card$customer_lifetime))) && !length(setdiff(unlist(schema$required), names(card))), "the card's required keys")
  .tab_expect(identical(vapply(card$inputs, function(i) i$name, ""), c("x", "tx", "T", "m")) && card$customer_lifetime$purchase_model == "mbg_nbd" &&
              card$customer_lifetime$chosen_by == "manual" && card$customer_lifetime$horizon == 40 && card$customer_lifetime$library$CLVTools == "0.12.1" &&
              identical(as.numeric(unlist(card$customer_lifetime$purchases$params)), unname(fit$purchases$par)) &&
              identical(as.numeric(unlist(card$customer_lifetime$spend$params)), unname(fit$spend$par)), "the card's content")
  .tab_expect(nchar(text, type = "bytes") < 5000, "a card is a few kilobytes: ", nchar(text, type = "bytes"))
  # Predict on the training rows: the fit's own numbers, bit for bit
  ptask <- list(params = list(), limits = tab_catalog()$limits, .frame = S)
  got <- clv_predict_node(ptask, card)
  pred <- got$output
  same <- function(a, b) identical(as.numeric(a[!is.na(b)]), as.numeric(b[!is.na(b)]))
  .tab_expect(!is.na(pred$prediction[[3L]]) && same(pred$prediction, out$pred_transactions) && same(pred$prediction_p_alive, out$pred_p_alive) &&
              same(pred$prediction_spend, out$pred_spend) && same(pred$prediction_clv, out$pred_clv), "Predict on the training rows is not the fit's")
  # a horizon of its own, rows that break a rule, a missing column
  longer <- clv_predict_node(list(params = list(horizon = 80), limits = tab_catalog()$limits, .frame = S), card)$output
  .tab_expect(all(longer$prediction >= pred$prediction - 1e-12) && any(longer$prediction > pred$prediction), "a longer horizon expects more")
  bad <- S
  bad$tx[5] <- bad$T[5] + 10
  bad$x[6] <- 2.5
  bad$x[7] <- NA
  b <- suppressWarnings(clv_predict_node(list(params = list(), limits = tab_catalog()$limits, .frame = bad), card))
  .tab_expect(b$n_bad == 2L && b$n_null == 1L && all(is.na(b$output$prediction[5:7])) && !is.na(b$output$prediction[[8L]]) &&
              identical(b$output$prediction[8:20], pred$prediction[8:20]) && identical(b$output$prediction[1:4], pred$prediction[1:4]), "rows that break a rule get NULL predictions")
  .tab_expect(grepl("no column «m»", .tab_refused_with("missing_column", clv_predict_node(list(params = list(), limits = tab_catalog()$limits, .frame = S[, setdiff(names(S), "m")]), card)), fixed = TRUE), "a missing column")
  # the variant without a spend column writes no spend, no CLV
  p2 <- .clv_task(.clv_params(list(purchase_model = "bg_nbd", output = "results")), S, variant = "purchases")
  r2 <- clv_results(p2, clv_fit(p2, "clv", S), S)
  .tab_expect(!length(tab_check_declared(r2$rows, "clv", "purchases")) && is.null(r2$output) && is.null(r2$card$customer_lifetime$spend), "the purchases variant")
  card2 <- jsonlite::fromJSON(tab_card_text(r2$card), simplifyVector = FALSE)
  .tab_expect(is.null(card2$customer_lifetime$spend) && length(card2$inputs) == 3L &&
              is.null(clv_predict_node(list(params = list(), limits = tab_catalog()$limits, .frame = S), card2)$output$prediction_clv), "the purchases card")
  # BG/BB, with a spend and a holdout
  set.seed(9)
  bb <- .clv_sim_bgbb(600, c(1.2, 0.75, 0.66, 2.78), 6)
  bb$m <- .clv_sim_spend(bb$x)
  bb$h <- stats::rbinom(nrow(bb), 5, 0.1 * (bb$x + 1) / 7)
  bb$id <- seq_len(nrow(bb))
  pb <- .clv_params(list(spend = "m", horizon = 8, holdout_transactions = "h", holdout_length = 5, output = "rows"), "bg_bb")
  btask <- .clv_task(pb, bb, "bg_bb")
  bfit <- clv_fit(btask, "bg_bb", bb)
  bres <- clv_results(btask, bfit, bb)
  problems <- tab_check_declared(bres$rows, "bg_bb", "value")
  .tab_expect(!length(problems), paste(head(problems, 3L), collapse = "; "))
  bdf <- tab_results_frame(bres$rows)
  .tab_expect(!any(bdf$section == "candidate") && bfit$model == "bg_bb" && "model|||param_gamma" %in% bdf$key && "model|||spend_param_gamma" %in% bdf$key &&
              bdf$text[bdf$key == "model|||purchase_model"] == "bg_bb" && "cell|3|3|p_alive" %in% bdf$key, "the BG/BB rows")
  bcard <- jsonlite::fromJSON(tab_card_text(bres$card), simplifyVector = FALSE)
  bpred <- clv_predict_node(list(params = list(), limits = tab_catalog()$limits, .frame = bb), bcard)$output
  .tab_expect(identical(bpred$prediction, bres$output$pred_transactions) && identical(bpred$prediction_clv, bres$output$pred_clv) &&
              identical(bpred$prediction_p_alive, bres$output$pred_p_alive) && bcard$customer_lifetime$time_unit == "month", "BG/BB's Predict is the fit's")
  # `auto` with three candidates and no spend: the highest log-likelihood is chosen, and marked
  a <- .clv_task(.clv_params(list(purchase_model = "auto", output = "results")), S, variant = "purchases")
  af <- clv_fit(a, "clv", S)
  lls <- vapply(af$candidates, function(c) c$p$log_lik, 0)
  .tab_expect(length(af$candidates) == 3L && af$chosen == which.max(lls) && af$chosen_by == "log_lik" && af$asked == "auto", "auto")
  # the long horizon of the CLV is added in steps
  lh <- .clv_task(.clv_params(list(purchase_model = "bg_nbd", horizon = 1000, output = "results")), S, variant = "purchases")
  lf <- clv_fit(lh, "clv", S)
  .tab_expect(lf$ev$step_length == 2L && lf$ev$steps == 500L && identical(clv_notes(lf)$clv_steps, "@clv_steps max=520 step=2"), "clv_steps")
}

TAB_COMMON_TESTS[["customer_lifetime: the card's column names that look like code, and a customer of 20 000 repeat purchases"]] <- function(code) {
  set.seed(6)
  S <- .clv_summary_of_days(.clv_sim_days("bg_nbd", 80, c(0.25, 4, 0.8, 2.5)))
  S$m <- .clv_sim_spend(S$x)
  names(S) <- c("x); system(\"id\")", "a`b", "ü ñ", "ḿ")
  S$`customer id` <- paste0("c", seq_len(nrow(S)))
  # a customer with 5 000 repeat purchases needs the quadrature, and the fit stays finite
  big <- S[1:3, ]
  big[1:2, 1] <- c(5000, 20000)
  big[1:2, 2] <- c(99.9, 100)
  big[1:2, 3] <- 100
  big[1:2, 4] <- c(30, 25)
  big$`customer id` <- c("huge1", "huge2", "huge3")
  S <- rbind(big[1:2, ], S)
  params <- list(customer = "customer id", frequency = "x); system(\"id\")", recency = "a`b", age = "ü ñ", time_unit = "week", spend = "ḿ",
                 purchase_model = "bg_nbd", horizon = 20, seed = 1, output = "rows")
  task <- .clv_task(params, S)
  fit <- clv_fit(task, "clv", S)
  res <- clv_results(task, fit, S)
  df <- tab_results_frame(res$rows)
  .tab_expect(!length(tab_check_declared(res$rows, "clv", "value")) && all(is.finite(res$output$pred_clv)) && any(df$level == "huge2"), "column names and the huge customers")
  .tab_expect(sum(fit$ev$quadrature) >= 2L && grepl("^@cet_quadrature n=[0-9]+$", df$text[df$key == "model|||note_cet_quadrature"]), "the note cet_quadrature")
  card <- jsonlite::fromJSON(tab_card_text(res$card), simplifyVector = FALSE)
  .tab_expect(identical(vapply(card$inputs, function(i) i$name, ""), c(params$frequency, params$recency, params$age, params$spend)), "the card's inputs")
  pred <- clv_predict_node(list(params = list(), limits = tab_catalog()$limits, .frame = S), card)$output
  .tab_expect(identical(pred$prediction, res$output$pred_transactions), "Predict with such names")
}

TAB_COMMON_TESTS[["customer_lifetime: the spend points are capped with the largest spends kept and labelled, the top customers are 20"]] <- function(code) {
  set.seed(14)
  bb <- .clv_sim_bgbb(1800, c(1.2, 0.75, 0.66, 2.78), 6)
  bb$m <- .clv_sim_spend(bb$x)
  bb$id <- paste0("k", seq_len(nrow(bb)))
  limits <- tab_catalog()$limits
  limits$max_chart_points <- 300
  task <- .clv_task(.clv_params(list(spend = "m", output = "results"), "bg_bb"), bb, "bg_bb", limits = limits)
  tab_seed(task)
  fit <- clv_fit(task, "bg_bb", bb)
  df <- tab_results_frame(clv_results(task, fit, bb)$rows)
  pts <- df[df$section == "point", ]
  repeaters <- which(bb$x > 0 & !is.na(bb$m))
  top <- bb$id[repeaters][head(order(-bb$m[repeaters]), 10L)]
  .tab_expect(nrow(pts) == 300L && length(repeaters) > 300L, "the cap: ", nrow(pts))
  .tab_expect(setequal(pts$text[!is.na(pts$text)], top), "the ten largest spends are labelled")
  .tab_expect(length(unique(df$level[df$section == "customer"])) == 20L, "twenty top customers")
  tab_seed(task)
  again <- tab_results_frame(clv_results(task, clv_fit(task, "bg_bb", bb), bb)$rows)
  .tab_expect(identical(again$value, df$value) && identical(again$x0, df$x0), "the same seed, the same points")
}
