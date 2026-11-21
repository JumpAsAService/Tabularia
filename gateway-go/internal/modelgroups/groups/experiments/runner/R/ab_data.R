# Reading the data of an experiment (docs/specs/ab-testing.md, «Types of the input's columns», «The unit column»,
# «Which rows count», «CUPED», «The cap», «SRM», «Reading the data: sufficient statistics (the light path)»).
#
# ab_read(task) reads only the node's columns, applies the rules of the input, and gives back the sufficient
# statistics the comparisons need, never the rows again:
#
#   d$variants   labels (text order), values (the input column's own values), type (its Arrow type), k, control
#                (index), expected (shares, sum 1)
#   d$n_assigned units per variant before any choice of rows (crossover units left out): what SRM counts
#   d$n_used     units per variant left after the rows that count (a NULL metric leaves a unit out)
#   d$n          units per variant in the comparisons (after `covariate_missing = exclude`)
#   d$cells      one row per (variant, day) that has a unit: variant, day, then n, sum_*, ss_* (see below)
#   d$totals     the same, summed by variant (k rows); d$own and d$totals_own: the units that have a covariate
#                of their own (NULL when every unit has one)
#   d$shift      the centre of each channel: ss_ab is the sum of (a − shift_a)(b − shift_b), so no cancellation
#                in Σy² − (Σy)²/n; sum_* are raw sums (a binary metric's counts stay exact integers)
#   d$covariate  counts of given and empty values per variant, the fill values, the units filled or left out
#   d$cap, d$hist, d$daily_units, d$days, d$srm, d$notes
#
# Channels: y (the metric, or the numerator), x (the denominator, ratio), c (the covariate, its empty values
# filled by the mean of its own variant when `covariate_missing` is `fill`). No random number is drawn.
#
# arrow for R reads an Int64 whose values fit in 32 bits as an R integer (its option `arrow.int64_downcast`,
# true by default). This group keeps every input column's own type wherever one reaches an output (the
# variant of the `variants` output): an Int64 stays a 64-bit integer (`bit64::integer64`), so that the unit's
# id and the variant are never a double. The option is set when the file is sourced, before any reading.

options(arrow.int64_downcast = FALSE)

AB_HIST_BINS <- 30L

# ── small helpers ────────────────────────────────────────────────────────────────
.ab_int <- function(v) format(v, scientific = FALSE, trim = TRUE)
.ab_num <- function(v) if (is.na(v)) "NULL" else format(signif(v, 15), scientific = FALSE, trim = TRUE, drop0trailing = TRUE, big.mark = ",")
.ab_nf <- function(v) format(signif(v, 15), scientific = FALSE, trim = TRUE, drop0trailing = TRUE)
.ab_units <- function(n) paste0(.ab_int(n), if (n == 1) " unit" else " units")
# a list of names in a note: each one encoded as a reading's text (its «%», « », «,»), so that a variant called
# «A count=9» or «x%41y» stays one name and never another value of the note
.ab_names <- function(labels) paste(ab_reading_text(labels), collapse = ",")

# A label of a value, as tab_label writes it; a 64-bit integer is its decimal digits.
ab_label <- function(x) {
  if (inherits(x, "integer64")) return(as.character(x))
  tab_label(x)
}

.ab_double <- function(x) {
  if (inherits(x, "integer64")) return(bit64::as.double.integer64(x))
  as.numeric(x)
}

# Days since 1970-01-01 of a date or a datetime (UTC).
.ab_days <- function(x) if (inherits(x, "POSIXt")) floor(as.numeric(x) / 86400) else as.numeric(x)

# A note: «@id name=value …», the form the report's templates read.
.ab_note <- function(id, ...) {
  v <- list(...)
  paste0("@", id, paste0(" ", names(v), "=", unlist(v), collapse = ""))
}

# The quantile of type 1 (a value of the data), by a partial sort: the same value as quantile(type = 1).
ab_quantile1 <- function(x, p) {
  n <- length(x)
  nppm <- n * p
  j <- floor(nppm + 4 * .Machine$double.eps)
  k <- pmin(pmax(j + (nppm > j), 1), n)
  x <- sort(x, partial = unique(k))
  x[k]
}

# The unit's id as something `order` and `!=` compare exactly: an Int64 above 2^53 is text, never a double.
.ab_unit_key <- function(u) {
  if (inherits(u, "integer64")) {
    r <- range(u, na.rm = TRUE)
    return(if (all(abs(.ab_double(r)) < 2^53)) .ab_double(u) else as.character(u))
  }
  if (is.factor(u)) return(as.character(u))
  if (is.logical(u) || inherits(u, "Date")) return(as.numeric(u))
  if (inherits(u, "POSIXt")) return(as.numeric(u))
  u
}

# ── the node's columns ───────────────────────────────────────────────────────────
.ab_given <- function(v) !is.null(v) && length(v) == 1L && !is.na(v) && nzchar(as.character(v))

ab_columns <- function(task) {
  p <- task$params
  shape <- p$input_shape %||% "units"
  type <- p$metric_type %||% task$variant
  if (identical(shape, "aggregated") && identical(type, "ratio")) {
    tab_refuse("ratio_needs_units", "a ratio metric needs one row per unit: the delta method uses each unit's numerator and denominator")
  }
  cols <- list(variant = p$variant)
  if (identical(shape, "aggregated")) {
    cols$units_count <- p$units_count
    cols$metric_sum <- p$metric_sum
    if (identical(type, "continuous")) cols$metric_sum_squares <- p$metric_sum_squares
  } else {
    if (.ab_given(p$unit)) cols$unit <- p$unit
    if (identical(type, "ratio")) {
      cols$numerator <- p$numerator
      cols$denominator <- p$denominator
    } else {
      cols$metric <- p$metric
    }
    if (.ab_given(p$covariate)) cols$covariate <- p$covariate
  }
  if (.ab_given(p$date)) cols$date <- p$date
  for (role in names(cols)) {
    if (!.ab_given(cols[[role]])) {
      tab_refuse(paste0(role, "_needed"), paste0("the node needs a column for «", role, "»"), list(role = role))
    }
  }
  cols
}

.ab_schema <- function(task) {
  if (!is.null(task$.frame)) return(NULL)
  path <- Sys.getenv("TABULARIA_INPUT")
  if (!nzchar(path)) return(NULL)
  tryCatch(arrow::ParquetFileReader$create(path)$GetSchema(), error = function(e) NULL)
}

# What the node reads: the frame, once the columns exist.
.ab_input <- function(task, cols) {
  names_in <- if (!is.null(task$.frame)) names(task$.frame) else {
    schema <- .ab_schema(task)
    if (is.null(schema)) NULL else schema$names
  }
  wanted <- unique(unlist(cols, use.names = FALSE))
  if (!is.null(names_in)) {
    for (w in wanted) {
      if (!w %in% names_in) tab_refuse("missing_column", paste0("the table has no column «", w, "»"), list(column = w))
    }
  }
  df <- tab_input(task, wanted)
  for (w in wanted) {
    if (!w %in% names(df)) tab_refuse("missing_column", paste0("the table has no column «", w, "»"), list(column = w))
  }
  df
}

# A number column as doubles; an infinite value stops the node and names the column.
.ab_numeric <- function(x, name, what = "a metric is true/false or a number") {
  kind <- tab_kind(x)
  if (!kind %in% c("integer", "number", "boolean")) {
    tab_refuse("column_type", paste0("the column «", name, "» is ", if (kind == "text") "text" else paste0("of the kind ", kind),
                                     ": ", what), list(column = name, kind = kind))
  }
  v <- .ab_double(x)
  if (any(is.infinite(v))) {
    tab_refuse("infinite", paste0("the column «", name, "» holds an infinite value: filter it out or replace it before the node"),
               list(column = name))
  }
  v
}

# ── the variants ─────────────────────────────────────────────────────────────────
.ab_param_label <- function(v) {
  if (is.logical(v)) return(if (is.na(v)) NA_character_ else if (v) "true" else "false")
  if (is.numeric(v)) return(.ab_int(v))
  as.character(v)
}

ab_setup_variants <- function(task, x, schema) {
  p <- task$params
  name <- p$variant
  lab <- ab_label(x)
  labels <- tab_sort_labels(lab[!is.na(lab)])
  k <- length(labels)
  most <- tab_limit(task, "max_variants")
  if (k > most) {
    tab_refuse("too_many_variants", paste0("the column «", name, "» has ", k, " values; at most ", .ab_int(most),
                                           ": filter the variants you compare"), list(column = name, values = k, limit = most))
  }
  control <- .ab_param_label(p$control)
  ci <- match(control, labels)
  if (is.na(ci)) {
    tab_refuse("control_missing", paste0("the control «", control, "» has no row: check the value, or the filter before the node"),
               list(control = control))
  }
  if (k < 2L) tab_refuse("one_variant", "only the control has rows: an experiment needs at least one treatment")
  shares <- p$expected_shares
  expected <- rep(1 / k, k)
  if (length(shares)) {
    for (key in names(shares)) {
      if (!key %in% labels) tab_refuse("share_without_rows", paste0("the variant «", key, "» has an expected share but no row"), list(variant = key))
    }
    for (l in labels) {
      if (is.null(shares[[l]])) {
        tab_refuse("share_missing", paste0("«", l, "» has rows but no expected share: give a share for every variant, or none"), list(variant = l))
      }
    }
    w <- vapply(labels, function(l) as.numeric(shares[[l]]), 0)
    expected <- unname(w / sum(w))
  }
  type <- NULL
  if (!is.null(schema)) type <- tryCatch(schema$GetFieldByName(name)$type, error = function(e) NULL)
  list(labels = labels, values = x[match(labels, lab)], type = type, k = k, control = ci, expected = expected,
       code = match(lab, labels))
}

# The variant's values as an Arrow array of the input column's own type (the `variants` output's `variant`).
ab_variant_array <- function(d) {
  if (is.null(d$variants$type)) arrow::Array$create(d$variants$values) else arrow::Array$create(d$variants$values, type = d$variants$type)
}

# The notes of the covariate's empty values, one per variant that has some, named «<id>_<index of the variant>».
.ab_covariate_notes <- function(id, labels, n_empty, n_given, fill = NULL) {
  out <- lapply(which(n_empty > 0L), function(j) {
    share <- .ab_nf(n_empty[[j]] / (n_empty[[j]] + n_given[[j]]))
    if (is.null(fill)) .ab_note(id, variant = .ab_names(labels[[j]]), count = n_empty[[j]], share = share)
    else .ab_note(id, variant = .ab_names(labels[[j]]), count = n_empty[[j]], share = share, mean = .ab_nf(fill[[j]]))
  })
  if (length(out)) names(out) <- paste0(id, "_", which(n_empty > 0L))
  out
}

# ── SRM: the split of the units ──────────────────────────────────────────────────
ab_srm <- function(n, expected, alpha) {
  total <- sum(n)
  want <- total * expected
  stat <- sum((n - want)^2 / want)
  df <- length(n) - 1L
  p <- stats::pchisq(stat, df, lower.tail = FALSE)
  list(n = n, expected_n = want, share = n / total, expected_share = expected, statistic = stat, df = df, p_value = p,
       alpha = alpha, verdict = if (p < alpha) "failed" else "ok")
}

# ── the days ─────────────────────────────────────────────────────────────────────
# Days 0…D−1 from the first day; beyond max_looks calendar days, equal periods of several days and a note.
.ab_day_axis <- function(task, days) {
  most <- tab_limit(task, "max_looks")
  first <- min(days)
  span <- max(days) - first + 1
  period <- if (span > most) ceiling(span / most) else 1
  list(first = first, last = max(days), span = span, period = period, D = as.integer(floor((span - 1) / period) + 1),
       index = as.integer(floor((days - first) / period)))
}

# ── the sums ─────────────────────────────────────────────────────────────────────
# One rowsum of the channels' counts, sums, shifted products by the cell g = (variant − 1)·D + day + 1.
.ab_cell_matrix <- function(chan, shift, third = FALSE) {
  n <- length(chan[[1L]])
  cols <- list(n = rep(1, n))
  for (a in names(chan)) cols[[paste0("sum_", a)]] <- chan[[a]]
  nm <- names(chan)
  for (i in seq_along(nm)) {
    for (j in i:length(nm)) {
      cols[[paste0("ss_", nm[[i]], nm[[j]])]] <- (chan[[nm[[i]]]] - shift[[nm[[i]]]]) * (chan[[nm[[j]]]] - shift[[nm[[j]]]])
    }
  }
  if (third) cols[["s3_yyy"]] <- (chan$y - shift[["y"]])^3
  do.call(cbind, cols)
}

.ab_sum_cells <- function(mat, g, k, D) {
  sums <- rowsum(mat, g, reorder = TRUE)
  cell <- as.integer(rownames(sums))
  cells <- list(variant = (cell - 1L) %/% D + 1L, day = (cell - 1L) %% D, sums = sums)
  rownames(cells$sums) <- NULL
  cells
}

.ab_totals <- function(cells, k) {
  if (is.null(cells)) return(NULL)
  tot <- matrix(0, k, ncol(cells$sums), dimnames = list(NULL, colnames(cells$sums)))
  rs <- rowsum(cells$sums, cells$variant, reorder = TRUE)
  tot[as.integer(rownames(rs)), ] <- rs
  tot
}

# Mean and (co)variance, per variant, of the channels a and b, from the totals (n − 1 in the divisor).
ab_mean <- function(d, a = "y", own = FALSE) {
  t <- if (own && !is.null(d$totals_own)) d$totals_own else d$totals
  t[, paste0("sum_", a)] / t[, "n"]
}

ab_variance <- function(d, a = "y", b = a, own = FALSE) {
  t <- if (own && !is.null(d$totals_own)) d$totals_own else d$totals
  col <- paste0("ss_", a, b)
  if (!col %in% colnames(t)) col <- paste0("ss_", b, a)
  n <- t[, "n"]
  ca <- t[, paste0("sum_", a)] - n * d$shift[[a]]
  cb <- t[, paste0("sum_", b)] - n * d$shift[[b]]
  (t[, col] - ca * cb / n) / (n - 1)
}

# Cumulative share of the units of each variant, by day (the `srm_share` chart).
ab_srm_daily <- function(d) {
  m <- d$daily_units
  cum <- t(apply(m, 1L, cumsum))
  if (d$days$D == 1L) cum <- matrix(cum, nrow = nrow(m))
  sweep(cum, 2L, colSums(cum), "/")
}

# ── the unit collapse ────────────────────────────────────────────────────────────
# One `order` on the unit, then runs: the rows of a unit are next to each other. `sums` are summed over the
# rows that count (`valid`), `first` columns come from the unit's first row (its first by date when `date` is
# given: the rows are ordered by (unit, date), the left-out rows' dates last).
.ab_collapse <- function(key, vcode, valid, sums, cov = NULL, day = NULL, rows = NULL) {
  o <- if (is.null(day)) order(key, method = "radix") else order(key, day, method = "radix")
  ks <- key[o]
  m <- length(ks)
  newrun <- if (m > 1L) c(TRUE, ks[-1L] != ks[-m]) else TRUE
  gid <- cumsum(newrun)
  first <- which(newrun)
  ng <- length(first)
  vs <- vcode[o]
  vf <- vs[first]
  crossover <- tabulate(gid[vs != vf[gid]], ng) > 0
  out <- list(variant = vf, crossover = crossover, first = first, o = o, nv = NULL)
  if (!is.null(cov)) {
    cs <- cov[o]
    cf <- cs[first][gid]
    vary <- (is.na(cs) != is.na(cf)) | (!is.na(cs) & !is.na(cf) & cs != cf)
    out$cov <- cs[first]
    out$varies <- tabulate(gid[vary], ng) > 0
  }
  mat <- cbind(nv = as.numeric(valid[o]), do.call(cbind, lapply(sums, function(s) s[o])))
  rs <- rowsum(mat, gid, reorder = FALSE)
  out$nv <- rs[, 1L]
  out$sums <- lapply(seq_along(sums), function(i) rs[, i + 1L])
  names(out$sums) <- names(sums)
  if (!is.null(day)) out$day <- day[o][first]
  out$row <- if (is.null(rows)) o[first] else rows[o][first]
  out
}

# ── reading ──────────────────────────────────────────────────────────────────────
ab_read <- function(task) {
  # the byte compiler costs more than it gains here: the module is vectorized, with no loop over the rows
  # (measured: 0.4 s of compiling at the first call, 0.4 s on 100,000 rows); it is back on for the code after
  jit <- compiler::enableJIT(0L)
  on.exit(compiler::enableJIT(jit), add = TRUE)
  p <- task$params
  cols <- ab_columns(task)
  df <- .ab_input(task, cols)
  schema <- .ab_schema(task)
  var <- ab_setup_variants(task, df[[cols$variant]], schema)
  shape <- p$input_shape %||% "units"
  type <- p$metric_type %||% task$variant
  d <- if (identical(shape, "aggregated")) .ab_read_aggregated(task, df, cols, var, type) else .ab_read_units(task, df, cols, var, type)
  d$shape <- shape
  d$metric_type <- type
  d$variants <- var[c("labels", "values", "type", "k", "control", "expected")]
  d$srm <- ab_srm(d$n_assigned, var$expected, as.numeric(p$srm_alpha %||% 0.001))
  d
}

.ab_check_few_units <- function(n, labels) {
  low <- which(n < 2)
  if (length(low)) {
    tab_refuse("few_units", paste0("the variant «", labels[[low[[1L]]]], "» has ", .ab_units(n[[low[[1L]]]]), ": each variant needs at least 2"),
               list(variant = labels[[low[[1L]]]], units = n[[low[[1L]]]]))
  }
}

# The metric's columns as doubles, with their rules (a binary metric is 0/1, a true/false one is a conversion).
.ab_unit_columns <- function(df, cols, type) {
  mcol <- if (type == "ratio") cols$numerator else cols$metric
  y <- .ab_numeric(df[[mcol]], mcol, if (type == "ratio") "a ratio's numerator and denominator are numbers" else "a metric is true/false or a number")
  kind <- tab_kind(df[[mcol]])
  if (type == "continuous" && kind == "boolean") {
    tab_refuse("binary_boolean_continuous", "a true/false metric is a conversion: choose the type binary")
  }
  if (type == "ratio" && kind == "boolean") {
    tab_refuse("column_type", paste0("the column «", mcol, "» is true/false: a ratio's numerator and denominator are numbers"), list(column = mcol))
  }
  x <- if (type == "ratio") .ab_numeric(df[[cols$denominator]], cols$denominator, "a ratio's numerator and denominator are numbers") else NULL
  if (type == "binary") {
    bad <- which(!is.na(y) & y != 0 & y != 1)
    if (length(bad)) {
      tab_refuse("binary_values", paste0("a binary metric is true/false or 0/1: the column «", mcol, "» has ", .ab_num(y[[bad[[1L]]]])),
                 list(column = mcol, value = y[[bad[[1L]]]]))
    }
  }
  cv <- if (!is.null(cols$covariate)) .ab_numeric(df[[cols$covariate]], cols$covariate, "a covariate is a number") else NULL
  dv <- NULL
  if (!is.null(cols$date)) {
    if (!tab_kind(df[[cols$date]]) %in% c("date", "datetime")) {
      tab_refuse("column_type", paste0("the column «", cols$date, "» is not a date"), list(column = cols$date))
    }
    dv <- .ab_days(df[[cols$date]])
  }
  list(y = y, x = x, cv = cv, dv = dv)
}

# The rows, then the units: one row of the table is one unit, or `unit` puts the rows of a unit together. A row
# without a variant (or a unit) is no unit of any variant; a NULL in a column the node uses leaves the row out.
.ab_unit_table <- function(df, cols, var, inp, labels) {
  r1 <- !is.na(var$code)
  if (!is.null(cols$unit)) r1 <- r1 & !is.na(df[[cols$unit]])
  ri <- if (all(r1)) NULL else which(r1)
  pick <- function(v) if (is.null(v) || is.null(ri)) v else v[ri]
  vc <- pick(var$code)
  yv <- pick(inp$y)
  xv <- pick(inp$x)
  dvv <- pick(inp$dv)
  metric_na <- is.na(yv) | (if (is.null(xv)) FALSE else is.na(xv))
  date_na <- if (is.null(dvv)) rep(FALSE, length(yv)) else is.na(dvv)
  valid <- !metric_na & !date_na
  if (!is.null(dvv)) dvv[!valid] <- NA_real_
  yv[!valid] <- 0
  if (!is.null(xv)) xv[!valid] <- 0
  notes <- list()
  if (any(metric_na)) {
    notes$metric_missing <- .ab_note("metric_missing", count = sum(metric_na), variants = .ab_names(labels[sort(unique(vc[metric_na]))]))
  }
  if (any(!metric_na & date_na)) {
    dna <- !metric_na & date_na
    notes$date_missing <- .ab_note("date_missing", count = sum(dna), variants = .ab_names(labels[sort(unique(vc[dna]))]))
  }
  rows <- if (is.null(ri)) seq_along(yv) else ri
  out <- list(n_unassigned = sum(!r1), notes = notes)
  if (!is.null(cols$unit)) {
    sums <- list(y = yv)
    if (!is.null(xv)) sums$x <- xv
    cl <- .ab_collapse(.ab_unit_key(pick(df[[cols$unit]])), vc, valid, sums, cov = pick(inp$cv), day = dvv, rows = rows)
    if (!is.null(cl$varies) && any(cl$varies & !cl$crossover)) {
      u <- which(cl$varies & !cl$crossover)
      u <- u[[which.min(cl$row[u])]]
      who <- ab_label(df[[cols$unit]][cl$row[[u]]])
      tab_refuse("covariate_varies", paste0("the unit ", who, " has two values of the covariate: a pre-experiment value is one per unit"), list(unit = who))
    }
    out <- c(out, list(crossover = cl$crossover, variant = cl$variant, nv = cl$nv, ucov = cl$cov, uday = cl$day, ux = cl$sums$x,
                       uy = cl$sums$y))
  } else {
    out <- c(out, list(crossover = rep(FALSE, length(yv)), variant = vc, nv = as.numeric(valid), ucov = pick(inp$cv), uday = dvv, ux = xv, uy = yv))
  }
  out
}

# The covariate's empty values: filled by the mean of the unit's own variant, or the unit is left out.
.ab_covariate <- function(p, cols, labels, uvar, ucov, used0) {
  k <- length(labels)
  given <- !is.na(ucov)
  n_given <- tabulate(uvar[used0 & given], k)
  n_empty <- tabulate(uvar[used0 & !given], k)
  if (sum(n_given) == 0L) {
    tab_refuse("covariate_all_missing", paste0("no unit has a value of «", cols$covariate, "»: CUPED needs the covariate on some units"),
               list(column = cols$covariate))
  }
  mode <- p$covariate_missing %||% "fill"
  cov <- list(mode = mode, n_given = n_given, n_empty = n_empty, n_filled = integer(k), n_excluded = integer(k), fill_value = rep(NA_real_, k))
  if (identical(mode, "exclude")) {
    cov$n_excluded <- n_empty
    return(list(covariate = cov, keep = used0 & given, notes = .ab_covariate_notes("covariate_excluded", labels, n_empty, n_given)))
  }
  none <- which(n_given == 0L & n_empty > 0L)
  if (length(none)) {
    tab_refuse("covariate_variant_empty", paste0("no unit of the variant «", labels[[none[[1L]]]], "» has a value of «", cols$covariate,
                                                 "»: the empty values cannot be filled; choose to leave them out, or no covariate"),
               list(variant = labels[[none[[1L]]]], column = cols$covariate))
  }
  fv <- rowsum(ifelse(used0 & given, ucov, 0), uvar, reorder = TRUE)
  fill <- rep(NA_real_, k)
  fill[as.integer(rownames(fv))] <- fv[, 1L]
  fill <- fill / pmax(n_given, 1)
  fill[n_given == 0L] <- NA_real_
  cov$fill_value <- fill
  cov$n_filled <- n_empty
  list(covariate = cov, keep = used0, notes = .ab_covariate_notes("covariate_filled", labels, n_empty, n_given, fill))
}

.ab_read_units <- function(task, df, cols, var, type) {
  p <- task$params
  k <- var$k
  labels <- var$labels
  inp <- .ab_unit_columns(df, cols, type)
  u <- .ab_unit_table(df, cols, var, inp, labels)
  notes <- u$notes
  n_cross <- sum(u$crossover)
  if (n_cross > 0L) notes$crossover_excluded <- .ab_note("crossover_excluded", units = n_cross)
  uvar <- u$variant
  assigned <- !u$crossover
  used0 <- assigned & u$nv > 0
  n_assigned <- tabulate(uvar[assigned], k)
  n_used <- tabulate(uvar[used0], k)
  keep <- used0
  covariate <- NULL
  if (!is.null(cols$covariate)) {
    cv <- .ab_covariate(p, cols, labels, uvar, u$ucov, used0)
    covariate <- cv$covariate
    keep <- cv$keep
    notes <- c(notes, cv$notes)
  }
  ui <- which(keep)
  uv <- uvar[ui]
  yy <- u$uy[ui]
  xx <- if (is.null(u$ux)) NULL else u$ux[ui]
  cc <- NULL
  has_own <- NULL
  if (!is.null(covariate)) {
    cc <- u$ucov[ui]
    has_own <- !is.na(cc)
    if (any(!has_own)) cc[!has_own] <- covariate$fill_value[uv[!has_own]]
  }
  n <- tabulate(uv, k)
  .ab_check_few_units(n, labels)
  if (type == "binary") yy <- as.numeric(yy > 0)
  # the cap: the percentile of all units of all variants, upper tail only
  cap <- NULL
  pct <- p$cap_percentile
  if (type == "continuous" && !is.null(pct) && !is.na(pct)) {
    value <- ab_quantile1(yy, as.numeric(pct))
    over <- yy > value
    cap <- list(percentile = as.numeric(pct), value = value, n_capped = tabulate(uv[over], k))
    yy[over] <- value
    notes$cap_applied <- .ab_note("cap_applied", percentile = .ab_nf(pct), value = .ab_nf(value), count = sum(cap$n_capped))
  }
  ax <- .ab_axis(task, if (is.null(u$uday)) NULL else u$uday[ui], notes)
  g <- (uv - 1L) * ax$days$D + ax$index + 1L
  chan <- list(y = yy)
  if (!is.null(xx)) chan$x <- xx
  if (!is.null(cc)) chan$c <- cc
  shift <- vapply(chan, mean, 0)
  mat <- .ab_cell_matrix(chan, shift, third = type == "continuous")
  cells <- .ab_sum_cells(mat, g, k, ax$days$D)
  own <- if (!is.null(has_own) && any(!has_own)) .ab_sum_cells(mat * as.numeric(has_own), g, k, ax$days$D) else NULL
  d <- list(n_rows = nrow(df), n_unassigned_rows = u$n_unassigned, n_crossover = n_cross, n_assigned = n_assigned, n_used = n_used, n = n,
            n_missing = n_assigned - n_used, shift = shift, cells = cells, totals = .ab_totals(cells, k), own = own,
            totals_own = .ab_totals(own, k), covariate = covariate, cap = cap, days = ax$days,
            daily_units = matrix(tabulate(g, k * ax$days$D), nrow = k, ncol = ax$days$D, byrow = TRUE), notes = ax$notes)
  if (type == "ratio") .ab_check_denominators(d, labels)
  if (type == "continuous") d$hist <- .ab_hist(yy, uv, k, cap)
  d
}

.ab_check_denominators <- function(d, labels) {
  zero <- which(d$totals[, "sum_x"] == 0)
  if (length(zero)) {
    tab_refuse("denominator_zero", paste0("the variant «", labels[[zero[[1L]]]], "»'s denominators sum to 0: the ratio has no value"),
               list(variant = labels[[zero[[1L]]]]))
  }
}

# The day axis of the units kept: index per unit (0 without a date), the days, the notes (a grouping of the days).
.ab_axis <- function(task, day, notes) {
  if (is.null(day)) {
    return(list(index = 0L, days = list(first = NA_real_, last = NA_real_, span = 1, period = 1, D = 1L), notes = notes))
  }
  days <- .ab_day_axis(task, day)
  if (days$period > 1) notes$looks_grouped <- .ab_note("looks_grouped", days = days$span, period = days$period)
  list(index = days$index, days = days[c("first", "last", "span", "period", "D")], notes = notes)
}

# 30 equal bins between the pooled 0.5th and 99.5th percentiles (0 and the cap when there is a cap), plus an open
# bin each side: counts per variant (k × 32); NULL where the metric has no spread.
.ab_hist <- function(y, v, k, cap) {
  lo_hi <- if (is.null(cap)) ab_quantile1(y, c(0.005, 0.995)) else c(0, cap$value)
  if (!(lo_hi[[2L]] > lo_hi[[1L]])) return(NULL)
  breaks <- seq(lo_hi[[1L]], lo_hi[[2L]], length.out = AB_HIST_BINS + 1L)
  bin <- findInterval(y, breaks, rightmost.closed = TRUE)
  counts <- matrix(tabulate((bin + 1L) * k + v - k, (AB_HIST_BINS + 2L) * k), nrow = k)
  list(breaks = breaks, counts = counts, range = c(min(y), max(y)))
}

# The histogram's rows: one bar an interval, its two edges in x0 and x1, so that every bar is said by its edges
# («[59.8 – 258)»). The open bin on each side runs to the smallest (largest) value of the metric, and is left out
# when no unit falls in it; `labels`: the variants (level_2), NULL for one series (the planning's history).
ab_hist_points <- function(rows, h, labels = NULL) {
  if (is.null(h)) return(invisible(rows))
  b <- h$breaks
  x0 <- c(h$range[[1L]], b)
  x1 <- c(b, h$range[[2L]])
  nb <- length(x0)
  keep <- seq_len(nb)
  if (all(h$counts[, 1L] == 0L)) keep <- setdiff(keep, 1L)
  if (all(h$counts[, nb] == 0L)) keep <- setdiff(keep, nb)
  if (is.null(labels)) tab_points(rows, "hist", "metric", "count", x = x0[keep], y = h$counts[1L, keep], x1 = x1[keep])
  else for (j in seq_along(labels)) tab_points(rows, "hist", "metric", "count", x = x0[keep], y = h$counts[j, keep], level_2 = labels[[j]], x1 = x1[keep])
  invisible(rows)
}

# The rules of the aggregated rows, one by one, with the row's number (its place in the input).
.ab_check_aggregated <- function(type, n, s, q, valid) {
  for (i in which(valid)) {
    msg <- NULL
    if (n[[i]] < 0 || n[[i]] != round(n[[i]])) {
      msg <- paste0("units are whole numbers, 0 or more: the row ", i, " has ", .ab_num(n[[i]]), " units")
    } else if (type == "binary" && (s[[i]] < 0 || s[[i]] != round(s[[i]]) || s[[i]] > n[[i]])) {
      msg <- paste0("units and conversions are whole numbers, conversions at most units: the row ", i, " has ", .ab_num(s[[i]]),
                    " conversions of ", .ab_num(n[[i]]), " units")
    } else if (n[[i]] == 0 && (s[[i]] != 0 || (!is.null(q) && q[[i]] != 0))) {
      msg <- paste0("units and conversions are whole numbers, conversions at most units: the row ", i, " has sums but no units")
    }
    if (!is.null(msg)) tab_refuse("aggregated_counts", msg, list(row = i))
    if (!is.null(q) && n[[i]] > 0) {
      sq <- s[[i]]^2 / n[[i]]
      if (q[[i]] < sq - 1e-9 * (abs(q[[i]]) + sq)) {
        tab_refuse("aggregated_squares", paste0("the row ", i, "'s sum of squares is below its sum squared over its units: ",
                                                "the sums do not come from the same values"), list(row = i))
      }
    }
  }
}

.ab_read_aggregated <- function(task, df, cols, var, type) {
  p <- task$params
  k <- var$k
  labels <- var$labels
  if (type == "ratio") {
    tab_refuse("ratio_needs_units", "a ratio metric needs one row per unit: the delta method uses each unit's numerator and denominator")
  }
  n <- .ab_numeric(df[[cols$units_count]], cols$units_count, "the number of units is a whole number")
  s <- .ab_numeric(df[[cols$metric_sum]], cols$metric_sum, "the sum of the metric is a number")
  q <- if (type == "continuous") .ab_numeric(df[[cols$metric_sum_squares]], cols$metric_sum_squares, "the sum of squares is a number") else NULL
  dv <- if (!is.null(cols$date)) .ab_days(df[[cols$date]]) else NULL
  r1 <- !is.na(var$code)
  na <- is.na(n) | is.na(s) | (if (is.null(q)) FALSE else is.na(q)) | (if (is.null(dv)) FALSE else is.na(dv))
  valid <- r1 & !na
  notes <- list()
  metric_na <- r1 & (is.na(n) | is.na(s) | (if (is.null(q)) FALSE else is.na(q)))
  if (any(metric_na)) {
    mna <- which(metric_na)
    notes$metric_missing <- .ab_note("metric_missing", count = length(mna), variants = .ab_names(labels[sort(unique(var$code[mna]))]))
  }
  .ab_check_aggregated(type, n, s, q, valid)
  ri <- which(valid)
  vc <- var$code[ri]
  nn <- n[ri]
  ss <- s[ri]
  qq <- if (type == "binary") ss else q[ri]
  n_k <- vapply(seq_len(k), function(j) sum(nn[vc == j]), 0)
  .ab_check_few_units(n_k, labels)
  ax <- .ab_axis(task, if (is.null(dv)) NULL else dv[ri], notes)
  notes <- ax$notes
  days <- ax$days
  D <- days$D
  g_day <- ax$index
  g <- (vc - 1L) * D + g_day + 1L
  centre <- sum(ss) / sum(nn)
  mat <- cbind(n = nn, sum_y = ss, ss_yy = qq - 2 * centre * ss + nn * centre^2)
  cells <- .ab_sum_cells(mat, g, k, D)
  # the number of rows is not the number of units: the daily series counts units
  daily <- matrix(0, nrow = k, ncol = D)
  daily[cbind(cells$variant, cells$day + 1L)] <- cells$sums[, "n"]
  list(n_rows = nrow(df), n_unassigned_rows = sum(!r1), n_crossover = 0L, n_assigned = n_k, n_used = n_k, n = n_k,
       n_missing = tabulate(var$code[which(r1 & na)], k), n_missing_rows = sum(r1 & na), shift = c(y = centre), cells = cells,
       totals = .ab_totals(cells, k), own = NULL, totals_own = NULL, covariate = NULL, cap = NULL, days = days, daily_units = daily, notes = notes)
}
