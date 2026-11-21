# The results: the statistics' ten columns (section, level, level_2, stat, value, text, x0,
# x1, ord, key), one row per number or text, in the order written. A runner adds rows with
# tab_number, tab_text, tab_p (a p-value and R's stars after it) and tab_points (the points
# of a chart); tab_results_frame gives the data.frame and tab_write_results the Parquet file
# with the exact types (string, double, int64). The engine checks the file
# (app/engine/models/results.py) against the catalogue's declaration for the variant;
# tab_check_declared runs the same check here, so a runner's own tests see it first.

TAB_COLUMNS <- c("section", "level", "level_2", "stat", "value", "text", "x0", "x1", "ord", "key")

tab_rows <- function() {
  e <- new.env(parent = emptyenv())
  e$n <- 0L
  e$chunks <- list()
  e
}

# A number as the table holds it: finite or NA; −0 is 0; below the smallest normal double, 0
# (the engine's rule: some SQL parsers refuse a subnormal literal).
tab_finite <- function(v) {
  v <- suppressWarnings(as.numeric(v))
  v[!is.finite(v)] <- NA_real_
  v[!is.na(v) & abs(v) < .Machine$double.xmin] <- 0
  v + 0
}

.tab_part <- function(x, n) {
  if (is.null(x) || length(x) == 0L) return(rep(NA_character_, n))
  x <- enc2utf8(as.character(x))
  if (length(x) == 1L && n > 1L) rep(x, n) else x
}

# Adds rows (vectors of equal length, or one value repeated). `index`: the points of a chart,
# whose key ends with «|index».
tab_add <- function(rows, section, stat, value = NA_real_, text = NA_character_, level = NULL, level_2 = NULL,
                    x0 = NA_real_, x1 = NA_real_, index = NULL) {
  n <- max(length(stat), length(value), length(text), length(level), length(level_2), length(x0), length(x1), length(index))
  if (n == 0L) return(invisible(rows))
  rep_n <- function(x) if (length(x) == n) x else rep(x, length.out = n)
  section <- rep_n(.tab_part(section, 1L))
  level <- .tab_part(level, n)
  level_2 <- .tab_part(level_2, n)
  stat <- rep_n(.tab_part(stat, 1L))
  key <- paste(section, ifelse(is.na(level), "", level), ifelse(is.na(level_2), "", level_2), stat, sep = "|")
  if (!is.null(index)) key <- paste0(key, "|", format(as.integer(index), scientific = FALSE, trim = TRUE))
  rows$chunks[[length(rows$chunks) + 1L]] <- list(
    section = section, level = level, level_2 = level_2, stat = stat, value = rep_n(tab_finite(value)),
    text = rep_n(.tab_part(text, 1L)), x0 = rep_n(tab_finite(x0)), x1 = rep_n(tab_finite(x1)), key = key)
  rows$n <- rows$n + n
  invisible(rows)
}

tab_number <- function(rows, section, stat, value, level = NULL, level_2 = NULL) {
  tab_add(rows, section, stat, value = value, level = level, level_2 = level_2)
}

tab_text <- function(rows, section, stat, text, level = NULL, level_2 = NULL) {
  if (length(text) == 0L || is.na(text[[1L]])) return(invisible(rows))
  tab_add(rows, section, stat, text = text, level = level, level_2 = level_2)
}

# R's significance code of a p-value (summary.lm's stars); NA for no p-value.
tab_significance <- function(p) {
  ifelse(is.na(p) | !is.finite(p), NA_character_,
         ifelse(p < 0.001, "***", ifelse(p < 0.01, "**", ifelse(p < 0.05, "*", ifelse(p < 0.1, ".", "")))))
}

# A p-value and, after it, its significance: `stat` = "p_value" → "significance",
# "lr_p_value" → "lr_significance".
# With vectors (one p-value per term), each p-value is followed by its own code.
tab_p <- function(rows, section, value, stat = "p_value", level = NULL, level_2 = NULL) {
  at <- function(x, i) if (length(x) > 1L) x[[i]] else x
  for (i in seq_along(value)) {
    lv <- at(level, i)
    lv2 <- at(level_2, i)
    if (!is.null(lv2) && is.na(lv2)) lv2 <- NULL
    tab_number(rows, section, stat, value[[i]], lv, lv2)
    code <- tab_significance(tab_finite(value[[i]]))
    if (!is.na(code)) tab_text(rows, section, sub("p_value$", "significance", stat), code, lv, lv2)
  }
  invisible(rows)
}

# The points of a chart: one row each, `x` in x0, `y` in value, an optional label in text
# (a row number), `x1` for a histogram's right edges. The index counts from 0 per series.
tab_points <- function(rows, section, level, stat, x, y, level_2 = NULL, labels = NULL, x1 = NULL) {
  n <- length(y)
  if (n == 0L) return(invisible(rows))
  tab_add(rows, section, stat, value = y, text = if (is.null(labels)) NA_character_ else labels,
          level = level, level_2 = level_2, x0 = x, x1 = if (is.null(x1)) NA_real_ else x1, index = seq_len(n) - 1L)
}

# The rows every model starts with: its id, its variant, where the rows came from, the
# group's version.
tab_rows_start <- function(task) {
  rows <- tab_rows()
  tab_text(rows, "model", "model", task$model)
  tab_text(rows, "model", "variant", task$variant)
  tab_text(rows, "model", "rows_from", task$rows_from %||% "run")
  tab_text(rows, "model", "group_version", as.character(task$group_version %||% "dev"))
  rows
}

# Notes: note_1, note_2… in the order added.
tab_note <- function(rows, sentence) {
  rows$notes <- (rows$notes %||% 0L) + 1L
  tab_text(rows, "model", paste0("note_", rows$notes), sentence)
}

tab_results_frame <- function(rows) {
  chunks <- rows$chunks
  col <- function(name, empty) if (length(chunks)) unlist(lapply(chunks, `[[`, name), use.names = FALSE) else empty
  df <- data.frame(section = col("section", character()), level = col("level", character()),
                   level_2 = col("level_2", character()), stat = col("stat", character()),
                   value = as.numeric(col("value", numeric())), text = col("text", character()),
                   x0 = as.numeric(col("x0", numeric())), x1 = as.numeric(col("x1", numeric())),
                   stringsAsFactors = FALSE)
  df$ord <- seq_len(nrow(df)) - 1L
  df$key <- col("key", character())
  df[, TAB_COLUMNS]
}

tab_results_schema <- function() {
  arrow::schema(section = arrow::utf8(), level = arrow::utf8(), level_2 = arrow::utf8(), stat = arrow::utf8(),
                value = arrow::float64(), text = arrow::utf8(), x0 = arrow::float64(), x1 = arrow::float64(),
                ord = arrow::int64(), key = arrow::utf8())
}

tab_write_results <- function(rows, path = Sys.getenv("TABULARIA_RESULTS")) {
  df <- tab_results_frame(rows)
  if (!nzchar(path)) stop("TABULARIA_RESULTS is empty")
  arrow::write_parquet(arrow::Table$create(df, schema = tab_results_schema()), path)
  invisible(nrow(df))
}

# ── the catalogue's declaration, checked here as the engine checks it ──────────
# Whether each of `stat` is one of `names` (a name ending in «*» takes any stat it begins).
.tab_declared <- function(stat, names) {
  names <- unlist(names)
  if (!length(names)) return(rep(FALSE, length(stat)))
  out <- stat %in% names
  wild <- names[endsWith(names, "*")]
  for (prefix in substr(wild, 1L, nchar(wild) - 1L)) out <- out | startsWith(stat, prefix)
  out
}

# The most rows a model's results may hold: the engine refuses more (app/engine/models/results.py).
tab_max_result_rows <- function() as.numeric(tab_catalog()$limits$max_result_rows)

# What is wrong with `rows` for the model's variant, one sentence each (empty: nothing), in the
# order of the rows. The rules are applied to each section's rows at once (a row at a time took
# 18 s on 110,000 rows).
tab_check_declared <- function(rows, model, variant) {
  df <- if (is.data.frame(rows)) rows else tab_results_frame(rows)
  cat_ <- tab_catalog()
  spec <- tab_variant_spec(model, variant)
  if (is.null(spec)) return(paste0("the catalogue has no variant ", variant, " of ", model))
  problems <- character()
  most <- tab_max_result_rows()
  if (nrow(df) > most) problems <- c(problems, paste0(nrow(df), " rows, more than the ", format(most, scientific = FALSE), " a model's results may hold"))
  if (anyDuplicated(df$key)) problems <- c(problems, paste0("the key ", df$key[anyDuplicated(df$key)], " is there twice"))
  # one column per rule, in the order a row's problems are said; NA where the rule holds
  rules <- c("section", "level_null", "level_required", "level_2_null", "level_2_required", "levels", "number_text",
             "text_value", "verdict", "stat")
  said <- matrix(NA_character_, nrow(df), length(rules), dimnames = list(NULL, rules))
  where <- paste0("the row ", df$key)
  verdicts <- unlist(cat_$verdicts)
  for (section in unique(df$section)) {
    i <- which(df$section == section)
    shape <- cat_$section_shapes[[section]]
    decl <- spec$sections[[section]]
    if (is.null(shape) || is.null(decl)) {
      said[i, "section"] <- paste0(where[i], ": the variant ", variant, " of ", model, " has no section ", section)
      next
    }
    for (part in c("level", "level_2")) {
      v <- df[[part]][i]
      if (identical(shape[[part]], "null")) {
        bad <- !is.na(v)
        said[i[bad], paste0(part, "_null")] <- paste0(where[i[bad]], ": ", part, " must be null")
      }
      if (identical(shape[[part]], "required")) {
        bad <- is.na(v) | !nzchar(v)
        said[i[bad], paste0(part, "_required")] <- paste0(where[i[bad]], ": ", part, " is required")
      }
    }
    if (!is.null(decl$levels)) {
      bad <- !(df$level[i] %in% unlist(decl$levels))
      said[i[bad], "levels"] <- paste0(where[i[bad]], ": ", df$level[i[bad]], " is not one of the declared ", section, " rows")
    }
    stat <- df$stat[i]
    number <- .tab_declared(stat, decl$numbers)
    text <- !number & .tab_declared(stat, decl$texts)
    if (!section %in% c("point", "curve")) {
      bad <- number & !is.na(df$text[i])
      said[i[bad], "number_text"] <- paste0(where[i[bad]], ": a number's text must be null")
    }
    bad <- text & !is.na(df$value[i])
    said[i[bad], "text_value"] <- paste0(where[i[bad]], ": a text's value must be null")
    if (section == "assumption") {
      bad <- text & stat == "verdict" & !(df$text[i] %in% verdicts)
      said[i[bad], "verdict"] <- paste0(where[i[bad]], ": the verdict «", df$text[i[bad]], "» is not one of ",
                                        paste(verdicts, collapse = ", "))
    }
    bad <- !number & !text
    said[i[bad], "stat"] <- paste0(where[i[bad]], ": ", model, " (", variant, ") declares no ", stat[bad], " in ", section)
  }
  found <- as.vector(t(said))
  problems <- c(problems, found[!is.na(found)])
  for (key in c("model|||model", "model|||variant", "model|||n")) {
    if (!key %in% df$key) problems <- c(problems, paste0("the row ", key, " is missing"))
  }
  problems
}
