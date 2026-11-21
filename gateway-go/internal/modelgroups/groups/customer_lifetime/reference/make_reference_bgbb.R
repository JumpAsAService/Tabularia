# Writes the reference cases of the node `bg_bb` (docs/specs/customer-lifetime-models.md, «Reference values»,
# lot B3): the CSVs bgbb_*.csv of reference/data/ and the cases of reference/expected.json that carry
# `by` = "make_reference_bgbb.R". Run by hand, inside the group's image, with the reference folder mounted
# writable:
#
#   docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/customer_lifetime/reference:/ref" \
#     tabularia-models-customer_lifetime:dev Rscript /ref/make_reference_bgbb.R
#
# expected.json is shared with make_reference.R (the refusals of lot B1 and the cases of `clv` of lot B2): each
# case says which script wrote it (`by`), and this script replaces only its own, keeping the others and the
# file's header untouched.
#
# No copy of a public data set: the donors are drawn from BG/BB's own process (Fader, Hardie and Shang 2010) with
# fixed seeds. The expected values are computed here WITHOUT the runner's code, from the article's definitions:
# the likelihood as the sum over the possible moments of dropout, written out term by term (the runner uses the
# same sum over compressed rows, but another function), maximized to the end by Nelder–Mead, BFGS and nlminb;
# P(alive) and the expected transactions as sums over the opportunities (the runner has the closed form with
# the telescoped sum; here the sum is not telescoped, so that γ = 1 needs no limit); Gamma–Gamma's likelihood
# maximized here; Pearson's r and the HC3 test through lm(). BTYD's bgbb.* functions are the witness of the
# same numbers (make_witness.R), never a part of the image.

args <- commandArgs(FALSE)
here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", args, value = TRUE))))
dir.create(file.path(here, "data"), showWarnings = FALSE)
BY <- "make_reference_bgbb.R"

# ── drawing the donors ───────────────────────────────────────────────────────────
# n_i opportunities after the first transaction (`opportunities`, recycled), then `holdout` more. A donor
# transacts at an opportunity with probability p while alive; after each opportunity (and before the first)
# it drops out with probability θ, so that it is alive for a geometric number of opportunities.
draw_donors <- function(seed, n, par, opportunities, holdout = 0, gg = c(6, 4, 15), prefix = "d", theta = NULL) {
  set.seed(seed)
  opp <- if (length(opportunities) == 1L) rep(opportunities, n) else sample(opportunities, n, replace = TRUE)
  p <- stats::rbeta(n, par[[1L]], par[[2L]])
  th <- if (is.null(theta)) stats::rbeta(n, par[[3L]], par[[4L]]) else rep(theta, n)
  alive_for <- stats::rgeom(n, th)
  x <- tx <- later <- numeric(n)
  for (i in seq_len(n)) {
    horizon <- opp[[i]] + holdout
    got <- if (alive_for[[i]] > 0) stats::rbinom(min(alive_for[[i]], horizon), 1, p[[i]]) else integer()
    cal <- got[seq_len(min(length(got), opp[[i]]))]
    x[[i]] <- sum(cal)
    tx[[i]] <- if (any(cal == 1)) max(which(cal == 1)) else 0
    if (holdout > 0 && length(got) > opp[[i]]) later[[i]] <- sum(got[(opp[[i]] + 1):length(got)])
  }
  nu <- stats::rgamma(n, gg[[2L]], rate = gg[[3L]])
  m <- stats::rgamma(n, shape = gg[[1L]] * pmax(x, 1), rate = nu * pmax(x, 1))
  d <- data.frame(donor = paste0(prefix, seq_len(n)), x = x, t_x = tx, n = opp, avg_gift = ifelse(x > 0, m, NA_real_), stringsAsFactors = FALSE)
  if (holdout > 0) d$later_gifts <- later
  d
}

write_exact <- function(d, name) {
  for (k in names(d)) if (is.double(d[[k]])) d[[k]] <- ifelse(is.na(d[[k]]), NA_character_, formatC(d[[k]], digits = 17, format = "g"))
  utils::write.csv(d, file.path(here, "data", name), row.names = FALSE, na = "")
}

# ── the expected values, from the definitions ────────────────────────────────────
LB <- lbeta
# log of the likelihood of each customer: the moment of dropout, after j opportunities (j from t_x to n − 1), or
# alive through the n of them; summed term by term in logs
bgbb_logl <- function(par, x, tx, n) {
  al <- par[[1L]]; be <- par[[2L]]; ga <- par[[3L]]; de <- par[[4L]]
  vapply(seq_along(x), function(i) {
    j <- seq_len(n[[i]] - tx[[i]]) + tx[[i]] - 1  # tx … n − 1 (empty when tx = n)
    terms <- c(LB(al + x[[i]], be + n[[i]] - x[[i]]) - LB(al, be) + LB(ga, de + n[[i]]) - LB(ga, de),
               if (length(j)) LB(al + x[[i]], be + j - x[[i]]) - LB(al, be) + LB(ga + 1, de + j) - LB(ga, de))
    m <- max(terms)
    m + log(sum(exp(terms - m)))
  }, 0)
}
compress <- function(d) {
  key <- paste(d$x, d$t_x, d$n)
  u <- !duplicated(key)
  list(x = d$x[u], tx = d$t_x[u], n = d$n[u], w = as.vector(table(factor(key, levels = key[u]))))
}
bgbb_max <- function(d) {
  k <- compress(d)
  f <- function(lp) -sum(k$w * bgbb_logl(exp(lp), k$x, k$tx, k$n))
  o <- stats::optim(rep(0, 4), f, method = "Nelder-Mead", control = list(maxit = 20000, reltol = 1e-14))
  for (i in 1:3) {
    o <- stats::optim(o$par, f, method = "BFGS", control = list(maxit = 10000, reltol = 1e-16))
    nl <- stats::nlminb(o$par, f, control = list(eval.max = 10000, iter.max = 10000, rel.tol = 1e-15, x.tol = 1e-12))
    o$par <- nl$par
  }
  list(par = stats::setNames(exp(o$par), c("alpha", "beta", "gamma", "delta")), log_lik = -f(o$par))
}
# P(alive for the opportunity n + 1) and the expected transactions in the opportunities n + 1 … n + h: sums over
# the opportunities (the customer is alive for the opportunity n + k when it has survived n + k dropout events)
bgbb_palive <- function(par, x, tx, n) {
  al <- par[[1L]]; be <- par[[2L]]; ga <- par[[3L]]; de <- par[[4L]]
  exp(LB(al + x, be + n - x) - LB(al, be) + LB(ga, de + n + 1) - LB(ga, de) - bgbb_logl(par, x, tx, n))
}
bgbb_cet <- function(par, h, x, tx, n) {
  al <- par[[1L]]; be <- par[[2L]]; ga <- par[[3L]]; de <- par[[4L]]
  front <- LB(al + x + 1, be + n - x) - LB(al, be) - bgbb_logl(par, x, tx, n)
  vapply(seq_along(x), function(i) exp(front[[i]]) * sum(exp(LB(ga, de + n[[i]] + seq_len(h)) - LB(ga, de))), 0)
}
# Gamma–Gamma, maximized here
gg_max <- function(x, m) {
  f <- function(lp) {
    p <- exp(lp[[1L]]); q <- exp(lp[[2L]]); g <- exp(lp[[3L]])
    -sum(lgamma(p * x + q) - lgamma(p * x) - lgamma(q) + q * log(g) + (p * x - 1) * log(m) + p * x * log(x) - (p * x + q) * log(g + x * m))
  }
  o <- stats::optim(rep(0, 3), f, method = "Nelder-Mead", control = list(maxit = 20000, reltol = 1e-14))
  for (i in 1:3) {
    o <- stats::optim(o$par, f, method = "BFGS", control = list(maxit = 10000, reltol = 1e-16))
    nl <- stats::nlminb(o$par, f, control = list(eval.max = 10000, iter.max = 10000, rel.tol = 1e-15, x.tol = 1e-12))
    o$par <- nl$par
  }
  list(par = stats::setNames(exp(o$par), c("p", "q", "gamma")), log_lik = -f(o$par))
}
hc3 <- function(d) {
  u <- d[d$x > 0 & !is.na(d$avg_gift), ]
  fit <- stats::lm(avg_gift ~ x, data = u)
  X <- stats::model.matrix(fit)
  e <- stats::residuals(fit) / (1 - stats::hatvalues(fit))
  bread <- solve(crossprod(X))
  V <- bread %*% crossprod(X * e) %*% bread
  z <- stats::coef(fit)[[2L]] / sqrt(V[2L, 2L])
  list(r = stats::cor(u$x, u$avg_gift), z = z, p = 2 * stats::pnorm(-abs(z)), n = nrow(u))
}

# ── the expectations: a number with its tolerances, a text exactly ───────────────
num_ <- function(key, value, rel = 1e-9, abs = 1e-12, from = NULL) {
  out <- list(key = key, value = unname(value), rel = rel, abs = abs)
  if (!is.null(from)) out$from <- from
  out
}
txt_ <- function(key, text) list(key = key, text = text)
params_of <- function(prefix, par, rel, from) unname(Map(function(k, v) num_(paste0(prefix, k), v, rel = rel, from = from), names(par), par))
checks_ <- function(verdicts) unname(Map(function(k, v) txt_(paste0("assumption|", k, "||verdict"), v), names(verdicts), verdicts))

# Tolerances (docs/specs/customer-lifetime-models.md, «As built, B3» and «As built, B10»), as measured: the runner's
# estimates against the maximum found here to the end. Since B10 the runner's L-BFGS-B runs to tight tolerances
# (CLV_TIGHT) on BG/BB's own likelihood, so the parameters differ by at most 7.1e-6 relative (2.1e-3 with optim's
# defaults, on the ridge of the four parameters), tolerance 5e-5; the log-likelihoods by 1e-10 absolute, tolerance
# 1e-8; the quantities that are sums over the customers (expected transactions, P(alive), the CLV, the holdout),
# evaluated here at this script's maximum, by at most 1.3e-6 relative, tolerance 1e-5 (the holdout's relative error,
# a small difference of two numbers, 1.6e-4, tolerance 1e-3). Gamma–Gamma keeps CLVTools' optimizer settings (the
# clv node's spend is CLVTools' own): its parameters differ by 2.4e-5, tolerance 2e-4, its log-likelihood by 3e-8.
REL_PAR <- 5e-5
LL_ABS <- 1e-8
REL_SUM <- 1e-5
REL_SPEND <- 2e-4
LL_SPEND <- 1e-6

types_of <- function(d) {
  t <- as.list(stats::setNames(rep("number", ncol(d)), names(d)))
  t[["donor"]] <- "text"
  t
}
bb_params <- function(...) {
  utils::modifyList(list(customer = "donor", frequency = "x", recency = "t_x", opportunities = "n", period_unit = "year", alpha = 0.05, seed = 1),
                    list(...))
}
case_ <- function(id, file, d, params, expect, variant) {
  list(id = id, model = "bg_bb", variant = variant, data = paste0("data/", file), types = types_of(d), params = params, expect = expect,
       refuses = NULL, by = BY)
}

cases <- list()

# A. The donations' shape (α 1.2, β 0.75, γ 0.66, δ 2.78, as in Fader, Hardie, Shang 2010): 1,000 donors with 6
#    opportunities, a Gamma–Gamma gift (6, 4, 15), a 5-opportunity holdout, the CLV over 5 opportunities at 10 %
#    a year, the opportunity a year.
A <- draw_donors(11, 1000, c(1.2, 0.75, 0.66, 2.78), 6, holdout = 5)
write_exact(A, "bgbb_donations_like.csv")
mA <- bgbb_max(A)
ggA <- gg_max(A$x[A$x > 0], A$avg_gift[A$x > 0])
hA <- hc3(A)
cetA <- bgbb_cet(mA$par, 5, A$x, A$t_x, A$n)
holdA <- bgbb_cet(mA$par, 5, A$x, A$t_x, A$n)
palA <- bgbb_palive(mA$par, A$x, A$t_x, A$n)
# E[M | x, m̄] and the CLV, k = 1 … 5, each opportunity's expectation discounted at its end
EM <- function(g, x, m) {
  out <- rep(g[["p"]] * g[["gamma"]] / (g[["q"]] - 1), length(x))
  u <- x > 0 & !is.na(m)
  out[u] <- (g[["gamma"]] + x[u] * m[u]) * g[["p"]] / (g[["p"]] * x[u] + g[["q"]] - 1)
  out
}
clv_of <- function(par, g, d, horizon, rate, margin = 1) {
  disc <- 0
  prev <- 0
  for (k in seq_len(horizon)) {
    cur <- bgbb_cet(par, k, d$x, d$t_x, d$n)
    disc <- disc + (cur - prev) / (1 + rate)^k
    prev <- cur
  }
  list(clv = margin * EM(g, d$x, d$avg_gift) * disc, discounted = disc)
}
clvA <- clv_of(mA$par, ggA$par, A, 5, 0.1)
cases[[length(cases) + 1L]] <- case_(
  "bg_bb donors like the donations, value with a holdout", "bgbb_donations_like.csv", A,
  bb_params(spend = "avg_gift", holdout_transactions = "later_gifts", holdout_length = 5, horizon = 5),
  c(list(txt_("model|||purchase_model", "bg_bb"), txt_("model|||purchase_model_asked", "bg_bb"), txt_("model|||optimizer", "L-BFGS-B"),
         txt_("model|||time_unit", "year"), txt_("model|||formula", "x: transactions, t_x: opportunity of the last, n: opportunities, one opportunity is a year"),
         num_("model|||n", 1000), num_("model|||n_repeaters", sum(A$x > 0)), num_("model|||horizon", 5),
         num_("model|||log_lik", mA$log_lik, abs = LL_ABS, from = "BG/BB maximized here"),
         num_("model|||aic", -2 * mA$log_lik + 8, rel = 1e-9, abs = 2 * LL_ABS, from = "BG/BB maximized here"),
         num_("model|||expected_transactions", sum(cetA), rel = REL_SUM, from = "the sum over the opportunities, here"),
         num_("model|||mean_p_alive", mean(palA), rel = REL_SUM, from = "BG/BB definitions, here"),
         num_("model|||holdout_observed", sum(A$later_gifts)),
         num_("model|||holdout_expected", sum(holdA), rel = REL_SUM, from = "the sum over the opportunities, here"),
         num_("assumption|holdout||value", (sum(holdA) - sum(A$later_gifts)) / sum(A$later_gifts), rel = REL_SUM * 100, from = "the sum over the opportunities, here"),
         num_("model|||spend_n", sum(A$x > 0)), num_("model|||discount_per_period", 0.1, rel = 1e-12), num_("model|||margin", 1),
         num_("model|||clv_total", sum(clvA$clv), rel = REL_SUM, from = "the CLV's definition, here"),
         num_("model|||discounted_transactions", sum(clvA$discounted), rel = REL_SUM, from = "the CLV's definition, here"),
         num_("model|||spend_log_lik", ggA$log_lik, abs = LL_SPEND, from = "Gamma-Gamma maximized here"),
         num_("assumption|spend_independence||value", hA$r, from = "cor()"),
         num_("assumption|spend_independence||p_value", hA$p, rel = 1e-8, from = "lm(), HC3"),
         num_("assumption|sample_size||value", 1000), num_("assumption|sample_size||count", sum(A$x > 0)),
         num_("assumption|horizon_length||value", 5 / 6)),
    params_of("model|||param_", mA$par, REL_PAR, "BG/BB maximized here"),
    params_of("model|||spend_param_", ggA$par, REL_SPEND, "Gamma-Gamma maximized here"),
    checks_(c(convergence = "ok", frequency_fit = "ok", spend_independence = "ok", spend_tail = "ok"))),
  "value")

# B. Cohorts of different lengths (3 to 12 opportunities a month), the `purchases` variant: no spend, no holdout,
#    the default horizon (a year, 12 opportunities).
B <- draw_donors(22, 800, c(0.9, 1.4, 0.4, 3.5), 3:12, prefix = "m")
B$avg_gift <- NULL
write_exact(B, "bgbb_cohorts.csv")
mB <- bgbb_max(B)
cetB <- bgbb_cet(mB$par, 12, B$x, B$t_x, B$n)
palB <- bgbb_palive(mB$par, B$x, B$t_x, B$n)
cases[[length(cases) + 1L]] <- case_(
  "bg_bb cohorts of different lengths, the purchases variant", "bgbb_cohorts.csv", B, bb_params(period_unit = "month"),
  c(list(txt_("model|||variant", "purchases"), txt_("model|||time_unit", "month"),
         num_("model|||n", 800), num_("model|||horizon", 12), num_("model|||max_age", 12), num_("model|||median_age", stats::median(B$n)),
         num_("model|||log_lik", mB$log_lik, abs = LL_ABS, from = "BG/BB maximized here"),
         num_("model|||expected_transactions", sum(cetB), rel = REL_SUM, from = "the sum over the opportunities, here"),
         num_("model|||mean_p_alive", mean(palB), rel = REL_SUM, from = "BG/BB definitions, here"),
         num_("assumption|horizon_length||value", 1)),
    params_of("model|||param_", mB$par, REL_PAR, "BG/BB maximized here"),
    checks_(c(convergence = "ok", frequency_fit = "ok"))),
  "purchases")

# C. Donors who never drop out (θ = 1e-9, 20 opportunities): γ goes to its edge (measured 4.5e-6), the dropout's
#    mean to 0, and the Hessian is not positive definite there. The estimate at the edge is a measure with its
#    reading (the edge's value is not a reference: the likelihood is flat), the convergence check is the verdict.
C <- draw_donors(33, 300, c(1, 1, 1, 1), 20, theta = 1e-9, prefix = "n")
C$avg_gift <- NULL
write_exact(C, "bgbb_no_dropout.csv")
cases[[length(cases) + 1L]] <- case_(
  "bg_bb donors who never drop out", "bgbb_no_dropout.csv", C, bb_params(),
  list(txt_("assumption|convergence||verdict", "doubtful"), txt_("assumption|convergence||advice_id", "convergence_edge"),
       txt_("assumption|boundary|gamma|advice_id", "boundary_no_dropout")),
  "purchases")

# the cases of the other scripts stay, with the header
path <- file.path(here, "expected.json")
ref <- jsonlite::fromJSON(path, simplifyVector = FALSE)
others <- Filter(function(k) !identical(k$by, BY), ref$cases)
ref$cases <- c(others, cases)
writeLines(jsonlite::toJSON(ref, auto_unbox = TRUE, pretty = TRUE, null = "null", digits = NA), path, useBytes = TRUE)
cat("wrote", length(cases), "cases of", BY, "and kept", length(others), "of other scripts\n")
cat(sprintf("A: %s LL %.6f | B: %s LL %.6f\n", paste(signif(mA$par, 7), collapse = " "), mA$log_lik, paste(signif(mB$par, 7), collapse = " "), mB$log_lik))
