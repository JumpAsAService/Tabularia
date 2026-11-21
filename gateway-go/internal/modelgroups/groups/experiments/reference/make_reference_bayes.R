# The reference cases of lot E3, the Bayesian reading (docs/specs/ab-testing.md, «Bayesian (a reading by
# measures)», «Self-test, reference values, witnesses»). Sourced by make_reference.R, whose ab_case, add_case and
# read_table it uses.
#
# Each case runs one of E2's tables with the Bayesian part on. Its posteriors come from the numbers E2's own case
# computes from the raw rows (the counts, the means and the standard errors of its expected values), and the measures
# are computed here WITHOUT the runner's method (`integrate`):
#
#   P(t ahead of c), the losses, binary, Beta(1, 1)   Miller's closed sum and Stucchio's identity (Miller 2015,
#                                                       Stucchio 2015), exact for a whole α
#   every other measure                                a composite Gauss–Legendre rule (24 nodes on 1,500 equal pieces
#                                                       of [q(1e-15), q(1 − 1e-15)]) written here
#   normal posteriors, two variants                    Φ and φ in closed form
#
# For the case on Miller's sums the two ways are compared and the script stops where they differ by more than 1e-10.
# The Python witness (make_witness.py: bayes_witness) recomputes every value with mpmath at 30 digits. The cases
# carry only the Bayesian keys and the reading: E2's cases hold the rest on the same tables, so the self-test's
# ConfigMap grows by this file's cases only (measured in the spec's «As built / E3»).

# ── a reference quadrature ───────────────────────────────────────────────────────
.gl <- local({
  n <- 24L
  i <- seq_len(n - 1L)
  b <- i / sqrt(4 * i^2 - 1)
  j <- matrix(0, n, n)
  j[cbind(i, i + 1L)] <- b
  j[cbind(i + 1L, i)] <- b
  e <- eigen(j, symmetric = TRUE)
  o <- order(e$values)
  list(x = e$values[o], w = 2 * e$vectors[1L, o]^2)
})

gl_integral <- function(f, lo, hi, panels = 1500L) {
  edges <- seq(lo, hi, length.out = panels + 1L)
  half <- (edges[-1L] - edges[-length(edges)]) / 2
  mid <- (edges[-1L] + edges[-length(edges)]) / 2
  x <- as.vector(outer(.gl$x, half) + rep(mid, each = length(.gl$x)))
  w <- as.vector(outer(.gl$w, half))
  sum(w * f(x))
}

# Miller's closed sum: P(X > Y) for X ~ Beta(ax, bx) with a whole ax, Y ~ Beta(ay, by).
p_greater <- function(ax, bx, ay, by) {
  i <- 0:(round(ax) - 1L)
  sum(exp(lbeta(ay + i, by + bx) - log(bx + i) - lbeta(1 + i, bx) - lbeta(ay, by)))
}

# ── the measures of a case ───────────────────────────────────────────────────────
# post: kind, per variant a, b (Beta) or m, s (normal); higher; control (an index).
bayes_reference <- function(post, labels, control, higher, alpha, threshold) {
  k <- length(labels)
  beta <- post$kind == "beta"
  cdf <- function(j, x, lower = TRUE) if (beta) pbeta(x, post$a[[j]], post$b[[j]], lower.tail = lower) else pnorm(x, post$m[[j]], post$s[[j]], lower.tail = lower)
  pdf <- function(j, x) if (beta) dbeta(x, post$a[[j]], post$b[[j]]) else dnorm(x, post$m[[j]], post$s[[j]])
  qf <- function(j, p, lower = TRUE) if (beta) qbeta(p, post$a[[j]], post$b[[j]], lower.tail = lower) else qnorm(p, post$m[[j]], post$s[[j]], lower.tail = lower)
  mean_j <- if (beta) post$a / (post$a + post$b) else post$m
  sd_j <- if (beta) sqrt(post$a * post$b / ((post$a + post$b)^2 * (post$a + post$b + 1))) else post$s
  behind <- function(j, x) cdf(j, x, lower = higher)
  ahead <- function(j, x) cdf(j, x, lower = !higher)
  span <- function(js) c(min(vapply(js, function(j) qf(j, 1e-15), 0)), max(vapply(js, function(j) qf(j, 1e-15, lower = FALSE), 0)))
  integral <- function(f, js) { s <- span(js); gl_integral(f, s[[1L]], s[[2L]]) }
  base <- abs(mean_j[[control]])
  sign <- if (higher) 1 else -1
  out <- list()
  for (j in seq_len(k)) {
    others <- setdiff(seq_len(k), j)
    pb <- integral(function(x) { v <- pdf(j, x); for (o in others) v <- v * behind(o, x); v }, seq_len(k))
    lb <- integral(function(x) { v <- rep(1, length(x)); for (o in seq_len(k)) v <- v * behind(o, x); behind(j, x) - v }, seq_len(k))
    x <- list(label = labels[[j]], mean = mean_j[[j]], sd = sd_j[[j]], low = qf(j, alpha / 2), high = qf(j, alpha / 2, lower = FALSE),
              prob_best = pb, loss_best = lb, loss_best_relative = lb / base)
    if (j != control) {
      t <- j
      c <- control
      d <- (mean_j[[t]] - mean_j[[c]]) * sign
      if (beta) {
        x$prob_beat <- integral(function(y) pdf(t, y) * behind(c, y), c(t, c))
        x$loss <- integral(function(y) behind(t, y) * ahead(c, y), c(t, c))
        x$loss_control <- integral(function(y) behind(c, y) * ahead(t, y), c(t, c))
        if (higher && all(round(post$a[c(t, c)]) == post$a[c(t, c)])) {
          # Miller and Stucchio, exact: the two ways must agree
          pm <- p_greater(post$a[[t]], post$b[[t]], post$a[[c]], post$b[[c]])
          lm <- mean_j[[c]] * p_greater(post$a[[c]] + 1, post$b[[c]], post$a[[t]], post$b[[t]]) -
            mean_j[[t]] * p_greater(post$a[[c]], post$b[[c]], post$a[[t]] + 1, post$b[[t]])
          km <- mean_j[[t]] * p_greater(post$a[[t]] + 1, post$b[[t]], post$a[[c]], post$b[[c]]) -
            mean_j[[c]] * p_greater(post$a[[t]], post$b[[t]], post$a[[c]] + 1, post$b[[c]])
          gap <- max(abs(pm - x$prob_beat), abs(lm - x$loss), abs(km - x$loss_control))
          if (gap > 1e-10) stop("Miller/Stucchio and the quadrature differ by ", gap, " in ", labels[[j]])
          x$prob_beat <- pm
          x$loss <- lm
          x$loss_control <- km
        }
      } else {
        s <- sqrt(post$s[[t]]^2 + post$s[[c]]^2)
        z <- d / s
        x$prob_beat <- pnorm(z)
        x$loss <- s * dnorm(z) - d * pnorm(-z)
        x$loss_control <- s * dnorm(z) + d * pnorm(z)
      }
      x$loss_relative <- x$loss / base
      x$loss_control_relative <- x$loss_control / base
      x$position <- if (x$loss_relative <= threshold) "below" else "above"
    }
    out[[j]] <- x
  }
  out
}

# One case: E2's case on the same table (its reading, its params), with the Bayesian part on and its expected values
# replaced by the Bayesian ones.
bayes_case <- function(id, data, types, params, note = NULL) {
  p <- utils::modifyList(list(higher_is_better = TRUE, alpha = 0.05, loss_threshold = 0.001, prior_alpha = 1, prior_beta = 1), params)
  case <- ab_case(id, data, types, c(params, list(sequential = FALSE)), note)
  have <- function(key) { e <- Filter(function(e) identical(e$key, key), case$expect); if (length(e)) e[[1L]]$value else NULL }
  keys <- vapply(case$expect, `[[`, "", "key")
  labels <- sub("^variant\\|(.*)\\|\\|n$", "\\1", keys[grepl("^variant\\|.*\\|\\|n$", keys)])
  control <- match(p[["control"]], labels)
  n <- vapply(labels, function(l) have(paste0("variant|", l, "||n")), 0)
  m <- vapply(labels, function(l) have(paste0("variant|", l, "||mean")), 0)
  se <- vapply(labels, function(l) have(paste0("variant|", l, "||std_error")), 0)
  cuped <- !is.null(p[["covariate"]])
  binary <- identical(p[["metric_type"]], "binary") && !cuped
  post <- if (binary) {
    k <- round(m * n)
    list(kind = "beta", a = p$prior_alpha + k, b = p$prior_beta + n - k)
  } else {
    list(kind = "normal", m = m, s = se)
  }
  ms <- bayes_reference(post, labels, control, isTRUE(p$higher_is_better), p$alpha, p$loss_threshold)
  ex <- list()
  num <- function(key, value, abs = NULL) {
    e <- list(key = key, value = if (is.na(value)) NULL else value)
    if (!is.null(abs)) e$abs <- abs
    ex[[length(ex) + 1L]] <<- e
  }
  tol <- 1e-10  # the spec's tolerance for the probabilities, the losses and their ratios
  for (x in ms) {
    lv <- paste0("bayes|", x$label, "||")
    num(paste0(lv, "posterior_mean"), x$mean)
    num(paste0(lv, "posterior_sd"), x$sd)
    num(paste0(lv, "cred_low"), x$low)
    num(paste0(lv, "cred_high"), x$high)
    num(paste0(lv, "prob_best"), x$prob_best, tol)
    num(paste0(lv, "expected_loss_best"), x$loss_best, tol)
    num(paste0(lv, "expected_loss_best_relative"), x$loss_best_relative, tol)
    if (!is.null(x$prob_beat)) {
      num(paste0(lv, "prob_beat_control"), x$prob_beat, tol)
      num(paste0(lv, "expected_loss"), x$loss, tol)
      num(paste0(lv, "expected_loss_relative"), x$loss_relative, tol)
      num(paste0(lv, "expected_loss_control"), x$loss_control, tol)
      num(paste0(lv, "expected_loss_control_relative"), x$loss_control_relative, tol)
      ex[[length(ex) + 1L]] <- list(key = paste0(lv, "loss_position"), text = x$position)
    }
  }
  num("model|||loss_threshold", p$loss_threshold)
  if (binary) {
    num("model|||prior_alpha", p$prior_alpha)
    num("model|||prior_beta", p$prior_beta)
  }
  case$expect <- ex
  # the reading: the Bayesian block after the cap and before the power; its method and its formula at the ends
  at <- function(r) r[[1L]]
  add_line <- function(reading, block, id) {
    last <- max(which(startsWith(vapply(reading, at, ""), paste0("reading|", block, "||line_"))))
    count <- sum(startsWith(vapply(reading, at, ""), paste0("reading|", block, "||line_")))
    append(reading, list(list(paste0("reading|", block, "||line_", count + 1L), id)), after = last)
  }
  reading <- case$reading
  bayes_lines <- list()
  for (j in setdiff(seq_along(labels), control)) {
    bayes_lines[[length(bayes_lines) + 1L]] <- list(paste0("reading|bayes|", labels[[j]], "|line_1"), "bayes_beat")
    bayes_lines[[length(bayes_lines) + 1L]] <- list(paste0("reading|bayes|", labels[[j]], "|line_2"), "bayes_loss")
  }
  first_after <- which(grepl("^reading\\|(power|method|formulas)\\|", vapply(reading, at, "")))[[1L]]
  reading <- append(reading, bayes_lines, after = first_after - 1L)
  reading <- add_line(reading, "method", "method_bayes")
  reading <- add_line(reading, "formulas", if (binary) "formula_beta" else "formula_normal")
  case$reading <- reading
  case
}

# ── the cases ────────────────────────────────────────────────────────────────────
add_case(bayes_case("bayes_binary_two", "binary_two.csv", list(variant = "text", converted = "number"),
                    list(variant = "variant", control = "A", metric_type = "binary", metric = "converted"),
                    "two Beta(1, 1) posteriors: Miller's sum and Stucchio's identity"))
add_case(bayes_case("bayes_binary_prior_less", "binary_two.csv", list(variant = "text", converted = "number"),
                    list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", prior_alpha = 0.5, prior_beta = 0.5,
                         higher_is_better = FALSE, loss_threshold = 0.05),
                    "Beta(0.5, 0.5) and a lower value is better: the mirrored losses; the loss is above the threshold"))
add_case(bayes_case("bayes_continuous_three", "continuous_three.csv", list(variant = "text", revenue = "number"),
                    list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", loss_threshold = 0.01),
                    "three normal posteriors: P(best) of each, the losses against the best"))
add_case(bayes_case("bayes_ratio_four", "ratio_four.csv", list(variant = "text", clicks = "number", impressions = "number"),
                    list(variant = "variant", control = "A", metric_type = "ratio", numerator = "clicks", denominator = "impressions"),
                    "four normal posteriors of a ratio"))
add_case(bayes_case("bayes_cuped_binary", "srm.csv", list(variant = "text", converted = "number", visits = "number"),
                    list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", covariate = "visits"),
                    "a binary metric with CUPED: the normal posterior of the adjusted rate, no prior"))
add_case(bayes_case("bayes_edge_control_zero", "edge_control_zero.csv", list(variant = "text", converted = "number"),
                    list(variant = "variant", control = "A", metric_type = "binary", metric = "converted"),
                    "a control without conversions: Beta(1, 21), the losses relative to its posterior mean"))
