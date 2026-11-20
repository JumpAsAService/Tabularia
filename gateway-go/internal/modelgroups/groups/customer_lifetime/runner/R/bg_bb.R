# BG/BB, the discrete-opportunity model (docs/specs/customer-lifetime-models.md, «The two nodes», «The models»):
# fit_bg_bb(task) and predict_bg_bb(task, card).
#
# As clv.R, the node is the wiring of the shared module (clv_shared.R) and of the checks (clv_checks.R, the same
# ones as clv's): clv_fit(task, "bg_bb") reads the summary (frequency x, recency t_x, opportunities n: whole
# numbers, x <= t_x <= n), fits the four parameters (α, β: the transaction probability's Beta; γ, δ: the dropout's
# Beta) on the log scale from (1, 1, 1, 1) by L-BFGS-B over the distinct (x, t_x, n) with their counts, computes
# the Hessian only at the end, fits Gamma–Gamma when a spend column is given, and evaluates every customer
# (P(alive) at n + 1, the expected transactions in the next opportunities with the limit form at γ = 1, the CLV
# over the horizon counted in opportunities). Nothing here is a threshold: a verdict comes from clv_checks().
# Predict is clv_predict_node(): the card's parameters and the same functions, so that the training rows get the
# fit's numbers bit for bit.

fit_bg_bb <- function(task) {
  frame <- task$.frame
  fit <- clv_fit(task, "bg_bb", frame)
  result <- clv_results(task, fit, frame)
  clv_checks(result$rows, fit)
  result
}

predict_bg_bb <- function(task, card) {
  got <- clv_predict_node(task, card)
  list(output = got$output)
}
