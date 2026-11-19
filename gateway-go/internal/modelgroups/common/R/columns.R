# Columns as data: the input read, its columns' kinds, their names renamed to positional
# ones (v1, v2…) before any formula or design, and the labels of values as the engine writes
# them (catalog.label_of, operations.pivot_label: «true», «false», a date as 2026-11-19).

# The kind of an R vector, as the catalogue names a column's kind.
tab_kind <- function(x) {
  if (inherits(x, "POSIXt")) return("datetime")
  if (inherits(x, "Date")) return("date")
  if (is.logical(x)) return("boolean")
  if (is.factor(x) || is.character(x)) return("text")
  if (is.integer(x) || inherits(x, "integer64")) return("integer")
  if (is.numeric(x)) return("number")
  "other"
}

# A value's label: what a level of a categorical column is called in the results and the
# card, the same as the engine's for the same value.
tab_label <- function(x) {
  if (is.factor(x)) x <- as.character(x)
  if (is.logical(x)) return(ifelse(is.na(x), NA_character_, ifelse(x, "true", "false")))
  if (inherits(x, "Date")) return(format(x, "%Y-%m-%d"))
  if (inherits(x, "POSIXt")) return(format(x, "%Y-%m-%d %H:%M:%OS", tz = "UTC"))
  if (is.integer(x)) return(ifelse(is.na(x), NA_character_, as.character(x)))
  if (is.numeric(x)) return(vapply(x, tab_number_label, character(1)))
  enc2utf8(as.character(x))
}

# A double as Python's repr writes it: the shortest text that reads back as the same
# number, «.0» on a whole one («2.0», «0.1», «1e-05», «1.5e+20»).
tab_number_label <- function(v) {
  if (is.na(v)) return(NA_character_)
  if (is.infinite(v)) return(if (v > 0) "inf" else "-inf")
  if (v == trunc(v) && abs(v) < 1e16) return(paste0(format(v, scientific = FALSE), ".0"))
  for (d in 15:17) {
    s <- sprintf("%.*g", d, v)
    if (as.numeric(s) == v) return(s)
  }
  sprintf("%.17g", v)
}

# Text order as the engine's (by code point: UTF-8 bytes sort in the same order).
tab_sort_labels <- function(labels) sort(unique(labels), method = "radix")

# The positional names of the columns, and the way back. `names` are the input's, in order.
tab_rename <- function(names) {
  positional <- paste0("v", seq_along(names))
  list(to = stats::setNames(positional, names), from = stats::setNames(names, positional))
}

# The input as a data.frame with the input's own names (check.names off: a name is data).
tab_read_input <- function(path = Sys.getenv("TABULARIA_INPUT"), columns = NULL) {
  if (!nzchar(path)) tab_refuse("no_input", "the step has no input (TABULARIA_INPUT is empty)")
  tbl <- arrow::read_parquet(path, col_select = if (is.null(columns)) NULL else tidyselect::all_of(columns),
                             as_data_frame = TRUE)
  df <- as.data.frame(tbl, stringsAsFactors = FALSE, optional = TRUE)
  for (j in seq_along(df)) if (is.factor(df[[j]])) df[[j]] <- as.character(df[[j]])
  df
}

# A CSV of the reference cases or of the self-test, typed by `types` (column → kind: number,
# integer, text, boolean, date, datetime): a CSV says no type. An empty cell is NULL.
tab_read_csv <- function(path, types) {
  df <- utils::read.csv(path, colClasses = "character", check.names = FALSE, na.strings = "",
                        encoding = "UTF-8", stringsAsFactors = FALSE)
  for (name in names(types)) {
    if (!name %in% names(df)) stop("the reference table ", path, " has no column ", name)
    x <- df[[name]]
    df[[name]] <- switch(types[[name]],
      number = as.numeric(x),
      integer = as.integer(x),
      text = x,
      boolean = ifelse(is.na(x), NA, tolower(x) %in% c("true", "1")),
      date = as.Date(x),
      datetime = as.POSIXct(x, tz = "UTC"),
      stop("unknown type ", types[[name]]))
  }
  df
}

# Rows that the model can use: no NULL or NaN in `columns` (the count is n_missing); an
# infinite number stops the node and names the column.
tab_complete_rows <- function(df, columns) {
  keep <- rep(TRUE, nrow(df))
  for (name in columns) {
    x <- df[[name]]
    if (is.numeric(x) && any(is.infinite(x))) {
      tab_refuse("infinite", paste0("the column «", name, "» holds an infinite value: filter it out or replace it before the node"),
                 list(column = name))
    }
    keep <- keep & !is.na(x)
  }
  list(keep = keep, n = sum(keep), n_missing = sum(!keep))
}
