# Kaplan–Meier: strata, log-rank, pairs, medians, restricted means (docs/specs/statistical-models.md, lane M4).
# A stub until that lane builds it:
# the fit and the prediction refuse with «not built yet», so a flow that holds the node fails
# with a sentence and the group's self-test still runs.
#
# A fit reads its input with tab_input(task), builds its design with tab_design (never a
# formula from a column's name), starts its rows with tab_rows_start(task) and returns
# list(rows, output, card, native); see common/R/main.R.

fit_kaplan_meier <- function(task) tab_not_built("the Kaplan–Meier estimate")

predict_kaplan_meier <- function(task, card) tab_not_built("the prediction of a Kaplan–Meier estimate")
