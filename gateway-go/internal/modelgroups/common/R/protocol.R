# The runner protocol (docs/specs/statistical-models.md, «The runner protocol»), shared by
# every R runner. A model Job runs `Rscript runner/run.R` in /tabularia/code, which holds:
#
#   runner/run.R, runner/R/<model>.R   the group's runner (the dispatcher and one file per model)
#   common/R/*.R                       this library
#   catalog.json                       the catalogue (the gateway's copy)
#   card.schema.json                   the model card's schema
#   task.json                          what to do (below)
#   reference/                         the group's reference cases (self-test only)
#
# task.json: {"protocol": 1, "mode": "fit" | "predict" | "selftest", "group", "group_version",
#   "model", "variant", "params" (checked by the gateway, every key that applies),
#   "output" ("results" | "rows" | "table" | "forecast"), "write_output" (the node has nodes
#   after it), "rows_from" ("trial" | "run"), "mlflow" (write native.rds), "limits" (the
#   catalogue's, MODELS__MAX_CHART_POINTS applied), "input_rows", "ids" {flow_id, node_id,
#   run_id, step_run_id}, "recipe_hash", "runner_hash"}.
#
# Environment: TABULARIA_INPUT (the input, Parquet), TABULARIA_OUTPUT (rows or forecast,
# Parquet), TABULARIA_RESULTS (the ten columns, Parquet), TABULARIA_ARTIFACTS (a prefix:
# model.json, native.rds, run.json, error.json), TABULARIA_MODEL_IN (Predict: the card), all
# s3:// addresses on a cluster (arrow reads the AWS_* variables), paths on a laptop.
#
# Exit codes: 0 done; 3 refused (artifacts/error.json says why, and the log's line
# «TABULARIA_ERROR {json}» says the same when the bucket cannot be written); 4 a self-test
# that failed; anything else is a crash of the runner (a bug), the log's tail says what.
#
# No user text ever becomes code here: a column's name is data, renamed to v1, v2… before
# any formula or design is built (columns.R), and mapped back when results are written.

TAB_PROTOCOL <- 1L
TAB_EXIT_OK <- 0L
TAB_EXIT_REFUSED <- 3L
TAB_EXIT_SELFTEST_FAILED <- 4L
TAB_MODES <- c("fit", "predict", "selftest")

`%||%` <- function(a, b) if (is.null(a) || length(a) == 0L) b else a

# ── refusals: a sentence for the person, never a crash ────────────────────────
# A refusal is a condition of class tabularia_refusal: code (a short id), message (an
# English sentence that names the way out), params (what the sentence names, as data).
tab_refuse <- function(code, message, params = list()) {
  stop(structure(class = c("tabularia_refusal", "error", "condition"),
                 list(message = message, call = NULL, code = code, params = params)))
}

tab_not_built <- function(what) {
  tab_refuse("not_built", paste0(what, " is not built yet in this version of Tabularia"), list(what = what))
}

# Numbers keep 17 significant digits (I(17): significant, not decimal, digits), so that a double
# read back is the same double to the last bit: a card's coefficients give the fit's own
# predictions within 1e-9 (jsonlite's digits = NA keeps 15, and loses the last bits).
tab_json <- function(x, pretty = FALSE) {
  as.character(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = I(17), pretty = pretty))
}

tab_is_uri <- function(path) grepl("^[a-z][a-z0-9+.-]*://", path)

# Writes a text to a path or an s3:// address (arrow's file systems).
tab_write_text <- function(path, text) {
  if (!nzchar(path)) return(invisible(FALSE))
  bytes <- charToRaw(enc2utf8(text))
  if (tab_is_uri(path)) {
    fs <- arrow::FileSystem$from_uri(path)
    stream <- fs$fs$OpenOutputStream(fs$path)
    on.exit(stream$close())
    stream$write(bytes)
  } else {
    dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
    writeBin(bytes, path)
  }
  invisible(TRUE)
}

tab_read_text <- function(path) {
  if (tab_is_uri(path)) {
    fs <- arrow::FileSystem$from_uri(path)
    stream <- fs$fs$OpenInputStream(fs$path)
    on.exit(stream$close())
    return(rawToChar(stream$Read()$data()))
  }
  paste(readLines(path, encoding = "UTF-8", warn = FALSE), collapse = "\n")
}

# The artifacts' prefix and a file under it.
tab_artifact <- function(name) {
  prefix <- Sys.getenv("TABULARIA_ARTIFACTS")
  if (!nzchar(prefix)) return("")
  paste0(sub("/*$", "/", prefix), name)
}

# error.json and the log's line: what the step fails with.
tab_write_error <- function(e) {
  body <- list(code = e$code %||% "refused", message = conditionMessage(e), params = e$params %||% list())
  text <- tab_json(body)
  cat("TABULARIA_ERROR ", text, "\n", sep = "")
  tryCatch(tab_write_text(tab_artifact("error.json"), text),
           error = function(w) cat("error.json could not be written: ", conditionMessage(w), "\n", sep = "", file = stderr()))
  invisible(body)
}

# ── task.json ─────────────────────────────────────────────────────────────────
tab_code_dir <- function() {
  file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  if (length(file) == 1L && nzchar(file)) return(dirname(dirname(normalizePath(file))))
  normalizePath(".")
}

tab_read_task <- function(path) {
  if (!file.exists(path)) tab_refuse("no_task", "task.json is missing: the Job was not made by Tabularia")
  task <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  if (!identical(as.integer(task$protocol %||% 0L), TAB_PROTOCOL)) {
    tab_refuse("protocol", paste0("task.json speaks protocol ", task$protocol %||% "none", ", this runner ", TAB_PROTOCOL,
                                  ": the image and Tabularia's version differ"))
  }
  if (!(task$mode %||% "") %in% TAB_MODES) tab_refuse("mode", paste0("unknown mode «", task$mode %||% "", "»"))
  task$params <- task$params %||% list()
  task$limits <- task$limits %||% tab_catalog()$limits
  task
}

# The catalogue (catalog.json beside the runner), read once.
tab_catalog <- local({
  cache <- NULL
  function(code = tab_code_dir()) {
    if (is.null(cache)) cache <<- jsonlite::fromJSON(file.path(code, "catalog.json"), simplifyVector = FALSE)
    cache
  }
})

tab_model_spec <- function(model) {
  for (m in tab_catalog()$models) if (identical(m$id, model)) return(m)
  NULL
}

tab_variant_spec <- function(model, variant) {
  spec <- tab_model_spec(model)
  for (v in spec$variants) if (identical(v$id, variant)) return(v)
  NULL
}

tab_limit <- function(task, name) as.numeric(task$limits[[name]] %||% tab_catalog()$limits[[name]])

# A seed for every random choice: the node's, the same for the same input.
tab_seed <- function(task) set.seed(as.integer(task$params$seed %||% 1L), kind = "Mersenne-Twister",
                                    normal.kind = "Inversion", sample.kind = "Rejection")
