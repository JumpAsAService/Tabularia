# Customer lifetime value (docs/specs/customer-lifetime-models.md, «The two nodes», «The models»):
# fit_clv(task) and predict_clv(task, card). Pareto/NBD and BG/NBD through the CLVTools functions
# bound in clv_shared.R, MBG/NBD written there, `auto` and `compare`, GSL's status and the
# Gauss–Jacobi fallback.
#
# Lot B0 gave the node its contract (the catalogue, the card's schema, the recipe, the route in run.R);
# lot B1 wrote the shared module (clv_shared.R: the summary's checks, the models, the optimizer, the CLV, the
# rows, the card, the Predict core, and their self-tests). Lot B2 makes this file the node: the checks
# (assumption rows), the reference cases and the witness. Until then the node reads and checks the summary
# (so that a wrong table stops with its sentence) and refuses, as every node not built yet.

fit_clv <- function(task) {
  clv_read_summary(task, "clv", task$.frame)
  tab_not_built("Customer lifetime value")
}

predict_clv <- function(task, card) tab_not_built("Predict for a customer lifetime value model")
