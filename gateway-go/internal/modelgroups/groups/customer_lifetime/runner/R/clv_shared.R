# What the two nodes of the group share (docs/specs/customer-lifetime-models.md, «The group
# customer_lifetime — R»): the summary's checks, the optimizer, the standard errors, the five models'
# functions, Gamma–Gamma, the discounting and the CLV, the holdout, the chi-square, the P(alive)
# grid, the charts' points, the card and the rows. Lot B0 wrote the names of the CLVTools functions the
# runner binds; lot B1 writes the rest. The nodes (clv.R, bg_bb.R) are thin: they read the summary,
# fit the candidates, choose, and hand the fit to clv_results().
#
# CLVTools' fitting functions take a transaction log, and the nodes take a summary, one row per
# customer. The summary is all the data the likelihoods ever see, so the runner calls CLVTools' own
# compiled likelihoods on it, with CLVTools' own optimizer settings. Those functions are documented in
# CLVTools' manual and are not exported: the runner reaches them by name in the version the recipe
# pins (0.12.1), and the self-test compares the estimates they give with the public pnbd(), bgnbd() and
# gg() on a transaction log rebuilt from a whole-day case: a later version that renames or changes one
# of them fails the build's check instead of the numbers.
#
# The files of runner/R are sourced in alphabetical order, and clv.R and bg_bb.R come before this one:
# they must not call anything of this file while they are sourced, only when they run.
#
# The vocabulary. A «summary» is the list clv_read_summary() returns: one entry per customer kept,
# `x` (repeat purchases), `tx` (recency: the time of the last one; BG/BB: its opportunity), `T` (the
# age; BG/BB: the opportunities n), `m` (the average spend of the repeat purchases, NA when none),
# `h` (the holdout's purchases, NA when none) and `row` (the customer's row in the input). A
# «model» is one of pareto_nbd, bg_nbd, mbg_nbd, bg_bb (the purchases) or gamma_gamma (the spend);
# its parameters are positive, kept in the order of CLV_MODELS[[model]]$params and estimated on the
# log scale.

CLV_CLVTOOLS_FUNCTIONS <- c(
  # Pareto/NBD
  "pnbd_nocov_LL_ind", "pnbd_nocov_LL_sum", "pnbd_nocov_PAlive", "pnbd_nocov_CET", "pnbd_nocov_DERT",
  "pnbd_nocov_PMF", "pnbd_nocov_expectation",
  # BG/NBD (no DERT: CLVTools does not derive that expression)
  "bgnbd_nocov_LL_ind", "bgnbd_nocov_LL_sum", "bgnbd_nocov_PAlive", "bgnbd_nocov_CET", "bgnbd_nocov_PMF",
  "bgnbd_nocov_expectation",
  # Gamma–Gamma, and GSL's 2F1 with its status
  "gg_LL", "vec_gsl_hyp2f1_e"
)

# One of CLVTools' functions by name; a fault of the image, said as one, when it is not there.
clv_clvtools <- function(name) {
  found <- tryCatch(utils::getFromNamespace(name, "CLVTools"), error = function(e) NULL)
  if (!is.function(found)) {
    tab_refuse("clvtools_missing", paste0("the image's CLVTools lacks ", name, ": rebuild the group"), list(function_name = name))
  }
  found
}

# The names among CLV_CLVTOOLS_FUNCTIONS that this image's CLVTools does not have.
clv_missing_clvtools <- function() {
  CLV_CLVTOOLS_FUNCTIONS[!vapply(CLV_CLVTOOLS_FUNCTIONS, function(name) {
    is.function(tryCatch(utils::getFromNamespace(name, "CLVTools"), error = function(e) NULL))
  }, logical(1))]
}

# The function of CLVTools, bound at the first use (not when the file is sourced).
clv_fn <- local({
  bound <- list()
  function(name) {
    if (is.null(bound[[name]])) bound[[name]] <<- clv_clvtools(name)
    bound[[name]]
  }
})

# ── the units ────────────────────────────────────────────────────────────────────
CLV_UNITS <- c("day", "week", "month", "quarter", "year")
CLV_PERIODS_PER_YEAR <- c(day = 365.25, week = 365.25 / 7, month = 12, quarter = 4, year = 1)
CLV_UNITS_IN_A_YEAR <- c(day = 365, week = 52, month = 12, quarter = 4, year = 1)

clv_unit <- function(unit) {
  if (!is.character(unit) || length(unit) != 1L || !unit %in% CLV_UNITS) {
    tab_refuse("bad_unit", paste0("the time unit is one of ", paste(CLV_UNITS, collapse = ", "), ", not «", paste(unit, collapse = ""), "»"))
  }
  unit
}

# Periods in a year: how the annual rate becomes a rate per period.
clv_periods_per_year <- function(unit) unname(CLV_PERIODS_PER_YEAR[[clv_unit(unit)]])

# One year in `unit`: the horizon when the node gives none.
clv_default_horizon <- function(unit) unname(CLV_UNITS_IN_A_YEAR[[clv_unit(unit)]])

# The rate of one period from the annual rate: (1 + D)^(1/k) − 1.
clv_period_rate <- function(annual, unit) (1 + annual)^(1 / clv_periods_per_year(unit)) - 1

# ── the models' table ────────────────────────────────────────────────────────────
# `start`: the start values (CLVTools' for Pareto/NBD, BG/NBD and Gamma–Gamma, BTYDplus' for MBG/NBD,
# ones for BG/BB); `method`: "default" is optimx's own list (Nelder–Mead, then BFGS), whose last row
# CLVTools keeps for BG/NBD. `tight`: the model is written here (MBG/NBD, BG/BB), so its L-BFGS-B runs to
# the tolerances of CLV_TIGHT instead of optim's defaults (CLVTools' models keep CLVTools' own settings, so
# that their estimates are CLVTools').
CLV_MODELS <- list(
  pareto_nbd = list(params = c("r", "alpha", "s", "beta"), start = c(0.5, 15, 0.5, 10), method = "L-BFGS-B", tight = FALSE),
  bg_nbd = list(params = c("r", "alpha", "a", "b"), start = c(1, 3, 1, 3), method = "default", tight = FALSE),
  mbg_nbd = list(params = c("r", "alpha", "a", "b"), start = c(1, 3, 1, 3), method = "L-BFGS-B", tight = TRUE),
  bg_bb = list(params = c("alpha", "beta", "gamma", "delta"), start = c(1, 1, 1, 1), method = "L-BFGS-B", tight = TRUE),
  gamma_gamma = list(params = c("p", "q", "gamma"), start = c(1, 1, 1), method = "L-BFGS-B", tight = FALSE)
)

# L-BFGS-B's stopping rules for the models written here: a relative change of the likelihood below
# factr × the machine's epsilon (optim's default factr is 1e7, about 2e-9), and a projected gradient below
# pgtol (optim's default 0 switches that test off). A few more iterations of a cheap likelihood, and the
# estimate is the maximum to many more digits (the reference cases' tolerances, «As built, B10»).
CLV_TIGHT <- list(factr = 10, pgtol = 1e-10)

# The rounding of a sum of logs, relative: how far two computations of the same log-likelihood may differ
# because of floating point alone. A numerical tolerance of the guard (clv_sound), never a verdict.
CLV_ROUNDING <- sqrt(.Machine$double.eps)

clv_num <- function(v) {
  s <- format(round(v, 6), trim = TRUE, scientific = FALSE)
  sub("^-", "−", s)
}

clv_label <- function(x) if (inherits(x, "integer64")) as.character(x) else tab_label(x)

# ── the summary ──────────────────────────────────────────────────────────────────
# A refusal that names one customer and the count of customers that break the same rule.
clv_refuse_rule <- function(rule, id) {
  bad <- which(rule$bad)
  i <- bad[[1L]]
  sentence <- rule$msg(i)
  if (length(bad) > 1L) sentence <- paste0(sentence, " (", length(bad), " customers in all break this rule)")
  tab_refuse(rule$code, sentence, list(customer = id[[i]], count = length(bad)))
}

# The rules a summary's rows must obey, in the order they are checked: each a code, which rows
# break it (`bad`, a logical per row) and the sentence for row i. `v` holds id, x, tx, T, m and h.
# Fit refuses at the first rule broken; Predict gives NULL predictions to the rows that break any.
clv_rules <- function(v, node, max_opportunities = 1000, holdout_length = NULL) {
  bgbb <- identical(node, "bg_bb")
  eps <- 1e-9
  whole <- function(z) is.na(z) | abs(z - round(z)) <= eps
  id <- v$id
  rules <- list()
  add <- function(code, bad, msg) rules[[length(rules) + 1L]] <<- list(code = code, bad = bad %in% TRUE, msg = msg)
  x <- v$x
  tx <- v$tx
  age <- v$T
  neg <- ifelse(x < 0, 1L, ifelse(tx < 0, 2L, ifelse(age < 0, 3L, 0L)))
  names_ <- if (bgbb) c("frequency", "recency", "opportunities") else c("frequency", "recency", "age")
  add("negative_value", neg > 0L, function(i) {
    paste0(if (bgbb) "frequency, recency and opportunities" else "recency, age and frequency", " are 0 or more: the customer ", id[[i]],
           " has a ", names_[[neg[[i]]]], " of ", clv_num(c(x[[i]], tx[[i]], age[[i]])[[neg[[i]]]]))
  })
  add("frequency_not_whole", !whole(x), function(i) {
    paste0(if (bgbb) "frequency must count transactions: the customer " else "frequency must count purchases: the customer ", id[[i]],
           " has ", clv_num(x[[i]]))
  })
  if (bgbb) {
    add("bgbb_opportunities", !whole(age) | age < 1 | age > max_opportunities, function(i) {
      paste0("opportunities run from 1 to ", format(max_opportunities, big.mark = ","), ": the customer ", id[[i]], " has ", clv_num(age[[i]]))
    })
    add("bgbb_recency", !whole(tx), function(i) {
      paste0("recency is the opportunity of the last transaction, a whole number: the customer ", id[[i]], " has ", clv_num(tx[[i]]))
    })
    add("recency_after_age", tx > age, function(i) {
      paste0("the customer ", id[[i]], " bought last at the opportunity ", clv_num(tx[[i]]), ", after the last of its ", clv_num(age[[i]]),
             " opportunities: recency is at most opportunities")
    })
    add("bgbb_frequency", round(x) > round(tx), function(i) {
      paste0("the customer ", id[[i]], " has ", clv_num(x[[i]]), " transactions in ", clv_num(tx[[i]]),
             " opportunities up to its last: at most one transaction per opportunity")
    })
    add("recency_without_purchase", round(x) == 0 & tx > 0, function(i) {
      paste0("the customer ", id[[i]], " has no repeat transaction but a recency of ", clv_num(tx[[i]]), ": recency is 0 without repeat transactions")
    })
  } else {
    add("age_zero", age == 0, function(i) {
      paste0("the customer ", id[[i]], " has an age of 0: age is the time since the first purchase, more than 0")
    })
    add("recency_after_age", tx > age * (1 + eps), function(i) {
      paste0("the customer ", id[[i]], " bought last (recency ", clv_num(tx[[i]]), ") after the end of the period (age ", clv_num(age[[i]]),
             "): recency is at most age")
    })
    add("recency_without_purchase", round(x) == 0 & tx > 0, function(i) {
      paste0("the customer ", id[[i]], " has no repeat purchase but a recency of ", clv_num(tx[[i]]), ": recency is 0 without repeat purchases")
    })
    add("same_moment", round(x) > 0 & tx == 0, function(i) {
      paste0("the customer ", id[[i]], " has ", clv_num(x[[i]]), " repeat purchases at recency 0: purchases of the same day must be one purchase; ",
             "group by customer and day first")
    })
  }
  m <- v$m
  add("spend_not_positive", round(x) > 0 & !is.na(m) & m <= 0, function(i) {
    paste0("the customer ", id[[i]], " has an average spend of ", clv_num(m[[i]]), " with repeat purchases: the spend model needs amounts above 0; ",
           "leave out returns and refunds, or those customers")
  })
  h <- v$h
  add("holdout_negative", !is.na(h) & (h < 0 | !whole(h)), function(i) {
    paste0("holdout purchases are whole numbers, 0 or more: the customer ", id[[i]], " has ", clv_num(h[[i]]))
  })
  if (bgbb && !is.null(holdout_length)) {
    add("bgbb_holdout", !is.na(h) & h > holdout_length, function(i) {
      paste0("a customer has at most one transaction per opportunity: the customer ", id[[i]], " has ", clv_num(h[[i]]),
             " in the ", clv_num(holdout_length), " opportunities of the holdout")
    })
  }
  rules
}

# The rows of a frame as the model reads them: the numbers of the roles, NA where a number is NULL.
clv_numbers <- function(df, name) {
  x <- df[[name]]
  if (!(is.numeric(x) || inherits(x, "integer64"))) {
    tab_refuse("not_a_number", paste0("the column «", name, "» is not a number"), list(column = name))
  }
  as.numeric(x)
}

# The summary of a node's input: one row per customer, checked rule by rule. `frame` is the
# self-test's table; else the step's input is read.
clv_read_summary <- function(task, node, frame = NULL) {
  p <- task$params
  bgbb <- identical(node, "bg_bb")
  roles <- list(customer = p$customer, frequency = p$frequency, recency = p$recency,
                age = if (bgbb) p$opportunities else p$age, spend = p$spend, holdout = p$holdout_transactions)
  roles <- roles[vapply(roles, function(v) is.character(v) && length(v) == 1L && !is.na(v) && nzchar(v), logical(1))]
  unit <- clv_unit(if (bgbb) p$period_unit else p$time_unit)
  df <- if (!is.null(frame)) frame else tab_input(task, columns = unique(unlist(roles)))
  for (role in names(roles)) {
    if (!roles[[role]] %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows have no column «", roles[[role]], "»"), list(column = roles[[role]]))
    }
  }
  n_input <- nrow(df)
  num <- function(role) if (is.null(roles[[role]])) rep(NA_real_, n_input) else clv_numbers(df, roles[[role]])
  id_raw <- df[[roles$customer]]
  x <- num("frequency")
  tx <- num("recency")
  age <- num("age")
  m <- num("spend")
  h <- num("holdout")
  for (role in intersect(c("spend", "holdout"), names(roles))) {
    z <- if (role == "spend") m else h
    if (any(is.infinite(z))) {
      tab_refuse("infinite", paste0("the column «", roles[[role]], "» holds an infinite value: filter it out or replace it before the node"),
                 list(column = roles[[role]]))
    }
  }
  base <- data.frame(x = x, tx = tx, age = age)
  names(base) <- c(roles$frequency, roles$recency, roles$age)
  complete <- tab_complete_rows(base, names(base))
  keep <- complete$keep & !is.na(id_raw)
  v <- list(id = clv_label(id_raw)[keep], x = x[keep], tx = tx[keep], T = age[keep], m = m[keep], h = h[keep], row = which(keep))
  dup <- duplicated(id_raw[keep])
  if (any(dup)) {
    i <- which(dup)[[1L]]
    clv_refuse_rule(list(code = "duplicate_customer", bad = dup, msg = function(i) {
      paste0("the customer ", v$id[[i]], " appears on ", sum(v$id == v$id[[i]]), " rows: one row per customer; group the transactions by customer first")
    }), v$id)
  }
  hold_len <- if (is.null(roles$holdout)) NULL else p$holdout_length
  for (rule in clv_rules(v, node, tab_limit(task, "max_opportunities"), hold_len)) if (any(rule$bad)) clv_refuse_rule(rule, v$id)
  v$x <- round(v$x)
  if (bgbb) {
    v$tx <- round(v$tx)
    v$T <- round(v$T)
  }
  notes <- list()
  clamped <- !bgbb & v$tx > v$T
  if (any(clamped)) {
    v$tx[clamped] <- v$T[clamped]
    notes$t_x_clamped <- paste0("@t_x_clamped n=", sum(clamped))
  }
  v$h <- ifelse(is.na(v$h), NA_real_, round(v$h))
  v$m[v$x == 0] <- NA_real_
  n_spend_missing <- sum(v$x > 0 & is.na(v$m))
  if (!is.null(roles$spend) && n_spend_missing > 0L) {
    notes$spend_missing <- paste0("@spend_missing n=", n_spend_missing)
  }
  if (!bgbb && max(v$T, 0) > 30 * clv_periods_per_year(unit)) {
    notes$unit_check <- paste0("@unit_check age=", format(max(v$T), digits = 15), " unit=", unit)
  }
  v$n <- length(v$x)
  v$n_input <- n_input
  v$n_missing <- n_input - v$n
  v$node <- node
  v$unit <- unit
  v$has_spend <- !is.null(roles$spend)
  v$has_holdout <- !is.null(roles$holdout)
  v$holdout_length <- if (v$has_holdout) as.numeric(hold_len) else NA_real_
  v$notes <- notes
  v$columns <- roles
  clv_check_counts(task, v)
  v
}

# The refusals about how many customers there are.
clv_check_counts <- function(task, s) {
  most <- tab_limit(task, "min_customers")
  if (s$n < most) tab_refuse("few_customers", paste0(s$n, if (s$n == 1L) " customer: " else " customers: ", "these models need at least ", most), list(n = s$n))
  repeaters <- sum(s$x > 0)
  if (repeaters == 0L) {
    tab_refuse("no_repeaters", paste0("no customer bought again: the models cannot tell the purchase rate from the dropout without repeat ",
                                       if (identical(s$node, "bg_bb")) "transactions" else "purchases"))
  }
  least <- tab_limit(task, "min_repeaters")
  if (repeaters < least) {
    tab_refuse("few_repeaters", paste0(repeaters, if (repeaters == 1L) " customer bought again: " else " customers bought again: ",
                                        "these models need at least ", least), list(n = repeaters))
  }
  spenders <- sum(s$x > 0 & !is.na(s$m))
  if (s$has_spend && spenders < least) {
    tab_refuse("few_spenders", paste0(spenders, if (spenders == 1L) " repeat customer has a spend: " else " repeat customers have a spend: ",
                                       "the spend model needs at least ", least), list(n = spenders))
  }
  invisible(TRUE)
}

# The distinct (x, t_x, T) with the number of customers of each, and for each customer its group
# (`index`): the likelihoods and the expectations depend on these three only.
clv_compress <- function(x, tx, age) {
  o <- order(age, tx, x)
  xs <- x[o]
  ts <- tx[o]
  as_ <- age[o]
  first <- c(TRUE, xs[-1L] != xs[-length(xs)] | ts[-1L] != ts[-length(ts)] | as_[-1L] != as_[-length(as_)])
  group <- cumsum(first)
  index <- integer(length(x))
  index[o] <- group
  list(x = xs[first], tx = ts[first], T = as_[first], n = tabulate(group), index = index)
}

# ── the optimizer ────────────────────────────────────────────────────────────────
clv_optimx <- function(fn, start, names, method, itnmax = 3000, kkt = TRUE, tight = FALSE) {
  args <- list(par = stats::setNames(start, names), fn = fn)
  if (identical(method, "default")) {
    # CLVTools' BG/NBD: optimx's own list, no other setting (the KKT tests, which compute a Hessian, only when asked)
    if (!kkt) args$control <- list(kkt = FALSE)
  } else {
    args$method <- method
    args$itnmax <- itnmax
    args$control <- list(kkt = kkt, save.failures = TRUE, starttests = FALSE)
    if (tight && identical(method, "L-BFGS-B")) args$control <- c(args$control, CLV_TIGHT)
  }
  tryCatch(suppressWarnings(do.call(optimx::optimx, args)), error = function(e) NULL)
}

# The estimate of a model, on the log scale, from `fn(log_par)`: the negative log-likelihood (a sum
# over the customers). The settings are CLVTools' own (the model's `method`); when they fail (a trial point
# overflows: «L-BFGS-B needs finite values of 'fn'»), do not converge, or stop where the likelihood is
# numerical noise (`sound(lp, value)` is FALSE: clv_sound), the fallback works on `stable` (the same
# likelihood computed in a form that does not lose its digits; `fn` when none is given): L-BFGS-B polishes
# from the best sound row the first run left (BG/NBD's optimx list leaves Nelder–Mead's row before BFGS's,
# and that row is where BFGS jumped from into the noise), or, when there is none, Nelder–Mead restarts from
# the start and L-BFGS-B polishes its end point (`fallback`). An end point that is not sound is never kept
# over one that is; when none is, the fit is `fault`: not converged, and `auto` never chooses it.
clv_optimize <- function(fn, model, hessian = TRUE, sound = function(lp, value) TRUE, stable = fn) {
  spec <- CLV_MODELS[[model]]
  names_ <- paste0("log.", spec$params)
  start <- log(spec$start)
  take <- function(res, f, i = if (is.null(res)) 0L else nrow(res)) {
    if (is.null(res) || !nrow(res)) return(NULL)
    lp <- unname(unlist(res[i, names_]))
    value <- res$value[[i]]
    if (!all(is.finite(lp)) || !is.finite(value)) return(NULL)
    det <- attr(res, "details")
    h <- tryCatch(det[i, "nhatend"][[1L]], error = function(e) NULL)
    list(lp = lp, value = value, convcode = res$convcode[[i]], kkt1 = isTRUE(res$kkt1[[i]]), kkt2 = isTRUE(res$kkt2[[i]]),
         hessian = if (is.matrix(h) && all(dim(h) == length(lp)) && all(is.finite(h))) h else NULL,
         rows = rownames(res), sound = isTRUE(sound(lp, value)), fn = f)
  }
  first <- clv_optimx(fn, start, names_, spec$method, kkt = hessian, tight = spec$tight)
  fit <- take(first, fn)
  fallback <- FALSE
  if (is.null(fit) || fit$convcode != 0 || !fit$sound) {
    fallback <- TRUE
    capped <- function(lp) {
      v <- stable(lp)
      if (is.finite(v)) v else 1e300
    }
    earlier <- if (is.null(first) || nrow(first) < 2L) list() else
      Filter(function(b) !is.null(b) && b$sound && b$convcode == 0, lapply(seq_len(nrow(first) - 1L), function(i) take(first, fn, i)))
    nm <- if (length(earlier)) earlier[[which.min(vapply(earlier, function(b) b$value, 0))]] else
      take(clv_optimx(capped, start, names_, "Nelder-Mead", itnmax = 10000, kkt = FALSE), stable)
    polished <- if (is.null(nm)) NULL else take(clv_optimx(stable, nm$lp, names_, "L-BFGS-B", kkt = hessian, tight = TRUE), stable)
    best <- Filter(Negate(is.null), list(polished, nm, fit))
    if (!length(best)) {
      tab_refuse("optimizer_failed", paste0("the ", clv_model_label(model), " estimate could not be found: the data may hold too few repeat customers for this model; try another purchase model"),
                 list(model = model))
    }
    good <- vapply(best, function(b) b$sound, logical(1))
    if (any(good)) best <- best[good]
    fit <- best[[which.min(vapply(best, function(b) b$value, 0))]]
    fit$kkt1 <- fit$kkt2 <- NA
  }
  # the Hessian is taken on the function the estimate was found on (CLVTools' own, or the stable form after a fallback)
  if (is.null(fit$hessian) && hessian && fit$sound) fit$hessian <- tryCatch(numDeriv::hessian(fit$fn, fit$lp), error = function(e) NULL)
  fit$fault <- !fit$sound
  if (fit$fault) fit$convcode <- 9999L
  fit$fallback <- fallback
  fit$optimizer <- if (fallback) "Nelder-Mead, L-BFGS-B (fallback)" else if (identical(spec$method, "default")) "Nelder-Mead, BFGS" else spec$method
  fit
}

# Is the log-likelihood at an end point what the model says, or numerical noise? Two facts, checked at the
# end point only (one more evaluation of the likelihood; docs/specs/customer-lifetime-models.md, «As built,
# B10»): (1) a customer without a repeat purchase (x = 0) has a probability, not a density, so its
# log-likelihood is at most 0 (for BG/BB every customer's is a probability); above 0 by more than rounding is
# a broken computation; (2) the optimizer's value (−`value`) equals the sum of the customers' log-likelihoods
# computed in the stable form (clv_ll_stable), up to rounding. CLVTools' BG/NBD sums r·log α − (r + x)·log(α + T)
# as two terms: where r and α run to 1e35 and 1e67 the two cancel to noise of the order of 1e24, which the
# optimizer took for a maximum; the stable sum there is −992,183, not +4.9e24.
clv_sound <- function(model, comp, lp, value) {
  if (!all(is.finite(lp)) || !is.finite(value)) return(FALSE)
  ind <- clv_ll_stable(model, exp(lp), comp$x, comp$tx, comp$T)
  if (anyNA(ind) || any(ind == Inf)) return(FALSE)
  zero <- if (identical(model, "bg_bb")) rep(TRUE, length(ind)) else comp$x == 0
  if (any(ind[zero] > CLV_ROUNDING)) return(FALSE)
  total <- sum(comp$n * ind)
  is.finite(total) && abs(-value - total) <= CLV_ROUNDING * (1 + abs(total))
}

clv_model_label <- function(model) {
  c(pareto_nbd = "Pareto/NBD", bg_nbd = "BG/NBD", mbg_nbd = "MBG/NBD", bg_bb = "BG/BB", gamma_gamma = "Gamma-Gamma")[[model]]
}

# The covariance of the log parameters (NULL when the Hessian is not positive definite), the
# standard errors on the original scale (delta method, as CLVTools' vcov()) and the intervals at
# 1 − alpha on the log scale, back-transformed so that they stay positive.
clv_inference <- function(fit, alpha = 0.05) {
  par <- exp(fit$lp)
  v <- NULL
  if (!is.null(fit$hessian)) {
    v <- tryCatch({
      chol(fit$hessian)
      solve(fit$hessian)
    }, error = function(e) NULL)
    if (!is.null(v) && !all(is.finite(v))) v <- NULL
  }
  out <- list(vcov_log = v, se = rep(NA_real_, length(par)), ci_low = rep(NA_real_, length(par)), ci_high = rep(NA_real_, length(par)))
  if (!is.null(v)) {
    se_log <- sqrt(pmax(diag(v), 0))
    z <- stats::qnorm(1 - alpha / 2)
    jac <- diag(par, length(par))
    out$se <- sqrt(pmax(diag(jac %*% v %*% jac), 0))
    out$ci_low <- exp(fit$lp - z * se_log)
    out$ci_high <- exp(fit$lp + z * se_log)
  }
  out
}

# ── the purchase models' functions ───────────────────────────────────────────────
# par: the positive parameters in CLV_MODELS' order. All of them vectorized over the customers.

# The log of the Beta function's ratio and the like are R's lbeta/lgamma.
clv_logsumexp2 <- function(a, b) {
  m <- pmax(a, b)
  ifelse(is.finite(m), m + log(exp(a - m) + exp(b - m)), m)
}

# log Γ(r + x) − log Γ(r) without the cancellation of two large numbers: Γ(r + x)/Γ(r) = Γ(x)/B(r, x) for x > 0,
# and R's lbeta keeps its digits for large arguments (it subtracts the Stirling corrections, not the lgammas).
# At r = 1e35 the two lgammas are 8e36, whose last digit is 1e21: their difference is noise, lbeta's is not.
clv_lgamma_ratio <- function(r, x) {
  out <- numeric(length(x))
  pos <- x > 0
  out[pos] <- lgamma(x[pos]) - lbeta(r, x[pos])
  out
}

# The part of the NBD's likelihood that holds r and α: r·log(α/(α + t)) − x·log(α + t), with the first term as
# −r·log1p(t/α), which stays exact where α is far larger than t (r·log α − (r + x)·log(α + t) as two terms
# loses every digit there).
clv_nbd_part <- function(r, alpha, x, t) -r * log1p(t / alpha) - x * log(alpha + t)

# BG/NBD (Fader, Hardie, Lee 2005), the per-customer log-likelihood in the stable form: what CLVTools' compiled
# function computes, kept to its digits where r and α are huge. The fit uses CLVTools' own (its settings, its
# estimates); this one is the guard's recomputation (clv_sound) and the fallback's likelihood.
clv_bgnbd_ll_ind <- function(par, x, tx, T) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  g <- clv_lgamma_ratio(r, x)
  t1 <- g + clv_nbd_part(r, alpha, x, T) + lbeta(a, b + x) - lbeta(a, b)
  t2 <- rep(-Inf, length(x))
  pos <- x > 0
  t2[pos] <- g[pos] + clv_nbd_part(r, alpha, x[pos], tx[pos]) + lbeta(a + 1, b + x[pos] - 1) - lbeta(a, b)
  clv_logsumexp2(t1, t2)
}

# MBG/NBD (Batislam, Denizel, Filiztekin 2007): the BG/NBD in which a customer may also leave right after
# the first purchase. The stable form, as clv_bgnbd_ll_ind.
clv_mbg_ll_ind <- function(par, x, tx, T) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  l0 <- clv_lgamma_ratio(r, x)
  t1 <- lbeta(a, b + x + 1) - lbeta(a, b) + clv_nbd_part(r, alpha, x, T)
  t2 <- lbeta(a + 1, b + x) - lbeta(a, b) + clv_nbd_part(r, alpha, x, tx)
  l0 + clv_logsumexp2(t1, t2)
}

# log((α + T)/(α + t_x)) as log1p((T − t_x)/(α + t_x)): exact where α is far larger than T.
clv_mbg_palive <- function(par, x, tx, T) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  1 / (1 + exp(log(a) - log(b + x) + (r + x) * log1p((T - tx) / (alpha + tx))))
}

# P(X(t) = k) for MBG/NBD: t is a vector (the customers' ages), k one count.
clv_mbg_pmf <- function(par, k, t) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  nb <- function(j) stats::dnbinom(j, size = r, prob = alpha / (alpha + t))
  below <- if (k == 0) 0 else stats::pnbinom(k - 1, size = r, prob = alpha / (alpha + t))
  exp(lbeta(a, b + k + 1) - lbeta(a, b)) * nb(k) + exp(lbeta(a + 1, b + k) - lbeta(a, b)) * (1 - below)
}

# BG/BB (Fader, Hardie, Shang 2010): x transactions in n opportunities, the last at opportunity tx.
clv_bgbb_ll_ind <- function(par, x, tx, n) {
  al <- par[[1L]]
  be <- par[[2L]]
  ga <- par[[3L]]
  de <- par[[4L]]
  base <- lbeta(al + x, be + n - x) - lbeta(al, be) + lbeta(ga, de + n) - lbeta(ga, de)
  out <- base
  for (j in which(n > tx)) {
    i <- 0:(n[[j]] - tx[[j]] - 1)
    t2 <- lbeta(al + x[[j]], be + tx[[j]] - x[[j]] + i) - lbeta(al, be) + lbeta(ga + 1, de + tx[[j]] + i) - lbeta(ga, de)
    z <- c(base[[j]], t2)
    mx <- max(z)
    out[[j]] <- mx + log(sum(exp(z - mx)))
  }
  out
}

# P(alive at the opportunity n + 1) for BG/BB.
clv_bgbb_palive <- function(par, x, tx, n) {
  al <- par[[1L]]
  be <- par[[2L]]
  ga <- par[[3L]]
  de <- par[[4L]]
  exp(lbeta(al + x, be + n - x) - lbeta(al, be) + lbeta(ga, de + n + 1) - lbeta(ga, de) - clv_bgbb_ll_ind(par, x, tx, n))
}

# Σ_{i=1}^{n*} B(γ, δ + n + i) / B(γ, δ) for each n of a vector (BG/BB's dropout part of the expected
# transactions; n* whole). Each ratio B(γ, δ + m)/B(γ, δ) = Π_{j<m} (δ + j)/(γ + δ + j) is a cumulative sum of
# log1p(−γ/(γ + δ + j)), exact for any γ and δ; the window of each n is read off one sum taken from the top, so
# the part subtracted is the terms after the window, never larger than the window's own (the terms fall with
# m). The closed form, a difference of two ratios of gamma functions, cancels to nothing when δ is large (δ
# above 1e7 lost every digit) and gives Inf · 0 when γ is large too: customers who never drop out push the fit
# there.
clv_bgbb_tail <- function(ga, de, n, nstar) {
  if (nstar <= 0) return(numeric(length(n)))
  top <- max(n) + nstar
  terms <- exp(cumsum(log1p(-ga / (ga + de + 0:(top - 1)))))
  from_top <- c(rev(cumsum(rev(terms))), 0)
  from_top[n + 1] - from_top[n + nstar + 1]
}

# The expected transactions in the next `nstar` opportunities (BG/BB): E[p^(x+1) (1−p)^(n−x)] over the
# likelihood, times the dropout's sum (clv_bgbb_tail).
clv_bgbb_cet <- function(par, nstar, x, tx, n) {
  al <- par[[1L]]
  be <- par[[2L]]
  front <- exp(lbeta(al + x + 1, be + n - x) - lbeta(al, be) - clv_bgbb_ll_ind(par, x, tx, n))
  front * clv_bgbb_tail(par[[3L]], par[[4L]], n, round(nstar))
}

# P(X(n) = k) for BG/BB; n a vector of whole opportunities, k one count. The sum over the moment of dropout
# i = k … n − 1 is the same for every n up to its end, so it is one cumulative sum up to the largest n,
# read at each n (the distinct n of a summary, up to max_opportunities, cost one pass).
clv_bgbb_pmf <- function(par, k, n) {
  al <- par[[1L]]
  be <- par[[2L]]
  ga <- par[[3L]]
  de <- par[[4L]]
  out <- numeric(length(n))
  top <- max(n, 0)
  cum <- if (top - 1 >= k) {
    i <- k:(top - 1)
    cumsum(exp(lchoose(i, k) + lbeta(al + k, be + i - k) - lbeta(al, be) + lbeta(ga + 1, de + i) - lbeta(ga, de)))
  } else numeric()
  use <- n >= k
  m <- n[use]
  alive <- exp(lchoose(m, k) + lbeta(al + k, be + m - k) - lbeta(al, be) + lbeta(ga, de + m) - lbeta(ga, de))
  left <- numeric(length(m))
  left[m - 1 >= k] <- cum[m[m - 1 >= k] - k]
  out[use] <- alive + left
  out
}

# Per-customer log-likelihood on natural parameters.
clv_ll_ind <- function(model, par, x, tx, T) {
  switch(model,
    pareto_nbd = clv_fn("pnbd_nocov_LL_ind")(log(par), x, tx, T),
    bg_nbd = clv_fn("bgnbd_nocov_LL_ind")(log(par), x, tx, T),
    mbg_nbd = clv_mbg_ll_ind(par, x, tx, T),
    bg_bb = clv_bgbb_ll_ind(par, x, tx, T),
    stop("no likelihood for ", model))
}

# The per-customer log-likelihood the guard recomputes (clv_sound): BG/NBD in the stable form written here, the
# others as the fit computes them (MBG/NBD and BG/BB are written here in that form; Pareto/NBD is CLVTools' own,
# so for it the guard checks that CLVTools' sum is the sum of CLVTools' customers).
clv_ll_stable <- function(model, par, x, tx, T) {
  if (identical(model, "bg_nbd")) clv_bgnbd_ll_ind(par, x, tx, T) else clv_ll_ind(model, par, x, tx, T)
}

# The negative log-likelihood of a model on the compressed summary, as a function of the log parameters.
# `stable`: BG/NBD in the stable form written here instead of CLVTools' compiled sum (the fallback's, the edges').
clv_nll <- function(model, comp, stable = FALSE) {
  if (stable && identical(model, "bg_nbd")) {
    return(function(lp) -sum(comp$n * clv_bgnbd_ll_ind(exp(unname(lp)), comp$x, comp$tx, comp$T)))
  }
  switch(model,
    pareto_nbd = {
      f <- clv_fn("pnbd_nocov_LL_sum")
      function(lp) f(unname(lp), comp$x, comp$tx, comp$T, comp$n)
    },
    bg_nbd = {
      f <- clv_fn("bgnbd_nocov_LL_sum")
      function(lp) f(unname(lp), comp$x, comp$tx, comp$T, comp$n)
    },
    mbg_nbd = function(lp) -sum(comp$n * clv_mbg_ll_ind(exp(unname(lp)), comp$x, comp$tx, comp$T)),
    bg_bb = function(lp) -sum(comp$n * clv_bgbb_ll_ind(exp(unname(lp)), comp$x, comp$tx, comp$T)),
    stop("no likelihood for ", model))
}

clv_palive <- function(model, par, x, tx, T) {
  switch(model,
    pareto_nbd = clv_fn("pnbd_nocov_PAlive")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], x, tx, T),
    bg_nbd = clv_fn("bgnbd_nocov_PAlive")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], x, tx, T),
    mbg_nbd = clv_mbg_palive(par, x, tx, T),
    bg_bb = clv_bgbb_palive(par, x, tx, T),
    stop("no P(alive) for ", model))
}

# P(X(T) = k), the probability of k repeat purchases in the age T (vector), k one count. CLVTools' PMF gives
# NaN where the probability is below what its terms can hold (k of dozens in a short age): that is 0 here.
clv_pmf <- function(model, par, k, T) {
  p <- switch(model,
    pareto_nbd = clv_fn("pnbd_nocov_PMF")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], k, T),
    bg_nbd = clv_fn("bgnbd_nocov_PMF")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], k, T),
    mbg_nbd = clv_mbg_pmf(par, k, T),
    bg_bb = clv_bgbb_pmf(par, k, T),
    stop("no PMF for ", model))
  p[is.nan(p)] <- 0
  p
}

# ── the Gauss hypergeometric function and the quadrature ripiego ────────────────
# GSL's ₂F₁ through CLVTools' wrapper: value, with NA where GSL's status is not 0.
clv_hyp2f1 <- function(a, b, c, z) {
  g <- clv_fn("vec_gsl_hyp2f1_e")(a, b, c, z)
  v <- as.vector(g$value)
  v[as.vector(g$status) != 0L] <- NA_real_
  v
}

# The nodes and weights of Gauss–Jacobi quadrature of `n` points for E[f(p)], p ~ Beta(A, B)
# (Golub–Welsch on the Jacobi matrix, u = 2p − 1; the Beta weight is exact).
clv_gauss_beta <- local({
  cache <- new.env()
  function(A, B, n = 64L) {
    key <- paste(format(A, digits = 17), format(B, digits = 17), n)
    if (!is.null(cache[[key]])) return(cache[[key]])
    al <- B - 1
    be <- A - 1
    i <- 0:(n - 1)
    s <- 2 * i + al + be
    d <- ifelse(i == 0, (be - al) / (al + be + 2), (be^2 - al^2) / (ifelse(i == 0, 1, s) * (ifelse(i == 0, 1, s) + 2)))
    k <- 1:(n - 1)
    s1 <- 2 * k + al + be
    o <- sqrt(4 * k * (k + al) * (k + be) * (k + al + be) / (s1^2 * (s1 + 1) * (s1 - 1)))
    # k = 1: the factor (1 + α + β) is in the numerator and in (s1 − 1): cancelled
    o[[1L]] <- sqrt(4 * (1 + al) * (1 + be) / ((2 + al + be)^2 * (3 + al + be)))
    jac <- diag(d, n)
    jac[cbind(k, k + 1L)] <- o
    jac[cbind(k + 1L, k)] <- o
    e <- eigen(jac, symmetric = TRUE)
    out <- list(p = (e$values + 1) / 2, w = e$vectors[1L, ]^2)
    if (length(ls(cache)) > 500L) rm(list = ls(cache), envir = cache)
    cache[[key]] <- out
    out
  }
})

# The expected transactions of BG/NBD and MBG/NBD from the integral form, for each customer:
# CET(t) = P(alive) · E[G(p)/p], p ~ Beta(a, B), B = b + x + shift, G(p) = 1 − (1 + p·t/(α+T))^−(r+x).
# G(p)/p bends at the knee p* = (α+T)/((r+x)·t): below it G(p)/p is nearly (r+x)·t/(α+T), above it nearly 1/p.
# Where x is large and T short the knee falls inside the Beta's mass, and a quadrature on p alone misses it
# (64 Gauss–Jacobi nodes were off by 0.5–2 % at x = 200…20,000, T = 2, t = 520). So the integral is cut in three:
#   [0, c₀], c₀ = e⁻² · min(p*, 1/B, 1/2): p = c₀u, the weight u^(a−1) exact (Gauss–Jacobi, 32 nodes), the rest
#     nearly constant there;
#   [c₀, 1/2] on u = log p: smooth in u through the knee and the Beta's cut-off at p ≈ 1/B, Gauss–Legendre with
#     16 nodes on panels at most 1 wide (about log(1/c₀) panels: 3 to 30);
#   [1/2, 1]: p = (1 + v)/2, the weight (1 − v)^(B−1) exact (Gauss–Jacobi, 32 nodes); below 2^−60 of the rest
#     when B ≥ 60, so left out there.
# Measured against integrate() at rel.tol 1e-13 on x = 0…20,000, T = 2…100, t = 1…520: within 2e-14 relative.
clv_cet_quadrature <- function(par, t, x, T, palive, shift) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  near0 <- clv_gauss_beta(a, 1, 32L)
  legendre <- clv_gauss_beta(1, 1, 16L)
  out <- numeric(length(x))
  for (i in seq_along(x)) {
    B <- b + x[[i]] + shift
    k <- t / (alpha + T[[i]])
    n <- r + x[[i]]
    G <- function(p) -expm1(-n * log1p(p * k))
    lb <- lbeta(a, B)
    c0 <- exp(-2) * min(1 / (k * n), 1 / B, 0.5)
    p1 <- c0 * near0$p
    s1 <- exp(a * log(c0) - log(a) - lb) * sum(near0$w * exp((B - 1) * log1p(-p1)) * G(p1) / p1)
    lo <- log(c0)
    hi <- -log(2)
    panels <- max(1L, ceiling(hi - lo))
    edges <- seq(lo, hi, length.out = panels + 1L)
    u <- as.vector(outer(legendre$p, diff(edges)) + rep(edges[-(panels + 1L)], each = length(legendre$p)))
    wu <- as.vector(outer(legendre$w, diff(edges)))
    s2 <- sum(wu * exp((a - 1) * u + (B - 1) * log1p(-exp(u)) - lb) * G(exp(u)))
    s3 <- 0
    if (B < 60) {
      far <- clv_gauss_beta(1, B, 32L)
      p3 <- (1 + far$p) / 2
      s3 <- exp(-B * log(2) - log(B) - lb) * sum(far$w * p3^(a - 2) * G(p3))
    }
    out[[i]] <- palive[[i]] * (s1 + s2 + s3)
  }
  out
}

# BG/NBD's and MBG/NBD's expected transactions in (T, T + t]: the closed forms with GSL's ₂F₁ (NA where GSL's
# status is not 0), ((α+T)/(α+T+t))^(r+x) as exp(−(r+x)·log1p(t/(α+T))). BG/NBD (Fader, Hardie, Lee 2005, eq. 10):
# (a+b+x−1)/(a−1) · [1 − (…)^(r+x) ₂F₁(r+x, b+x; a+b+x−1; z)] · P(alive); written here, not CLVTools' CET, because
# CLVTools' does not say when GSL failed (8 configurations in 3,000 drawn by the review returned a number off by
# up to 241 %).
clv_bgnbd_cet_closed <- function(par, t, x, tx, T) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  h <- clv_hyp2f1(r + x, b + x, a + b + x - 1, t / (alpha + T + t))
  (a + b + x - 1) / (a - 1) * (1 - exp(-(r + x) * log1p(t / (alpha + T))) * h) * clv_palive("bg_nbd", par, x, tx, T)
}

clv_mbg_cet_closed <- function(par, t, x, tx, T) {
  r <- par[[1L]]
  alpha <- par[[2L]]
  a <- par[[3L]]
  b <- par[[4L]]
  h <- clv_hyp2f1(r + x, b + x + 1, a + b + x, t / (alpha + T + t))
  (a + b + x) / (a - 1) * (1 - exp(-(r + x) * log1p(t / (alpha + T))) * h) * clv_mbg_palive(par, x, tx, T)
}

# The expected transactions of a continuous-time model in the next `t` periods, for each customer.
# BG/NBD and MBG/NBD fall back on the quadrature where GSL fails (a status other than 0, a result that is not
# finite or is negative, or a = 1 where the closed form divides by zero); `attr(, "quadrature")` says
# which customers.
clv_cet <- function(model, par, t, x, tx, T) {
  if (model == "bg_bb") {
    return(structure(clv_bgbb_cet(par, t, x, tx, T), quadrature = rep(FALSE, length(x))))
  }
  if (model == "pareto_nbd") {
    out <- clv_fn("pnbd_nocov_CET")(par[[1L]], par[[2L]], par[[3L]], par[[4L]], t, x, tx, T)
    return(structure(out, quadrature = rep(FALSE, length(x))))
  }
  closed <- if (model == "bg_nbd") clv_bgnbd_cet_closed(par, t, x, tx, T) else clv_mbg_cet_closed(par, t, x, tx, T)
  bad <- !is.finite(closed) | closed < 0 | par[[3L]] == 1
  if (any(bad)) {
    pa <- clv_palive(model, par, x[bad], tx[bad], T[bad])
    closed[bad] <- clv_cet_quadrature(par, t, x[bad], T[bad], pa, if (model == "mbg_nbd") 1 else 0)
  }
  structure(closed, quadrature = bad)
}

# ── a purchase model's fit ───────────────────────────────────────────────────────
# The fit of one model on the summary `s`: its estimates, standard errors, intervals, log-likelihood,
# AIC, BIC, and what the optimizer said. `comp` is the compressed summary (clv_compress), made once.
clv_fit_purchases <- function(model, s, comp = NULL, alpha = 0.05, inference = TRUE) {
  comp <- comp %||% clv_compress(s$x, s$tx, s$T)
  nll <- clv_nll(model, comp)
  fit <- clv_optimize(nll, model, hessian = inference, sound = function(lp, value) clv_sound(model, comp, lp, value),
                      stable = clv_nll(model, comp, stable = TRUE))
  inf <- clv_inference(fit, alpha)
  par <- exp(fit$lp)
  names(par) <- CLV_MODELS[[model]]$params
  k <- length(par)
  ll <- -fit$value
  list(model = model, par = par, log_par = fit$lp, log_lik = ll, aic = 2 * k - 2 * ll, bic = k * log(s$n) - 2 * ll, n_params = k,
       converged = fit$convcode == 0, fault = fit$fault, kkt1 = fit$kkt1, kkt2 = fit$kkt2, convcode = fit$convcode, fallback = fit$fallback,
       optimizer = fit$optimizer, vcov_log = inf$vcov_log, se = inf$se, ci_low = inf$ci_low, ci_high = inf$ci_high)
}

# The Hessian, the standard errors and the intervals of a fit made without them (`inference = FALSE`), at its
# optimum: what the candidates not chosen never need. The KKT tests are optimx's: a small gradient, a positive
# definite Hessian.
# A fit whose end point is numerical noise (`fault`) gets none of it: its Hessian would be the noise's. After a
# fallback the derivatives are taken on the function the estimate was found on (BG/NBD's stable form).
clv_add_inference <- function(p, s, comp, alpha = 0.05) {
  if (isTRUE(p$fault)) {
    p$edges <- clv_no_edges()
    return(p)
  }
  nll <- clv_nll(p$model, comp, stable = isTRUE(p$fallback))
  h <- tryCatch(numDeriv::hessian(nll, p$log_par), error = function(e) NULL)
  g <- tryCatch(numDeriv::grad(nll, p$log_par), error = function(e) NULL)
  inf <- clv_inference(list(lp = p$log_par, hessian = h), alpha)
  p$vcov_log <- inf$vcov_log
  p$se <- inf$se
  p$ci_low <- inf$ci_low
  p$ci_high <- inf$ci_high
  p$gradient <- g
  if (!isTRUE(p$fallback)) {
    p$kkt1 <- !is.null(g) && max(abs(g)) <= 1e-3 * (1 + abs(nll(p$log_par)))
    p$kkt2 <- !is.null(h) && all(is.finite(h)) && all(eigen(h, symmetric = TRUE, only.values = TRUE)$values > 0)
  }
  p$edges <- clv_edges(clv_nll(p$model, comp, stable = TRUE), p$log_par, p$model)
  p
}

# ── the edges: where the likelihood keeps growing ────────────────────────────────
# The two parameters of each mixing distribution of a model: the purchase rate's, the dropout's (Gamma–Gamma:
# the spend's scale, q and γ, whose ratio is the mean of ν).
CLV_PAIRS <- list(
  pareto_nbd = list(rate = c("r", "alpha"), dropout = c("s", "beta")),
  bg_nbd = list(rate = c("r", "alpha"), dropout = c("a", "b")),
  mbg_nbd = list(rate = c("r", "alpha"), dropout = c("a", "b")),
  bg_bb = list(rate = c("alpha", "beta"), dropout = c("gamma", "delta")),
  gamma_gamma = list(scale = c("q", "gamma")))

clv_no_edges <- function() data.frame(param = character(), side = character(), pair = character(), stringsAsFactors = FALSE)

# The estimates at an edge, as a fact of the likelihood: one step further towards the edge (a factor e on the
# parameter, the scale the optimizer works on) the log-likelihood is not lower than at the estimate, beyond
# rounding. The directions: each parameter down (towards 0) and up (towards ∞), and each pair of a mixing
# distribution up together (its mean kept: the spread goes to 0, where the customers look alike). At an
# interior maximum every step lowers it; at an edge the maximum is the limit, not a point, so the step does
# not. Returns one row per parameter at an edge: `side` low or high, `pair` the pair's kind when it is the
# pair's direction. 2k + pairs evaluations of the likelihood, once, for the chosen model and the spend.
clv_edges <- function(nll, lp, model) {
  names_ <- CLV_MODELS[[model]]$params
  v0 <- tryCatch(nll(lp), error = function(e) NA_real_)
  out <- clv_no_edges()
  if (!is.finite(v0)) return(out)
  tol <- CLV_ROUNDING * (1 + abs(v0))
  grows <- function(d) {
    v <- tryCatch(nll(lp + d), error = function(e) NA_real_)
    is.finite(v) && v <= v0 + tol
  }
  for (k in seq_along(names_)) {
    for (side in c("low", "high")) {
      d <- numeric(length(names_))
      d[[k]] <- if (side == "low") -1 else 1
      if (grows(d)) out[nrow(out) + 1L, ] <- list(names_[[k]], side, "")
    }
  }
  for (kind in names(CLV_PAIRS[[model]])) {
    two <- CLV_PAIRS[[model]][[kind]]
    d <- as.numeric(names_ %in% two)
    if (grows(d)) for (name in two) out[nrow(out) + 1L, ] <- list(name, "high", kind)
  }
  out
}

# ── Gamma–Gamma ──────────────────────────────────────────────────────────────────
# Fitted on the repeaters with a spend, through CLVTools' gg_LL (the negative log-likelihood).
clv_fit_spend <- function(s, alpha = 0.05) {
  use <- s$x > 0 & !is.na(s$m)
  x <- s$x[use]
  m <- s$m[use]
  gg <- clv_fn("gg_LL")
  nll <- function(lp) gg(unname(lp), x, m, rep(1, length(x)))
  fit <- clv_optimize(nll, "gamma_gamma")
  inf <- clv_inference(fit, alpha)
  edges <- clv_edges(nll, fit$lp, "gamma_gamma")
  par <- stats::setNames(exp(fit$lp), CLV_MODELS$gamma_gamma$params)
  if (par[["q"]] <= 1) {
    tab_refuse("spend_no_mean", paste0("the spend model's q is ", clv_num(round(par[["q"]], 2)), ", at most 1: the average spend has no finite mean, ",
                                       "and the CLV cannot be computed; look for extreme amounts, or leave out the spend column"),
               list(q = par[["q"]]))
  }
  list(model = "gamma_gamma", par = par, log_par = fit$lp, log_lik = -fit$value, n = length(x), converged = fit$convcode == 0,
       kkt1 = fit$kkt1, kkt2 = fit$kkt2, convcode = fit$convcode, fallback = fit$fallback, optimizer = fit$optimizer,
       vcov_log = inf$vcov_log, se = inf$se, ci_low = inf$ci_low, ci_high = inf$ci_high, edges = edges)
}

# The population's expected average spend, pγ/(q − 1).
clv_spend_population <- function(par) par[["p"]] * par[["gamma"]] / (par[["q"]] - 1)

# E[M | x, m̄] = (γ + x m̄) p / (p x + q − 1) for a repeater with a spend; the population's otherwise.
clv_expected_spend <- function(par, x, m) {
  p <- par[["p"]]
  q <- par[["q"]]
  g <- par[["gamma"]]
  out <- rep(clv_spend_population(par), length(x))
  use <- x > 0 & !is.na(m)
  out[use] <- (g + x[use] * m[use]) * p / (p * x[use] + q - 1)
  out
}

# ── the CLV ───────────────────────────────────────────────────────────────────────
# Σ_k [CET(k) − CET(k−1)] / (1 + d)^k over the horizon, the periods' ends discounted; `cet(t)` gives the
# customers' expected transactions in (T, T + t]. Above `max_steps` periods the steps are ⌈H / max_steps⌉
# long, each discounted at its end. Returns the discounted transactions, CET(H), the steps used and
# which customers needed the quadrature.
clv_discounted <- function(cet, horizon, rate, max_steps = 520) {
  len <- ceiling(horizon / max_steps)
  times <- unique(pmin(seq(len, by = len, length.out = ceiling(horizon / len)), horizon))
  prev <- NULL
  disc <- NULL
  quad <- NULL
  for (t in times) {
    cur <- cet(t)
    q <- attr(cur, "quadrature")
    quad <- if (is.null(quad)) q else quad | q
    if (is.null(prev)) prev <- rep(0, length(cur))
    if (is.null(disc)) disc <- rep(0, length(cur))
    disc <- disc + (as.vector(cur) - prev) / (1 + rate)^t
    prev <- as.vector(cur)
  }
  list(discounted = disc, cet = prev, steps = length(times), step_length = len, quadrature = quad)
}

# The per-customer numbers of a fit, from the compressed summary: P(alive), CET(horizon),
# CET(holdout length), the expected spend, the discounted transactions and the CLV. `purchases` is a
# clv_fit_purchases() (or any list with model and par), `spend` a clv_fit_spend() or NULL. `s` is a summary
# (or any list with x, tx, T, m); `plan` the horizon (periods), annual rate, unit, margin, holdout length and
# max_steps. The same function serves Predict from the card, so a prediction of the training rows is the fit's.
clv_evaluate <- function(model, par, spend_par, s, plan) {
  comp <- clv_compress(s$x, s$tx, s$T)
  at <- comp$index
  d <- clv_period_rate(plan$discount_rate, plan$unit)
  cet <- function(t) clv_cet(model, par, t, comp$x, comp$tx, comp$T)
  pa <- clv_palive(model, par, comp$x, comp$tx, comp$T)
  value <- clv_discounted(cet, plan$horizon, d, plan$max_steps %||% 520)
  hold <- if (is.null(plan$holdout_length) || is.na(plan$holdout_length)) NULL else as.vector(cet(plan$holdout_length))
  out <- list(p_alive = pa[at], transactions = value$cet[at], holdout = if (is.null(hold)) NULL else hold[at],
              discounted_transactions = value$discounted[at], steps = value$steps, step_length = value$step_length,
              quadrature = value$quadrature[at], rate_per_period = d)
  if (!is.null(spend_par)) {
    out$spend <- clv_expected_spend(spend_par, s$x, s$m)
    out$clv <- plan$margin * out$spend * out$discounted_transactions
  }
  out
}

# ── the holdout, the chi-square, the checks' numbers ─────────────────────────────
# The holdout's totals and errors for the customers that have one.
clv_holdout <- function(expected, observed) {
  use <- !is.na(observed)
  e <- expected[use]
  o <- observed[use]
  if (!length(o)) return(NULL)
  list(n = length(o), observed = sum(o), expected = sum(e), rel_error = if (sum(o) > 0) (sum(e) - sum(o)) / sum(o) else NA_real_,
       mae = mean(abs(e - o)), rmse = sqrt(mean((e - o)^2)),
       correlation = if (length(o) > 2L && stats::sd(e) > 0 && stats::sd(o) > 0) stats::cor(e, o) else NA_real_)
}

# The frequency classes for the fit's check and charts: the counts 0, 1, 2…, the last open («k+»), pooled into
# runs of adjacent counts until EVERY class expects at least `min_expected` customers. The 5 is Cochran's rule
# for Pearson's χ² (its χ² distribution is an approximation that needs expected counts of about 5 or more), a
# condition of the test, not a threshold of a verdict. From the top down: the open tail gathers counts until it
# expects 5, then each next class downwards does the same; a remainder at the bottom joins the class above it.
# `expected` is the model's expected number of customers per count 0…K, with the open tail as its last element,
# `observed` likewise. x0 is a class's lowest count, x1 its highest (NA for the open last class).
clv_pool_classes <- function(observed, expected, min_expected = 5) {
  k <- length(expected)
  lo <- integer()
  hi <- integer()
  o <- numeric()
  e <- numeric()
  acc_o <- 0
  acc_e <- 0
  top <- k
  for (j in k:1) {
    acc_o <- acc_o + observed[[j]]
    acc_e <- acc_e + expected[[j]]
    if (acc_e >= min_expected) {
      lo <- c(lo, j)
      hi <- c(hi, top)
      o <- c(o, acc_o)
      e <- c(e, acc_e)
      acc_o <- 0
      acc_e <- 0
      top <- j - 1L
    }
  }
  if (top >= 1L) {
    # a remainder at the bottom joins the class above it (or is the only class)
    m <- length(e)
    if (m) {
      lo[[m]] <- 1L
      o[[m]] <- o[[m]] + acc_o
      e[[m]] <- e[[m]] + acc_e
    } else {
      lo <- 1L
      hi <- k
      o <- acc_o
      e <- acc_e
    }
  }
  ord <- rev(seq_along(e))
  x1 <- as.numeric(hi[ord] - 1L)
  x1[[length(x1)]] <- NA_real_
  list(x0 = as.numeric(lo[ord] - 1L), x1 = x1, observed = o[ord], expected = e[ord])
}

# The model's expected number of customers per repeat count, against the observed ones: the χ² with its
# degrees of freedom (classes − 1 − the parameters), the largest gap between the observed and expected
# shares. Expected = Σ_i P(X(T_i) = k); it depends on T only, so the distinct T are taken with their counts.
# Continuous ages: when they are more than `max_ages` distinct, the PMF (smooth in T) is taken at the mean age of
# `max_ages` classes of equal counts. BG/BB's opportunities are whole numbers (at most `max_opportunities`): its
# PMF is taken at each distinct n, never at a mean (a PMF at 37.4 opportunities is not a probability).
clv_frequency_fit <- function(model, par, s, max_class = 60L, max_ages = 200L) {
  k_max <- min(max(s$x), max_class)
  tt <- sort(unique(s$T))
  w <- tabulate(match(s$T, tt), length(tt))
  if (length(tt) > max_ages && !identical(model, "bg_bb")) {
    # many distinct ages (continuous time): the PMF is smooth in T, so the customers are grouped in `max_ages`
    # classes of equal counts and each class is taken at its mean age (a few hundred PMF evaluations, not one per customer)
    g <- findInterval(s$T, unique(stats::quantile(s$T, seq(0, 1, length.out = max_ages + 1L), names = FALSE, type = 1)), rightmost.closed = TRUE)
    w <- tabulate(g)
    tt <- as.numeric(tapply(s$T, g, mean))
    w <- w[w > 0]
  }
  e <- vapply(0:k_max, function(k) sum(w * clv_pmf(model, par, k, tt)), 0)
  e <- c(e, max(s$n - sum(e), 0))
  o <- c(tabulate(s$x[s$x <= k_max] + 1, k_max + 1L), sum(s$x > k_max))
  cl <- clv_pool_classes(o, e)
  stat <- sum((cl$observed - cl$expected)^2 / cl$expected)
  df <- length(cl$expected) - 1 - length(par)
  c(cl, list(statistic = stat, df = df, p_value = if (df >= 1) stats::pchisq(stat, df, lower.tail = FALSE) else NA_real_,
             max_share_gap = max(abs(cl$observed - cl$expected)) / s$n, n_classes = length(cl$expected)))
}

# The frequency class of each customer (the classes of clv_frequency_fit: x0 the lowest count of each).
clv_class_of <- function(x, classes) findInterval(x, classes$x0)

# The mean holdout purchases per customer of each frequency class, observed and expected.
clv_holdout_by_class <- function(x, observed, expected, classes) {
  cls <- clv_class_of(x, classes)
  use <- !is.na(observed)
  k <- length(classes$x0)
  n <- tabulate(cls[use], k)
  avg <- function(v) vapply(seq_len(k), function(j) if (n[[j]]) mean(v[use & cls == j]) else NA_real_, 0)
  list(x0 = classes$x0, x1 = classes$x1, n = n, observed = avg(observed), expected = avg(expected))
}

# Pearson's r between the frequency and the average spend of the repeaters, and the test of the slope of
# m̄ on x with HC3 standard errors (under Gamma–Gamma the variance of m̄ falls with x, so the classical test
# would flag the model's own data).
clv_spend_independence <- function(x, m) {
  use <- x > 0 & !is.na(m)
  x <- x[use]
  m <- m[use]
  if (length(x) < 4L || stats::sd(x) == 0 || stats::sd(m) == 0) return(list(r = NA_real_, statistic = NA_real_, p_value = NA_real_, n = length(x)))
  design <- cbind(1, x)
  fit <- stats::lm.fit(design, m)
  e <- fit$residuals
  h <- rowSums((design %*% solve(crossprod(design))) * design)
  bread <- solve(crossprod(design))
  v <- bread %*% crossprod(design * (e / (1 - h))) %*% bread
  z <- fit$coefficients[[2L]] / sqrt(v[2L, 2L])
  list(r = stats::cor(x, m), statistic = z, p_value = 2 * stats::pnorm(-abs(z)), n = length(x))
}

# ── P(alive): the grid, the curves, the distribution ────────────────────────────
# The typical age: the median, rounded to a tenth (at least a tenth).
clv_typical_age <- function(age) max(round(stats::median(age), 1), 0.1)

# P(alive) over frequency × recency class for a customer known for `age`: x = 0…`x_max`, ten classes of
# [0, age], the class's middle as t_x; for x = 0 one cell (t_x = 0). BG/BB counts opportunities: the classes
# are up to ten opportunities (the last one of a class is its t_x), and a class is there only when t_x ≥ x.
clv_palive_cells <- function(model, par, age, x_max) {
  bgbb <- identical(model, "bg_bb")
  levels <- character()
  level_2 <- character()
  value <- numeric()
  if (bgbb) {
    age <- max(round(age), 1)
    ends <- unique(round(seq(1, age, length.out = min(10, age))))
  } else {
    edges <- seq(0, age, length.out = 11L)
    mid <- (edges[-1L] + edges[-11L]) / 2
    lab <- sprintf("%.1f\u2013%.1f", edges[-11L], edges[-1L])
  }
  for (x in 0:x_max) {
    if (x == 0) {
      levels <- c(levels, "0")
      level_2 <- c(level_2, "0")
      value <- c(value, clv_palive(model, par, 0, 0, age))
      next
    }
    if (bgbb) {
      tx <- ends[ends >= x]
      if (!length(tx)) next
      lab_x <- as.character(tx)
    } else {
      tx <- pmin(pmax(mid, 1e-6 * age), age)
      lab_x <- lab
    }
    levels <- c(levels, rep(as.character(x), length(tx)))
    level_2 <- c(level_2, lab_x)
    value <- c(value, clv_palive(model, par, rep(x, length(tx)), tx, rep(age, length(tx))))
  }
  data.frame(level = levels, level_2 = level_2, value = value, stringsAsFactors = FALSE)
}

# P(alive) against the recency for x = 1, 2, 5, 10 (those within the largest x) at the age: 50 points
# (BG/BB: the opportunities x…age, at most 50 of them).
clv_palive_curves <- function(model, par, age, x_max) {
  bgbb <- identical(model, "bg_bb")
  out <- list()
  if (bgbb) age <- max(round(age), 1)
  for (x in c(1, 2, 5, 10)) {
    if (x > x_max || (bgbb && x > age)) next
    at <- if (bgbb) unique(round(seq(x, age, length.out = min(50, age - x + 1)))) else seq(0, age, length.out = 50)
    tx <- if (bgbb) at else pmax(at, 1e-6 * age)
    out[[length(out) + 1L]] <- data.frame(level_2 = paste0("x=", x), x = at,
                                          y = clv_palive(model, par, rep(x, length(tx)), tx, rep(age, length(tx))), stringsAsFactors = FALSE)
  }
  if (length(out)) do.call(rbind, out) else data.frame(level_2 = character(), x = numeric(), y = numeric())
}

# The distribution of P(alive): 20 bins of 0.05.
clv_palive_hist <- function(p) {
  edges <- seq(0, 1, by = 0.05)
  counts <- tabulate(pmin(findInterval(p, edges, rightmost.closed = TRUE), 20L), 20L)
  data.frame(x0 = edges[-21L], x1 = edges[-1L], count = counts)
}

# ── the spend's chart, the CLV's histogram, the concentration, the top customers ─
# The observed spend of the repeaters in 20 classes at the quantiles 0, 5 %… 95 % (the last open),
# and the model's expected counts: Σ_i P(m̄ in the class | x_i), from x m̄/(γ + x m̄) ~ Beta(p x, q).
# The first and the last class are open for the expected counts, so that they add up to the observed.
clv_spend_hist <- function(par, x, m) {
  use <- x > 0 & !is.na(m)
  x <- x[use]
  m <- m[use]
  edges <- unique(as.numeric(stats::quantile(m, seq(0, 0.95, by = 0.05), type = 7, names = FALSE)))
  k <- length(edges)
  at <- pmin(findInterval(m, edges), k)
  at[at == 0L] <- 1L
  observed <- tabulate(at, k)
  upper <- c(edges[-1L], Inf)
  cum <- vapply(upper, function(b) if (is.finite(b)) sum(stats::pbeta(x * b / (par[["gamma"]] + x * b), par[["p"]] * x, par[["q"]])) else length(x), 0)
  expected <- diff(c(0, cum))
  data.frame(x0 = edges, x1 = c(edges[-1L], NA_real_), observed = observed, expected = expected)
}

# The CLV's histogram: a class for 0, then up to 20 classes equal on log10 of the values above 0.
clv_value_hist <- function(v) {
  v <- v[is.finite(v)]
  zero <- sum(v <= 0)
  pos <- v[v > 0]
  out <- data.frame(x0 = 0, x1 = 0, count = zero)
  if (!length(pos)) return(out)
  lo <- log10(min(pos))
  hi <- log10(max(pos))
  if (hi - lo < 1e-9) return(rbind(out, data.frame(x0 = min(pos), x1 = max(pos), count = length(pos))))
  edges <- 10^seq(lo, hi, length.out = 21L)
  counts <- tabulate(pmin(findInterval(pos, edges, rightmost.closed = TRUE), 20L), 20L)
  rbind(out, data.frame(x0 = edges[-21L], x1 = edges[-1L], count = counts))
}

# The share of the total held by the top customers: 101 points, x the share of customers ranked from the highest.
clv_concentration <- function(v) {
  v <- sort(v[is.finite(v)], decreasing = TRUE)
  total <- sum(v)
  q <- seq(0, 1, by = 0.01)
  top <- ceiling(q * length(v) - 1e-9)
  cum <- c(0, cumsum(v))
  data.frame(x = q, y = if (total > 0) cum[top + 1L] / total else NA_real_)
}

# The share of the total held by the top `fraction` of the customers.
clv_top_share <- function(v, fraction) {
  v <- sort(v[is.finite(v)], decreasing = TRUE)
  if (!length(v) || sum(v) <= 0) return(NA_real_)
  sum(v[seq_len(max(ceiling(fraction * length(v) - 1e-9), 1L))]) / sum(v)
}

# ── the card ──────────────────────────────────────────────────────────────────────
# The card of a fit (card.schema.json «customer_lifetime»): the parameters, the unit, the horizon, the
# rate and the margin are all that a prediction needs. `inputs` are the numbers the rows must hold.
clv_card <- function(task, s, fit, library = list()) {
  card <- tab_card(task, NULL, packages = c("optimx", "CLVTools"))
  inputs <- c(s$columns$frequency, s$columns$recency, s$columns$age)
  if (!is.null(fit$spend)) inputs <- c(inputs, s$columns$spend)
  card$inputs <- lapply(inputs, function(name) list(name = name, type = "number"))
  parts <- function(f) {
    out <- list(model = f$model, params = as.list(f$par))
    out$vcov_log <- if (is.null(f$vcov_log)) NULL else lapply(seq_len(nrow(f$vcov_log)), function(i) as.list(f$vcov_log[i, ]))
    out
  }
  card$customer_lifetime <- list(
    node = s$node, time_unit = s$unit, purchase_model = fit$purchases$model, chosen_by = fit$chosen_by,
    purchases = parts(fit$purchases), spend = if (is.null(fit$spend)) NULL else parts(fit$spend),
    horizon = fit$plan$horizon, discount_rate = fit$plan$discount_rate, periods_per_year = clv_periods_per_year(s$unit),
    margin = fit$plan$margin, library = library)
  card
}

# The versions of the libraries that fitted the model, for the card and the model rows.
clv_library <- function() {
  gsl <- tryCatch(suppressWarnings(system2("gsl-config", "--version", stdout = TRUE, stderr = FALSE)), error = function(e) character())
  if (!length(gsl)) {
    gsl <- tryCatch(suppressWarnings(system2("dpkg-query", c("-W", "-f=${Version}", "libgsl27"), stdout = TRUE, stderr = FALSE)), error = function(e) character())
    gsl <- sub("^([0-9]+[.][0-9]+([.][0-9]+)?).*$", "\\1", gsl)
  }
  list(CLVTools = as.character(utils::packageVersion("CLVTools")), gsl = if (length(gsl) && nzchar(gsl[[1L]])) gsl[[1L]] else "unknown")
}

# The plan of a node's run: the horizon, the discounting, the margin, the holdout.
clv_plan <- function(task, s) {
  p <- task$params
  list(horizon = as.integer(p$horizon %||% clv_default_horizon(s$unit)), discount_rate = as.numeric(p$discount_rate %||% 0.1),
       margin = as.numeric(p$margin %||% 1), unit = s$unit, holdout_length = if (s$has_holdout) s$holdout_length else NA_real_,
       max_steps = tab_limit(task, "max_clv_steps"))
}

# The numbers a prediction needs, evaluated for the rows of `df` from a card (Predict): the card's columns
# become the summary, the rows that break a rule of the summary (or hold a NULL) get NULL predictions. Returns
# the data frame of predictions, with `n_bad` and `n_null`. The same functions as the fit's: the training rows
# give the fit's numbers.
clv_predict_frame <- function(card, df, horizon = NULL, max_steps = 520, max_opportunities = 1000) {
  cl <- card$customer_lifetime
  node <- cl$node
  purchases <- clv_card_params(cl, "purchases")
  spend <- if (is.null(cl$spend)) NULL else clv_card_params(cl, "spend")
  inputs <- vapply(card$inputs, function(i) i$name, "")
  for (name in inputs) {
    if (!name %in% names(df)) tab_refuse("missing_column", paste0("the rows have no column «", name, "», which the model was fitted on"), list(column = name))
  }
  num <- function(name) clv_numbers(df, name)
  with_spend <- !is.null(cl$spend)
  v <- list(id = as.character(seq_len(nrow(df))), x = num(inputs[[1L]]), tx = num(inputs[[2L]]), T = num(inputs[[3L]]),
            m = if (with_spend) num(inputs[[4L]]) else rep(NA_real_, nrow(df)), h = rep(NA_real_, nrow(df)))
  # a NULL or an infinite number gets a NULL prediction (the fit refuses an infinite spend: Predict leaves its row out)
  isnull <- is.na(v$x) | is.na(v$tx) | is.na(v$T) | is.infinite(v$x) | is.infinite(v$tx) | is.infinite(v$T) | is.infinite(v$m)
  bad <- rep(FALSE, nrow(df))
  usable <- !isnull
  if (any(usable)) {
    w <- lapply(v, function(z) z[usable])
    for (rule in clv_rules(w, node, max_opportunities)) bad[usable] <- bad[usable] | rule$bad
  }
  ok <- usable & !bad
  out <- data.frame(p_alive = rep(NA_real_, nrow(df)), transactions = NA_real_, spend = NA_real_, clv = NA_real_, discounted_transactions = NA_real_)
  if (any(ok)) {
    # as the fit reads its summary (clv_read_summary): x rounded; BG/BB's recency and opportunities rounded, the
    # continuous recency clamped to the age
    s <- list(x = round(v$x[ok]), tx = v$tx[ok], T = v$T[ok], m = v$m[ok])
    if (identical(node, "bg_bb")) {
      s$tx <- round(s$tx)
      s$T <- round(s$T)
    } else {
      s$tx <- pmin(s$tx, s$T)
    }
    s$m[s$x == 0] <- NA_real_
    plan <- list(horizon = as.integer(horizon %||% cl$horizon), discount_rate = cl$discount_rate, unit = cl$time_unit, margin = cl$margin,
                 holdout_length = NA_real_, max_steps = max_steps)
    ev <- clv_evaluate(cl$purchases$model, purchases, spend, s, plan)
    out$p_alive[ok] <- ev$p_alive
    out$transactions[ok] <- ev$transactions
    out$discounted_transactions[ok] <- ev$discounted_transactions
    if (with_spend) {
      out$spend[ok] <- ev$spend
      out$clv[ok] <- ev$clv
    }
  }
  list(predictions = out, n_bad = sum(bad), n_null = sum(isnull), ok = ok)
}

# The parameters of a card's part ("purchases" or "spend"), by their names in CLV_MODELS' order: a card read
# from MLflow is JSON, and the order of its keys is not a promise. A card that does not hold what the fit wrote
# is refused with a sentence: a model outside the list (or one the node does not fit), a parameter missing, or
# one that is not a positive finite number.
clv_card_params <- function(cl, part) {
  bad <- function(why) tab_refuse("bad_card", paste0("the saved model cannot predict: ", why, "; fit it again"), list(part = part))
  node <- cl$node
  if (!is.character(node) || length(node) != 1L || !node %in% c("clv", "bg_bb")) bad("its node is not clv or bg_bb")
  model <- cl[[part]]$model
  allowed <- if (part == "spend") "gamma_gamma" else if (identical(node, "bg_bb")) "bg_bb" else c("pareto_nbd", "bg_nbd", "mbg_nbd")
  if (!is.character(model) || length(model) != 1L || !model %in% allowed) {
    bad(paste0("its ", part, " model «", paste(model, collapse = ""), "» is not one the node ", node, " fits"))
  }
  if (identical(part, "purchases") && !identical(cl$purchase_model, model)) bad("its purchase model and its parameters' model differ")
  names_ <- CLV_MODELS[[model]]$params
  raw <- cl[[part]]$params
  got <- vapply(names_, function(k) {
    v <- raw[[k]]
    if (is.numeric(v) && length(v) == 1L) as.numeric(v) else NA_real_
  }, 0)
  wrong <- names_[!is.finite(got) | got <= 0]
  if (length(wrong)) bad(paste0("the ", part, " parameter ", wrong[[1L]], " is not a positive number"))
  stats::setNames(got, names_)
}

# ── a candidate, the choice, the whole fit ───────────────────────────────────────
# One purchase model fitted and read against the data: its estimates, the per-customer numbers
# without spend, the χ² of the frequency classes and the holdout.
clv_candidate <- function(model, s, comp, plan, alpha) {
  p <- clv_fit_purchases(model, s, comp, alpha, inference = FALSE)
  ev <- clv_evaluate(model, p$par, NULL, s, plan)
  freq <- clv_frequency_fit(model, p$par, s)
  hold <- if (s$has_holdout) clv_holdout(ev$holdout, s$h) else NULL
  list(model = model, p = p, ev = ev, freq = freq, holdout = hold)
}

# The models a node fits: BG/BB alone; `auto` the three of continuous time; `compare` the one asked and the others.
clv_candidates <- function(node, params) {
  if (identical(node, "bg_bb")) return("bg_bb")
  three <- c("pareto_nbd", "bg_nbd", "mbg_nbd")
  asked <- params$purchase_model %||% "auto"
  if (identical(asked, "auto")) return(three)
  if (isTRUE(params$compare)) c(asked, setdiff(three, asked)) else asked
}

# `auto` keeps the highest log-likelihood among the models that converged; a tie within 1e-6 keeps the first.
# A model whose end point is numerical noise (`fault`, clv_sound) is never chosen: its log-likelihood is not
# one. When none converged, the highest among those that are not noise; when all are noise, the first.
clv_choose <- function(candidates, asked) {
  if (!identical(asked, "auto")) return(1L)
  ll <- vapply(candidates, function(c) c$p$log_lik, 0)
  fault <- vapply(candidates, function(c) isTRUE(c$p$fault), logical(1))
  if (all(fault)) return(1L)
  ok <- vapply(candidates, function(c) isTRUE(c$p$converged), logical(1)) & !fault
  pool <- if (any(ok)) which(ok) else which(!fault)
  best <- max(ll[pool])
  pool[ll[pool] >= best - 1e-6][[1L]]
}

# The whole fit of a node from the task: the summary, the candidates, the choice, the spend, the numbers of every customer.
# Returns what the rows, the card and the output are made from (clv_results).
clv_fit <- function(task, node, frame = NULL) {
  p <- task$params
  s <- clv_read_summary(task, node, frame)
  alpha <- as.numeric(p$alpha %||% 0.05)
  asked <- if (identical(node, "bg_bb")) "bg_bb" else p$purchase_model %||% "auto"
  plan <- clv_plan(task, s)
  comp <- clv_compress(s$x, s$tx, s$T)
  models <- clv_candidates(node, p)
  fitted <- lapply(models, function(m) tryCatch(clv_candidate(m, s, comp, plan, alpha), tabularia_refusal = function(e) e))
  failed <- vapply(fitted, inherits, logical(1), "tabularia_refusal")
  if (identical(asked, "auto") || length(models) > 1L) {
    if (all(failed)) stop(fitted[[1L]])
    if (failed[[1L]] && !identical(asked, "auto")) stop(fitted[[1L]])
  } else if (failed[[1L]]) {
    stop(fitted[[1L]])
  }
  candidates <- fitted[!failed]
  skipped <- models[failed]
  chosen <- clv_choose(candidates, asked)
  best <- candidates[[chosen]]
  best$p <- clv_add_inference(best$p, s, comp, alpha)
  candidates[[chosen]] <- best
  spend <- if (s$has_spend) clv_fit_spend(s, alpha) else NULL
  ev <- clv_evaluate(best$model, best$p$par, if (is.null(spend)) NULL else spend$par, s, plan)
  list(node = node, s = s, plan = plan, alpha = alpha, asked = asked, chosen_by = if (identical(asked, "auto")) "log_lik" else "manual",
       candidates = candidates, skipped = skipped, chosen = chosen, model = best$model, purchases = best$p, spend = spend, ev = ev,
       freq = best$freq, holdout = best$holdout, holdout_classes = if (s$has_holdout) clv_holdout_by_class(s$x, s$h, ev$holdout, best$freq) else NULL)
}

# ── the rows ─────────────────────────────────────────────────────────────────────
clv_formula_text <- function(s) {
  if (identical(s$node, "bg_bb")) {
    paste0("x: transactions, t_x: opportunity of the last, n: opportunities, one opportunity is a ", s$unit)
  } else {
    paste0("x: frequency, t_x: recency, T: age, in ", s$unit, "s")
  }
}

# The notes, as `@id name=value …` (lists joined with «,», numbers with a point): the report words them in the page's language.
clv_notes <- function(fit) {
  notes <- fit$s$notes
  if (any(vapply(fit$candidates, function(c) c$p$fallback, logical(1))) || (!is.null(fit$spend) && fit$spend$fallback)) {
    notes$optimizer_fallback <- "@optimizer_fallback"
  }
  q <- sum(fit$ev$quadrature %in% TRUE)
  if (q > 0L) {
    notes$cet_quadrature <- paste0("@cet_quadrature n=", q)
  }
  if (fit$ev$step_length > 1L) {
    notes$clv_steps <- paste0("@clv_steps max=", fit$plan$max_steps, " step=", fit$ev$step_length)
  }
  if (length(fit$skipped)) {
    notes$candidate_failed <- paste0("@candidate_failed models=", paste(fit$skipped, collapse = ","))
  }
  notes
}

# The section `model` and the estimates: what MLflow logs.
clv_rows_model <- function(rows, task, fit) {
  s <- fit$s
  best <- fit$purchases
  ev <- fit$ev
  bgbb <- identical(s$node, "bg_bb")
  par <- best$par
  tab_text(rows, "model", "formula", clv_formula_text(s))
  lib <- clv_library()
  tab_text(rows, "model", "software", paste0("R ", R.version$major, ".", R.version$minor, "; CLVTools ", lib$CLVTools, "; optimx ",
                                              utils::packageDescription("optimx")$Version, "; GSL ", lib$gsl))
  tab_text(rows, "model", "purchase_model", fit$model)
  tab_text(rows, "model", "purchase_model_asked", fit$asked)
  tab_text(rows, "model", "time_unit", s$unit)
  tab_text(rows, "model", "optimizer", best$optimizer)
  num <- function(stat, value) tab_number(rows, "model", stat, value)
  num("n", s$n)
  num("n_missing", s$n_missing)
  num("n_repeaters", sum(s$x > 0))
  num("share_repeaters", mean(s$x > 0))
  num("mean_frequency", mean(s$x))
  num("max_age", max(s$T))
  num("median_age", stats::median(s$T))
  num("horizon", fit$plan$horizon)
  num("log_lik", best$log_lik)
  num("aic", best$aic)
  num("bic", best$bic)
  num("n_params", best$n_params)
  num("converged", as.numeric(best$converged))
  for (k in seq_along(par)) num(paste0("param_", names(par)[[k]]), par[[k]])
  num("mean_rate", if (bgbb) par[["alpha"]] / (par[["alpha"]] + par[["beta"]]) else par[["r"]] / par[["alpha"]])
  num("mean_dropout", switch(fit$model, pareto_nbd = par[["s"]] / par[["beta"]], bg_bb = par[["gamma"]] / (par[["gamma"]] + par[["delta"]]),
                             par[["a"]] / (par[["a"]] + par[["b"]])))
  num("expected_transactions", sum(ev$transactions))
  num("mean_p_alive", mean(ev$p_alive))
  if (!is.null(fit$holdout)) {
    h <- fit$holdout
    num("holdout_length", s$holdout_length)
    num("holdout_observed", h$observed)
    num("holdout_expected", h$expected)
    num("holdout_rel_error", h$rel_error)
    num("holdout_mae", h$mae)
    num("holdout_rmse", h$rmse)
    num("holdout_correlation", h$correlation)
  }
  if (!is.null(fit$spend)) {
    sp <- fit$spend
    num("spend_n", sp$n)
    num("spend_log_lik", sp$log_lik)
    for (k in seq_along(sp$par)) num(paste0("spend_param_", names(sp$par)[[k]]), sp$par[[k]])
    num("mean_spend_population", clv_spend_population(sp$par))
    num("discount_rate", fit$plan$discount_rate)
    num("discount_per_period", ev$rate_per_period)
    num("margin", fit$plan$margin)
    num("clv_total", sum(ev$clv))
    num("clv_mean", mean(ev$clv))
    num("clv_median", stats::median(ev$clv))
    num("clv_top10_share", clv_top_share(ev$clv, 0.1))
    num("clv_top20_share", clv_top_share(ev$clv, 0.2))
    num("discounted_transactions", sum(ev$discounted_transactions))
  }
  notes <- clv_notes(fit)
  for (id in names(notes)) tab_text(rows, "model", paste0("note_", id), notes[[id]])
  invisible(rows)
}

# The estimates, their standard errors and intervals: a part is «purchases» or «spend».
clv_rows_coef <- function(rows, f, part) {
  for (k in seq_along(f$par)) {
    tab_add(rows, "coef", c("estimate", "std_error", "ci_low", "ci_high"), value = c(f$par[[k]], f$se[[k]], f$ci_low[[k]], f$ci_high[[k]]),
            level = names(f$par)[[k]], level_2 = part)
  }
  invisible(rows)
}

# The comparison of the candidates (with `auto` or `compare`).
clv_rows_candidates <- function(rows, fit) {
  for (k in seq_along(fit$candidates)) {
    c_ <- fit$candidates[[k]]
    p <- c_$p
    stats_ <- c(log_lik = p$log_lik, aic = p$aic, bic = p$bic, n_params = p$n_params, chi_square = c_$freq$statistic,
                chi_square_p_value = c_$freq$p_value, converged = as.numeric(p$converged), chosen = as.numeric(k == fit$chosen))
    if (!is.null(c_$holdout)) stats_ <- c(stats_, holdout_rel_error = c_$holdout$rel_error, holdout_mae = c_$holdout$mae)
    for (k2 in seq_along(p$par)) stats_[[paste0("param_", names(p$par)[[k2]])]] <- p$par[[k2]]
    tab_add(rows, "candidate", names(stats_), value = unname(stats_), level = c_$model)
  }
  invisible(rows)
}

# The charts' points and tables.
clv_rows_charts <- function(rows, task, fit) {
  s <- fit$s
  ev <- fit$ev
  par <- fit$purchases$par
  model <- fit$model
  cl <- fit$freq
  # frequency classes, observed and expected
  for (which in c("observed", "expected")) {
    tab_points(rows, "hist", "frequency", "count", x = cl$x0, y = cl[[which]], level_2 = which, x1 = ifelse(is.na(cl$x1), NA_real_, cl$x1))
  }
  if (!is.null(fit$holdout_classes)) {
    hc <- fit$holdout_classes
    # the classes of the frequency chart (x1: the upper end, NA for the open last class, which the page prints «k+»); the customers
    # of each class are the rows of the stat `count`, so that the means are read against how many they average
    for (which in c("observed", "expected")) {
      tab_points(rows, "hist", "holdout", "mean", x = hc$x0, y = hc[[which]], level_2 = which, x1 = ifelse(is.na(hc$x1), NA_real_, hc$x1))
    }
    tab_points(rows, "hist", "holdout", "count", x = hc$x0, y = hc$n, level_2 = "customers", x1 = ifelse(is.na(hc$x1), NA_real_, hc$x1))
  }
  ph <- clv_palive_hist(ev$p_alive)
  tab_points(rows, "hist", "p_alive", "count", x = ph$x0, y = ph$count, x1 = ph$x1)
  value <- if (is.null(fit$spend)) ev$transactions else ev$clv
  vh <- clv_value_hist(value)
  tab_points(rows, "hist", "clv", "count", x = vh$x0, y = vh$count, x1 = vh$x1)
  age <- clv_typical_age(s$T)
  x_max <- min(10, as.numeric(stats::quantile(s$x, 0.99, names = FALSE)))
  x_max <- max(floor(max(x_max, 1)), 1)
  cells <- clv_palive_cells(model, par, age, x_max)
  tab_number(rows, "cell", "p_alive", cells$value, level = cells$level, level_2 = cells$level_2)
  cv <- clv_palive_curves(model, par, age, max(s$x))
  for (l in unique(cv$level_2)) tab_points(rows, "curve", "p_alive_recency", "y", x = cv$x[cv$level_2 == l], y = cv$y[cv$level_2 == l], level_2 = l)
  conc <- clv_concentration(value)
  tab_points(rows, "curve", "concentration", "y", x = conc$x, y = conc$y)
  if (!is.null(fit$spend)) {
    sh <- clv_spend_hist(fit$spend$par, s$x, s$m)
    for (which in c("observed", "expected")) tab_points(rows, "hist", "spend", "count", x = sh$x0, y = sh[[which]], level_2 = which, x1 = sh$x1)
    rep_ <- which(s$x > 0 & !is.na(s$m))
    keep <- rep_[tab_cap(length(rep_), tab_limit(task, "max_chart_points"), score = s$m[rep_], keep = 10)]
    tab_points(rows, "point", "spend_frequency", "y", x = s$x[keep], y = s$m[keep], labels = tab_extreme_labels(s$m[keep], s$id[keep]))
    line <- tab_lowess(s$x[rep_], s$m[rep_])
    tab_points(rows, "curve", "spend_frequency", "y", x = line$x, y = line$y)
  }
  # the top customers
  top <- head(order(-value, seq_along(value)), tab_limit(task, "max_top_customers"))
  for (k in seq_along(top)) {
    i <- top[[k]]
    stats_ <- c(rank = k, frequency = s$x[[i]], recency = s$tx[[i]], age = s$T[[i]], p_alive = ev$p_alive[[i]], expected_transactions = ev$transactions[[i]])
    if (!is.null(fit$spend)) stats_ <- c(stats_, expected_spend = ev$spend[[i]], clv = ev$clv[[i]])
    tab_add(rows, "customer", names(stats_), value = unname(stats_), level = s$id[[i]])
  }
  invisible(rows)
}

# The node's output rows: the input's columns and the predictions; NULL predictions on the rows left out.
clv_output <- function(df, fit) {
  s <- fit$s
  ev <- fit$ev
  made <- c("pred_p_alive", "pred_transactions", if (s$has_holdout) "pred_holdout_transactions",
            if (!is.null(fit$spend)) c("pred_spend", "pred_discounted_transactions", "pred_clv"))
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"), list(column = clash[[1L]]))
  }
  put <- function(v) {
    out <- rep(NA_real_, nrow(df))
    out[s$row] <- v
    out
  }
  df$pred_p_alive <- put(ev$p_alive)
  df$pred_transactions <- put(ev$transactions)
  if (s$has_holdout) df$pred_holdout_transactions <- put(ev$holdout)
  if (!is.null(fit$spend)) {
    df$pred_spend <- put(ev$spend)
    df$pred_discounted_transactions <- put(ev$discounted_transactions)
    df$pred_clv <- put(ev$clv)
  }
  df
}

# What a node's fit returns to tab_main: the rows, the output (output = rows), the card.
clv_results <- function(task, fit, frame = NULL) {
  rows <- tab_rows_start(task)
  clv_rows_model(rows, task, fit)
  clv_rows_coef(rows, fit$purchases, "purchases")
  if (!is.null(fit$spend)) clv_rows_coef(rows, fit$spend, "spend")
  if (length(fit$candidates) > 1L) clv_rows_candidates(rows, fit)
  clv_rows_charts(rows, task, fit)
  output <- NULL
  if (identical(task$output %||% task$params$output, "rows")) {
    output <- clv_output(if (!is.null(frame)) frame else tab_input(task), fit)
  }
  list(rows = rows, output = output, card = clv_card(task, fit$s, fit, clv_library()), native = NULL)
}

# Predict: the card's model on the rows of a step; the columns are `prediction` (the expected transactions over
# the horizon), `prediction_p_alive` and, with a spend model, `prediction_spend` and `prediction_clv`.
clv_predict_node <- function(task, card) {
  df <- tab_input(task)
  made <- c("prediction", "prediction_p_alive", if (!is.null(card$customer_lifetime$spend)) c("prediction_spend", "prediction_clv"))
  clash <- intersect(made, names(df))
  if (length(clash)) {
    tab_refuse("output_column_exists", paste0("the rows already have a column «", clash[[1L]], "»: rename it before the node"), list(column = clash[[1L]]))
  }
  got <- clv_predict_frame(card, df, horizon = task$params$horizon, max_steps = tab_limit(task, "max_clv_steps"),
                           max_opportunities = tab_limit(task, "max_opportunities"))
  if (got$n_bad > 0L) warning(got$n_bad, " rows break a rule of the summary: their predictions are NULL")
  df$prediction <- got$predictions$transactions
  df$prediction_p_alive <- got$predictions$p_alive
  if (!is.null(card$customer_lifetime$spend)) {
    df$prediction_spend <- got$predictions$spend
    df$prediction_clv <- got$predictions$clv
  }
  list(output = df, n_bad = got$n_bad, n_null = got$n_null)
}
