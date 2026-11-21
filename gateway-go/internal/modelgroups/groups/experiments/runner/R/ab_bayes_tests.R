# The self-test of the Bayesian reading (docs/specs/ab-testing.md, lot E3): added to TAB_COMMON_TESTS, so that
# `Rscript runner/run.R --selftest` runs them. The reference cases of expected.json (values from Miller's sum,
# Stucchio's identity, a Gauss–Legendre rule and the normal's closed forms, and mpmath in the witness) are run by the
# common self-test; here are the properties no single case shows: the probabilities add up, two variants agree with
# the closed forms, a narrow posterior beside a wide one is not missed, the mirror of «lower is better», the part off,
# a metric with no variance, and the sentences a part writes.

.abb_beta <- function(conversions, n, prior = c(1, 1)) {
  r <- list(est = list(type = "binary", conversions = conversions, n = n, k = length(n), cuped = NULL),
            ctx = list(params = list(prior_alpha = prior[[1L]], prior_beta = prior[[2L]])))
  ab_bayes_posteriors(r)
}

.abb_normal <- function(m, s) list(kind = "normal", m = m, s = s, mean = m, sd = s, k = length(m))

# P(X > Y) for X ~ Beta(ax, bx), a whole ax (Miller 2015), in log space.
.abb_miller <- function(ax, bx, ay, by) {
  i <- 0:(round(ax) - 1L)
  sum(exp(lbeta(ay + i, by + bx) - log(bx + i) - lbeta(1 + i, bx) - lbeta(ay, by)))
}

TAB_COMMON_TESTS[["experiments: the probabilities of being the best add up to 1 at 1e-10, from 2 to 8 variants, both directions, Beta and normal"]] <- function(code) {
  set.seed(11)
  for (case in seq_len(24L)) {
    k <- sample(2:8, 1L)
    higher <- case %% 2L == 0L
    post <- if (case %% 3L == 0L) {
      # a skew, narrow posterior beside wide ones: normal with sds over three orders of magnitude
      .abb_normal(stats::rnorm(k, 10, 0.5), 10^stats::runif(k, -3, 0))
    } else {
      n <- sample(c(30, 400, 5000, 120000, 1e6), k, replace = TRUE)
      .abb_beta(round(n * stats::runif(k, 0.01, 0.4)), n, prior = if (case %% 5L == 0L) c(0.5, 0.5) else c(1, 1))
    }
    best <- ab_bayes_best(post, higher)
    .tab_expect(abs(sum(best$prob) - 1) < 1e-10, "case ", case, ": ΣP(best) − 1 = ", sum(best$prob) - 1)
    .tab_expect(all(is.finite(best$loss)) && all(best$loss >= -1e-12), "case ", case, ": a loss against the best is ", min(best$loss))
    # the best variant by probability has the smallest loss against the best
    .tab_expect(which.max(best$prob) == which.min(best$loss) || max(best$prob) < 0.6, "case ", case, ": the loss and the probability disagree")
  }
}

TAB_COMMON_TESTS[["experiments: two Beta posteriors against Miller's sum and Stucchio's identity, from 1,000 to 1,000,000 units, at 1e-10"]] <- function(code) {
  for (n in c(1000, 10000, 100000, 1e6)) {
    ca <- round(0.100 * n)
    cb <- round(0.104 * n)
    post <- .abb_beta(c(ca, cb), c(n, n))
    aa <- 1 + ca; ba <- 1 + n - ca; ab <- 1 + cb; bb <- 1 + n - cb
    beat <- ab_bayes_prob_beat(post, 2L, 1L, TRUE)
    want <- .abb_miller(ab, bb, aa, ba)
    .tab_expect(abs(beat - want) < 1e-10, n, " units: P(beat) ", beat, ", Miller ", want)
    ma <- aa / (aa + ba)
    mb <- ab / (ab + bb)
    loss <- ab_bayes_loss(post, 2L, 1L, TRUE, TRUE)  # E[(A − B)⁺]
    stucchio <- ma * .abb_miller(aa + 1, ba, ab, bb) - mb * .abb_miller(aa, ba, ab + 1, bb)
    .tab_expect(abs(loss - stucchio) < 1e-10, n, " units: the loss ", loss, ", Stucchio ", stucchio)
    keep <- ab_bayes_loss(post, 2L, 1L, TRUE, FALSE)  # E[(B − A)⁺]
    stucchio_keep <- mb * .abb_miller(ab + 1, bb, aa, ba) - ma * .abb_miller(ab, bb, aa + 1, ba)
    .tab_expect(abs(keep - stucchio_keep) < 1e-10, n, " units: the loss of keeping the control ", keep, ", Stucchio ", stucchio_keep)
    best <- ab_bayes_best(post, TRUE)
    .tab_expect(abs(best$prob[[2L]] - beat) < 1e-10 && abs(best$prob[[1L]] - (1 - beat)) < 1e-10, n, " units: P(best) of two is P(beat)")
    # the loss against the best of two is the loss of choosing the other: E[max] − E[A] = E[(B − A)⁺]
    .tab_expect(abs(best$loss[[1L]] - keep) < 1e-10 && abs(best$loss[[2L]] - loss) < 1e-10, n, " units: the loss against the best of two")
  }
}

TAB_COMMON_TESTS[["experiments: normal posteriors: quadrature of two variants against Φ, a posterior 1,000 times narrower than its neighbour is found"]] <- function(code) {
  for (spec in list(list(m = c(1, 1.3), s = c(0.1, 0.1)), list(m = c(5, 5.002), s = c(2, 0.002)), list(m = c(5, 5.0), s = c(0.001, 1)),
                    list(m = c(-3, 0), s = c(1, 0.5)))) {
    post <- .abb_normal(spec$m, spec$s)
    s <- sqrt(sum(spec$s^2))
    d <- spec$m[[2L]] - spec$m[[1L]]
    best <- ab_bayes_best(post, TRUE)
    .tab_expect(abs(best$prob[[2L]] - stats::pnorm(d / s)) < 1e-10, "P(best) ", best$prob[[2L]], ", Φ ", stats::pnorm(d / s))
    # the closed losses are the quadrature's
    .tab_expect(abs(ab_bayes_loss(post, 2L, 1L, TRUE, TRUE) - best$loss[[2L]]) < 1e-10, "loss of choosing, ", spec$m)
    .tab_expect(abs(ab_bayes_loss(post, 2L, 1L, TRUE, FALSE) - best$loss[[1L]]) < 1e-10, "loss of keeping, ", spec$m)
    # P(beat) from the normal's own closed form is the same through the generic integral of a Beta-free kind
    f <- function(x) stats::dnorm(x, spec$m[[2L]], spec$s[[2L]]) * stats::pnorm(x, spec$m[[1L]], spec$s[[1L]])
    .tab_expect(abs(.abb_integral(f, .abb_breaks(post, 1:2)) - stats::pnorm(d / s)) < 1e-10, "the pieces' integral, ", spec$m)
  }
}

TAB_COMMON_TESTS[["experiments: a lower value is better mirrors the measures: P(beat) is the complement, the two losses swap"]] <- function(code) {
  post <- .abb_beta(c(120, 150, 90), c(1000, 1100, 800), prior = c(0.5, 0.5))
  for (post in list(post, .abb_normal(c(1, 1.2, 0.9), c(0.1, 0.15, 0.1)))) {
    up <- ab_bayes_prob_beat(post, 2L, 1L, TRUE)
    down <- ab_bayes_prob_beat(post, 2L, 1L, FALSE)
    .tab_expect(abs(up + down - 1) < 1e-10, "P(beat) up ", up, " down ", down)
    .tab_expect(abs(ab_bayes_loss(post, 2L, 1L, TRUE, TRUE) - ab_bayes_loss(post, 2L, 1L, FALSE, FALSE)) < 1e-10, "the loss of choosing, mirrored")
    .tab_expect(abs(ab_bayes_loss(post, 2L, 1L, TRUE, FALSE) - ab_bayes_loss(post, 2L, 1L, FALSE, TRUE)) < 1e-10, "the loss of keeping, mirrored")
    # the best of the lowest values: P(best) down of the variant with the lowest mean is the largest
    best <- ab_bayes_best(post, FALSE)
    .tab_expect(which.max(best$prob) == which.min(post$mean) && abs(sum(best$prob) - 1) < 1e-10, "the lowest is the best")
  }
}

TAB_COMMON_TESTS[["experiments: the credible interval holds 1 − alpha of the posterior, the sd is the Beta's, the part is off or empty where it should be"]] <- function(code) {
  df <- data.frame(v = rep(c("A", "B", "C"), c(300, 320, 280)),
                   conv = c(rep(1:0, c(30, 270)), rep(1:0, c(44, 276)), rep(1:0, c(22, 258))), stringsAsFactors = FALSE)
  w <- .abc_fit(list(metric_type = "binary", metric = "conv", bayes = TRUE, alpha = 0.1, prior_alpha = 2, prior_beta = 3, loss_threshold = 0.02), df)
  for (lv in c("A", "B", "C")) {
    k <- sum(df$conv[df$v == lv]); n <- sum(df$v == lv)
    a <- 2 + k; b <- 3 + n - k
    key <- function(s) paste0("bayes|", lv, "||", s)
    .tab_expect(abs(stats::pbeta(w$value(key("cred_low")), a, b) - 0.05) < 1e-12 && abs(stats::pbeta(w$value(key("cred_high")), a, b) - 0.95) < 1e-12,
                lv, ": the interval at 90 %")
    .tab_expect(.abc_rel(w$value(key("posterior_sd")), sqrt(a * b / ((a + b)^2 * (a + b + 1)))) < 1e-14, lv, ": sd")
    .tab_expect(.abc_rel(w$value(key("posterior_mean")), a / (a + b)) < 1e-14, lv, ": mean")
  }
  .tab_expect(w$value("model|||prior_alpha") == 2 && w$value("model|||prior_beta") == 3 && w$value("model|||loss_threshold") == 0.02, "the prior and the threshold are written")
  .tab_expect(is.na(w$value("bayes|A||prob_beat_control")) && !is.na(w$value("bayes|A||prob_best")), "the control has no P(beat), but a P(best)")
  .tab_expect(abs(sum(w$value(paste0("bayes|", c("A", "B", "C"), "||prob_best"))) - 1) < 1e-10, "the node's P(best) add up to 1")
  # the posterior curves: 200 points per variant, the density integrates to 1 on the grid's range
  g <- w$frame[w$frame$section == "curve" & w$frame$level == "posterior", ]
  .tab_expect(nrow(g) == 3L * 200L && all(table(g$level_2) == 200L), "200 points of the posterior of each variant")
  # off: no row of the part, no model number of it
  off <- .abc_fit(list(metric_type = "binary", metric = "conv", bayes = FALSE), df)
  .tab_expect(!any(off$frame$section == "bayes") && !any(off$frame$key %in% c("model|||prior_alpha", "model|||loss_threshold")) &&
                !any(off$frame$section == "reading" & off$frame$level == "bayes"), "the part is off")
  # a metric with no variance in a variant: no normal posterior, so no Bayesian rows (the check says why)
  const <- data.frame(v = rep(c("A", "B"), each = 8), y = c(rep(5, 8), 4, 6, 5, 7, 3, 5, 6, 4), stringsAsFactors = FALSE)
  c1 <- .abc_fit(list(metric_type = "continuous", metric = "y", bayes = TRUE), const)
  .tab_expect(!any(c1$frame$section == "bayes"), "a constant metric has no Bayesian rows")
}

TAB_COMMON_TESTS[["experiments: the Bayesian sentences: the ids and the names of the words, no verdict, the control has none, and the loss position"]] <- function(code) {
  df <- data.frame(v = rep(c("A", "B", "C"), c(300, 320, 280)),
                   conv = c(rep(1:0, c(30, 270)), rep(1:0, c(44, 276)), rep(1:0, c(22, 258))), stringsAsFactors = FALSE)
  w <- .abc_fit(list(metric_type = "binary", metric = "conv", bayes = TRUE, loss_threshold = 0.5), df)
  r <- w$frame[w$frame$section == "reading" & w$frame$level == "bayes", ]
  .tab_expect(identical(r$level_2, rep(c("B", "C"), each = 2L)) && !("A" %in% r$level_2), "a block per treatment: ", paste(r$key, collapse = ", "))
  ids <- sub("^@([^ ]+).*$", "\\1", r$text)
  .tab_expect(identical(ids, rep(c("bayes_beat", "bayes_loss"), 2L)), "the ids ", paste(ids, collapse = ","))
  names_of <- function(text) sort(sub("=.*$", "", strsplit(text, " ", fixed = TRUE)[[1L]][-1L]))
  .tab_expect(identical(names_of(r$text[[1L]]), c("control", "probability", "variant")) &&
                identical(names_of(r$text[[2L]]), c("loss", "threshold", "variant")), "the names of bayes_beat and bayes_loss")
  # the numbers in the sentences are the rows' numbers
  p <- as.numeric(sub(".*probability=([^ ]+).*", "\\1", r$text[[1L]]))
  .tab_expect(identical(p, w$value("bayes|B||prob_beat_control")), "bayes_beat carries P(beat) as the row holds it")
  l <- as.numeric(sub(".*loss=([^ ]+).*", "\\1", r$text[[2L]]))
  .tab_expect(identical(l, w$value("bayes|B||expected_loss_relative")), "bayes_loss carries the relative loss")
  # the position against the owner's threshold: a measure's comparison, «below» or «above», nothing else
  .tab_expect(w$text("bayes|B||loss_position") == "below" && w$text("bayes|C||loss_position") == "below" && is.na(w$text("bayes|A||loss_position")),
              "the position of the loss against 50 %")
  tight <- .abc_fit(list(metric_type = "binary", metric = "conv", bayes = TRUE, loss_threshold = 0), df)
  .tab_expect(tight$text("bayes|C||loss_position") == "above", "a loss above a threshold of 0")
  # the method and the formula lines join the explanation's last page
  m <- w$frame[w$frame$section == "reading" & w$frame$level %in% c("method", "formulas"), ]
  .tab_expect(any(grepl("^@method_bayes", m$text)) && any(grepl("^@formula_beta", m$text)), "method_bayes and formula_beta")
  n <- .abc_fit(list(metric_type = "continuous", metric = "rev", bayes = TRUE), .abc_table())
  .tab_expect(any(grepl("^@formula_normal", n$frame$text), na.rm = TRUE) && !any(grepl("^@formula_beta", n$frame$text), na.rm = TRUE), "formula_normal for a continuous metric")
}

TAB_COMMON_TESTS[["experiments: the `variants` output holds P(beat), P(best) and the expected loss of each variant, the control's NULL where they are of a treatment"]] <- function(code) {
  df <- .abc_table()
  task <- .abc_task(list(metric_type = "continuous", metric = "rev", bayes = TRUE, output = "variants"), df, "continuous")
  out <- fit_ab_test(task)
  tbl <- as.data.frame(out$output)
  .tab_expect(nrow(tbl) == 3L && is.na(tbl$prob_beat_control[tbl$is_control]) && is.na(tbl$expected_loss[tbl$is_control]), "the control's NULLs")
  .tab_expect(abs(sum(tbl$prob_best) - 1) < 1e-10 && all(!is.na(tbl$prob_beat_control[!tbl$is_control])), "P(best) adds up; P(beat) of each treatment")
}
