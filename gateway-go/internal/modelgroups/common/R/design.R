# The design of a regression, built from data, never from text: the outcome, the
# predictors (numbers as they are; texts and booleans as categories with treatment
# contrasts, the reference level first in text order unless `reference` names one), the
# interactions (products of two predictors), the intercept. The matrix's columns are named
# d1, d2… and `terms` maps each back to what a person reads: the term (a column, or two
# joined by « × ») and, for a category, its level. A runner fits on the matrix; it writes
# `terms$term` as the coefficient's level and `terms$level` as its level_2.
#
# Refused with a sentence: a date or datetime predictor, a category with more values than
# max_levels, a design with more columns than max_terms, a reference level the column does
# not hold, an infinite value.

tab_design <- function(df, predictors, outcome = NULL, reference = list(), interactions = list(),
                       intercept = TRUE, also = character(), max_levels = 50, max_terms = 200) {
  used <- unique(c(outcome, predictors, also))
  rows <- tab_complete_rows(df, used)
  df <- df[rows$keep, , drop = FALSE]
  kinds <- vapply(predictors, function(p) tab_kind(df[[p]]), character(1))
  parts <- list()   # per predictor: list(columns (matrix), level (labels or NA))
  inputs <- list()
  for (p in predictors) {
    kind <- kinds[[p]]
    if (kind %in% c("date", "datetime")) {
      tab_refuse("date_predictor", paste0("the column «", p, "» is a ", kind, " and cannot be a predictor: compute a number ",
                                          "from the date first: the month, the days since a start"), list(column = p))
    }
    if (kind %in% c("number", "integer")) {
      parts[[p]] <- list(x = matrix(as.numeric(df[[p]]), ncol = 1L), level = NA_character_)
      inputs[[length(inputs) + 1L]] <- list(name = p, type = "number")
      next
    }
    if (kind == "other") tab_refuse("predictor_type", paste0("the column «", p, "» cannot be a predictor: numbers, texts and true or false can"))
    labels <- tab_label(df[[p]])
    levels <- tab_sort_labels(labels)
    if (length(levels) > max_levels) {
      tab_refuse("too_many_levels", paste0("the column «", p, "» has ", length(levels), " values (at most ", max_levels,
                                           "): is it the right column?"), list(column = p, levels = length(levels)))
    }
    if (length(levels) < 2L) {
      tab_refuse("one_level", paste0("the column «", p, "» holds one value only in the rows the model uses: it cannot explain ",
                                     "anything, leave it out"), list(column = p))
    }
    ref <- reference[[p]]
    if (!is.null(ref)) {
      ref <- tab_label(ref)
      if (!ref %in% levels) {
        tab_refuse("reference_level", paste0("the column «", p, "» has no value «", ref, "» to be its reference level"),
                   list(column = p, level = ref))
      }
    } else {
      ref <- levels[[1L]]
    }
    others <- setdiff(levels, ref)
    x <- vapply(others, function(l) as.numeric(labels == l), numeric(nrow(df)))
    if (!is.matrix(x)) x <- matrix(x, nrow = nrow(df))
    parts[[p]] <- list(x = x, level = others)
    inputs[[length(inputs) + 1L]] <- list(name = p, type = "category", levels = as.list(c(ref, others)), reference = ref)
  }
  columns <- list()
  terms <- list()
  add <- function(x, term, level, index, variables) {
    columns[[length(columns) + 1L]] <<- x
    terms[[length(terms) + 1L]] <<- data.frame(term = term, level = level, term_index = index,
                                               variables = paste(variables, collapse = "\u001f"), stringsAsFactors = FALSE)
  }
  index <- 0L
  if (isTRUE(intercept)) {
    index <- index + 1L
    add(rep(1, nrow(df)), "(Intercept)", NA_character_, index, character())
  }
  for (p in predictors) {
    index <- index + 1L
    part <- parts[[p]]
    for (j in seq_len(ncol(part$x))) add(part$x[, j], p, part$level[[j]], index, p)
  }
  for (pair in interactions) {
    a <- pair[[1L]]
    b <- pair[[2L]]
    index <- index + 1L
    pa <- parts[[a]]
    pb <- parts[[b]]
    for (i in seq_len(ncol(pa$x))) for (j in seq_len(ncol(pb$x))) {
      level <- c(pa$level[[i]], pb$level[[j]])
      level <- if (all(is.na(level))) NA_character_ else paste(level[!is.na(level)], collapse = " × ")
      add(pa$x[, i] * pb$x[, j], paste(a, b, sep = " × "), level, index, c(a, b))
    }
  }
  if (length(columns) > max_terms) {
    tab_refuse("too_many_terms", paste0("the predictors make ", length(columns), " terms (at most ", max_terms,
                                        "): fewer categories, or turn on a penalty"), list(terms = length(columns)))
  }
  X <- if (length(columns)) do.call(cbind, columns) else matrix(numeric(), nrow = nrow(df), ncol = 0L)
  colnames(X) <- if (ncol(X)) paste0("d", seq_len(ncol(X))) else character()
  terms <- if (length(terms)) do.call(rbind, terms) else
    data.frame(term = character(), level = character(), term_index = integer(), variables = character())
  terms$column <- colnames(X)
  terms$label <- ifelse(is.na(terms$level), terms$term, paste0(terms$term, ": ", terms$level))
  rhs <- c(if (!isTRUE(intercept)) "0", predictors, vapply(interactions, function(p) paste(p[[1L]], p[[2L]], sep = " × "), ""))
  formula <- paste0(if (is.null(outcome)) "" else paste0(outcome, " "), "~ ", paste(rhs, collapse = " + "))
  list(X = X, y = if (is.null(outcome)) NULL else df[[outcome]], data = df, rows = which(rows$keep),
       n = rows$n, n_missing = rows$n_missing, terms = terms, inputs = inputs, formula = formula)
}

# The columns of a design that are combinations of the others (aliased), by the pivoted QR
# a fit uses: the terms named, so that the refusal says which to drop.
tab_aliased <- function(design, tol = 1e-7) {
  if (!ncol(design$X)) return(character())
  q <- qr(design$X, tol = tol)
  if (q$rank == ncol(design$X)) return(character())
  design$terms$label[q$pivot[seq.int(q$rank + 1L, ncol(design$X))]]
}

tab_refuse_aliased <- function(design) {
  aliased <- tab_aliased(design)
  if (length(aliased)) {
    tab_refuse("aliased", paste0("the term «", aliased[[1L]], "» is a combination of the others (the design is singular): drop it, ",
                                 "or a predictor that repeats it"), list(terms = as.list(aliased)))
  }
  invisible(NULL)
}

# The design of a card's model on new rows (Predict, and a fit's rows output): the same columns
# in the same order as tab_design made when fitting, from the card's `inputs` (a category's
# levels, the reference first) and its parameters (`predictors`, `interactions`, `intercept`),
# never from the rows' own levels. A row with an empty or infinite value, or with a level the
# fit did not see, is not ok (its prediction is empty); `unseen` counts the rows of the latter.
tab_design_rows <- function(card, df) {
  params <- card$params %||% list()
  predictors <- unlist(params$predictors)
  interactions <- params$interactions %||% list()
  intercept <- !isFALSE(params$intercept)
  inputs <- list()
  for (i in card$inputs) inputs[[i$name]] <- i
  n <- nrow(df)
  ok <- rep(TRUE, n)
  unseen <- rep(FALSE, n)
  parts <- list()
  for (p in predictors) {
    spec <- inputs[[p]]
    if (is.null(spec)) stop("the card has no input ", p)
    if (!p %in% names(df)) {
      tab_refuse("missing_column", paste0("the rows have no column «", p, "», which the model needs"), list(column = p))
    }
    x <- df[[p]]
    if (identical(spec$type, "number")) {
      if (!tab_kind(x) %in% c("number", "integer")) {
        tab_refuse("column_kind", paste0("the column «", p, "» must hold numbers, as when the model was fitted"), list(column = p))
      }
      v <- as.numeric(x)
      ok <- ok & is.finite(v)
      parts[[p]] <- matrix(v, ncol = 1L)
      next
    }
    labels <- tab_label(x)
    levels <- vapply(spec$levels, as.character, "")
    known <- !is.na(labels) & labels %in% levels
    unseen <- unseen | (!is.na(labels) & !known)
    ok <- ok & known
    others <- levels[-1L]
    parts[[p]] <- matrix(vapply(others, function(l) as.numeric(labels == l), numeric(n)), nrow = n)
  }
  columns <- list()
  if (intercept) columns[[1L]] <- rep(1, n)
  for (p in predictors) for (j in seq_len(ncol(parts[[p]]))) columns[[length(columns) + 1L]] <- parts[[p]][, j]
  for (pair in interactions) {
    pa <- parts[[pair[[1L]]]]
    pb <- parts[[pair[[2L]]]]
    for (i in seq_len(ncol(pa))) for (j in seq_len(ncol(pb))) columns[[length(columns) + 1L]] <- pa[, i] * pb[, j]
  }
  X <- if (length(columns)) do.call(cbind, columns) else matrix(numeric(), nrow = n, ncol = 0L)
  if (length(card$terms) && ncol(X) != length(card$terms)) stop("the card's terms and its design differ")
  X[!ok, ] <- NA_real_
  list(X = X, ok = ok, unseen = sum(unseen))
}
