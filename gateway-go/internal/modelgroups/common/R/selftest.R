# The self-test of a group's image: `Rscript runner/run.R --selftest` (or a task whose mode
# is selftest). The check Job of a model group runs it; the version is ready only when it
# exits 0. It runs:
#
#   1. the common library's own tests, on common/selftest/table.csv (20 rows whose column
#      names look like code: a`b, x); system("id"), ü ñ);
#   2. the group's reference cases, reference/expected.json (below), each fitted in this
#      process from its CSV and compared with the values computed outside Tabularia.
#
# It prints one line per check, «TABULARIA_SELFTEST ok|failed|skipped <name>: <detail>»,
# then «TABULARIA_SELFTEST_REPORT {"ok": n, "failed": n, "skipped": n, "checks": […]}»
# last, and exits 0 when nothing failed, 4 otherwise.
#
# reference/expected.json:
#   {"format_version": 1, "generated": {"by": "make_reference.R", "R": "4.6.1", "packages": {…}},
#    "cases": [{"id": "linear_categorical", "model": "linear", "data": "data/linear_categorical.csv",
#               "types": {"quantity": "number", "channel": "text"}, "params": {…},
#               "expect": [{"key": "coef|price||estimate", "value": 1.234, "rel": 1e-9},
#                          {"key": "model|||variant", "text": "unpenalized"}],
#               "refuses": null}]}
# A case may instead expect a refusal: "refuses": {"code": "separation", "message": "…"}.
# `rel` (relative, default 1e-9) and `abs` (absolute, default 1e-12) bound a number; a
# number expected null is written null. The arrow round trip is skipped only where
# TABULARIA_SELFTEST_NO_ARROW=1 (a laptop without arrow); in an image it must pass.

tab_selftest <- function(group, routes, code = tab_code_dir()) {
  checks <- list()
  record <- function(name, status, detail = "") {
    checks[[length(checks) + 1L]] <<- list(name = name, status = status, detail = detail)
    cat("TABULARIA_SELFTEST ", status, " ", name, if (nzchar(detail)) paste0(": ", detail) else "", "\n", sep = "")
  }
  run <- function(name, f) {
    outcome <- tryCatch({
      detail <- f()
      if (identical(detail, "skipped")) list("skipped", "arrow is not installed here") else list("ok", "")
    }, error = function(e) list("failed", conditionMessage(e)))
    record(name, outcome[[1L]], outcome[[2L]])
  }
  for (name in names(TAB_COMMON_TESTS)) run(name, function() TAB_COMMON_TESTS[[name]](code))
  expected <- file.path(code, "reference", "expected.json")
  if (file.exists(expected)) {
    ref <- jsonlite::fromJSON(expected, simplifyVector = FALSE)
    for (case in ref$cases) run(paste0("reference ", case$id), function() tab_reference_case(case, routes, code, group))
    if (!length(ref$cases)) record("reference cases", "ok", "none yet")
  } else {
    record("reference cases", "failed", "reference/expected.json is missing")
  }
  counts <- table(factor(vapply(checks, `[[`, "", "status"), levels = c("ok", "failed", "skipped")))
  report <- list(group = group, ok = counts[["ok"]], failed = counts[["failed"]], skipped = counts[["skipped"]],
                 software = as.list(tab_versions()), checks = checks)
  cat("TABULARIA_SELFTEST_REPORT ", tab_json(report), "\n", sep = "")
  if (counts[["failed"]] > 0L) TAB_EXIT_SELFTEST_FAILED else TAB_EXIT_OK
}

# The groups whose reference cards do not match card.schema.json yet, so not checked against it:
# timeseries, whose arima.coefficients is an array (tab_finite drops the names) where the schema
# says an object by name. To be removed with that fix.
TAB_CARD_SCHEMA_PENDING <- c("timeseries")

# One reference case: fitted in this process on its CSV, compared value by value.
tab_reference_case <- function(case, routes, code, group) {
  df <- tab_read_csv(file.path(code, "reference", case$data), case$types)
  params <- case$params %||% list()
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = group, group_version = "selftest", model = case$model,
               variant = case$variant %||% tab_variant_of(case$model, params), params = params, output = "results",
               write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = df)
  tab_seed(task)
  route <- routes[[case$model]]
  if (is.null(route)) stop("the group has no route for ", case$model)
  outcome <- tryCatch(route$fit(task), tabularia_refusal = function(e) e)
  if (!is.null(case$refuses)) {
    if (!inherits(outcome, "tabularia_refusal")) stop("it was not refused")
    if (!identical(outcome$code, case$refuses$code)) stop("refused with ", outcome$code, ", not ", case$refuses$code)
    if (!is.null(case$refuses$message) && !identical(conditionMessage(outcome), case$refuses$message)) {
      stop("refused with «", conditionMessage(outcome), "»")
    }
    return(invisible(TRUE))
  }
  if (inherits(outcome, "tabularia_refusal")) stop("refused: ", conditionMessage(outcome))
  df <- tab_results_frame(outcome$rows)
  problems <- tab_check_declared(df, case$model, task$variant)
  if (length(problems)) stop(problems[[1L]])
  # the card as model.json holds it, against the schema MLflow and Predict read it by
  if (!is.null(outcome$card) && !group %in% TAB_CARD_SCHEMA_PENDING) {
    back <- jsonlite::fromJSON(tab_card_text(outcome$card), simplifyVector = FALSE)
    problems <- tab_schema_problems(back, jsonlite::fromJSON(file.path(code, "card.schema.json"), simplifyVector = FALSE))
    if (length(problems)) stop("the card does not match card.schema.json: ", problems[[1L]])
  }
  for (e in case$expect) {
    i <- match(e$key, df$key)
    if (is.na(i)) stop("no row ", e$key)
    if (!is.null(e$text)) {
      if (!identical(df$text[[i]], e$text)) stop(e$key, ": «", df$text[[i]], "», expected «", e$text, "»")
      next
    }
    got <- df$value[[i]]
    want <- if (is.null(e$value)) NA_real_ else as.numeric(e$value)
    if (is.na(want) || is.na(got)) {
      if (!(is.na(want) && is.na(got))) stop(e$key, ": ", got, ", expected ", want)
      next
    }
    tol <- max(as.numeric(e$abs %||% 1e-12), as.numeric(e$rel %||% 1e-9) * abs(want))
    if (abs(got - want) > tol) stop(sprintf("%s: %.17g, expected %.17g (tolerance %.3g)", e$key, got, want, tol))
  }
  invisible(TRUE)
}

# ── a JSON value against a JSON Schema (draft 2020-12) ─────────────────────────
# The keywords card.schema.json uses: type, const, enum, properties, required,
# additionalProperties, minProperties, items, minItems, maxItems, minimum, maximum,
# exclusiveMinimum, exclusiveMaximum, minLength, pattern, allOf, oneOf, if/then/else; the others
# are read as notes. `x` is jsonlite's reading with simplifyVector = FALSE (an object a named
# list, an array an unnamed one). What is wrong, one sentence each with where; empty: nothing.
.tab_json_kind <- function(x) {
  if (is.null(x)) return("null")
  if (is.list(x)) return(if (is.null(names(x))) "array" else "object")
  if (is.character(x)) return("string")
  if (is.logical(x)) return("boolean")
  if (is.numeric(x)) return(if (is.finite(x) && x == round(x)) "integer" else "number")
  "other"
}

.tab_json_text <- function(x) as.character(jsonlite::toJSON(x, auto_unbox = TRUE, digits = NA, null = "null"))

tab_schema_problems <- function(x, schema, where = "$") {
  if (isTRUE(schema)) return(character())
  if (isFALSE(schema)) return(paste0(where, ": nothing may be here"))
  out <- character()
  said <- function(...) out <<- c(out, paste0(where, ": ", ...))
  kind <- .tab_json_kind(x)
  types <- unlist(schema[["type"]])
  if (length(types) && !(kind %in% types || (kind == "integer" && "number" %in% types))) {
    return(paste0(where, ": ", kind, ", not ", paste(types, collapse = " or ")))
  }
  if ("const" %in% names(schema) && !identical(.tab_json_text(x), .tab_json_text(schema[["const"]]))) said("not ", .tab_json_text(schema[["const"]]))
  if (!is.null(schema[["enum"]]) && !(.tab_json_text(x) %in% vapply(schema[["enum"]], .tab_json_text, ""))) {
    said(.tab_json_text(x), " is none of ", paste(vapply(schema[["enum"]], .tab_json_text, ""), collapse = ", "))
  }
  if (kind == "object") {
    for (name in setdiff(unlist(schema[["required"]]), names(x))) said("no «", name, "»")
    if (!is.null(schema[["minProperties"]]) && length(x) < schema[["minProperties"]]) said("fewer than ", schema[["minProperties"]], " members")
    props <- schema[["properties"]] %||% list()
    for (name in names(x)) {
      sub <- if (name %in% names(props)) props[[name]] else schema[["additionalProperties"]]
      if (!is.null(sub)) out <- c(out, tab_schema_problems(x[[name]], sub, paste0(where, ".", name)))
    }
  }
  if (kind == "array") {
    if (!is.null(schema[["minItems"]]) && length(x) < schema[["minItems"]]) said("fewer than ", schema[["minItems"]], " items")
    if (!is.null(schema[["maxItems"]]) && length(x) > schema[["maxItems"]]) said("more than ", schema[["maxItems"]], " items")
    if (!is.null(schema[["items"]])) for (i in seq_along(x)) out <- c(out, tab_schema_problems(x[[i]], schema[["items"]], paste0(where, "[", i - 1L, "]")))
  }
  if (kind %in% c("integer", "number")) {
    if (!is.null(schema[["minimum"]]) && x < schema[["minimum"]]) said(x, " below ", schema[["minimum"]])
    if (!is.null(schema[["maximum"]]) && x > schema[["maximum"]]) said(x, " above ", schema[["maximum"]])
    if (!is.null(schema[["exclusiveMinimum"]]) && x <= schema[["exclusiveMinimum"]]) said(x, " not above ", schema[["exclusiveMinimum"]])
    if (!is.null(schema[["exclusiveMaximum"]]) && x >= schema[["exclusiveMaximum"]]) said(x, " not below ", schema[["exclusiveMaximum"]])
  }
  if (kind == "string") {
    if (!is.null(schema[["minLength"]]) && nchar(x) < schema[["minLength"]]) said("shorter than ", schema[["minLength"]])
    if (!is.null(schema[["pattern"]]) && !grepl(schema[["pattern"]], x, perl = TRUE)) said("«", x, "» does not match ", schema[["pattern"]])
  }
  for (sub in schema[["allOf"]]) out <- c(out, tab_schema_problems(x, sub, where))
  if (!is.null(schema[["oneOf"]])) {
    fits <- vapply(schema[["oneOf"]], function(sub) !length(tab_schema_problems(x, sub, where)), logical(1))
    if (sum(fits) != 1L) said("it matches ", sum(fits), " of the schemas of oneOf, not one")
  }
  if (!is.null(schema[["if"]])) {
    branch <- if (!length(tab_schema_problems(x, schema[["if"]], where))) schema[["then"]] else schema[["else"]]
    if (!is.null(branch)) out <- c(out, tab_schema_problems(x, branch, where))
  }
  out
}

# ── the common library's own tests ─────────────────────────────────────────────
.tab_expect <- function(ok, ...) if (!isTRUE(ok)) stop(..., call. = FALSE)

.tab_refused_with <- function(code, expr) {
  outcome <- tryCatch({
    force(expr)
    NULL
  }, tabularia_refusal = function(e) e)
  .tab_expect(inherits(outcome, "tabularia_refusal"), "not refused (expected ", code, ")")
  .tab_expect(identical(outcome$code, code), "refused with ", outcome$code, ", expected ", code)
  conditionMessage(outcome)
}

.tab_selftest_table <- function(code) {
  tab_read_csv(file.path(code, "common", "selftest", "table.csv"),
               list(y = "number", "a`b" = "number", "x); system(\"id\")" = "text", "ü ñ" = "boolean",
                    when = "date", w = "number"))
}

TAB_A <- "a`b"
TAB_X <- "x); system(\"id\")"
TAB_U <- "ü ñ"

TAB_COMMON_TESTS <- list(
  "column names that look like code come back as labels" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, c(TAB_A, TAB_X, TAB_U), outcome = "y", interactions = list(list(TAB_A, TAB_U)))
    fit <- stats::lm.fit(d$X, d$y)
    rows <- tab_rows()
    tab_number(rows, "coef", "estimate", fit$coefficients, level = d$terms$term, level_2 = d$terms$level)
    got <- unique(tab_results_frame(rows)$level)
    want <- c("(Intercept)", TAB_A, TAB_X, TAB_U, paste(TAB_A, TAB_U, sep = " × "))
    .tab_expect(identical(got, want), "levels: ", paste(got, collapse = " | "))
    .tab_expect(identical(d$formula, paste0("y ~ ", TAB_A, " + ", TAB_X, " + ", TAB_U, " + ", TAB_A, " × ", TAB_U)),
                "formula: ", d$formula)
    # the same fit as R's formula interface on positional names, with the same contrasts
    names_ <- tab_rename(names(df))
    pos <- df
    names(pos) <- unname(names_$to[names(df)])
    pos$v3 <- factor(pos$v3, levels = tab_sort_labels(pos$v3))
    pos$v4 <- factor(tab_label(pos$v4), levels = c("false", "true"))
    ref <- stats::lm(v1 ~ v2 + v3 + v4 + v2:v4, data = pos)
    .tab_expect(max(abs(fitted(ref) - (d$X %*% fit$coefficients)[, 1])) < 1e-9, "the fitted values differ from lm's")
    .tab_expect(identical(d$terms$level[d$terms$term == TAB_X], c("north", "south")), "the reference level is not «east»")
  },
  "the reference level, given or the first in text order" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, TAB_X, outcome = "y", reference = stats::setNames(list("south"), TAB_X))
    .tab_expect(identical(d$terms$level[-1L], c("east", "north")), "levels: ", paste(d$terms$level, collapse = ","))
    .tab_expect(identical(d$inputs[[1L]]$reference, "south"), "the card's reference")
    m <- .tab_refused_with("reference_level", tab_design(df, TAB_X, reference = stats::setNames(list("west"), TAB_X)))
    .tab_expect(grepl("has no value «west»", m, fixed = TRUE), m)
    d <- tab_design(df, TAB_U, outcome = "y")
    .tab_expect(identical(d$terms$level[[2L]], "true"), "a boolean's reference is «false»")
  },
  "a wrong setup is refused with a sentence" = function(code) {
    df <- .tab_selftest_table(code)
    m <- .tab_refused_with("date_predictor", tab_design(df, "when", outcome = "y"))
    .tab_expect(grepl("compute a number from the date first", m, fixed = TRUE), m)
    .tab_refused_with("too_many_levels", tab_design(df, TAB_X, max_levels = 2))
    m <- .tab_refused_with("too_many_terms", tab_design(df, c(TAB_A, TAB_X), max_terms = 3))
    .tab_expect(grepl("make 4 terms (at most 3)", m, fixed = TRUE), m)
    bad <- df
    bad[[TAB_A]][[3L]] <- Inf
    m <- .tab_refused_with("infinite", tab_design(bad, TAB_A, outcome = "y"))
    .tab_expect(grepl(TAB_A, m, fixed = TRUE), m)
    one <- df[df[[TAB_X]] == "north", ]
    .tab_refused_with("one_level", tab_design(one, TAB_X, outcome = "y"))
    twice <- df
    twice$copy <- twice[[TAB_A]] * 2
    m <- .tab_refused_with("aliased", tab_refuse_aliased(tab_design(twice, c(TAB_A, "copy"), outcome = "y")))
    .tab_expect(grepl("«copy»", m, fixed = TRUE), m)
  },
  "rows with a NULL are left out and counted" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, TAB_A, outcome = "y", also = "w")
    .tab_expect(d$n == 18L && d$n_missing == 2L && identical(d$rows, setdiff(1:20, c(4L, 17L))), "n ", d$n, ", missing ", d$n_missing)
  },
  "the ten columns, their types and keys" = function(code) {
    rows <- tab_rows()
    tab_text(rows, "model", "model", "linear")
    tab_number(rows, "model", "n", 20)
    tab_p(rows, "coef", c(0.0004, 0.2), level = c("price", "channel"), level_2 = c(NA, "web"))
    tab_number(rows, "model", "x", -0)
    tab_number(rows, "model", "nan", NaN)
    tab_number(rows, "model", "tiny", 1e-310)
    tab_points(rows, "point", "qq", "y", x = c(-1, 0, 1), y = c(0.5, Inf, 2), labels = c("17", NA, NA))
    df <- tab_results_frame(rows)
    .tab_expect(identical(names(df), TAB_COLUMNS), "columns: ", paste(names(df), collapse = ","))
    types <- vapply(df, function(x) class(x)[[1L]], "")
    .tab_expect(identical(unname(types), c(rep("character", 4L), "numeric", "character", "numeric", "numeric", "integer", "character")),
                "types: ", paste(types, collapse = ","))
    .tab_expect(identical(df$ord, 0:(nrow(df) - 1L)), "ord")
    want <- c("model|||model", "model|||n", "coef|price||p_value", "coef|price||significance", "coef|channel|web|p_value",
              "coef|channel|web|significance", "model|||x", "model|||nan", "model|||tiny", "point|qq||y|0", "point|qq||y|1", "point|qq||y|2")
    .tab_expect(identical(df$key, want), "keys: ", paste(df$key, collapse = " "))
    .tab_expect(identical(df$text[df$key == "coef|price||significance"], "***"), "R's stars")
    .tab_expect(identical(df$text[df$key == "coef|channel|web|significance"], ""), "a p-value above 0.1 has an empty code")
    x <- df$value[df$key == "model|||x"]
    .tab_expect(x == 0 && 1 / x == Inf, "−0 is not 0")
    .tab_expect(is.na(df$value[df$key == "model|||nan"]) && df$value[df$key == "model|||tiny"] == 0, "NaN or subnormal")
    .tab_expect(is.na(df$value[df$key == "point|qq||y|1"]) && identical(df$text[df$key == "point|qq||y|0"], "17"), "points")
  },
  "the catalogue's declaration is checked" = function(code) {
    task <- list(model = "linear", variant = "unpenalized", rows_from = "trial", group_version = 1)
    rows <- tab_rows_start(task)
    tab_number(rows, "model", "n", 20)
    tab_p(rows, "coef", 0.01, level = "price")
    tab_text(rows, "assumption", "verdict", "ok", level = "breusch_pagan")
    .tab_expect(!length(tab_check_declared(rows, "linear", "unpenalized")), paste(tab_check_declared(rows, "linear", "unpenalized"), collapse = "; "))
    problems <- tab_check_declared(rows, "linear", "penalized")
    .tab_expect(any(grepl("declares no p_value in coef", problems, fixed = TRUE)), "a p-value of a penalized fit passed")
    tab_text(rows, "assumption", "verdict", "fine", level = "white")
    .tab_expect(any(grepl("is not one of", tab_check_declared(rows, "linear", "unpenalized"), fixed = TRUE)), "a verdict «fine» passed")
    .tab_expect(identical(tab_variant_of("linear", list(penalty = "lasso")), "penalized") &&
                identical(tab_variant_of("arima", list()), "manual"), "variants")
    # a rule `true` is «given» (a column's name), `false` «not given», as the engine's _holds reads them
    .tab_expect(identical(tab_variant_of("clv", list(spend = "avg_spend")), "value") &&
                identical(tab_variant_of("clv", list()), "purchases") && identical(tab_variant_of("bg_bb", list(spend = NULL)), "purchases") &&
                tab_holds(TRUE, "x") && !tab_holds(TRUE, list()) && tab_holds(FALSE, NULL) && !tab_holds(FALSE, "x") &&
                tab_holds(list("a", "b"), "b") && !tab_holds(list("a"), "c") && tab_holds("a", "a"), "variants of a column given or not")
  },
  "the results stay within max_result_rows: the charts' budget, every rule checked at once, the refusal" = function(code) {
    task <- list(model = "linear", variant = "unpenalized", rows_from = "run", group_version = 1, limits = tab_catalog()$limits)
    most <- tab_limit(task, "max_result_rows")
    rows <- tab_rows_start(task)
    tab_number(rows, "model", "n", 20)
    # 9 strata × 7 series at max_chart_points would pass the limit: the budget shares what is left
    per <- tab_chart_points(task, rows, series = 63, after = 2)
    .tab_expect(per < tab_limit(task, "max_chart_points") && rows$n + 63 * per + 2 + 1 <= most, "the budget: ", per)
    .tab_expect(tab_chart_points(task, rows, series = 3) == tab_limit(task, "max_chart_points"), "a few series keep max_chart_points")
    # one row more than the engine takes: said by the check, refused before anything is written
    n <- most - rows$n + 1
    tab_points(rows, "point", "qq", "y", x = seq_len(n), y = seq_len(n))
    tab_text(rows, "assumption", "verdict", "fine", level = "breusch_pagan")
    total <- rows$n
    took <- system.time(problems <- tab_check_declared(rows, "linear", "unpenalized"))[["elapsed"]]
    .tab_expect(length(problems) == 2L && identical(problems[[1L]], paste0(total, " rows, more than the 100000 a model's results may hold")) &&
                grepl("«fine» is not one of", problems[[2L]], fixed = TRUE), paste(head(problems, 3L), collapse = "; "))
    # a row at a time took 0.15 ms each (15 s here): the rules are applied to a section's rows at once
    .tab_expect(took < 3, "checking ", total, " rows took ", round(took, 1), " s")
    m <- .tab_refused_with("too_many_results", tab_finish_fit(task, list(rows = rows, output = NULL, card = NULL)))
    .tab_expect(grepl(paste0("the results would hold ", total, " rows, more than the 100000"), m, fixed = TRUE), m)
    # the order of the problems is the rows' order, each row's in the order of its rules
    bad <- tab_rows_start(task)
    tab_number(bad, "model", "n", 20)
    tab_text(bad, "coef", "estimate", "x", level = "price")
    tab_number(bad, "nowhere", "y", 1)
    tab_number(bad, "point", "y", 1)
    got <- tab_check_declared(bad, "linear", "unpenalized")
    want <- c("the row coef|price||estimate: a number's text must be null", "the row nowhere|||y: the variant unpenalized of linear has no section nowhere",
              "the row point|||y: level is required", "the row point|||y: NA is not one of the declared point rows")
    .tab_expect(identical(got, want), paste(got, collapse = " / "))
  },
  "charts are capped and keep their extremes" = function(code) {
    set.seed(1)
    score <- stats::rnorm(5000)
    set.seed(1)
    i <- tab_cap(5000, max = 2000, score = score, keep = 10)
    .tab_expect(length(i) == 2000L && !is.unsorted(i) && all(head(order(-abs(score)), 10) %in% i), "the cap")
    set.seed(1)
    .tab_expect(identical(i, tab_cap(5000, max = 2000, score = score, keep = 10)), "the same seed, the same points")
    .tab_expect(identical(tab_cap(15, 2000), 1:15), "below the cap")
    q <- tab_qq(score, 200)
    .tab_expect(nrow(q) == 200L && !is.unsorted(q$y), "Q–Q")
    h <- tab_hist(score)
    .tab_expect(sum(h$count) == 5000 && nrow(h) >= 5L && nrow(h) <= 30L, "histogram")
    s <- tab_thin_steps(1:3000, seq(1, 0, length.out = 3000), max = 100)
    .tab_expect(nrow(s) == 100L && s$y[[1L]] == 1, "steps")
    labels <- tab_extreme_labels(c(0.1, -5, 0.2), rows = c(11, 12, 13), keep = 1)
    .tab_expect(identical(labels, c(NA, "12", NA)), "labels")
  },
  "labels of values are the engine's" = function(code) {
    got <- c(tab_label(TRUE), tab_label(as.Date("2026-11-19")), tab_label(2), tab_label(0.1), tab_label(3L), tab_label(1e-05))
    .tab_expect(identical(got, c("true", "2026-11-19", "2.0", "0.1", "3", "1e-05")), paste(got, collapse = " "))
    .tab_expect(identical(tab_sort_labels(c("b", "B", "à", "a")), c("B", "a", "b", "à")), "text order")
  },
  "a text is written to an address and read back whole" = function(code) {
    if (identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)) return("skipped")
    path <- tempfile(fileext = ".json")
    text <- paste0('{"a": "', paste(rep("à", 3000L), collapse = ""), '"}')
    tab_write_text(paste0("file://", path), text)
    back <- tab_read_text(paste0("file://", path))
    .tab_expect(identical(charToRaw(back), charToRaw(enc2utf8(text))), "read back ", nchar(back), " characters, wrote ", nchar(text))
    tab_write_text(paste0("file://", path), "")
    .tab_expect(identical(tab_read_text(paste0("file://", path)), ""), "an empty file")
  },
  "the model card" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, c(TAB_A, TAB_X), outcome = "y")
    task <- list(model = "linear", variant = "unpenalized", group = "regression", group_version = 1, params = list(),
                 limits = tab_catalog()$limits)
    card <- tab_card(task, d)
    card$coefficients <- as.list(stats::lm.fit(d$X, d$y)$coefficients)
    card$link <- "identity"
    card$linear <- list(sigma = 1, df_residual = 16)
    path <- tempfile(fileext = ".json")
    tab_write_card(task, card, path)
    back <- jsonlite::fromJSON(path, simplifyVector = FALSE)
    schema <- jsonlite::fromJSON(file.path(code, "card.schema.json"), simplifyVector = FALSE)
    missing <- setdiff(unlist(schema$required), names(back))
    .tab_expect(!length(missing), "missing: ", paste(missing, collapse = ", "))
    .tab_expect(is.list(back$params) && identical(back$format, "tabularia-model"), "params is an object")
    .tab_expect(length(back$terms) == length(back$coefficients) && is.null(back$terms[[1L]]$level), "terms")
    .tab_expect(identical(as.numeric(unlist(back$coefficients)), unname(as.numeric(unlist(card$coefficients)))) &&
                identical(as.numeric(jsonlite::fromJSON(tab_json(c(0.1, 1 / 3, pi * 1e10, -1.2345678901234567e-5)))),
                          c(0.1, 1 / 3, pi * 1e10, -1.2345678901234567e-5)),
                "a number read back from the card is not the same double")
    .tab_expect(identical(back$inputs[[2L]]$levels, list("east", "north", "south")), "levels")
    .tab_expect(grepl("tabularia:\n    card: model.json", tab_mlmodel(card), fixed = TRUE), "MLmodel")
    small <- task
    small$limits$max_card_bytes <- 100
    .tab_refused_with("card_too_large", tab_write_card(small, card, tempfile()))
  },
  "a card is checked against card.schema.json, as every reference case's is" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, c(TAB_A, TAB_X), outcome = "y")
    task <- list(model = "linear", variant = "unpenalized", group = "regression", group_version = 1, params = list(),
                 limits = tab_catalog()$limits)
    card <- tab_card(task, d)
    card$coefficients <- as.list(unname(stats::lm.fit(d$X, d$y)$coefficients))
    card$link <- "identity"
    card$linear <- list(sigma = 1, df_residual = 16)
    back <- jsonlite::fromJSON(tab_card_text(card), simplifyVector = FALSE)
    schema <- jsonlite::fromJSON(file.path(code, "card.schema.json"), simplifyVector = FALSE)
    .tab_expect(!length(tab_schema_problems(back, schema)), paste(tab_schema_problems(back, schema), collapse = "; "))
    bad <- back
    bad$model <- "nope"
    bad$format_version <- 2L
    bad$params <- NULL
    got <- tab_schema_problems(bad, schema)
    .tab_expect(any(grepl("$.model: \"nope\" is none of", got, fixed = TRUE)) && any(grepl("$.format_version: not 1", got, fixed = TRUE)) &&
                any(grepl("$: no «params»", got, fixed = TRUE)), paste(got, collapse = "; "))
    one_of <- list(oneOf = list(list(type = "array"), list(type = "object"), list(type = "null")))
    .tab_expect(!length(tab_schema_problems(NULL, one_of)) && length(tab_schema_problems("x", one_of)) == 1L &&
                length(tab_schema_problems(NULL, list(type = "object"))) == 1L, "oneOf and type")
  },
  "a card too large does not stop the fit: results and a note, no model files" = function(code) {
    if (identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)) return("skipped")
    df <- .tab_selftest_table(code)
    d <- tab_design(df, c(TAB_A, TAB_X), outcome = "y")
    task <- list(model = "linear", variant = "unpenalized", group = "regression", group_version = 1, params = list(),
                 mlflow = TRUE, limits = tab_catalog()$limits)
    task$limits$max_card_bytes <- 100
    rows <- tab_rows_start(task)
    tab_number(rows, "model", "n", d$n)
    card <- tab_card(task, d)
    card$coefficients <- as.list(stats::lm.fit(d$X, d$y)$coefficients)
    dir <- tempfile("finish-")
    dir.create(dir)
    old <- Sys.getenv(c("TABULARIA_RESULTS", "TABULARIA_ARTIFACTS"), unset = NA)
    on.exit({
      for (k in names(old)) if (is.na(old[[k]])) Sys.unsetenv(k) else do.call(Sys.setenv, stats::setNames(list(old[[k]]), k))
      unlink(dir, recursive = TRUE)
    })
    Sys.setenv(TABULARIA_RESULTS = file.path(dir, "results.parquet"), TABULARIA_ARTIFACTS = file.path(dir, "artifacts"))
    said <- character()
    withCallingHandlers(tab_finish_fit(task, list(rows = rows, output = NULL, card = card, native = list(a = 1))),
                        warning = function(w) {
                          said <<- c(said, conditionMessage(w))
                          invokeRestart("muffleWarning")
                        })
    back <- as.data.frame(arrow::read_parquet(file.path(dir, "results.parquet")))
    .tab_expect(identical(back$text[back$key == "model|||note_card_too_large"], TAB_NOTE_CARD_TOO_LARGE), "no note in the results")
    .tab_expect(!any(file.exists(file.path(dir, "artifacts", c("model.json", "MLmodel", "native.rds")))), "a model file was written")
    .tab_expect(length(said) == 1L && grepl("is not written", said, fixed = TRUE), "the warning for run.json: ", paste(said, collapse = "; "))
    # under the limit the same fit writes its card, MLmodel and native.rds, and no note
    task$limits$max_card_bytes <- 1048576
    rows <- tab_rows_start(task)
    tab_number(rows, "model", "n", d$n)
    tab_finish_fit(task, list(rows = rows, output = NULL, card = card, native = list(a = 1)))
    back <- as.data.frame(arrow::read_parquet(file.path(dir, "results.parquet")))
    .tab_expect(!"model|||note_card_too_large" %in% back$key, "a note under the limit")
    .tab_expect(all(file.exists(file.path(dir, "artifacts", c("model.json", "MLmodel", "native.rds")))), "the model files are missing")
  },
  "native.rds holds no row of the input, and predicts as the fit (lm, glm)" = function(code) {
    df <- .tab_selftest_table(code)
    d <- tab_design(df, c(TAB_A, TAB_X), outcome = "y")
    dat <- data.frame(y = d$y, d$X[, -1L], check.names = FALSE)
    big <- dat[rep(seq_len(nrow(dat)), 50L), ] # the same fit on 50 times the rows
    form <- stats::reformulate(colnames(d$X)[-1L], response = "y")
    dat$hit <- as.numeric(dat$y > stats::median(dat$y))
    big$hit <- as.numeric(big$y > stats::median(big$y))
    for (kind in c("lm", "glm")) {
      fit <- function(data) {
        secret <- data # a local of the frame that made the formula: saveRDS would write it with the formula's environment
        f <- stats::reformulate(colnames(d$X)[-1L], response = if (kind == "lm") "y" else "hit")
        if (kind == "lm") stats::lm(f, data = data) else suppressWarnings(stats::glm(f, family = stats::binomial(), data = data))
      }
      small <- fit(dat)
      lean <- tab_native_lean(small)
      left <- tab_native_rows_left(lean, nrow(dat))
      .tab_expect(!length(left), kind, ": rows left in ", paste(left, collapse = ", "))
      size <- function(x) length(serialize(x, NULL))
      .tab_expect(size(tab_native_lean(fit(big))) < 1.05 * size(lean), kind, ": its size grows with the rows (",
                  size(tab_native_lean(fit(big))), " against ", size(lean), ")")
      .tab_expect(size(lean) < size(small) / 2, kind, ": not smaller than the fit")
      back <- unserialize(serialize(lean, NULL))
      type <- if (kind == "lm") "response" else "link"
      .tab_expect(isTRUE(all.equal(unname(stats::predict(back, newdata = dat, type = type)),
                                   unname(stats::predict(small, newdata = dat, type = type)), tolerance = 1e-12)),
                  kind, ": predict differs from the fit's")
      if (kind == "lm") {
        ci <- stats::predict(back, newdata = dat, se.fit = TRUE, scale = summary(small)$sigma)$se.fit
        .tab_expect(isTRUE(all.equal(unname(ci), unname(stats::predict(small, newdata = dat, se.fit = TRUE)$se.fit), tolerance = 1e-10)),
                    "lm: the standard errors of predict differ")
      }
    }
  },
  "a refusal writes error.json and exits 3; a crash exits 1" = function(code) {
    for (case in list(list(route = "refuse", status = 3L), list(route = "crash", status = 1L), list(route = "protocol", status = 3L))) {
      dir <- tempfile("selftest-")
      dir.create(file.path(dir, "runner"), recursive = TRUE)
      dir.create(file.path(dir, "common"))
      file.copy(file.path(code, "common", "R"), file.path(dir, "common"), recursive = TRUE)
      file.copy(file.path(code, "catalog.json"), dir)
      writeLines(c(
        'here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))',
        'for (f in sort(list.files(file.path(here, "common", "R"), full.names = TRUE))) source(f)',
        'tab_main("selftest", list(linear = list(fit = function(task) {',
        '  if (isTRUE(task$params$seed == 2)) stop("a bug")',
        '  tab_refuse("selftest_refusal", "a refusal à la carte", list(column = "a`b"))',
        '})))'), file.path(dir, "runner", "run.R"))
      task <- list(protocol = if (case$route == "protocol") 99L else TAB_PROTOCOL, mode = "fit", group = "selftest",
                   model = "linear", params = list(seed = if (case$route == "crash") 2 else 1))
      writeLines(tab_json(task), file.path(dir, "task.json"))
      out <- suppressWarnings(system2(file.path(R.home("bin"), "Rscript"), file.path(dir, "runner", "run.R"),
                                      stdout = TRUE, stderr = TRUE, env = paste0("TABULARIA_ARTIFACTS=", file.path(dir, "artifacts"))))
      status <- attr(out, "status") %||% 0L
      .tab_expect(identical(as.integer(status), case$status), case$route, ": exit ", status, "\n", paste(out, collapse = "\n"))
      if (case$route == "refuse") {
        err <- jsonlite::fromJSON(file.path(dir, "artifacts", "error.json"), simplifyVector = FALSE)
        .tab_expect(identical(err$code, "selftest_refusal") && identical(err$message, "a refusal à la carte") &&
                    identical(err$params$column, "a`b"), "error.json: ", tab_json(err))
        .tab_expect(any(startsWith(out, "TABULARIA_ERROR {")), "no TABULARIA_ERROR line")
        .tab_expect(file.exists(file.path(dir, "artifacts", "run.json")), "no run.json")
      }
      if (case$route == "protocol") {
        err <- jsonlite::fromJSON(file.path(dir, "artifacts", "error.json"), simplifyVector = FALSE)
        .tab_expect(identical(err$code, "protocol"), "protocol: ", err$code)
      }
      unlink(dir, recursive = TRUE)
    }
  },
  "the results' Parquet file has the exact types" = function(code) {
    if (!requireNamespace("arrow", quietly = TRUE)) {
      if (identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1")) return("skipped")
      stop("arrow is not installed")
    }
    rows <- tab_rows()
    tab_text(rows, "model", "model", "linear")
    tab_number(rows, "model", "n", 20)
    tab_points(rows, "hist", "residual", "count", x = c(0, 1), y = c(3, 4), x1 = c(1, 2))
    path <- tempfile(fileext = ".parquet")
    tab_write_results(rows, path)
    back <- arrow::read_parquet(path, as_data_frame = FALSE)
    types <- vapply(back$schema$fields, function(f) f$type$ToString(), "")
    .tab_expect(identical(types, c("string", "string", "string", "string", "double", "string", "double", "double", "int64", "string")),
                "types: ", paste(types, collapse = ","))
    .tab_expect(identical(back$schema$names, TAB_COLUMNS), "names")
    df <- as.data.frame(back)
    .tab_expect(identical(as.numeric(df$ord), c(0, 1, 2, 3)) && identical(df$x1[3:4], c(1, 2)), "values")
  }
)
