# A/B test (docs/specs/ab-testing.md, «The node ab_test»): fit_ab_test(task).
#
# Lot E0 gives the node its contract (the catalogue, the recipe, the route in run.R); the lots after it
# write the data module (E1, ab_data.R), the comparisons (E2, ab_compare.R and here), the Bayesian and
# the sequential readings (E3, E4) and the secondary metrics (E8). Until then the node refuses, with a
# sentence, as every node not built yet. It returns no card: `card = NULL` writes no model.json.

fit_ab_test <- function(task) tab_not_built("A/B test")
