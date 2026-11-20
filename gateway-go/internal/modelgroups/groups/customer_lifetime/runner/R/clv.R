# Customer lifetime value (docs/specs/customer-lifetime-models.md, «The two nodes», «The models»):
# fit_clv(task) and predict_clv(task, card). Pareto/NBD and BG/NBD through the CLVTools functions
# bound in clv_shared.R, MBG/NBD written here, `auto` and `compare`, GSL's status and the
# Gauss–Jacobi fallback.
#
# Lot B0 gives the node its contract (the catalogue, the card's schema, the recipe, the route in
# run.R); lot B1 writes the shared module (clv_shared.R), lot B2 the fit, the card, the rows and
# Predict here. Until then the node refuses, with a sentence, as every node not built yet.

fit_clv <- function(task) tab_not_built("Customer lifetime value")

predict_clv <- function(task, card) tab_not_built("Predict for a customer lifetime value model")
