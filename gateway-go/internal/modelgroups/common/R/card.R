# The model card (card.schema.json): what applying the model to new rows needs, as data.
# A runner starts from tab_card(task, design) and adds what its model needs (sigma and
# df_residual, the penalty, Cox's centring and baseline, the Kaplan–Meier table, ARIMA's
# orders and last observations); tab_finish_fit (main.R) writes model.json, and MLmodel and
# native.rds when the node logs to MLflow, unless the card is larger than max_card_bytes
# (tab_card_fits: a note, and no model files). Tabularia never loads native.rds: it is for people.

TAB_CARD_FORMAT <- "tabularia-model"
TAB_CARD_VERSION <- 1L

# «R 4.6.1; stats 4.6.1; sandwich 3.1-3»: R and the packages a model used.
tab_versions <- function(packages = character()) {
  v <- c(R = paste(R.version$major, R.version$minor, sep = "."))
  for (p in unique(c("stats", packages))) v[[p]] <- as.character(utils::packageVersion(p))
  v
}

tab_software <- function(packages = character()) {
  v <- tab_versions(packages)
  paste(paste(names(v), v), collapse = "; ")
}

tab_card <- function(task, design = NULL, packages = character()) {
  card <- list(format = TAB_CARD_FORMAT, format_version = TAB_CARD_VERSION, model = task$model, variant = task$variant,
               group = task$group, group_version = task$group_version %||% "dev",
               recipe_hash = task$recipe_hash %||% "", runner_hash = task$runner_hash %||% "",
               software = as.list(tab_versions(packages)),
               created_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
               params = tab_object(task$params), inputs = list())
  if (!is.null(design)) {
    card$formula <- design$formula
    card$inputs <- design$inputs
    card$terms <- lapply(seq_len(nrow(design$terms)), function(i) {
      list(term = design$terms$term[[i]], level = if (is.na(design$terms$level[[i]])) NULL else design$terms$level[[i]],
           variables = as.list(strsplit(design$terms$variables[[i]], "\u001f", fixed = TRUE)[[1L]]))
    })
  }
  card
}

# The covariance kept in the card: up to max_vcov_terms terms, else none.
tab_card_vcov <- function(task, vcov) {
  if (is.null(vcov) || nrow(vcov) > tab_limit(task, "max_vcov_terms")) return(NULL)
  lapply(seq_len(nrow(vcov)), function(i) as.list(tab_finite(vcov[i, ])))
}

tab_card_text <- function(card) tab_json(card)

# A list written as a JSON object even when empty.
tab_object <- function(x) if (length(x)) x else structure(list(), names = character())

tab_write_card <- function(task, card, path = tab_artifact("model.json")) {
  text <- tab_card_text(card)
  size <- nchar(text, type = "bytes")
  if (size > tab_limit(task, "max_card_bytes")) {
    tab_refuse("card_too_large", paste0("the model's card would be ", size, " bytes (at most ", tab_limit(task, "max_card_bytes"), ")"),
               list(bytes = size))
  }
  tab_write_text(path, text)
  invisible(text)
}

# The note of a fit whose card is too large. A card too large never stops the fit: the step
# gives its results, its rows and its report; only the model is not logged to MLflow (no
# model.json, MLmodel or native.rds), so no Predict node can apply it. The sentence has no
# number or name in it: report_texts/common.json says it in the page's language.
TAB_NOTE_CARD_TOO_LARGE <- paste0(
  "The model card would be larger than a card can be (1 MiB): the results and the report are complete, but the model ",
  "is not logged to MLflow and no Predict node can apply it.")

# The card's text when it is not larger than max_card_bytes; else NULL, with the note
# note_card_too_large in the rows and a warning for run.json.
tab_card_fits <- function(task, card, rows) {
  text <- tab_card_text(card)
  size <- nchar(text, type = "bytes")
  most <- tab_limit(task, "max_card_bytes")
  if (size <= most) return(text)
  tab_text(rows, "model", "note_card_too_large", TAB_NOTE_CARD_TOO_LARGE)
  warning("the model card would be ", size, " bytes (at most ", most, "): it is not written, and the model is not logged to MLflow")
  NULL
}

# native.rds: R's own object of the fit, for people who open it in R, without the input's rows.
# A fit object carries its rows several ways: the model frame (`model`), the design and the
# outcome (`x`, `y`), one value per row (residuals, fitted values, linear predictors, weights,
# effects, a Cox model's stratum of each row, the cross-validation's out-of-fold predictions
# and folds), the data itself (glm's `data`), the QR decomposition of the design (`qr$qr`, from
# which the design comes back), and the environment of its formula, which saveRDS writes whole
# (the runner's frame, with the input in it). All of that is removed or cut: the QR keeps its R
# (the first `rank` rows), what predict's standard errors read; the formula's environment
# becomes the global one (not written). What stays answers predict(fit, newdata) as the fit
# itself does (lm, glm, coxph's linear predictor and risk with reference = "sample",
# cv.glmnet's predict(newx, s)); what needs the rows (summary, residuals, survfit of a Cox
# model) does not work on it: the report and coefficients.csv have those numbers.
# Kept whole: a survfit (Kaplan–Meier: the estimate is the curve, per event time) and an ARIMA
# (forecast::Arima keeps the series, which forecast() needs to go on from its last values).
# A flexsurvreg (parametric survival) keeps its rows in `data` (the outcome Y, the model frame
# m, the design matrices mml) and in `logliki` (each row's log-likelihood): Y and logliki are
# removed, the frame and the matrices emptied (their columns and attributes kept), and
# summary(fit, newdata = …) still answers as the fit does.
TAB_NATIVE_ROWS <- c("model", "x", "y", "residuals", "fitted.values", "effects", "linear.predictors", "weights", "prior.weights",
                     "data", "offset", "na.action", "strata", "fit.preval", "foldid")

tab_native_lean <- function(fit) {
  if (inherits(fit, "flexsurvreg")) return(tab_native_lean_flexsurv(fit))
  if (is.null(fit) || !inherits(fit, c("lm", "coxph", "cv.glmnet"))) return(fit)
  for (k in intersect(TAB_NATIVE_ROWS, names(fit))) fit[[k]] <- NULL
  if (is.list(fit$qr) && is.matrix(fit$qr$qr)) fit$qr$qr <- fit$qr$qr[seq_len(fit$rank), , drop = FALSE]
  for (k in c("terms", "formula")) {
    if (!is.null(fit[[k]]) && !is.null(attr(fit[[k]], ".Environment"))) attr(fit[[k]], ".Environment") <- globalenv()
  }
  fit
}

tab_native_lean_flexsurv <- function(fit) {
  fit$data$Y <- NULL
  if (is.data.frame(fit$data$m)) fit$data$m <- fit$data$m[0L, , drop = FALSE]
  fit$data$mml <- lapply(fit$data$mml, function(m) if (is.matrix(m)) m[0L, , drop = FALSE] else m)
  fit$logliki <- NULL
  fit
}

# What a lean object still holds of the rows: the names of its elements with one value per row
# (for the self-tests: none).
tab_native_rows_left <- function(fit, n) {
  names(fit)[vapply(fit, function(x) is.atomic(x) && NROW(x) == n || is.data.frame(x), logical(1))]
}

# MLflow's MLmodel, written by hand (no mlflow package in the images): a `tabularia` flavor
# naming model.json, and the inputs' signature in MLflow's format.
tab_mlmodel <- function(card, run_id = "") {
  type_of <- c(number = "double", category = "string", time = "datetime")
  signature <- tab_json(lapply(card$inputs, function(i) list(name = i$name, type = unname(type_of[[i$type]]))))
  paste0(
    "artifact_path: model\n",
    "flavors:\n",
    "  tabularia:\n",
    "    card: model.json\n",
    "    format: ", TAB_CARD_FORMAT, "\n",
    "    format_version: ", TAB_CARD_VERSION, "\n",
    "    model: ", card$model, "\n",
    "    group: ", card$group, "\n",
    if (nzchar(run_id)) paste0("run_id: ", run_id, "\n") else "",
    "signature:\n",
    "  inputs: '", gsub("'", "''", signature, fixed = TRUE), "'\n",
    "utc_time_created: '", sub("T", " ", sub("Z$", "", card$created_at)), "'\n")
}
