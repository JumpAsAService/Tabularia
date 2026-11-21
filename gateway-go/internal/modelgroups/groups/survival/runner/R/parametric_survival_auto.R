# The automatic choice of the node «Parametric survival» (docs/specs/statistical-models.md,
# «The node «Parametric survival»» → «Automatic choice», lot P3): `distribution = auto`.
# `fit_parametric_survival` finds `ps_auto` by name (parametric_survival.R) and takes from it the
# fit of the distribution chosen and a function that writes what belongs to the choice.
#
#   ps_auto(task, prepared) → list(fitted, write(rows), fits)
#
# The candidates are the six distributions, fitted in order of cost (exponential, Weibull,
# log-normal, log-logistic, Gompertz, then the generalized gamma, by far the dearest), each with the same predictors, strata
# and weights, and each with the second start `ps_fit` gives it. The generalized gamma (lot P12)
# is taken by Newton steps from the rows' own derivatives straight from the point nested in it of
# the better of the Weibull and the log-normal just fitted; only when they do not reach its maximum
# is it fitted by flexsurv from three starts (flexsurv's own, the Weibull's, the log-normal's).
#
#   admissible   converged with a finite log-likelihood. The candidates are compared by
#                log-likelihood, AIC and BIC; the second start is tried only when the first fails;
#   checked      the convergence check (ps_newton: its Newton steps and the covariance from the
#                rows' own derivatives, NULL when the Hessian is not negative definite) is made only
#                for the candidates the choice reaches (owner, 2026-11-20: nothing that costs eons at
#                run time): the lowest AIC and the one the parsimony rule takes. One that fails it is
#                `not_converged` and out of the choice, and the choice is made again without it.
#                A candidate never checked has no `converged` row (neither 1 nor 0, as in the
#                customer lifetime's choice);
#   chosen       the admissible candidate with the lowest AIC (a tie: fewer parameters, then the
#                order above) — unless a simpler distribution nested in it is admissible and
#                within 2 of its AIC (PS_AUTO_NESTED): then the simplest such one (the owner's
#                rule of 2026-11-20). `selection` stays `auto`; `parsimony` is 1 on the chosen
#                row when the rule moved the choice;
#   ΔAIC, weights against the lowest AIC, over the admissible ones; a candidate that is not stays
#                in the table with its status and, when finite, its log-likelihood, AIC and BIC;
#   no candidate admissible → the refusal `no_candidate`; the exponential, the cheapest, with too
#                few rows → the refusal `too_few_rows` (the others, status `too_few_rows`);
#   time         before each candidate, when more than half of the step's `time_budget_seconds`
#                has passed (counted from the runner's start), it and the later ones are not
#                fitted: status `time`.
#
# What it writes beside the node's own sections: the section `candidate` (one row group per
# distribution), the notes `auto_close`, `auto_not_converged`, `auto_time` (model|||note_<id>, each
# `@id name=value …`, which the report words in the page's language: lists joined by «,»,
# distributions as their slugs, numbers with a point) and the chart `candidates` (curve: `km` and each admissible distribution's
# mean fitted survival, stat `surv`).
#
# Self-test hook: `task$.selftest_clock`, a function giving the seconds elapsed, in place of the
# process's clock. The reference cases are made by reference/make_reference_auto.R.

PS_AUTO_ORDER <- c("exponential", "weibull", "lognormal", "loglogistic", "gompertz", "gengamma")
# The distributions nested in each (the owner's rule): the exponential in the Weibull and in the
# Gompertz (shape 0), the Weibull and the log-normal in the generalized gamma, and through them
# the exponential.
PS_AUTO_NESTED <- list(weibull = "exponential", gompertz = "exponential", gengamma = c("exponential", "weibull", "lognormal"))
PS_AUTO_TIME_SHARE <- 0.5      # of the step's time budget, after which no candidate is started
PS_AUTO_CURVE_ROWS <- 5000L    # rows the mean fitted curves are taken over, at most (equally spaced)
PS_AUTO_CURVE_TIMES <- 100L    # the curves' times, from 0 to the last observed time

# The number of parameters a candidate has with this design (what ps_refuse_too_few_rows counts).
.psa_k <- function(m) m$nb + length(m$i_terms) + length(m$i_strata) + length(m$i_anc)

# The note's text: `@id name=value name=value`.
.psa_note <- function(id, ...) {
  a <- list(...)
  paste0("@", id, " ", paste(paste0(names(a), "=", vapply(a, function(v) paste(v, collapse = ","), "")), collapse = " "))
}

# The choice, from the admissible candidates' AIC and parameters, listed in the order of cost:
# the lowest AIC, then the parsimony rule (`margin`: the node's option `aic_margin`). Pure: the self-test runs it on made-up numbers.
# Returns the index of the lowest, of the chosen one, and whether the rule moved the choice.
ps_auto_choose <- function(distributions, aic, k, margin) {
  by <- order(round(aic, 9L), k, seq_along(aic))
  lowest <- by[[1L]]
  chosen <- lowest
  nested <- match(PS_AUTO_NESTED[[distributions[[lowest]]]] %||% character(), distributions)
  nested <- nested[!is.na(nested)]
  near <- nested[aic[nested] - aic[[lowest]] <= margin + 1e-9]
  if (length(near)) chosen <- near[order(k[near], aic[near], near)][[1L]]
  list(lowest = lowest, chosen = chosen, parsimony = chosen != lowest)
}

# ΔAIC and the Akaike weights of AICs (the admissible ones).
ps_auto_weights <- function(aic) {
  delta <- aic - min(aic)
  w <- exp(-delta / 2)
  list(delta = delta, weight = w / sum(w))
}

ps_auto <- function(task, prepared) {
  budget <- suppressWarnings(as.numeric(task$limits$time_budget_seconds %||% NA_real_))
  clock <- task$.selftest_clock %||% function() proc.time()[["elapsed"]]
  late <- function() length(budget) == 1L && is.finite(budget) && budget > 0 && clock() > PS_AUTO_TIME_SHARE * budget
  # the node's option (the catalogue's default, 2, when the rule of the owner stands)
  margin <- as.numeric(prepared$p$aic_margin)
  if (length(margin) != 1L || !is.finite(margin) || margin < 0) stop("the catalogue gave no AIC margin")
  n_terms <- ncol(prepared$X)
  n_strata <- length(prepared$strata_levels)
  # the cheapest candidate has nothing to fit with: the node refuses as a single distribution does
  ps_refuse_too_few_rows(prepared, PS_AUTO_ORDER[[1L]])

  cands <- list()
  fits <- list()
  out_of_time <- FALSE
  for (d in PS_AUTO_ORDER) {
    m <- ps_spec(d, n_terms, n_strata)
    entry <- list(distribution = d, k = .psa_k(m), status = NA_character_, loglik = NA_real_, converged = FALSE,
                  admissible = FALSE, reason = "")
    if (out_of_time || late()) {
      out_of_time <- TRUE
      entry$status <- "time"
    } else if (length(prepared$time) < entry$k + 2L) {
      entry$status <- "too_few_rows"
    } else {
      starts <- if (d == "gengamma") list(weibull = fits$weibull, lognormal = fits$lognormal) else NULL
      fit_d <- function() ps_fit(prepared, d, starts = starts)
      if (d == "gengamma") {
        # lot P12: the generalized gamma from the point of the better of the Weibull and the
        # log-normal already fitted, by Newton steps from the rows' own derivatives (BFGS from three
        # starts took 10.7 s of the choice's 17.6 on 5,000 rows and 33 parameters); flexsurv's fit
        # from the three starts when they do not reach the maximum
        nested <- Filter(Negate(is.null), list(fits$weibull, fits$lognormal))
        if (length(nested)) {
          best <- nested[[which.max(vapply(nested, function(x) x$loglik, numeric(1)))]]
          fit_d <- function() ps_fit_nested(prepared, d, ps_nested_point(best, d, prepared), function() ps_fit(prepared, d, starts = starts))
        }
      }
      f <- tryCatch(fit_d(), error = function(e) {
        ps_caught(e)
        list(converged = FALSE, reason = conditionMessage(e), loglik = NA_real_)
      })
      entry$loglik <- if (is.finite(f$loglik %||% NA_real_)) f$loglik else NA_real_
      entry$converged <- isTRUE(f$converged)
      if (!entry$converged) {
        entry$status <- "not_converged"
        entry$reason <- f$reason %||% ""
      } else {
        entry$status <- "ok"
        entry$admissible <- TRUE
        fits[[d]] <- f
      }
    }
    cands[[d]] <- entry
  }

  # the choice, and the convergence check of the candidates it reaches (the lowest AIC, the one the
  # parsimony rule takes): ps_newton takes each to its maximum (every ΔAIC is read from the lowest
  # AIC, which BFGS leaves a little short of it: up to 0.03 for the generalized gamma) and gives its
  # covariance, NULL when the Hessian is not negative definite. A candidate that fails is not
  # converged and leaves the choice, which is made again (a degenerate generalized gamma, Q = −27 and
  # no standard error, had the lowest AIC and was chosen with `converged` 1)
  repeat {
    adm <- PS_AUTO_ORDER[vapply(cands, function(e) e$admissible, logical(1))]
    if (!length(adm)) .psa_refuse_no_candidate(cands)
    aic <- vapply(adm, function(d) -2 * cands[[d]]$loglik + 2 * cands[[d]]$k, numeric(1))
    pick <- ps_auto_choose(adm, aic, vapply(adm, function(d) cands[[d]]$k, numeric(1)), margin)
    todo <- unique(adm[c(pick$lowest, pick$chosen)])
    todo <- todo[!vapply(todo, function(d) isTRUE(cands[[d]]$checked), logical(1))]
    if (!length(todo)) break
    d <- todo[[1L]]
    checked <- ps_auto_check(fits[[d]])
    cands[[d]]$checked <- TRUE
    if (is.null(checked)) {
      cands[[d]]$converged <- FALSE
      cands[[d]]$admissible <- FALSE
      cands[[d]]$status <- "not_converged"
      cands[[d]]$reason <- "the Hessian is not negative definite at the estimates: they are not a maximum"
      fits[[d]] <- NULL
    } else {
      cands[[d]]$loglik <- checked$loglik
      fits[[d]] <- checked
    }
  }
  lowest <- adm[[pick$lowest]]
  chosen <- adm[[pick$chosen]]
  max_points <- tab_limit(task, "max_chart_points")
  write <- function(rows) .psa_write(rows, prepared, cands, fits, adm, lowest, chosen, pick$parsimony, max_points, margin)
  # `fits`: the candidates that converged, which the checks and the LR test against the exponential
  # reuse instead of fitting them again (lot P12)
  list(fitted = fits[[chosen]], write = write, fits = fits)
}

# The convergence check of a candidate: its fit after ps_newton (`checked` TRUE: the node does not
# take the steps again), or NULL when the steps leave no covariance (the Hessian is not negative
# definite) or no finite log-likelihood. The log-likelihood is never lower than the candidate's.
ps_auto_check <- function(fitted) {
  out <- tryCatch(ps_newton(fitted), error = function(e) {
    ps_caught(e)
    NULL
  })
  if (is.null(out) || is.null(out$vcov) || !is.finite(out$loglik)) return(NULL)
  out$checked <- TRUE
  out
}

.psa_refuse_no_candidate <- function(cands) {
  why <- vapply(cands, function(e) paste0(PS_DISTRIBUTIONS[[e$distribution]]$label, ": ", switch(e$status,
    not_converged = "did not converge", time = "no time left",
    too_few_rows = "too few rows", e$status)), "")
  tab_refuse("no_candidate", paste0("no distribution could be fitted to these rows (", paste(why, collapse = "; "),
                                    "): fewer predictors, merge rare levels, or check the times"),
             list(candidates = as.list(vapply(cands, function(e) e$status, ""))))
}

# The log-likelihood the node wrote for the fit chosen (after the Newton steps): the candidate
# row of that distribution says the same number, to the bit.
.psa_final_loglik <- function(rows) {
  for (chunk in rows$chunks) {
    i <- match("model|||log_lik", chunk$key)
    if (!is.na(i)) return(chunk$value[[i]])
  }
  NA_real_
}

.psa_write <- function(rows, prepared, cands, fits, adm, lowest, chosen, parsimony, max_points, margin) {
  n <- length(prepared$time)
  final <- .psa_final_loglik(rows)
  if (is.finite(final)) cands[[chosen]]$loglik <- final
  aic <- vapply(cands, function(e) if (is.finite(e$loglik)) -2 * e$loglik + 2 * e$k else NA_real_, numeric(1))
  bic <- vapply(cands, function(e) if (is.finite(e$loglik)) -2 * e$loglik + e$k * log(n) else NA_real_, numeric(1))
  adm_at <- match(adm, names(cands))
  w <- ps_auto_weights(aic[adm_at])
  delta <- stats::setNames(rep(NA_real_, length(cands)), names(cands))
  weight <- delta
  delta[adm_at] <- w$delta
  weight[adm_at] <- w$weight

  for (d in names(cands)) {
    e <- cands[[d]]
    tab_number(rows, "candidate", "n_parameters", e$k, d)
    if (is.finite(e$loglik)) {
      tab_number(rows, "candidate", "log_lik", e$loglik, d)
      tab_number(rows, "candidate", "aic", aic[[d]], d)
      tab_number(rows, "candidate", "bic", bic[[d]], d)
    }
    if (e$admissible) {
      tab_number(rows, "candidate", "delta_aic", delta[[d]], d)
      tab_number(rows, "candidate", "akaike_weight", weight[[d]], d)
    }
    # a candidate fitted but never checked (the choice stopped before it) has no `converged` row: it was
    # not shown to be a maximum, and a 0 would say it was shown not to be
    if (!identical(e$status, "ok") || isTRUE(e$checked)) tab_number(rows, "candidate", "converged", as.integer(e$converged), d)
    tab_number(rows, "candidate", "admissible", as.integer(e$admissible), d)
    tab_number(rows, "candidate", "chosen", as.integer(d == chosen), d)
    tab_number(rows, "candidate", "parsimony", as.integer(parsimony && d == chosen), d)
    tab_text(rows, "candidate", "status", e$status, d)
  }

  tab_number(rows, "model", "aic_margin", margin)
  if (parsimony) {
    tab_text(rows, "model", "note_auto_close", .psa_note("auto_close", distribution = chosen, other = lowest, margin = format(margin),
                                                         delta_aic = formatC(delta[[chosen]], format = "f", digits = 2L),
                                                         weight = formatC(weight[[chosen]], format = "f", digits = 2L)))
  }
  failed <- names(cands)[vapply(cands, function(e) identical(e$status, "not_converged"), logical(1))]
  if (length(failed)) tab_text(rows, "model", "note_auto_not_converged", .psa_note("auto_not_converged", names = failed))
  skipped <- names(cands)[vapply(cands, function(e) identical(e$status, "time"), logical(1))]
  if (length(skipped)) tab_text(rows, "model", "note_auto_time", .psa_note("auto_time", names = skipped))
  .psa_curves(rows, prepared, fits, adm, max_points)
  invisible(rows)
}

# The chart `candidates`: Kaplan–Meier's survival (steps) and, for each admissible distribution,
# the mean over the rows of its fitted S(t | x), at PS_AUTO_CURVE_TIMES times from 0 to the last
# observed time. The mean runs over at most PS_AUTO_CURVE_ROWS rows, equally spaced (no random
# choice: a table of a million rows costs as much as one of 5,000).
.psa_curves <- function(rows, prepared, fits, adm, max_points) {
  time <- prepared$time
  # delayed entry: Kaplan–Meier by survfit(Surv(start, time, status)), read from the first time with
  # enough rows at risk (psc_entry_floor, as the checks' chart), and the fitted curves conditional on lasting until then
  t0 <- 0
  if (is.null(prepared$start)) {
    km <- survival::survfit(survival::Surv(time, prepared$status) ~ 1, weights = prepared$w)
  } else {
    t0 <- psc_entry_floor(prepared$start, time, prepared$status, prepared$w, PS_CHECK_LIMITS$km_min_at_risk)
    late <- time > t0
    km <- survival::survfit(survival::Surv(pmax(prepared$start[late], t0), time[late], prepared$status[late]) ~ 1,
                            weights = prepared$w[late])
  }
  steps <- tab_thin_steps(c(t0, km$time), c(1, km$surv), max = max_points)
  tab_points(rows, "curve", "candidates", "surv", steps$x, steps$y, level_2 = "km")
  at <- unique(round(seq(1, length(time), length.out = min(length(time), PS_AUTO_CURVE_ROWS))))
  w <- prepared$w[at]
  grid <- seq(t0, max(time), length.out = PS_AUTO_CURVE_TIMES)
  for (d in adm) {
    f <- fits[[d]]
    pars <- ps_natural(f$spec, f$par, prepared$X[at, , drop = FALSE], prepared$S[at, , drop = FALSE])
    base <- if (t0 > 0) sum(w * exp(ps_log_survival(f$spec, rep(t0, length(at)), pars))) else sum(w) # Σ w S(t0): the rows' chance of lasting until t0
    s <- vapply(grid, function(t) {
      if (t <= t0) return(1)
      sum(w * exp(ps_log_survival(f$spec, t, pars))) / base
    }, numeric(1))
    tab_points(rows, "curve", "candidates", "surv", grid, s, level_2 = d)
  }
}

# ── the automatic choice's own tests, run by the group's self-test with the common library's ──
.psa_test_params <- function(...) {
  utils::modifyList(list(time = "days", event = "status", event_value = "failed", predictors = list("load", "line"),
                         weights = "w", distribution = "auto"), list(...))
}

TAB_COMMON_TESTS[["parametric survival, auto: the lowest AIC, the parsimony rule on made-up numbers, the Akaike weights"]] <- function(code) {
  k_all <- c(exponential = 1, weibull = 2, lognormal = 2, loglogistic = 2, gompertz = 2, gengamma = 3)
  # the AICs by name; `out` the distributions not admissible
  choose <- function(aic, out = character()) {
    full <- c(exponential = 140, weibull = 140, lognormal = 140, loglogistic = 140, gompertz = 140, gengamma = 140)
    full[names(aic)] <- aic
    d <- setdiff(PS_AUTO_ORDER, out)
    r <- ps_auto_choose(d, full[d], k_all[d], 2)
    list(lowest = d[[r$lowest]], chosen = d[[r$chosen]], parsimony = r$parsimony)
  }
  r <- choose(c(exponential = 110, weibull = 100, loglogistic = 103, gengamma = 101))
  .tab_expect(r$chosen == "weibull" && !r$parsimony, "the lowest AIC: ", r$chosen)
  r <- choose(c(weibull = 100.3, gengamma = 100))
  .tab_expect(r$lowest == "gengamma" && r$chosen == "weibull" && r$parsimony, "parsimony: ", r$chosen)
  r <- choose(c(weibull = 102, gengamma = 100))
  .tab_expect(r$chosen == "weibull" && r$parsimony, "2 away: ", r$chosen)
  r <- choose(c(weibull = 102.01, gengamma = 100))
  .tab_expect(r$chosen == "gengamma" && !r$parsimony, "2.01 away: ", r$chosen)
  r <- choose(c(exponential = 101, weibull = 100.5, gengamma = 100))
  .tab_expect(r$chosen == "exponential" && r$parsimony, "the simplest: ", r$chosen)
  r <- choose(c(exponential = 102.5, weibull = 101, gengamma = 100))
  .tab_expect(r$chosen == "weibull" && r$parsimony, "not chained: ", r$chosen)
  r <- choose(c(lognormal = 101, gengamma = 100))
  .tab_expect(r$chosen == "lognormal" && r$parsimony, "the log-normal: ", r$chosen)
  r <- choose(c(exponential = 101, gompertz = 100))
  .tab_expect(r$lowest == "gompertz" && r$chosen == "exponential" && r$parsimony, "Gompertz, the exponential nested in it (shape 0): ", r$chosen)
  r <- choose(c(exponential = 102.01, gompertz = 100))
  .tab_expect(r$chosen == "gompertz" && !r$parsimony, "Gompertz, the exponential 2.01 away: ", r$chosen)
  r <- choose(c(weibull = 100.5, gompertz = 100))
  .tab_expect(r$chosen == "gompertz" && !r$parsimony, "Gompertz: the Weibull is not nested in it: ", r$chosen)
  r <- choose(c(weibull = 100.3, gengamma = 100), out = "weibull")
  .tab_expect(r$chosen == "gengamma", "a distribution out: ", r$chosen)
  r <- choose(c(weibull = 100, lognormal = 100))
  .tab_expect(r$chosen == "weibull" && !r$parsimony, "a tie: the order: ", r$chosen)
  r <- choose(c(exponential = 100, weibull = 100))
  .tab_expect(r$chosen == "exponential", "a tie: fewer parameters: ", r$chosen)
  w <- ps_auto_weights(c(1344.915, 1345.220, 1347.1))
  # the margin is the option's: 0 never moves the choice, 5 takes the exponential 4 above
  r1 <- ps_auto_choose(c("weibull", "gengamma"), c(100.3, 100), c(2, 3), 0)
  r2 <- ps_auto_choose(c("exponential", "gengamma"), c(104, 100), c(1, 3), 5)
  .tab_expect(!r1$parsimony && r2$chosen == 1L && r2$parsimony, "the margin")
  .tab_expect(abs(sum(w$weight) - 1) < 1e-15 && w$delta[[1L]] == 0 && abs(w$delta[[2L]] - 0.305) < 1e-9 &&
              abs(w$weight[[2L]] / w$weight[[1L]] - exp(-0.305 / 2)) < 1e-12, "the weights")
}

TAB_COMMON_TESTS[["parametric survival, auto: the candidates' rows agree with the fit chosen and with a single fit; the card says auto"]] <- function(code) {
  df <- .ps_test_table(code)
  fit <- fit_parametric_survival(.ps_test_task(df, .psa_test_params()))
  r <- tab_results_frame(fit$rows)
  .tab_expect(!length(tab_check_declared(r, "parametric_survival", "auto")), "the rows are not what the variant declares: ",
              paste(tab_check_declared(r, "parametric_survival", "auto"), collapse = "; "))
  get <- function(key) r$value[match(key, r$key)]
  chosen <- r$text[match("model|||distribution", r$key)]
  .tab_expect(identical(r$text[match("model|||selection", r$key)], "auto") && identical(fit$card$parametric_survival$selection, "auto"), "selection")
  one <- tab_results_frame(fit_parametric_survival(.ps_test_task(df, .psa_test_params(distribution = chosen)))$rows)
  for (key in c("model|||log_lik", "model|||aic", "model|||n_parameters", "coef|load||estimate", "coef|load||std_error")) {
    .tab_expect(identical(get(key), one$value[match(key, one$key)]), key, " differs from the single fit's")
  }
  # the candidate of the chosen one says the node's own numbers, to the bit
  .tab_expect(identical(get(paste0("candidate|", chosen, "||log_lik")), get("model|||log_lik")) &&
              identical(get(paste0("candidate|", chosen, "||chosen")), 1), "the chosen candidate's row")
  cand <- PS_AUTO_ORDER
  weights <- vapply(cand, function(d) get(paste0("candidate|", d, "||akaike_weight")), numeric(1))
  delta <- vapply(cand, function(d) get(paste0("candidate|", d, "||delta_aic")), numeric(1))
  aic <- vapply(cand, function(d) get(paste0("candidate|", d, "||aic")), numeric(1))
  .tab_expect(abs(sum(weights) - 1) < 1e-12 && min(delta) == 0 && max(abs(delta - (aic - min(aic)))) < 1e-9, "weights and deltas")
  .tab_expect(sum(vapply(cand, function(d) get(paste0("candidate|", d, "||chosen")), numeric(1))) == 1, "one chosen")
  # the chart: Kaplan–Meier's steps and one curve per admissible distribution, from 1 at time 0
  starts <- r$key[grepl("^curve\\|candidates\\|[a-z]+\\|surv\\|0$", r$key)]
  .tab_expect(setequal(starts, paste0("curve|candidates|", c("km", cand), "|surv|0")), "the curves: ", paste(starts, collapse = ", "))
  for (k in starts) .tab_expect(r$value[match(k, r$key)] == 1 && r$x0[match(k, r$key)] == 0, k, " does not start at (0, 1)")
  last <- paste0("curve|candidates|weibull|surv|", PS_AUTO_CURVE_TIMES - 1L)
  .tab_expect(r$value[match(last, r$key)] > 0 && r$value[match(last, r$key)] < 1 && abs(r$x0[match(last, r$key)] - max(df$days)) < 1e-9, "the last point")
}

TAB_COMMON_TESTS[["parametric survival, auto: the time rule, and no candidate left"]] <- function(code) {
  df <- .ps_test_table(code)
  # a clock that gives 0, 1, 2… seconds a call: with a budget of 6 the fifth call (4 s) is past
  # half of it (3 s), so Gompertz and the generalized gamma are not fitted
  calls <- 0
  task <- .ps_test_task(df, .psa_test_params())
  task$limits$time_budget_seconds <- 6
  task$.selftest_clock <- function() {
    calls <<- calls + 1
    calls - 1
  }
  r <- tab_results_frame(fit_parametric_survival(task)$rows)
  status <- vapply(PS_AUTO_ORDER, function(d) r$text[match(paste0("candidate|", d, "||status"), r$key)], "")
  .tab_expect(identical(unname(status), c("ok", "ok", "ok", "ok", "time", "time")), "the statuses: ", paste(status, collapse = " "))
  .tab_expect(identical(r$text[match("model|||note_auto_time", r$key)],
                        "@auto_time names=gompertz,gengamma"),
              "the note: ", r$text[match("model|||note_auto_time", r$key)])
  .tab_expect(is.na(r$value[match("candidate|gengamma||akaike_weight", r$key)]) && is.na(r$value[match("candidate|gengamma||aic", r$key)]) &&
              r$value[match("candidate|gengamma||converged", r$key)] == 0, "a candidate not fitted has no numbers")
  .tab_expect(abs(sum(vapply(c("exponential", "weibull", "lognormal", "loglogistic"),
                             function(d) r$value[match(paste0("candidate|", d, "||akaike_weight"), r$key)], numeric(1))) - 1) < 1e-12,
              "the weights are over the four fitted")
  # no budget at all: nothing is late; a budget with the time already gone: no candidate
  task$limits$time_budget_seconds <- NULL
  task$.selftest_clock <- function() 1e9
  r <- tab_results_frame(fit_parametric_survival(task)$rows)
  .tab_expect(!"model|||note_auto_time" %in% r$key, "no budget, no note")
  task$limits$time_budget_seconds <- 1
  msg <- .tab_refused_with("no_candidate", fit_parametric_survival(task))
  .tab_expect(grepl("^no distribution could be fitted to these rows \\(exponential: no time left;", msg), "the sentence: ", msg)
}

TAB_COMMON_TESTS[["parametric survival, auto: a candidate whose Hessian is not negative definite is not converged, and the next is chosen"]] <- function(code) {
  # events from day 183 on, with a spike at 183, and units censored from day 1: the generalized gamma
  # runs off to a degenerate point (Q about −30, no standard error) with the lowest AIC by far
  set.seed(2)
  ev <- 183 + round(stats::rexp(54, 1 / 150))
  ev[1:4] <- 183
  ce <- round(stats::runif(96, 0, 1100))
  ce[ce == 0] <- 1
  df <- data.frame(days = c(ev, ce), status = c(rep(1, 54), rep(0, 96)))
  fit <- fit_parametric_survival(.ps_test_task(df, list(time = "days", event = "status", event_value = 1, distribution = "auto")))
  r <- tab_results_frame(fit$rows)
  get <- function(key) r$value[match(key, r$key)]
  said <- function(key) r$text[match(key, r$key)]
  chosen <- said("model|||distribution")
  .tab_expect(!identical(chosen, "gengamma") && identical(get("candidate|gengamma||converged"), 0) &&
              identical(get("candidate|gengamma||admissible"), 0) && identical(said("candidate|gengamma||status"), "not_converged"),
              "the degenerate generalized gamma: chosen ", chosen, ", converged ", get("candidate|gengamma||converged"))
  .tab_expect(is.na(get("candidate|gengamma||akaike_weight")) && grepl("gengamma", said("model|||note_auto_not_converged")),
              "out of the weights, and in the note")
  .tab_expect(identical(get(paste0("candidate|", chosen, "||converged")), 1) && identical(said("assumption|convergence||verdict"), "ok"),
              "the chosen one is checked")
  # the candidates the choice never reached have no row `converged`: not 1, not 0
  unchecked <- setdiff(PS_AUTO_ORDER, c(chosen, "gengamma"))
  has <- vapply(unchecked, function(d) paste0("candidate|", d, "||converged") %in% r$key, logical(1))
  .tab_expect(any(!has), "every candidate was checked")
  for (d in unchecked[!has]) .tab_expect(identical(said(paste0("candidate|", d, "||status")), "ok"), d, ": not checked but not fitted")
  .tab_expect(!length(tab_check_declared(r, "parametric_survival", "auto")), "the rows are not what the variant declares")
}
