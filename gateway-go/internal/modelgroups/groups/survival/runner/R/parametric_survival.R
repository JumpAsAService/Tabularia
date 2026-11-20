# Parametric survival (docs/specs/statistical-models.md, «The node «Parametric survival»» and
# «The parametric survival lots»): fit_parametric_survival(task) and predict_parametric_survival(task, card).
#
# Lot P0 gives the node its contract (the catalogue, the card's schema, the recipe's packages,
# the route in run.R); lot P1 writes the fit, the card, the rows and Predict here, P2 the checks
# (parametric_survival_checks.R) and P3 the automatic choice (parametric_survival_auto.R). Until
# then the node refuses, with a sentence, as every node not built yet.

fit_parametric_survival <- function(task) tab_not_built("Parametric survival")

predict_parametric_survival <- function(task, card) tab_not_built("Predict for a parametric survival model")
