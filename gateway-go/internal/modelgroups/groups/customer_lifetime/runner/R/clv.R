# Customer lifetime value (docs/specs/customer-lifetime-models.md, «The two nodes», «The models»):
# fit_clv(task) and predict_clv(task, card).
#
# The node is the wiring of the shared module (clv_shared.R, lot B1) and of the checks (clv_checks.R):
#   - clv_fit() reads and checks the summary, fits the purchase models the node asks for (Pareto/NBD and
#     BG/NBD through CLVTools' compiled functions with CLVTools' own optimizer settings, MBG/NBD written in
#     the module), keeps the one asked for or, with `auto`, the highest log-likelihood among those that
#     converged, computes its Hessian only, fits Gamma–Gamma when a spend column is given, and evaluates
#     every customer (GSL's ₂F₁, the Gauss–Jacobi quadrature only where GSL fails);
#   - clv_results() writes the rows of every section but `assumption`, the output rows and the card;
#   - clv_checks() writes the section `assumption`: verdicts from p-values against the node's alpha and
#     from certain facts, the rest measures without a verdict.
# Predict is clv_predict_node(): the card's parameters and the same functions as the fit, so that the
# training rows get the fit's numbers bit for bit; a row that breaks a rule of the summary gets NULL.

fit_clv <- function(task) {
  frame <- task$.frame
  fit <- clv_fit(task, "clv", frame)
  result <- clv_results(task, fit, frame)
  clv_checks(result$rows, fit)
  result
}

predict_clv <- function(task, card) {
  got <- clv_predict_node(task, card)
  list(output = got$output)
}
