# Spec: the expression language of the Bayesian editor (`expression-language`)

Status: **approved by the owner on 2026-11-23.** It specifies the module `expression-language`
of `docs/specs/bayesian-editor-map.md` (**the map**), added to the map on 2026-11-23 by the owner's answer 3
of the interview (**the answers**, numbered as in the map's «Interview answers»): step 1 of the order of
construction, after `distribution-catalog` and before `model-graph`. Nothing of it is built. Decisions marked
**(owner)** are the owner's; those marked «spec decision, 2026-11-23, owner may revisit» are this spec's; the
rest are this spec's, to confirm. The owner answered this spec's questions on 2026-11-23 («Decisions of
2026-11-23»); no question is open.

Words: **verified** = read in this repository's code on 2026-11-23; **checked online** = read in the Stan
reference manual or functions reference (version 2.40) on 2026-11-23; **measured** = run on 2026-11-23 outside
the repository (a few lines of Go, Python and Node); **assumed** = from documentation, not run here, checked
by the lot named beside it.

## Objective

In the free model (and wherever a graph node takes a value), a person **types** what a quantity is:

```text
a + b * x                       the mean of each row
alpha_school[school] + beta * hours
inv_logit(a + b * dose)         a probability
exp(log_sigma)                  a positive scale
phi * lag(y, 1)                 the previous month of a series
```

The owner chose this against the recommendation of a graph without typed text (answer 3, owner): the graph
alone is too verbose for the free model. In exchange, the text a person types is the one place where
something written by a person becomes code that is compiled and executed. This module makes that safe and
useful:

- a **closed grammar** of our own (below, in EBNF), read by a lexer and a parser into a syntax tree (AST);
- **types and shapes**: every name is a node of the graph, with its type (real, integer, per-plate vector,
  group index) and its plate; the rules say what may meet what, and the errors say how to fix it in words a
  person who does not know Stan understands (answer 1);
- the **Stan text** of an expression **emitted from the typed AST only**, never from the person's text
  (owner): fully parenthesized, identifiers given by the caller, numbers re-printed from their double;
- **limits** of length, nesting and size, checked before the work they bound;
- **one authority**: the parser in Go, in the gateway; a **mirror in TypeScript** for instant feedback while
  typing, never trusted, kept equal by a shared corpus;
- **no user-defined functions** (owner, answer 3); no strings, comments, statements, conditionals, loops.

The threats it closes are in «Threats»; it gets a security review of its own (owner, answer 3: «serve una
revisione di sicurezza dedicata»).

## Decisions

| Question | Decision | By |
|---|---|---|
| Typed expressions | Yes, read by a grammar of ours into an AST; Stan generated only from the AST | Owner (answer 3) |
| User-defined functions | No | Owner (answer 3) |
| Security review | A dedicated one | Owner (answer 3); when: «Security review» (spec) |
| Where the parser lives | **Go**, `gateway-go/internal/bayesexpr`: the gateway alone decides whether an expression is accepted and what Stan text it gives; the TypeScript mirror (`frontend/composables/bayesExpr.ts`) only gives feedback | Spec (the map's provisional decision, the reports' language as precedent) |
| Alphabet | **ASCII only**; any other character is refused with its code point and, for known look-alikes, the character meant | Spec |
| Numbers | Every literal is a real; the only integer literal is `lag`'s second argument | Spec decision, 2026-11-23, owner may revisit |
| Elementwise operators | The person writes `*`, `/`, `^`; the emitter chooses `.*`, `./`, `.^` from the types | Spec |
| Names | A name is a node of the graph that has a valid name (answer 5); a call's name is a function of the catalogue; nothing else | Owner (answer 5), spec |
| Series | `lag(x, k)` **in the language** for observed and per-row quantities of a series; a latent recursion is a node of `model-graph`, not an expression | Owner (2026-11-23) |
| Limits | Documented **constants** of the catalogue (`max_expression_bytes` 1,000, `max_expression_nodes` 256 and the others), not administrator settings | Owner (2026-11-23); the defaults: spec decision, 2026-11-23, owner may revisit |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| An expression language judged in one place and checked while typing | The reports' language: `backend/app/reports/expressions.py` (the engine «alone judges an expression»; `MAX_EXPRESSION_CHARS = 4000`, `MAX_DEPTH = 64`, `MAX_TREE_DEPTH = 200`; `_HINTS` such as `"^": "use POWER(x, y)"`; a `_nested` guard that raises «the expression is nested too deeply»); `frontend/composables/reportExpressions.ts` («what is checked here is what can be told without the server») | The same split, with the authority in Go; the same habit of hints for the operators of other languages |
| Cases read by two implementations | `backend/tests/fixtures/models/params_cases.json`, read by the engine's tests and by `gateway-go/internal/modelcatalog/modelcatalog_test.go` (`os.ReadFile("../../../backend/tests/fixtures/models/params_cases.json")`): the gateway answers «word for word» | The corpus in `gateway-go/internal/bayesexpr/testdata/`, read by the Go tests and by `frontend/tests/bayes-expr.test.mjs` |
| The vocabulary | `distribution-catalog` (`bayes-distribution-catalog.md`): `language.functions`, `operators`, `hints`, `confusables`, `limits`, Stan's reserved lists | Read from the catalogue; nothing restated here as data |
| Tests of the app's TypeScript under Node | `frontend/tests/alias-hooks.mjs` (`~/composables/x` resolved as Nuxt does), `npm test` = `node --experimental-strip-types --test tests/*.test.mjs` | `bayes-expr.test.mjs` imports `~/composables/bayesExpr` |
| Fuzz tests | **None** in the repository (no `func Fuzz` under `gateway-go`) | The first ones: Go's native fuzzing (`testing.F`, Go 1.27) |

**A trap seen while reading** (measured): Python's `re` matches `\d` on Arabic-Indic digits and `float()`
reads them (`float('١٢')` = 12.0); `str.isspace()` is true for U+00A0; in JavaScript `\s` matches U+00A0 and
`/[a-z]/iu` matches U+212A (the Kelvin sign). The reports' lexer uses `\d`, `isdigit` and `isspace` — harmless
there (the engine evaluates; nothing is compiled), but it is why this language **compares bytes** and never
uses a Unicode-aware class, in Go (`unicode.IsDigit`, `unicode.IsSpace`) or in TypeScript (`\s`, `\d`, `\w`,
`/i`, `/u`).

## Where expressions appear

`model-graph` gives a **slot** to every value a node takes: a distribution's argument, a transformed node's
value, a correlated effect's mean, a latent state's scale. A slot holds the person's text (or a reference to a
node by id, for nodes without a name: answer 5, `bayes-model-graph.md`). This module receives, for one slot:
the text, an **environment** (the graph's named nodes with their types and plates, the slot's **context
plate**, the plates' nesting), and returns either a typed AST or the first error.

## The grammar

### Lexical

The input is checked **before** it is read as tokens:

1. its UTF-8 length is at most `max_expression_bytes` (catalogue, 1,000), or `expr.too_long`;
2. every character is ASCII: a byte ≥ 0x80 is `expr.non_ascii` (with its code point, and the character meant
   when it is in `language.confusables`); a control byte other than tab, line feed and carriage return is
   `expr.control`.

Then, from left to right, longest match first:

```ebnf
whitespace = " " | "\t" | "\n" | "\r" ;                         (* separates tokens; otherwise ignored *)
name       = lower , { lower | digit | "_" } ;                    (* at most max_name_bytes; not ending in "__" *)
number     = digits , [ "." , digits ] , [ exponent ] ;           (* at most max_number_chars *)
exponent   = ( "e" | "E" ) , [ "+" | "-" ] , digits ;
digits     = digit , { digit } ;
lower      = "a" | "b" | "c" | "d" | "e" | "f" | "g" | "h" | "i" | "j" | "k" | "l" | "m"
           | "n" | "o" | "p" | "q" | "r" | "s" | "t" | "u" | "v" | "w" | "x" | "y" | "z" ;
digit      = "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" ;
symbol     = "+" | "-" | "*" | "/" | "^" | "(" | ")" | "[" | "]" | "," ;
```

Lexical rules beyond the EBNF, each with its error:

- A number directly followed by a letter, a digit-less `.`, `_` or another `.` is `expr.number_form` («`2i`»:
  in Stan a number followed by `i` is an imaginary literal, checked online; «`1.`», «`.5`», «`1.2.3`»,
  «`1e`»). The message says how to write it (`2 * i`, `1.0`, `0.5`).
- A number that does not fit: more than `max_number_chars`, or infinite in double precision (Go's
  `strconv.ParseFloat` returns `ErrRange` for `1e400`, measured), or above `max_abs_literal` (1e15) in
  magnitude: `expr.number_range`. A number with a non-zero digit that becomes 0 (Go reads `1e-400` as 0 without
  an error, measured) is `expr.number_underflow`.
- An upper-case letter starts `expr.name_case` («names are lower case: `beta`»). A name longer than
  `max_name_bytes` is `expr.name_long`; one ending in `__` is `expr.name_reserved` (Stan reserves it, checked
  online).
- `//` and `/*` are `expr.comment`; `#` is `expr.hash` (comments and `#include` are not part of the language);
  `"` and `'` are `expr.text` (no texts); every other character outside the EBNF is `expr.hint` when it starts
  an entry of `language.hints` (the entry's message), `expr.symbol` otherwise.

`language.hints` (catalogue data; the message ids are this module's): `**` → «write `x ^ 2`»; `%`, `%/%` → not
in the language; `=`, `==`, `<-` → «an expression has no `=`: its name is the node's»; `;` → «one expression,
no statements»; `~` → «the distribution is chosen on the node»; `<`, `>`, `!`, `&`, `|`, `?`, `:` → «no
comparisons, conditions or ranges»; `.*`, `./`, `.^` → «write `*`, `/`, `^`: the editor chooses the elementwise
form»; `\` → no left division; `{`, `}` → no blocks.

`language.confusables` (catalogue data, a short list, never a Unicode-wide table): U+2212 `−`, U+2013 `–`,
U+2014 `—` → `-`; U+00D7 `×`, U+00B7 `·`, U+2219 `∙`, U+22C5 `⋅` → `*`; U+00F7 `÷`, U+2215 `∕` → `/`; U+00A0,
U+2009, U+202F (spaces) → a space; U+200B, U+200C, U+200D, U+FEFF (invisible) → nothing; U+FF10–U+FF19
(full-width digits) → `0`–`9`; U+0430 `а`, U+0435 `е`, U+043E `о`, U+0440 `р`, U+0441 `с`, U+0445 `х`,
U+0443 `у`, U+03BF `ο`, U+03B1 `α` → the Latin letter; U+202A–U+202E, U+2066–U+2069 (direction controls) →
«an invisible character that changes the direction of the text». Any other non-ASCII character: its code point
only.

### Syntax

```ebnf
input      = expression ;                                     (* whitespace allowed around every token *)
expression = sum ;
sum        = product , { ( "+" | "-" ) , product } ;          (* left-associative *)
product    = signed , { ( "*" | "/" ) , signed } ;            (* left-associative *)
signed     = "-" , signed | power ;                           (* unary minus *)
power      = atom , [ "^" , signed ] ;                        (* right-associative: a ^ b ^ c = a ^ (b ^ c) *)
atom       = number
           | name , "(" , expression , { "," , expression } , ")"     (* a call *)
           | name , "[" , name , "]"                                  (* indexing by a group index *)
           | name                                                     (* a node of the graph *)
           | "(" , expression , ")" ;
```

**Precedence, from the tightest:** `^` (right), unary `-`, `*` `/` (left), `+` `-` (left). So `-a ^ 2` is
`-(a ^ 2)`, `a ^ -b` is allowed, `-a * b` is `(-a) * b`, `a / b * c` is `(a / b) * c`. This is Stan's own order
(reference manual, «Operator precedence and associativity»: `+ -` 5 left, `* / .* ./` 4 left, unary `-` 2,
`^ .^` 1 right; checked online), so the canonical text a person reads (`Format`) means the same thing in both
languages; the emitter does not rely on it anyway (it parenthesizes everything).

Syntax errors: `expr.empty`, `expr.unexpected` (a token where another was expected, with what was expected:
an operator, a value, `)`), `expr.unclosed` (a `(` or `[` without its end), `expr.call_empty` (`f()`).
The parser stops at the **first** error.

### Limits (catalogue `limits`)

| Limit | Default | Checked | Error |
|---|---|---|---|
| `max_expression_bytes` | 1,000 | before the lexer, on the UTF-8 length | `expr.too_long` |
| `max_number_chars`, `max_abs_literal` | 32, 1e15 | by the lexer | `expr.number_range` |
| `max_name_bytes` | 40 | by the lexer | `expr.name_long` |
| `max_expression_nesting` | 32 | by the parser, **before** each recursive step (`signed`, `power`'s exponent, parentheses, a call's arguments, brackets) | `expr.too_deep` |
| `max_expression_nodes` | 256 | by the parser, at each node it creates | `expr.too_big` |
| `max_lag` | 60 | by the type checker | `expr.lag_k` |

With these, the parser's work and memory are linear in the input and bounded by constants: no input makes
it recurse past 32 levels or build more than 256 nodes, in Go or in the browser. A left-associative chain
(`a + b + c + …`) is a loop, not a recursion; its tree is at most 256 nodes deep, which bounds the recursive
walks after the parser.

## Types and shapes

### The types

| Type | Written in messages | What it is | Where it comes from |
|---|---|---|---|
| `real` | real number | one real | a literal; a scalar parameter, transformed node or real data |
| `int` | integer | one integer | scalar integer data |
| `real[P]` | one real per unit of P | a vector over plate P | a parameter, transformed node, latent state or real data on plate P |
| `int[P]` | one integer per unit of P | integer data over P | integer data on P |
| `index[P→G]` | the group G of each unit of P | for each unit of P, its unit of group plate G (1…J) | a group-index data node on P (rows), or a category's levels |

A `time` data node is not a number: naming it is `expr.time_value`.

### The environment (given by `model-graph`)

```go
type Env interface {
	Lookup(name string) (Symbol, bool) // a named node: its id, type, plate, role, declared interval
	Context() Plate                    // the plate of the slot's node ("" for a scalar node)
	Encloses(outer, inner Plate) bool  // reflexive; a group plate encloses its nested plates and the rows
	IsSeries(p Plate) bool             // the rows of a series
	IndexOf(target Plate) (string, bool) // the name of the group index whose target is `target`, for messages
}
```

### The rules (context C)

| Form | Accepted when | Type | Error otherwise |
|---|---|---|---|
| number | always | `real` | — |
| `x` | `x` is a named node; not an index; not a time | `x`'s element, scalar when `x` is scalar, over C when `x`'s plate is C | `expr.unknown_name`; `expr.index_alone` («`school` gives the school of each row: use it inside brackets, `mu[school]`»); `expr.needs_index` when `x`'s plate encloses C («`mu_school` is per school: write `mu_school[school]`», the index named by `IndexOf`); `expr.wrong_plate` otherwise («`score` is per pupil; this value is per school») |
| `x[g]` | `g` is an index `index[P→G]`; `x` is not an index and its plate is G; and either C = P, or G encloses C and C encloses P (the index is **lifted**: each unit of C belongs to one unit of G, which the runner checks in the data) | `x`'s element over C | `expr.not_index`, `expr.index_scalar`, `expr.index_target` («`mu_district` is per district, `school` gives schools»), `expr.index_context` |
| `f(e₁, …)` | `f` in `language.functions`, the arity right, every argument a number (not an index) | `real`, over C if any argument is | `expr.unknown_function`, `expr.not_function` (a node's name called), `expr.arity` |
| `lag(x, k)` | C is a series; `x` is a bare name of a node over C (data, parameter, transformed, latent state); `k` an integer literal (digits only) in 1…`max_lag` | `real[C]`; the AST records k | `expr.lag_not_series`, `expr.lag_argument`, `expr.lag_k` |
| `-e`, `e₁ op e₂` | numbers | `real`; over C if any operand is | — |

Because every non-scalar term is brought to the context plate C by these rules, two vectors that meet in an
operator are always over the same plate: there is no broadcasting rule to learn and none to get wrong.
Integers are numbers: `int` and `int[P]` may enter arithmetic and become real (the emitter converts them).

The **slot's expected type** (scalar, or over C) is checked by `model-graph`, not here.

### Intervals (for warnings, not refusals)

`Interval(ast)` gives the static interval of a value from the catalogue: a literal is [v, v]; a node gives
its declared interval (a parameter's range, a data node's, unbounded otherwise); a function gives the
interval of `language.functions`; `+`, `-`, `*` follow interval arithmetic; `/` by an interval that contains 0
is unbounded; `^` is (0, ∞) when the base is in (0, ∞), unbounded otherwise. `model-graph` uses it: a slot that
needs a positive value receives an interval inside (0, ∞) (fine), disjoint from it (an error), or overlapping it
(a warning: «Stan will reject a draw where `a - b` is not positive»). The arithmetic is IEEE double in the same
order in Go and TypeScript, so both give the same endpoints.

## Errors and positions

An error is `{code, pos, len, args}`. **`pos` and `len` count code points** of the input. Before the first
non-ASCII character every character is one byte and one UTF-16 unit, so Go's byte offsets, JavaScript's
string indices and code points agree on every error the corpus holds (the non-ASCII error is at the first
such character). Whatever the language, the message is chosen by `code` in five languages
(`frontend/i18n/locales/*.gen.ts`, namespace `bayes.expr`), with `args` (the name, the plate's label, the
character, its code point, the hint's id). Messages say what to write, in words for a reader who does not
know Stan (answer 1).

## The AST

```json
{"op": "+", "args": [{"name": "a"},
                     {"op": "*", "args": [{"name": "b"}, {"name": "x"}]}]}
{"num": "0.5"}
{"neg": {"name": "a"}}
{"call": "inv_logit", "args": [{"name": "eta"}]}
{"index": "mu_school", "by": "school"}
{"lag": "y", "k": 2}
```

`num` holds the shortest text that reads back to the same double (`Format`'s number form), not the person's
text. The JSON form is the same in Go and TypeScript; the corpus compares it key by key.

`Format(ast)` gives the canonical text: minimal parentheses by the precedence above, one space around each
binary operator, none after a unary minus, numbers in their shortest form. `Parse(Format(ast))` = `ast` for
every accepted input (a property test).

## Emission: Stan text from the typed AST

`Emit(ast, env) → string` is the only way an expression reaches a Stan program. `stan-codegen` calls it and
supplies an emission environment:

```go
type EmitEnv interface {
	Identifier(nodeID string) string               // the node's Stan identifier (answer 5: its name, or a generated one)
	IndexArray(index string, context Plate) string  // the data array of a (possibly lifted) group index
	LagArray(k int) string                          // the data array of the rows k steps before, in a series
	RowsSubset() string                             // the rows a lagged expression runs on, "" when none
}
```

| AST | Emitted |
|---|---|
| number v | `strconv.FormatFloat(v, 'g', -1, 64)`, with `.0` appended when it has neither `.` nor `e` (every literal is a real: `2` → `2.0`, `1e15` → `1e+15`, `1e-5` → `1e-05`, measured) |
| name | `Identifier(id)`; integer data over a plate inside arithmetic → `to_vector(id)` (Stan has `vector to_vector(array[] int a)`, checked online); a per-row name in a lagged expression → `id[subset]` |
| `x[g]` | `(x_id[array])`, the array from `IndexArray` |
| `lag(x, k)` | `(x_id[LagArray(k)])` |
| `f(e)` | `stan_name(e)`, the Stan name from the catalogue |
| `-e` | `(-e)` |
| `e₁ + e₂`, `e₁ - e₂` | `(e₁ + e₂)`, `(e₁ - e₂)` |
| `e₁ * e₂` | `(e₁ * e₂)` when at least one is a scalar; `(e₁ .* e₂)` when both are vectors |
| `e₁ / e₂` | `(e₁ / e₂)` when `e₂` is a scalar; `(e₁ ./ e₂)` when `e₂` is a vector (Stan has `vector operator/(vector, real)`, `operator./(real, vector)`, `operator./(vector, vector)`; checked online); when both are integers, `e₁` is emitted as `(1.0 * e₁)`: Stan's `int / int` is the integer quotient (functions reference, «deprecated in 2.24», still there; checked online) |
| `e₁ ^ e₂` | `(e₁ ^ e₂)` when both are scalars, `(e₁ .^ e₂)` otherwise (Stan has `.^` for (vector, real), (real, vector), (vector, vector); checked online) |

**The alphabet of the output.** Every emitted string matches `^[A-Za-z0-9_ .()\[\],+\-*/^]*$` and contains none
of `//`, `/*`, `*/`, `#`, `"`, `'`, `;`, `{`, `}`, `~`, `<`, `>`, `=`, a newline. Every binary operator is
surrounded by spaces, so `/` is never next to `/` or `*`. Every identifier comes from `EmitEnv` and is checked
against `^[A-Za-z][A-Za-z0-9_]*$` and not ending in `__` before it is written (a failure is a bug of the caller:
`Emit` returns an error, never a string). Every function name comes from the catalogue. Nothing of the
person's text is copied, not even a number's digits.

## Threats and how each is closed

| Threat | Closed by |
|---|---|
| **Injection into the Stan program** (a `;`, a `}` closing the model block, a statement, a `target +=`) | The grammar has no such tokens; the AST has no node that could hold them; `Emit` writes only from the AST, with the alphabet above (tested on every accepted input of the corpus and of the fuzzers) |
| **Reserved names** (`target`, `lower`, `model`, `normal_lpdf`, `a__`) | A name in an expression is only a key into the environment: it never reaches the program. The program's identifier comes from `stan-codegen` (answer 5: the person's name when it is not reserved, a generated one otherwise). A name ending in `__` is refused by the lexer |
| **Comments** (`//`, `/*`, `#`) hiding or ending code | Not tokens: refused with `expr.comment`, `expr.hash`; never emitted (spaces around every operator) |
| **Strings** | No string token; `"` and `'` refused |
| **`#include`** | `#` refused; `stan-codegen` never emits `#`, and stanc runs without include paths (map) |
| **Length** | `max_expression_bytes` before any reading |
| **Depth** (stack exhaustion in Go or in the browser) | `max_expression_nesting` before each recursive step; `max_expression_nodes` |
| **Huge or tiny numbers** (`1e400`, `9…9` × 1,000, `1e-400`) | `max_number_chars`, finite, `max_abs_literal`, no silent underflow; every literal re-printed from its double |
| **Confusable Unicode** (Cyrillic `а` for `a`, `−` for `-`, full-width digits, a non-breaking space, a direction control) | ASCII only, byte by byte; no Unicode-aware class in either implementation; the confusables named in the message |
| **Unknown identifiers** (a variable of the program that is not a node: `N`, `target`, a generated `V_…`) | Every name must be a named node of the graph (`Lookup`); a call's name must be a catalogue function; generated identifiers start with an upper-case letter, which the lexer refuses |
| **User-defined functions** | Not in the grammar (owner) |
| **Mirror drift** (the browser accepts what the gateway refuses, or the opposite) | The browser's verdict is never used: the gateway parses again on every save, validation and code generation; the shared corpus keeps the two equal for the person's sake |
| **Integer division** (`n / m` with two integers: 0 instead of 0.5) | Every literal is a real; two integer operands of `/` are emitted with `1.0 *` |
| **Wrong shapes** (a per-school value added to a per-pupil one by position) | The shape rules; no implicit broadcasting between plates |

What this module does **not** close, and who does: a model that is valid and meaningless (`log` of something
that can be negative: warnings from `Interval`, `model-graph`); a model too big to compile (the graph's limits,
`stan-compile`'s Job); hostile data (`bayes-runner`).

## The TypeScript mirror

`frontend/composables/bayesExpr.ts`: the same lexer, parser, type rules, intervals, `Format`, AST JSON and
error codes, reading the same catalogue copy (`~/assets/bayes-catalog.json`). It has **no emitter** (the
browser never produces Stan text). It is written with character codes (`charCodeAt`), never with regular
expression classes (`\s`, `\d`, `\w`, `/i`, `/u`). A lone UTF-16 surrogate is reported as U+FFFD, the
character Go's JSON decoder gives in its place, so the two report the same code point. Its answers feed the
editor's highlighting and messages as the person types; the gateway's answer on save replaces them.

## Tech stack

- **Go** 1.27 (`gateway-go/go.mod`; CI `golang:1.27.3-bookworm`): the standard library only (`strconv`,
  `encoding/json`, `testing` with native fuzzing); no parser generator, no new module in `go.mod`.
- **TypeScript** in the Nuxt 3.21 frontend, no new dependency; tests with `node --test` under Node 22.
- **Inputs**: the catalogue snapshot of `distribution-catalog` (functions, operators, hints, confusables,
  limits).

## Commands

```bash
cd gateway-go && go test ./internal/bayesexpr/...                       # unit, corpus, the fuzz seeds
cd gateway-go && BAYES_CORPUS_WRITE=1 go test ./internal/bayesexpr/ -run TestCorpus   # rewrites the expected results (reviewed by hand)
cd gateway-go && go test ./internal/bayesexpr/ -run '^$' -fuzz '^FuzzParse$' -fuzztime 10m    # by hand, one target at a time
cd gateway-go && go test ./internal/bayesexpr/ -run '^$' -fuzz '^FuzzEmit$' -fuzztime 10m
cd gateway-go && go test ./internal/bayesexpr/ -run '^$' -bench . -benchmem   # the largest inputs, recorded
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

A fuzz run is a heavy job for this machine's rule («one heavy job at a time»): never beside a build or a
suite.

## Project structure

```text
gateway-go/internal/bayesexpr/
  lex.go            the checks before reading, the tokens, the hints and confusables
  parse.go          the recursive descent with its guards
  ast.go            the nodes, their JSON form, Format
  check.go          the types against an Env
  interval.go       the static intervals
  emit.go           the Stan text from a typed AST, with its alphabet check
  errors.go         the codes
  *_test.go         per file; corpus_test.go; fuzz_test.go (FuzzParse, FuzzEmit); bench_test.go
  testdata/corpus/handwritten.json     inputs, environments, expected results (written by hand, reviewed)
  testdata/corpus/generated.json       2,000 seeded inputs and Go's results (written by BAYES_CORPUS_WRITE)
  testdata/fuzz/FuzzParse/, testdata/fuzz/FuzzEmit/   inputs the fuzzers found, kept as seeds
frontend/composables/bayesExpr.ts
frontend/tests/bayes-expr.test.mjs     reads ../gateway-go/internal/bayesexpr/testdata/corpus/*.json
```

## Code style

The reports' parser guards its recursion this way (verified, `backend/app/reports/expressions.py`):

```python
    def _nested(self, fn: Callable[[], Node]) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("the expression is nested too deeply", self.tok.pos, 1)
        try:
            return fn()
        finally:
            self.depth -= 1
```

The same guard in this module's Go (a sketch), with an error value instead of an exception and the count of
nodes beside it:

```go
// nested runs one recursive step of the grammar, refused before it starts when the input is
// already as deep as the catalogue allows: no input can exhaust the stack.
func (p *parser) nested(step func() (*Node, error)) (*Node, error) {
	if p.depth == p.limits.MaxNesting {
		return nil, p.errorAt(p.tok, CodeTooDeep, nil)
	}
	p.depth++
	defer func() { p.depth-- }()
	return step()
}
```

## Testing strategy

All inputs are short strings: the rule «small data» is met by construction.

- **Unit** (Go and TypeScript alike): each EBNF rule, each precedence and associativity case, each lexical
  error with its position, each hint and confusable, each type rule and each of its errors, the lift of an
  index through nested plates, `lag` in and out of a series, the intervals, `Format`, the AST JSON.
- **The handwritten corpus** (`handwritten.json`, at least 300 cases): every code of «Errors» at least twice,
  every row of the type rules, every row of the emission table (Go only), the threats' table as inputs (`a;
  target += 1`, `a } model {`, `a // b`, `a /* b */`, `#include x`, `"a"`, `print(a)`, `reject(a)`, `target`,
  `x__`, `V_n1`, `N`, `2i`, `1e400`, `1e-400`, 10,000 digits, 1,001 bytes, 33 nested parentheses, 257 nodes,
  `-` × 40, `а + b` with a Cyrillic `а`, `a − b` with U+2212, `a + b`, `a‮+ b`, a lone surrogate).
  Each case: input, environment (named nodes, context, plates), expected `{ok, ast, format}` or
  `{code, pos, len, args}`.
- **The generated corpus** (`generated.json`): 2,000 inputs from a seeded generator in the Go test — half
  grammar-driven (valid trees, rendered with random spacing and parentheses), half mutations of the
  handwritten inputs (a byte inserted, deleted or replaced, from an alphabet weighted towards the symbols, the
  hints and the confusables) — with Go's results. `go test` regenerates it in memory and fails when the
  committed file differs (rewritten only with `BAYES_CORPUS_WRITE=1`, reviewed).
- **Mirror equality** (`bayes-expr.test.mjs`): on both corpora, the TypeScript result equals Go's: the same
  acceptance, code, position, length, args, AST JSON and `Format`. A difference fails the suite.
- **Fuzzing (Go native)**:
  - `FuzzParse(input string)`: never panics; returns within the limits; when it accepts, the tree has at most
    `max_expression_nodes` nodes, `Parse(Format(ast))` equals `ast`, and the check against a fixed synthetic
    environment either types it or gives a code of «Errors»;
  - `FuzzEmit(seed []byte)`: a grammar-driven tree built from the fuzzer's bytes, typed against a synthetic
    environment, emitted with an `EmitEnv` that returns `V_<n>` identifiers: the output matches the alphabet,
    contains none of the forbidden sequences, has balanced parentheses, and names only catalogue functions;
  - both run their seeds (the corpora and `testdata/fuzz/`) in every `go test`; a 10-minute run of each is a
    lot's acceptance, by hand, and any input they find is kept as a seed.
- **Stan's side** (handed to `stan-codegen`): every emitted expression of its golden corpus compiles with the
  pinned stanc; that is where the emission table's «checked online» becomes «run».
- **Benchmarks**: the largest inputs the limits allow (1,000 bytes, 32 levels, 256 nodes) parsed and typed;
  times and allocations recorded in «As built», not asserted.

## Security review

The owner asked for a dedicated review (answer 3). It is run by a fresh agent (opus), given the module's
code, this spec's grammar and threats table, and the catalogue — **not** this spec's conclusions — with the
task of producing an input that makes `Emit` write anything outside its alphabet, a name that reaches the
program, or a parse that exhausts time or memory. It runs when lots E0–E2 are done, **before** `stan-codegen`
calls `Emit`; its findings are corrected and recorded in this spec; the map's step 8 repeats it on the whole
editor.

## Boundaries

- **Always**: the gateway parses again whatever the browser sends; Stan text only from `Emit`; the limits
  checked before the work; bytes compared, never Unicode classes; every error code in five languages; the
  corpus updated with every rule and every bug.
- **Ask first**: a new operator, function or syntactic form; a change of a limit's default; an integer
  literal anywhere but `lag`; any dependency.
- **Never**: user-defined functions (owner); strings, comments, statements, conditionals or loops in the
  language; the person's text copied into a program; a regular expression class over the input; trusting the
  mirror's verdict.

## Success criteria

1. Every case of the handwritten corpus gives its expected result in Go; the TypeScript mirror equals Go on
   every case of both corpora.
2. `go test ./internal/bayesexpr/...` is green with the fuzz seeds; a 10-minute run of each fuzz target finds
   nothing (or what it found is corrected and kept as a seed).
3. For every accepted input of the corpora and the fuzzers, `Emit` writes only the alphabet and none of the
   forbidden sequences, and `Parse(Format(ast))` = `ast`.
4. Inputs one past each limit are refused with their codes, and the over-long one before the lexer runs (a test
   counts the lexer's calls).
5. No input of the corpora or the fuzzers makes Go panic or the mirror throw anything but its own error.
6. The security review is done and its findings closed before `stan-codegen` uses `Emit`.
7. No new module in `go.mod` and no new package in `package.json` for this module.

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **E0** | Lexer, parser, AST, `Format`, errors, limits; the handwritten corpus's syntax cases; `FuzzParse` | `lex.go`, `parse.go`, `ast.go`, `errors.go`, tests, `testdata/` | unit and corpus green; 10 minutes of `FuzzParse` | **opus** |
| **E1** | Types, environment, intervals; the corpus's type cases | `check.go`, `interval.go`, tests | green | sonnet |
| **E2** | `Emit` and its alphabet; `FuzzEmit`; the generated corpus | `emit.go`, `fuzz_test.go`, `corpus_test.go`, `generated.json` | green; 10 minutes of `FuzzEmit` | **opus** |
| **E3** | The TypeScript mirror, the messages in five languages | `frontend/composables/bayesExpr.ts`, `frontend/tests/bayes-expr.test.mjs`, the locales (`bayes.expr`) | mirror equality on both corpora; `vue-tsc` green | sonnet |
| **E4** | The security review and its corrections | this spec's «As built» | findings closed | **opus** (fresh context) |

E0 first (it needs the catalogue's lot C0); E1 and E3's lexer and parser in parallel after it; E2 after E1;
E4 last. Fuzz runs one at a time, never beside a build or a suite.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| `lag` in the language, or an autoregressive node | **`lag(x, k)` in the language**: AR(p) is one likelihood with `phi1 * lag(y, 1) + phi2 * lag(y, 2)` | Owner, 2026-11-23 |
| The limits: catalogue constants or administrator settings | **Documented constants of the catalogue** (the run's limits stay settings) | Owner, 2026-11-23 |
| Numbers | **Every literal is a real**; only `lag`'s `k` is an integer: Stan's integer division cannot be written by accident | Spec decision, 2026-11-23, owner may revisit |
| The size of an expression | **1,000 bytes and 256 nodes** (a sum of about 60 terms); longer linear predictors belong in the graph's linear-predictor node | Spec decision, 2026-11-23, owner may revisit |

## Open questions

None.
