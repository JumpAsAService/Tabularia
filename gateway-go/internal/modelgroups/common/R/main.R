# The dispatcher every group's run.R calls: tab_main(group, routes).
#
# `routes` maps a model's id to its functions:
#   list(linear = list(fit = function(task) …, predict = function(task, card) …), …)
# A fit returns list(rows = <tab_rows>, output = <data.frame or NULL>, card = <list or
# NULL>, native = <an R object or NULL>); a predict returns list(output = <data.frame>).
# Before anything is written the rows are checked against the catalogue's declaration for
# the variant: a runner that writes another shape is a bug, and the Job fails (exit 1).
#
# What tab_main writes, in fit mode: TABULARIA_RESULTS (always), TABULARIA_OUTPUT (when the
# task says write_output and the fit gave rows), artifacts/model.json (the card, unless it is
# larger than max_card_bytes),
# artifacts/MLmodel and artifacts/native.rds (when the task says mlflow), artifacts/run.json
# (software, timings, warnings) — in predict mode TABULARIA_OUTPUT and run.json.

tab_main <- function(group, routes) {
  args <- commandArgs(trailingOnly = TRUE)
  code <- tab_code_dir()
  started <- Sys.time()
  phases <- list()
  warnings <- character()
  phase <- function(name, expr) {
    t0 <- Sys.time()
    value <- force(expr)
    phases[[name]] <<- round(as.numeric(difftime(Sys.time(), t0, units = "secs")), 3)
    value
  }
  task <- NULL
  trace <- NULL
  status <- tryCatch(
    withCallingHandlers({
      task <- if ("--selftest" %in% args) list(protocol = TAB_PROTOCOL, mode = "selftest", group = group) else
        tab_read_task(file.path(code, "task.json"))
      if (identical(task$mode, "selftest")) {
        tab_selftest(group, routes, code)
      } else {
        threads <- Sys.getenv("OMP_NUM_THREADS")
        if (nzchar(threads)) Sys.setenv(OPENBLAS_NUM_THREADS = threads)
        tab_seed(task)
        task$variant <- task$variant %||% tab_variant_of(task$model, task$params)
        route <- routes[[task$model %||% ""]]
        if (is.null(route)) {
          tab_refuse("unknown_model", paste0("the group ", group, " has no model «", task$model %||% "", "»"))
        }
        if (identical(task$mode, "fit")) {
          result <- phase("fit", route$fit(task))
          phase("write", tab_finish_fit(task, result))
        } else {
          card <- phase("read_card", jsonlite::fromJSON(tab_read_text(Sys.getenv("TABULARIA_MODEL_IN")), simplifyVector = FALSE))
          result <- phase("predict", route$predict(task, card))
          phase("write", tab_write_output(result$output))
        }
        TAB_EXIT_OK
      }
    },
    error = function(e) if (!inherits(e, "tabularia_refusal")) trace <<- sys.calls(),
    warning = function(w) {
      warnings <<- c(warnings, conditionMessage(w))
      invokeRestart("muffleWarning")
    }),
    tabularia_refusal = function(e) {
      tab_write_error(e)
      TAB_EXIT_REFUSED
    },
    error = function(e) {
      cat("the runner failed: ", conditionMessage(e), "\n", sep = "", file = stderr())
      if (length(trace)) cat(paste("  at", vapply(tail(trace, 10L), function(x) deparse(x, nlines = 1L)[[1L]], "")), sep = "\n", file = stderr())
      1L
    })
  if (!is.null(task) && !identical(task$mode, "selftest")) {
    run <- list(protocol = TAB_PROTOCOL, group = group, model = task$model %||% NULL, mode = task$mode, exit = status,
                software = as.list(tab_versions()), seconds = round(as.numeric(difftime(Sys.time(), started, units = "secs")), 3),
                phases = tab_object(phases), warnings = as.list(unique(head(warnings, 50L))))
    tryCatch(tab_write_text(tab_artifact("run.json"), tab_json(run)),
             error = function(e) cat("run.json could not be written: ", conditionMessage(e), "\n", sep = "", file = stderr()))
  }
  quit(save = "no", status = status)
}

# Writes what a fit gave, after checking its rows.
# A card larger than max_card_bytes does not stop the step (tab_card_fits): its results, rows
# and report are written, with a note; model.json, MLmodel and native.rds are not.
tab_finish_fit <- function(task, result) {
  card <- if (is.null(result$card)) NULL else tab_card_fits(task, result$card, result$rows)
  problems <- tab_check_declared(result$rows, task$model, task$variant)
  if (length(problems)) stop("the results do not match the catalogue:\n  ", paste(head(problems, 20L), collapse = "\n  "))
  tab_write_results(result$rows)
  if (isTRUE(task$write_output) && !is.null(result$output)) tab_write_output(result$output)
  if (!is.null(card)) {
    tab_write_text(tab_artifact("model.json"), card)
    if (isTRUE(task$mlflow)) {
      tab_write_text(tab_artifact("MLmodel"), tab_mlmodel(result$card))
      if (!is.null(result$native)) {
        file <- tempfile(fileext = ".rds")
        saveRDS(tab_native_lean(result$native), file)
        tab_copy_file(file, tab_artifact("native.rds"))
      }
    }
  }
  invisible(TRUE)
}

tab_write_output <- function(output, path = Sys.getenv("TABULARIA_OUTPUT")) {
  if (is.null(output)) return(invisible(FALSE))
  if (!nzchar(path)) stop("TABULARIA_OUTPUT is empty")
  arrow::write_parquet(output, path)
  invisible(TRUE)
}

tab_copy_file <- function(from, to) {
  if (!nzchar(to)) return(invisible(FALSE))
  if (!tab_is_uri(to)) {
    dir.create(dirname(to), recursive = TRUE, showWarnings = FALSE)
    return(invisible(file.copy(from, to, overwrite = TRUE)))
  }
  bytes <- readBin(from, "raw", file.info(from)$size)
  fs <- arrow::FileSystem$from_uri(to)
  stream <- fs$fs$OpenOutputStream(fs$path)
  on.exit(stream$close())
  stream$write(bytes)
  invisible(TRUE)
}

# Whether a regression's task asks for a penalty (routes send it to penalized.R).
tab_penalized <- function(task) !identical(task$params$penalty %||% "none", "none")

# Whether an ARIMA's task asks for automatic orders (routes send it to arima_auto.R).
tab_auto_orders <- function(task) identical(task$params$orders %||% "manual", "auto")

# The variant a model's parameters make, as the catalogue says (the gateway writes it in
# task.json; this is for a task written by hand, and for the self-test's reference cases).
# A rule of `when` is read as the engine's `_holds` and the gateway's `holds` read it: `true`
# is «given» (a column's name, any value but null or an empty list), `false` «not given», a
# list one of its values, anything else that value.
tab_variant_of <- function(model, params) {
  spec <- tab_model_spec(model)
  for (v in spec$variants) {
    ok <- TRUE
    for (k in names(v$when)) {
      if (!tab_holds(v$when[[k]], params[[k]] %||% spec$defaults[[k]])) ok <- FALSE
    }
    if (ok) return(v$id)
  }
  NULL
}

tab_given <- function(value) !is.null(value) && length(value) > 0L

tab_holds <- function(rule, value) {
  if (is.logical(rule) && length(rule) == 1L && !is.na(rule)) return(if (rule) tab_given(value) else !tab_given(value))
  if (is.list(rule)) return(any(vapply(rule, identical, logical(1), value)))
  identical(rule, value)
}

# The step's input: the frame a self-test hands over, or the Parquet file of TABULARIA_INPUT.
# A runner reads its input through this, never TABULARIA_INPUT itself.
tab_input <- function(task, columns = NULL) {
  if (!is.null(task$.frame)) return(task$.frame)
  tab_read_input(columns = columns)
}
