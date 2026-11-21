# Decomposition (docs/specs/ts-decomposition.md, «The node `decomposition`»): fit_decomposition(task).
#
# Lot D0 gives the node its contract (the catalogue, the fixtures, the image with Rlibeemd); lots D1 to D3 write the
# series, the checks and the four methods (STL, classical, MSTL, EMD) here and in their own files. Until then the node
# refuses, with a sentence, as every node not built yet. It returns no card: `card = NULL` writes no model.json, so
# nothing can be registered or predicted. Nothing here is random.

fit_decomposition <- function(task) tab_not_built("Decomposition")
