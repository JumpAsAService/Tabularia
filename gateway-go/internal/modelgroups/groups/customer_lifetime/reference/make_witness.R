# The witnesses of the group customer_lifetime (docs/specs/customer-lifetime-models.md, «Reference values»):
# the runner's functions against packages that compute the same numbers outside Tabularia, and the published
# values against the data they were published on. Run by hand, never in the self-test and never with these
# packages in the group's image: in a scratch R library with GSL, BTYD 2.4.3, BTYDplus 1.2.0 (archived by CRAN:
# from the CRAN Archive), hypergeo 1.2-15 and the recipe's CLVTools (witness.json lists them), for instance
# the group's image with that library on R_LIBS_USER and the runner's code mounted:
#
#   docker run --rm -e R_LIBS_USER=/wl -v <scratch library>:/wl:ro -v <code>:/tabularia/code:ro \
#     -v "$PWD/gateway-go/internal/modelgroups/groups/customer_lifetime/reference:/ref:ro" \
#     tabularia-models-customer_lifetime:dev Rscript /ref/make_witness.R
#
# <code> is `go run ./cmd/modelgroup code customer_lifetime <code>`; without it the script finds the runner
# beside itself in the repository (../runner/R and ../../../common/R).
#
# The CDNOW data (Fader & Hardie 2001; CLVTools' `cdnow`, BTYD's `cdnowSummary`) are loaded from the installed
# packages at run time and never copied into the repository. One line per check, «WITNESS ok|failed <name>:
# measured <difference> (tolerance <tolerance>)»; the script exits 1 when one failed. Lot B3 adds BG/BB's lines
# (the donations, BTYD's bgbb.*) in its own block at the end.

suppressMessages({
  library(CLVTools)
  library(data.table)
  library(BTYD)
  library(BTYDplus)
  library(hypergeo)
})

# ── the runner's code ────────────────────────────────────────────────────────────
here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
code <- Sys.getenv("TABULARIA_CODE", "/tabularia/code")
dirs <- if (dir.exists(file.path(code, "runner", "R"))) file.path(code, c("common/R", "runner/R")) else
  file.path(here, c("../../../common/R", "../runner/R"))
for (d in dirs) for (f in sort(list.files(d, pattern = "[.]R$", full.names = TRUE))) if (!grepl("_tests[.]R$", f)) source(f)
tab_code_dir <- function() if (dir.exists(file.path(code, "runner"))) code else normalizePath(file.path(here, ".."))

# ── the record ───────────────────────────────────────────────────────────────────
failed <- 0L
witness <- function(name, measured, tolerance) {
  ok <- is.finite(measured) && measured <= tolerance
  if (!ok) failed <<- failed + 1L
  cat(sprintf("WITNESS %s %s: measured %.3g (tolerance %.3g)\n", if (ok) "ok" else "failed", name, measured, tolerance))
}
rel <- function(got, want) max(abs(got - want) / pmax(abs(want), 1e-300))

# A published number agrees when it is the measured one to its printed digits (within half a unit of its last
# digit) or within 1e-3 relative, whichever is larger: the articles print three or four digits, and
# Pareto/NBD's s and β lie on a flat ridge (the MATLAB note's own table moves β by 1e-3 with the start).
# A log-likelihood agrees to its printed digits only (relative = 0).
published <- function(name, measured, printed, relative = 1e-3) {
  digits <- nchar(sub("^[^.]*[.]?", "", printed))
  value <- as.numeric(printed)
  tol <- max(0.5 * 10^(-digits) + 1e-12, relative * abs(value))
  ok <- abs(measured - value) <= tol
  if (!ok) failed <<- failed + 1L
  cat(sprintf("WITNESS %s published %s: %s printed, %.6f measured (tolerance %.3g)\n", if (ok) "ok" else "failed", name, printed, measured, tol))
}

# ── CDNOW, as Fader and Hardie summarized it (BTYD's cdnowSummary: 2,357 customers, 39 weeks) ─────────
data(cdnowSummary, package = "BTYD", envir = environment())
cbs <- cdnowSummary$cbs
S <- list(x = cbs[, "x"], tx = cbs[, "t.x"], T = cbs[, "T.cal"], m = ifelse(cbs[, "x"] > 0, cdnowSummary$m.x, NA_real_), n = nrow(cbs))
comp <- clv_compress(S$x, S$tx, S$T)

# The published values (read on 2026-11-20 in the sources named):
#   BG/NBD: Fader, Hardie, Lee (2005), «"Counting your customers" the easy way», Marketing Science 24(2), Table 2,
#     as quoted in Fader & Hardie, «Implementing the BG/NBD model for customer base analysis in Excel»
#     (brucehardie.com/notes/004): LL −9582.4, r 0.243, α 4.414, a 0.793, b 2.426.
#   Pareto/NBD: Fader & Hardie (2005), «A note on implementing the Pareto/NBD model in MATLAB»
#     (brucehardie.com/notes/008), the table of starting values, first row: r 0.5533, α 10.5777, s 0.6062,
#     β 11.6681, LL −9595.0.
#   Gamma–Gamma: Fader & Hardie (2013), «The Gamma-Gamma model of monetary value» (brucehardie.com/notes/025):
#     p 6.25, q 3.74, γ 15.44.
bg <- clv_fit_purchases("bg_nbd", S, comp)
for (k in seq_along(bg$par)) published(paste("BG/NBD, CDNOW,", names(bg$par)[[k]]), bg$par[[k]], c("0.243", "4.414", "0.793", "2.426")[[k]])
published("BG/NBD, CDNOW, log-likelihood", bg$log_lik, "-9582.4", relative = 0)
pn <- clv_fit_purchases("pareto_nbd", S, comp)
for (k in seq_along(pn$par)) published(paste("Pareto/NBD, CDNOW,", names(pn$par)[[k]]), pn$par[[k]], c("0.5533", "10.5777", "0.6062", "11.6681")[[k]])
published("Pareto/NBD, CDNOW, log-likelihood", pn$log_lik, "-9595.0", relative = 0)
gg_ <- clv_fit_spend(S)
for (k in seq_along(gg_$par)) published(paste("Gamma-Gamma, CDNOW,", names(gg_$par)[[k]]), gg_$par[[k]], c("6.25", "3.74", "15.44")[[k]])
cat(sprintf("WITNESS note: corr(x, average spend) over CDNOW's repeaters %.4f (no published value checked: the 2013 note does not print it)\n",
            stats::cor(S$x[S$x > 0], S$m[S$x > 0])))

# ── CLVTools' Pareto/NBD and BG/NBD functions against BTYD's ─────────────────────
pe <- unname(pn$par)
be <- unname(bg$par)
witness("Pareto/NBD P(alive), CLVTools against BTYD", rel(clv_palive("pareto_nbd", pe, S$x, S$tx, S$T), pnbd.PAlive(pe, S$x, S$tx, S$T)), 1e-12)
witness("Pareto/NBD CET(39), CLVTools against BTYD",
        rel(as.vector(clv_cet("pareto_nbd", pe, 39, S$x, S$tx, S$T)), pnbd.ConditionalExpectedTransactions(pe, 39, S$x, S$tx, S$T)), 1e-12)
witness("Pareto/NBD PMF x = 0…10 at t = 39, CLVTools against BTYD",
        max(vapply(0:10, function(k) abs(clv_pmf("pareto_nbd", pe, k, 39) - pnbd.pmf(pe, 39, k)), 0)), 1e-12)
witness("BG/NBD P(alive), CLVTools against BTYD", rel(clv_palive("bg_nbd", be, S$x, S$tx, S$T), bgnbd.PAlive(be, S$x, S$tx, S$T)), 1e-12)
witness("BG/NBD CET(39), CLVTools against BTYD",
        rel(as.vector(clv_cet("bg_nbd", be, 39, S$x, S$tx, S$T)), bgnbd.ConditionalExpectedTransactions(be, 39, S$x, S$tx, S$T)), 1e-12)
witness("BG/NBD PMF x = 0…10 at t = 39, CLVTools against BTYD",
        max(vapply(0:10, function(k) abs(clv_pmf("bg_nbd", be, k, 39) - bgnbd.pmf(be, 39, k)), 0)), 1e-12)

# ── MBG/NBD (written in the runner) against BTYDplus' MBG/CNBD-k with k = 1 ──────
kpar <- function(p) stats::setNames(c(1, p), c("k", "r", "alpha", "a", "b"))
set.seed(3)
worst <- 0
for (i in 1:20) {
  p <- exp(stats::runif(4, -2, 2))
  ours <- clv_mbg_ll_ind(p, S$x, S$tx, S$T)
  theirs <- BTYDplus::mbgcnbd.LL(kpar(p), S$x, S$tx, S$T, litt = 0)
  worst <- max(worst, max(abs(ours - theirs) / pmax(1, abs(theirs))))
}
witness("MBG/NBD log-likelihood, 20 random parameter sets on CDNOW, against BTYDplus", worst, 1e-12)
mb <- clv_fit_purchases("mbg_nbd", S, comp)
mp <- unname(mb$par)
witness("MBG/NBD P(alive) against BTYDplus", rel(clv_palive("mbg_nbd", mp, S$x, S$tx, S$T), BTYDplus::mbgcnbd.PAlive(kpar(mp), S$x, S$tx, S$T)), 1e-12)
witness("MBG/NBD CET(39) against BTYDplus",
        rel(as.vector(clv_cet("mbg_nbd", mp, 39, S$x, S$tx, S$T)), BTYDplus::mbgcnbd.ConditionalExpectedTransactions(kpar(mp), 39, S$x, S$tx, S$T)), 1e-12)
witness("MBG/NBD PMF x = 0…12 at t = 39, against BTYDplus",
        max(vapply(0:12, function(k) abs(clv_pmf("mbg_nbd", mp, k, 39) - BTYDplus::mbgcnbd.pmf(kpar(mp), 39, k)), 0)), 1e-12)
cal <- data.frame(x = S$x, t.x = S$tx, T.cal = S$T, litt = 0)
theirs <- BTYDplus::mbgnbd.EstimateParameters(cal)  # k (1), r, alpha, a, b
witness("MBG/NBD fit on CDNOW, the parameters against BTYDplus' mbgnbd.EstimateParameters", rel(mp, unname(theirs)[-1]), 1e-4)
witness("MBG/NBD fit on CDNOW, the log-likelihood against BTYDplus' at its estimates",
        abs(mb$log_lik - BTYDplus::mbgcnbd.cbs.LL(theirs, cal)), 1e-4)
# the MBG/NBD reference cases of expected.json, against BTYDplus' fit on the same customers
ref <- jsonlite::fromJSON(file.path(here, "expected.json"), simplifyVector = FALSE)
for (case in ref$cases) {
  if (!identical(case$model, "clv") || !is.null(case$refuses)) next
  keys <- vapply(case$expect, function(e) e$key, "")
  for (prefix in c("model|||param_", "candidate|mbg_nbd||param_")) {
    at <- match(paste0(prefix, c("r", "alpha", "a", "b")), keys)
    if (anyNA(at) || (prefix == "model|||param_" && !identical(case$params$purchase_model, "mbg_nbd"))) next
    d <- utils::read.csv(file.path(here, case$data), check.names = FALSE)
    f <- d[[case$params$frequency]]
    fit <- BTYDplus::mbgnbd.EstimateParameters(data.frame(x = f, t.x = d[[case$params$recency]], T.cal = d[[case$params$age]], litt = 0))
    want <- vapply(case$expect[at], function(e) e$value, 0)
    witness(paste0("MBG/NBD reference case «", case$id, "» against BTYDplus"), rel(want, unname(fit)[-1]), 1e-3)
  }
}

# ── Gamma–Gamma against BTYD's spend.EstimateParameters ──────────────────────────
rep_ <- S$x > 0
witness("Gamma-Gamma fit on CDNOW against BTYD", rel(unname(gg_$par), BTYD::spend.EstimateParameters(S$m[rep_], S$x[rep_])), 1e-4)

# ── GSL's ₂F₁ against two witnesses, a grid up to a = 150 and z from 0.1 to 0.995 ─────
# The runner calls ₂F₁ only at 0 ≤ z < 1 (t/(α+T+t) for the expectations; CLVTools' Pareto/NBD chooses its
# transformation so that its z is ≥ 0 too). There the Gauss series has positive terms only: summed term by
# term (Kahan), it has no cancellation and is exact to its rounding (≈ terms × 1e-16), the first witness.
# hypergeo is the second where it is reliable (z ≤ 0.5): near z = 1 with c − a − b far below 0 it returns values
# of the wrong sign (measured: −2.2e21 at a = 60.5, b = 25.2, c = 80.3, z = 0.9, where every term is
# positive), and at z = −0.5 with a large a both it and GSL lose every digit to cancellation (values near
# 1e-16 with opposite signs): a region the runner never reaches. Points where GSL's status is not 0 are
# counted, not compared: the runner then uses the quadrature, which the self-test checks against GSL.
series_2f1 <- function(a, b, c, z) {
  s <- 1
  comp_ <- 0
  t <- 1
  n <- 0
  repeat {
    t <- t * (a + n) * (b + n) / ((c + n) * (n + 1)) * z
    y <- t - comp_
    u <- s + y
    comp_ <- (u - s) - y
    s <- u
    n <- n + 1
    if (!is.finite(s)) return(NA_real_)
    if ((abs(t) < 1e-18 * abs(s) && n > (a + b) * z / (1 - z)) || n > 2e6) break
  }
  s
}
grid <- expand.grid(a = c(0.3, 1.7, 12.5, 60.5, 120.5, 150), b = c(0.8, 3.1, 25.2), c = c(2.2, 9.9, 80.3), z = c(0.1, 0.5, 0.9, 0.99, 0.995))
gsl <- clv_hyp2f1(grid$a, grid$b, grid$c, grid$z)
series <- vapply(seq_len(nrow(grid)), function(i) series_2f1(grid$a[[i]], grid$b[[i]], grid$c[[i]], grid$z[[i]]), 0)
use <- !is.na(gsl) & is.finite(series) & abs(series) < 1e300
witness(paste0("GSL's 2F1 against the positive series on ", sum(use), " points (", sum(is.na(gsl)), " where GSL's status is not 0)"),
        rel(gsl[use], series[use]), 1e-9)
low <- use & grid$z <= 0.5
hg <- vapply(which(low), function(i) Re(hypergeo::hypergeo(grid$a[[i]], grid$b[[i]], grid$c[[i]], grid$z[[i]])), 0)
witness(paste0("GSL's 2F1 against hypergeo on the ", sum(low), " points with z at most 0.5"), rel(gsl[low], hg), 1e-9)

# ── the discrete CLV against CLVTools' DERT (Pareto/NBD, weekly, 10 % a year, 100 years) ──
d <- clv_period_rate(0.1, "week")
H <- round(100 * clv_periods_per_year("week"))
disc <- clv_discounted(function(t) clv_cet("pareto_nbd", pe, t, comp$x, comp$tx, comp$T), H, d, max_steps = H)
dert <- clv_fn("pnbd_nocov_DERT")(pe[[1L]], pe[[2L]], pe[[3L]], pe[[4L]], log(1 + d), comp$x, comp$tx, comp$T)
ratio <- sum(comp$n * disc$discounted) / sum(comp$n * dert)
witness(sprintf("the discounted transactions over 100 years against DERT × e^(−δ/2) (ratio %.6f, e^(−δ/2) %.6f)", ratio, exp(-log(1 + d) / 2)),
        abs(ratio / exp(-log(1 + d) / 2) - 1), 1e-4)

# ── BG/BB (lot B3 adds its block here) ───────────────────────────────────────────

cat(sprintf("WITNESS_REPORT %d failed\n", failed))
quit(save = "no", status = if (failed) 1L else 0L)
