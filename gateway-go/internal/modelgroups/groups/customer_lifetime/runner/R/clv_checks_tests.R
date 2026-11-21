# The self-test of the checks (clv_checks.R, lot B2): added to TAB_COMMON_TESTS. Each verdict at both
# sides of what decides it, on fits made by hand (no optimizer runs here): a test's p-value against the
# node's alpha, a fact's two states; the measures with a value and no verdict.

# `edges`, `spend_edges`: the rows clv_edges() would give (param, side, pair), e.g. .clv_edge("a", "low").
.clv_edge <- function(param, side = "high", pair = "") data.frame(param = param, side = side, pair = pair, stringsAsFactors = FALSE)

.clv_check_fit <- function(model = "bg_nbd", par = c(r = 0.25, alpha = 4, a = 0.8, b = 2.5), spend = c(p = 6, q = 4, gamma = 15),
                           alpha = 0.05, p_value = 0.03, df = 3, holdout = -0.12, vcov = TRUE, converged = TRUE,
                           edges = clv_no_edges(), spend_edges = clv_no_edges()) {
  set.seed(4)
  n <- 200
  x <- stats::rpois(n, 1.5)
  m <- ifelse(x > 0, stats::rgamma(n, 20, rate = 1), NA_real_)
  k <- length(par)
  list(model = model, alpha = alpha, plan = list(horizon = 52),
       s = list(x = x, m = m, T = stats::runif(n, 20, 40), n = n, has_holdout = !is.null(holdout)),
       purchases = list(model = model, par = par, converged = converged, vcov_log = if (vcov) diag(0.01, k) else NULL, gradient = rep(1e-4, k), edges = edges),
       spend = if (is.null(spend)) NULL else list(model = "gamma_gamma", par = spend, converged = TRUE, vcov_log = diag(0.01, 3), edges = spend_edges),
       freq = list(statistic = 9.1, df = df, p_value = p_value, max_share_gap = 0.021, n_classes = df + 1 + k),
       holdout = if (is.null(holdout)) NULL else list(rel_error = holdout))
}

.clv_check_frame <- function(fit, variant = if (is.null(fit$spend)) "purchases" else "value") {
  rows <- tab_rows_start(list(model = "clv", variant = variant, rows_from = "run", group_version = "selftest"))
  tab_number(rows, "model", "n", fit$s$n)
  clv_checks(rows, fit)
  problems <- tab_check_declared(rows, "clv", variant)
  .tab_expect(!length(problems), paste(head(problems, 3L), collapse = "; "))
  df <- tab_results_frame(rows)
  df[df$section == "assumption", ]
}

.clv_check_get <- function(a, id, stat, level_2 = "") {
  at <- a$key == paste0("assumption|", id, "|", level_2, "|", stat)
  if (!any(at)) return(NULL)
  if (stat %in% c("verdict", "advice", "advice_id", "h0", "significance")) a$text[at] else a$value[at]
}

TAB_COMMON_TESTS[["customer_lifetime: a verdict is a p-value against the node's alpha or a certain fact, both sides; a measure has none"]] <- function(code) {
  g <- .clv_check_get
  # the χ² of the frequency classes: p 0.03 fails at alpha 0.05, passes at 0.01; no degree of freedom left: not computed
  a <- .clv_check_frame(.clv_check_fit(alpha = 0.05))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "failed") && g(a, "frequency_fit", "threshold") == 0.05 &&
              g(a, "frequency_fit", "value") == 0.021 && g(a, "frequency_fit", "p_value") == 0.03, "frequency_fit at alpha 0.05")
  a <- .clv_check_frame(.clv_check_fit(alpha = 0.01))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "ok") && g(a, "frequency_fit", "threshold") == 0.01, "frequency_fit at alpha 0.01")
  a <- .clv_check_frame(.clv_check_fit(df = 0, p_value = NA_real_))
  .tab_expect(identical(g(a, "frequency_fit", "verdict"), "not_computed") && is.null(g(a, "frequency_fit", "p_value")) &&
              grepl("no degree of freedom", g(a, "frequency_fit", "advice"), fixed = TRUE), "frequency_fit without a degree of freedom")
  # the spend's independence: its own HC3 p-value, read against alpha on both sides of it
  fit <- .clv_check_fit()
  si <- clv_spend_independence(fit$s$x, fit$s$m)
  .tab_expect(is.finite(si$p_value) && si$p_value > 1e-6 && si$p_value < 1 - 1e-6, "an HC3 p-value inside (0, 1)")
  a <- .clv_check_frame(.clv_check_fit(alpha = min(si$p_value * 1.01, 0.999)))
  .tab_expect(identical(g(a, "spend_independence", "verdict"), "failed") && g(a, "spend_independence", "value") == si$r, "spend_independence below p")
  a <- .clv_check_frame(.clv_check_fit(alpha = si$p_value * 0.99))
  .tab_expect(identical(g(a, "spend_independence", "verdict"), "ok") && g(a, "spend_independence", "count") == sum(fit$s$x > 0), "spend_independence above p")
  # the spend's tail: q above 2 a finite variance, at most 2 none (a fact, its threshold 2)
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2.5, gamma = 15))), "spend_tail", "verdict"), "ok") &&
              identical(g(.clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2, gamma = 15))), "spend_tail", "verdict"), "doubtful"), "spend_tail")
  # the measures: a value, no verdict, no advice, no H0
  a <- .clv_check_frame(.clv_check_fit(holdout = -0.12))
  for (id in c("sample_size", "holdout", "horizon_length")) {
    at <- a$level == id
    .tab_expect(any(at & a$stat == "value") && !any(at & a$stat %in% c("verdict", "advice", "advice_id", "h0")), id, " is not a measure")
  }
  .tab_expect(g(a, "sample_size", "value") == 200 && g(a, "sample_size", "count") == sum(.clv_check_fit()$s$x > 0) &&
              g(a, "holdout", "value") == -0.12 && abs(g(a, "horizon_length", "value") - 52 / max(.clv_check_fit()$s$T)) < 1e-15, "the measures' numbers")
  # nothing bought in the holdout: no denominator, not computed; no holdout column: no row
  a <- .clv_check_frame(.clv_check_fit(holdout = NA_real_))
  .tab_expect(identical(g(a, "holdout", "verdict"), "not_computed"), "holdout without purchases")
  a <- .clv_check_frame(.clv_check_fit(holdout = NULL, spend = NULL))
  .tab_expect(!any(a$level %in% c("holdout", "spend_independence", "spend_tail")), "no holdout, no spend: their rows are not written")
  # convergence: a fact in three states; the edge is the likelihood's fact (clv_edges), not the estimate's size
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit()), "convergence", "verdict"), "ok"), "convergence ok")
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(converged = FALSE)), "convergence", "verdict"), "failed"), "not converged")
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(vcov = FALSE)), "convergence", "verdict"), "failed"), "no Hessian and no edge")
  .tab_expect(identical(g(.clv_check_frame(.clv_check_fit(vcov = FALSE, par = c(r = 0.25, alpha = 4, a = 3e-5, b = 2.5))), "convergence", "verdict"), "failed"),
              "an estimate of 3e-5 is no edge by its size alone")
  a <- .clv_check_frame(.clv_check_fit(vcov = FALSE, par = c(r = 0.25, alpha = 4, a = 0.02, b = 2.5), edges = .clv_edge("a", "low")))
  .tab_expect(identical(g(a, "convergence", "verdict"), "doubtful") && identical(g(a, "convergence", "advice_id"), "convergence_edge"), "no Hessian at an edge")
  a <- .clv_check_frame(.clv_check_fit(vcov = FALSE, converged = FALSE, edges = .clv_edge("a", "low")))
  .tab_expect(identical(g(a, "convergence", "verdict"), "failed"), "not converged at an edge is failed")
  # at an edge the maximum is the limit, even where the Hessian is still positive definite to its last digits
  a <- .clv_check_frame(.clv_check_fit(vcov = TRUE, par = c(r = 0.25, alpha = 4, a = 1e-7, b = 2.5), edges = .clv_edge("a", "low")))
  .tab_expect(identical(g(a, "convergence", "verdict"), "doubtful") && identical(g(a, "convergence", "advice_id"), "convergence_edge"), "an edge with a Hessian")
  a <- .clv_check_frame(.clv_check_fit(spend_edges = .clv_edge("p", "high")))
  .tab_expect(identical(g(a, "convergence", "verdict"), "doubtful"), "the spend at an edge")
}

TAB_COMMON_TESTS[["customer_lifetime: an edge is where the likelihood keeps growing, found on the likelihood and not on the estimate's size"]] <- function(code) {
  # customers who never drop out (Poisson purchases: the reference case F, clv_no_dropout.csv): MBG/NBD's a goes towards 0, and
  # the likelihood keeps growing that way
  f0 <- utils::read.csv(file.path(code, "reference", "data", "clv_no_dropout.csv"))
  comp <- clv_compress(f0$frequency, f0$recency, f0$age)
  f <- clv_fit_purchases("mbg_nbd", list(x = f0$frequency, tx = f0$recency, T = f0$age, n = nrow(f0)), comp)
  e <- clv_edges(clv_nll("mbg_nbd", comp), f$log_par, "mbg_nbd")
  .tab_expect(any(e$param == "a" & e$side == "low") && identical(clv_check_edge_reading("mbg_nbd", "a", e[e$param == "a", ]), "boundary_no_dropout"),
              "no dropout: ", paste(e$param, e$side, e$pair, collapse = "; "))
  # an interior maximum: every step lowers the likelihood
  set.seed(5)
  sm <- .clv_summary_of_days(.clv_sim_days("bg_nbd", 400, c(0.25, 4, 0.8, 2.5)))
  c2 <- clv_compress(sm$x, sm$tx, sm$T)
  f2 <- clv_fit_purchases("bg_nbd", list(x = sm$x, tx = sm$tx, T = sm$T, n = nrow(sm)), c2)
  .tab_expect(!nrow(clv_edges(clv_nll("bg_nbd", c2, stable = TRUE), f2$log_par, "bg_nbd")), "an interior maximum has no edge")
  # everyone buys at the same rate (Poisson, one rate): r and α go up together, the rate's spread is not identified
  set.seed(606)
  n <- 300
  T <- sample(140:364, n, replace = TRUE) / 7
  z <- stats::rpois(n, 0.3 * T)
  tz <- ifelse(z > 0, T * stats::rbeta(n, pmax(z, 1), 1), 0)
  c3 <- clv_compress(z, tz, T)
  f3 <- clv_fit_purchases("mbg_nbd", list(x = z, tx = tz, T = T, n = n), c3)
  e3 <- clv_edges(clv_nll("mbg_nbd", c3), f3$log_par, "mbg_nbd")
  .tab_expect(any(e3$pair == "rate") && identical(clv_check_edge_reading("mbg_nbd", "r", e3[e3$param == "r", ]), "boundary_homogeneous"),
              "r and alpha up together: ", paste(e3$param, e3$side, e3$pair, collapse = "; "))
}

TAB_COMMON_TESTS[["customer_lifetime: the edges of the parameters are a measure, with the reading of each and no verdict"]] <- function(code) {
  g <- .clv_check_get
  a <- .clv_check_frame(.clv_check_fit())
  .tab_expect(!any(a$level == "boundary"), "no estimate at an edge: no row")
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4, a = 5e-5, b = 2.5)))
  .tab_expect(!any(a$level == "boundary"), "a small estimate where the likelihood does not grow is no edge")
  # BG/NBD: a → 0 or b → ∞ is no dropout; r and α up together, a rate with no spread
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4, a = 5e-5, b = 2.5), edges = .clv_edge("a", "low")))
  .tab_expect(g(a, "boundary", "value", "a") == 5e-5 && identical(g(a, "boundary", "advice_id", "a"), "boundary_no_dropout") &&
              !any(a$level == "boundary" & a$stat %in% c("verdict", "advice", "h0", "count")) && sum(a$level == "boundary" & a$stat == "value") == 1,
              "BG/NBD a at 0: a value and its reading, no verdict")
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4, a = 0.8, b = 2e4), edges = .clv_edge("b", "high")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "b"), "boundary_no_dropout") && g(a, "boundary", "value", "b") == 2e4, "BG/NBD b at infinity")
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 3e4, alpha = 5e5, a = 0.8, b = 2.5), edges = .clv_edge(c("r", "alpha"), "high", "rate")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "r"), "boundary_homogeneous") && identical(g(a, "boundary", "advice_id", "alpha"), "boundary_homogeneous"),
              "a purchase rate with no spread")
  # Pareto/NBD: s and β up together, the same dropout rate for everyone; β alone up, no dropout
  a <- .clv_check_frame(.clv_check_fit(model = "pareto_nbd", par = c(r = 0.5, alpha = 10, s = 2e4, beta = 3e5), edges = .clv_edge(c("s", "beta"), "high", "dropout")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "s"), "boundary_homogeneous") && identical(g(a, "boundary", "advice_id", "beta"), "boundary_homogeneous"),
              "Pareto/NBD s and beta")
  a <- .clv_check_frame(.clv_check_fit(model = "pareto_nbd", par = c(r = 0.5, alpha = 10, s = 0.6, beta = 3e5), edges = .clv_edge("beta", "high")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "beta"), "boundary_no_dropout"), "Pareto/NBD beta alone")
  # BG/BB: γ → 0 or δ → ∞ is no dropout; α and β up together, a transaction probability with no spread
  a <- .clv_check_frame(.clv_check_fit(model = "bg_bb", par = c(alpha = 1.2, beta = 0.75, gamma = 2e-5, delta = 2.7), edges = .clv_edge("gamma", "low")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "gamma"), "boundary_no_dropout"), "BG/BB gamma at 0")
  a <- .clv_check_frame(.clv_check_fit(model = "bg_bb", par = c(alpha = 2e4, beta = 3e4, gamma = 0.6, delta = 2.7), edges = .clv_edge(c("alpha", "beta"), "high", "rate")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "alpha"), "boundary_homogeneous") && identical(g(a, "boundary", "advice_id", "beta"), "boundary_homogeneous"),
              "BG/BB alpha and beta")
  # a rate's parameter alone at an edge has no reading of its own
  a <- .clv_check_frame(.clv_check_fit(par = c(r = 0.25, alpha = 4e5, a = 0.8, b = 2.5), edges = .clv_edge("alpha", "high")))
  .tab_expect(identical(g(a, "boundary", "advice_id", "alpha"), "boundary"), "alpha alone")
  # the spend's parameters are named as the model section names them, with no reading of their own
  a <- .clv_check_frame(.clv_check_fit(spend = c(p = 6, q = 2e4, gamma = 15), spend_edges = .clv_edge("q", "high")))
  .tab_expect(g(a, "boundary", "value", "spend_q") == 2e4 && identical(g(a, "boundary", "advice_id", "spend_q"), "boundary"), "the spend's q")
}

TAB_COMMON_TESTS[["customer_lifetime: spend_independence does not depend on the rows' order, and a customer whose leverage is 1 leaves it not computed"]] <- function(code) {
  # 200 donors over two yearly campaigns: 40 gave once more, one gave at both. That donor alone has x = 2: its leverage is 1,
  # HC3's e / (1 − h) is 0/0, and the p-value was rounding (ok at one order of the rows, failed at another)
  set.seed(71)
  n <- 200
  x <- c(rep(1, 40), 2, rep(0, n - 41))
  d <- data.frame(id = sprintf("d%03d", 1:n), x = x, tx = c(sample(1:2, 40, TRUE), 2, rep(0, n - 41)), T = 2,
                  m = ifelse(x > 0, round(stats::rgamma(n, 6, 1) * 5, 2), NA))
  params <- .clv_params(list(spend = "m", period_unit = "year"), "bg_bb")
  rows_of <- function(frame) {
    df <- tab_results_frame(fit_bg_bb(.clv_task(params, frame, "bg_bb"))$rows)
    df[df$section == "assumption" & df$level == "spend_independence", c("key", "value", "text")]
  }
  first <- rows_of(d)
  said <- function(stat) first$text[first$key == paste0("assumption|spend_independence||", stat)]
  .tab_expect(identical(said("verdict"), "not_computed") && identical(said("advice_id"), "spend_independence_leverage") &&
              grepl("one repeat customer alone", said("advice"), fixed = TRUE) && !"assumption|spend_independence||p_value" %in% first$key,
              "a leverage of 1: not computed, with its own sentence")
  for (seed in 1:4) {
    set.seed(seed)
    .tab_expect(identical(rows_of(d[sample(n), ]), first), "the rows in another order give other numbers (seed ", seed, ")")
  }
  # without such a customer the test runs, and its numbers are the same in any order of the rows, to the last digit
  set.seed(9)
  x2 <- stats::rpois(300, 2) + 1
  m2 <- stats::rgamma(300, 6, 0.2)
  base <- clv_spend_independence(x2, m2)
  .tab_expect(is.finite(base$p_value) && identical(base$why, ""), "a test where the frequencies vary")
  for (seed in 1:4) {
    set.seed(seed)
    o <- sample(300)
    .tab_expect(identical(clv_spend_independence(x2[o], m2[o]), base), "the same test in another order (seed ", seed, ")")
  }
  .tab_expect(identical(clv_spend_independence(c(1, 1, 1, 1, 2), c(5, 6, 7, 8, 9))$why, "leverage") &&
              identical(clv_spend_independence(c(1, 1, 2, 2, 2), c(5, 6, 7, 8, 9))$why, ""), "the leverage of a lone frequency")
}

TAB_COMMON_TESTS[["customer_lifetime: a candidate whose convergence check fails is not converged, for auto and in the rows; an ok check has no advice"]] <- function(code) {
  # 400 customers who never drop out: BG/NBD's optimizer stops with code 0 on a ridge (a 16.9, b 4,973, below the
  # maximum), its Hessian is not positive definite and no estimate is at an edge: its convergence check fails, so it
  # is not converged, neither for auto nor in its rows (MLflow's converged and candidate.bg_nbd.converged)
  set.seed(51)
  n <- 400
  T <- stats::runif(n, 10, 60)
  x <- stats::rpois(n, stats::rgamma(n, 0.8, rate = 4) * T)
  tx <- ifelse(x > 0, T * vapply(x, function(k) if (k == 0) 0 else max(stats::runif(k)), 0), 0)
  d <- data.frame(id = paste0("c", seq_len(n)), x = x, tx = tx, T = T)
  res <- fit_clv(.clv_task(.clv_params(list(purchase_model = "bg_nbd", output = "results")), d, variant = "purchases"))
  df <- tab_results_frame(res$rows)
  .tab_expect(df$value[df$key == "model|||converged"] == 0 && identical(df$text[df$key == "assumption|convergence||verdict"], "failed"),
              "the model row says what the check says")
  s <- list(x = x, tx = tx, T = T, n = n)
  comp <- clv_compress(x, tx, T)
  cands <- lapply(c("bg_nbd", "mbg_nbd"), function(m) {
    p <- clv_add_inference(clv_fit_purchases(m, s, comp, inference = FALSE), s, comp)
    rows <- tab_rows_start(.clv_task(.clv_params(), d))
    clv_check_convergence(rows, list(purchases = p))
    list(model = m, p = p, said = tab_results_frame(rows)$text[tab_results_frame(rows)$stat == "verdict"])
  })
  for (cand in cands) .tab_expect(identical(cand$said == "failed", !cand$p$converged), cand$model, ": the check says ", cand$said, ", converged ", cand$p$converged)
  .tab_expect(!cands[[1L]]$p$converged && cands[[2L]]$p$converged, "BG/NBD on the ridge is not converged, MBG/NBD is")
  # auto takes the converged one even when the ridge's log-likelihood were the higher
  cands[[1L]]$p$log_lik <- cands[[2L]]$p$log_lik + 1
  .tab_expect(clv_choose(cands, "auto") == 2L, "auto chose the model whose convergence check fails")
  # an ok verdict comes with no advice (the advice says what to do about a check that is not ok)
  a <- .clv_check_frame(.clv_check_fit(alpha = 0.01, spend = c(p = 6, q = 2.5, gamma = 15)))
  ok <- a$level[a$stat == "verdict" & a$text == "ok"]
  .tab_expect(all(c("convergence", "frequency_fit", "spend_tail") %in% ok) && !any(a$level %in% ok & a$stat %in% c("advice", "advice_id")),
              "advice next to ok: ", paste(ok, collapse = ", "))
}
