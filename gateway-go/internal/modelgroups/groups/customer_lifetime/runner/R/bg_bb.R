# BG/BB, the discrete-opportunity model (docs/specs/customer-lifetime-models.md, «The two nodes»): fit_bg_bb(task)
# and predict_bg_bb(task, card). The likelihood, P(alive), the expectation and the PMF are written in clv_shared.R
# (lot B1), with the fit's pieces.
#
# Lot B3 makes this file the node: the checks (assumption rows), the reference cases and the witness lines.
# Until then the node reads and checks the summary (so that a wrong table stops with its sentence) and
# refuses, as every node not built yet.

fit_bg_bb <- function(task) {
  clv_read_summary(task, "bg_bb", task$.frame)
  tab_not_built("BG/BB")
}

predict_bg_bb <- function(task, card) tab_not_built("Predict for a BG/BB model")
