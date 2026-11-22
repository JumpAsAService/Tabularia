# Risk simulation (docs/specs/garch-risk.md, «Part 2 — the node `risk_simulation`»): fit_risk_simulation(task).
#
# Lot G0 gives the node its contract (the catalogue, the fixtures, the image with `rugarch`, the task's memory, disk and start
# time); lots G2 and G3 write the simulation engine (seed blocks, batches from `memory_bytes`, the Parquet writer, the two passes)
# and the node (the ARIMA and GARCH paths, the risk variable, the summary and paths outputs). Until then the node refuses, with a
# sentence, as every node not built yet. It makes no model: `card = NULL` writes no model.json, so nothing can be registered or
# predicted, and there is no `predict` route.

fit_risk_simulation <- function(task) tab_not_built("Risk simulation")
