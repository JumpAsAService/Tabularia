# Decomposition (docs/specs/ts-decomposition.md, «The node `decomposition`»): fit_decomposition(task).
#
# Lot D0 gives the node its contract (the catalogue, the fixtures, the image with Rlibeemd); lots D1 to D3 write the
# series, the checks and the four methods (STL, classical, MSTL, EMD) here and in their own files. Until then the node
# refuses, with a sentence, as every node not built yet. It returns no card: `card = NULL` writes no model.json, so
# nothing can be registered or predicted. Nothing here is random.

# EMD (lot D3) is built: decomposition_emd.R. The other methods refuse until lot D2 replaces this dispatch with its own
# (which calls fit_decomposition_emd(task, o) the same way).
fit_decomposition <- function(task) {
  if (identical(task$params$method, "emd")) return(fit_decomposition_emd(task))
  tab_not_built("Decomposition")
}
