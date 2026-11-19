# The model card (card.schema.json): what applying the model to new rows needs, as data.
# A runner starts from tab_card(task, design) and adds what its model needs (sigma and
# df_residual, the penalty, Cox's centring and baseline, the Kaplan–Meier table, ARIMA's
# orders and last observations); tab_write_card checks the size and writes model.json, and
# MLmodel when the node logs to MLflow. Tabularia never loads native.rds: it is for people.

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
    tab_refuse("card_too_large", paste0("the model's card would be ", size, " bytes (at most ", tab_limit(task, "max_card_bytes"),
                                        "): fewer terms or strata"), list(bytes = size))
  }
  tab_write_text(path, text)
  invisible(text)
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
