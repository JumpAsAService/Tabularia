# A/B test planning (docs/specs/ab-testing.md, «The node ab_plan»): fit_ab_plan(task).
#
# Lot E0 gives the node its contract; lot E5 writes the baseline, the sizes, the minimum detectable
# effect and the durations here. Until then the node refuses, with a sentence, as every node not built
# yet. It returns no card: `card = NULL` writes no model.json.

fit_ab_plan <- function(task) tab_not_built("A/B test planning")
